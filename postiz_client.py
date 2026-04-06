import os
import requests
from datetime import datetime
from config_manager import load_config
from utils import logger


def schedule_post(
    video_path: str,
    caption: str,
    scheduled_time: str,
    platform: str | None = None,
) -> dict:
    """Upload video and create a scheduled post via Postiz API.

    Returns {"success": True, "post_id": "..."} or {"success": False, "error": "..."}.
    """
    cfg = load_config()
    api_key = cfg.get("postiz_api_key", "").strip()
    base_url = cfg.get("postiz_base_url", "").strip().rstrip("/")

    if not api_key or not base_url:
        return {"success": False, "error": "Postiz is not configured. Set API key and base URL in Settings."}

    if not os.path.exists(video_path):
        return {"success": False, "error": f"Video file not found: {video_path}"}

    headers = {
        "Authorization": f"Bearer {api_key}",
    }

    try:
        parsed_time = _parse_scheduled_time(scheduled_time)
    except ValueError as e:
        return {"success": False, "error": f"Invalid scheduled_time: {e}"}

    try:
        media_id = _upload_media(base_url, headers, video_path)
    except Exception as e:
        logger.error(f"Postiz media upload failed: {e}")
        return {"success": False, "error": f"Media upload failed: {e}"}

    try:
        post_id = _create_post(base_url, headers, media_id, caption, parsed_time, platform)
    except Exception as e:
        logger.error(f"Postiz post creation failed: {e}")
        return {"success": False, "error": f"Post creation failed: {e}"}

    logger.info(f"Postiz scheduled: post_id={post_id}, time={parsed_time}")
    return {"success": True, "post_id": post_id, "scheduled_for": parsed_time}


def _upload_media(base_url: str, headers: dict, video_path: str) -> str:
    """Upload video file; returns media ID string."""
    upload_url = f"{base_url}/api/media"
    filename = os.path.basename(video_path)

    logger.info(f"Uploading media to Postiz: {filename}")
    with open(video_path, "rb") as f:
        resp = requests.post(
            upload_url,
            headers=headers,
            files={"file": (filename, f, "video/mp4")},
            timeout=120,
        )

    if resp.status_code not in (200, 201):
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")

    data = resp.json()
    media_id = data.get("id") or data.get("mediaId") or data.get("media_id")
    if not media_id:
        raise RuntimeError(f"No media ID in response: {data}")

    logger.info(f"Media uploaded, id={media_id}")
    return str(media_id)


def _create_post(
    base_url: str,
    headers: dict,
    media_id: str,
    caption: str,
    scheduled_time: str,
    platform: str | None,
) -> str:
    """Create a scheduled post; returns post ID string."""
    post_url = f"{base_url}/api/posts"
    payload: dict = {
        "content": caption,
        "scheduledAt": scheduled_time,
        "media": [{"id": media_id}],
    }
    if platform:
        payload["platform"] = platform

    logger.info(f"Creating Postiz post: {post_url}")
    json_headers = {**headers, "Content-Type": "application/json"}
    resp = requests.post(post_url, headers=json_headers, json=payload, timeout=30)

    if resp.status_code not in (200, 201):
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:300]}")

    data = resp.json()
    post_id = (
        data.get("id")
        or data.get("postId")
        or data.get("post_id")
        or data.get("data", {}).get("id")
        or "unknown"
    )
    return str(post_id)


def _parse_scheduled_time(raw: str) -> str:
    """Normalise scheduled_time to ISO 8601 UTC string."""
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(raw.strip(), fmt)
            return dt.isoformat() + "Z"
        except ValueError:
            continue
    raise ValueError(f"Cannot parse datetime: {raw!r}")
