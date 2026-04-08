"""buffer_routes.py — Flask Blueprint for /buffer/* endpoints.

Endpoints:
  POST   /buffer/validate         — validate api_key, fetch channels, store key
  GET    /buffer/key              — check if a key is stored (masked)
  DELETE /buffer/key              — remove stored key
  GET    /buffer/channels         — list connected channels (uses stored key)
  POST   /buffer/publish          — upload clip + create posts on selected channels
  GET    /buffer/posts            — fetch recent posts from Buffer
  GET    /buffer/history          — publish history from local DB
  GET    /buffer/test             — debug: raw API response for get_channels
"""

import json
import os
import requests as _requests
from flask import Blueprint, jsonify, request
from flask_login import login_required, current_user
from utils import logger
from buffer_client import BUFFER_API_URL, get_channels, create_post, get_posts
from publisher_db import (
    save_buffer_key,
    get_buffer_key,
    delete_buffer_key,
    log_publish,
    get_publish_history,
)

buffer_bp = Blueprint("buffer", __name__, url_prefix="/buffer")

OUTPUT_DIR = "output"


def _user_id() -> str:
    return current_user.id


# ── API key management ─────────────────────────────────────────────────────────

@buffer_bp.route("/key", methods=["GET"])
@login_required
def get_key():
    """Return whether a Buffer API key is stored (shows masked version)."""
    key = get_buffer_key(_user_id())
    if key:
        masked = key[:6] + "…" + key[-4:] if len(key) > 10 else "***"
        return jsonify({"configured": True, "key_hint": masked})
    return jsonify({"configured": False, "key_hint": None})


@buffer_bp.route("/key", methods=["DELETE"])
@login_required
def remove_key():
    """Remove the stored Buffer API key (disconnects Buffer)."""
    delete_buffer_key(_user_id())
    return jsonify({"ok": True})


# ── Validate + connect ─────────────────────────────────────────────────────────

@buffer_bp.route("/validate", methods=["POST"])
@login_required
def validate():
    """Validate an API key by fetching channels, then store it.

    Body: { "api_key": "..." }
    Returns: { "valid": true, "channels": [...] }
    """
    data = request.get_json(silent=True) or {}
    api_key = (data.get("api_key") or "").strip()
    if not api_key:
        return jsonify({"error": "api_key is required"}), 400
    try:
        channels = get_channels(api_key)
        save_buffer_key(_user_id(), api_key)
        logger.info(f"[buffer] user={_user_id()} API key validated — {len(channels)} channel(s)")
        return jsonify({"valid": True, "channels": channels})
    except Exception as exc:
        logger.warning(f"[buffer] user={_user_id()} validation failed: {exc}")
        return jsonify({"valid": False, "error": str(exc)}), 502


# ── Channels ───────────────────────────────────────────────────────────────────

@buffer_bp.route("/channels", methods=["GET"])
@login_required
def channels():
    """List all connected channels for the stored (or provided) API key."""
    api_key = (request.args.get("api_key") or "").strip()
    if not api_key:
        api_key = get_buffer_key(_user_id()) or ""
    if not api_key:
        return jsonify({"error": "No Buffer API key configured. Please connect Buffer first."}), 401
    try:
        ch = get_channels(api_key)
        return jsonify({"channels": ch})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502


# ── Publish ────────────────────────────────────────────────────────────────────

@buffer_bp.route("/publish", methods=["POST"])
@login_required
def publish():
    """Upload a clip to Buffer and create posts on one or more channels.

    Body JSON:
      {
        "api_key":       "...",       (optional — falls back to stored key)
        "channel_ids":   ["ch_abc"],  (required — at least one)
        "clip_filename": "clip_01.mp4",
        "caption":       "Check this out!",
        "due_at":        "2025-04-08T18:00:00Z"   (optional)
      }
    """
    uid          = _user_id()
    data         = request.get_json(silent=True) or {}
    api_key      = (data.get("api_key") or "").strip() or get_buffer_key(uid) or ""
    channel_ids  = data.get("channel_ids") or []
    clip_filename = (data.get("clip_filename") or "").strip()
    caption      = (data.get("caption") or "").strip()
    due_at       = (data.get("due_at") or "").strip() or None

    if not api_key:
        return jsonify({"error": "No Buffer API key configured."}), 401
    if not channel_ids:
        return jsonify({"error": "At least one channel_id is required."}), 400
    if not clip_filename:
        return jsonify({"error": "clip_filename is required."}), 400
    if not caption:
        return jsonify({"error": "caption is required."}), 400

    # Per-user clip directory
    user_output = os.path.join(OUTPUT_DIR, uid)
    video_path  = os.path.join(user_output, os.path.basename(clip_filename))
    if not os.path.exists(video_path):
        return jsonify({"error": f"Clip not found: {clip_filename}"}), 404

    results = []
    for ch_id in channel_ids:
        try:
            post = create_post(api_key, ch_id, caption, video_path, due_at)
            log_publish(uid, clip_filename, ch_id, post.get("post_id"), "queued")
            results.append({
                "channel_id":   ch_id,
                "post_id":      post.get("post_id"),
                "scheduled_at": post.get("scheduled_at"),
                "ok":           True,
            })
        except Exception as exc:
            logger.error(f"[buffer] user={uid} publish to {ch_id} failed: {exc}")
            log_publish(uid, clip_filename, ch_id, None, "failed", str(exc))
            results.append({"channel_id": ch_id, "ok": False, "error": str(exc)})

    return jsonify({"results": results})


# ── Posts ──────────────────────────────────────────────────────────────────────

@buffer_bp.route("/posts", methods=["GET"])
@login_required
def posts():
    """Return recent posts from Buffer."""
    uid        = _user_id()
    api_key    = (request.args.get("api_key") or "").strip() or get_buffer_key(uid) or ""
    channel_id = (request.args.get("channel_id") or "").strip() or None
    if not api_key:
        return jsonify({"error": "No Buffer API key configured."}), 401
    try:
        p = get_posts(api_key, channel_id=channel_id)
        return jsonify({"posts": p})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502


# ── History ────────────────────────────────────────────────────────────────────

@buffer_bp.route("/history", methods=["GET"])
@login_required
def history():
    """Return local publish history (clips sent to Buffer from this app)."""
    rows = get_publish_history(_user_id())
    return jsonify({"history": rows})


# ── Debug test ─────────────────────────────────────────────────────────────────

@buffer_bp.route("/test", methods=["GET"])
@login_required
def test_api():
    """Debug endpoint — make raw API call and return the full response body.

    Query params:
      api_key  (optional) — falls back to stored key
    """
    api_key = (request.args.get("api_key") or "").strip() or get_buffer_key(_user_id()) or ""
    if not api_key:
        return jsonify({"error": "No api_key provided and none stored."}), 400

    query = """
    query GetOrganizations {
      account {
        organizations {
          id
          name
          channels {
            id
            name
            service
          }
        }
      }
    }
    """
    payload = json.dumps({"query": query})
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    try:
        resp = _requests.post(BUFFER_API_URL, headers=headers, data=payload, timeout=30)
        return jsonify({
            "status_code": resp.status_code,
            "raw":         resp.json() if resp.headers.get("content-type", "").startswith("application/json") else resp.text,
            "parsed_channels": get_channels(api_key) if resp.ok else None,
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 502
