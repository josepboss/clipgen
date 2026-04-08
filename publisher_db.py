"""
publisher_db.py — SQLite storage for the publish queue and Buffer integration.

Tables:
  publish_queue   — legacy queue (kept for any existing data; not actively used)
  buffer_keys     — one row per user_id; stores the Buffer API access token
  publish_log     — record of every clip published (or attempted) via Buffer
"""

import sqlite3
import os
from datetime import datetime, timezone

DB_PATH = os.path.join(os.path.dirname(__file__), "publisher.db")

VALID_STATUSES = {"pending", "running", "done", "failed", "session_expired", "queued"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Create all tables if they don't already exist."""
    with _conn() as c:
        # Legacy queue (kept for schema compatibility)
        c.execute("""
            CREATE TABLE IF NOT EXISTS publish_queue (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id        TEXT    NOT NULL,
                video_path     TEXT    NOT NULL,
                caption        TEXT    NOT NULL,
                platform       TEXT    NOT NULL DEFAULT 'buffer',
                scheduled_time TEXT,
                status         TEXT    NOT NULL DEFAULT 'pending',
                error_msg      TEXT,
                created_at     TEXT    NOT NULL,
                updated_at     TEXT    NOT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_queue_user   ON publish_queue(user_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_queue_status ON publish_queue(status)")

        # Buffer API key store (one row per user)
        c.execute("""
            CREATE TABLE IF NOT EXISTS buffer_keys (
                user_id    TEXT PRIMARY KEY,
                api_key    TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)

        # Publish log (one row per channel per clip)
        c.execute("""
            CREATE TABLE IF NOT EXISTS publish_log (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    TEXT    NOT NULL,
                clip       TEXT    NOT NULL,
                channel_id TEXT    NOT NULL,
                post_id    TEXT,
                status     TEXT    NOT NULL DEFAULT 'queued',
                error_msg  TEXT,
                created_at TEXT    NOT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_log_user ON publish_log(user_id)")
        c.commit()


# ── Buffer key ────────────────────────────────────────────────────────────────

def save_buffer_key(user_id: str, api_key: str) -> None:
    """Insert or replace the Buffer API key for a user."""
    now = _now()
    with _conn() as c:
        c.execute("""
            INSERT INTO buffer_keys (user_id, api_key, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET api_key=excluded.api_key, updated_at=excluded.updated_at
        """, (user_id, api_key, now, now))
        c.commit()


def get_buffer_key(user_id: str) -> str | None:
    """Return the stored Buffer API key for a user, or None."""
    with _conn() as c:
        row = c.execute(
            "SELECT api_key FROM buffer_keys WHERE user_id = ?", (user_id,)
        ).fetchone()
    return row["api_key"] if row else None


def delete_buffer_key(user_id: str) -> bool:
    """Remove the stored Buffer API key. Returns True if a row was deleted."""
    with _conn() as c:
        cur = c.execute("DELETE FROM buffer_keys WHERE user_id = ?", (user_id,))
        c.commit()
    return cur.rowcount > 0


# ── Publish log ───────────────────────────────────────────────────────────────

def log_publish(
    user_id: str,
    clip: str,
    channel_id: str,
    post_id: str | None,
    status: str = "queued",
    error_msg: str | None = None,
) -> dict:
    """Insert a publish-log entry and return it as a dict."""
    now = _now()
    with _conn() as c:
        cur = c.execute(
            """
            INSERT INTO publish_log
                (user_id, clip, channel_id, post_id, status, error_msg, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, clip, channel_id, post_id, status, error_msg, now),
        )
        c.commit()
        row = c.execute(
            "SELECT * FROM publish_log WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
    return dict(row)


def get_publish_history(user_id: str, limit: int = 50) -> list[dict]:
    """Return the most recent publish-log entries for a user, newest first."""
    with _conn() as c:
        rows = c.execute(
            """
            SELECT * FROM publish_log
            WHERE user_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


# ── Legacy queue API (kept for backward compat) ───────────────────────────────

def enqueue(
    user_id: str,
    video_path: str,
    caption: str,
    scheduled_time: str | None = None,
    platform: str = "buffer",
) -> dict:
    now = _now()
    with _conn() as c:
        cur = c.execute(
            """
            INSERT INTO publish_queue
                (user_id, video_path, caption, platform, scheduled_time, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
            """,
            (user_id, video_path, caption, platform, scheduled_time, now, now),
        )
        c.commit()
        return get_item(cur.lastrowid)


def get_queue(user_id: str) -> list[dict]:
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM publish_queue WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_item(item_id: int) -> dict | None:
    with _conn() as c:
        row = c.execute(
            "SELECT * FROM publish_queue WHERE id = ?", (item_id,)
        ).fetchone()
    return dict(row) if row else None


def set_status(item_id: int, status: str, error_msg: str | None = None) -> None:
    if status not in VALID_STATUSES:
        raise ValueError(f"Invalid status: {status!r}")
    with _conn() as c:
        c.execute(
            "UPDATE publish_queue SET status=?, error_msg=?, updated_at=? WHERE id=?",
            (status, error_msg, _now(), item_id),
        )
        c.commit()


def delete_item(item_id: int) -> bool:
    with _conn() as c:
        cur = c.execute("DELETE FROM publish_queue WHERE id=?", (item_id,))
        c.commit()
    return cur.rowcount > 0
