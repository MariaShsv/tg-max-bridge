# reset.py — одноразовый скрипт: сбросить очереди TG и MAX
#
# Запустите ОДИН РАЗ перед первым запуском бота.
# Он обнулит накопившиеся сообщения, чтобы старые не пересылались.
# После этого запускайте main.py — бот начнёт с чистого листа.

import asyncio
import httpx
import redis.asyncio as aioredis
from config import TG_BOT_TOKEN, MAX_BOT_TOKEN, MAX_API_URL, REDIS_URL

TG_API = f"https://api.telegram.org/bot{TG_BOT_TOKEN}"


async def reset_all():
    print("=" * 50)
    print("🧹 Сброс очередей — начинаю...")
    print("=" * 50)

    # --- 1. Сбрасываем очередь Telegram ---
    print("\n📱 Telegram: сбрасываю накопившиеся сообщения...")
    async with httpx.AsyncClient(timeout=10.0) as http:
        try:
            # getUpdates с offset=-1 помечает все старые обновления как прочитанные
            resp = await http.get(f"{TG_API}/getUpdates", params={"offset": -1, "timeout": 1})
            data = resp.json()
            updates = data.get("result", [])
            if updates:
                # Ставим offset на последнее обновление + 1 — всё старое сброшено
                last_id = updates[-1]["update_id"]
                await http.get(f"{TG_API}/getUpdates", params={"offset": last_id + 1, "timeout": 1})
                print(f"   ✅ Сброшено! Последний update_id: {last_id}")
            else:
                print(f"   ✅ Очередь уже пуста")
        except Exception as e:
            print(f"   ❌ Ошибка: {e}")

    # --- 2. Сбрасываем маркер MAX ---
    print("\n💬 MAX: получаю текущую позицию...")
    async with httpx.AsyncClient(timeout=10.0) as http:
        try:
            # Делаем один запрос к MAX, чтобы узнать текущий marker
            resp = await http.get(
                f"{MAX_API_URL}/updates",
                params={"timeout": 1, "limit": 1, "types": "message_created"},
                headers={"Authorization": MAX_BOT_TOKEN},
            )
            data = resp.json()
            marker = data.get("marker")
            if marker is not None:
                # Сохраняем маркер в Redis — бот начнёт ПОСЛЕ этой точки
                r = aioredis.from_url(REDIS_URL, decode_responses=True)
                await r.set("max:marker", str(marker))
                await r.aclose()
                print(f"   ✅ Маркер сохранён в Redis: {marker}")
                print(f"   Все старые сообщения MAX будут пропущены")
            else:
                print(f"   ⚠️ MAX не вернул маркер (возможно нет сообщений)")
        except Exception as e:
            print(f"   ❌ Ошибка: {e}")

    # --- 3. Чистим очередь отправки в TG (на случай если там что-то осталось) ---
    print("\n📤 Redis: чищу очередь отправки в TG...")
    try:
        r = aioredis.from_url(REDIS_URL, decode_responses=True)
        deleted = await r.delete("tg_send_queue")
        await r.aclose()
        if deleted:
            print(f"   ✅ Очередь tg_send_queue очищена")
        else:
            print(f"   ✅ Очередь уже пуста")
    except Exception as e:
        print(f"   ❌ Ошибка: {e}")

    print("\n" + "=" * 50)
    print("✅ Готово! Теперь запускайте бота: python main.py")
    print("=" * 50)


if __name__ == "__main__":
    asyncio.run(reset_all())
