import os
import subprocess
from utils import logger


def download_video(url: str, output_dir: str = "downloads") -> str:
    """Download video using pytube first; fall back to yt-dlp on failure.
    Saves result as input.mp4 inside output_dir.
    """
    os.makedirs(output_dir, exist_ok=True)
    dest = os.path.join(output_dir, "input.mp4")

    # Only use cached file if it's a valid, non-empty MP4
    if os.path.exists(dest) and os.path.getsize(dest) > 100_000:
        logger.info(f"Using cached download: {dest} ({os.path.getsize(dest)//1024} KB)")
        return dest
    elif os.path.exists(dest):
        logger.warning(f"Cached file too small ({os.path.getsize(dest)} bytes) – re-downloading")
        os.remove(dest)

    logger.info("STEP 1a: Attempting download via pytube...")
    try:
        result = _download_pytube(url, dest)
        logger.info(f"pytube download success: {result}")
        return result
    except Exception as e:
        logger.warning(f"pytube failed: {e}. Falling back to yt-dlp...")

    logger.info("STEP 1b: Downloading via yt-dlp (fallback)...")
    return _download_ytdlp(url, dest)


def _download_pytube(url: str, dest: str) -> str:
    from pytube import YouTube

    yt = YouTube(url)
    logger.info(f"pytube: fetching streams for '{yt.title}'")

    stream = (
        yt.streams
        .filter(progressive=True, file_extension="mp4")
        .order_by("resolution")
        .last()
    )

    if stream is None:
        stream = (
            yt.streams
            .filter(file_extension="mp4")
            .order_by("resolution")
            .last()
        )

    if stream is None:
        raise RuntimeError("No suitable pytube stream found")

    logger.info(f"pytube: selected stream {stream.resolution} ({stream.mime_type})")

    raw_path = stream.download(output_path=os.path.dirname(dest))

    if raw_path != dest:
        os.replace(raw_path, dest)

    if not os.path.exists(dest) or os.path.getsize(dest) == 0:
        raise RuntimeError("pytube download produced an empty file")

    return dest


def _download_ytdlp(url: str, dest: str) -> str:
    tmp_template = dest.replace(".mp4", ".%(ext)s")
    cmd = [
        "yt-dlp",
        "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "--merge-output-format", "mp4",
        "-o", tmp_template,
        "--no-playlist",
        "--retries", "3",
        url,
    ]
    logger.info(f"yt-dlp cmd: {' '.join(cmd)}")
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    if result.returncode != 0:
        logger.error(f"yt-dlp stderr:\n{result.stderr[-2000:]}")
        raise RuntimeError(f"yt-dlp failed with exit code {result.returncode}")

    expected = dest
    if not os.path.exists(expected):
        for fname in os.listdir(os.path.dirname(dest)):
            if fname.startswith("input") and fname.endswith(".mp4"):
                found = os.path.join(os.path.dirname(dest), fname)
                os.replace(found, expected)
                break

    if not os.path.exists(expected) or os.path.getsize(expected) == 0:
        raise RuntimeError("yt-dlp download produced no output file")

    logger.info(f"yt-dlp download success: {expected}")
    return expected
