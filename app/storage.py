"""SQLite persistence for seen movies, Telegram subscribers, and bot state."""

from datetime import datetime, timezone
from pathlib import Path
import sqlite3


class Storage:
    """Lightweight SQLite store for notifier state."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS seen_movies (
                    movie_id INTEGER PRIMARY KEY,
                    title TEXT NOT NULL,
                    slug TEXT NOT NULL,
                    categories TEXT NOT NULL,
                    first_seen_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS subscribers (
                    chat_id TEXT PRIMARY KEY,
                    subscribed_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS app_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )

    def get_state(self, key: str, default: str | None = None) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT value FROM app_state WHERE key = ?", (key,)
            ).fetchone()
            return row["value"] if row else default

    def set_state(self, key: str, value: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO app_state (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    def is_initialized(self) -> bool:
        return self.get_state("initialized") == "1"

    def mark_initialized(self) -> None:
        self.set_state("initialized", "1")

    def get_seen_movie_ids(self) -> set[int]:
        with self._connect() as conn:
            rows = conn.execute("SELECT movie_id FROM seen_movies").fetchall()
            return {int(row["movie_id"]) for row in rows}

    def mark_movie_seen(
        self, movie_id: int, title: str, slug: str, categories: list[str]
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        cats_str = ",".join(categories)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO seen_movies (movie_id, title, slug, categories, first_seen_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(movie_id) DO UPDATE SET
                    title = excluded.title,
                    slug = excluded.slug,
                    categories = excluded.categories
                """,
                (movie_id, title, slug, cats_str, now),
            )

    def add_subscriber(self, chat_id: str) -> bool:
        """Add subscriber chat_id. Returns True if newly added."""
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO subscribers (chat_id, subscribed_at)
                VALUES (?, ?)
                """,
                (str(chat_id), now),
            )
            return cur.rowcount > 0

    def remove_subscriber(self, chat_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute(
                "DELETE FROM subscribers WHERE chat_id = ?", (str(chat_id),)
            )
            return cur.rowcount > 0

    def get_subscribers(self, default_chat_id: str | None = None) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute("SELECT chat_id FROM subscribers").fetchall()
            ids = [row["chat_id"] for row in rows]
        if default_chat_id and str(default_chat_id) not in ids:
            ids.append(str(default_chat_id))
        return ids

    def get_branch_filter(
        self, chat_id: str, default_filter: list[str] | None = None
    ) -> list[str]:
        """Return lowercase branch filter substrings for a chat_id."""
        raw = self.get_state(f"branch_filter:{chat_id}")
        if raw is None:
            return list(default_filter or [])
        if not raw.strip():
            return []
        return [b.strip().lower() for b in raw.split(",") if b.strip()]

    def set_branch_filter(self, chat_id: str, branches: list[str]) -> None:
        """Persist branch filter substrings for a chat_id (empty = all)."""
        cleaned = ",".join(b.strip().lower() for b in branches if b.strip())
        self.set_state(f"branch_filter:{chat_id}", cleaned)
