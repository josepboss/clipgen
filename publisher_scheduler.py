"""
publisher_scheduler.py — Background scheduler that drains the publish queue.

Started as a daemon thread from app.py. Every PUBLISH_INTERVAL_MINUTES it picks up
all due "pending" items and calls the appropriate Node.js posting script.
"""

import os
import json
import subprocess
import threading
import time
from utils import logger
from publisher_db import get_due_items, set_status

INTERVAL_MINUTES = int(os.environ.get("PUBLISH_INTERVAL_MINUTES", "30"))
SESSION_DIR = os.environ.get("SESSION_DIR", "./sessions")
HEADLESS = os.environ.get("HEADLESS", "true")

# Path to the Node.js platform scripts
_PUBLISHER_DIR = os.path.join(os.path.dirname(__file__), "src", "publisher")
_PLATFORM_SCRIPTS = {
    "tiktok": os.path.join(_PUBLISHER_DIR, "platforms", "tiktok.js"),
}


def _post_item(item: dict) -> None:
    """Call the platform-specific Node.js script to post one queue item."""
    platform = item["platform"]
    script = _PLATFORM_SCRIPTS.get(platform)

    if not script or not os.path.exists(script):
        logger.error(f"[publisher] No script for platform '{platform}' — marking failed")
        set_status(item["id"], "failed", f"No publisher script for platform '{platform}'")
        return

    video_path = item["video_path"]
    if not os.path.isabs(video_path):
        video_path = os.path.join(os.path.dirname(__file__), video_path)

    if not os.path.exists(video_path):
        set_status(item["id"], "failed", f"Video file not found: {video_path}")
        logger.error(f"[publisher] Video not found for item {item['id']}: {video_path}")
        return

    set_status(item["id"], "running")
    logger.info(
        f"[publisher] Posting item {item['id']} for user '{item['user_id']}' "
        f"via {platform}: {os.path.basename(video_path)}"
    )

    env = os.environ.copy()
    env["SESSION_DIR"] = SESSION_DIR
    env["HEADLESS"] = HEADLESS

    try:
        result = subprocess.run(
            ["node", script, item["user_id"], video_path, item["caption"], str(item["id"])],
            capture_output=True,
            text=True,
            timeout=7 * 60,  # 7-minute hard timeout
            env=env,
        )

        # The script always writes a JSON line to stdout
        stdout = result.stdout.strip()
        last_line = stdout.split("\n")[-1] if stdout else "{}"
        try:
            payload = json.loads(last_line)
        except json.JSONDecodeError:
            payload = {"ok": False, "error": f"Non-JSON output: {stdout[:200]}"}

        if payload.get("ok"):
            set_status(item["id"], "done")
            logger.info(f"[publisher] Item {item['id']} posted successfully")
        elif payload.get("sessionExpired"):
            set_status(item["id"], "session_expired", payload.get("error", "Session expired"))
            logger.warning(
                f"[publisher] Item {item['id']}: session expired for user '{item['user_id']}'"
            )
        else:
            err = payload.get("error", result.stderr[:300] or "Unknown error")
            set_status(item["id"], "failed", err)
            logger.error(f"[publisher] Item {item['id']} failed: {err}")

    except subprocess.TimeoutExpired:
        set_status(item["id"], "failed", "Publisher timed out after 7 minutes")
        logger.error(f"[publisher] Item {item['id']} timed out")
    except Exception as exc:
        set_status(item["id"], "failed", str(exc))
        logger.error(f"[publisher] Item {item['id']} exception: {exc}")


def _run_cycle() -> None:
    """Process all currently due queue items."""
    try:
        items = get_due_items()
        if items:
            logger.info(f"[publisher] Processing {len(items)} due item(s)")
            for item in items:
                _post_item(item)
    except Exception as exc:
        logger.error(f"[publisher] Scheduler cycle error: {exc}")


def start() -> threading.Thread:
    """Start the background scheduler thread. Call once at app startup."""
    interval_sec = INTERVAL_MINUTES * 60

    def _loop():
        logger.info(f"[publisher] Scheduler started — interval {INTERVAL_MINUTES}m")
        _run_cycle()  # run immediately on startup
        while True:
            time.sleep(interval_sec)
            _run_cycle()

    t = threading.Thread(target=_loop, daemon=True, name="publisher-scheduler")
    t.start()
    return t
