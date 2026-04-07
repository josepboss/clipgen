"""
publisher_db.py — SQLite queue and session management for the TikTok publisher.

Schema:
  publish_queue
    id             INTEGER PK AUTOINCREMENT
    user_id        TEXT    — caller-supplied identifier (e.g. "user_42", "alice")
    video_path     TEXT    — absolute or relative path to the .mp4 file
    caption        TEXT    — post caption / description
    platform       TEXT    — "tiktok" (extensible later)
    scheduled_time TEXT    — ISO-8601 datetime; NULL = post ASAP
    status         TEXT    — pending | running | done | failed | session_expired
    error_msg      TEXT    — populated on failure
    created_at     TEXT    — ISO-8601
    updated_at     TEXT    — ISO-8601
"""

import sqlite3
import os
from datetime import datetime, timezone

DB_PATH = os.path.join(os.path.dirname(__file__), "publisher.db")

VALID_STATUSES = {"pending", "running", "done", "failed", "session_expired"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Create tables if they don't exist."""
    with _conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS publish_queue (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id        TEXT    NOT NULL,
                video_path     TEXT    NOT NULL,
                caption        TEXT    NOT NULL,
                platform       TEXT    NOT NULL DEFAULT 'tiktok',
                scheduled_time TEXT,
                status         TEXT    NOT NULL DEFAULT 'pending',
                error_msg      TEXT,
                created_at     TEXT    NOT NULL,
                updated_at     TEXT    NOT NULL
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_queue_user ON publish_queue(user_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_queue_status ON publish_queue(status)")
        c.commit()


def enqueue(
    user_id: str,
    video_path: str,
    caption: str,
    scheduled_time: str | None = None,
    platform: str = "tiktok",
) -> dict:
    """Add a new item to the publish queue. Returns the created row as a dict."""
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
    """Return all queue items for a user, newest first."""
    with _conn() as c:
        rows = c.execute(
            "SELECT * FROM publish_queue WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_item(item_id: int) -> dict | None:
    with _conn() as c:
        row = c.execute("SELECT * FROM publish_queue WHERE id = ?", (item_id,)).fetchone()
    return dict(row) if row else None


def get_due_items(platform: str = "tiktok") -> list[dict]:
    """Return pending items whose scheduled_time <= now (or NULL), ordered oldest-first."""
    now = _now()
    with _conn() as c:
        rows = c.execute(
            """
            SELECT * FROM publish_queue
            WHERE platform = ?
              AND status = 'pending'
              AND (scheduled_time IS NULL OR scheduled_time <= ?)
            ORDER BY created_at ASC
            """,
            (platform, now),
        ).fetchall()
    return [dict(r) for r in rows]


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
