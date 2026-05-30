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
ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "TheCure@2024"
# ────────────────────────────────────────────────────────────────────────────

IS_VERCEL    = bool(os.environ.get("VERCEL"))
DATABASE_URL = os.environ.get("DATABASE_URL")          # set this in Vercel env vars
USE_POSTGRES = bool(DATABASE_URL)

SQLITE_PATH = "/tmp/medication_tracker.db" if IS_VERCEL else os.path.join(
    os.path.dirname(__file__), "medication_tracker.db"
)

# Table names — med_ prefix in Supabase to avoid conflicts with existing tables
if USE_POSTGRES:
    T_PATIENTS    = "med_patients"
    T_MEDICATIONS = "med_medications"
    T_SCHEDULES   = "med_schedules"
    T_REMINDERS   = "med_reminders"
    T_LOGS        = "med_logs"
    DATE_NOW      = "CURRENT_DATE"
    MINUS_4H      = "NOW() - INTERVAL '4 hours'"
else:
    T_PATIENTS    = "patients"
    T_MEDICATIONS = "medications"
    T_SCHEDULES   = "schedules"
    T_REMINDERS   = "reminders"
    T_LOGS        = "logs"
    DATE_NOW      = "date('now')"
    MINUS_4H      = "datetime('now', '-4 hours')"


# ── Database abstraction ──────────────────────────────────────────────────────

class _DBContext:
    """Thin wrapper that makes psycopg2 behave like sqlite3 for this app."""

    def __init__(self):
        if USE_POSTGRES:
            import psycopg2
            import psycopg2.extras
            self._conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
            self._pg = True
        else:
            self._conn = sqlite3.connect(SQLITE_PATH)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._pg = False

    def execute(self, sql, params=()):
        if self._pg:
            cur = self._conn.cursor()
            cur.execute(sql, params)
            return cur
        else:
            return self._conn.execute(sql.replace("%s", "?"), params)

    def executescript(self, sql):
        if self._pg:
            pass  # Postgres tables managed via Supabase migration
        else:
            self._conn.executescript(sql)

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type:
            if self._pg:
                self._conn.rollback()
        else:
            self._conn.commit()
        self._conn.close()
        return False


def get_db():
    return _DBContext()


def _days(row_val):
    """Return days list whether stored as JSON string (SQLite) or list (Postgres JSONB)."""
    if isinstance(row_val, list):
        return row_val
    return json.loads(row_val)


# ── DB init (SQLite only) ─────────────────────────────────────────────────────

def init_db():
    if USE_POSTGRES:
        return  # Schema managed via Supabase migration
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


init_db()


# ── Scheduler job ─────────────────────────────────────────────────────────────

def check_and_create_reminders():
    now          = datetime.now()
    current_time = now.strftime("%H:%M")
    current_day  = now.strftime("%a")
    today_str    = now.strftime("%Y-%m-%d")

    with get_db() as db:
        rows = db.execute(
            f"SELECT * FROM {T_SCHEDULES} WHERE active = %s AND reminder_time = %s",
            (True if USE_POSTGRES else 1, current_time),
        ).fetchall()

        for s in rows:
            if current_day not in _days(s["days_of_week"]):
                continue

            existing = db.execute(
                f"SELECT id FROM {T_REMINDERS} WHERE schedule_id = %s AND DATE(scheduled_for) = %s",
                (s["id"], today_str),
            ).fetchone()

            if not existing:
                db.execute(
                    f"""INSERT INTO {T_REMINDERS}
                        (schedule_id, patient_id, medication_id, scheduled_for, status)
                        VALUES (%s, %s, %s, %s, 'pending')""",
                    (s["id"], s["patient_id"], s["medication_id"], now.isoformat()),
                )

        db.execute(
            f"""UPDATE {T_REMINDERS} SET status = 'missed'
                WHERE status = 'pending'
                AND scheduled_for < {MINUS_4H}"""
        )


# Only run the background scheduler when running locally
if not IS_VERCEL:
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
        if not app.debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
            _scheduler = BackgroundScheduler(daemon=True)
            _scheduler.add_job(check_and_create_reminders, "interval", minutes=1)
            _scheduler.start()
            atexit.register(lambda: _scheduler.shutdown(wait=False))
    except Exception:
        pass


# ── Auth decorator ────────────────────────────────────────────────────────────

def login_required(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("admin_logged_in"):
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)
    return decorated


# ── Patient-facing routes ─────────────────────────────────────────────────────

@app.route("/")
def index():
    db       = get_db()
    patients = db.execute(f"SELECT * FROM {T_PATIENTS} ORDER BY name").fetchall()
    db.close()
    return render_template("index.html", patients=patients)


@app.route("/api/reminders/<int:patient_id>")
def api_reminders(patient_id):
    if IS_VERCEL:
        try:
            check_and_create_reminders()
        except Exception:
            pass

    db   = get_db()
    rows = db.execute(
        f"""SELECT r.id, r.scheduled_for, r.status,
                   m.id as medication_id, m.name as medication_name,
                   m.dosage, m.instructions,
                   p.name as patient_name
            FROM {T_REMINDERS} r
            JOIN {T_MEDICATIONS} m ON r.medication_id = m.id
            JOIN {T_PATIENTS}    p ON r.patient_id    = p.id
            WHERE r.patient_id = %s AND r.status = 'pending'
            ORDER BY r.scheduled_for DESC""",
        (patient_id,),
    ).fetchall()
    db.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/confirm", methods=["POST"])
def api_confirm():
    data          = request.get_json(silent=True) or {}
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
            f"""INSERT INTO {T_LOGS}
                (reminder_id, patient_id, medication_id, entered_day, entered_time, initials, notes)
                VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (reminder_id, patient_id, medication_id, entered_day, entered_time, initials, notes),
        )
        if reminder_id:
            db.execute(
                f"UPDATE {T_REMINDERS} SET status = 'confirmed' WHERE id = %s",
                (reminder_id,),
            )

    return jsonify({"success": True, "message": "Medication logged successfully!"})


# ── Admin routes ──────────────────────────────────────────────────────────────

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
    db    = get_db()
    stats = {
        "patients":        db.execute(f"SELECT COUNT(*) FROM {T_PATIENTS}").fetchone()[0],
        "medications":     db.execute(f"SELECT COUNT(*) FROM {T_MEDICATIONS}").fetchone()[0],
        "pending":         db.execute(f"SELECT COUNT(*) FROM {T_REMINDERS} WHERE status='pending'").fetchone()[0],
        "today_confirmed": db.execute(
            f"SELECT COUNT(*) FROM {T_LOGS} WHERE DATE(confirmed_at) = {DATE_NOW}"
        ).fetchone()[0],
    }
    recent_logs = db.execute(
        f"""SELECT l.*, p.name as patient_name, m.name as medication_name, m.dosage
            FROM {T_LOGS} l
            JOIN {T_PATIENTS}    p ON l.patient_id    = p.id
            JOIN {T_MEDICATIONS} m ON l.medication_id = m.id
            ORDER BY l.confirmed_at DESC LIMIT 25"""
    ).fetchall()
    patients    = db.execute(f"SELECT * FROM {T_PATIENTS} ORDER BY name").fetchall()
    medications = db.execute(f"SELECT * FROM {T_MEDICATIONS} ORDER BY name").fetchall()
    db.close()
    return render_template(
        "admin_dashboard.html",
        stats=stats, recent_logs=recent_logs,
        patients=patients, medications=medications,
    )


@app.route("/admin/patients", methods=["GET", "POST"])
@login_required
def admin_patients():
    with get_db() as db:
        if request.method == "POST":
            action = request.form.get("action")
            if action == "add":
                name = request.form.get("name", "").strip()
                if name:
                    db.execute(f"INSERT INTO {T_PATIENTS} (name) VALUES (%s)", (name,))
                    flash(f'Patient "{name}" added.', "success")
            elif action == "delete":
                pid = request.form.get("patient_id")
                db.execute(f"DELETE FROM {T_SCHEDULES} WHERE patient_id = %s", (pid,))
                db.execute(f"DELETE FROM {T_PATIENTS} WHERE id = %s", (pid,))
                flash("Patient removed.", "success")

    db       = get_db()
    patients = db.execute(
        f"""SELECT p.*, COUNT(DISTINCT s.id) as schedule_count
            FROM {T_PATIENTS} p
            LEFT JOIN {T_SCHEDULES} s ON p.id = s.patient_id AND s.active = %s
            GROUP BY p.id ORDER BY p.name""",
        (True if USE_POSTGRES else 1,),
    ).fetchall()
    db.close()
    return render_template("admin_patients.html", patients=patients)


@app.route("/admin/medications", methods=["GET", "POST"])
@login_required
def admin_medications():
    with get_db() as db:
        if request.method == "POST":
            action = request.form.get("action")
            if action == "add":
                name         = request.form.get("name", "").strip()
                dosage       = request.form.get("dosage", "").strip()
                instructions = request.form.get("instructions", "").strip()
                if name:
                    db.execute(
                        f"INSERT INTO {T_MEDICATIONS} (name, dosage, instructions) VALUES (%s, %s, %s)",
                        (name, dosage, instructions),
                    )
                    flash(f'Medication "{name}" added.', "success")
            elif action == "delete":
                mid = request.form.get("medication_id")
                db.execute(f"DELETE FROM {T_MEDICATIONS} WHERE id = %s", (mid,))
                flash("Medication removed.", "success")

    db          = get_db()
    medications = db.execute(f"SELECT * FROM {T_MEDICATIONS} ORDER BY name").fetchall()
    db.close()
    return render_template("admin_medications.html", medications=medications)


@app.route("/admin/schedules", methods=["GET", "POST"])
@login_required
def admin_schedules():
    with get_db() as db:
        if request.method == "POST":
            action = request.form.get("action")
            if action == "add":
                patient_id    = request.form.get("patient_id")
                medication_id = request.form.get("medication_id")
                reminder_time = request.form.get("reminder_time")
                days = request.form.getlist("days") or ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]
                db.execute(
                    f"""INSERT INTO {T_SCHEDULES}
                        (patient_id, medication_id, reminder_time, days_of_week)
                        VALUES (%s, %s, %s, %s)""",
                    (patient_id, medication_id, reminder_time, json.dumps(days)),
                )
                flash("Schedule created.", "success")
            elif action == "delete":
                sid = request.form.get("schedule_id")
                db.execute(f"DELETE FROM {T_SCHEDULES} WHERE id = %s", (sid,))
                flash("Schedule removed.", "success")
            elif action == "toggle":
                sid = request.form.get("schedule_id")
                db.execute(f"UPDATE {T_SCHEDULES} SET active = NOT active WHERE id = %s", (sid,))

    db          = get_db()
    schedules   = db.execute(
        f"""SELECT s.*, p.name as patient_name, m.name as medication_name, m.dosage
            FROM {T_SCHEDULES} s
            JOIN {T_PATIENTS}    p ON s.patient_id    = p.id
            JOIN {T_MEDICATIONS} m ON s.medication_id = m.id
            ORDER BY p.name, s.reminder_time"""
    ).fetchall()
    patients    = db.execute(f"SELECT * FROM {T_PATIENTS} ORDER BY name").fetchall()
    medications = db.execute(f"SELECT * FROM {T_MEDICATIONS} ORDER BY name").fetchall()
    db.close()
    return render_template(
        "admin_schedules.html",
        schedules=schedules, patients=patients, medications=medications,
    )


@app.route("/admin/logs")
@login_required
def admin_logs():
    patient_id = request.args.get("patient_id")
    date_from  = request.args.get("date_from")
    date_to    = request.args.get("date_to")

    query  = f"""SELECT l.*, p.name as patient_name, m.name as medication_name, m.dosage
                 FROM {T_LOGS} l
                 JOIN {T_PATIENTS}    p ON l.patient_id    = p.id
                 JOIN {T_MEDICATIONS} m ON l.medication_id = m.id
                 WHERE 1=1"""
    params = []

    if patient_id:
        query += " AND l.patient_id = %s"; params.append(patient_id)
    if date_from:
        query += " AND DATE(l.confirmed_at) >= %s"; params.append(date_from)
    if date_to:
        query += " AND DATE(l.confirmed_at) <= %s"; params.append(date_to)

    query += " ORDER BY l.confirmed_at DESC"

    db       = get_db()
    logs     = db.execute(query, params).fetchall()
    patients = db.execute(f"SELECT * FROM {T_PATIENTS} ORDER BY name").fetchall()
    db.close()
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
            f"""SELECT id FROM {T_REMINDERS}
                WHERE patient_id = %s AND medication_id = %s
                AND status = 'pending'
                AND DATE(scheduled_for) = {DATE_NOW}""",
            (patient_id, medication_id),
        ).fetchone()

        if existing:
            return jsonify({"success": True, "message": "A pending reminder already exists for today."})

        db.execute(
            f"""INSERT INTO {T_REMINDERS} (patient_id, medication_id, scheduled_for, status)
                VALUES (%s, %s, %s, 'pending')""",
            (patient_id, medication_id, datetime.now().isoformat()),
        )

    return jsonify({"success": True, "message": "Manual reminder sent!"})


@app.route("/admin/api/today-reminders")
@login_required
def api_today_reminders():
    db   = get_db()
    rows = db.execute(
        f"""SELECT r.*, p.name as patient_name, m.name as medication_name
            FROM {T_REMINDERS} r
            JOIN {T_PATIENTS}    p ON r.patient_id    = p.id
            JOIN {T_MEDICATIONS} m ON r.medication_id = m.id
            WHERE DATE(r.scheduled_for) = {DATE_NOW}
            ORDER BY r.scheduled_for DESC"""
    ).fetchall()
    db.close()
    return jsonify([dict(r) for r in rows])


if __name__ == "__main__":
    app.run(debug=True, port=5000, use_reloader=True)
