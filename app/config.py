"""Application configuration loaded from environment variables."""

from dataclasses import dataclass
import os
from pathlib import Path
from dotenv import load_dotenv


@dataclass(frozen=True)
class Settings:
    """Runtime settings for the Iraqi Cinema notifier."""

    telegram_bot_token: str
    telegram_chat_id: str | None
    check_interval_minutes: int
    branch_filter: list[str]
    db_path: Path
    base_url: str = "https://www.iraqicinema.net/Harith/en"
    request_timeout_seconds: float = 20.0


def load_settings() -> Settings:
    """Load and validate settings from .env and environment variables."""
    load_dotenv()

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise ValueError(
            "TELEGRAM_BOT_TOKEN is required. Set it in .env or environment variables."
        )

    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip() or None

    raw_interval = os.getenv("CHECK_INTERVAL_MINUTES", "5").strip()
    try:
        interval = max(1, int(raw_interval))
    except ValueError as exc:
        raise ValueError(f"Invalid CHECK_INTERVAL_MINUTES: {raw_interval}") from exc

    raw_filter = os.getenv("BRANCH_FILTER", "").strip()
    branch_filter = [b.strip().lower() for b in raw_filter.split(",") if b.strip()]

    db_path = Path(os.getenv("DB_PATH", "data/cinema_notifier.db").strip())

    return Settings(
        telegram_bot_token=token,
        telegram_chat_id=chat_id,
        check_interval_minutes=interval,
        branch_filter=branch_filter,
        db_path=db_path,
    )
