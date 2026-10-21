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
    _last_request_by_token: Dict[str, float] = {}
    _locks_by_token: Dict[str, asyncio.Lock] = {}

    def __init__(self, api_token: str):
        self.api_token = api_token.strip()
        self.token_key = self.api_token[-16:] if len(self.api_token) >= 16 else self.api_token
        if self.token_key not in WildberriesClient._locks_by_token:
            WildberriesClient._locks_by_token[self.token_key] = asyncio.Lock()

        self.headers = {
            "Authorization": self.api_token,
            "Content-Type": "application/json",
            "Accept": "application/json"
        }

    async def _rate_limit_throttle(self, min_interval: float = 15.0):
        """Индивидуальный троттлинг для каждого токена WB."""
        lock = WildberriesClient._locks_by_token[self.token_key]
        async with lock:
            last_time = WildberriesClient._last_request_by_token.get(self.token_key, 0.0)
            elapsed = time.time() - last_time
            if elapsed < min_interval:
                await asyncio.sleep(min_interval - elapsed)
            WildberriesClient._last_request_by_token[self.token_key] = time.time()

    @staticmethod
    def _get_wb_retry_seconds(headers: Any, default_sec: int = 20) -> int:
        """Извлекает точное время ожидания из заголовка X-RateLimit-Retry."""
        raw_retry = (
            headers.get("X-RateLimit-Retry")
            or headers.get("x-ratelimit-retry")
            or headers.get("Retry-After")
        )
        try:
            return max(int(float(raw_retry)) + 2, 5) if raw_retry else default_sec
        except (ValueError, TypeError):
            return default_sec

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
                    if resp.status == 429:
                        wait_s = self._get_wb_retry_seconds(resp.headers, 15)
                        await asyncio.sleep(wait_s)
                        return True, "Токен валиден (выдержана пауза Rate-Limit)"
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

        for attempt in range(1, 6):
            await self._rate_limit_throttle(15.0)
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, headers=self.headers, params=params, timeout=30) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            data_block = data.get("data", {})
                            if isinstance(data_block, dict):
                                return data_block.get("feedbacks", [])
                            return []

                        if resp.status == 429:
                            wait_sec = self._get_wb_retry_seconds(resp.headers, default_sec=20 * attempt)
                            logging.warning(
                                f"WB 429 при чтении отзывов (попытка {attempt}/5). "
                                f"Заголовок X-RateLimit-Retry требует ждать {wait_sec} сек..."
                            )
                            await asyncio.sleep(wait_sec)
                            WildberriesClient._last_request_by_token[self.token_key] = time.time()
                            continue

                        text = await resp.text()
                        raise WBAPIError(
                            f"Ошибка получения отзывов WB ({resp.status}): {text[:150]}",
                            status_code=resp.status
                        )
            except aiohttp.ClientError as e:
                if attempt == 5:
                    raise WBAPIError(f"Сетевая ошибка WB: {e}")
                await asyncio.sleep(10)

        raise WBAPIError("Лимит запросов WB (429) не сбросился после 5 попыток", status_code=429)

    async def publish_reply(self, feedback_id: str, text: str):
        url = f"{self.BASE_URL}/api/v1/feedbacks/answer"
        payload = {
            "id": str(feedback_id).strip(),
            "text": text.strip()
        }

        for attempt in range(1, 6):
            await self._rate_limit_throttle(15.0)
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.post(url, headers=self.headers, json=payload, timeout=30) as resp:
                        if resp.status in (200, 204):
                            return

                        if resp.status == 429:
                            wait_sec = self._get_wb_retry_seconds(resp.headers, default_sec=25 * attempt)
                            logging.warning(
                                f"WB 429 при публикации ответа (попытка {attempt}/5). "
                                f"Заголовок X-RateLimit-Retry требует ждать {wait_sec} сек..."
                            )
                            await asyncio.sleep(wait_sec)
                            WildberriesClient._last_request_by_token[self.token_key] = time.time()
                            continue

                        err_text = await resp.text()
                        raise WBAPIError(
                            f"Ошибка публикации ответа WB ({resp.status}): {err_text[:150]}",
                            status_code=resp.status
                        )
            except aiohttp.ClientError as e:
                if attempt == 5:
                    raise WBAPIError(f"Сетевая ошибка WB при отправке ответа: {e}")
                await asyncio.sleep(10)

        raise WBAPIError("Лимит запросов WB при отправке ответа (429) после 5 попыток", status_code=429)
