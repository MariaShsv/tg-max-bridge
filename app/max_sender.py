# max_sender.py — отправка сообщений в MAX
# MAX не имеет Python-библиотеки, поэтому используем httpx (HTTP-запросы)

import httpx
from config import MAX_BOT_TOKEN, MAX_API_URL, MAX_GROUP_ID

# Клиент для HTTP-запросов (создаём один раз, используем везде)
client: httpx.AsyncClient | None = None


async def get_client() -> httpx.AsyncClient:
    """Получить HTTP-клиент (создаёт при первом вызове)."""
    global client
    if client is None:
        client = httpx.AsyncClient(timeout=30.0)
    return client


async def close_client() -> None:
    """Закрыть HTTP-клиент при остановке бота."""
    global client
    if client:
        await client.aclose()
        client = None


async def send_text(chat_id: int, text: str, reply_to: str = None) -> str | None:
    """Отправить текстовое сообщение в MAX.

    Аргументы:
        chat_id:  ID чата в MAX (наша группа)
        text:     текст для отправки
        reply_to: mid сообщения для ответа (опционально)

    Возвращает:
        mid отправленного сообщения (или None если ошибка)
    """
    http = await get_client()

    payload = {
        "text": text,
    }

    if reply_to:
        payload["link"] = {
            "type": "reply",
            "mid": reply_to,
        }

    try:
        resp = await http.post(
            f"{MAX_API_URL}/messages",
            params={"chat_id": chat_id},
            headers={"Authorization": MAX_BOT_TOKEN},
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("message", {}).get("body", {}).get("mid")
    except httpx.HTTPStatusError as e:
        print(f"[MAX SEND ERROR] HTTP {e.response.status_code}: {e.response.text}")
        return None
    except Exception as e:
        print(f"[MAX SEND ERROR] {e}")
        return None


async def send_album(chat_id: int, tokens: list[dict], caption: str,
                     reply_to: str = None) -> str | None:
    """Отправить альбом (несколько фото/файлов) в MAX одним сообщением.

    Аргументы:
        chat_id:  ID чата в MAX
        tokens:   список токенов вида [{"type": "image", "token": "abc..."}, ...]
                  type может быть "image" или "file"
        caption:  подпись к альбому (атрибуция автора + текст)
        reply_to: mid сообщения для ответа (опционально)

    Возвращает:
        mid отправленного сообщения (или None если ошибка)
    """
    http = await get_client()

    # Формируем список вложений из токенов
    # MAX API принимает: [{"type": "image", "payload": {"token": "..."}}, ...]
    attachments = [
        {
            "type": t["type"],
            "payload": {"token": t["token"]},
        }
        for t in tokens
    ]

    payload = {
        "text": caption,
        "attachments": attachments,
    }

    if reply_to:
        payload["link"] = {
            "type": "reply",
            "mid": reply_to,
        }

    try:
        resp = await http.post(
            f"{MAX_API_URL}/messages",
            params={"chat_id": chat_id},
            headers={"Authorization": MAX_BOT_TOKEN},
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()
        mid = data.get("message", {}).get("body", {}).get("mid")
        print(f"[MAX ALBUM] Отправлено {len(tokens)} вложений, mid={mid}")
        return mid
    except httpx.HTTPStatusError as e:
        print(f"[MAX ALBUM ERROR] HTTP {e.response.status_code}: {e.response.text}")
        return None
    except Exception as e:
        print(f"[MAX ALBUM ERROR] {e}")
        return None


async def edit_text(message_id: str, text: str) -> bool:
    """Отредактировать сообщение в MAX."""
    http = await get_client()
    try:
        resp = await http.put(
            f"{MAX_API_URL}/messages",
            params={"message_id": message_id},
            headers={"Authorization": MAX_BOT_TOKEN},
            json={"text": text},
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        print(f"[MAX EDIT ERROR] {e}")
        return False


async def delete_message(message_id: str) -> bool:
    """Удалить сообщение в MAX."""
    http = await get_client()
    try:
        resp = await http.delete(
            f"{MAX_API_URL}/messages",
            params={"message_id": message_id},
            headers={"Authorization": MAX_BOT_TOKEN},
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        print(f"[MAX DELETE ERROR] {e}")
        return False
