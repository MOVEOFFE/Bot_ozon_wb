import os
import uuid
import base64
import logging
import aiohttp
from typing import Optional, Dict, Any

YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID", "")
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY", "")

API_URL = "https://api.yookassa.ru/v3/payments"


def get_auth_headers() -> Dict[str, str]:
    credentials = f"{YOOKASSA_SHOP_ID}:{YOOKASSA_SECRET_KEY}"
    encoded = base64.b64encode(credentials.encode()).decode()
    return {
        "Authorization": f"Basic {encoded}",
        "Content-Type": "application/json"
    }


async def create_payment(
    amount_rub: int,
    description: str,
    metadata: dict,
    return_url: str = "https://t.me/vellio_bot"
) -> Optional[Dict[str, Any]]:
    if not YOOKASSA_SHOP_ID or not YOOKASSA_SECRET_KEY:
        logging.error("YOOKASSA_SHOP_ID или YOOKASSA_SECRET_KEY не заданы в переменных окружения.")
        return None

    idempotence_key = str(uuid.uuid4())
    headers = get_auth_headers()
    headers["Idempotence-Key"] = idempotence_key

    payload = {
        "amount": {
            "value": f"{amount_rub}.00",
            "currency": "RUB"
        },
        "confirmation": {
            "type": "redirect",
            "return_url": return_url
        },
        "capture": True,
        "description": description[:128],
        "metadata": metadata
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(API_URL, json=payload, headers=headers, timeout=15) as resp:
                data = await resp.json()
                if resp.status in [200, 201]:
                    return {
                        "payment_id": data.get("id"),
                        "payment_url": data.get("confirmation", {}).get("confirmation_url"),
                        "status": data.get("status")
                    }
                else:
                    logging.error(f"Ошибка создания платежа ЮKassa ({resp.status}): {data}")
                    return None
    except Exception as e:
        logging.error(f"Исключение при запросе к ЮKassa: {e}")
        return None


async def get_payment_status(payment_id: str) -> Optional[Dict[str, Any]]:
    if not YOOKASSA_SHOP_ID or not YOOKASSA_SECRET_KEY:
        return None

    headers = get_auth_headers()
    url = f"{API_URL}/{payment_id}"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=10) as resp:
                if resp.status == 200:
                    return await resp.json()
                else:
                    data = await resp.json()
                    logging.error(f"Ошибка проверки статуса ЮKassa ({resp.status}): {data}")
                    return None
    except Exception as e:
        logging.error(f"Ошибка проверки платежа {payment_id}: {e}")
        return None