import os
import re
import json
import time
import requests
from utils import logger

PRIMARY_MODEL = "qwen/qwen3-32b"
FALLBACK_MODEL = "deepseek/deepseek-chat"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
MIN_SEGMENTS = 3


def score_segments(segments: list[dict], top_n: int = 5) -> list[dict]:
    """Score all segments with AI; return top_n by score.

    Attempt order:
      1. PRIMARY_MODEL  (first try)
      2. FALLBACK_MODEL (second try on any failure)
      3. Position-based fallback (never crashes)
    Always returns at least MIN_SEGMENTS valid segments.
    """
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        logger.warning("OPENROUTER_API_KEY not set – using position-based fallback")
        return _position_based_fallback(segments, top_n)

    if not segments:
        raise RuntimeError("No segments provided to score")

    prompt = _build_prompt(segments)

    for attempt, model in enumerate([PRIMARY_MODEL, FALLBACK_MODEL]):
        logger.info(f"Scoring {len(segments)} segments with {model} (attempt {attempt + 1})")
        try:
            raw = _call_api(api_key, model, prompt)
            cleaned = _strip_thinking(raw)
            scored = _parse_response(cleaned, segments)
            top = sorted(scored, key=lambda x: x["score"], reverse=True)[:top_n]
            if len(top) < MIN_SEGMENTS:
                logger.warning(f"AI returned only {len(top)} segments – supplementing with position-based")
                top = _supplement(top, segments, top_n)
            logger.info(f"Selected {len(top)} segments")
            for i, s in enumerate(top):
                logger.info(
                    f"  [{i+1}] {s['start']:.1f}s–{s['end']:.1f}s "
                    f"score={s['score']:.1f} {s.get('reason','')[:60]}"
                )
            return top
        except Exception as e:
            logger.warning(f"Attempt {attempt + 1} failed ({model}): {e}")
            if attempt == 0:
                logger.info("Retrying with fallback model in 2s…")
                time.sleep(2)

    logger.warning("Both AI attempts failed – using position-based selection")
    return _position_based_fallback(segments, top_n)


# ── Prompt ───────────────────────────────────────────────────────────────────

def _build_prompt(segments: list[dict]) -> str:
    numbered = []
    for i, seg in enumerate(segments):
        numbered.append(
            f"[{i}] start={seg['start']:.1f}s end={seg['end']:.1f}s\n{seg['text'][:400]}"
        )
    joined = "\n\n".join(numbered)
    return (
        "You are an expert video editor selecting the most engaging short clips from a YouTube video.\n"
        "Score each segment 0–10 based on: informativeness, entertainment value, emotional impact, "
        "clarity, and standalone watchability.\n\n"
        "Segments:\n"
        f"{joined}\n\n"
        "Return ONLY a valid JSON array. No markdown, no code fences, no explanation. Format:\n"
        '[\n  {"start": <seconds>, "end": <seconds>, "score": <float 0-10>, "reason": "<short reason>"}\n]\n'
        "Include ALL segments in your response."
    )


# ── API call ─────────────────────────────────────────────────────────────────

def _call_api(api_key: str, model: str, prompt: str) -> str:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://clipgen.local",
        "X-Title": "ClipGen",
    }
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.3,
    }
    resp = requests.post(OPENROUTER_URL, headers=headers, json=payload, timeout=120)
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


# ── Parsing ──────────────────────────────────────────────────────────────────

def _strip_thinking(content: str) -> str:
    """Remove <think>…</think> chain-of-thought blocks (e.g. qwen3)."""
    stripped = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL)
    return stripped.strip()


def _parse_response(content: str, original_segments: list[dict]) -> list[dict]:
    content = content.strip()

    # Extract JSON array even if surrounded by stray text
    start = content.find("[")
    end = content.rfind("]") + 1
    if start == -1 or end == 0:
        raise ValueError(f"No JSON array in response:\n{content[:400]}")

    json_str = content[start:end]
    try:
        parsed = json.loads(json_str)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON: {e}\nRaw: {json_str[:400]}")

    if not isinstance(parsed, list):
        raise ValueError(f"Expected list, got {type(parsed)}")

    result = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        try:
            score = float(item.get("score", 0))
            start_t = float(item.get("start", 0))
            end_t = float(item.get("end", 0))
            reason = str(item.get("reason", ""))
            orig = _find_segment(original_segments, start_t, end_t)
            if orig:
                result.append({
                    "start": orig["start"],
                    "end": orig["end"],
                    "score": score,
                    "reason": reason,
                    "text": orig.get("text", ""),
                })
        except (TypeError, ValueError):
            continue

    if not result:
        raise ValueError("No valid scored segments in AI response")
    return result


def _find_segment(
    segments: list[dict], start: float, end: float, tolerance: float = 5.0
) -> dict | None:
    # Exact match within tolerance
    for seg in segments:
        if abs(seg["start"] - start) <= tolerance and abs(seg["end"] - end) <= tolerance:
            return seg
    # Looser: just match start
    for seg in segments:
        if abs(seg["start"] - start) <= tolerance * 2:
            return seg
    return None


# ── Fallbacks ────────────────────────────────────────────────────────────────

def _position_based_fallback(segments: list[dict], top_n: int) -> list[dict]:
    """Select evenly-spaced segments when AI is unavailable."""
    n = max(MIN_SEGMENTS, top_n)
    if not segments:
        return []
    if len(segments) <= n:
        return [dict(s, score=5.0, reason="position-based fallback") for s in segments]
    total = len(segments)
    step = total / n
    picked = [segments[int(i * step)] for i in range(n)]
    return [dict(s, score=5.0, reason="position-based fallback") for s in picked]


def _supplement(
    scored: list[dict], all_segments: list[dict], top_n: int
) -> list[dict]:
    """Add position-based segments to fill up to top_n / MIN_SEGMENTS."""
    need = max(MIN_SEGMENTS, top_n)
    existing_starts = {s["start"] for s in scored}
    extras = [
        dict(s, score=1.0, reason="position-based supplement")
        for s in all_segments
        if s["start"] not in existing_starts
    ]
    combined = scored + extras
    return combined[:need]
