import os
import shutil
import sqlite3
import threading
import uuid
from functools import wraps

from flask import (
    Flask, abort, jsonify, request, render_template,
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
    get_user_by_id,
    get_user_by_username,
    get_user_count,
    set_admin,
    delete_user,
    get_all_users,
    get_all_publish_history,
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


# ── Helpers ────────────────────────────────────────────────────────────────────

def _user_output_dir(user_id: str) -> str:
    d = os.path.join(OUTPUT_DIR, user_id)
    os.makedirs(d, exist_ok=True)
    return d


def _clip_count(user_id: str) -> int:
    d = os.path.join(OUTPUT_DIR, str(user_id))
    if not os.path.isdir(d):
        return 0
    return sum(1 for f in os.listdir(d) if f.endswith(".mp4"))


def _dir_size_mb(path: str) -> float:
    total = 0
    if os.path.isdir(path):
        for dirpath, _, filenames in os.walk(path):
            for f in filenames:
                try:
                    total += os.path.getsize(os.path.join(dirpath, f))
                except OSError:
                    pass
    return round(total / 1_048_576, 1)


MAX_CLIPS_PER_USER = 30
MIN_AGE_SECS_FOR_DELETE = 300  # 5 minutes — protect files being downloaded


def _cleanup_clips(user_id: str) -> int:
    """Keep the MAX_CLIPS_PER_USER most recent .mp4 files; delete the rest.

    Files younger than MIN_AGE_SECS_FOR_DELETE are never deleted (safety guard
    for in-progress downloads). Returns the number of files deleted.
    """
    user_dir = os.path.join(OUTPUT_DIR, str(user_id))
    if not os.path.isdir(user_dir):
        return 0

    now = __import__("time").time()
    mp4_files = []
    for fname in os.listdir(user_dir):
        if not fname.endswith(".mp4"):
            continue
        fpath = os.path.join(user_dir, fname)
        try:
            mtime = os.path.getmtime(fpath)
            mp4_files.append((mtime, fpath))
        except OSError:
            pass

    # Sort newest-first; keep the first MAX_CLIPS_PER_USER
    mp4_files.sort(reverse=True)
    to_delete = mp4_files[MAX_CLIPS_PER_USER:]
    deleted = 0
    for mtime, fpath in to_delete:
        if now - mtime < MIN_AGE_SECS_FOR_DELETE:
            continue   # too new — skip
        try:
            os.remove(fpath)
            deleted += 1
        except OSError:
            pass

    if deleted:
        logger.info(f"[cleanup] user={user_id}: deleted {deleted} old clip(s)")
    return deleted


def _startup_cleanup() -> None:
    """Run clip cleanup for every existing user directory at startup."""
    if not os.path.isdir(OUTPUT_DIR):
        return
    for uid in os.listdir(OUTPUT_DIR):
        if os.path.isdir(os.path.join(OUTPUT_DIR, uid)):
            _cleanup_clips(uid)

_startup_cleanup()


def admin_required(f):
    """Decorator: requires login + admin status. Returns 403 otherwise."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated:
            return redirect(url_for("login", next=request.path))
        if not current_user.is_admin:
            abort(403)
        return f(*args, **kwargs)
    return decorated


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
        # Keep only the 30 most recent clips for this user
        _cleanup_clips(user_id)
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
                # First user to register automatically becomes admin
                is_first = get_user_count() == 0
                row  = create_user(username, email, generate_password_hash(password))
                if is_first:
                    set_admin(row["id"], True)
                    row = get_user_by_id(row["id"])  # refresh row
                user = User(row["id"], row["username"], row["email"], row.get("is_admin", 0))
                login_user(user, remember=True)
                logger.info(
                    f"[auth] New user registered: {username!r} (id={row['id']}, admin={is_first})"
                )
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
            user = User(row["id"], row["username"], row["email"], row.get("is_admin", 0))
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
    return render_template(
        "index.html",
        username=current_user.username,
        is_admin=current_user.is_admin,
    )


# ── Admin dashboard ────────────────────────────────────────────────────────────

@app.route("/admin")
@admin_required
def admin():
    users   = get_all_users()
    history = get_all_publish_history(50)

    # Augment users with clip counts
    for u in users:
        u["clip_count"] = _clip_count(str(u["id"]))

    # System stats
    total_clips   = sum(u["clip_count"] for u in users)
    storage_mb    = _dir_size_mb(OUTPUT_DIR)
    total_publishes = len(history)

    return render_template(
        "admin.html",
        users=users,
        history=history,
        total_users=len(users),
        total_clips=total_clips,
        storage_mb=storage_mb,
        total_publishes=total_publishes,
        current_user_id=current_user.id,
    )


@app.route("/admin/users/<int:uid>/promote", methods=["POST"])
@admin_required
def admin_promote(uid: int):
    set_admin(uid, True)
    logger.info(f"[admin] {current_user.username!r} promoted user id={uid}")
    return jsonify({"ok": True})


@app.route("/admin/users/<int:uid>/demote", methods=["POST"])
@admin_required
def admin_demote(uid: int):
    if str(uid) == current_user.id:
        return jsonify({"error": "You cannot remove your own admin status."}), 400
    set_admin(uid, False)
    logger.info(f"[admin] {current_user.username!r} demoted user id={uid}")
    return jsonify({"ok": True})


@app.route("/admin/users/<int:uid>/delete", methods=["DELETE"])
@admin_required
def admin_delete_user(uid: int):
    if str(uid) == current_user.id:
        return jsonify({"error": "You cannot delete your own account here."}), 400
    # Remove clip directory
    clip_dir = os.path.join(OUTPUT_DIR, str(uid))
    if os.path.isdir(clip_dir):
        shutil.rmtree(clip_dir, ignore_errors=True)
    deleted = delete_user(uid)
    logger.info(f"[admin] {current_user.username!r} deleted user id={uid}")
    return jsonify({"ok": deleted})


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


@app.route("/output/<filename>", methods=["GET", "DELETE"])
@login_required
def serve_clip(filename: str):
    user_dir  = _user_output_dir(current_user.id)
    safe_name = os.path.basename(filename)
    if request.method == "DELETE":
        fpath = os.path.join(user_dir, safe_name)
        if not os.path.isfile(fpath):
            return jsonify({"error": "File not found."}), 404
        try:
            os.remove(fpath)
            logger.info(f"[clip] user={current_user.id} deleted {safe_name!r}")
            return jsonify({"deleted": True})
        except OSError as exc:
            return jsonify({"error": str(exc)}), 500
    return send_from_directory(user_dir, safe_name, as_attachment=False)


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


# ── Clip history & storage ─────────────────────────────────────────────────────

@app.route("/history")
@login_required
def history():
    """Return JSON list of the most recent MAX_CLIPS_PER_USER clips for current user."""
    import time as _time
    user_dir = _user_output_dir(current_user.id)
    clips = []
    for fname in os.listdir(user_dir):
        if not fname.endswith(".mp4"):
            continue
        fpath = os.path.join(user_dir, fname)
        try:
            stat = os.stat(fpath)
            clips.append({
                "filename":   fname,
                "size_mb":    round(stat.st_size / 1_048_576, 2),
                "created_at": stat.st_mtime,
                "url":        f"/output/{fname}",
                "download":   f"/download/{fname}",
            })
        except OSError:
            pass

    clips.sort(key=lambda c: c["created_at"], reverse=True)
    return jsonify({"clips": clips[:MAX_CLIPS_PER_USER]})


@app.route("/storage")
@login_required
def storage_info():
    """Return storage statistics for the current user."""
    import shutil as _shutil
    user_dir = _user_output_dir(current_user.id)
    clips = []
    for fname in os.listdir(user_dir):
        if not fname.endswith(".mp4"):
            continue
        fpath = os.path.join(user_dir, fname)
        try:
            clips.append((os.path.getmtime(fpath), fname))
        except OSError:
            pass

    clips.sort()
    used_mb = _dir_size_mb(user_dir)
    try:
        disk = _shutil.disk_usage(user_dir)
        free_mb = round(disk.free / 1_048_576, 1)
    except Exception:
        free_mb = None

    return jsonify({
        "used_mb":    used_mb,
        "free_mb":    free_mb,
        "clip_count": len(clips),
        "oldest_clip": clips[0][1]  if clips else None,
        "newest_clip": clips[-1][1] if clips else None,
    })


# ── 403 handler ────────────────────────────────────────────────────────────────

@app.errorhandler(403)
def forbidden(e):
    return render_template("403.html"), 403


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    logger.info(f"ClipGen UI starting on http://0.0.0.0:{port}")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
