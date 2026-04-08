import os
import subprocess
import tempfile
from collections import deque
import cv2
import numpy as np
from utils import logger

FACE_CASCADE_PATH = cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
TARGET_W, TARGET_H = 1080, 1920
TARGET_RATIO = TARGET_W / TARGET_H   # 9:16 ≈ 0.5625
MIN_OUTPUT_BYTES = 10_000

# ── Single-face tracking constants ────────────────────────────────────────────
SAMPLE_STEP = 5       # detect face every N frames  (~6 detections/s at 30 fps)
N_SMOOTH = 8          # moving-average window length
N_MISS_FALLBACK = 25  # hold last position for this many consecutive misses
#                       then drift gradually to center
SNAP_LIMIT = 80       # max crop-x change allowed per 0.5 s keyframe (pixels)
CMD_STEP_SEC = 0.5    # interval between sendcmd keyframes

# ── Speaker-detection constants ────────────────────────────────────────────────
SPEAKER_SMOOTH_SEC = 0.5     # blend duration (sec) when switching speaker focus
LIP_TOP_FRAC = 0.55          # mouth region top    (fraction from face-bbox top)
LIP_BOT_FRAC = 0.92          # mouth region bottom (fraction from face-bbox top)
AUDIO_SR = 8000              # sample rate for lightweight audio extraction
LIP_HISTORY_N = 3            # rolling window for lip-activity score averaging
MIN_AUDIO_ENERGY = 0.015     # normalised RMS below this → treat frame as silence
FACE_MATCH_MAX_DIST_FRAC = 0.35  # max center distance (fraction of orig_w)
                                  # beyond which a face is treated as a new identity


# ── Public API ────────────────────────────────────────────────────────────────

def cut_clip(source: str, start: float, end: float, temp_path: str) -> str:
    """Precisely cut a clip from source using fast input-seek + stream-copy."""
    duration = max(0.5, end - start)
    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{start:.3f}",
        "-i", source,
        "-t", f"{duration:.3f}",
        "-c", "copy",
        "-avoid_negative_ts", "make_zero",
        "-map_metadata", "-1",
        temp_path,
    ]
    _run(cmd, f"cut {start:.1f}s-{end:.1f}s")
    _validate_output(temp_path, "cut_clip")
    return temp_path


def process_to_vertical(source_clip: str, output_path: str) -> str:
    """Face-track and crop to 1080×1920, normalize audio, encode H.264/AAC.

    Tracking pipeline:
      1. Extract per-frame audio energy once from the source clip.
      2. Dense Haar face detection every SAMPLE_STEP frames.
      3a. Single face  → moving-average smoothing (unchanged behaviour).
      3b. Multiple faces → speaker-focused tracking:
            - lip-activity score (mouth-region frame diff) per face identity
            - weighted by per-frame audio energy
            - exponential pan toward speaker with SPEAKER_SMOOTH_SEC blend
            - fallback: largest face (silence), then centre (no faces)
      4. Anti-snap limiter: cap position jump to SNAP_LIMIT px per keyframe.
      5. FFmpeg sendcmd: dynamic crop x updated every CMD_STEP_SEC seconds.
    """
    logger.info(
        f"Vertical encode: {os.path.basename(source_clip)} → {os.path.basename(output_path)}"
    )

    cap = cv2.VideoCapture(source_clip)
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open video for processing: {source_clip}")

    orig_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    orig_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    if orig_w == 0 or orig_h == 0:
        cap.release()
        raise RuntimeError(f"Source clip has invalid dimensions (0×0): {source_clip}")

    logger.info(f"Source: {orig_w}×{orig_h} @ {fps:.2f}fps, {total_frames} frames")

    crop_w, crop_h = _compute_crop_dims(orig_w, orig_h)
    crop_y = _clamp((orig_h - crop_h) // 2, 0, max(0, orig_h - crop_h))

    face_cascade = cv2.CascadeClassifier(FACE_CASCADE_PATH)
    if face_cascade.empty():
        logger.warning("Face cascade failed to load – center crop will be used")

    # Extract lightweight per-frame audio energy (used for speaker selection)
    audio_energy = _extract_audio_energy(source_clip, total_frames, fps)

    # Build smoothed per-timestamp crop positions
    positions = _track_speaker_focused(
        cap, total_frames, fps, face_cascade, orig_w, crop_w, audio_energy
    )
    cap.release()

    # Build FFmpeg filter string (sendcmd + crop + scale)
    vf, cmd_file = _build_dynamic_crop_vf(positions, crop_w, crop_h, crop_y)

    try:
        cmd = [
            "ffmpeg", "-y",
            "-i", source_clip,
            "-vf", vf,
            "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "23",
            "-profile:v", "high",
            "-level", "4.1",
            "-c:a", "aac",
            "-b:a", "128k",
            "-ar", "44100",
            "-movflags", "+faststart",
            "-pix_fmt", "yuv420p",
            output_path,
        ]
        _run(cmd, f"encode → {os.path.basename(output_path)}")
    finally:
        if cmd_file and os.path.exists(cmd_file):
            try:
                os.remove(cmd_file)
            except OSError:
                pass

    _validate_output(output_path, "process_to_vertical")
    logger.info(f"Saved: {output_path}")
    return output_path


# ── Crop geometry ─────────────────────────────────────────────────────────────

def _compute_crop_dims(orig_w: int, orig_h: int) -> tuple[int, int]:
    """Return (crop_w, crop_h) for the largest 9:16 region fitting in orig_w×orig_h."""
    w_from_h = int(orig_h * TARGET_RATIO)
    if w_from_h <= orig_w:
        return w_from_h, orig_h
    h_from_w = int(orig_w / TARGET_RATIO)
    return orig_w, min(h_from_w, orig_h)


# ── Audio energy extraction ───────────────────────────────────────────────────

def _extract_audio_energy(source_clip: str, total_frames: int, fps: float) -> np.ndarray:
    """Return per-frame normalised RMS energy as float32 array of length total_frames.

    Uses FFmpeg to pipe mono 8 kHz PCM (no new dependencies). On any failure
    returns an all-zero array so the caller gracefully falls back to largest-face.
    """
    energy = np.zeros(total_frames, dtype=np.float32)
    if total_frames <= 0 or fps <= 0:
        return energy

    try:
        cmd = [
            "ffmpeg", "-i", source_clip,
            "-vn", "-ac", "1", "-ar", str(AUDIO_SR),
            "-f", "f32le", "-",
        ]
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        raw, _ = proc.communicate(timeout=120)
        if not raw:
            return energy

        audio = np.frombuffer(raw, dtype=np.float32)
        samples_per_frame = AUDIO_SR / fps

        for fi in range(total_frames):
            s0 = int(fi * samples_per_frame)
            s1 = int((fi + 1) * samples_per_frame)
            if s0 >= len(audio):
                break
            chunk = audio[s0:min(s1, len(audio))]
            if chunk.size:
                energy[fi] = float(np.sqrt(np.mean(chunk ** 2)))

        peak = float(energy.max())
        if peak > 0:
            energy /= peak

        logger.info(
            f"Audio energy: extracted {len(audio)} samples @ {AUDIO_SR} Hz, "
            f"peak normalised to 1.0"
        )
    except Exception as exc:
        logger.warning(f"Audio energy extraction failed – speaker detection disabled: {exc}")

    return energy


# ── Speaker-focused tracking ─────────────────────────────────────────────────

def _track_speaker_focused(
    cap: cv2.VideoCapture,
    total_frames: int,
    fps: float,
    face_cascade: cv2.CascadeClassifier,
    orig_w: int,
    crop_w: int,
    audio_energy: np.ndarray,
) -> dict[float, int]:
    """Return {timestamp_sec: crop_left_x} using speaker-focused tracking.

    Single-face frames use a moving-average (identical to the old behaviour).
    Multi-face frames use lip-activity × audio-energy scoring to select the
    speaker, with exponential smoothing over SPEAKER_SMOOTH_SEC when focus
    shifts from one face to another.
    """
    center_cx = orig_w // 2
    max_x = max(0, orig_w - crop_w)
    center_crop_x = _clamp(center_cx - crop_w // 2, 0, max_x)

    if total_frames <= 0 or fps <= 0:
        return {0.0: center_crop_x}

    # ── Single-face path state ─────────────────────────────────────────────
    ma_buf: deque[int] = deque(maxlen=N_SMOOTH)
    last_known_cx: int = center_cx
    consec_miss: int = 0

    # ── Multi-face / speaker path state ───────────────────────────────────
    dt_per_sample = SAMPLE_STEP / fps
    # Exponential blend alpha: at alpha=1 we'd snap instantly,
    # at alpha≈0 we'd never arrive. Target: 95 % there in SPEAKER_SMOOTH_SEC.
    blend_alpha = min(1.0, dt_per_sample / max(dt_per_sample, SPEAKER_SMOOTH_SEC))

    current_cx = float(center_cx)   # continuously smoothed position
    target_cx = float(center_cx)    # where we want to move to

    # Face-identity tracking across sampled frames
    prev_face_centers: list[tuple[int, int]] = []
    prev_track_ids: list[int] = []
    next_track_id: int = 0

    # Per-identity lip-activity history
    lip_history: dict[int, deque] = {}
    mouth_patches: dict[int, np.ndarray] = {}   # track_id → last mouth patch

    n_detected = 0
    n_sampled = 0
    positions: dict[float, int] = {}

    for frame_idx in range(0, total_frames, SAMPLE_STEP):
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
        ret, frame = cap.read()
        if not ret:
            continue

        timestamp = frame_idx / fps
        n_sampled += 1

        # ── Detect faces ────────────────────────────────────────────────
        faces: list[tuple[int, int, int, int]] = []
        gray: np.ndarray | None = None

        if not face_cascade.empty():
            try:
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                raw = face_cascade.detectMultiScale(
                    gray,
                    scaleFactor=1.1,
                    minNeighbors=3,
                    minSize=(25, 25),
                )
                if len(raw) > 0:
                    faces = [tuple(int(v) for v in f) for f in raw]
            except Exception as exc:
                logger.debug(f"Face detection error at frame {frame_idx}: {exc}")

        # ── No faces detected ───────────────────────────────────────────
        if not faces:
            consec_miss += 1
            if consec_miss <= N_MISS_FALLBACK:
                # Hold: keep target unchanged, let current track toward it
                pass
            else:
                # Drift target gradually toward center
                progress = min(1.0, (consec_miss - N_MISS_FALLBACK) / N_MISS_FALLBACK)
                target_cx = current_cx * (1.0 - progress) + center_cx * progress

            current_cx += (target_cx - current_cx) * blend_alpha
            positions[timestamp] = _clamp(int(round(current_cx)) - crop_w // 2, 0, max_x)
            continue

        # ── Faces detected ──────────────────────────────────────────────
        consec_miss = 0
        n_detected += 1

        # ── Single face → use original moving-average path ─────────────
        if len(faces) == 1:
            fx, fy, fw, fh = faces[0]
            raw_cx = _clamp(fx + fw // 2, crop_w // 2, orig_w - crop_w // 2)
            ma_buf.append(raw_cx)
            smoothed_cx = int(sum(ma_buf) / len(ma_buf))
            last_known_cx = smoothed_cx
            target_cx = float(smoothed_cx)
            current_cx += (target_cx - current_cx) * blend_alpha

            # Keep identity state in sync for the single face
            cx_i, cy_i = fx + fw // 2, fy + fh // 2
            prev_face_centers = [(cx_i, cy_i)]
            if prev_track_ids:
                tid = prev_track_ids[0]
            else:
                tid = next_track_id
                next_track_id += 1
            prev_track_ids = [tid]
            if gray is not None:
                patch = _get_mouth_patch((fx, fy, fw, fh), gray)
                if patch is not None:
                    mouth_patches[tid] = patch

        # ── Multiple faces → speaker selection ─────────────────────────
        else:
            curr_centers = [(fx + fw // 2, fy + fh // 2) for fx, fy, fw, fh in faces]

            curr_track_ids, next_track_id = _match_faces(
                prev_face_centers, prev_track_ids,
                curr_centers, orig_w, next_track_id,
            )

            a_energy = float(audio_energy[min(frame_idx, len(audio_energy) - 1)])
            is_silent = a_energy < MIN_AUDIO_ENERGY

            best_score = -1.0
            speaker_cx: int | None = None
            largest_cx: int | None = None
            largest_area = 0

            for face, tid, cc in zip(faces, curr_track_ids, curr_centers):
                fx, fy, fw, fh = face
                face_cx = _clamp(fx + fw // 2, crop_w // 2, orig_w - crop_w // 2)
                area = fw * fh

                # Track largest face (fallback)
                if area > largest_area:
                    largest_area = area
                    largest_cx = face_cx

                # Lip-activity score
                lip_score = 0.0
                if gray is not None:
                    patch = _get_mouth_patch((fx, fy, fw, fh), gray)
                    if patch is not None and tid in mouth_patches:
                        prev_patch = mouth_patches[tid]
                        if prev_patch.shape == patch.shape and patch.size > 0:
                            lip_score = float(
                                np.mean(
                                    np.abs(
                                        patch.astype(np.float32)
                                        - prev_patch.astype(np.float32)
                                    )
                                )
                            ) / 255.0
                        mouth_patches[tid] = patch
                    elif patch is not None:
                        mouth_patches[tid] = patch

                # Rolling average of lip activity
                if tid not in lip_history:
                    lip_history[tid] = deque(maxlen=LIP_HISTORY_N)
                lip_history[tid].append(lip_score)
                avg_lip = float(np.mean(lip_history[tid]))

                # Combined score: lip activity weighted by audio presence.
                # Area term gives a small tiebreaker toward closer/larger faces.
                area_norm = area / max(1, orig_w * orig_w // 4)
                score = avg_lip * (0.7 + 0.3 * a_energy) + area_norm * 0.05

                if score > best_score:
                    best_score = score
                    speaker_cx = face_cx

            # Fallback priority: speaking face → largest face → centre
            if is_silent or speaker_cx is None:
                new_target = float(largest_cx if largest_cx is not None else center_cx)
            else:
                new_target = float(speaker_cx)

            target_cx = new_target

            # Also keep the MA buffer fed from current target (for continuity
            # if we later transition back to single-face path)
            ma_buf.append(int(round(target_cx)) + crop_w // 2)
            last_known_cx = int(round(target_cx)) + crop_w // 2

            current_cx += (target_cx - current_cx) * blend_alpha

            # Update identity state
            prev_face_centers = curr_centers
            prev_track_ids = curr_track_ids

        positions[timestamp] = _clamp(int(round(current_cx)) - crop_w // 2, 0, max_x)

    if n_sampled == 0:
        return {0.0: center_crop_x}

    pct = 100 * n_detected // n_sampled if n_sampled else 0
    logger.info(
        f"Face tracking: detected in {n_detected}/{n_sampled} samples ({pct}%)"
    )
    return positions


# ── Face identity helpers ─────────────────────────────────────────────────────

def _get_mouth_patch(
    face_bbox: tuple[int, int, int, int],
    gray: np.ndarray,
) -> np.ndarray | None:
    """Extract and resize the mouth region from a face bounding box.

    Returns a 32×16 uint8 patch suitable for frame-diff comparison,
    or None if the region is degenerate.
    """
    fx, fy, fw, fh = face_bbox
    y1 = int(fy + fh * LIP_TOP_FRAC)
    y2 = int(fy + fh * LIP_BOT_FRAC)
    x1, x2 = fx, fx + fw
    frame_h, frame_w = gray.shape[:2]
    y1, y2 = max(0, y1), min(frame_h, y2)
    x1, x2 = max(0, x1), min(frame_w, x2)
    if y2 <= y1 or x2 <= x1:
        return None
    patch = gray[y1:y2, x1:x2]
    if patch.size == 0:
        return None
    return cv2.resize(patch, (32, 16), interpolation=cv2.INTER_AREA)


def _match_faces(
    prev_centers: list[tuple[int, int]],
    prev_ids: list[int],
    curr_centers: list[tuple[int, int]],
    orig_w: int,
    next_id: int,
) -> tuple[list[int], int]:
    """Greedy nearest-neighbour matching of current faces to previous identities.

    Returns (curr_track_ids, next_id) where unmatched faces receive a fresh ID.
    """
    max_dist = orig_w * FACE_MATCH_MAX_DIST_FRAC
    curr_ids = [-1] * len(curr_centers)

    if prev_centers:
        # Build all (distance, curr_idx, prev_idx) pairs and sort ascending
        pairs: list[tuple[float, int, int]] = []
        for ci, cc in enumerate(curr_centers):
            for pi, pc in enumerate(prev_centers):
                d = ((cc[0] - pc[0]) ** 2 + (cc[1] - pc[1]) ** 2) ** 0.5
                pairs.append((d, ci, pi))
        pairs.sort()

        used_prev: set[int] = set()
        for dist, ci, pi in pairs:
            if dist > max_dist:
                break
            if curr_ids[ci] == -1 and pi not in used_prev:
                curr_ids[ci] = prev_ids[pi]
                used_prev.add(pi)

    # Assign fresh IDs to any unmatched current face
    for ci in range(len(curr_centers)):
        if curr_ids[ci] == -1:
            curr_ids[ci] = next_id
            next_id += 1

    return curr_ids, next_id


# ── Dynamic crop filter string ────────────────────────────────────────────────

def _build_dynamic_crop_vf(
    positions: dict[float, int],
    crop_w: int,
    crop_h: int,
    crop_y: int,
) -> tuple[str, str | None]:
    """Build an FFmpeg filtergraph string that moves the crop window dynamically.

    Uses FFmpeg's `sendcmd` filter to update crop x at CMD_STEP_SEC intervals.
    Returns (vf_string, temp_sendcmd_file_or_None).
    Caller is responsible for deleting the temp file.

    If the sendcmd file cannot be written, falls back to a static median crop.
    """
    if not positions:
        vf = (
            f"crop={crop_w}:{crop_h}:0:{crop_y},"
            f"scale={TARGET_W}:{TARGET_H}:flags=lanczos"
        )
        return vf, None

    sorted_ts = sorted(positions.keys())
    total_dur = sorted_ts[-1]

    # Build keypoints at CMD_STEP_SEC intervals
    raw_keypoints: list[tuple[float, int]] = []
    t = 0.0
    while t <= total_dur + CMD_STEP_SEC:
        x = _interp(positions, sorted_ts, t)
        raw_keypoints.append((t, x))
        t += CMD_STEP_SEC

    # Anti-snap: limit how far the crop can jump per keyframe
    smooth_kp: list[tuple[float, int]] = [raw_keypoints[0]]
    for ts, x in raw_keypoints[1:]:
        prev_x = smooth_kp[-1][1]
        delta = x - prev_x
        if abs(delta) > SNAP_LIMIT:
            x = prev_x + int(SNAP_LIMIT * (1 if delta > 0 else -1))
        smooth_kp.append((ts, x))

    initial_x = smooth_kp[0][1]
    xs = [x for _, x in smooth_kp]
    logger.info(
        f"Dynamic crop: {len(smooth_kp)} keyframes over {total_dur:.1f}s, "
        f"x ∈ [{min(xs)}, {max(xs)}]  (crop_w={crop_w}, crop_y={crop_y})"
    )

    # Write sendcmd temp file
    cmd_file: str | None = None
    try:
        fd, cmd_file = tempfile.mkstemp(suffix=".txt", prefix="clipgen_crop_")
        with os.fdopen(fd, "w") as f:
            f.write(
                ";".join(f"{ts:.3f} crop x {x}" for ts, x in smooth_kp) + ";"
            )
    except Exception as e:
        logger.warning(f"Could not write sendcmd file – falling back to static crop: {e}")
        if cmd_file and os.path.exists(cmd_file):
            try:
                os.remove(cmd_file)
            except OSError:
                pass
        cmd_file = None

    if cmd_file:
        vf = (
            f"sendcmd=f={cmd_file},"
            f"crop={crop_w}:{crop_h}:{initial_x}:{crop_y},"
            f"scale={TARGET_W}:{TARGET_H}:flags=lanczos"
        )
    else:
        median_x = int(np.median(xs))
        logger.info(f"Static fallback: crop x={median_x}")
        vf = (
            f"crop={crop_w}:{crop_h}:{median_x}:{crop_y},"
            f"scale={TARGET_W}:{TARGET_H}:flags=lanczos"
        )

    return vf, cmd_file


def _interp(positions: dict[float, int], sorted_ts: list[float], t: float) -> int:
    """Linear interpolation of crop-x at time t from sorted sample points."""
    if not sorted_ts:
        return 0
    if t <= sorted_ts[0]:
        return positions[sorted_ts[0]]
    if t >= sorted_ts[-1]:
        return positions[sorted_ts[-1]]

    lo, hi = 0, len(sorted_ts) - 1
    while lo < hi - 1:
        mid = (lo + hi) // 2
        if sorted_ts[mid] <= t:
            lo = mid
        else:
            hi = mid

    t0, t1 = sorted_ts[lo], sorted_ts[hi]
    x0, x1 = positions[t0], positions[t1]
    frac = (t - t0) / (t1 - t0) if t1 > t0 else 0.0
    return int(x0 + frac * (x1 - x0))


# ── Utilities ─────────────────────────────────────────────────────────────────

def _clamp(val: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, val))


def _validate_output(path: str, label: str) -> None:
    if not os.path.exists(path):
        raise RuntimeError(f"[{label}] Output file was not created: {path}")
    size = os.path.getsize(path)
    if size < MIN_OUTPUT_BYTES:
        raise RuntimeError(
            f"[{label}] Output file too small ({size} bytes) – likely corrupt: {path}"
        )
    logger.info(f"[{label}] OK: {os.path.basename(path)} ({size/1024:.1f} KB)")


def _run(cmd: list[str], label: str) -> None:
    logger.info(f"[ffmpeg:{label}] " + " ".join(cmd))
    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if result.returncode != 0:
        logger.error(
            f"[ffmpeg:{label}] FAILED (exit {result.returncode}):\n"
            f"{result.stderr[-3000:] if result.stderr else '(no stderr)'}"
        )
        raise RuntimeError(
            f"FFmpeg [{label}] exited {result.returncode}. "
            f"Last error: {result.stderr[-200:].strip()}"
        )
    logger.debug(f"[ffmpeg:{label}] done")
