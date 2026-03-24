# tg_handler.py — обработка событий из Telegram
# Когда кто-то пишет в TG-группу → форматируем → отправляем в MAX
#
# Альбомы (несколько фото одним постом):
#   Telegram присылает каждое фото отдельным сообщением с одинаковым media_group_id.
#   Мы копим их в буфер 2 секунды, потом отправляем в MAX одним сообщением.

import asyncio
from aiogram import Router, types, F, Bot
from config import TG_GROUP_ID, MAX_GROUP_ID, ADMIN_IDS
from formatter import format_tg_to_max, format_quote, get_display_name_tg
from max_sender import send_text as max_send_text, edit_text as max_edit_text, send_album
from media import (
    get_tg_media_info, download_tg_file, send_media_to_max,
    upload_to_max, format_size, MAX_FILE_LIMIT,
)
from tg_sender import enqueue_message
from mapping import (
    save_mapping, is_processed, save_topic_name, get_topic_name, get_max_id
)

router = Router(name="tg_handler")

# --- Буфер альбомов ---
# Ключ: media_group_id (строка вида "12345678901234")
# Значение: список сообщений из этого альбома
_album_buffer: dict[str, list] = {}

# Таймеры: через 2 секунды после последнего фото — отправляем альбом
_album_timers: dict[str, asyncio.Task] = {}

ALBUM_WAIT_SECONDS = 2.0  # ждём 2 секунды чтобы собрать все фото альбома


# ─────────────────────────────────────────────
#  Вспомогательные функции
# ─────────────────────────────────────────────

async def resolve_topic_name(message: types.Message) -> str | None:
    """Узнать название топика из которого пришло сообщение."""
    thread_id = message.message_thread_id
    if not thread_id:
        return None

    cached = await get_topic_name(message.chat.id, thread_id)
    if cached:
        return cached

    reply = message.reply_to_message
    if reply and reply.forum_topic_created:
        name = reply.forum_topic_created.name
        await save_topic_name(message.chat.id, thread_id, name)
        print(f"[TOPIC] Закэшировал: #{thread_id} = «{name}»")
        return name

    return None


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


# ─────────────────────────────────────────────
#  Обработчики топиков
# ─────────────────────────────────────────────

@router.message(F.chat.id == TG_GROUP_ID, F.forum_topic_created)
async def handle_topic_created(message: types.Message) -> None:
    """Кто-то создал новый топик — запоминаем его название."""
    name = message.forum_topic_created.name
    thread_id = message.message_thread_id
    if thread_id and name:
        await save_topic_name(message.chat.id, thread_id, name)
        print(f"[TOPIC] Создан топик: #{thread_id} = «{name}»")


@router.message(F.chat.id == TG_GROUP_ID, F.forum_topic_edited)
async def handle_topic_edited(message: types.Message) -> None:
    """Кто-то переименовал топик — обновляем кэш."""
    thread_id = message.message_thread_id
    if thread_id and message.forum_topic_edited.name:
        name = message.forum_topic_edited.name
        await save_topic_name(message.chat.id, thread_id, name)
        print(f"[TOPIC] Переименован топик: #{thread_id} → «{name}»")


# ─────────────────────────────────────────────
#  Логика альбомов
# ─────────────────────────────────────────────

async def _buffer_album_message(message: types.Message, bot: Bot) -> None:
    """Добавить фото в буфер альбома и (пере)запустить таймер сброса."""
    gid = message.media_group_id

    # Добавляем сообщение в буфер
    if gid not in _album_buffer:
        _album_buffer[gid] = []
    _album_buffer[gid].append(message)

    # Отменяем старый таймер если есть (пришло ещё одно фото — ждём заново)
    old_task = _album_timers.get(gid)
    if old_task and not old_task.done():
        old_task.cancel()

    # Запускаем новый таймер
    _album_timers[gid] = asyncio.create_task(
        _flush_album_after_delay(gid, bot)
    )


async def _flush_album_after_delay(gid: str, bot: Bot) -> None:
    """Подождать ALBUM_WAIT_SECONDS, потом отправить альбом."""
    try:
        await asyncio.sleep(ALBUM_WAIT_SECONDS)
        await _flush_album(gid, bot)
    except asyncio.CancelledError:
        pass  # таймер отменили (пришло ещё фото) — это нормально


async def _flush_album(gid: str, bot: Bot) -> None:
    """Отправить накопленный альбом в MAX одним сообщением."""

    messages = _album_buffer.pop(gid, [])
    _album_timers.pop(gid, None)

    if not messages:
        return

    # Сортируем по ID чтобы фото шли в правильном порядке
    messages.sort(key=lambda m: m.message_id)
    first = messages[0]

    if not first.from_user:
        return

    # Определяем топик
    topic_name = await resolve_topic_name(first)
    if first.message_thread_id and not topic_name:
        topic_name = f"#{first.message_thread_id}"

    # Подпись: берём из первого сообщения у которого есть caption
    caption_text = ""
    for m in messages:
        if m.caption or m.text:
            caption_text = m.caption or m.text or ""
            break

    formatted = format_tg_to_max(first.from_user, caption_text, topic_name)

    # Reply: смотрим только на первое сообщение альбома
    reply_to_max_mid = None
    reply_msg = first.reply_to_message
    if reply_msg and reply_msg.message_id:
        is_topic_root = reply_msg.forum_topic_created is not None
        if not is_topic_root:
            reply_to_max_mid = await get_max_id(reply_msg.message_id)
            if not reply_to_max_mid:
                original_text = reply_msg.text or ""
                quote = format_quote(original_text)
                if quote:
                    formatted = quote + formatted

    # Загружаем все фото в MAX и собираем токены
    tokens = []
    upload_failed = False

    for msg in messages:
        media_info = get_tg_media_info(msg)
        if not media_info or media_info["type"] not in ("photo", "document", "video"):
            continue

        file_size = media_info["file_size"]
        if file_size > MAX_FILE_LIMIT:
            # Хотя бы одно фото слишком большое — падаем на текстовую заглушку
            size_str = format_size(file_size)
            formatted += f"\n📎 {media_info['file_name']} ({size_str})"
            upload_failed = True
            break

        try:
            file_data, dl_name = await download_tg_file(
                bot, media_info["file_id"], media_info["file_name"]
            )
            upload_type = "image" if media_info["type"] == "photo" else "file"
            token = await upload_to_max(file_data, dl_name, upload_type)
            del file_data  # сразу освобождаем память

            if token:
                tokens.append({"type": upload_type, "token": token})
            else:
                print(f"[TG→MAX] Альбом: не получили токен для фото, пропускаем")
                upload_failed = True
                break

        except Exception as e:
            print(f"[TG→MAX] Альбом: ошибка загрузки фото: {e}")
            upload_failed = True
            break

    # Отправляем: если токены есть и всё загрузилось — альбомом, иначе — текстом
    if tokens and not upload_failed:
        max_msg_id = await send_album(
            chat_id=MAX_GROUP_ID,
            tokens=tokens,
            caption=formatted,
            reply_to=reply_to_max_mid,
        )
        log_action = f"Альбом ({len(tokens)} фото)"
    else:
        # Фолбэк: хотя бы текст с атрибуцией дойдёт
        max_msg_id = await max_send_text(
            chat_id=MAX_GROUP_ID, text=formatted, reply_to=reply_to_max_mid,
        )
        log_action = f"Альбом→текст (ошибка загрузки)"

    if max_msg_id:
        # Привязываем первый TG message_id к MAX mid
        await save_mapping(first.message_id, max_msg_id)
        topic_label = f" [{topic_name}]" if topic_name else ""
        print(f"[TG→MAX] {log_action}{topic_label} {first.from_user.first_name}: {caption_text[:50]}")
    else:
        print(f"[TG→MAX] ОШИБКА отправки альбома в MAX")


# ─────────────────────────────────────────────
#  Основной обработчик сообщений
# ─────────────────────────────────────────────

@router.message(F.chat.id == TG_GROUP_ID)
async def handle_tg_message(message: types.Message, bot: Bot) -> None:
    """Обработать новое сообщение из TG-группы."""

    if message.from_user and message.from_user.is_bot:
        return

    if await is_processed(f"tg:{message.message_id}"):
        return

    # Альбом: несколько фото одним постом — складываем в буфер
    if message.media_group_id:
        await _buffer_album_message(message, bot)
        return

    # Дальше — обычные одиночные сообщения

    media_info = get_tg_media_info(message)
    text = message.text or message.caption or ""

    if not text and not media_info:
        return

    # Игнорируемые типы: голосовые, видео-кружки
    if media_info and media_info["type"] in ("voice", "video_note"):
        return

    topic_name = await resolve_topic_name(message)
    if message.message_thread_id and not topic_name:
        topic_name = f"#{message.message_thread_id}"

    formatted = format_tg_to_max(message.from_user, text, topic_name)

    # Reply
    reply_to_max_mid = None
    reply_msg = message.reply_to_message

    if reply_msg and reply_msg.message_id:
        is_topic_root = reply_msg.forum_topic_created is not None
        if not is_topic_root:
            reply_to_max_mid = await get_max_id(reply_msg.message_id)
            if not reply_to_max_mid:
                original_text = reply_msg.text or ""
                quote = format_quote(original_text)
                if quote:
                    formatted = quote + formatted

    # Медиа: фото, документ, видео
    max_msg_id = None

    if media_info and media_info["type"] in ("photo", "document", "video"):
        file_size = media_info["file_size"]
        file_name = media_info["file_name"]

        if file_size > MAX_FILE_LIMIT:
            # Файл слишком большой — заглушка + уведомление админу
            size_str = format_size(file_size)
            formatted += f"\n📎 {file_name} ({size_str})"
            max_msg_id = await max_send_text(
                chat_id=MAX_GROUP_ID, text=formatted, reply_to=reply_to_max_mid,
            )
            sender_name = get_display_name_tg(message.from_user)
            await notify_admin_large_file(sender_name, file_name, file_size, "TG")
        else:
            try:
                file_data, dl_name = await download_tg_file(
                    bot, media_info["file_id"], media_info["file_name"]
                )
                upload_type = "image" if media_info["type"] == "photo" else "file"
                max_msg_id = await send_media_to_max(
                    chat_id=MAX_GROUP_ID,
                    file_data=file_data,
                    file_name=dl_name,
                    caption=formatted,
                    upload_type=upload_type,
                )
                del file_data
            except Exception as e:
                print(f"[TG→MAX] Ошибка медиа: {e}")
                max_msg_id = await max_send_text(
                    chat_id=MAX_GROUP_ID, text=formatted, reply_to=reply_to_max_mid,
                )
    else:
        max_msg_id = await max_send_text(
            chat_id=MAX_GROUP_ID, text=formatted, reply_to=reply_to_max_mid,
        )

    if max_msg_id:
        await save_mapping(message.message_id, max_msg_id)
        topic_label = f" [{topic_name}]" if topic_name else ""
        log_text = (text or "[медиа]")[:50]
        print(f"[TG→MAX]{topic_label} {message.from_user.first_name}: {log_text}")
    else:
        print(f"[TG→MAX] ОШИБКА отправки в MAX")


# ─────────────────────────────────────────────
#  Обработчик редактирования
# ─────────────────────────────────────────────

@router.edited_message(F.chat.id == TG_GROUP_ID)
async def handle_tg_edit(message: types.Message) -> None:
    """Сообщение отредактировали в TG → редактируем зеркало в MAX."""

    if message.from_user and message.from_user.is_bot:
        return

    text = message.text or message.caption or ""
    if not text:
        return

    max_mid = await get_max_id(message.message_id)
    if not max_mid:
        print(f"[TG→MAX] Edit: пара не найдена для msg_id={message.message_id}, игнор")
        return

    topic_name = await resolve_topic_name(message)
    if message.message_thread_id and not topic_name:
        topic_name = f"#{message.message_thread_id}"

    formatted = format_tg_to_max(message.from_user, text, topic_name)

    success = await max_edit_text(max_mid, formatted)
    if success:
        print(f"[TG→MAX] Edit: {message.from_user.first_name}: {text[:50]}")
    else:
        print(f"[TG→MAX] Edit: ОШИБКА редактирования в MAX")
