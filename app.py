import os
import json
import sqlite3
import atexit
import functools
from datetime import datetime
from flask import (
    Flask, render_template, request, redirect,
    url_for, session, jsonify, flash
)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "change-me-in-production-abc123")

@app.context_processor
def inject_now():
    return {"now": datetime.now}

# ── Admin credentials ────────────────────────────────────────────────────────
# Change ADMIN_USERNAME and ADMIN_PASSWORD to secure values before deploying.
ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "TheCure@2024"
# ────────────────────────────────────────────────────────────────────────────

# Vercel's filesystem is read-only except /tmp
IS_VERCEL = bool(os.environ.get("VERCEL"))
DATABASE = "/tmp/medication_tracker.db" if IS_VERCEL else os.path.join(os.path.dirname(__file__), "medication_tracker.db")


# ── Database helpers ─────────────────────────────────────────────────────────

def get_db():
    db = sqlite3.connect(DATABASE)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


def init_db():
    with get_db() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS patients (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS medications (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                name         TEXT NOT NULL,
                dosage       TEXT,
                instructions TEXT,
                created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS schedules (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                patient_id    INTEGER NOT NULL REFERENCES patients(id),
                medication_id INTEGER NOT NULL REFERENCES medications(id),
                reminder_time TEXT NOT NULL,
                days_of_week  TEXT DEFAULT '["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]',
                active        INTEGER DEFAULT 1,
                created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS reminders (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                schedule_id   INTEGER REFERENCES schedules(id),
                patient_id    INTEGER NOT NULL REFERENCES patients(id),
                medication_id INTEGER NOT NULL REFERENCES medications(id),
                scheduled_for TIMESTAMP NOT NULL,
                status        TEXT DEFAULT 'pending',
                created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS logs (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                reminder_id   INTEGER REFERENCES reminders(id),
                patient_id    INTEGER NOT NULL REFERENCES patients(id),
                medication_id INTEGER NOT NULL REFERENCES medications(id),
                entered_day   TEXT NOT NULL,
                entered_time  TEXT NOT NULL,
                initials      TEXT NOT NULL,
                notes         TEXT,
                confirmed_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        """)


# Initialise tables at import time so Vercel's WSGI runner picks it up
init_db()


# ── Scheduler job ────────────────────────────────────────────────────────────

def check_and_create_reminders():
    now = datetime.now()
    current_time = now.strftime("%H:%M")
    current_day = now.strftime("%a")
    today_str = now.strftime("%Y-%m-%d")

    with get_db() as db:
        schedules = db.execute(
            "SELECT * FROM schedules WHERE active = 1 AND reminder_time = ?",
            (current_time,),
        ).fetchall()

        for s in schedules:
            days = json.loads(s["days_of_week"])
            if current_day not in days:
                continue

            existing = db.execute(
                "SELECT id FROM reminders WHERE schedule_id = ? AND date(scheduled_for) = ?",
                (s["id"], today_str),
            ).fetchone()

            if not existing:
                db.execute(
                    """INSERT INTO reminders (schedule_id, patient_id, medication_id, scheduled_for, status)
                       VALUES (?, ?, ?, ?, 'pending')""",
                    (s["id"], s["patient_id"], s["medication_id"], now.isoformat()),
                )

        # Age out reminders not confirmed after 4 hours
        db.execute(
            """UPDATE reminders SET status = 'missed'
               WHERE status = 'pending'
               AND datetime(scheduled_for) < datetime('now', '-4 hours')"""
        )


# Only start the background scheduler when running locally (not on Vercel serverless)
if not IS_VERCEL:
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        if not app.debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
            _scheduler = BackgroundScheduler(daemon=True)
            _scheduler.add_job(check_and_create_reminders, "interval", minutes=1)
            _scheduler.start()
            atexit.register(lambda: _scheduler.shutdown(wait=False))
    except Exception:
        pass  # APScheduler unavailable — reminders triggered manually


# ── Auth decorator ───────────────────────────────────────────────────────────

def login_required(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("admin_logged_in"):
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)
    return decorated


# ── Patient-facing routes ────────────────────────────────────────────────────

@app.route("/")
def index():
    db = get_db()
    patients = db.execute("SELECT * FROM patients ORDER BY name").fetchall()
    return render_template("index.html", patients=patients)


@app.route("/api/reminders/<int:patient_id>")
def api_reminders(patient_id):
    # On Vercel, also run the schedule check inline (no background scheduler)
    if IS_VERCEL:
        try:
            check_and_create_reminders()
        except Exception:
            pass

    db = get_db()
    rows = db.execute(
        """SELECT r.id, r.scheduled_for, r.status,
                  m.id as medication_id, m.name as medication_name,
                  m.dosage, m.instructions,
                  p.name as patient_name
           FROM reminders r
           JOIN medications m ON r.medication_id = m.id
           JOIN patients p ON r.patient_id = p.id
           WHERE r.patient_id = ? AND r.status = 'pending'
           ORDER BY r.scheduled_for DESC""",
        (patient_id,),
    ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/api/confirm", methods=["POST"])
def api_confirm():
    data = request.get_json(silent=True) or {}
    reminder_id   = data.get("reminder_id")
    patient_id    = data.get("patient_id")
    medication_id = data.get("medication_id")
    entered_day   = data.get("entered_day", "").strip()
    entered_time  = data.get("entered_time", "").strip()
    initials      = data.get("initials", "").strip().upper()
    notes         = data.get("notes", "").strip()

    if not all([patient_id, medication_id, entered_day, entered_time, initials]):
        return jsonify({"error": "All fields are required"}), 400
    if len(initials) > 6:
        return jsonify({"error": "Initials must be 6 characters or fewer"}), 400

    with get_db() as db:
        db.execute(
            """INSERT INTO logs
               (reminder_id, patient_id, medication_id, entered_day, entered_time, initials, notes)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (reminder_id, patient_id, medication_id, entered_day, entered_time, initials, notes),
        )
        if reminder_id:
            db.execute(
                "UPDATE reminders SET status = 'confirmed' WHERE id = ?",
                (reminder_id,),
            )

    return jsonify({"success": True, "message": "Medication logged successfully!"})


# ── Admin routes ─────────────────────────────────────────────────────────────

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if session.get("admin_logged_in"):
        return redirect(url_for("admin_dashboard"))

    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            session["admin_logged_in"] = True
            return redirect(url_for("admin_dashboard"))
        flash("Invalid username or password.", "danger")

    return render_template("admin_login.html")


@app.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))


@app.route("/admin")
@login_required
def admin_dashboard():
    db = get_db()
    stats = {
        "patients":       db.execute("SELECT COUNT(*) FROM patients").fetchone()[0],
        "medications":    db.execute("SELECT COUNT(*) FROM medications").fetchone()[0],
        "pending":        db.execute("SELECT COUNT(*) FROM reminders WHERE status='pending'").fetchone()[0],
        "today_confirmed": db.execute(
            "SELECT COUNT(*) FROM logs WHERE date(confirmed_at) = date('now')"
        ).fetchone()[0],
    }
    recent_logs = db.execute(
        """SELECT l.*, p.name as patient_name, m.name as medication_name, m.dosage
           FROM logs l
           JOIN patients p ON l.patient_id = p.id
           JOIN medications m ON l.medication_id = m.id
           ORDER BY l.confirmed_at DESC LIMIT 25"""
    ).fetchall()
    patients    = db.execute("SELECT * FROM patients ORDER BY name").fetchall()
    medications = db.execute("SELECT * FROM medications ORDER BY name").fetchall()
    return render_template(
        "admin_dashboard.html",
        stats=stats, recent_logs=recent_logs,
        patients=patients, medications=medications,
    )


@app.route("/admin/patients", methods=["GET", "POST"])
@login_required
def admin_patients():
    db = get_db()
    if request.method == "POST":
        action = request.form.get("action")
        if action == "add":
            name = request.form.get("name", "").strip()
            if name:
                db.execute("INSERT INTO patients (name) VALUES (?)", (name,))
                db.commit()
                flash(f'Patient "{name}" added.', "success")
        elif action == "delete":
            pid = request.form.get("patient_id")
            db.execute("DELETE FROM schedules WHERE patient_id = ?", (pid,))
            db.execute("DELETE FROM patients WHERE id = ?", (pid,))
            db.commit()
            flash("Patient removed.", "success")

    patients = db.execute(
        """SELECT p.*, COUNT(DISTINCT s.id) as schedule_count
           FROM patients p
           LEFT JOIN schedules s ON p.id = s.patient_id AND s.active = 1
           GROUP BY p.id ORDER BY p.name"""
    ).fetchall()
    return render_template("admin_patients.html", patients=patients)


@app.route("/admin/medications", methods=["GET", "POST"])
@login_required
def admin_medications():
    db = get_db()
    if request.method == "POST":
        action = request.form.get("action")
        if action == "add":
            name         = request.form.get("name", "").strip()
            dosage       = request.form.get("dosage", "").strip()
            instructions = request.form.get("instructions", "").strip()
            if name:
                db.execute(
                    "INSERT INTO medications (name, dosage, instructions) VALUES (?, ?, ?)",
                    (name, dosage, instructions),
                )
                db.commit()
                flash(f'Medication "{name}" added.', "success")
        elif action == "delete":
            mid = request.form.get("medication_id")
            db.execute("DELETE FROM medications WHERE id = ?", (mid,))
            db.commit()
            flash("Medication removed.", "success")

    medications = db.execute("SELECT * FROM medications ORDER BY name").fetchall()
    return render_template("admin_medications.html", medications=medications)


@app.route("/admin/schedules", methods=["GET", "POST"])
@login_required
def admin_schedules():
    db = get_db()
    if request.method == "POST":
        action = request.form.get("action")
        if action == "add":
            patient_id    = request.form.get("patient_id")
            medication_id = request.form.get("medication_id")
            reminder_time = request.form.get("reminder_time")
            days = request.form.getlist("days") or ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]
            db.execute(
                """INSERT INTO schedules (patient_id, medication_id, reminder_time, days_of_week)
                   VALUES (?, ?, ?, ?)""",
                (patient_id, medication_id, reminder_time, json.dumps(days)),
            )
            db.commit()
            flash("Schedule created.", "success")
        elif action == "delete":
            sid = request.form.get("schedule_id")
            db.execute("DELETE FROM schedules WHERE id = ?", (sid,))
            db.commit()
            flash("Schedule removed.", "success")
        elif action == "toggle":
            sid = request.form.get("schedule_id")
            db.execute("UPDATE schedules SET active = NOT active WHERE id = ?", (sid,))
            db.commit()

    schedules   = db.execute(
        """SELECT s.*, p.name as patient_name, m.name as medication_name, m.dosage
           FROM schedules s
           JOIN patients p ON s.patient_id = p.id
           JOIN medications m ON s.medication_id = m.id
           ORDER BY p.name, s.reminder_time"""
    ).fetchall()
    patients    = db.execute("SELECT * FROM patients ORDER BY name").fetchall()
    medications = db.execute("SELECT * FROM medications ORDER BY name").fetchall()
    return render_template(
        "admin_schedules.html",
        schedules=schedules, patients=patients, medications=medications,
    )


@app.route("/admin/logs")
@login_required
def admin_logs():
    db = get_db()
    patient_id = request.args.get("patient_id")
    date_from  = request.args.get("date_from")
    date_to    = request.args.get("date_to")

    query  = """SELECT l.*, p.name as patient_name, m.name as medication_name, m.dosage
                FROM logs l
                JOIN patients p ON l.patient_id = p.id
                JOIN medications m ON l.medication_id = m.id
                WHERE 1=1"""
    params = []

    if patient_id:
        query += " AND l.patient_id = ?"; params.append(patient_id)
    if date_from:
        query += " AND date(l.confirmed_at) >= ?"; params.append(date_from)
    if date_to:
        query += " AND date(l.confirmed_at) <= ?"; params.append(date_to)

    query += " ORDER BY l.confirmed_at DESC"
    logs     = db.execute(query, params).fetchall()
    patients = db.execute("SELECT * FROM patients ORDER BY name").fetchall()

    return render_template(
        "admin_logs.html",
        logs=logs, patients=patients,
        selected_patient=patient_id, date_from=date_from, date_to=date_to,
    )


@app.route("/admin/api/send-reminder", methods=["POST"])
@login_required
def api_send_reminder():
    data          = request.get_json(silent=True) or {}
    patient_id    = data.get("patient_id")
    medication_id = data.get("medication_id")

    if not patient_id or not medication_id:
        return jsonify({"error": "Patient and medication are required"}), 400

    with get_db() as db:
        existing = db.execute(
            """SELECT id FROM reminders
               WHERE patient_id = ? AND medication_id = ? AND status = 'pending'
               AND date(scheduled_for) = date('now')""",
            (patient_id, medication_id),
        ).fetchone()

        if existing:
            return jsonify({"success": True, "message": "A pending reminder already exists for today."})

        db.execute(
            """INSERT INTO reminders (patient_id, medication_id, scheduled_for, status)
               VALUES (?, ?, ?, 'pending')""",
            (patient_id, medication_id, datetime.now().isoformat()),
        )

    return jsonify({"success": True, "message": "Manual reminder sent!"})


@app.route("/admin/api/today-reminders")
@login_required
def api_today_reminders():
    db = get_db()
    rows = db.execute(
        """SELECT r.*, p.name as patient_name, m.name as medication_name
           FROM reminders r
           JOIN patients p ON r.patient_id = p.id
           JOIN medications m ON r.medication_id = m.id
           WHERE date(r.scheduled_for) = date('now')
           ORDER BY r.scheduled_for DESC"""
    ).fetchall()
    return jsonify([dict(r) for r in rows])


if __name__ == "__main__":
    app.run(debug=True, port=5000, use_reloader=True)
