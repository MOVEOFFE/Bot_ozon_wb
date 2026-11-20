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
            "Content-Type": "application/json",
        }

    async def verify_credentials(self) -> Tuple[bool, str]:
        url = f"{self.BASE_URL}/v1/review/list"
        payload = {"limit": 20}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, headers=self.headers, json=payload, timeout=aiohttp.ClientTimeout(total=15)
                ) as resp:
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

    async def get_unprocessed_reviews(self, limit: int = 100, offset: int = 0) -> Dict[str, Any]:
        """
        Получение списка новых (неотвеченных) отзывов с сортировкой от свежих к старым.
        """
        url = f"{self.BASE_URL}/v1/review/list"
        payload = {
            "limit": max(20, min(limit, 100)),
            "offset": max(0, offset),
            "sort_dir": "DESC",
            "status": "UNPROCESSED",
        }

        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=self.headers, json=payload, timeout=timeout) as resp:
                if resp.status == 429:
                    raise OzonAPIError("Лимит запросов Ozon превышен (429)", status_code=429)

                if resp.status != 200:
                    # Fallback только для ошибки валидации параметра status.
                    if resp.status != 400:
                        err_text = await resp.text()
                        raise OzonAPIError(
                            f"Ошибка Ozon ({resp.status}): {err_text[:150]}",
                            status_code=resp.status,
                        )
                    payload.pop("status", None)
                    async with session.post(
                        url, headers=self.headers, json=payload, timeout=timeout
                    ) as retry_resp:
                        if retry_resp.status == 429:
                            raise OzonAPIError("Лимит запросов Ozon превышен (429)", status_code=429)
                        if retry_resp.status != 200:
                            err_text = await retry_resp.text()
                            raise OzonAPIError(
                                f"Ошибка Ozon ({retry_resp.status}): {err_text[:150]}",
                                status_code=retry_resp.status,
                            )
                        data = await retry_resp.json()
                else:
                    data = await resp.json()

                result_block = data.get("result") or {}
                reviews = data.get("reviews") if "reviews" in data else result_block.get("reviews", [])
                reviews = reviews or []
                try:
                    total = int(data.get("count") or result_block.get("count") or len(reviews))
                except (TypeError, ValueError):
                    total = len(reviews)
                has_next = offset + len(reviews) < total

                return {
                    "reviews": reviews,
                    "has_next": has_next,
                    "total": total,
                }

    async def get_product_names(self, skus: List[int]) -> Dict[int, str]:
        valid_skus = []
        for s in skus:
            if s is not None:
                try:
                    valid_skus.append(int(s))
                except (TypeError, ValueError):
                    pass
        if not valid_skus:
            return {}

        url = f"{self.BASE_URL}/v2/product/info/list"
        payload = {"sku": valid_skus}
        names_map: Dict[int, str] = {}

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, headers=self.headers, json=payload, timeout=aiohttp.ClientTimeout(total=20)
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        items = data.get("result", {}).get("items", [])
                        for item in items:
                            sku = item.get("sku")
                            name = item.get("name")
                            if sku and name:
                                names_map[sku] = name
        except Exception as e:
            logging.error(f"Ozon get_product_names error: {e}")

        return names_map

    async def publish_reply(self, review_id: str, text: str):
        url = f"{self.BASE_URL}/v1/review/comment/create"
        payload = {
            "review_id": review_id,
            "text": text,
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, headers=self.headers, json=payload, timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                if resp.status == 429:
                    raise OzonAPIError("Лимит запросов Ozon при отправке ответа (429)", status_code=429)

                if resp.status not in (200, 201, 204):
                    err_text = await resp.text()
                    raise OzonAPIError(
                        f"Ошибка публикации ответа Ozon ({resp.status}): {err_text[:200]}",
                        status_code=resp.status,
                    )
