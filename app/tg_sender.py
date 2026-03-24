# tg_sender.py — отправка сообщений в Telegram через очередь
#
# Зачем очередь: Telegram банит ботов за слишком частые сообщения.
# Поэтому мы не отправляем сразу, а кладём в очередь Redis,
# и фоновый воркер достаёт по одному каждые 3.5 секунды.

import asyncio
import io
import json
import redis.asyncio as redis
from aiogram import Bot
from aiogram.types import BufferedInputFile, InputMediaPhoto
from aiogram.exceptions import TelegramRetryAfter
from config import REDIS_URL, TG_SEND_INTERVAL
from mapping import save_mapping

pool = redis.from_url(REDIS_URL, decode_responses=True)
QUEUE_KEY = "tg_send_queue"


async def enqueue_message(
    chat_id: int,
    text: str,
    reply_to: int | None = None,
    message_thread_id: int | None = None,
    max_msg_id: str | None = None,
    action: str = "send",
    tg_msg_id: int | None = None,
    media_url: str | None = None,
    media_type: str | None = None,
    media_name: str | None = None,
) -> None:
    """Положить задачу в очередь."""
    task = json.dumps({
        "action": action,
        "chat_id": chat_id,
        "text": text,
        "reply_to": reply_to,
        "thread_id": message_thread_id,
        "max_msg_id": max_msg_id,
        "tg_msg_id": tg_msg_id,
        "media_url": media_url,
        "media_type": media_type,
        "media_name": media_name,
    })
    await pool.rpush(QUEUE_KEY, task)


async def get_queue_size() -> int:
    """Размер очереди (для команды /status)."""
    return await pool.llen(QUEUE_KEY)


async def sender_worker(bot: Bot) -> None:
    """Фоновый воркер: достаёт задачи из очереди и выполняет.

    Между действиями — пауза 3.5 сек.
    Если Telegram вернул 429 — ждёт сколько сказали + 1 сек.
    """
    print("[TG SENDER] Воркер запущен, интервал:", TG_SEND_INTERVAL, "сек")

    while True:
        try:
            result = await pool.blpop(QUEUE_KEY, timeout=1)
            if result is None:
                continue

            task = json.loads(result[1])
            action = task.get("action", "send")

            if action == "send":
                await _do_send(bot, task)
            elif action == "edit":
                await _do_edit(bot, task)
            elif action == "media":
                await _do_media(bot, task)
            elif action == "media_group":
                await _do_media_group(bot, task)

            await asyncio.sleep(TG_SEND_INTERVAL)

        except TelegramRetryAfter as e:
            wait = e.retry_after + 1
            print(f"[TG SENDER] Rate limit! Жду {wait} сек...")
            await asyncio.sleep(wait)

        except asyncio.CancelledError:
            print("[TG SENDER] Воркер остановлен")
            break

        except Exception as e:
            print(f"[TG SENDER] Ошибка: {e}")
            await asyncio.sleep(1)


async def _do_send(bot: Bot, task: dict) -> None:
    """Отправить новое сообщение в TG."""
    kwargs = {
        "chat_id": task["chat_id"],
        "text": task["text"],
    }
    if task.get("reply_to"):
        kwargs["reply_to_message_id"] = task["reply_to"]
    if task.get("thread_id"):
        kwargs["message_thread_id"] = task["thread_id"]

    sent = await bot.send_message(**kwargs)

    max_msg_id = task.get("max_msg_id")
    if max_msg_id:
        await save_mapping(sent.message_id, max_msg_id)

    print(f"[TG SENDER] Отправлено msg_id={sent.message_id}")


async def _do_edit(bot: Bot, task: dict) -> None:
    """Отредактировать существующее сообщение в TG.
    Пробуем edit_message_text, если не вышло — edit_message_caption (для фото).
    """
    tg_msg_id = task.get("tg_msg_id")
    if not tg_msg_id:
        return

    try:
        await bot.edit_message_text(
            chat_id=task["chat_id"],
            message_id=tg_msg_id,
            text=task["text"],
        )
    except Exception:
        try:
            await bot.edit_message_caption(
                chat_id=task["chat_id"],
                message_id=tg_msg_id,
                caption=task["text"],
            )
        except Exception as e:
            print(f"[TG SENDER] Edit ошибка: {e}")
            return
    print(f"[TG SENDER] Отредактировано msg_id={tg_msg_id}")


async def _do_media(bot: Bot, task: dict) -> None:
    """Скачать файл из MAX и отправить в TG как фото/документ."""
    from media import download_max_file

    media_url = task.get("media_url", "")
    media_type = task.get("media_type", "document")
    caption = task.get("text", "")

    if not media_url:
        await _do_send(bot, task)
        return

    result = await download_max_file(media_url, task.get("media_name"))
    if not result:
        await _do_send(bot, task)
        return

    file_data, file_name = result

    kwargs = {"chat_id": task["chat_id"], "caption": caption}
    if task.get("reply_to"):
        kwargs["reply_to_message_id"] = task["reply_to"]
    if task.get("thread_id"):
        kwargs["message_thread_id"] = task["thread_id"]

    input_file = BufferedInputFile(file_data, filename=file_name)

    if media_type == "photo":
        sent = await bot.send_photo(**kwargs, photo=input_file)
    else:
        sent = await bot.send_document(**kwargs, document=input_file)

    del file_data

    max_msg_id = task.get("max_msg_id")
    if max_msg_id:
        await save_mapping(sent.message_id, max_msg_id)

    print(f"[TG SENDER] Медиа отправлено msg_id={sent.message_id}")


async def _do_media_group(bot: Bot, task: dict) -> None:
    """Скачать несколько фото из MAX и отправить в TG как альбом.

    media_url содержит несколько URL через запятую:
    "https://url1,https://url2,https://url3"
    """
    from media import download_max_file

    urls_raw = task.get("media_url", "")
    caption = task.get("text", "")

    if not urls_raw:
        await _do_send(bot, task)
        return

    urls = urls_raw.split(",")

    # Скачиваем все фото
    media_group = []
    all_data = []

    for i, url in enumerate(urls):
        url = url.strip()
        if not url:
            continue

        result = await download_max_file(url)
        if not result:
            print(f"[TG SENDER] Альбом: не удалось скачать фото {i+1}, пропускаем")
            continue

        file_data, file_name = result
        all_data.append(file_data)

        input_file = BufferedInputFile(file_data, filename=file_name)

        # Подпись только к первому фото
        if i == 0:
            media_group.append(InputMediaPhoto(media=input_file, caption=caption))
        else:
            media_group.append(InputMediaPhoto(media=input_file))

    if not media_group:
        # Не удалось скачать ни одного — отправляем текст
        await _do_send(bot, task)
        return

    if len(media_group) == 1:
        # Одно фото — отправляем обычно
        kwargs = {"chat_id": task["chat_id"], "caption": caption}
        if task.get("reply_to"):
            kwargs["reply_to_message_id"] = task["reply_to"]
        if task.get("thread_id"):
            kwargs["message_thread_id"] = task["thread_id"]
        sent = await bot.send_photo(**kwargs, photo=media_group[0].media)
        tg_id = sent.message_id
    else:
        # Несколько фото — отправляем альбомом
        kwargs = {"chat_id": task["chat_id"], "media": media_group}
        if task.get("reply_to"):
            kwargs["reply_to_message_id"] = task["reply_to"]
        if task.get("thread_id"):
            kwargs["message_thread_id"] = task["thread_id"]

        sent_messages = await bot.send_media_group(**kwargs)
        tg_id = sent_messages[0].message_id

    # Освобождаем память
    for data in all_data:
        del data

    max_msg_id = task.get("max_msg_id")
    if max_msg_id:
        await save_mapping(tg_id, max_msg_id)

    print(f"[TG SENDER] Альбом ({len(media_group)} фото) отправлен msg_id={tg_id}")
