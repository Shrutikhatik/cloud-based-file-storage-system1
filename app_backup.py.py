import os
import sqlite3
from datetime import datetime
from flask import (
    Flask, render_template, request, redirect,
    url_for, flash, send_from_directory, abort
)
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "change-this-development-key")

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "local_uploads")
DB_PATH = os.path.join(BASE_DIR, "storage.db")

os.makedirs(UPLOAD_DIR, exist_ok=True)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024

ALLOWED_EXTENSIONS = {
    "pdf", "txt", "csv", "doc", "docx", "xls", "xlsx",
    "ppt", "pptx", "png", "jpg", "jpeg", "gif",
    "mp4", "zip", "rar", "json"
}


def get_db():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    return db


def init_db():
    with get_db() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                original_name TEXT NOT NULL,
                saved_name TEXT NOT NULL,
                size INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )
        """)


@app.route("/")
def dashboard():
    with get_db() as db:
        files = db.execute(
            "SELECT * FROM files ORDER BY id DESC"
        ).fetchall()

    total_bytes = sum(f["size"] for f in files)

    return render_template(
        "index.html",
        files=files,
        folders=[],
        total_files=len(files),
        total_folders=0,
        total_bytes=total_bytes,
        storage_limit=10 * 1024 * 1024 * 1024,
        storage_percent=round(
            total_bytes / (10 * 1024 * 1024 * 1024) * 100, 1
        ),
        shared_count=0
    )


@app.route("/upload", methods=["POST"])
def upload():
    uploaded = request.files.get("file")

    if not uploaded or not uploaded.filename:
        flash("Please select a file first.", "warning")
        return redirect(url_for("dashboard"))

    original_name = secure_filename(uploaded.filename)

    if (
        not original_name
        or "." not in original_name
        or original_name.rsplit(".", 1)[1].lower() not in ALLOWED_EXTENSIONS
    ):
        flash("This file type is not allowed.", "danger")
        return redirect(url_for("dashboard"))

    # Unique saved name avoids overwriting another uploaded file.
    saved_name = datetime.now().strftime("%Y%m%d%H%M%S%f") + "_" + original_name
    saved_path = os.path.join(UPLOAD_DIR, saved_name)

    try:
        uploaded.save(saved_path)
        file_size = os.path.getsize(saved_path)

        with get_db() as db:
            db.execute("""
                INSERT INTO files (original_name, saved_name, size, created_at)
                VALUES (?, ?, ?, ?)
            """, (
                original_name,
                saved_name,
                file_size,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            ))

        flash("File uploaded successfully!", "success")

    except Exception:
        app.logger.exception("File upload failed")
        if os.path.exists(saved_path):
            os.remove(saved_path)
        flash("Upload failed. Check the terminal for details.", "danger")

    return redirect(url_for("dashboard"))


@app.route("/download/<int:file_id>")
def download(file_id):
    with get_db() as db:
        record = db.execute(
            "SELECT * FROM files WHERE id = ?", (file_id,)
        ).fetchone()

    if not record:
        abort(404)

    return send_from_directory(
        UPLOAD_DIR,
        record["saved_name"],
        as_attachment=True,
        download_name=record["original_name"]
    )


@app.route("/delete/<int:file_id>", methods=["POST"])
def delete_file(file_id):
    with get_db() as db:
        record = db.execute(
            "SELECT * FROM files WHERE id = ?", (file_id,)
        ).fetchone()

        if not record:
            abort(404)

        saved_path = os.path.join(UPLOAD_DIR, record["saved_name"])

        if os.path.isfile(saved_path):
            os.remove(saved_path)

        db.execute("DELETE FROM files WHERE id = ?", (file_id,))

    flash("File deleted successfully.", "success")
    return redirect(url_for("dashboard"))


@app.errorhandler(413)
def too_large(_error):
    return "File too large. Maximum upload size is 50 MB.", 413


init_db()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
