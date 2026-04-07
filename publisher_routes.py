"""
publisher_routes.py — Flask Blueprint for /publisher/* endpoints.

Endpoints:
  POST /publisher/login/:userId          — trigger browser login (saves session)
  POST /publisher/queue/:userId          — add a video to the publish queue
  GET  /publisher/queue/:userId          — get queue status for a user
  POST /publisher/publish/:userId        — manually trigger a publish run for a user
  DELETE /publisher/queue/:userId/:itemId — remove a queue item
  GET  /publisher/session/:userId        — check if a user has a valid session file
"""

import os
import json
import subprocess
import threading
from flask import Blueprint, jsonify, request
from utils import logger
from publisher_db import enqueue, get_queue, get_item, set_status, delete_item

publisher_bp = Blueprint("publisher", __name__, url_prefix="/publisher")

OUTPUT_DIR = "output"
SESSION_DIR = os.environ.get("SESSION_DIR", "./sessions")
HEADLESS = os.environ.get("HEADLESS", "true")

_PUBLISHER_DIR = os.path.join(os.path.dirname(__file__), "src", "publisher")
_SESSION_LOGIN_SCRIPT = os.path.join(_PUBLISHER_DIR, "session-login.js")
_TIKTOK_SCRIPT = os.path.join(_PUBLISHER_DIR, "platforms", "tiktok.js")

# Track in-progress login processes per userId {userId: subprocess.Popen}
_login_procs: dict[str, subprocess.Popen] = {}
_login_lock = threading.Lock()


# ── Session ────────────────────────────────────────────────────────────────────

@publisher_bp.route("/session/<user_id>", methods=["GET"])
def session_status(user_id: str):
    """Check if a session file exists for this user."""
    session_path = os.path.join(SESSION_DIR, user_id, "tiktok.json")
    exists = os.path.exists(session_path)
    mtime = None
    if exists:
        import datetime
        mtime = datetime.datetime.fromtimestamp(
            os.path.getmtime(session_path)
        ).isoformat()
    return jsonify({"userId": user_id, "sessionExists": exists, "sessionSaved": mtime})


@publisher_bp.route("/login/<user_id>", methods=["POST"])
def trigger_login(user_id: str):
    """
    Start the browser login flow for userId.

    In HEADLESS=false mode this opens a real Chrome window — only works when
    running locally or via remote desktop. In HEADLESS=true mode (default/server)
    a QR-code / manual-cookie workflow is required instead; this endpoint returns
    instructions.

    POST body (optional JSON):
      { "timeout_sec": 120 }
    """
    if not os.path.exists(_SESSION_LOGIN_SCRIPT):
        return jsonify({"error": "Publisher scripts not installed. Run: npm install in src/publisher/"}), 503

    data = request.get_json(silent=True) or {}
    timeout_sec = int(data.get("timeout_sec", 120))

    if HEADLESS.lower() == "true":
        return jsonify({
            "ok": False,
            "error": "Server is running in HEADLESS=true mode.",
            "instructions": (
                "To connect your TikTok account:\n"
                "1. Set HEADLESS=false in your environment.\n"
                "2. Run locally: node src/publisher/session-login.js " + user_id + "\n"
                "3. Log in when the browser opens.\n"
                "4. Copy sessions/" + user_id + "/tiktok.json to the server."
            ),
        }), 400

    with _login_lock:
        existing = _login_procs.get(user_id)
        if existing and existing.poll() is None:
            return jsonify({"ok": False, "error": f"Login already in progress for user '{user_id}'"}), 409

    env = os.environ.copy()
    env["SESSION_DIR"] = SESSION_DIR
    env["HEADLESS"] = "false"
    env["LOGIN_TIMEOUT_SEC"] = str(timeout_sec)

    proc = subprocess.Popen(
        ["node", _SESSION_LOGIN_SCRIPT, user_id],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )

    with _login_lock:
        _login_procs[user_id] = proc

    # Wait in a background thread; result retrievable via GET /publisher/session/:userId
    def _wait():
        stdout, stderr = proc.communicate()
        last_line = stdout.strip().split("\n")[-1] if stdout.strip() else "{}"
        try:
            payload = json.loads(last_line)
        except json.JSONDecodeError:
            payload = {"ok": False, "error": f"Unexpected output: {stdout[:200]}"}
        if payload.get("ok"):
            logger.info(f"[publisher] Login succeeded for user '{user_id}'")
        else:
            logger.warning(f"[publisher] Login failed for user '{user_id}': {payload.get('error')}")
        with _login_lock:
            _login_procs.pop(user_id, None)

    threading.Thread(target=_wait, daemon=True).start()

    return jsonify({
        "ok": True,
        "message": f"Browser opened for user '{user_id}'. Log in within {timeout_sec}s.",
        "userId": user_id,
    }), 202


# ── Queue ──────────────────────────────────────────────────────────────────────

@publisher_bp.route("/queue/<user_id>", methods=["POST"])
def add_to_queue(user_id: str):
    """
    Add a video to the publish queue.

    Body JSON:
      {
        "video_path":     "clip_01.mp4",          -- filename in output/ OR absolute path
        "caption":        "Check this out! #fyp",
        "scheduled_time": "2025-04-07T18:00:00Z"  -- optional ISO-8601; omit to post ASAP
      }
    """
    data = request.get_json(silent=True) or {}
    video_path = (data.get("video_path") or "").strip()
    caption = (data.get("caption") or "").strip()
    scheduled_time = (data.get("scheduled_time") or "").strip() or None

    if not video_path:
        return jsonify({"error": "video_path is required"}), 400
    if not caption:
        return jsonify({"error": "caption is required"}), 400

    # Resolve to absolute path (default: output/ directory)
    if not os.path.isabs(video_path):
        video_path = os.path.abspath(os.path.join(OUTPUT_DIR, os.path.basename(video_path)))

    if not os.path.exists(video_path):
        return jsonify({"error": f"Video file not found: {video_path}"}), 404

    session_path = os.path.join(SESSION_DIR, user_id, "tiktok.json")
    if not os.path.exists(session_path):
        return jsonify({
            "error": f"No TikTok session for user '{user_id}'. Connect their account first via POST /publisher/login/{user_id}",
            "sessionMissing": True,
        }), 409

    item = enqueue(user_id, video_path, caption, scheduled_time)
    return jsonify({"ok": True, "item": item}), 201


@publisher_bp.route("/queue/<user_id>", methods=["GET"])
def list_queue(user_id: str):
    """Return the full publish queue for a user."""
    items = get_queue(user_id)
    return jsonify({"userId": user_id, "total": len(items), "items": items})


@publisher_bp.route("/queue/<user_id>/<int:item_id>", methods=["DELETE"])
def remove_queue_item(user_id: str, item_id: int):
    """Delete a specific queue item (only if it belongs to this user)."""
    item = get_item(item_id)
    if not item:
        return jsonify({"error": "Item not found"}), 404
    if item["user_id"] != user_id:
        return jsonify({"error": "Item does not belong to this user"}), 403
    if item["status"] == "running":
        return jsonify({"error": "Cannot delete an item that is currently running"}), 409
    delete_item(item_id)
    return jsonify({"ok": True, "deleted": item_id})


# ── Manual publish trigger ─────────────────────────────────────────────────────

@publisher_bp.route("/publish/<user_id>", methods=["POST"])
def manual_publish(user_id: str):
    """
    Immediately post the next pending item for this user (bypasses scheduled time).

    Optionally accepts body: { "item_id": 42 } to publish a specific item.
    Runs synchronously — returns the result. May take a few minutes.
    """
    if not os.path.exists(_TIKTOK_SCRIPT):
        return jsonify({"error": "Publisher scripts not installed"}), 503

    data = request.get_json(silent=True) or {}
    item_id = data.get("item_id")

    if item_id:
        item = get_item(int(item_id))
        if not item:
            return jsonify({"error": f"Queue item {item_id} not found"}), 404
        if item["user_id"] != user_id:
            return jsonify({"error": "Item does not belong to this user"}), 403
    else:
        pending = [i for i in get_queue(user_id) if i["status"] == "pending"]
        if not pending:
            return jsonify({"ok": False, "message": "No pending items for this user"}), 404
        item = pending[0]

    if item["status"] == "running":
        return jsonify({"error": "Item is already being processed"}), 409

    # Import the scheduler helper to reuse the same posting logic
    from publisher_scheduler import _post_item  # noqa: PLC0415
    _post_item(item)

    updated = get_item(item["id"])
    return jsonify({"ok": updated["status"] == "done", "item": updated})
