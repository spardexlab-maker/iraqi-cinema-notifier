"""Telegram Bot API client and message formatting."""

from collections import defaultdict
import html
import logging
import httpx

from app.scraper import MovieDetails

logger = logging.getLogger(__name__)

_TELEGRAM_LIMIT = 4000

MAIN_KEYBOARD = {
    "keyboard": [
        [{"text": "🎬 الأفلام المتاحة الآن"}, {"text": "🔍 فحص فوري"}],
        [{"text": "🏛 تحديد السينمات"}, {"text": "📊 حالة البوت"}],
    ],
    "resize_keyboard": True,
    "is_persistent": True,
}

BOT_COMMANDS = [
    {"command": "now", "description": "عرض الأفلام المتاحة للحجز حالياً"},
    {"command": "cinemas", "description": "تحديد السينمات المفضلة"},
    {"command": "check", "description": "فحص فوري للأفلام الجديدة"},
    {"command": "status", "description": "حالة البوت وآخر فحص"},
    {"command": "start", "description": "تفعيل الإشعارات وعرض الأزرار"},
]


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

    def send_message(
        self,
        chat_id: str,
        text: str,
        reply_markup: dict | None = None,
    ) -> None:
        url = f"{self.base_url}/sendMessage"
        chunks = split_message(text)
        with httpx.Client(timeout=self.timeout) as client:
            for idx, chunk in enumerate(chunks):
                payload: dict = {
                    "chat_id": chat_id,
                    "text": chunk,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                }
                if reply_markup is not None and idx == len(chunks) - 1:
                    payload["reply_markup"] = reply_markup
                resp = client.post(url, json=payload)
                if resp.status_code >= 400:
                    logger.error(
                        "Telegram sendMessage failed (%s): %s",
                        resp.status_code,
                        resp.text,
                    )
                    resp.raise_for_status()

    def set_my_commands(self) -> None:
        url = f"{self.base_url}/setMyCommands"
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(url, json={"commands": BOT_COMMANDS})
            if resp.status_code >= 400:
                logger.warning("Failed to setMyCommands: %s", resp.text)

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
