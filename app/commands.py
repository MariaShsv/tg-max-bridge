# commands.py — команды бота
#
# /status — показать состояние моста (только в личке боту)

import time
from aiogram import Router, types, F
from tg_sender import get_queue_size

router = Router(name="commands")

# Время запуска бота — запоминаем при импорте модуля
_start_time = time.time()


def format_uptime() -> str:
    """Посчитать сколько бот работает, вернуть красивую строку."""
    seconds = int(time.time() - _start_time)

    days = seconds // 86400
    hours = (seconds % 86400) // 3600
    minutes = (seconds % 3600) // 60

    parts = []
    if days:
        parts.append(f"{days}д")
    if hours:
        parts.append(f"{hours}ч")
    parts.append(f"{minutes}мин")

    return " ".join(parts)


@router.message(F.text == "/status", F.chat.type == "private")
async def cmd_status(message: types.Message) -> None:
    """Команда /status — показать состояние моста. Только в личке."""

    uptime = format_uptime()
    queue = await get_queue_size()

    text = (
        f"🟢 Мост TG ↔ MAX работает\n"
        f"⏱ Uptime: {uptime}\n"
        f"📤 Очередь в TG: {queue} сообщений"
    )

    await message.answer(text)
