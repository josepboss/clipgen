import os
import uuid
import threading
from flask import Flask, jsonify, request, render_template, send_from_directory
from dotenv import load_dotenv
from utils import validate_youtube_url, logger
from processor import run_pipeline

load_dotenv()

app = Flask(__name__)

OUTPUT_DIR = "output"
os.makedirs(OUTPUT_DIR, exist_ok=True)

jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()

STEPS = [
    ("downloading",   "Downloading video"),
    ("transcript",    "Extracting transcript"),
    ("segmenting",    "Building segments"),
    ("scoring",       "Analyzing with AI"),
    ("processing",    "Processing clips"),
    ("done",          "Done"),
]


def _set_status(job_id: str, step: str, detail: str = "") -> None:
    with jobs_lock:
        jobs[job_id]["step"] = step
        jobs[job_id]["detail"] = detail
        logger.info(f"[job {job_id[:8]}] {step}: {detail}")


def _run_job(job_id: str, url: str, top_n: int) -> None:
    try:
        def on_status(step: str, detail: str = "") -> None:
            _set_status(job_id, step, detail)

        clips = run_pipeline(
            url,
            output_dir=OUTPUT_DIR,
            top_n=top_n,
            on_status=on_status,
        )

        filenames = [os.path.basename(c) for c in clips]
        with jobs_lock:
            jobs[job_id]["step"] = "done"
            jobs[job_id]["detail"] = f"{len(filenames)} clip(s) ready"
            jobs[job_id]["clips"] = filenames
            jobs[job_id]["error"] = None

    except Exception as exc:
        logger.error(f"Job {job_id} failed: {exc}")
        import traceback
        tb = traceback.format_exc()
        with jobs_lock:
            jobs[job_id]["step"] = "error"
            jobs[job_id]["detail"] = str(exc)
            jobs[job_id]["error"] = str(exc)
            jobs[job_id]["traceback"] = tb


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/process", methods=["POST"])
def process():
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    top_n = int(data.get("top", 5))

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    if not validate_youtube_url(url):
        return jsonify({"error": "Invalid YouTube URL"}), 400

    if top_n < 1 or top_n > 10:
        top_n = 5

    job_id = str(uuid.uuid4())
    with jobs_lock:
        jobs[job_id] = {
            "step": "queued",
            "detail": "Job queued",
            "clips": [],
            "error": None,
        }

    thread = threading.Thread(target=_run_job, args=(job_id, url, top_n), daemon=True)
    thread.start()

    return jsonify({"job_id": job_id}), 202


@app.route("/status/<job_id>")
def status(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404

    step_labels = {s: label for s, label in STEPS}
    step_order = [s for s, _ in STEPS]

    current_step = job["step"]
    progress = 0
    if current_step in step_order:
        idx = step_order.index(current_step)
        progress = int((idx / (len(step_order) - 1)) * 100)
    elif current_step == "error":
        progress = 0

    return jsonify({
        "step": current_step,
        "step_label": step_labels.get(current_step, current_step.capitalize()),
        "detail": job.get("detail", ""),
        "progress": progress,
        "clips": job.get("clips", []),
        "error": job.get("error"),
        "done": current_step == "done",
    })


@app.route("/output/<filename>")
def serve_clip(filename: str):
    return send_from_directory(OUTPUT_DIR, filename, as_attachment=False)


@app.route("/download/<filename>")
def download_clip(filename: str):
    return send_from_directory(OUTPUT_DIR, filename, as_attachment=True)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    logger.info(f"ClipGen UI starting on http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
