# Vellio Bot (`getvellio_bot`) — Инструкции проекта

## О проекте
Асинхронный Telegram-бот для автоматизации ответов на отзывы покупателей на **Ozon** и **Wildberries** с генерацией ответов через **DeepSeek API**.

## Стек и окружение
- Python 3.12 (`.\venv\Scripts\python.exe`)
- `aiogram` 3.x (Telegram Bot API, `ParseMode.HTML` по умолчанию)
- `SQLAlchemy` 2.x async + `aiosqlite` (`vellio_bot.db`)
- `aiohttp` (HTTP-клиенты Ozon, Wildberries, DeepSeek, ЮKassa)
- `python-dotenv`, `cryptography`

## Структура файлов
- `config.py` — конфигурация из `.env`; `.env.example` — шаблон без секретов.
- `bot.py` — точка входа (`main()`), Telegram-хэндлеры, FSM, независимые фоновые задачи магазинов и проверка подписок.
- `database.py` — модели (`User`, `Shop`, `ReplyLog`, `ProcessedReview`), операции с БД и шифрование ключей маркетплейсов через Fernet.
- `ai_generator.py` — `DeepSeekReplyGenerator` (генерация ответов на отзывы).
- `ozon_client.py` — `OzonClient` (Ozon Seller API).
- `wb_client.py` — `WildberriesClient` (WB Feedbacks API с асинхронным троттлингом).
- `admin_router.py` — подключаемый роутер супер-администратора (метрики, выдача подписок, рассылка).
- `yookassa_service.py` — интеграция с API ЮKassa (оплата пока не подключена к интерфейсу).
- `tests/` — автотесты на стандартном `unittest` с изолированной SQLite in-memory БД и моками API.
- `.opencode/agents/qa-fixer.md` — проектный агент для поиска, тестирования и исправления багов.

## Правила разработки
1. **Секреты и конфигурация:** не хардкодить токены, API-ключи и пароли в коде — загружать через `.env` и `config.py`. Не читать и не коммитить `.env` и `vellio_bot.db`. При публикации токена или его попадании в репозиторий требуется ротация внешнего ключа.
2. **Форматирование сообщений Telegram:** использовать единый `ParseMode.HTML` и экранировать пользовательский/внешний текст (названия товаров, отзывы, подписи, стоп-слова) через `html.escape()`.
3. **Безопасность и доступ:** в любых операциях с магазинами проверять принадлежность `shop.user_id == user_id`; использовать параметры `user_id` в `get_shop_by_id`, `update_shop`, `delete_shop` и других операциях.
4. **Асинхронность и БД:**
   - Не блокировать event loop синхронными вызовами.
   - Учитывать, что SQLite хранит `DateTime` без таймзоны (naive) — использовать UTC-время без `tzinfo` для значений в БД.
   - В `aiogram` 3 объекты `CallbackQuery` и `Message` (Pydantic v2) неизменяемы: использовать `model_copy(...)` вместо прямой мутации полей.
   - Дедупликацию обработанных отзывов хранить в таблице `ProcessedReview`, а не только в памяти процесса.
5. **Проверка после изменений:**
   - Проверять синтаксис и импорты: `.\venv\Scripts\python.exe -m py_compile config.py bot.py database.py ai_generator.py ozon_client.py wb_client.py admin_router.py yookassa_service.py`
   - Прогонять автотесты: `.\venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -v`
   - Для тестов использовать in-memory БД (`StaticPool`) и `unittest.mock`; не использовать реальную `vellio_bot.db`, внешние API и реальный Telegram polling.
