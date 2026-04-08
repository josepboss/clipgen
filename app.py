import os
import uuid
import sqlite3
import threading
from flask import (
    Flask, jsonify, request, render_template,
    send_from_directory, redirect, url_for, flash,
)
from flask_login import (
    login_required, login_user, logout_user, current_user,
)
from werkzeug.security import generate_password_hash, check_password_hash
from dotenv import load_dotenv
from utils import validate_youtube_url, logger
from processor import run_pipeline
from config_manager import load_config, save_config, is_configured, mask_api_key
from postiz_client import schedule_post
from publisher_db import (
    init_db,
    create_user,
    get_user_by_username,
    get_user_count,
)
from auth import login_manager, User
from buffer_routes import buffer_bp

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("SESSION_SECRET", "clipgen-dev-secret-change-me")

login_manager.init_app(app)
app.register_blueprint(buffer_bp)

OUTPUT_DIR = "output"
os.makedirs(OUTPUT_DIR, exist_ok=True)

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


def _user_output_dir(user_id: str) -> str:
    d = os.path.join(OUTPUT_DIR, user_id)
    os.makedirs(d, exist_ok=True)
    return d


def _set_status(job_id: str, step: str, detail: str = "") -> None:
    with jobs_lock:
        jobs[job_id]["step"]   = step
        jobs[job_id]["detail"] = detail


def _run_job(job_id: str, url: str, top_n: int, user_id: str) -> None:
    try:
        user_dir = _user_output_dir(user_id)

        def on_status(step: str, detail: str = "") -> None:
            _set_status(job_id, step, detail)

        clips = run_pipeline(url, output_dir=user_dir, top_n=top_n, on_status=on_status)
        filenames = [os.path.basename(c) for c in clips]
        with jobs_lock:
            jobs[job_id].update({
                "step":   "done",
                "detail": f"{len(filenames)} clip(s) ready",
                "clips":  filenames,
                "error":  None,
            })
    except Exception as exc:
        import traceback
        logger.error(f"Job {job_id} failed: {exc}")
        with jobs_lock:
            jobs[job_id].update({
                "step":      "error",
                "detail":    str(exc),
                "error":     str(exc),
                "traceback": traceback.format_exc(),
            })


# ── Auth routes ────────────────────────────────────────────────────────────────

@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("index"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email    = request.form.get("email", "").strip() or None
        password = request.form.get("password", "")
        confirm  = request.form.get("confirm", "")

        if not username or len(username) < 3:
            flash("Username must be at least 3 characters.", "error")
        elif not password or len(password) < 8:
            flash("Password must be at least 8 characters.", "error")
        elif password != confirm:
            flash("Passwords do not match.", "error")
        else:
            try:
                row = create_user(username, email, generate_password_hash(password))
                user = User(row["id"], row["username"], row["email"])
                login_user(user, remember=True)
                logger.info(f"[auth] New user registered: {username!r} (id={row['id']})")
                return redirect(url_for("index"))
            except sqlite3.IntegrityError:
                flash("Username or email already taken — please choose another.", "error")

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        row = get_user_by_username(username)
        if row and check_password_hash(row["password_hash"], password):
            user = User(row["id"], row["username"], row["email"])
            login_user(user, remember=True)
            logger.info(f"[auth] Login: {username!r} (id={row['id']})")
            next_url = request.args.get("next") or url_for("index")
            return redirect(next_url)
        flash("Invalid username or password.", "error")

    return render_template("login.html", user_count=get_user_count())


@app.route("/logout")
@login_required
def logout():
    logger.info(f"[auth] Logout: {current_user.username!r}")
    logout_user()
    return redirect(url_for("login"))


# ── Main page ──────────────────────────────────────────────────────────────────

@app.route("/")
@login_required
def index():
    return render_template("index.html", username=current_user.username)


# ── Clip generation ────────────────────────────────────────────────────────────

@app.route("/process", methods=["POST"])
@login_required
def process():
    data  = request.get_json(silent=True) or {}
    url   = (data.get("url") or "").strip()
    top_n = int(data.get("top", 5))

    if not url:
        return jsonify({"error": "No URL provided"}), 400
    if not validate_youtube_url(url):
        return jsonify({"error": "Invalid YouTube URL"}), 400
    top_n = max(1, min(10, top_n))

    job_id  = str(uuid.uuid4())
    user_id = current_user.id
    with jobs_lock:
        jobs[job_id] = {
            "step":    "queued",
            "detail":  "Job queued",
            "clips":   [],
            "error":   None,
            "user_id": user_id,
        }

    threading.Thread(
        target=_run_job, args=(job_id, url, top_n, user_id), daemon=True
    ).start()
    return jsonify({"job_id": job_id}), 202


@app.route("/status/<job_id>")
@login_required
def status(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    if job.get("user_id") != current_user.id:
        return jsonify({"error": "Not found"}), 404

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
@login_required
def serve_clip(filename: str):
    user_dir = _user_output_dir(current_user.id)
    return send_from_directory(user_dir, filename, as_attachment=False)


@app.route("/download/<filename>")
@login_required
def download_clip(filename: str):
    user_dir = _user_output_dir(current_user.id)
    return send_from_directory(user_dir, filename, as_attachment=True)


# ── Postiz scheduling ──────────────────────────────────────────────────────────

@app.route("/schedule", methods=["POST"])
@login_required
def schedule():
    data           = request.get_json(silent=True) or {}
    video_path     = (data.get("video_path") or "").strip()
    caption        = (data.get("caption") or "").strip()
    scheduled_time = (data.get("scheduled_time") or "").strip()
    platform       = data.get("platform") or None

    if not video_path:
        return jsonify({"error": "video_path is required"}), 400
    if not caption:
        return jsonify({"error": "caption is required"}), 400
    if not scheduled_time:
        return jsonify({"error": "scheduled_time is required"}), 400

    user_dir  = _user_output_dir(current_user.id)
    full_path = os.path.join(user_dir, os.path.basename(video_path))
    if not os.path.exists(full_path):
        return jsonify({"error": f"File not found: {os.path.basename(video_path)}"}), 404

    result = schedule_post(full_path, caption, scheduled_time, platform)
    if result.get("success"):
        return jsonify(result), 200
    return jsonify(result), 502


# ── Config (Postiz credentials) ────────────────────────────────────────────────

@app.route("/config", methods=["GET"])
@login_required
def get_config():
    cfg = load_config()
    return jsonify({
        "postiz_api_key":  mask_api_key(cfg.get("postiz_api_key", "")),
        "postiz_base_url": cfg.get("postiz_base_url", ""),
        "configured":      is_configured(),
    })


@app.route("/config", methods=["POST"])
@login_required
def set_config():
    data     = request.get_json(silent=True) or {}
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


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    logger.info(f"ClipGen UI starting on http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
