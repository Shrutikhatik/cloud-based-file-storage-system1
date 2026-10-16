import os
import re
import io
import sqlite3
import secrets
from datetime import datetime, timezone
from functools import wraps

import boto3
from botocore.exceptions import ClientError, BotoCoreError
from flask import (
    Flask, render_template, request, redirect, url_for,
    session, flash, send_file, abort
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DB_PATH = os.path.join(BASE_DIR, "storage.db")
UPLOAD_FOLDER = os.path.join(BASE_DIR, "local_uploads")
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or secrets.token_hex(32)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024

BUCKET_NAME = os.environ.get("S3_BUCKET_NAME", "").strip()
AWS_REGION = os.environ.get("AWS_DEFAULT_REGION", "ap-south-1")
s3 = boto3.client("s3", region_name=AWS_REGION)

ALLOWED_EXTENSIONS = {
    "pdf", "txt", "csv", "doc", "docx", "xls", "xlsx",
    "ppt", "pptx", "png", "jpg", "jpeg", "gif", "webp",
    "mp4", "zip", "rar", "json"
}


def now():
    return datetime.now(timezone.utc).isoformat()


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def columns(db, table):
    return {
        row["name"]
        for row in db.execute(f"PRAGMA table_info({table})").fetchall()
    }


def init_db():
    with get_db() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS folders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(user_id, name)
        );

        CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL DEFAULT 0,
            folder_id INTEGER,
            original_name TEXT NOT NULL DEFAULT '',
            saved_name TEXT,
            object_key TEXT,
            size INTEGER NOT NULL DEFAULT 0,
            content_type TEXT,
            storage_backend TEXT NOT NULL DEFAULT 'local',
            created_at TEXT NOT NULL DEFAULT '',
            deleted INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS login_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            email TEXT NOT NULL,
            success INTEGER NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS shares (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            token TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL
        );
        """)

        # Upgrade older files table without deleting its records.
        existing = columns(db, "files")
        additions = {
            "user_id": "INTEGER NOT NULL DEFAULT 0",
            "folder_id": "INTEGER",
            "original_name": "TEXT NOT NULL DEFAULT ''",
            "saved_name": "TEXT",
            "object_key": "TEXT",
            "size": "INTEGER NOT NULL DEFAULT 0",
            "content_type": "TEXT",
            "storage_backend": "TEXT NOT NULL DEFAULT 'local'",
            "created_at": "TEXT NOT NULL DEFAULT ''",
            "deleted": "INTEGER NOT NULL DEFAULT 0",
        }
        for name, definition in additions.items():
            if name not in existing:
                db.execute(f"ALTER TABLE files ADD COLUMN {name} {definition}")

        existing = columns(db, "files")
        if "saved_name" in existing:
            db.execute("""
                UPDATE files
                SET object_key=saved_name
                WHERE (object_key IS NULL OR object_key='')
                  AND saved_name IS NOT NULL
            """)

        db.execute("""
            UPDATE files SET original_name='unnamed-file'
            WHERE original_name IS NULL OR original_name=''
        """)
        db.execute("""
            UPDATE files SET created_at=?
            WHERE created_at IS NULL OR created_at=''
        """, (now(),))

        # Assign older unowned file records to the first account if one exists.
        first_user = db.execute(
            "SELECT id FROM users ORDER BY id LIMIT 1"
        ).fetchone()
        if first_user:
            db.execute(
                "UPDATE files SET user_id=? WHERE user_id IS NULL OR user_id=0",
                (first_user["id"],)
            )


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapper


def allowed_file(filename):
    return (
        bool(filename)
        and "." in filename
        and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS
    )


@app.route("/")
@login_required
def dashboard():
    uid = session["user_id"]
    with get_db() as db:
        stats = db.execute("""
            SELECT COUNT(*) AS total_files,
                   COALESCE(SUM(size), 0) AS total_bytes
            FROM files
            WHERE user_id=? AND deleted=0
        """, (uid,)).fetchone()

        folders_count = db.execute(
            "SELECT COUNT(*) FROM folders WHERE user_id=?", (uid,)
        ).fetchone()[0]

        files = db.execute("""
            SELECT * FROM files
            WHERE user_id=? AND deleted=0
            ORDER BY id DESC LIMIT 10
        """, (uid,)).fetchall()

        folders = db.execute("""
            SELECT * FROM folders WHERE user_id=? ORDER BY name
        """, (uid,)).fetchall()

    return render_template(
        "index.html",
        stats=stats,
        folders_count=folders_count,
        files=files,
        folders=folders
    )


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        if not name or not email or not password:
            flash("Please fill in all fields.", "danger")
            return render_template("register.html")

        if (
            len(password) < 8
            or not re.search(r"[A-Z]", password)
            or not re.search(r"[a-z]", password)
            or not re.search(r"\d", password)
            or not re.search(r"[^A-Za-z0-9]", password)
        ):
            flash(
                "Password needs 8+ characters, uppercase, lowercase, "
                "a number and a symbol.",
                "danger"
            )
            return render_template("register.html")

        try:
            with get_db() as db:
                cur = db.execute("""
                    INSERT INTO users(name,email,password_hash,created_at)
                    VALUES(?,?,?,?)
                """, (
                    name, email, generate_password_hash(password), now()
                ))
                uid = cur.lastrowid
                db.execute("""
                    INSERT OR IGNORE INTO folders(user_id,name,created_at)
                    VALUES(?,?,?)
                """, (uid, "My Files", now()))

            flash("Registration successful. Please log in.", "success")
            return redirect(url_for("login"))

        except sqlite3.IntegrityError:
            flash("This email is already registered.", "danger")

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")

        with get_db() as db:
            user = db.execute(
                "SELECT * FROM users WHERE email=?", (email,)
            ).fetchone()

            success = bool(
                user and check_password_hash(user["password_hash"], password)
            )

            db.execute("""
                INSERT INTO login_logs(user_id,email,success,created_at)
                VALUES(?,?,?,?)
            """, (
                user["id"] if user else None,
                email or "unknown",
                int(success),
                now()
            ))

        if success:
            session.clear()
            session["user_id"] = user["id"]
            session["user_name"] = user["name"]
            flash("Login successful!", "success")
            return redirect(url_for("dashboard"))

        flash("Invalid email or password.", "danger")

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/upload", methods=["POST"])
@login_required
def upload():
    file = request.files.get("file")
    if not file or not file.filename:
        flash("Please select a file.", "danger")
        return redirect(request.referrer or url_for("dashboard"))

    filename = secure_filename(file.filename)
    if not allowed_file(filename):
        flash("This file type is not allowed.", "danger")
        return redirect(request.referrer or url_for("dashboard"))

    uid = session["user_id"]
    size = file.seek(0, os.SEEK_END)
    file.seek(0)
    content_type = file.mimetype or "application/octet-stream"
    saved_name = secrets.token_hex(12) + "_" + filename
    object_key = f"users/{uid}/{saved_name}"
    backend = "local"

    try:
        if BUCKET_NAME:
            s3.upload_fileobj(
                file,
                BUCKET_NAME,
                object_key,
                ExtraArgs={"ContentType": content_type}
            )
            backend = "s3"
        else:
            file.save(os.path.join(UPLOAD_FOLDER, saved_name))
            object_key = saved_name

        with get_db() as db:
            db.execute("""
                INSERT INTO files
                (user_id,folder_id,original_name,saved_name,object_key,
                 size,content_type,storage_backend,created_at,deleted)
                VALUES(?,NULL,?,?,?,?,?,?,?,0)
            """, (
                uid, filename, saved_name, object_key, size,
                content_type, backend, now()
            ))

        flash("File uploaded successfully.", "success")

    except (OSError, sqlite3.Error, ClientError, BotoCoreError):
        app.logger.exception("File upload failed")
        flash("Upload failed. Check Git Bash for the error.", "danger")

    return redirect(request.referrer or url_for("dashboard"))


@app.route("/download/<int:file_id>")
@login_required
def download(file_id):
    with get_db() as db:
        file = db.execute("""
            SELECT * FROM files
            WHERE id=? AND user_id=? AND deleted=0
        """, (file_id, session["user_id"])).fetchone()

    if not file:
        abort(404)

    try:
        if file["storage_backend"] == "s3" and BUCKET_NAME:
            data = io.BytesIO()
            s3.download_fileobj(BUCKET_NAME, file["object_key"], data)
            data.seek(0)
            return send_file(
                data, as_attachment=True,
                download_name=file["original_name"],
                mimetype=file["content_type"] or "application/octet-stream"
            )

        saved = file["saved_name"] or file["object_key"] or ""
        path = os.path.join(UPLOAD_FOLDER, os.path.basename(saved))
        if not os.path.isfile(path):
            abort(404)
        return send_file(path, as_attachment=True,
                         download_name=file["original_name"])

    except (OSError, ClientError, BotoCoreError):
        app.logger.exception("Download failed")
        flash("Download failed.", "danger")
        return redirect(url_for("dashboard"))


@app.route("/delete/<int:file_id>", methods=["POST"])
@login_required
def delete_file(file_id):
    with get_db() as db:
        result = db.execute("""
            UPDATE files SET deleted=1 WHERE id=? AND user_id=?
        """, (file_id, session["user_id"]))

    flash("File moved to recycle bin." if result.rowcount else "File not found.",
          "success")
    return redirect(request.referrer or url_for("dashboard"))


@app.route("/recycle-bin")
@login_required
def recycle_bin():
    with get_db() as db:
        files = db.execute("""
            SELECT * FROM files
            WHERE user_id=? AND deleted=1 ORDER BY id DESC
        """, (session["user_id"],)).fetchall()
    return render_template("recycle_bin.html", files=files)


@app.route("/restore/<int:file_id>", methods=["POST"])
@login_required
def restore_file(file_id):
    with get_db() as db:
        result = db.execute("""
            UPDATE files SET deleted=0 WHERE id=? AND user_id=?
        """, (file_id, session["user_id"]))

    flash("File restored." if result.rowcount else "File not found.", "success")
    return redirect(url_for("recycle_bin"))


@app.route("/permanent-delete/<int:file_id>", methods=["POST"])
@login_required
def permanent_delete(file_id):
    with get_db() as db:
        file = db.execute("""
            SELECT * FROM files
            WHERE id=? AND user_id=? AND deleted=1
        """, (file_id, session["user_id"])).fetchone()

        if file:
            if file["storage_backend"] == "s3" and BUCKET_NAME:
                try:
                    s3.delete_object(
                        Bucket=BUCKET_NAME, Key=file["object_key"]
                    )
                except (ClientError, BotoCoreError):
                    app.logger.exception("S3 delete failed")
            else:
                saved = file["saved_name"] or file["object_key"] or ""
                path = os.path.join(UPLOAD_FOLDER, os.path.basename(saved))
                if os.path.isfile(path):
                    os.remove(path)

            db.execute("DELETE FROM shares WHERE file_id=?", (file_id,))
            db.execute("DELETE FROM files WHERE id=?", (file_id,))

    flash("File permanently deleted." if file else "File not found.", "success")
    return redirect(url_for("recycle_bin"))


@app.route("/create-folder", methods=["POST"])
@login_required
def create_folder():
    name = request.form.get("name", "").strip()
    if not name:
        flash("Folder name is required.", "danger")
    else:
        try:
            with get_db() as db:
                db.execute("""
                    INSERT INTO folders(user_id,name,created_at) VALUES(?,?,?)
                """, (session["user_id"], name, now()))
            flash("Folder created.", "success")
        except sqlite3.IntegrityError:
            flash("That folder already exists.", "danger")

    return redirect(request.referrer or url_for("dashboard"))


@app.route("/files")
@login_required
def files_page():
    query = request.args.get("q", "").strip()
    uid = session["user_id"]

    with get_db() as db:
        if query:
            files = db.execute("""
                SELECT * FROM files
                WHERE user_id=? AND deleted=0 AND original_name LIKE ?
                ORDER BY id DESC
            """, (uid, f"%{query}%")).fetchall()
        else:
            files = db.execute("""
                SELECT * FROM files
                WHERE user_id=? AND deleted=0 ORDER BY id DESC
            """, (uid,)).fetchall()

        folders = db.execute("""
            SELECT * FROM folders WHERE user_id=? ORDER BY name
        """, (uid,)).fetchall()

    return render_template(
        "files.html", files=files, folders=folders, query=query, q=query
    )


@app.route("/shared")
@login_required
def shared():
    with get_db() as db:
        shares = db.execute("""
            SELECT shares.*, files.original_name
            FROM shares JOIN files ON files.id=shares.file_id
            WHERE shares.user_id=? ORDER BY shares.id DESC
        """, (session["user_id"],)).fetchall()

    return render_template("shared.html", shares=shares)


@app.route("/share/<int:file_id>", methods=["POST"])
@login_required
def create_share(file_id):
    with get_db() as db:
        file = db.execute("""
            SELECT id FROM files
            WHERE id=? AND user_id=? AND deleted=0
        """, (file_id, session["user_id"])).fetchone()

        if not file:
            abort(404)

        token = secrets.token_urlsafe(24)
        db.execute("""
            INSERT INTO shares(file_id,user_id,token,created_at)
            VALUES(?,?,?,?)
        """, (file_id, session["user_id"], token, now()))

    link = url_for("shared_download", token=token, _external=True)
    flash("Share link: " + link, "success")
    return redirect(request.referrer or url_for("files_page"))


@app.route("/share-link/<token>")
def shared_download(token):
    with get_db() as db:
        file = db.execute("""
            SELECT files.* FROM shares
            JOIN files ON files.id=shares.file_id
            WHERE shares.token=? AND files.deleted=0
        """, (token,)).fetchone()

    if not file:
        abort(404)

    if file["storage_backend"] == "s3" and BUCKET_NAME:
        data = io.BytesIO()
        s3.download_fileobj(BUCKET_NAME, file["object_key"], data)
        data.seek(0)
        return send_file(data, as_attachment=True,
                         download_name=file["original_name"])

    saved = file["saved_name"] or file["object_key"] or ""
    path = os.path.join(UPLOAD_FOLDER, os.path.basename(saved))
    if not os.path.isfile(path):
        abort(404)
    return send_file(path, as_attachment=True,
                     download_name=file["original_name"])


@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    uid = session["user_id"]

    with get_db() as db:
        if request.method == "POST":
            name = request.form.get("name", "").strip()
            if name:
                db.execute("UPDATE users SET name=? WHERE id=?", (name, uid))
                session["user_name"] = name
                flash("Profile updated.", "success")
            else:
                flash("Name is required.", "danger")

        user = db.execute("""
            SELECT id,name,email,created_at FROM users WHERE id=?
        """, (uid,)).fetchone()

    return render_template("profile.html", user=user)


@app.errorhandler(500)
def internal_error(error):
    app.logger.exception("Internal server error")
    return "Internal Server Error. Check the Git Bash traceback.", 500


init_db()

if __name__ == "__main__":
    app.run(debug=True)
