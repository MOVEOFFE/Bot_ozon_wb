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

    def __init__(self, api_token: str):
        self.api_token = api_token.strip()
        self.headers = {
            "Authorization": self.api_token,
            "Content-Type": "application/json",
            "Accept": "application/json"
        }

    async def _rate_limit_throttle(self):
        elapsed = time.time() - WildberriesClient._last_request_time
        if elapsed < 15.0:
            await asyncio.sleep(15.0 - elapsed)
        WildberriesClient._last_request_time = time.time()

    async def verify_credentials(self) -> Tuple[bool, str]:
        await self._rate_limit_throttle()
        url = f"{self.BASE_URL}/api/v1/feedbacks/count-unanswered"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=self.headers, timeout=15) as resp:
                    if resp.status == 200:
                        return True, "Успешное подключение к Wildberries"
                    if resp.status == 401:
                        return False, "Неверный API-токен WB."
                    if resp.status == 429:
                        return False, "Превышен лимит запросов Wildberries (429)."
                    text = await resp.text()
                    return False, f"Ошибка WB ({resp.status}): {text[:150]}"
        except Exception as e:
            return False, f"Сетевая ошибка WB: {e}"

    async def get_unprocessed_feedbacks(self, take: int = 30) -> List[Dict[str, Any]]:
        await self._rate_limit_throttle()
        url = f"{self.BASE_URL}/api/v1/feedbacks"
        params = {
            "isAnswered": "false",
            "take": min(take, 30),
            "skip": 0,
            "order": "dateDesc"
        }

        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=self.headers, params=params, timeout=30) as resp:
                if resp.status == 429:
                    raise WBAPIError("Лимит запросов WB превышен (429)", status_code=429)

                if resp.status != 200:
                    text = await resp.text()
                    raise WBAPIError(
                        f"Ошибка получения отзывов WB ({resp.status}): {text[:200]}",
                        status_code=resp.status
                    )

                data = await resp.json()
                data_block = data.get("data", {})
                if isinstance(data_block, dict):
                    return data_block.get("feedbacks", [])
                return []

    async def publish_reply(self, feedback_id: str, text: str):
        """
        Публикация ответа на отзыв Wildberries.
        Эндпоинт по документации WB: POST https://feedbacks-api.wildberries.ru/api/v1/feedbacks/answer
        """
        await self._rate_limit_throttle()
        url = f"{self.BASE_URL}/api/v1/feedbacks/answer"
        payload = {
            "id": str(feedback_id).strip(),
            "text": text.strip()
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=self.headers, json=payload, timeout=30) as resp:
                if resp.status == 429:
                    raise WBAPIError("Лимит запросов WB при отправке ответа (429)", status_code=429)

                if resp.status in (200, 204):
                    return

                err_text = await resp.text()
                raise WBAPIError(
                    f"Ошибка публикации ответа WB ({resp.status}): {err_text[:200]}",
                    status_code=resp.status
                )