from __future__ import annotations

from datetime import date, timedelta
from urllib.parse import urlparse

import mysql.connector
from flask import Flask, flash, redirect, render_template, request, url_for

app = Flask(__name__)
app.secret_key = "change-me"  # needed for flash messages


# -----------------------------
# MySQL configuration (EDIT ME)
# -----------------------------
DB_CONFIG = {
    "host": "localhost",
    "user": "root",
    "password": "demo123",
    "database": "job_tracker",
}


ALLOWED_STATUSES = ["Applied", "Interview", "Rejected", "Offer"]
ALLOWED_DIFFICULTIES = ["Easy", "Medium", "Hard"]


_INTERVIEWS_COLUMNS: set[str] | None = None


def get_interviews_columns() -> set[str]:
    """
    Cached lookup of interviews table columns.
    This lets us support legacy schemas (some DBs have a required `venue` column).
    """
    global _INTERVIEWS_COLUMNS
    if _INTERVIEWS_COLUMNS is not None:
        return _INTERVIEWS_COLUMNS

    conn = None
    cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT COLUMN_NAME
            FROM INFORMATION_SCHEMA.COLUMNS
            WHERE TABLE_SCHEMA = %s
              AND TABLE_NAME = 'interviews'
            """,
            (DB_CONFIG["database"],),
        )
        _INTERVIEWS_COLUMNS = {r[0] for r in (cursor.fetchall() or [])}
        return _INTERVIEWS_COLUMNS
    except mysql.connector.Error:
        _INTERVIEWS_COLUMNS = set()
        return _INTERVIEWS_COLUMNS
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()


def is_valid_job_link(url: str) -> bool:
    """
    Very simple URL validation:
    - must be http(s)
    - must have a hostname
    """
    try:
        parsed = urlparse(url)
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)
    except Exception:
        return False


def get_connection():
    # On some Windows setups, mysql-connector's optional native extension can crash.
    # use_pure=True forces the pure-Python implementation (more stable, still beginner-friendly).
    return mysql.connector.connect(**DB_CONFIG, use_pure=True, connection_timeout=5)


def ensure_schema():
    """
    Creates (or migrates) the jobs table if needed.
    Assumes the database (job_tracker) already exists.
    """
    create_jobs_table_sql = """
    CREATE TABLE IF NOT EXISTS jobs (
        id INT AUTO_INCREMENT PRIMARY KEY,
        company VARCHAR(255) NOT NULL,
        role VARCHAR(255) NOT NULL,
        location VARCHAR(255) NOT NULL,
        job_link TEXT NOT NULL,
        status VARCHAR(50) NOT NULL,
        applied_date DATE NOT NULL,
        notes TEXT NULL,

        -- Legacy interview columns (kept for backward compatibility; new data goes to interviews table)
        interview_date DATE NULL,
        interview_time TIME NULL,
        interview_venue VARCHAR(255) NULL,
        interview_completed TINYINT(1) NOT NULL DEFAULT 0,
        interview_difficulty VARCHAR(50) NULL,
        interview_experience_notes TEXT NULL
    );
    """

    create_interviews_table_sql = """
    CREATE TABLE IF NOT EXISTS interviews (
        id INT AUTO_INCREMENT PRIMARY KEY,
        job_id INT NOT NULL,
        company_tag VARCHAR(255) NOT NULL,
        role_tag VARCHAR(255) NOT NULL,
        interview_date DATE NOT NULL,
        interview_time TIME NOT NULL,
        interview_venue VARCHAR(255) NOT NULL,
        interview_completed TINYINT(1) NOT NULL DEFAULT 0,
        interview_difficulty VARCHAR(50) NULL,
        interview_experience_notes TEXT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        UNIQUE KEY uq_interviews_job (job_id),
        CONSTRAINT fk_interviews_job FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE
    );
    """

    conn = None
    cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(create_jobs_table_sql)
        cursor.execute(create_interviews_table_sql)

        # Migrations for existing tables (safe to run multiple times).
        # If a column/index already exists, MySQL will raise an error we ignore.
        alter_statements = [
            "ALTER TABLE jobs ADD COLUMN interview_date DATE NULL",
            "ALTER TABLE jobs ADD COLUMN interview_time TIME NULL",
            "ALTER TABLE jobs ADD COLUMN interview_venue VARCHAR(255) NULL",
            "ALTER TABLE jobs ADD COLUMN interview_completed TINYINT(1) NOT NULL DEFAULT 0",
            "ALTER TABLE jobs ADD COLUMN interview_difficulty VARCHAR(50) NULL",
            "ALTER TABLE jobs ADD COLUMN interview_experience_notes TEXT NULL",
        ]
        for stmt in alter_statements:
            try:
                cursor.execute(stmt)
            except mysql.connector.Error:
                # Likely "Duplicate column name" — ignore for beginner-friendly migrations.
                pass

        alter_interviews_statements = [
            "ALTER TABLE interviews ADD COLUMN company_tag VARCHAR(255) NOT NULL DEFAULT ''",
            "ALTER TABLE interviews ADD COLUMN role_tag VARCHAR(255) NOT NULL DEFAULT ''",
            "ALTER TABLE interviews ADD COLUMN interview_date DATE NULL",
            "ALTER TABLE interviews ADD COLUMN interview_time TIME NULL",
            "ALTER TABLE interviews ADD COLUMN interview_venue VARCHAR(255) NULL",
            "ALTER TABLE interviews ADD COLUMN interview_completed TINYINT(1) NOT NULL DEFAULT 0",
            "ALTER TABLE interviews ADD COLUMN interview_difficulty VARCHAR(50) NULL",
            "ALTER TABLE interviews ADD COLUMN interview_experience_notes TEXT NULL",
            "ALTER TABLE interviews ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
            "ALTER TABLE interviews ADD COLUMN updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP",
            "ALTER TABLE interviews ADD UNIQUE KEY uq_interviews_job (job_id)",
        ]
        for stmt in alter_interviews_statements:
            try:
                cursor.execute(stmt)
            except mysql.connector.Error:
                pass

        # Backfill company_tag / role_tag for existing interview rows if blank
        try:
            cursor.execute(
                """
                UPDATE interviews i
                JOIN jobs j ON j.id = i.job_id
                SET i.company_tag = COALESCE(NULLIF(i.company_tag, ''), j.company),
                    i.role_tag = COALESCE(NULLIF(i.role_tag, ''), j.role)
                """
            )
        except mysql.connector.Error:
            pass

        # Legacy support: some older interview tables have a required `venue` column.
        # Make sure it won't block inserts/migrations.
        try:
            cursor.execute("UPDATE interviews SET venue='Online' WHERE (venue IS NULL OR venue='')")
        except mysql.connector.Error:
            pass

        # One-time-ish migration: copy legacy interview_* data from jobs into interviews
        # if the interviews row doesn't exist yet.
        cursor.execute("SHOW COLUMNS FROM interviews")
        interviews_cols = {row[0] for row in (cursor.fetchall() or [])}

        cursor.execute(
            """
            SELECT j.id AS job_id, j.company, j.role, j.interview_date, j.interview_time, j.interview_venue,
                   j.interview_completed, j.interview_difficulty, j.interview_experience_notes
            FROM jobs j
            LEFT JOIN interviews i ON i.job_id = j.id
            WHERE i.job_id IS NULL
              AND j.interview_date IS NOT NULL
            """
        )
        rows = cursor.fetchall() or []
        for (job_id, company, role, i_date, i_time, i_venue, i_completed, i_diff, i_notes) in rows:
            venue_value = i_venue or "Online"
            if "venue" in interviews_cols:
                cursor.execute(
                    """
                    INSERT INTO interviews (
                      job_id, company_tag, role_tag,
                      interview_date, interview_time, venue,
                      interview_completed, interview_difficulty, interview_experience_notes
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        job_id,
                        company,
                        role,
                        i_date,
                        i_time,
                        venue_value,
                        int(i_completed or 0),
                        i_diff,
                        i_notes,
                    ),
                )
                if "interview_venue" in interviews_cols:
                    cursor.execute(
                        "UPDATE interviews SET interview_venue=%s WHERE job_id=%s",
                        (venue_value, job_id),
                    )
            else:
                cursor.execute(
                    """
                    INSERT INTO interviews (
                      job_id, company_tag, role_tag,
                      interview_date, interview_time, interview_venue,
                      interview_completed, interview_difficulty, interview_experience_notes
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        job_id,
                        company,
                        role,
                        i_date,
                        i_time,
                        venue_value,
                        int(i_completed or 0),
                        i_diff,
                        i_notes,
                    ),
                )

        conn.commit()
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()


@app.route("/", methods=["GET"])
def index():
    conn = None
    cursor = None
    jobs: list[dict] = []
    error_message = None
    today = date.today()

    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)
        interviews_cols = get_interviews_columns()
        venue_expr = "i.interview_venue"
        if "venue" in interviews_cols and "interview_venue" in interviews_cols:
            venue_expr = "COALESCE(i.interview_venue, i.venue)"
        elif "venue" in interviews_cols and "interview_venue" not in interviews_cols:
            venue_expr = "i.venue"

        cursor.execute(
            f"""
            SELECT
              j.id, j.company, j.role, j.location, j.job_link, j.status, j.applied_date, j.notes,
              i.interview_date, i.interview_time, {venue_expr} AS interview_venue, i.interview_completed,
              i.interview_difficulty, i.interview_experience_notes
            FROM jobs j
            LEFT JOIN interviews i ON i.job_id = j.id
            ORDER BY j.applied_date DESC, j.id DESC
            """
        )
        jobs = cursor.fetchall() or []

        # Auto status reminder: Applied for 3+ days => show follow-up reminder
        for job in jobs:
            job["follow_up_reminder"] = False
            if job.get("status") == "Applied" and job.get("applied_date"):
                try:
                    days = (today - job["applied_date"]).days
                    job["follow_up_reminder"] = days >= 3
                except Exception:
                    job["follow_up_reminder"] = False

            # If status is Interview but details aren't filled yet, show a gentle prompt
            job["needs_interview_details"] = job.get("status") == "Interview" and not job.get("interview_date")
    except mysql.connector.Error as e:
        error_message = f"Database error: {e}"
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()

    status_badge = {
        "Applied": "secondary",
        "Interview": "info",
        "Rejected": "danger",
        "Offer": "success",
    }
    return render_template(
        "index.html",
        jobs=jobs,
        error_message=error_message,
        status_badge=status_badge,
        today=today,
    )


@app.route("/add", methods=["GET"])
def add_job_form():
    # Provide a reasonable default date for the form
    return render_template("add_job.html", statuses=ALLOWED_STATUSES, today=date.today().isoformat())


@app.route("/add", methods=["POST"])
def add_job_submit():
    company = (request.form.get("company") or "").strip()
    role = (request.form.get("role") or "").strip()
    location = (request.form.get("location") or "").strip()
    job_link = (request.form.get("job_link") or "").strip()
    status = (request.form.get("status") or "").strip()
    applied_date_str = (request.form.get("applied_date") or "").strip()
    notes = (request.form.get("notes") or "").strip() or None

    # Basic validation
    if not company or not role or not location or not job_link or not applied_date_str:
        flash("Please fill in all required fields.", "danger")
        return redirect(url_for("add_job_form"))
    if not is_valid_job_link(job_link):
        flash("Please enter a valid Job Link (must start with http:// or https://).", "danger")
        return redirect(url_for("add_job_form"))
    if status not in ALLOWED_STATUSES:
        flash("Invalid status selected.", "danger")
        return redirect(url_for("add_job_form"))

    conn = None
    cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO jobs (company, role, location, job_link, status, applied_date, notes)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (company, role, location, job_link, status, applied_date_str, notes),
        )
        conn.commit()
        flash("Job added successfully.", "success")
        return redirect(url_for("index"))
    except mysql.connector.Error as e:
        if conn is not None:
            conn.rollback()
        flash(f"Failed to add job: {e}", "danger")
        return redirect(url_for("add_job_form"))
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()


def fetch_job(job_id: int) -> dict | None:
    conn = None
    cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)
        interviews_cols = get_interviews_columns()
        venue_expr = "i.interview_venue"
        if "venue" in interviews_cols and "interview_venue" in interviews_cols:
            venue_expr = "COALESCE(i.interview_venue, i.venue)"
        elif "venue" in interviews_cols and "interview_venue" not in interviews_cols:
            venue_expr = "i.venue"

        cursor.execute(
            f"""
            SELECT
              j.id, j.company, j.role, j.location, j.job_link, j.status, j.applied_date, j.notes,
              i.interview_date, i.interview_time, {venue_expr} AS interview_venue, i.interview_completed,
              i.interview_difficulty, i.interview_experience_notes
            FROM jobs j
            LEFT JOIN interviews i ON i.job_id = j.id
            WHERE j.id = %s
            """,
            (job_id,),
        )
        return cursor.fetchone()
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()


@app.route("/jobs/<int:job_id>/edit", methods=["GET"])
def edit_job_form(job_id: int):
    job = fetch_job(job_id)
    if not job:
        flash("Job not found.", "danger")
        return redirect(url_for("index"))

    return render_template(
        "edit_job.html",
        job=job,
        statuses=ALLOWED_STATUSES,
    )


@app.route("/jobs/<int:job_id>/edit", methods=["POST"])
def edit_job_submit(job_id: int):
    company = (request.form.get("company") or "").strip()
    role = (request.form.get("role") or "").strip()
    location = (request.form.get("location") or "").strip()
    job_link = (request.form.get("job_link") or "").strip()
    status = (request.form.get("status") or "").strip()
    applied_date_str = (request.form.get("applied_date") or "").strip()
    notes = (request.form.get("notes") or "").strip() or None

    if not company or not role or not location or not job_link or not applied_date_str:
        flash("Please fill in all required fields.", "danger")
        return redirect(url_for("edit_job_form", job_id=job_id))
    if not is_valid_job_link(job_link):
        flash("Please enter a valid Job Link (must start with http:// or https://).", "danger")
        return redirect(url_for("edit_job_form", job_id=job_id))
    if status not in ALLOWED_STATUSES:
        flash("Invalid status selected.", "danger")
        return redirect(url_for("edit_job_form", job_id=job_id))

    conn = None
    cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(
            """
            UPDATE jobs
            SET company=%s, role=%s, location=%s, job_link=%s, status=%s, applied_date=%s, notes=%s
            WHERE id=%s
            """,
            (company, role, location, job_link, status, applied_date_str, notes, job_id),
        )
        conn.commit()
    except mysql.connector.Error as e:
        if conn is not None:
            conn.rollback()
        flash(f"Failed to update job: {e}", "danger")
        return redirect(url_for("edit_job_form", job_id=job_id))
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()

    # If status is Interview, confirm interview details next
    if status == "Interview":
        flash("Status set to Interview. Please confirm interview details.", "info")
        return redirect(url_for("confirm_interview", job_id=job_id))

    flash("Job updated.", "success")
    return redirect(url_for("index"))


@app.route("/jobs/<int:job_id>/interview/confirm", methods=["GET", "POST"])
def confirm_interview(job_id: int):
    job = fetch_job(job_id)
    if not job:
        flash("Job not found.", "danger")
        return redirect(url_for("index"))

    if request.method == "GET":
        return render_template("confirm_interview.html", job=job)

    interview_date = (request.form.get("interview_date") or "").strip()
    interview_time = (request.form.get("interview_time") or "").strip()
    interview_venue = (request.form.get("interview_venue") or "").strip()

    if not interview_date or not interview_time or not interview_venue:
        flash("Please enter interview date, time, and venue (or type Online).", "danger")
        return redirect(url_for("confirm_interview", job_id=job_id))

    conn = None
    cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor()
        interviews_cols = get_interviews_columns()
        # Ensure job status is Interview
        cursor.execute("UPDATE jobs SET status='Interview' WHERE id=%s", (job_id,))

        # Store interview in interviews table (one interview per job for now).
        if "venue" in interviews_cols:
            cursor.execute(
                """
                INSERT INTO interviews (
                  job_id, company_tag, role_tag,
                  interview_date, interview_time, venue,
                  interview_completed
                )
                VALUES (
                  %s,
                  (SELECT company FROM jobs WHERE id=%s),
                  (SELECT role FROM jobs WHERE id=%s),
                  %s, %s, %s,
                  0
                )
                ON DUPLICATE KEY UPDATE
                  company_tag=VALUES(company_tag),
                  role_tag=VALUES(role_tag),
                  interview_date=VALUES(interview_date),
                  interview_time=VALUES(interview_time),
                  venue=VALUES(venue),
                  interview_completed=0
                """,
                (job_id, job_id, job_id, interview_date, interview_time, interview_venue),
            )
            if "interview_venue" in interviews_cols:
                cursor.execute(
                    "UPDATE interviews SET interview_venue=%s WHERE job_id=%s",
                    (interview_venue, job_id),
                )
        else:
            cursor.execute(
                """
                INSERT INTO interviews (
                  job_id, company_tag, role_tag,
                  interview_date, interview_time, interview_venue,
                  interview_completed
                )
                VALUES (
                  %s,
                  (SELECT company FROM jobs WHERE id=%s),
                  (SELECT role FROM jobs WHERE id=%s),
                  %s, %s, %s,
                  0
                )
                ON DUPLICATE KEY UPDATE
                  company_tag=VALUES(company_tag),
                  role_tag=VALUES(role_tag),
                  interview_date=VALUES(interview_date),
                  interview_time=VALUES(interview_time),
                  interview_venue=VALUES(interview_venue),
                  interview_completed=0
                """,
                (job_id, job_id, job_id, interview_date, interview_time, interview_venue),
            )
        conn.commit()
        flash("Interview details saved.", "success")
        return redirect(url_for("interviews"))
    except mysql.connector.Error as e:
        if conn is not None:
            conn.rollback()
        flash(f"Failed to save interview details: {e}", "danger")
        return redirect(url_for("confirm_interview", job_id=job_id))
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()


@app.route("/jobs/<int:job_id>/interview/complete", methods=["GET", "POST"])
def complete_interview(job_id: int):
    job = fetch_job(job_id)
    if not job:
        flash("Job not found.", "danger")
        return redirect(url_for("index"))

    if request.method == "GET":
        # Interview must exist first
        if not job.get("interview_date"):
            flash("Please confirm interview details first.", "info")
            return redirect(url_for("confirm_interview", job_id=job_id))
        return render_template(
            "complete_interview.html",
            job=job,
            difficulties=ALLOWED_DIFFICULTIES,
        )

    difficulty = (request.form.get("interview_difficulty") or "").strip()
    notes = (request.form.get("interview_experience_notes") or "").strip() or None

    if difficulty and difficulty not in ALLOWED_DIFFICULTIES:
        flash("Invalid difficulty selected.", "danger")
        return redirect(url_for("complete_interview", job_id=job_id))

    conn = None
    cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor()
        interviews_cols = get_interviews_columns()
        cursor.execute(
            """
            UPDATE interviews
            SET interview_completed=1,
                interview_difficulty=%s,
                interview_experience_notes=%s
            WHERE job_id=%s
            """,
            (difficulty or None, notes, job_id),
        )
        if "status" in interviews_cols:
            cursor.execute("UPDATE interviews SET status='Completed' WHERE job_id=%s", (job_id,))
        if "experience" in interviews_cols:
            cursor.execute("UPDATE interviews SET experience=%s WHERE job_id=%s", (notes, job_id))
        conn.commit()
        flash("Interview marked as completed and saved.", "success")
        return redirect(url_for("interviews"))
    except mysql.connector.Error as e:
        if conn is not None:
            conn.rollback()
        flash(f"Failed to save interview feedback: {e}", "danger")
        return redirect(url_for("complete_interview", job_id=job_id))
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()


@app.route("/interviews", methods=["GET"])
def interviews():
    conn = None
    cursor = None
    upcoming: list[dict] = []
    past: list[dict] = []
    error_message = None
    today = date.today()
    soon_threshold = today + timedelta(days=3)

    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)

        interviews_cols = get_interviews_columns()
        venue_expr = "i.interview_venue"
        if "venue" in interviews_cols and "interview_venue" in interviews_cols:
            venue_expr = "COALESCE(i.interview_venue, i.venue)"
        elif "venue" in interviews_cols and "interview_venue" not in interviews_cols:
            venue_expr = "i.venue"

        cursor.execute(
            f"""
            SELECT
              i.job_id AS id,
              i.company_tag AS company,
              i.role_tag AS role,
              i.interview_date, i.interview_time, {venue_expr} AS interview_venue,
              i.interview_completed, i.interview_difficulty, i.interview_experience_notes
            FROM interviews i
            JOIN jobs j ON j.id = i.job_id
            WHERE j.status='Interview'
              AND i.interview_completed=0
              AND i.interview_date >= CURDATE()
            ORDER BY i.interview_date ASC, i.interview_time ASC
            """
        )
        upcoming = cursor.fetchall() or []

        cursor.execute(
            f"""
            SELECT
              i.job_id AS id,
              i.company_tag AS company,
              i.role_tag AS role,
              i.interview_date, i.interview_time, {venue_expr} AS interview_venue,
              i.interview_completed, i.interview_difficulty, i.interview_experience_notes
            FROM interviews i
            JOIN jobs j ON j.id = i.job_id
            WHERE j.status='Interview'
              AND (i.interview_completed=1 OR i.interview_date < CURDATE())
            ORDER BY i.interview_date DESC, i.interview_time DESC
            """
        )
        past = cursor.fetchall() or []

        for item in upcoming:
            item["is_today"] = bool(item.get("interview_date") == today)
            item["is_soon"] = bool(item.get("interview_date") and today <= item["interview_date"] <= soon_threshold)

        for item in past:
            item["is_missed"] = bool(item.get("interview_completed") == 0 and item.get("interview_date") and item["interview_date"] < today)
    except mysql.connector.Error as e:
        error_message = f"Database error: {e}"
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()

    return render_template(
        "interviews.html",
        upcoming=upcoming,
        past=past,
        error_message=error_message,
        today=today,
    )


if __name__ == "__main__":
    # Try to create the table at startup so the app works out-of-the-box.
    try:
        ensure_schema()
    except mysql.connector.Error as e:
        print(f"[WARN] Could not ensure schema: {e}")
        print("       Make sure MySQL is running and your DB credentials are correct.")

    # Disable the debug reloader on Windows to avoid confusing "starts then exits" behavior.
    app.run(debug=True, use_reloader=False)


