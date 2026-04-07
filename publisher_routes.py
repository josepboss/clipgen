"""
publisher_routes.py — Flask Blueprint for /publisher/* endpoints.

Endpoints:
  GET    /publisher/session/:userId          — check if session exists (connected?)
  POST   /publisher/connect/:userId          — SSE: start Xvfb+Playwright login flow
  GET    /publisher/connect/status/:userId   — current screenshot (base64) + status
  POST   /publisher/connect/:userId/input    — relay click/type/key to the browser
  DELETE /publisher/session/:userId          — disconnect (delete session file)
  POST   /publisher/queue/:userId            — add a video to the publish queue
  GET    /publisher/queue/:userId            — get queue status for a user
  DELETE /publisher/queue/:userId/:itemId    — remove a queue item
  POST   /publisher/publish/:userId          — manually trigger publish for a user
"""

import os
import json
import base64
import shutil
import subprocess
import threading
import datetime
from flask import Blueprint, jsonify, request, Response, stream_with_context
from utils import logger
from publisher_db import enqueue, get_queue, get_item, set_status, delete_item

publisher_bp = Blueprint("publisher", __name__, url_prefix="/publisher")

OUTPUT_DIR  = "output"
SESSION_DIR = os.environ.get("SESSION_DIR", "./sessions")
TEMP_DIR    = os.path.join(os.path.dirname(__file__), "temp")

_PUBLISHER_DIR     = os.path.join(os.path.dirname(__file__), "src", "publisher")
_BROWSER_LOGIN_JS  = os.path.join(_PUBLISHER_DIR, "browser-login.js")
_TIKTOK_SCRIPT     = os.path.join(_PUBLISHER_DIR, "platforms", "tiktok.js")

os.makedirs(os.path.join(TEMP_DIR, "login_screenshots"), exist_ok=True)
os.makedirs(os.path.join(TEMP_DIR, "commands"), exist_ok=True)

# ── Per-user connect state ─────────────────────────────────────────────────────
# { userId: { status, message, proc, screenshot_path, commands_path } }
_connect_state: dict[str, dict] = {}
_connect_lock = threading.Lock()

# ── Login-process tracking for old /login endpoint ────────────────────────────
_login_procs: dict[str, subprocess.Popen] = {}
_login_lock = threading.Lock()

XVFB_RUN = shutil.which("xvfb-run")


def _kill_orphan_xvfb():
    """Kill any leftover Xvfb processes from previous runs."""
    try:
        subprocess.run(["pkill", "-f", "Xvfb :99"], capture_output=True)
    except Exception:
        pass


_kill_orphan_xvfb()


# ── Helpers ────────────────────────────────────────────────────────────────────

def _session_path(user_id: str) -> str:
    return os.path.join(SESSION_DIR, user_id, "tiktok.json")


def _screenshot_path(user_id: str) -> str:
    return os.path.join(TEMP_DIR, "login_screenshots", f"{user_id}.jpeg")


def _commands_path(user_id: str) -> str:
    return os.path.join(TEMP_DIR, "commands", f"{user_id}.json")


def _read_screenshot_b64(user_id: str) -> str | None:
    path = _screenshot_path(user_id)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "rb") as f:
            return base64.b64encode(f.read()).decode()
    except Exception:
        return None


# ── Session status & disconnect ────────────────────────────────────────────────

@publisher_bp.route("/session/<user_id>", methods=["GET"])
def session_status(user_id: str):
    """Check whether a TikTok session exists for this user."""
    sp = _session_path(user_id)
    exists = os.path.exists(sp)
    mtime = None
    if exists:
        mtime = datetime.datetime.fromtimestamp(os.path.getmtime(sp)).isoformat()
    return jsonify({"userId": user_id, "connected": exists, "sessionSaved": mtime})


@publisher_bp.route("/session/<user_id>", methods=["DELETE"])
def disconnect_tiktok(user_id: str):
    """Delete the session file to disconnect the TikTok account."""
    # Kill any in-progress login
    with _connect_lock:
        state = _connect_state.pop(user_id, None)
    if state and state.get("proc"):
        try:
            state["proc"].terminate()
        except Exception:
            pass

    sp = _session_path(user_id)
    if os.path.exists(sp):
        os.remove(sp)
        logger.info(f"[publisher] Disconnected TikTok for user '{user_id}'")
        return jsonify({"ok": True, "message": "TikTok account disconnected."})
    return jsonify({"ok": False, "message": "No session found for this user."}), 404


# ── Connect flow (SSE) ─────────────────────────────────────────────────────────

@publisher_bp.route("/connect/<user_id>", methods=["POST"])
def connect_tiktok(user_id: str):
    """
    Start the browser login flow.
    Returns a Server-Sent Events stream:
      data: {"status":"opening",   "message":"..."}
      data: {"status":"waiting",   "message":"..."}
      data: {"status":"screenshot","message":""}
      data: {"status":"success",   "message":"...","sessionPath":"..."}
      data: {"status":"error",     "message":"..."}
    """
    if not os.path.exists(_BROWSER_LOGIN_JS):
        return jsonify({"error": "Publisher scripts not installed."}), 503

    with _connect_lock:
        existing = _connect_state.get(user_id)
        if existing and existing.get("proc") and existing["proc"].poll() is None:
            return jsonify({"error": "Login already in progress for this user."}), 409

    screenshot_path = _screenshot_path(user_id)
    commands_path   = _commands_path(user_id)

    # Build launch command — prefer xvfb-run for headless:false mode
    if XVFB_RUN:
        cmd = [
            XVFB_RUN,
            "--auto-servernum",
            "--server-args=-screen 0 1280x800x24",
            "node",
            _BROWSER_LOGIN_JS,
            user_id,
            screenshot_path,
            commands_path,
        ]
    else:
        # Fallback: pure headless Chromium (no DISPLAY needed)
        cmd = ["node", _BROWSER_LOGIN_JS, user_id, screenshot_path, commands_path]

    env = os.environ.copy()
    env["SESSION_DIR"] = SESSION_DIR
    env["LOGIN_TIMEOUT_SEC"] = "180"

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )

    with _connect_lock:
        _connect_state[user_id] = {
            "status": "opening",
            "message": "Launching browser…",
            "proc": proc,
            "screenshot_path": screenshot_path,
            "commands_path": commands_path,
        }

    def _generate():
        try:
            for raw_line in proc.stdout:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    event = json.loads(raw_line)
                except json.JSONDecodeError:
                    event = {"status": "info", "message": raw_line}

                with _connect_lock:
                    if user_id in _connect_state:
                        _connect_state[user_id].update({
                            "status": event.get("status", "waiting"),
                            "message": event.get("message", ""),
                        })

                # Don't send "screenshot" noise on every frame — let the status
                # endpoint handle image delivery; only relay control events.
                if event.get("status") != "screenshot":
                    yield f"data: {json.dumps(event)}\n\n"

                if event.get("status") in ("success", "error"):
                    break

        except GeneratorExit:
            proc.terminate()
        finally:
            proc.wait(timeout=5)
            with _connect_lock:
                _connect_state.pop(user_id, None)

    return Response(
        stream_with_context(_generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection":    "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@publisher_bp.route("/connect/status/<user_id>", methods=["GET"])
def connect_status(user_id: str):
    """
    Return the current connect status and the latest screenshot as base64.
    Called by the frontend every 2 seconds during the login flow.
    """
    with _connect_lock:
        state = _connect_state.get(user_id, {})

    screenshot = _read_screenshot_b64(user_id)
    return jsonify({
        "userId":     user_id,
        "status":     state.get("status", "idle"),
        "message":    state.get("message", ""),
        "screenshot": screenshot,
        "active":     bool(state),
    })


@publisher_bp.route("/connect/<user_id>/input", methods=["POST"])
def relay_input(user_id: str):
    """
    Relay user mouse/keyboard input to the running browser.

    Body JSON:
      { "commands": [
          {"type":"click","x":320,"y":240},
          {"type":"type","text":"hello"},
          {"type":"key","key":"Enter"},
          {"type":"scroll","delta":300}
      ]}
    """
    with _connect_lock:
        state = _connect_state.get(user_id)
    if not state:
        return jsonify({"error": "No active login session for this user."}), 404

    data = request.get_json(silent=True) or {}
    commands = data.get("commands", [])
    if not commands:
        return jsonify({"error": "No commands provided."}), 400

    commands_path = state["commands_path"]
    try:
        existing: list = []
        if os.path.exists(commands_path):
            try:
                existing = json.loads(open(commands_path).read()) or []
            except Exception:
                existing = []
        existing.extend(commands)
        with open(commands_path, "w") as f:
            json.dump(existing, f)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    return jsonify({"ok": True, "queued": len(commands)})


# ── Queue ──────────────────────────────────────────────────────────────────────

@publisher_bp.route("/queue/<user_id>", methods=["POST"])
def add_to_queue(user_id: str):
    """
    Add a video to the publish queue.

    Body JSON:
      {
        "video_path":     "clip_01.mp4",
        "caption":        "Check this out! #fyp",
        "scheduled_time": "2025-04-07T18:00:00Z"  (optional)
      }
    """
    data = request.get_json(silent=True) or {}
    video_path     = (data.get("video_path") or "").strip()
    caption        = (data.get("caption") or "").strip()
    scheduled_time = (data.get("scheduled_time") or "").strip() or None

    if not video_path:
        return jsonify({"error": "video_path is required"}), 400
    if not caption:
        return jsonify({"error": "caption is required"}), 400

    if not os.path.isabs(video_path):
        video_path = os.path.abspath(os.path.join(OUTPUT_DIR, os.path.basename(video_path)))

    if not os.path.exists(video_path):
        return jsonify({"error": f"Video file not found: {video_path}"}), 404

    if not os.path.exists(_session_path(user_id)):
        return jsonify({
            "error": f"TikTok not connected for user '{user_id}'. Use Connect TikTok first.",
            "sessionMissing": True,
        }), 409

    item = enqueue(user_id, video_path, caption, scheduled_time)
    return jsonify({"ok": True, "item": item}), 201


@publisher_bp.route("/queue/<user_id>", methods=["GET"])
def list_queue(user_id: str):
    items = get_queue(user_id)
    return jsonify({"userId": user_id, "total": len(items), "items": items})


@publisher_bp.route("/queue/<user_id>/<int:item_id>", methods=["DELETE"])
def remove_queue_item(user_id: str, item_id: int):
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
    """Immediately post the next pending item for this user."""
    if not os.path.exists(_TIKTOK_SCRIPT):
        return jsonify({"error": "Publisher scripts not installed"}), 503

    data    = request.get_json(silent=True) or {}
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

    from publisher_scheduler import _post_item  # noqa: PLC0415
    _post_item(item)

    updated = get_item(item["id"])
    return jsonify({"ok": updated["status"] == "done", "item": updated})
