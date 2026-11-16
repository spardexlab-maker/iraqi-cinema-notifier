"""One-shot monitor and command-processor runner for GitHub Actions / cron."""

from datetime import datetime, timezone
import html
import json
import logging
import os
from pathlib import Path
import sys

from dotenv import load_dotenv

from app.scraper import IraqiCinemaClient, MovieDetails, MovieSummary
from app.telegram_bot import MAIN_KEYBOARD, TelegramBotClient, format_movie_notification

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("run_once")

STATE_FILE = Path(os.getenv("STATE_FILE", "state/seen_movies.json"))

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

BUTTON_TO_CMD = {
    "🎬 الأفلام المتاحة الآن": "/now",
    "🔍 فحص فوري": "/check",
    "🏛 تحديد السينمات": "/cinemas",
    "📊 حالة البوت": "/status",
}


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"initialized": False, "movies": {}, "branch_filters": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    data.setdefault("movies", {})
    data.setdefault("branch_filters", {})
    return data


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _parse_chat_ids(raw: str) -> list[str]:
    return [c.strip() for c in raw.split(",") if c.strip()]


def _get_filter_for_chat(
    state: dict, chat_id: str, default_filter: list[str]
) -> list[str]:
    filters = state.get("branch_filters", {})
    if chat_id in filters:
        return list(filters[chat_id])
    return list(default_filter)


def _handle_pending_commands(
    bot: TelegramBotClient,
    client: IraqiCinemaClient,
    movies: list[MovieSummary],
    state: dict,
    default_filter: list[str],
    new_alerts_count: int,
) -> None:
    last_id = state.get("last_update_id")
    offset = int(last_id) + 1 if last_id is not None else None
    try:
        updates = bot.get_updates(offset=offset, poll_timeout=0)
    except Exception as exc:
        logger.warning("Failed to fetch Telegram updates: %s", exc)
        return

    if not updates:
        return

    bookable = [m for m in movies if m.is_bookable]
    seen_count = len(state.get("movies", {}))
    last_check = state.get("last_check_at", "الآن")

    for upd in updates:
        upd_id = int(upd["update_id"])
        state["last_update_id"] = upd_id

        msg = upd.get("message")
        if not msg:
            continue
        chat_id = str(msg["chat"]["id"])
        raw_text = (msg.get("text") or "").strip()
        if not raw_text:
            continue

        mapped = BUTTON_TO_CMD.get(raw_text, raw_text)
        if not mapped.startswith("/"):
            continue

        cmd = mapped.split()[0].lower()
        arg = mapped[len(cmd) :].strip()

        try:
            if cmd == "/start":
                bot.send_message(
                    chat_id,
                    f"👋 أهلاً بك في بوت <b>Iraqi Cinema</b>!\n"
                    f"معرّف المحادثة (Chat ID): <code>{html.escape(chat_id)}</code>\n\n"
                    f"استخدم الأزرار بالأسفل أو الأوامر:\n"
                    f"• /now — عرض الأفلام المتاحة الآن\n"
                    f"• /now اسم الفلم — تفاصيل وأوقات فلم معين\n"
                    f"• /cinemas — تحديد السينمات المفضلة\n"
                    f"• /status — حالة النظام\n\n"
                    f"<i>(ملاحظة: البوت يعمل عبر GitHub Actions كل 10 دقائق، لذلك الرد على الأوامر يصلك خلال دقائق).</i>",
                    reply_markup=MAIN_KEYBOARD,
                )
            elif cmd == "/status":
                bot.send_message(
                    chat_id,
                    f"📊 <b>حالة مراقب Iraqi Cinema</b>\n"
                    f"• الوضع: <b>GitHub Actions 24/7</b> ✅\n"
                    f"• الفحص كل: <b>10 دقائق</b>\n"
                    f"• آخر فحص: <code>{html.escape(str(last_check))}</code>\n"
                    f"• الأفلام المسجلة: <b>{seen_count}</b>",
                    reply_markup=MAIN_KEYBOARD,
                )
            elif cmd == "/check":
                bot.send_message(
                    chat_id,
                    f"🔍 تم الفحص الآن: الأفلام الجديدة المرسلة بهذه الدورة = <b>{new_alerts_count}</b>.\n"
                    f"الأفلام المفتوحة حالياً في القائمة: <b>{len(bookable)}</b>.",
                    reply_markup=MAIN_KEYBOARD,
                )
            elif cmd == "/cinemas":
                _handle_cinemas(bot, state, chat_id, arg, default_filter)
            elif cmd == "/now":
                _handle_now(bot, client, movies, bookable, state, chat_id, arg, default_filter)
        except Exception as exc:
            logger.error("Error replying to command '%s' for %s: %s", cmd, chat_id, exc)


def _handle_cinemas(
    bot: TelegramBotClient,
    state: dict,
    chat_id: str,
    arg: str,
    default_filter: list[str],
) -> None:
    if not arg:
        current = _get_filter_for_chat(state, chat_id, default_filter)
        current_txt = "كل السينمات" if not current else ", ".join(current)
        lines = [
            "🏛 <b>تحديد السينمات المفضلة</b>",
            f"الحالي: <code>{html.escape(current_txt)}</code>",
            "",
            "قائمة السينمات:",
        ]
        for i, name in enumerate(KNOWN_BRANCHES, start=1):
            lines.append(f"{i}. {html.escape(name)}")
        lines.extend(
            [
                "",
                "للاختيار دز أرقام السينمات مفصولة بفاصلة، مثلاً:",
                "<code>/cinemas 1, 12</code>",
                "ولإرجاع كل السينمات: <code>/cinemas all</code>",
            ]
        )
        bot.send_message(chat_id, "\n".join(lines), reply_markup=MAIN_KEYBOARD)
        return

    if arg.strip().lower() in {"all", "الكل", "reset"}:
        state["branch_filters"][chat_id] = []
        bot.send_message(
            chat_id,
            "✅ تم التفعيل: راح يعرضلك <b>كل السينمات</b>.",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    chosen: list[str] = []
    for part in arg.split(","):
        token = part.strip()
        if not token:
            continue
        if token.isdigit():
            idx = int(token)
            if 1 <= idx <= len(KNOWN_BRANCHES):
                chosen.append(KNOWN_BRANCHES[idx - 1].lower())
        else:
            chosen.append(token.lower())

    if not chosen:
        bot.send_message(
            chat_id,
            "❌ ما فهمت الاختيار. دز <code>/cinemas</code> حتى تشوف الأرقام.",
            reply_markup=MAIN_KEYBOARD,
        )
        return

    state["branch_filters"][chat_id] = chosen
    bot.send_message(
        chat_id,
        "✅ تم حفظ السينمات المحددة:\n"
        + "\n".join(f"• {html.escape(c)}" for c in chosen),
        reply_markup=MAIN_KEYBOARD,
    )


def _handle_now(
    bot: TelegramBotClient,
    client: IraqiCinemaClient,
    all_movies: list[MovieSummary],
    bookable: list[MovieSummary],
    state: dict,
    chat_id: str,
    query: str,
    default_filter: list[str],
) -> None:
    flt = _get_filter_for_chat(state, chat_id, default_filter)
    if query:
        q = query.lower()
        matches = [m for m in bookable if q in m.title.lower()]
        if not matches:
            upcoming = [m for m in all_movies if q in m.title.lower()]
            if upcoming:
                found_adv = False
                for m in upcoming[:2]:
                    details = client.fetch_movie_details(
                        m, branch_filter=flt, check_advance_dates=True
                    )
                    if details.branches:
                        bot.send_message(
                            chat_id,
                            format_movie_notification(details, is_new=False),
                            reply_markup=MAIN_KEYBOARD,
                        )
                        found_adv = True
                if found_adv:
                    return
                names = ", ".join(html.escape(m.title) for m in upcoming[:3])
                bot.send_message(
                    chat_id,
                    f"⏳ الفلم (<b>{names}</b>) موجود بقائمة <b>قريباً (Coming Soon)</b> وبعد ما نزل بجدول الحجز.",
                    reply_markup=MAIN_KEYBOARD,
                )
                return

            bot.send_message(
                chat_id,
                f"❌ لم أجد فلماً يطابق: <b>{html.escape(query)}</b>",
                reply_markup=MAIN_KEYBOARD,
            )
            return

        for m in matches[:3]:
            details = client.fetch_movie_details(
                m, branch_filter=flt, check_advance_dates=True
            )
            bot.send_message(
                chat_id,
                format_movie_notification(details, is_new=False),
                reply_markup=MAIN_KEYBOARD,
            )
        return

    if not bookable:
        bot.send_message(
            chat_id, "لا توجد أفلام متاحة للحجز حالياً.", reply_markup=MAIN_KEYBOARD
        )
        return

    lines = [f"🎬 <b>الأفلام المتاحة للحجز حالياً ({len(bookable)}):</b>", ""]
    for idx, m in enumerate(bookable, start=1):
        dur = f" ({m.duration})" if m.duration else ""
        lines.append(f"{idx}. <b>{html.escape(m.title)}</b>{html.escape(dur)}")
    lines.append("")
    lines.append("💡 لعرض السينمات والأوقات لفلم معين أرسل:\n<code>/now اسم الفلم</code>")
    bot.send_message(chat_id, "\n".join(lines), reply_markup=MAIN_KEYBOARD)


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
    bot.set_my_commands()

    state = load_state(STATE_FILE)
    seen: dict[str, dict] = state.setdefault("movies", {})

    logger.info("Fetching Iraqi Cinema movie listing...")
    movies = client.fetch_movies()
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    state["last_check_at"] = now_iso

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
        _handle_pending_commands(bot, client, movies, state, branch_filter, new_alerts_count=0)
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
            details = client.fetch_movie_details(
                m, branch_filter=None, check_advance_dates=True
            )
            if not details.branches:
                if m.is_bookable:
                    logger.info(
                        "Movie '%s' has no showtimes yet; will retry next run.", m.title
                    )
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

    _handle_pending_commands(bot, client, movies, state, branch_filter, new_alerts_count=new_alerts)
    save_state(STATE_FILE, state)
    logger.info("Check complete. New alerts sent: %d", new_alerts)


if __name__ == "__main__":
    main()
