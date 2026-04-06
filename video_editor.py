import os
import subprocess
import cv2
import numpy as np
from utils import logger, format_time, ensure_output_dir

FACE_CASCADE_PATH = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"


def download_video(url: str, output_path: str = "downloads") -> str:
    os.makedirs(output_path, exist_ok=True)
    out_file = os.path.join(output_path, "source.%(ext)s")
    logger.info(f"Downloading video from: {url}")
    cmd = [
        "yt-dlp",
        "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "--merge-output-format", "mp4",
        "-o", out_file,
        "--no-playlist",
        url,
    ]
    _run_command(cmd, "yt-dlp download")

    for fname in os.listdir(output_path):
        if fname.startswith("source") and fname.endswith(".mp4"):
            full = os.path.join(output_path, fname)
            logger.info(f"Downloaded: {full}")
            return full

    raise FileNotFoundError("Downloaded video not found in downloads/")


def cut_clip(source: str, start: float, end: float, temp_path: str) -> str:
    duration = end - start
    cmd = [
        "ffmpeg", "-y",
        "-ss", str(start),
        "-i", source,
        "-t", str(duration),
        "-c", "copy",
        "-avoid_negative_ts", "make_zero",
        temp_path,
    ]
    _run_command(cmd, f"cut clip {start:.1f}s-{end:.1f}s")
    return temp_path


def process_to_vertical(source_clip: str, output_path: str) -> str:
    logger.info(f"Processing to vertical: {source_clip} -> {output_path}")

    cap = cv2.VideoCapture(source_clip)
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open video: {source_clip}")

    orig_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    orig_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    logger.info(f"Source dimensions: {orig_w}x{orig_h}, fps={fps:.2f}, frames={total_frames}")

    target_w, target_h = 1080, 1920
    target_ratio = target_w / target_h

    crop_w = min(orig_w, int(orig_h * target_ratio))
    crop_h = min(orig_h, int(orig_w / target_ratio))

    face_cascade = cv2.CascadeClassifier(FACE_CASCADE_PATH)

    sample_frames = _sample_frames(cap, total_frames, n=20)
    cap.release()

    face_x_positions = []
    for frame in sample_frames:
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces = face_cascade.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=4, minSize=(30, 30))
        if len(faces) > 0:
            largest = max(faces, key=lambda f: f[2] * f[3])
            fx, fy, fw, fh = largest
            face_cx = fx + fw // 2
            face_x_positions.append(face_cx)

    if face_x_positions:
        median_face_x = int(np.median(face_x_positions))
        logger.info(f"Face detected. Median face center X: {median_face_x}")
        crop_x = _clamp(median_face_x - crop_w // 2, 0, orig_w - crop_w)
    else:
        logger.info("No face detected. Using center crop.")
        crop_x = (orig_w - crop_w) // 2

    crop_y = (orig_h - crop_h) // 2
    crop_x = _clamp(crop_x, 0, orig_w - crop_w)
    crop_y = _clamp(crop_y, 0, orig_h - crop_h)

    logger.info(f"Crop box: x={crop_x}, y={crop_y}, w={crop_w}, h={crop_h}")

    vf = (
        f"crop={crop_w}:{crop_h}:{crop_x}:{crop_y},"
        f"scale={target_w}:{target_h}:flags=lanczos"
    )

    cmd = [
        "ffmpeg", "-y",
        "-i", source_clip,
        "-vf", vf,
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "23",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        output_path,
    ]
    _run_command(cmd, f"encode vertical clip -> {output_path}")
    logger.info(f"Vertical clip saved: {output_path}")
    return output_path


def _sample_frames(cap: cv2.VideoCapture, total_frames: int, n: int = 20) -> list:
    frames = []
    if total_frames <= 0:
        return frames
    step = max(1, total_frames // n)
    for i in range(0, total_frames, step):
        cap.set(cv2.CAP_PROP_POS_FRAMES, i)
        ret, frame = cap.read()
        if ret:
            frames.append(frame)
        if len(frames) >= n:
            break
    return frames


def _clamp(val: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, val))


def _run_command(cmd: list[str], label: str) -> None:
    logger.info(f"Running [{label}]: {' '.join(cmd)}")
    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode != 0:
        logger.error(f"[{label}] stderr:\n{result.stderr[-2000:]}")
        raise RuntimeError(f"Command failed [{label}]: exit code {result.returncode}")
    if result.stderr:
        logger.debug(f"[{label}] stderr (ok):\n{result.stderr[-500:]}")
