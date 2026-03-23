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

    # Тело запроса — только текст и (опционально) ссылка на reply
    payload = {
        "text": text,
    }

    # Если это ответ на конкретное сообщение — добавляем ссылку
    if reply_to:
        payload["link"] = {
            "type": "reply",
            "mid": reply_to,
        }

    # Отправляем POST-запрос в MAX API
    # Токен — в заголовке Authorization
    # chat_id — в query-параметре URL
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
