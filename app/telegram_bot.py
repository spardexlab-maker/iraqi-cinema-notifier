"""Telegram Bot API client and message formatting."""

from collections import defaultdict
import html
import logging
import httpx

from app.scraper import MovieDetails

logger = logging.getLogger(__name__)

_TELEGRAM_LIMIT = 4000


def format_movie_notification(details: MovieDetails, is_new: bool = True) -> str:
    """Format a movie's booking availability into an HTML Telegram message."""
    movie = details.movie
    header = (
        "🎬 <b>فلم جديد انفتح للحجز في Iraqi Cinema!</b>"
        if is_new
        else "🎥 <b>تفاصيل الحجز المتوفرة:</b>"
    )

    lines = [
        header,
        "",
        f"🎞 <b>{html.escape(movie.title)}</b>",
    ]
    if movie.duration:
        lines.append(f"⏱ المدة: {html.escape(movie.duration)}")
    if details.selected_date:
        lines.append(f"📅 التاريخ: <code>{html.escape(details.selected_date)}</code>")

    lines.append("")
    if not details.branches:
        lines.append("⚠️ الفلم مضاف للقائمة، لكن لا توجد أوقات عرض منشورة حالياً.")
    else:
        lines.append(f"🏛 <b>السينمات المتوفرة ({len(details.branches)}):</b>")
        for branch_name, showtimes in details.branches.items():
            lines.append(f"\n📍 <b>{html.escape(branch_name)}</b>")
            by_screen: dict[str, list[str]] = defaultdict(list)
            for st in showtimes:
                seat_info = (
                    f" ({st.available_seats} مقعد)"
                    if st.available_seats is not None
                    else ""
                )
                by_screen[st.screen_type].append(f"{st.time}{seat_info}")

            for screen_type, times in by_screen.items():
                joined = " | ".join(times)
                lines.append(
                    f"   • <b>{html.escape(screen_type)}:</b> {html.escape(joined)}"
                )

    lines.append("")
    lines.append(f'🔗 <a href="{html.escape(details.detail_url)}">فتح صفحة الفلم والحجز</a>')
    return "\n".join(lines)


def split_message(text: str, limit: int = _TELEGRAM_LIMIT) -> list[str]:
    """Split long messages on line boundaries to stay under Telegram's limit."""
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    for line in text.splitlines():
        add_len = len(line) + 1
        if current and current_len + add_len > limit:
            chunks.append("\n".join(current))
            current = [line]
            current_len = len(line)
        else:
            current.append(line)
            current_len += add_len

    if current:
        chunks.append("\n".join(current))
    return chunks


class TelegramBotClient:
    """Minimal, reliable Telegram Bot API wrapper using httpx."""

    def __init__(self, token: str, timeout: float = 35.0) -> None:
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.timeout = timeout

    def send_message(self, chat_id: str, text: str) -> None:
        url = f"{self.base_url}/sendMessage"
        with httpx.Client(timeout=self.timeout) as client:
            for chunk in split_message(text):
                resp = client.post(
                    url,
                    json={
                        "chat_id": chat_id,
                        "text": chunk,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                    },
                )
                if resp.status_code >= 400:
                    logger.error(
                        "Telegram sendMessage failed (%s): %s",
                        resp.status_code,
                        resp.text,
                    )
                    resp.raise_for_status()

    def get_updates(self, offset: int | None = None, poll_timeout: int = 20) -> list[dict]:
        url = f"{self.base_url}/getUpdates"
        params: dict[str, int] = {"timeout": poll_timeout}
        if offset is not None:
            params["offset"] = offset
        with httpx.Client(timeout=poll_timeout + 10) as client:
            resp = client.get(url, params=params)
            resp.raise_for_status()
            payload = resp.json()
            if not payload.get("ok"):
                return []
            return payload.get("result", [])
