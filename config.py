import os
from pathlib import Path
from typing import List

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def _parse_admin_ids(raw: str) -> List[int]:
    ids: List[int] = []
    for part in (raw or "").split(","):
        part = part.strip()
        if part.isdigit():
            ids.append(int(part))
    return ids


APP_VERSION: str = "2.2.0"
BOT_TOKEN: str = os.getenv("BOT_TOKEN", "").strip()
DEEPSEEK_API_KEY: str = os.getenv("DEEPSEEK_API_KEY", "").strip()
ADMIN_IDS: List[int] = _parse_admin_ids(os.getenv("ADMIN_IDS", ""))


def _resolve_database_url() -> str:
    """DATABASE_URL из окружения, иначе файл в постоянном каталоге данных.

    На хостинге (bothost) каталог /app пересобирается при обновлении из Git,
    а /app/data — постоянный том (платформа задаёт DATA_DIR=/app/data).
    """
    explicit = os.getenv("DATABASE_URL", "").strip()
    if explicit:
        return explicit
    data_dir = Path(os.getenv("DATA_DIR", "").strip() or "data")
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return "sqlite+aiosqlite:///vellio_bot.db"
    db_path = (data_dir / "vellio_bot.db").resolve()
    return f"sqlite+aiosqlite:///{db_path.as_posix()}"


DATABASE_URL: str = _resolve_database_url()
TOKEN_ENCRYPTION_KEY: str = os.getenv("TOKEN_ENCRYPTION_KEY", "").strip()
AI_MAX_REPLY_LENGTH: int = max(200, int(os.getenv("AI_MAX_REPLY_LENGTH", "1000")))

YOOKASSA_SHOP_ID: str = os.getenv("YOOKASSA_SHOP_ID", "").strip()
YOOKASSA_SECRET_KEY: str = os.getenv("YOOKASSA_SECRET_KEY", "").strip()
