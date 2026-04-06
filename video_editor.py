import os
import subprocess
import cv2
import numpy as np
from utils import logger

FACE_CASCADE_PATH = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
TARGET_W, TARGET_H = 1080, 1920
TARGET_RATIO = TARGET_W / TARGET_H
SMOOTH_ALPHA = 0.15


def cut_clip(source: str, start: float, end: float, temp_path: str) -> str:
    """Precisely cut a clip using stream-copy to avoid drift."""
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
    _run_command(cmd, f"cut {start:.1f}s-{end:.1f}s")
    return temp_path


def process_to_vertical(source_clip: str, output_path: str) -> str:
    """Detect faces, build smooth per-frame crop trajectory, encode to 1080x1920."""
    logger.info(f"Processing to vertical: {os.path.basename(source_clip)} -> {os.path.basename(output_path)}")

    cap = cv2.VideoCapture(source_clip)
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open video: {source_clip}")

    orig_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    orig_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    logger.info(f"Source: {orig_w}x{orig_h} @ {fps:.2f}fps, {total_frames} frames")

    crop_w = min(orig_w, int(orig_h * TARGET_RATIO))
    crop_h = min(orig_h, int(orig_w / TARGET_RATIO))

    face_cascade = cv2.CascadeClassifier(FACE_CASCADE_PATH)

    raw_cx_by_frame = _detect_face_positions(cap, total_frames, face_cascade, orig_w, crop_w)
    cap.release()

    smoothed_cx = _smooth_positions(raw_cx_by_frame, total_frames, orig_w, crop_w)

    face_detected = any(v is not None for v in raw_cx_by_frame.values())
    if face_detected:
        logger.info(f"Face tracking active across {len(raw_cx_by_frame)} sampled frames")
    else:
        logger.info("No faces detected — using center crop throughout")

    crop_y = _clamp((orig_h - crop_h) // 2, 0, orig_h - crop_h)

    vf = _build_vf_with_tracking(smoothed_cx, crop_w, crop_h, crop_y, orig_w, fps, total_frames)

    cmd = [
        "ffmpeg", "-y",
        "-i", source_clip,
        "-vf", vf,
        "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "23",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        output_path,
    ]
    _run_command(cmd, f"encode vertical -> {os.path.basename(output_path)}")
    logger.info(f"Saved: {output_path}")
    return output_path


def _detect_face_positions(
    cap: cv2.VideoCapture,
    total_frames: int,
    face_cascade: cv2.CascadeClassifier,
    orig_w: int,
    crop_w: int,
    sample_n: int = 40,
) -> dict[int, int | None]:
    """Sample N frames and detect face center X. Returns {frame_index: cx or None}."""
    results: dict[int, int | None] = {}
    if total_frames <= 0:
        return results

    step = max(1, total_frames // sample_n)
    indices = list(range(0, total_frames, step))[:sample_n]

    for idx in indices:
        cap.set(cv2.CAP_PROP_POS_FRAMES, idx)
        ret, frame = cap.read()
        if not ret:
            results[idx] = None
            continue

        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        faces = face_cascade.detectMultiScale(
            gray, scaleFactor=1.1, minNeighbors=4, minSize=(30, 30)
        )
        if len(faces) > 0:
            largest = max(faces, key=lambda f: f[2] * f[3])
            fx, fy, fw, fh = largest
            cx = _clamp(fx + fw // 2, crop_w // 2, orig_w - crop_w // 2)
            results[idx] = cx
        else:
            results[idx] = None

    return results


def _smooth_positions(
    raw: dict[int, int | None],
    total_frames: int,
    orig_w: int,
    crop_w: int,
) -> list[int]:
    """Interpolate and exponentially smooth face center X across all frames."""
    default_cx = orig_w // 2

    sampled_indices = sorted(raw.keys())
    known: list[tuple[int, int]] = [
        (idx, val) for idx, val in raw.items() if val is not None
    ]

    if not known:
        return [_clamp(default_cx - crop_w // 2, 0, orig_w - crop_w)] * total_frames

    interp_cx = [default_cx] * total_frames

    if len(known) == 1:
        for i in range(total_frames):
            interp_cx[i] = known[0][1]
    else:
        for i in range(len(known) - 1):
            start_idx, start_val = known[i]
            end_idx, end_val = known[i + 1]
            for f in range(start_idx, end_idx + 1):
                t = (f - start_idx) / max(1, end_idx - start_idx)
                interp_cx[f] = int(start_val + t * (end_val - start_val))
        for f in range(0, known[0][0]):
            interp_cx[f] = known[0][1]
        for f in range(known[-1][0], total_frames):
            interp_cx[f] = known[-1][1]

    smoothed = [0] * total_frames
    smoothed[0] = interp_cx[0]
    for i in range(1, total_frames):
        smoothed[i] = int(SMOOTH_ALPHA * interp_cx[i] + (1 - SMOOTH_ALPHA) * smoothed[i - 1])

    crop_x_list = [_clamp(cx - crop_w // 2, 0, orig_w - crop_w) for cx in smoothed]
    return crop_x_list


def _build_vf_with_tracking(
    crop_x_per_frame: list[int],
    crop_w: int,
    crop_h: int,
    crop_y: int,
    orig_w: int,
    fps: float,
    total_frames: int,
) -> str:
    """Build FFmpeg video filter string using median smoothed crop X position."""
    if not crop_x_per_frame:
        cx = max(0, (orig_w - crop_w) // 2)
    else:
        cx = int(np.median(crop_x_per_frame))

    logger.info(f"Final crop X: {cx}  (median of {len(crop_x_per_frame)} smoothed frames)")
    return (
        f"crop={crop_w}:{crop_h}:{cx}:{crop_y},"
        f"scale={TARGET_W}:{TARGET_H}:flags=lanczos"
    )


def _clamp(val: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, val))


def _run_command(cmd: list[str], label: str) -> None:
    logger.info(f"[{label}] cmd: {' '.join(cmd)}")
    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode != 0:
        logger.error(f"[{label}] FAILED:\n{result.stderr[-2000:]}")
        raise RuntimeError(f"FFmpeg command failed [{label}]: exit {result.returncode}")
    logger.debug(f"[{label}] done")
