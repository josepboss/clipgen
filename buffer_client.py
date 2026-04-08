"""buffer_client.py — Buffer GraphQL API client (pure Python / requests).

Buffer API:
  GraphQL endpoint : https://api.buffer.com/graphql
  Media upload     : https://api.buffer.com/media  (multipart)
  Auth             : Authorization: Bearer {api_key}
"""

import os
import requests
from utils import logger

BUFFER_GQL  = "https://api.buffer.com/graphql"
BUFFER_MEDIA = "https://api.buffer.com/media"
TIMEOUT_SEC  = 30
UPLOAD_TIMEOUT = 120


# ── GraphQL helper ─────────────────────────────────────────────────────────────

def _gql(api_key: str, query: str, variables: dict | None = None) -> dict:
    """Execute a GraphQL query/mutation and return the ``data`` block.

    Raises:
        requests.HTTPError  — non-2xx HTTP status
        ValueError          — GraphQL ``errors`` field present
    """
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload: dict = {"query": query}
    if variables:
        payload["variables"] = variables

    resp = requests.post(BUFFER_GQL, json=payload, headers=headers, timeout=TIMEOUT_SEC)
    resp.raise_for_status()
    body = resp.json()
    if "errors" in body:
        msgs = [e.get("message", str(e)) for e in body["errors"]]
        raise ValueError("Buffer API errors: " + "; ".join(msgs))
    return body.get("data", {})


# ── Public API ─────────────────────────────────────────────────────────────────

def get_channels(api_key: str) -> list[dict]:
    """Return all connected social channels for this Buffer account.

    Each channel dict contains: id, name, service, avatar, serviceId, timezone.
    """
    query = """
    query GetChannels {
      channels {
        id
        name
        service
        avatar
        serviceId
        timezone
        type
      }
    }
    """
    data = _gql(api_key, query)
    channels = data.get("channels", [])
    logger.info(f"[buffer] {len(channels)} channel(s) fetched")
    return channels


def create_post(
    api_key: str,
    channel_id: str,
    text: str,
    video_path: str,
    due_at: str | None = None,
) -> dict:
    """Upload video and create a Buffer post.

    Args:
        api_key    : Buffer access token
        channel_id : Buffer channel ID to post to
        text       : Caption / post body
        video_path : Absolute or relative path to the .mp4 file
        due_at     : Optional ISO-8601 UTC datetime string for custom scheduling.
                     When None the post is added to the channel's queue.

    Returns:
        {"post_id": str, "scheduled_at": str | None, "status": str}
    """
    media_id = _upload_media(api_key, video_path)

    is_scheduled = bool(due_at)
    scheduling_type = "custom" if is_scheduled else "automatic"
    mode = "customScheduled" if is_scheduled else "addToQueue"

    mutation = """
    mutation CreatePost($input: CreatePostInput!) {
      createPost(input: $input) {
        post {
          id
          status
          dueAt
          text
        }
      }
    }
    """

    inp: dict = {
        "channelId": channel_id,
        "text": text,
        "schedulingType": scheduling_type,
        "mode": mode,
    }
    if media_id:
        inp["mediaIds"] = [media_id]
    if due_at:
        inp["dueAt"] = due_at

    data = _gql(api_key, mutation, {"input": inp})
    post = (data.get("createPost") or {}).get("post") or {}
    result = {
        "post_id":      post.get("id"),
        "scheduled_at": post.get("dueAt"),
        "status":       post.get("status"),
    }
    logger.info(
        f"[buffer] Post created on channel {channel_id}: "
        f"id={result['post_id']}, scheduled_at={result['scheduled_at']}"
    )
    return result


def get_posts(
    api_key: str,
    org_id: str | None = None,
    channel_id: str | None = None,
) -> list[dict]:
    """Return up to 20 recent posts for this Buffer account.

    Optionally filter by channel_id.
    """
    query = """
    query GetPosts($channelId: String) {
      posts(channelId: $channelId, first: 20) {
        edges {
          node {
            id
            text
            status
            dueAt
            channel {
              id
              name
              service
            }
          }
        }
      }
    }
    """
    variables = {}
    if channel_id:
        variables["channelId"] = channel_id

    data = _gql(api_key, query, variables or None)
    edges = (data.get("posts") or {}).get("edges") or []
    return [e["node"] for e in edges if "node" in e]


# ── Media upload ───────────────────────────────────────────────────────────────

def _upload_media(api_key: str, video_path: str) -> str | None:
    """Upload a video file to Buffer and return its media ID.

    Returns None on failure (post will be created without media).
    """
    if not os.path.exists(video_path):
        logger.warning(f"[buffer] Media upload skipped — file not found: {video_path}")
        return None

    try:
        headers = {"Authorization": f"Bearer {api_key}"}
        filename = os.path.basename(video_path)
        with open(video_path, "rb") as fh:
            resp = requests.post(
                BUFFER_MEDIA,
                headers=headers,
                files={"file": (filename, fh, "video/mp4")},
                timeout=UPLOAD_TIMEOUT,
            )
        resp.raise_for_status()
        body = resp.json()
        media_id = body.get("id") or body.get("mediaId")
        logger.info(f"[buffer] Media uploaded: {filename} → id={media_id}")
        return media_id
    except Exception as exc:
        logger.warning(f"[buffer] Media upload failed: {exc}")
        return None
