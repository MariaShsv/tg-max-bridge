# main.py — точка входа: запускает мост в обе стороны
#
# Работают параллельно:
#   1. aiogram polling  — слушает Telegram
#   2. MAX polling      — слушает MAX
#   3. TG sender worker — отправляет в TG с rate limit

import asyncio
from aiogram import Bot, Dispatcher
from config import TG_BOT_TOKEN
from tg_handler import router as tg_router
from commands import router as cmd_router
from tg_sender import sender_worker
from max_handler import poll_max
from max_sender import close_client


async def main():
    """Запуск бота."""

    bot = Bot(token=TG_BOT_TOKEN)
    dp = Dispatcher()
    dp.include_router(cmd_router)    # команды ПЕРВЫМИ — чтобы /status не ушёл в tg_handler
    dp.include_router(tg_router)

    # Запускаем фоновые задачи
    worker_task = asyncio.create_task(sender_worker(bot))
    max_poll_task = asyncio.create_task(poll_max())

    print("=" * 50)
    print("🚀 Мост TG ↔ MAX запущен")
    print("📡 TG polling: слушаю Telegram")
    print("📡 MAX polling: слушаю MAX")
    print("📤 TG sender: воркер с rate limit (3.5 сек)")
    print("🔄 Режим: восстановление пропущенных сообщений включено")
    print("🛑 Для остановки нажмите Ctrl+C")
    print("=" * 50)

    try:
        await dp.start_polling(bot, skip_updates=False)
    finally:
        worker_task.cancel()
        max_poll_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass
        try:
            await max_poll_task
        except asyncio.CancelledError:
            pass
        await close_client()
        await bot.session.close()
        print("\n👋 Бот остановлен")


if __name__ == "__main__":
    asyncio.run(main())
