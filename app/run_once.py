"""One-shot monitor runner designed for GitHub Actions / cron execution."""

from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import sys

from dotenv import load_dotenv

from app.scraper import IraqiCinemaClient, MovieDetails
from app.telegram_bot import TelegramBotClient, format_movie_notification

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("run_once")

STATE_FILE = Path(os.getenv("STATE_FILE", "state/seen_movies.json"))


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"initialized": False, "movies": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _parse_chat_ids(raw: str) -> list[str]:
    return [c.strip() for c in raw.split(",") if c.strip()]


def main() -> None:
    load_dotenv()
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_ids = _parse_chat_ids(os.getenv("TELEGRAM_CHAT_ID", ""))
    raw_filter = os.getenv("BRANCH_FILTER", "").strip()
    branch_filter = [b.strip().lower() for b in raw_filter.split(",") if b.strip()]

    if not token:
        raise ValueError("TELEGRAM_BOT_TOKEN is required.")
    if not chat_ids:
        raise ValueError("TELEGRAM_CHAT_ID is required for GitHub Actions mode.")

    client = IraqiCinemaClient()
    bot = TelegramBotClient(token=token)
    state = load_state(STATE_FILE)
    seen: dict[str, dict] = state.setdefault("movies", {})

    logger.info("Fetching Iraqi Cinema movie listing...")
    movies = client.fetch_movies()
    now_iso = datetime.now(timezone.utc).isoformat()

    if not state.get("initialized"):
        for m in movies:
            if m.is_bookable:
                seen[str(m.movie_id)] = {
                    "title": m.title,
                    "slug": m.slug,
                    "first_seen_at": now_iso,
                }
            else:
                try:
                    adv = client.fetch_movie_details(
                        m, branch_filter=None, check_advance_dates=True
                    )
                    if adv.branches:
                        seen[str(m.movie_id)] = {
                            "title": m.title,
                            "slug": m.slug,
                            "first_seen_at": now_iso,
                        }
                except Exception as exc:
                    logger.debug("Initial advance check failed for %s: %s", m.slug, exc)

        state["initialized"] = True
        save_state(STATE_FILE, state)
        init_msg = (
            f"✅ <b>تم تفعيل مراقب Iraqi Cinema على GitHub Actions!</b>\n"
            f"تم تسجيل <b>{len(seen)}</b> فلم متاح حالياً، وسأرسل لك إشعاراً فورياً عند فتح الحجز لأي فلم جديد."
        )
        for cid in chat_ids:
            try:
                bot.send_message(cid, init_msg)
            except Exception as exc:
                logger.error("Failed sending init message to %s: %s", cid, exc)
        logger.info("Initialized state with %d movies.", len(seen))
        return

    new_alerts = 0
    for m in movies:
        key = str(m.movie_id)
        if key in seen:
            continue

        try:
            if m.is_bookable:
                details = client.fetch_movie_details(
                    m, branch_filter=None, check_advance_dates=True
                )
            else:
                details = client.fetch_movie_details(
                    m, branch_filter=None, check_advance_dates=True
                )
                if not details.branches:
                    continue

            if not details.branches:
                logger.info("Movie '%s' has no showtimes yet; will retry next run.", m.title)
                continue

            filtered_branches = (
                {
                    name: shows
                    for name, shows in details.branches.items()
                    if any(f in name.lower() for f in branch_filter)
                }
                if branch_filter
                else details.branches
            )
            if filtered_branches:
                filtered_details = MovieDetails(
                    movie=details.movie,
                    selected_date=details.selected_date,
                    branches=filtered_branches,
                    detail_url=details.detail_url,
                )
                msg_text = format_movie_notification(filtered_details, is_new=True)
                for cid in chat_ids:
                    try:
                        bot.send_message(cid, msg_text)
                    except Exception as exc:
                        logger.error("Failed sending alert to %s: %s", cid, exc)
                new_alerts += 1
                logger.info("Sent Telegram alert for '%s'", m.title)

            seen[key] = {
                "title": m.title,
                "slug": m.slug,
                "first_seen_at": now_iso,
            }
        except Exception as exc:
            logger.error("Failed checking movie '%s': %s", m.title, exc)

    save_state(STATE_FILE, state)
    logger.info("Check complete. New alerts sent: %d", new_alerts)


if __name__ == "__main__":
    main()
