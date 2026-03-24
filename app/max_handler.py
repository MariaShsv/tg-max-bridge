# max_handler.py — обработка событий из MAX
# Опрашиваем MAX API через GET /updates (long polling),
# форматируем сообщения и кладём в очередь на отправку в TG

import asyncio
import httpx
from config import MAX_BOT_TOKEN, MAX_API_URL, MAX_GROUP_ID, TG_GROUP_ID, TG_TOPIC_ID, ADMIN_IDS
from formatter import format_max_to_tg, format_quote, get_display_name_max
from tg_sender import enqueue_message
from media import get_max_media_info, format_size, MAX_FILE_LIMIT
from mapping import save_mapping, get_tg_id, is_processed, save_max_marker, get_max_marker

_marker: int | None = None


def get_all_images(attachments: list) -> list[dict]:
    """Вернуть список всех изображений из вложений MAX.
    Каждый элемент: {"url": "...", "file_size": 0}
    """
    images = []
    for att in attachments:
        if att.get("type") == "image":
            payload = att.get("payload", {})
            url = payload.get("url", "")
            if url:
                images.append({
                    "url": url,
                    "file_size": payload.get("file_size", 0),
                })
    return images


async def notify_admin_large_file(sender_name: str, file_name: str,
                                  file_size: int, source: str) -> None:
    """Уведомить админа в личку о большом файле."""
    size_str = format_size(file_size)
    if source == "TG":
        where = "в TG-группе"
        action = "Перекиньте вручную в MAX"
    else:
        where = "в MAX-группе"
        action = "Перекиньте вручную в TG"

    text = (
        f"📦 Большой файл {where}!\n"
        f"От: {sender_name}\n"
        f"Файл: {file_name} ({size_str})\n"
        f"{action}"
    )

    for admin_id in ADMIN_IDS:
        await enqueue_message(
            chat_id=admin_id,
            text=text,
        )


async def poll_max() -> None:
    global _marker

    saved_marker = await get_max_marker()
    if saved_marker is not None:
        _marker = saved_marker
        print(f"[MAX POLL] Восстановил маркер из Redis: {_marker}")
        print(f"[MAX POLL] Сообщения за время простоя будут обработаны")
    else:
        print(f"[MAX POLL] Первый запуск — маркера нет, начинаю с текущего момента")

    print("[MAX POLL] Запущен, слушаю события из MAX...")

    async with httpx.AsyncClient(timeout=60.0) as http:

        try:
            subs = await http.get(
                f"{MAX_API_URL}/subscriptions",
                headers={"Authorization": MAX_BOT_TOKEN},
            )
            if subs.status_code == 200:
                for sub in subs.json().get("subscriptions", []):
                    sub_url = sub.get("url", "")
                    await http.delete(
                        f"{MAX_API_URL}/subscriptions",
                        params={"url": sub_url},
                        headers={"Authorization": MAX_BOT_TOKEN},
                    )
                    print(f"[MAX POLL] Удалил webhook: {sub_url}")
        except Exception as e:
            print(f"[MAX POLL] Не удалось проверить подписки: {e}")

        while True:
            try:
                params = {
                    "timeout": 30,
                    "limit": 100,
                    "types": "message_created,message_edited",
                }
                if _marker is not None:
                    params["marker"] = _marker

                resp = await http.get(
                    f"{MAX_API_URL}/updates",
                    params=params,
                    headers={"Authorization": MAX_BOT_TOKEN},
                )
                resp.raise_for_status()
                data = resp.json()

                new_marker = data.get("marker")
                if new_marker is not None:
                    _marker = new_marker
                    await save_max_marker(_marker)

                for update in data.get("updates", []):
                    await handle_update(update)

            except httpx.ReadTimeout:
                continue

            except asyncio.CancelledError:
                print("[MAX POLL] Остановлен")
                break

            except Exception as e:
                print(f"[MAX POLL] Ошибка: {e}")
                await asyncio.sleep(3)


async def handle_update(update: dict) -> None:
    update_type = update.get("update_type")

    if update_type == "message_created":
        await handle_message_created(update)
    elif update_type == "message_edited":
        await handle_message_edited(update)


async def handle_message_created(update: dict) -> None:
    """Новое сообщение в MAX → переслать в TG."""

    message = update.get("message", {})
    body = message.get("body", {})
    sender = message.get("sender", {})
    recipient = message.get("recipient", {})

    chat_id = recipient.get("chat_id")
    if chat_id != MAX_GROUP_ID:
        return

    if sender.get("is_bot"):
        return

    mid = body.get("mid", "")
    if await is_processed(f"max:{mid}"):
        return

    text = body.get("text") or ""
    attachments = body.get("attachments", [])
    media_info = get_max_media_info(attachments)

    if not text and not media_info:
        return

    # Игнорируемые типы: аудио
    if media_info and media_info["type"] in ("audio",):
        return

    formatted = format_max_to_tg(sender, text)

    # Reply
    reply_to_tg_id = None
    link = message.get("link")
    if link and link.get("type") == "reply":
        original_mid = link.get("message", {}).get("mid")
        if original_mid:
            reply_to_tg_id = await get_tg_id(original_mid)

            if not reply_to_tg_id:
                original_text = link.get("message", {}).get("body", {}).get("text", "")
                quote = format_quote(original_text)
                if quote:
                    formatted = quote + formatted

    # Альбом: несколько фото в одном сообщении MAX
    all_images = get_all_images(attachments)
    if len(all_images) > 1:
        # Собираем все URL через запятую — передаём в очередь
        urls = ",".join(img["url"] for img in all_images)
        await enqueue_message(
            chat_id=TG_GROUP_ID,
            text=formatted,
            reply_to=reply_to_tg_id,
            message_thread_id=TG_TOPIC_ID,
            max_msg_id=mid,
            action="media_group",
            media_url=urls,
        )
        name = sender.get("name", "?")
        print(f"[MAX→TG] Альбом ({len(all_images)} фото) {name}: {text[:50]}")
        return

    # Одиночное медиа: фото, файл или видео
    if media_info and media_info["type"] in ("image", "file", "video"):
        file_size = media_info.get("file_size", 0)
        file_url = media_info.get("url", "")

        if file_size > MAX_FILE_LIMIT:
            # Заглушка + уведомление админу
            size_str = format_size(file_size)
            file_name = media_info.get("file_name", "файл")
            formatted += f"\n📎 {file_name} ({size_str})"
            await enqueue_message(
                chat_id=TG_GROUP_ID, text=formatted,
                reply_to=reply_to_tg_id, message_thread_id=TG_TOPIC_ID,
                max_msg_id=mid,
            )
            sender_name = get_display_name_max(sender)
            await notify_admin_large_file(sender_name, file_name, file_size, "MAX")
        elif file_url:
            media_type = "photo" if media_info["type"] == "image" else "document"
            await enqueue_message(
                chat_id=TG_GROUP_ID, text=formatted,
                reply_to=reply_to_tg_id, message_thread_id=TG_TOPIC_ID,
                max_msg_id=mid, action="media",
                media_url=file_url, media_type=media_type,
                media_name=media_info.get("file_name"),
            )
        else:
            await enqueue_message(
                chat_id=TG_GROUP_ID, text=formatted,
                reply_to=reply_to_tg_id, message_thread_id=TG_TOPIC_ID,
                max_msg_id=mid,
            )
    else:
        await enqueue_message(
            chat_id=TG_GROUP_ID, text=formatted,
            reply_to=reply_to_tg_id, message_thread_id=TG_TOPIC_ID,
            max_msg_id=mid,
        )

    name = sender.get("name", "?")
    print(f"[MAX→TG] {name}: {text[:50]}")


async def handle_message_edited(update: dict) -> None:
    """Сообщение отредактировали в MAX → редактировать зеркало в TG."""

    message = update.get("message", {})
    body = message.get("body", {})
    sender = message.get("sender", {})
    recipient = message.get("recipient", {})

    chat_id = recipient.get("chat_id")
    if chat_id != MAX_GROUP_ID:
        return

    if sender.get("is_bot"):
        return

    mid = body.get("mid", "")
    text = body.get("text")
    if not text:
        return

    tg_msg_id = await get_tg_id(mid)
    if not tg_msg_id:
        print(f"[MAX→TG] Edit: пара не найдена для mid={mid}, игнор")
        return

    formatted = format_max_to_tg(sender, text)

    await enqueue_message(
        chat_id=TG_GROUP_ID,
        text=formatted,
        action="edit",
        tg_msg_id=tg_msg_id,
    )

    name = sender.get("name", "?")
    print(f"[MAX→TG] Edit: {name}: {text[:50]}")
