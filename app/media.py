# media.py — работа с медиа-файлами
#
# TG → MAX: скачиваем из TG → получаем upload URL от MAX → загружаем → отправляем
# MAX → TG: скачиваем из MAX → отправляем в TG через aiogram
#
# Ограничения по ТЗ:
#   - Фото, документы и видео до 20 МБ — пересылаем
#   - Больше 20 МБ — заглушка (текст с именем и размером)
#   - Голосовые, видео-кружки — игнор

import io
import httpx
from aiogram import types as tg_types
from config import MAX_BOT_TOKEN, MAX_API_URL

MAX_FILE_LIMIT = 20 * 1024 * 1024  # 20 МБ в байтах


def format_size(size_bytes: int) -> str:
    """Человекочитаемый размер файла: 1234567 → '1.2 МБ'."""
    if size_bytes < 1024:
        return f"{size_bytes} Б"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} КБ"
    else:
        return f"{size_bytes / (1024 * 1024):.1f} МБ"


# ─────────────────────────────────────────────
#  TG → MAX: скачать из Telegram, загрузить в MAX
# ─────────────────────────────────────────────

async def download_tg_file(bot, file_id: str, original_name: str = None) -> tuple[bytes, str]:
    """Скачать файл из Telegram по file_id.

    Возвращает (байты файла, имя файла).
    original_name — оригинальное имя из message.document.file_name
    """
    file = await bot.get_file(file_id)
    file_path = file.file_path

    buf = io.BytesIO()
    await bot.download_file(file_path, buf)
    data = buf.getvalue()
    buf.close()

    # Используем оригинальное имя если есть, иначе из пути
    name = original_name or (file_path.split("/")[-1] if file_path else "file")
    return data, name


async def upload_to_max(file_data: bytes, file_name: str, upload_type: str = "file") -> str | None:
    """Загрузить файл в MAX и получить токен.

    Шаги:
    1. POST /uploads?type=file → получаем URL для загрузки
    2. POST на этот URL с файлом → получаем токен
    3. Возвращаем токен для прикрепления к сообщению

    upload_type: "image" для фото, "file" для документов и видео
    """
    async with httpx.AsyncClient(timeout=60.0) as http:
        # Шаг 1: получаем URL для загрузки
        resp = await http.post(
            f"{MAX_API_URL}/uploads",
            params={"type": upload_type},
            headers={"Authorization": MAX_BOT_TOKEN},
        )
        resp.raise_for_status()
        upload_url = resp.json().get("url")
        if not upload_url:
            print("[MEDIA] Не получил upload URL от MAX")
            return None

        # Шаг 2: загружаем файл
        files = {"data": (file_name, file_data)}
        upload_resp = await http.post(
            upload_url,
            files=files,
            headers={"Authorization": MAX_BOT_TOKEN},
        )
        upload_resp.raise_for_status()

        # Ответ содержит информацию о загруженном файле
        result = upload_resp.json()

        # MAX возвращает разные структуры для разных типов:
        # file:  {"token": "..."}
        # image: {"photos": {"HASH_KEY": {"token": "..."}}}
        if "token" in result:
            return result["token"]

        # Для image: photos — словарь, где ключ=хеш, значение={"token": "..."}
        photos = result.get("photos")
        if photos and isinstance(photos, dict):
            for key, val in photos.items():
                if isinstance(val, dict) and "token" in val:
                    return val["token"]

        # Пробуем найти token в любом вложенном объекте
        for key, val in result.items():
            if isinstance(val, dict) and "token" in val:
                return val["token"]

        print(f"[MEDIA] Не нашёл токен в ответе MAX: {result}")
        return None


async def send_media_to_max(chat_id: int, file_data: bytes, file_name: str,
                            caption: str, upload_type: str = "file") -> str | None:
    """Загрузить файл в MAX и отправить сообщение с вложением.

    Возвращает mid отправленного сообщения.
    """
    token = await upload_to_max(file_data, file_name, upload_type)
    if not token:
        return None

    # Формируем вложение
    attachment = {
        "type": upload_type,
        "payload": {"token": token},
    }

    # Отправляем сообщение с вложением
    async with httpx.AsyncClient(timeout=30.0) as http:
        # Пауза перед отправкой — MAX может не успеть обработать файл
        import asyncio
        await asyncio.sleep(1)

        for attempt in range(3):
            try:
                resp = await http.post(
                    f"{MAX_API_URL}/messages",
                    params={"chat_id": chat_id},
                    headers={"Authorization": MAX_BOT_TOKEN},
                    json={
                        "text": caption,
                        "attachments": [attachment],
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                return data.get("message", {}).get("body", {}).get("mid")
            except httpx.HTTPStatusError as e:
                if "attachment.not.ready" in e.response.text:
                    # Файл ещё обрабатывается — ждём и повторяем
                    await asyncio.sleep(2 * (attempt + 1))
                    continue
                print(f"[MEDIA] Ошибка отправки в MAX: {e.response.text}")
                return None
            except Exception as e:
                print(f"[MEDIA] Ошибка: {e}")
                return None

    return None


# ─────────────────────────────────────────────
#  MAX → TG: скачать из MAX
# ─────────────────────────────────────────────

async def download_max_file(url: str, original_name: str = None) -> tuple[bytes, str] | None:
    """Скачать файл из MAX по URL.

    Возвращает (байты файла, имя файла) или None при ошибке.
    original_name — имя из attachment metadata (MAX URL часто не содержит имени)
    """
    try:
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as http:
            resp = await http.get(url)
            resp.raise_for_status()
            data = resp.content

            # Используем оригинальное имя если есть
            if original_name:
                name = original_name
            else:
                name = url.split("/")[-1].split("?")[0]
                if not name or len(name) > 100 or "." not in name:
                    name = "file"

            return data, name
    except Exception as e:
        print(f"[MEDIA] Ошибка скачивания из MAX: {e}")
        return None


# ─────────────────────────────────────────────
#  Вспомогательные: определение типа медиа из TG
# ─────────────────────────────────────────────

def get_tg_media_info(message: tg_types.Message) -> dict | None:
    """Определить тип медиа в сообщении Telegram.

    Возвращает dict с ключами:
        type: "photo" | "document" | "video" | "voice" | "video_note" | None
        file_id: str
        file_size: int (в байтах, 0 если неизвестно)
        file_name: str
    Или None если медиа нет.
    """
    if message.photo:
        # Telegram шлёт фото в нескольких размерах — берём самый большой
        photo = message.photo[-1]
        return {
            "type": "photo",
            "file_id": photo.file_id,
            "file_size": photo.file_size or 0,
            "file_name": "photo.jpg",
        }

    if message.document:
        return {
            "type": "document",
            "file_id": message.document.file_id,
            "file_size": message.document.file_size or 0,
            "file_name": message.document.file_name or "document",
        }

    if message.video:
        # Видео пересылаем как файл (не как видео-плеер)
        return {
            "type": "video",
            "file_id": message.video.file_id,
            "file_size": message.video.file_size or 0,
            "file_name": message.video.file_name or "video.mp4",
        }

    # Игнорируемые типы
    if message.voice:
        return {"type": "voice", "file_id": "", "file_size": 0, "file_name": "voice"}
    if message.video_note:
        return {"type": "video_note", "file_id": "", "file_size": 0, "file_name": "video_note"}

    return None


def get_max_media_info(attachments: list) -> dict | None:
    """Определить тип медиа во вложениях MAX-сообщения.

    Возвращает dict с ключами:
        type: "image" | "file" | "video" | "audio" | etc
        url: str (ссылка для скачивания)
        file_name: str
        file_size: int
    Или None если нет поддерживаемых вложений.
    """
    if not attachments:
        return None

    for att in attachments:
        att_type = att.get("type")

        if att_type == "image":
            payload = att.get("payload", {})
            url = payload.get("url", "")
            return {
                "type": "image",
                "url": url,
                "file_name": "image.jpg",
                "file_size": payload.get("file_size", 0),
            }

        if att_type == "file":
            payload = att.get("payload", {})
            url = payload.get("url", "")
            # MAX кладёт filename и size на верхний уровень, а не в payload
            return {
                "type": "file",
                "url": url,
                "file_name": att.get("filename") or payload.get("filename", "file"),
                "file_size": att.get("size") or payload.get("file_size", 0),
            }

        if att_type == "video":
            # Видео из MAX — пересылаем как файл
            payload = att.get("payload", {})
            url = payload.get("url", "")
            return {
                "type": "video",
                "url": url,
                "file_name": att.get("filename") or "video.mp4",
                "file_size": att.get("size") or payload.get("file_size", 0),
            }

        # Игнорируемые типы
        if att_type == "audio":
            return {"type": "audio", "url": "", "file_name": "", "file_size": 0}

    return None
