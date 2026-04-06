import os
import shutil
import time
from typing import Callable
from utils import logger, ensure_output_dir
from downloader import download_video
from transcript import fetch_transcript, build_segments
from ai_selector import score_segments
from video_editor import cut_clip, process_to_vertical

DOWNLOADS_DIR = "downloads"
TEMPS_DIR = "temp_clips"
MIN_FILE_SIZE = 10_000  # 10 KB — anything smaller is broken

# Output retention policy
MAX_CLIPS = 50        # never keep more than this many .mp4 files in output/
MAX_AGE_DAYS = 7      # delete files older than this regardless of count


def run_pipeline(
    url: str,
    output_dir: str = "output",
    top_n: int = 5,
    on_status: Callable[[str, str], None] | None = None,
) -> list[str]:
    """Run the full ClipGen pipeline. Returns list of absolute output file paths."""

    def update(step: str, detail: str = "") -> None:
        logger.info(f"[pipeline] {step}: {detail}")
        if on_status:
            on_status(step, detail)

    ensure_output_dir(output_dir)
    os.makedirs(TEMPS_DIR, exist_ok=True)
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)

    source_video: str | None = None

    try:
        # ── Step 1: Download ──────────────────────────────────────────────────
        _banner("STEP 1: Download video")
        update("downloading", "Starting video download…")
        source_video = download_video(url, DOWNLOADS_DIR)
        _validate_file(source_video, "downloaded video")
        update("downloading", f"Downloaded: {os.path.basename(source_video)}")

        # ── Step 2: Transcript ────────────────────────────────────────────────
        _banner("STEP 2: Fetch transcript")
        update("transcript", "Fetching transcript…")
        raw_transcript = fetch_transcript(url)
        if not raw_transcript:
            raise RuntimeError("Transcript fetch returned no entries")
        update("transcript", f"Got {len(raw_transcript)} transcript entries")

        # ── Step 3: Segmentation ──────────────────────────────────────────────
        _banner("STEP 3: Build segments")
        update("segmenting", "Building coherent segments…")
        segments = build_segments(raw_transcript)
        if not segments:
            raise RuntimeError("No segments could be extracted from transcript")
        update("segmenting", f"Built {len(segments)} segments")

        # Ensure we don't request more clips than we have segments
        effective_n = min(top_n, len(segments))
        if effective_n < top_n:
            logger.info(f"Capping top_n from {top_n} to {effective_n} (only {len(segments)} segments available)")

        # ── Step 4: AI scoring ────────────────────────────────────────────────
        _banner("STEP 4: AI segment scoring")
        update("scoring", f"Scoring {len(segments)} segments with AI…")
        top_segments = score_segments(segments, top_n=effective_n)
        if not top_segments:
            raise RuntimeError("No segments returned by AI scorer")
        top_segments.sort(key=lambda s: s["start"])
        update("scoring", f"Selected top {len(top_segments)} segments")

        # ── Step 5: Cut + vertical encode ─────────────────────────────────────
        _banner("STEP 5: Cut and process clips")
        output_files: list[str] = []

        for i, seg in enumerate(top_segments, start=1):
            start = float(seg["start"])
            end = float(seg["end"])
            clip_label = f"clip_{i:02d}"

            update("processing", f"Processing {clip_label} ({i}/{len(top_segments)})…")
            logger.info(f"[{clip_label}] {start:.1f}s – {end:.1f}s")

            temp_path = os.path.join(TEMPS_DIR, f"{clip_label}_raw.mp4")
            final_path = os.path.join(output_dir, f"{clip_label}.mp4")

            try:
                cut_clip(source_video, start, end, temp_path)
                _validate_file(temp_path, f"{clip_label} raw cut")

                process_to_vertical(temp_path, final_path)
                _validate_file(final_path, f"{clip_label} final")

                output_files.append(final_path)
                size_mb = os.path.getsize(final_path) / (1024 * 1024)
                logger.info(f"[{clip_label}] Saved {final_path} ({size_mb:.1f} MB)")
            except Exception as exc:
                logger.error(f"[{clip_label}] Failed, skipping: {exc}")
                # Remove partial output if it exists
                for path in (temp_path, final_path):
                    if os.path.exists(path):
                        try:
                            os.remove(path)
                        except OSError:
                            pass

        if not output_files:
            raise RuntimeError("No clips were produced (all encoding steps failed)")

        _banner(f"DONE: {len(output_files)} clips in '{output_dir}/'")

        # Enforce retention policy AFTER new clips are confirmed on disk
        _purge_old_clips(output_dir)

        return output_files

    finally:
        # Always clean up temps
        shutil.rmtree(TEMPS_DIR, ignore_errors=True)
        logger.info("Temp clips cleaned up")

        # Delete source video to free disk space
        if source_video and os.path.exists(source_video):
            try:
                os.remove(source_video)
                logger.info(f"Deleted source video: {source_video}")
            except OSError as e:
                logger.warning(f"Could not delete source video: {e}")

        # Clean up empty downloads dir
        try:
            if os.path.isdir(DOWNLOADS_DIR) and not os.listdir(DOWNLOADS_DIR):
                os.rmdir(DOWNLOADS_DIR)
        except OSError:
            pass


# ── Helpers ──────────────────────────────────────────────────────────────────

def _validate_file(path: str, label: str) -> None:
    """Raise if path doesn't exist or is suspiciously small."""
    if not os.path.exists(path):
        raise RuntimeError(f"[{label}] Output file was not created: {path}")
    size = os.path.getsize(path)
    if size < MIN_FILE_SIZE:
        raise RuntimeError(
            f"[{label}] Output file is too small ({size} bytes) – likely corrupt: {path}"
        )
    logger.info(f"[{label}] Validated: {path} ({size / 1024:.1f} KB)")


def _purge_old_clips(output_dir: str) -> None:
    """Enforce output retention policy.

    Two-pass approach (both run every time):
      Pass 1 – delete any .mp4 file whose mtime is older than MAX_AGE_DAYS.
      Pass 2 – if more than MAX_CLIPS files remain, delete the oldest ones
               until the count is at or below MAX_CLIPS.

    Errors are logged as warnings and never propagate to the caller.
    """
    try:
        if not os.path.isdir(output_dir):
            return

        mp4s = [
            os.path.join(output_dir, f)
            for f in os.listdir(output_dir)
            if f.lower().endswith(".mp4")
        ]

        if not mp4s:
            return

        now = time.time()
        cutoff = now - MAX_AGE_DAYS * 86_400

        # Pass 1: age-based deletion
        survivors: list[str] = []
        for path in mp4s:
            try:
                if os.path.getmtime(path) < cutoff:
                    os.remove(path)
                    logger.info(f"[retention] Removed stale clip (>{MAX_AGE_DAYS}d): {os.path.basename(path)}")
                else:
                    survivors.append(path)
            except OSError as e:
                logger.warning(f"[retention] Could not check/remove {path}: {e}")
                survivors.append(path)  # keep in survivors to avoid accidental over-deletion

        # Pass 2: count-based deletion (oldest first)
        if len(survivors) > MAX_CLIPS:
            sorted_by_age = sorted(survivors, key=lambda p: os.path.getmtime(p))
            to_remove = sorted_by_age[: len(sorted_by_age) - MAX_CLIPS]
            for path in to_remove:
                try:
                    os.remove(path)
                    logger.info(f"[retention] Removed excess clip (>{MAX_CLIPS} total): {os.path.basename(path)}")
                except OSError as e:
                    logger.warning(f"[retention] Could not remove {path}: {e}")

        remaining = len([f for f in os.listdir(output_dir) if f.lower().endswith(".mp4")])
        logger.info(f"[retention] {remaining} clip(s) kept in '{output_dir}/'")

    except Exception as e:
        logger.warning(f"[retention] Cleanup failed (non-fatal): {e}")


def _banner(msg: str) -> None:
    logger.info("=" * 60)
    logger.info(msg)
    logger.info("=" * 60)
