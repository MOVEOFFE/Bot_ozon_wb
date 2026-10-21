import asyncio
import logging
import time
from typing import List, Dict, Any, Tuple
import aiohttp


class WBAPIError(Exception):
    def __init__(self, message: str, status_code: int = 0):
        super().__init__(message)
        self.status_code = status_code


class WildberriesClient:
    BASE_URL = "https://feedbacks-api.wildberries.ru"
    _last_request_time = 0.0
    _lock = asyncio.Lock()

    def __init__(self, api_token: str):
        self.api_token = api_token.strip()
        self.headers = {
            "Authorization": self.api_token,
            "Content-Type": "application/json",
            "Accept": "application/json"
        }

    async def _rate_limit_throttle(self, min_interval: float = 25.0):
        """Гарантирует атомарную паузу между ЛЮБЫМИ запросами к WB со всех магазинов."""
        async with WildberriesClient._lock:
            elapsed = time.time() - WildberriesClient._last_request_time
            if elapsed < min_interval:
                await asyncio.sleep(min_interval - elapsed)
            WildberriesClient._last_request_time = time.time()

    async def verify_credentials(self) -> Tuple[bool, str]:
        await self._rate_limit_throttle(5.0)
        url = f"{self.BASE_URL}/api/v1/feedbacks/count-unanswered"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=self.headers, timeout=15) as resp:
                    if resp.status == 200:
                        return True, "Успешное подключение к Wildberries"
                    if resp.status == 401:
                        return False, "Неверный API-токен WB."
                    text = await resp.text()
                    return False, f"Ошибка WB ({resp.status}): {text[:150]}"
        except Exception as e:
            return False, f"Сетевая ошибка WB: {e}"

    async def get_unprocessed_feedbacks(self, take: int = 30) -> List[Dict[str, Any]]:
        url = f"{self.BASE_URL}/api/v1/feedbacks"
        params = {
            "isAnswered": "false",
            "take": min(take, 30),
            "skip": 0,
            "order": "dateDesc"
        }

        await self._rate_limit_throttle(25.0)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=self.headers, params=params, timeout=30) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        data_block = data.get("data", {})
                        if isinstance(data_block, dict):
                            return data_block.get("feedbacks", [])
                        return []

                    text = await resp.text()
                    retry_after = resp.headers.get("Retry-After", "не указан")
                    logging.warning(f"WB Feedbacks API статус {resp.status} (Retry-After: {retry_after}): {text[:200]}")

                    if resp.status == 429:
                        raise WBAPIError(f"429 Too Many Requests (Retry-After: {retry_after}). Ответ WB: {text[:100]}", status_code=429)

                    raise WBAPIError(f"Ошибка получения отзывов WB ({resp.status}): {text[:150]}", status_code=resp.status)
        except aiohttp.ClientError as e:
            raise WBAPIError(f"Сетевая ошибка WB: {e}")

    async def publish_reply(self, feedback_id: str, text: str):
        url = f"{self.BASE_URL}/api/v1/feedbacks/answer"
        payload = {
            "id": str(feedback_id).strip(),
            "text": text.strip()
        }

        await self._rate_limit_throttle(30.0)
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=self.headers, json=payload, timeout=30) as resp:
                    if resp.status in (200, 204):
                        return

                    err_text = await resp.text()
                    retry_after = resp.headers.get("Retry-After", "не указан")
                    logging.warning(f"WB Publish API статус {resp.status} (Retry-After: {retry_after}): {err_text[:200]}")
                    raise WBAPIError(f"Ошибка публикации ответа WB ({resp.status}): {err_text[:150]}", status_code=resp.status)
        except aiohttp.ClientError as e:
            raise WBAPIError(f"Сетевая ошибка WB при отправке ответа: {e}")
