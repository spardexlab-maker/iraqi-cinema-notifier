"""24/7 Iraqi Cinema monitor and Telegram bot runner."""

from datetime import datetime, timezone
import html
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import signal
import sys
import threading
import time

from app.config import Settings, load_settings
from app.scraper import IraqiCinemaClient, MovieDetails, MovieSummary
from app.storage import Storage
from app.telegram_bot import TelegramBotClient, format_movie_notification

logger = logging.getLogger("iraqi_cinema_notifier")

KNOWN_BRANCHES = [
    "Mansour Mall",
    "DreamCity Mall",
    "Baghdad Mall",
    "Zayoona Mall",
    "Babylon Mall",
    "Cairo Mall",
    "Alamirya Mall",
    "Al-Jadriya Mall",
    "Al-Harith Mall",
    "Dijlah Village",
    "Boulevard",
    "Iraq Mall",
    "Outdoor Cinema Babylon Mall",
]


def setup_logging() -> None:
    """Configure console and rotating file logging."""
    log_dir = Path("logs")
    log_dir.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(logging.INFO)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    file_handler = RotatingFileHandler(
        log_dir / "notifier.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)


class CinemaNotifierService:
    """Coordinates periodic scraping, state tracking, and Telegram alerts."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.storage = Storage(settings.db_path)
        self.cinema = IraqiCinemaClient(
            base_url=settings.base_url,
            timeout=settings.request_timeout_seconds,
        )
        self.bot = TelegramBotClient(token=settings.telegram_bot_token)
        self.stop_event = threading.Event()
        self._check_lock = threading.Lock()
        self.started_at = datetime.now(timezone.utc)
        self.last_check_at: datetime | None = None
        self.last_advance_check_at: datetime | None = None

    def _notify_all(self, text: str) -> None:
        recipients = self.storage.get_subscribers(self.settings.telegram_chat_id)
        if not recipients:
            logger.warning(
                "No Telegram recipients configured. Send /start to your bot or set TELEGRAM_CHAT_ID."
            )
            return
        for chat_id in recipients:
            try:
                self.bot.send_message(chat_id, text)
            except Exception as exc:
                logger.error("Failed to send message to %s: %s", chat_id, exc)

    def check_once(self) -> int:
        """Check Iraqi Cinema for newly bookable movies. Returns count of new alerts."""
        with self._check_lock:
            logger.info("Checking Iraqi Cinema for new bookable movies...")
            movies = self.cinema.fetch_movies()
            bookable = [m for m in movies if m.is_bookable]
            self.last_check_at = datetime.now(timezone.utc)

            if not self.storage.is_initialized():
                for m in bookable:
                    self.storage.mark_movie_seen(m.movie_id, m.title, m.slug, m.categories)
                self.storage.mark_initialized()
                logger.info(
                    "Initial baseline saved: %d bookable movies indexed.", len(bookable)
                )
                self._notify_all(
                    f"✅ <b>بدأ مراقب Iraqi Cinema بالعمل 24/7!</b>\n"
                    f"تم تسجيل <b>{len(bookable)}</b> فلم متاح للحجز حالياً.\n"
                    f"سأرسل لك إشعاراً فورياً مع أسماء السينمات والأوقات عند فتح الحجز لأي فلم جديد.\n\n"
                    f"الأوامر المتاحة:\n"
                    f"/now - عرض الأفلام المتاحة الآن\n"
                    f"/check - فحص فوري الآن\n"
                    f"/status - حالة البوت"
                )
                return 0

            seen_ids = self.storage.get_seen_movie_ids()
            new_movies = [m for m in bookable if m.movie_id not in seen_ids]

            # Also check coming-soon movies for advance pre-bookings every 30 minutes
            now_utc = datetime.now(timezone.utc)
            if (
                self.last_advance_check_at is None
                or (now_utc - self.last_advance_check_at).total_seconds() >= 1800
            ):
                self.last_advance_check_at = now_utc
                coming_soon = [
                    m for m in movies if not m.is_bookable and m.movie_id not in seen_ids
                ]
                for m in coming_soon:
                    try:
                        adv = self.cinema.fetch_movie_details(
                            m, branch_filter=None, check_advance_dates=True
                        )
                        if adv.branches:
                            new_movies.append(m)
                    except Exception as exc:
                        logger.debug("Advance check failed for %s: %s", m.slug, exc)

            if not new_movies:
                logger.info("No new bookable movies found (%d currently open).", len(bookable))
                return 0

            sent_count = 0
            for movie in new_movies:
                try:
                    details = self.cinema.fetch_movie_details(
                        movie, branch_filter=None, check_advance_dates=True
                    )
                    if not details.branches:
                        logger.info(
                            "Movie '%s' (%d) is listed as bookable but has no showtimes yet; will retry next cycle.",
                            movie.title,
                            movie.movie_id,
                        )
                        continue

                    recipients = self.storage.get_subscribers(
                        self.settings.telegram_chat_id
                    )
                    for chat_id in recipients:
                        flt = self.storage.get_branch_filter(
                            chat_id, self.settings.branch_filter
                        )
                        filtered_branches = (
                            {
                                name: shows
                                for name, shows in details.branches.items()
                                if any(f in name.lower() for f in flt)
                            }
                            if flt
                            else details.branches
                        )
                        if not filtered_branches:
                            continue
                        sub_details = MovieDetails(
                            movie=details.movie,
                            selected_date=details.selected_date,
                            branches=filtered_branches,
                            detail_url=details.detail_url,
                        )
                        try:
                            self.bot.send_message(
                                chat_id,
                                format_movie_notification(sub_details, is_new=True),
                            )
                            sent_count += 1
                        except Exception as exc:
                            logger.error("Failed to send message to %s: %s", chat_id, exc)

                    logger.info(
                        "Processed new bookable movie: %s (%d branches)",
                        movie.title,
                        len(details.branches),
                    )
                    self.storage.mark_movie_seen(
                        movie.movie_id, movie.title, movie.slug, movie.categories
                    )
                except Exception as exc:
                    logger.error(
                        "Error processing new movie '%s': %s", movie.title, exc
                    )

            return sent_count

    def _handle_command(self, chat_id: str, text: str) -> None:
        cmd = text.strip().split()[0].lower()
        arg = text.strip()[len(cmd) :].strip()

        if cmd == "/start":
            added = self.storage.add_subscriber(chat_id)
            status_note = "تم تفعيل اشتراكك بنجاح!" if added else "أنت مشترك بالفعل."
            self.bot.send_message(
                chat_id,
                f"👋 أهلاً بك في مراقب <b>Iraqi Cinema</b>!\n"
                f"{status_note}\n"
                f"معرّف المحادثة (Chat ID): <code>{html.escape(chat_id)}</code>\n\n"
                f"الأوامر المتاحة:\n"
                f"/now - عرض الأفلام المتاحة للحجز حالياً\n"
                f"/now اسم الفلم - عرض السينمات والأوقات لفلم معين\n"
                f"/cinemas - تحديد السينمات اللي تتابعها\n"
                f"/check - فحص فوري للأفلام الجديدة\n"
                f"/status - حالة النظام\n"
                f"/stop - إيقاف الإشعارات",
            )
        elif cmd == "/cinemas":
            self._handle_cinemas_command(chat_id, arg)
        elif cmd == "/stop":
            self.storage.remove_subscriber(chat_id)
            self.bot.send_message(
                chat_id, "🔕 تم إيقاف الإشعارات لهذا الحساب. أرسل /start لإعادة التفعيل."
            )
        elif cmd == "/check":
            self.bot.send_message(chat_id, "🔍 جاري فحص Iraqi Cinema الآن...")
            try:
                new_count = self.check_once()
                if new_count == 0:
                    self.bot.send_message(chat_id, "✅ تم الفحص: لا توجد أفلام جديدة مفتوحة للحجز حالياً.")
            except Exception as exc:
                self.bot.send_message(chat_id, f"❌ حدث خطأ أثناء الفحص: {html.escape(str(exc))}")
        elif cmd == "/now":
            self._reply_now_showing(chat_id, query=arg)
        elif cmd == "/status":
            last_str = (
                self.last_check_at.strftime("%Y-%m-%d %H:%M:%S UTC")
                if self.last_check_at
                else "لم يتم بعد"
            )
            seen_count = len(self.storage.get_seen_movie_ids())
            self.bot.send_message(
                chat_id,
                f"📊 <b>حالة مراقب Iraqi Cinema</b>\n"
                f"• الحالة: يعمل 24/7 ✅\n"
                f"• الفحص كل: <b>{self.settings.check_interval_minutes}</b> دقيقة\n"
                f"• آخر فحص: <code>{last_str}</code>\n"
                f"• الأفلام المسجلة: <b>{seen_count}</b>",
            )

    def _handle_cinemas_command(self, chat_id: str, arg: str) -> None:
        if not arg:
            current = self.storage.get_branch_filter(
                chat_id, self.settings.branch_filter
            )
            current_txt = (
                "كل السينمات" if not current else ", ".join(current)
            )
            lines = [
                f"🏛 <b>تحديد السينمات المفضلة</b>",
                f"الحالي: <code>{html.escape(current_txt)}</code>",
                "",
                "قائمة السينمات:",
            ]
            for i, name in enumerate(KNOWN_BRANCHES, start=1):
                lines.append(f"{i}. {html.escape(name)}")
            lines.extend(
                [
                    "",
                    "للاختيار دز أرقام السينمات أو أسماءها مفصولة بفاصلة، مثلاً:",
                    "<code>/cinemas 1, 12</code>",
                    "أو: <code>/cinemas Mansour, Iraq Mall</code>",
                    "ولإرجاع كل السينمات: <code>/cinemas all</code>",
                ]
            )
            self.bot.send_message(chat_id, "\n".join(lines))
            return

        if arg.strip().lower() in {"all", "الكل", "reset"}:
            self.storage.set_branch_filter(chat_id, [])
            self.bot.send_message(chat_id, "✅ تم التفعيل: راح توصلك إشعارات <b>كل السينمات</b>.")
            return

        chosen: list[str] = []
        for part in arg.split(","):
            token = part.strip()
            if not token:
                continue
            if token.isdigit():
                idx = int(token)
                if 1 <= idx <= len(KNOWN_BRANCHES):
                    chosen.append(KNOWN_BRANCHES[idx - 1])
            else:
                chosen.append(token)

        if not chosen:
            self.bot.send_message(
                chat_id, "❌ ما فهمت الاختيار. دز <code>/cinemas</code> حتى تشوف الأرقام."
            )
            return

        self.storage.set_branch_filter(chat_id, chosen)
        self.bot.send_message(
            chat_id,
            "✅ تم حفظ السينمات المحددة:\n"
            + "\n".join(f"• {html.escape(c)}" for c in chosen),
        )

    def _reply_now_showing(self, chat_id: str, query: str = "") -> None:
        try:
            movies = self.cinema.fetch_movies()
            bookable: list[MovieSummary] = [m for m in movies if m.is_bookable]
            flt = self.storage.get_branch_filter(chat_id, self.settings.branch_filter)
            if query:
                q = query.lower()
                matches = [m for m in bookable if q in m.title.lower()]
                if not matches:
                    upcoming = [m for m in movies if q in m.title.lower()]
                    if upcoming:
                        found_any_advance = False
                        for m in upcoming[:2]:
                            details = self.cinema.fetch_movie_details(
                                m, branch_filter=flt, check_advance_dates=True
                            )
                            if details.branches:
                                self.bot.send_message(
                                    chat_id,
                                    format_movie_notification(details, is_new=False),
                                )
                                found_any_advance = True
                        if found_any_advance:
                            return
                        names = ", ".join(html.escape(m.title) for m in upcoming[:3])
                        self.bot.send_message(
                            chat_id,
                            f"⏳ الفلم (<b>{names}</b>) موجود حالياً بقائمة <b>قريباً (Coming Soon)</b> على موقع iraqicinema.net، وبعد ما نزل بجدول الحجز المفتوح على الموقع.\nأول ما ينفتح على الموقع راح يجيك إشعار!",
                        )
                        return
                    self.bot.send_message(
                        chat_id,
                        f"❌ لم أجد فلماً يطابق: <b>{html.escape(query)}</b>",
                    )
                    return
                for m in matches[:3]:
                    details = self.cinema.fetch_movie_details(m, branch_filter=flt)
                    self.bot.send_message(
                        chat_id, format_movie_notification(details, is_new=False)
                    )
                return

            if not bookable:
                self.bot.send_message(chat_id, "لا توجد أفلام متاحة للحجز حالياً.")
                return

            lines = [f"🎬 <b>الأفلام المتاحة للحجز حالياً ({len(bookable)}):</b>", ""]
            for idx, m in enumerate(bookable, start=1):
                dur = f" ({m.duration})" if m.duration else ""
                lines.append(f"{idx}. <b>{html.escape(m.title)}</b>{html.escape(dur)}")
            lines.append("")
            lines.append("💡 لعرض السينمات والأوقات لفلم معين أرسل:\n<code>/now اسم الفلم</code>")
            self.bot.send_message(chat_id, "\n".join(lines))
        except Exception as exc:
            self.bot.send_message(chat_id, f"❌ تعذر جلب القائمة: {html.escape(str(exc))}")

    def _telegram_poll_loop(self) -> None:
        raw_offset = self.storage.get_state("last_update_id")
        offset = int(raw_offset) + 1 if raw_offset else None
        logger.info("Telegram command listener started.")

        while not self.stop_event.is_set():
            try:
                updates = self.bot.get_updates(offset=offset, poll_timeout=20)
                for upd in updates:
                    upd_id = int(upd["update_id"])
                    offset = upd_id + 1
                    self.storage.set_state("last_update_id", str(upd_id))

                    msg = upd.get("message") or upd.get("channel_post")
                    if not msg:
                        continue
                    text = msg.get("text", "")
                    chat_id = str(msg["chat"]["id"])
                    if text.startswith("/"):
                        self._handle_command(chat_id, text)
            except Exception as exc:
                logger.warning("Telegram polling error: %s", exc)
                self.stop_event.wait(5)

    def run_forever(self) -> None:
        """Start the Telegram listener and 24/7 cinema monitor loop."""
        poll_thread = threading.Thread(
            target=self._telegram_poll_loop, name="telegram-poll", daemon=True
        )
        poll_thread.start()

        interval_seconds = self.settings.check_interval_minutes * 60
        logger.info(
            "Starting 24/7 Iraqi Cinema monitor (interval=%d min).",
            self.settings.check_interval_minutes,
        )

        while not self.stop_event.is_set():
            try:
                self.check_once()
            except Exception as exc:
                logger.exception("Error during scheduled cinema check: %s", exc)

            self.stop_event.wait(interval_seconds)

        logger.info("Shutdown complete.")


def main() -> None:
    setup_logging()
    settings = load_settings()
    service = CinemaNotifierService(settings)

    def _handle_signal(signum, _frame) -> None:
        logger.info("Received signal %s, shutting down...", signum)
        service.stop_event.set()

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    service.run_forever()


if __name__ == "__main__":
    main()
