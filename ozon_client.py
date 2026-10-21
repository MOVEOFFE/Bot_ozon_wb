import asyncio
import logging
from typing import List, Dict, Any, Tuple
import aiohttp


class OzonAPIError(Exception):
    def __init__(self, message: str, status_code: int = 0):
        super().__init__(message)
        self.status_code = status_code


class OzonClient:
    BASE_URL = "https://api-seller.ozon.ru"

    def __init__(self, client_id: str, api_key: str):
        self.client_id = client_id.strip()
        self.api_key = api_key.strip()
        self.headers = {
            "Client-Id": self.client_id,
            "Api-Key": self.api_key,
            "Content-Type": "application/json"
        }

    async def verify_credentials(self) -> Tuple[bool, str]:
        url = f"{self.BASE_URL}/v1/review/list"
        payload = {"limit": 20}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=self.headers, json=payload, timeout=15) as resp:
                    if resp.status == 200:
                        return True, "Успешное подключение к Ozon"
                    if resp.status == 401:
                        return False, "Неверный Client-Id или Api-Key Ozon."
                    if resp.status == 429:
                        return False, "Превышен лимит запросов Ozon (429)."
                    text = await resp.text()
                    return False, f"Ошибка Ozon ({resp.status}): {text[:150]}"
        except Exception as e:
            return False, f"Сетевая ошибка Ozon: {e}"

    async def get_unprocessed_reviews(self, limit: int = 50, last_id: str = "") -> Dict[str, Any]:
        url = f"{self.BASE_URL}/v1/review/list"
        payload = {
            "limit": max(20, min(limit, 100)),
            "sort_dir": "DESC",
            "status": "UNPROCESSED"
        }
        if last_id:
            payload["last_id"] = last_id

        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=self.headers, json=payload, timeout=30) as resp:
                if resp.status == 429:
                    raise OzonAPIError("Лимит запросов Ozon превышен (429)", status_code=429)

                if resp.status != 200:
                    payload.pop("status", None)
                    async with session.post(url, headers=self.headers, json=payload, timeout=30) as retry_resp:
                        if retry_resp.status != 200:
                            err_text = await retry_resp.text()
                            raise OzonAPIError(f"Ошибка Ozon ({retry_resp.status}): {err_text[:150]}")
                        data = await retry_resp.json()
                else:
                    data = await resp.json()

                reviews = data.get("reviews") or data.get("result", {}).get("reviews", [])
                last_id_val = data.get("last_id") or data.get("result", {}).get("last_id", "")
                has_next_val = data.get("has_next") or data.get("result", {}).get("has_next", False)

                return {
                    "reviews": reviews,
                    "last_id": last_id_val,
                    "has_next": has_next_val
                }

    async def get_product_names(self, skus: List[int]) -> Dict[Any, str]:
        if not skus:
            return {}

        clean_skus = list(set(int(s) for s in skus if s))
        names_map = {}

        url_v3 = f"{self.BASE_URL}/v3/product/info/list"
        payload_v3 = {"sku": clean_skus}

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url_v3, headers=self.headers, json=payload_v3, timeout=15) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        items = data.get("items") or data.get("result", {}).get("items", [])
                        for item in items:
                            sku = item.get("sku")
                            name = item.get("name")
                            if sku and name:
                                names_map[int(sku)] = name
                                names_map[str(sku)] = name
                        if names_map:
                            return names_map
                    else:
                        err_text = await resp.text()
                        logging.warning(f"Ozon v3 get_product_names warning ({resp.status}): {err_text[:200]}")
        except Exception as e:
            logging.warning(f"Ozon v3 get_product_names exception: {e}")

        url_v2 = f"{self.BASE_URL}/v2/product/info/list"
        payload_v2 = {"sku": clean_skus}

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url_v2, headers=self.headers, json=payload_v2, timeout=15) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        items = data.get("result", {}).get("items", []) or data.get("items", [])
                        for item in items:
                            sku = item.get("sku")
                            name = item.get("name")
                            if sku and name:
                                names_map[int(sku)] = name
                                names_map[str(sku)] = name
                    else:
                        err_text = await resp.text()
                        logging.error(f"Ozon v2 get_product_names error ({resp.status}): {err_text[:200]}")
        except Exception as e:
            logging.error(f"Ozon v2 get_product_names exception: {e}")

        return names_map

    async def publish_reply(self, review_id: str, text: str):
        url = f"{self.BASE_URL}/v1/review/comment/create"
        payload = {
            "review_id": review_id,
            "text": text
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=self.headers, json=payload, timeout=30) as resp:
                if resp.status == 429:
                    raise OzonAPIError("Лимит запросов Ozon при отправке ответа (429)", status_code=429)

                if resp.status not in (200, 201, 204):
                    err_text = await resp.text()
                    raise OzonAPIError(
                        f"Ошибка публикации ответа Ozon ({resp.status}): {err_text[:200]}",
                        status_code=resp.status
                    )
