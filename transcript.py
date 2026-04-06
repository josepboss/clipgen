import os
import json
import requests
from youtube_transcript_api import YouTubeTranscriptApi, TranscriptsDisabled, NoTranscriptFound
from utils import logger, extract_video_id


def fetch_transcript(url: str) -> list[dict]:
    video_id = extract_video_id(url)
    logger.info(f"Fetching transcript for video ID: {video_id}")

    try:
        transcript_list = YouTubeTranscriptApi.get_transcript(video_id)
        logger.info(f"Transcript fetched via YouTube API: {len(transcript_list)} segments")
        return transcript_list
    except (TranscriptsDisabled, NoTranscriptFound) as e:
        logger.warning(f"YouTube transcript not available: {e}")
        return _fallback_whisper(url)
    except Exception as e:
        logger.warning(f"Transcript fetch error: {e}")
        return _fallback_whisper(url)


def _fallback_whisper(url: str) -> list[dict]:
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY not set and YouTube transcript unavailable")

    logger.info("Falling back to Whisper transcription via OpenRouter...")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://clipgen.local",
        "X-Title": "ClipGen",
    }

    payload = {
        "model": "openai/whisper-large-v3",
        "messages": [
            {
                "role": "user",
                "content": (
                    f"Please transcribe the audio from this YouTube video URL: {url}\n"
                    "Return a JSON array of segments with 'start', 'duration', and 'text' fields."
                ),
            }
        ],
    }

    response = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers=headers,
        json=payload,
        timeout=120,
    )
    response.raise_for_status()

    content = response.json()["choices"][0]["message"]["content"]

    try:
        start = content.find("[")
        end = content.rfind("]") + 1
        segments = json.loads(content[start:end])
        logger.info(f"Whisper transcription: {len(segments)} segments")
        return segments
    except Exception as e:
        raise RuntimeError(f"Failed to parse Whisper response as JSON: {e}\nRaw: {content}")


def build_segments(transcript: list[dict], min_dur: float = 30.0, max_dur: float = 90.0) -> list[dict]:
    """Merge transcript entries into coherent segments between min_dur and max_dur seconds."""
    segments = []
    current_text = []
    current_start = None
    current_end = None

    for entry in transcript:
        start = float(entry.get("start", 0))
        duration = float(entry.get("duration", 2))
        end = start + duration
        text = entry.get("text", "").strip()

        if current_start is None:
            current_start = start
            current_end = end
            current_text.append(text)
            continue

        current_len = current_end - current_start

        if current_len >= max_dur:
            segments.append({
                "start": current_start,
                "end": current_end,
                "text": " ".join(current_text),
            })
            current_start = start
            current_end = end
            current_text = [text]
        elif current_len >= min_dur and _ends_sentence(current_text):
            segments.append({
                "start": current_start,
                "end": current_end,
                "text": " ".join(current_text),
            })
            current_start = start
            current_end = end
            current_text = [text]
        else:
            current_end = end
            current_text.append(text)

    if current_start is not None and current_text:
        duration = current_end - current_start
        if duration >= min_dur:
            segments.append({
                "start": current_start,
                "end": current_end,
                "text": " ".join(current_text),
            })
        elif segments:
            last = segments[-1]
            last["end"] = current_end
            last["text"] += " " + " ".join(current_text)

    logger.info(f"Built {len(segments)} segments from transcript")
    return segments


def _ends_sentence(text_parts: list[str]) -> bool:
    if not text_parts:
        return False
    last = text_parts[-1].strip()
    return last.endswith((".", "!", "?", "..."))
