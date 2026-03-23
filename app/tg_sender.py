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
from aiogram.types import BufferedInputFile
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

    # Сохраняем связку ID: TG ↔ MAX
    max_msg_id = task.get("max_msg_id")
    if max_msg_id:
        await save_mapping(sent.message_id, max_msg_id)

    print(f"[TG SENDER] Отправлено msg_id={sent.message_id}")


async def _do_edit(bot: Bot, task: dict) -> None:
    """Отредактировать существующее сообщение в TG."""
    tg_msg_id = task.get("tg_msg_id")
    if not tg_msg_id:
        return

    await bot.edit_message_text(
        chat_id=task["chat_id"],
        message_id=tg_msg_id,
        text=task["text"],
    )
    print(f"[TG SENDER] Отредактировано msg_id={tg_msg_id}")


async def _do_media(bot: Bot, task: dict) -> None:
    """Скачать файл из MAX и отправить в TG как фото/документ."""
    import httpx
    from media import download_max_file

    media_url = task.get("media_url", "")
    media_type = task.get("media_type", "document")
    caption = task.get("text", "")

    if not media_url:
        # Нет URL — фолбэк на текст
        await _do_send(bot, task)
        return

    # Скачиваем файл из MAX
    result = await download_max_file(media_url, task.get("media_name"))
    if not result:
        # Не удалось скачать — фолбэк на текст
        await _do_send(bot, task)
        return

    file_data, file_name = result

    # Общие параметры
    kwargs = {"chat_id": task["chat_id"], "caption": caption}
    if task.get("reply_to"):
        kwargs["reply_to_message_id"] = task["reply_to"]
    if task.get("thread_id"):
        kwargs["message_thread_id"] = task["thread_id"]

    # Отправляем как фото или документ
    input_file = BufferedInputFile(file_data, filename=file_name)

    if media_type == "photo":
        sent = await bot.send_photo(**kwargs, photo=input_file)
    else:
        sent = await bot.send_document(**kwargs, document=input_file)

    del file_data  # освобождаем память

    # Сохраняем mapping
    max_msg_id = task.get("max_msg_id")
    if max_msg_id:
        await save_mapping(sent.message_id, max_msg_id)

    print(f"[TG SENDER] Медиа отправлено msg_id={sent.message_id}")
