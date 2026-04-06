import os
import json
import time
import requests
from utils import logger


PRIMARY_MODEL = "qwen/qwen3-32b"
FALLBACK_MODEL = "deepseek/deepseek-chat"
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


def score_segments(segments: list[dict], top_n: int = 5) -> list[dict]:
    """Score all segments in one API call; return top_n by score."""
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY not set")

    prompt = _build_prompt(segments)

    for attempt in range(2):
        model = PRIMARY_MODEL if attempt == 0 else FALLBACK_MODEL
        logger.info(f"Scoring {len(segments)} segments with model: {model} (attempt {attempt + 1})")
        try:
            result = _call_api(api_key, model, prompt)
            scored = _parse_response(result, segments)
            top = sorted(scored, key=lambda x: x["score"], reverse=True)[:top_n]
            logger.info(f"Selected top {len(top)} segments by score")
            for i, s in enumerate(top):
                logger.info(f"  [{i+1}] {s['start']:.1f}s–{s['end']:.1f}s | score={s['score']:.1f} | {s.get('reason', '')}")
            return top
        except Exception as e:
            logger.warning(f"Attempt {attempt + 1} failed with model {model}: {e}")
            if attempt == 0:
                logger.info("Retrying with fallback model...")
                time.sleep(2)
            else:
                raise RuntimeError(f"AI scoring failed after 2 attempts: {e}")

    raise RuntimeError("AI scoring failed unexpectedly")


def _build_prompt(segments: list[dict]) -> str:
    numbered = []
    for i, seg in enumerate(segments):
        numbered.append(
            f"[{i}] start={seg['start']:.1f}s end={seg['end']:.1f}s\n{seg['text'][:400]}"
        )
    joined = "\n\n".join(numbered)

    return (
        "You are an expert video editor selecting the most engaging short clips from a YouTube video.\n"
        "Score each segment from 0 to 10 based on: informativeness, entertainment value, emotional impact, "
        "clarity, and standalone watchability.\n\n"
        "Segments:\n"
        f"{joined}\n\n"
        "Return ONLY a valid JSON array. No explanation, no markdown, no code fences. Format:\n"
        '[\n  {"start": <seconds>, "end": <seconds>, "score": <float 0-10>, "reason": "<short reason>"}\n]\n'
        "Include ALL segments in your response."
    )


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


def _parse_response(content: str, original_segments: list[dict]) -> list[dict]:
    content = content.strip()

    start = content.find("[")
    end = content.rfind("]") + 1
    if start == -1 or end == 0:
        raise ValueError(f"No JSON array found in response:\n{content[:500]}")

    json_str = content[start:end]
    try:
        parsed = json.loads(json_str)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON from AI: {e}\nRaw: {json_str[:500]}")

    if not isinstance(parsed, list):
        raise ValueError(f"Expected list, got: {type(parsed)}")

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
        raise ValueError("No valid scored segments parsed from AI response")

    return result


def _find_segment(segments: list[dict], start: float, end: float, tolerance: float = 5.0) -> dict | None:
    for seg in segments:
        if abs(seg["start"] - start) <= tolerance and abs(seg["end"] - end) <= tolerance:
            return seg
    for seg in segments:
        if abs(seg["start"] - start) <= tolerance * 2:
            return seg
    return None
