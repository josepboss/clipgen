import os
import re
import glob
import subprocess
import tempfile
from youtube_transcript_api import YouTubeTranscriptApi, YouTubeTranscriptApiException
from utils import logger, extract_video_id

_PREF_LANGS = ["en", "en-US", "en-GB", "en-CA", "en-AU"]


# ── Public API ──────────────────────────────────────────────────────────────

def fetch_transcript(url: str) -> list[dict]:
    """Fetch a word-timed transcript for a YouTube video.

    Attempt order:
      1. youtube_transcript_api – preferred languages
      2. youtube_transcript_api – any available language/auto-generated
      3. yt-dlp auto-subtitles (VTT → parse)
      4. Synthetic equal-time segments (ffprobe duration)
    Always returns a non-empty list.
    """
    video_id = extract_video_id(url)
    logger.info(f"Fetching transcript for video ID: {video_id}")

    # -- Attempt 1: youtube_transcript_api preferred languages ----------------
    ytt = YouTubeTranscriptApi()
    try:
        raw = ytt.fetch(video_id, languages=_PREF_LANGS)
        entries = _normalise(list(raw))
        if entries:
            logger.info(f"Transcript via YT API (preferred lang): {len(entries)} entries")
            return entries
    except Exception as e:
        logger.warning(f"YT API preferred-lang fetch failed: {e}")

    # -- Attempt 2: any available language / auto-generated -------------------
    try:
        tlist = ytt.list(video_id)
        for t in tlist:
            try:
                raw = t.fetch()
                entries = _normalise(list(raw))
                if entries:
                    logger.info(f"Transcript via YT API (lang={t.language_code}): {len(entries)} entries")
                    return entries
            except Exception:
                continue
        logger.warning("No usable transcript found in list")
    except Exception as e:
        logger.warning(f"YT API transcript list failed: {e}")

    # -- Attempt 3: yt-dlp auto-subtitles -------------------------------------
    logger.info("Trying yt-dlp subtitle extraction…")
    subs = _fallback_ytdlp_subs(url)
    if subs:
        logger.info(f"yt-dlp subtitles: {len(subs)} entries")
        return subs

    # -- Attempt 4: synthetic equal-time segments (no text) -------------------
    logger.warning("All transcript methods failed – using synthetic time segments")
    return _synthetic_segments(url)


def build_segments(
    transcript: list[dict],
    min_dur: float = 30.0,
    max_dur: float = 90.0,
) -> list[dict]:
    """Merge transcript entries into coherent 30–90 s segments.

    Guarantees at least 3 segments when possible.
    """
    segments = _merge_entries(transcript, min_dur, max_dur)

    # If we got very few segments try a more lenient merge
    if len(segments) < 3 and len(transcript) > 0:
        logger.info(f"Only {len(segments)} segment(s) – retrying with shorter min_dur=15s")
        segments = _merge_entries(transcript, min_dur=15.0, max_dur=max_dur)

    # Still empty? Wrap the whole transcript as one big segment
    if not segments and transcript:
        start = float(transcript[0].get("start", 0))
        last = transcript[-1]
        end = float(last.get("start", 0)) + float(last.get("duration", 2))
        segments = [{"start": start, "end": end, "text": _join_text(transcript)}]

    logger.info(f"Built {len(segments)} segments from transcript")
    return segments


# ── Private helpers ──────────────────────────────────────────────────────────

def _normalise(entries: list) -> list[dict]:
    """Convert any transcript entry format to {start, duration, text} dicts."""
    result = []
    for e in entries:
        try:
            if hasattr(e, "start"):
                result.append({
                    "start": float(e.start),
                    "duration": float(getattr(e, "duration", 2.0)),
                    "text": str(getattr(e, "text", "")).strip(),
                })
            elif isinstance(e, dict):
                result.append({
                    "start": float(e.get("start", 0)),
                    "duration": float(e.get("duration", 2.0)),
                    "text": str(e.get("text", "")).strip(),
                })
        except Exception:
            continue
    return [e for e in result if e.get("text")]


def _fallback_ytdlp_subs(url: str) -> list[dict]:
    """Download auto-generated VTT subtitles via yt-dlp and parse them."""
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            out_template = os.path.join(tmpdir, "subs.%(ext)s")
            cmd = [
                "yt-dlp",
                "--write-auto-subs",
                "--skip-download",
                "--sub-langs", "en.*",
                "--sub-format", "vtt",
                "-o", out_template,
                "--no-playlist",
                "--quiet",
                url,
            ]
            subprocess.run(cmd, capture_output=True, text=True, timeout=60)

            vtt_files = glob.glob(os.path.join(tmpdir, "*.vtt"))
            if not vtt_files:
                logger.warning("yt-dlp: no VTT subtitle file produced")
                return []

            with open(vtt_files[0], encoding="utf-8", errors="replace") as f:
                content = f.read()

            entries = _parse_vtt(content)
            return entries
    except Exception as e:
        logger.warning(f"yt-dlp subtitle extraction failed: {e}")
        return []


def _parse_vtt(content: str) -> list[dict]:
    """Parse WebVTT into {start, duration, text} list, deduplicating auto-sub repeats.

    YouTube auto-subtitles emit the same caption text in consecutive overlapping
    windows (rolling captions). We skip an entry if its normalised text is identical
    to the previous entry's normalised text.
    """
    entries = []
    lines = content.splitlines()
    prev_text: str | None = None
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if "-->" in line:
            parts = line.split("-->")
            try:
                start = _vtt_time(parts[0].strip())
                end = _vtt_time(parts[1].strip().split()[0])
            except Exception:
                i += 1
                continue
            i += 1
            text_parts = []
            while i < len(lines) and lines[i].strip():
                t = re.sub(r"<[^>]+>", "", lines[i]).strip()
                if t:
                    text_parts.append(t)
                i += 1
            text = " ".join(text_parts).strip()
            if text and text != prev_text:
                entries.append({
                    "start": start,
                    "duration": max(0.1, end - start),
                    "text": text,
                })
                prev_text = text
        else:
            i += 1
    return entries


def _vtt_time(s: str) -> float:
    """Parse HH:MM:SS.mmm or MM:SS.mmm to float seconds."""
    s = s.replace(",", ".")
    parts = s.split(":")
    if len(parts) == 3:
        return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
    if len(parts) == 2:
        return float(parts[0]) * 60 + float(parts[1])
    return float(parts[0])


def _synthetic_segments(url: str) -> list[dict]:
    """Create synthetic equal-time segments when no transcript is available."""
    total_seconds = _get_duration(url)
    seg_dur = 60.0
    segments = []
    t = 0.0
    seg_i = 1
    while t + seg_dur <= total_seconds:
        segments.append({
            "start": t,
            "duration": seg_dur,
            "text": f"[Segment {seg_i} – no transcript available]",
        })
        t += seg_dur
        seg_i += 1
    # Ensure at least 3 segments even for short videos
    if not segments:
        chunk = total_seconds / 3 if total_seconds >= 30 else 10
        for i in range(3):
            segments.append({
                "start": i * chunk,
                "duration": chunk,
                "text": f"[Segment {i+1} – no transcript available]",
            })
    logger.info(f"Synthetic segments: {len(segments)} for ~{total_seconds:.0f}s video")
    return segments


def _get_duration(url: str) -> float:
    """Get video duration in seconds via yt-dlp."""
    try:
        r = subprocess.run(
            ["yt-dlp", "--get-duration", "--no-playlist", "--quiet", url],
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode == 0:
            s = r.stdout.strip()
            parts = s.split(":")
            if len(parts) == 3:
                return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
            if len(parts) == 2:
                return int(parts[0]) * 60 + int(parts[1])
            return float(parts[0])
    except Exception as e:
        logger.warning(f"Could not get duration: {e}")
    return 600.0  # assume 10 min


def _merge_entries(
    transcript: list[dict],
    min_dur: float,
    max_dur: float,
) -> list[dict]:
    segments: list[dict] = []
    current_text: list[str] = []
    current_start: float | None = None
    current_end: float = 0.0

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
            segments.append({"start": current_start, "end": current_end, "text": _join_text(current_text)})
            current_start, current_end, current_text = start, end, [text]
        elif current_len >= min_dur and _ends_sentence(current_text):
            segments.append({"start": current_start, "end": current_end, "text": _join_text(current_text)})
            current_start, current_end, current_text = start, end, [text]
        else:
            current_end = end
            current_text.append(text)

    if current_start is not None and current_text:
        duration = current_end - current_start
        if duration >= min_dur:
            segments.append({"start": current_start, "end": current_end, "text": _join_text(current_text)})
        elif segments:
            segments[-1]["end"] = current_end
            segments[-1]["text"] += " " + _join_text(current_text)

    return segments


def _join_text(parts: list[str]) -> str:
    return " ".join(p for p in parts if p)


def _ends_sentence(text_parts: list[str]) -> bool:
    if not text_parts:
        return False
    last = text_parts[-1].strip()
    return last.endswith((".", "!", "?", "…", "..."))
