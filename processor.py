import os
import shutil
from typing import Callable
from utils import logger, ensure_output_dir
from downloader import download_video
from transcript import fetch_transcript, build_segments
from ai_selector import score_segments
from video_editor import cut_clip, process_to_vertical


def run_pipeline(
    url: str,
    output_dir: str = "output",
    top_n: int = 5,
    on_status: Callable[[str, str], None] | None = None,
) -> list[str]:
    def update(step: str, detail: str = "") -> None:
        logger.info(f"[pipeline] {step}: {detail}")
        if on_status:
            on_status(step, detail)

    ensure_output_dir(output_dir)
    downloads_dir = "downloads"
    temps_dir = "temp_clips"
    os.makedirs(temps_dir, exist_ok=True)

    logger.info("=" * 60)
    logger.info("STEP 1: Download video")
    logger.info("=" * 60)
    update("downloading", "Starting video download…")
    source_video = download_video(url, downloads_dir)
    update("downloading", f"Downloaded: {os.path.basename(source_video)}")

    logger.info("=" * 60)
    logger.info("STEP 2: Fetch transcript")
    logger.info("=" * 60)
    update("transcript", "Fetching YouTube transcript…")
    raw_transcript = fetch_transcript(url)
    update("transcript", f"Got {len(raw_transcript)} transcript entries")

    logger.info("=" * 60)
    logger.info("STEP 3: Build segments")
    logger.info("=" * 60)
    update("segmenting", "Building coherent segments…")
    segments = build_segments(raw_transcript)
    if not segments:
        raise RuntimeError("No segments extracted from transcript")
    update("segmenting", f"Built {len(segments)} segments")

    logger.info("=" * 60)
    logger.info("STEP 4: AI segment scoring")
    logger.info("=" * 60)
    update("scoring", f"Scoring {len(segments)} segments with AI…")
    top_segments = score_segments(segments, top_n=top_n)
    if not top_segments:
        raise RuntimeError("No segments returned by AI scorer")
    update("scoring", f"Selected top {len(top_segments)} segments")

    top_segments.sort(key=lambda s: s["start"])

    logger.info("=" * 60)
    logger.info("STEP 5: Cut and process clips")
    logger.info("=" * 60)
    output_files = []
    for i, seg in enumerate(top_segments, start=1):
        start = seg["start"]
        end = seg["end"]
        clip_label = f"clip_{i:02d}"

        update("processing", f"Processing {clip_label} ({i}/{len(top_segments)})…")

        temp_path = os.path.join(temps_dir, f"{clip_label}_raw.mp4")
        final_path = os.path.join(output_dir, f"{clip_label}.mp4")

        logger.info(f"Processing {clip_label}: {start:.1f}s – {end:.1f}s")
        cut_clip(source_video, start, end, temp_path)
        process_to_vertical(temp_path, final_path)
        output_files.append(final_path)
        logger.info(f"Saved: {final_path}")

    logger.info("=" * 60)
    logger.info("Cleaning up temp files…")
    shutil.rmtree(temps_dir, ignore_errors=True)

    logger.info("=" * 60)
    logger.info(f"DONE! Generated {len(output_files)} clips in '{output_dir}/':")
    for f in output_files:
        size_mb = os.path.getsize(f) / (1024 * 1024)
        logger.info(f"  {f} ({size_mb:.1f} MB)")

    return output_files
