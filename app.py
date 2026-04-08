import os
import uuid
import threading
from flask import Flask, jsonify, request, render_template, send_from_directory
from dotenv import load_dotenv
from utils import validate_youtube_url, logger
from processor import run_pipeline
from config_manager import load_config, save_config, is_configured, mask_api_key
from postiz_client import schedule_post
from publisher_db import init_db
from buffer_routes import buffer_bp

load_dotenv()

app = Flask(__name__)
app.register_blueprint(buffer_bp)

OUTPUT_DIR = "output"
os.makedirs(OUTPUT_DIR, exist_ok=True)

# Initialise publisher DB (creates tables if needed)
init_db()

jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()

STEPS = [
    ("downloading",  "Downloading video"),
    ("transcript",   "Extracting transcript"),
    ("segmenting",   "Building segments"),
    ("scoring",      "Analyzing with AI"),
    ("processing",   "Processing clips"),
    ("done",         "Done"),
]


def _set_status(job_id: str, step: str, detail: str = "") -> None:
    with jobs_lock:
        jobs[job_id]["step"] = step
        jobs[job_id]["detail"] = detail


def _run_job(job_id: str, url: str, top_n: int) -> None:
    try:
        def on_status(step: str, detail: str = "") -> None:
            _set_status(job_id, step, detail)

        clips = run_pipeline(url, output_dir=OUTPUT_DIR, top_n=top_n, on_status=on_status)
        filenames = [os.path.basename(c) for c in clips]
        with jobs_lock:
            jobs[job_id].update({"step": "done", "detail": f"{len(filenames)} clip(s) ready",
                                  "clips": filenames, "error": None})
    except Exception as exc:
        import traceback
        logger.error(f"Job {job_id} failed: {exc}")
        with jobs_lock:
            jobs[job_id].update({"step": "error", "detail": str(exc),
                                  "error": str(exc), "traceback": traceback.format_exc()})


# ── Main pages ────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


# ── Clip generation ───────────────────────────────────────────────────────────

@app.route("/process", methods=["POST"])
def process():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    top_n = int(data.get("top", 5))

    if not url:
        return jsonify({"error": "No URL provided"}), 400
    if not validate_youtube_url(url):
        return jsonify({"error": "Invalid YouTube URL"}), 400
    top_n = max(1, min(10, top_n))

    job_id = str(uuid.uuid4())
    with jobs_lock:
        jobs[job_id] = {"step": "queued", "detail": "Job queued", "clips": [], "error": None}

    threading.Thread(target=_run_job, args=(job_id, url, top_n), daemon=True).start()
    return jsonify({"job_id": job_id}), 202


@app.route("/status/<job_id>")
def status(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    step_labels = {s: lbl for s, lbl in STEPS}
    step_order  = [s for s, _ in STEPS]
    current     = job["step"]
    progress    = 0
    if current in step_order:
        progress = int(step_order.index(current) / (len(step_order) - 1) * 100)

    return jsonify({
        "step":       current,
        "step_label": step_labels.get(current, current.capitalize()),
        "detail":     job.get("detail", ""),
        "progress":   progress,
        "clips":      job.get("clips", []),
        "error":      job.get("error"),
        "done":       current == "done",
    })


@app.route("/output/<filename>")
def serve_clip(filename: str):
    return send_from_directory(OUTPUT_DIR, filename, as_attachment=False)


@app.route("/download/<filename>")
def download_clip(filename: str):
    return send_from_directory(OUTPUT_DIR, filename, as_attachment=True)


# ── Postiz scheduling ─────────────────────────────────────────────────────────

@app.route("/schedule", methods=["POST"])
def schedule():
    data = request.get_json(silent=True) or {}
    video_path    = (data.get("video_path") or "").strip()
    caption       = (data.get("caption") or "").strip()
    scheduled_time = (data.get("scheduled_time") or "").strip()
    platform      = data.get("platform") or None

    if not video_path:
        return jsonify({"error": "video_path is required"}), 400
    if not caption:
        return jsonify({"error": "caption is required"}), 400
    if not scheduled_time:
        return jsonify({"error": "scheduled_time is required"}), 400

    full_path = os.path.join(OUTPUT_DIR, os.path.basename(video_path))
    if not os.path.exists(full_path):
        return jsonify({"error": f"File not found: {os.path.basename(video_path)}"}), 404

    result = schedule_post(full_path, caption, scheduled_time, platform)
    if result.get("success"):
        return jsonify(result), 200
    return jsonify(result), 502


# ── Config (Postiz credentials) ───────────────────────────────────────────────

@app.route("/config", methods=["GET"])
def get_config():
    cfg = load_config()
    return jsonify({
        "postiz_api_key":  mask_api_key(cfg.get("postiz_api_key", "")),
        "postiz_base_url": cfg.get("postiz_base_url", ""),
        "configured":      is_configured(),
    })


@app.route("/config", methods=["POST"])
def set_config():
    data = request.get_json(silent=True) or {}
    api_key  = data.get("postiz_api_key", "").strip()
    base_url = data.get("postiz_base_url", "").strip()

    if not api_key and not base_url:
        return jsonify({"error": "Provide at least one field to update"}), 400
    if base_url and not (base_url.startswith("http://") or base_url.startswith("https://")):
        return jsonify({"error": "Base URL must start with http:// or https://"}), 400

    cfg = load_config()
    if api_key:
        cfg["postiz_api_key"] = api_key
    if base_url:
        cfg["postiz_base_url"] = base_url.rstrip("/")
    save_config(cfg)

    return jsonify({"success": True, "configured": is_configured()}), 200


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    logger.info(f"ClipGen UI starting on http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
