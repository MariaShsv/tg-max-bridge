# mapping.py — связки ID сообщений в Redis
# Когда сообщение из TG отправлено в MAX, сохраняем:
#   TG:86 → MAX:mid.123   и   MAX:mid.123 → TG:86
# Через 2 часа записи сами удалятся (TTL)

import redis.asyncio as redis
from config import REDIS_URL, MSG_TTL

# Подключаемся к Redis (Memurai на вашем компьютере)
# decode_responses=True — чтобы Redis отдавал обычные строки, а не байты
pool = redis.from_url(REDIS_URL, decode_responses=True)


async def save_mapping(tg_msg_id: int, max_msg_id: str) -> None:
    """Сохранить связку: TG-сообщение ↔ MAX-сообщение."""
    # Создаём два ключа — чтобы искать в обе стороны
    tg_key = f"TG:{tg_msg_id}"       # например "TG:86"
    max_key = f"MAX:{max_msg_id}"     # например "MAX:mid.123456"

    # Записываем оба ключа с временем жизни 7200 секунд (2 часа)
    await pool.set(tg_key, max_msg_id, ex=MSG_TTL)
    await pool.set(max_key, str(tg_msg_id), ex=MSG_TTL)


async def get_max_id(tg_msg_id: int) -> str | None:
    """По ID сообщения из TG найти парное ID в MAX."""
    return await pool.get(f"TG:{tg_msg_id}")


async def get_tg_id(max_msg_id: str) -> int | None:
    """По ID сообщения из MAX найти парное ID в TG."""
    result = await pool.get(f"MAX:{max_msg_id}")
    return int(result) if result else None


async def delete_mapping(tg_msg_id: int = None, max_msg_id: str = None) -> None:
    """Удалить связку (при удалении сообщения)."""
    if tg_msg_id:
        # Сначала находим парный ключ, потом удаляем оба
        max_id = await pool.get(f"TG:{tg_msg_id}")
        await pool.delete(f"TG:{tg_msg_id}")
        if max_id:
            await pool.delete(f"MAX:{max_id}")
    if max_msg_id:
        tg_id = await pool.get(f"MAX:{max_msg_id}")
        await pool.delete(f"MAX:{max_msg_id}")
        if tg_id:
            await pool.delete(f"TG:{tg_id}")


async def is_processed(update_id: int | str) -> bool:
    """Проверяем: мы уже обработали это событие?
    Защита от дублей — если Telegram/MAX пришлёт одно и то же дважды.
    """
    key = f"processed:{update_id}"
    # set с nx=True записывает ТОЛЬКО если ключа ещё нет
    # Если записал — значит первый раз видим (возвращает True → «новое»)
    # Если не записал — значит уже было (возвращает None → «дубль»)
    result = await pool.set(key, "1", ex=60, nx=True)
    return result is None  # True = уже обработано, False = новое


# --- Кэш названий топиков ---
# Топик создаётся редко, а сообщения идут постоянно.
# Поэтому сохраняем название один раз и используем долго.

async def save_topic_name(chat_id: int, thread_id: int, name: str) -> None:
    """Сохранить название топика в Redis (без TTL — живёт пока Redis работает)."""
    key = f"topic:{chat_id}:{thread_id}"
    await pool.set(key, name)


async def get_topic_name(chat_id: int, thread_id: int) -> str | None:
    """Получить название топика из кэша."""
    key = f"topic:{chat_id}:{thread_id}"
    return await pool.get(key)


# --- Маркер MAX (для восстановления после перезапуска) ---
# MAX отдаёт обновления с позиции marker.
# Если бот упал, при перезапуске нужно знать, где остановились.
# Сохраняем marker в Redis — он переживёт перезапуск.

async def save_max_marker(marker: int) -> None:
    """Сохранить маркер MAX в Redis (без TTL — живёт всегда)."""
    await pool.set("max:marker", str(marker))


async def get_max_marker() -> int | None:
    """Загрузить маркер MAX из Redis.
    Возвращает число или None (если бот запускается впервые).
    """
    result = await pool.get("max:marker")
    return int(result) if result else None
