from __future__ import annotations

from datetime import date, datetime, timedelta
from functools import wraps
from urllib.parse import quote, urlparse

import mysql.connector
from flask import Flask, flash, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

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


def generate_google_calendar_url(
    title: str,
    start_datetime: datetime,
    end_datetime: datetime | None = None,
    description: str = "",
    location: str = "",
) -> str:
    """
    Generate a Google Calendar event URL with pre-filled details.
    No OAuth required - opens Google Calendar in browser with event details.
    
    Args:
        title: Event title (e.g., "Interview at Company")
        start_datetime: Start date and time (datetime object)
        end_datetime: End date and time (defaults to start + 1 hour if None)
        description: Event description
        location: Event location/venue
    
    Returns:
        Google Calendar URL string
    """
    if end_datetime is None:
        end_datetime = start_datetime + timedelta(hours=1)
    
    # Format dates for Google Calendar: YYYYMMDDTHHMMSS (local time, no timezone)
    start_str = start_datetime.strftime("%Y%m%dT%H%M%S")
    end_str = end_datetime.strftime("%Y%m%dT%H%M%S")
    
    # Build Google Calendar URL
    params = {
        "action": "TEMPLATE",
        "text": title,
        "dates": f"{start_str}/{end_str}",
        "details": description,
        "location": location,
    }
    
    # URL encode parameters
    query_string = "&".join(f"{k}={quote(str(v))}" for k, v in params.items() if v)
    
    return f"https://calendar.google.com/calendar/render?{query_string}"


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

    create_users_table_sql = """
    CREATE TABLE IF NOT EXISTS users (
        id INT AUTO_INCREMENT PRIMARY KEY,
        name VARCHAR(255) NOT NULL,
        email VARCHAR(255) NOT NULL UNIQUE,
        password_hash VARCHAR(255) NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );
    """

    create_profiles_table_sql = """
    CREATE TABLE IF NOT EXISTS profiles (
        id INT AUTO_INCREMENT PRIMARY KEY,
        user_id INT NOT NULL UNIQUE,
        name VARCHAR(255) NULL,
        age INT NULL,
        bio TEXT NULL,
        qualification TEXT NULL,
        experience TEXT NULL,
        projects TEXT NULL,
        skills TEXT NULL,
        achievements TEXT NULL,
        portfolio_links TEXT NULL,
        looking_for VARCHAR(255) NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        CONSTRAINT fk_profiles_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
    );
    """

    conn = None
    cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(create_jobs_table_sql)
        cursor.execute(create_interviews_table_sql)
        cursor.execute(create_users_table_sql)
        cursor.execute(create_profiles_table_sql)

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


# ============================================================================
# Authentication helpers
# ============================================================================


def login_required(f):
    """Decorator to protect routes that require authentication."""

    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "user_id" not in session:
            flash("Please log in to access this page.", "info")
            return redirect(url_for("login"))
        return f(*args, **kwargs)

    return decorated_function


# ============================================================================
# Authentication routes (public)
# ============================================================================


@app.route("/register", methods=["GET", "POST"])
def register():
    """User registration page."""
    if request.method == "GET":
        return render_template("register.html")

    name = (request.form.get("name") or "").strip()
    email = (request.form.get("email") or "").strip()
    password = request.form.get("password") or ""
    confirm_password = request.form.get("confirm_password") or ""

    # Validation
    if not name or not email or not password:
        flash("Please fill in all fields.", "danger")
        return redirect(url_for("register"))
    if password != confirm_password:
        flash("Passwords do not match.", "danger")
        return redirect(url_for("register"))
    if len(password) < 6:
        flash("Password must be at least 6 characters long.", "danger")
        return redirect(url_for("register"))

    conn = None
    cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor()

        # Check if email already exists
        cursor.execute("SELECT id FROM users WHERE email = %s", (email,))
        if cursor.fetchone():
            flash("Email already registered. Please log in instead.", "danger")
            return redirect(url_for("login"))

        # Create new user
        password_hash = generate_password_hash(password)
        cursor.execute(
            "INSERT INTO users (name, email, password_hash) VALUES (%s, %s, %s)",
            (name, email, password_hash),
        )
        conn.commit()
        flash("Registration successful! Please log in.", "success")
        return redirect(url_for("login"))
    except mysql.connector.Error as e:
        if conn is not None:
            conn.rollback()
        flash(f"Registration failed: {e}", "danger")
        return redirect(url_for("register"))
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()


@app.route("/login", methods=["GET", "POST"])
def login():
    """User login page."""
    if request.method == "GET":
        # If already logged in, redirect to home
        if "user_id" in session:
            return redirect(url_for("index"))
        return render_template("login.html")

    email = (request.form.get("email") or "").strip()
    password = request.form.get("password") or ""

    if not email or not password:
        flash("Please enter both email and password.", "danger")
        return redirect(url_for("login"))

    conn = None
    cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT id, name, email, password_hash FROM users WHERE email = %s", (email,))
        user = cursor.fetchone()

        if user and check_password_hash(user["password_hash"], password):
            session["user_id"] = user["id"]
            session["user_name"] = user["name"]
            session["user_email"] = user["email"]
            flash(f"Welcome back, {user['name']}!", "success")
            return redirect(url_for("index"))
        else:
            flash("Invalid email or password.", "danger")
            return redirect(url_for("login"))
    except mysql.connector.Error as e:
        flash(f"Login error: {e}", "danger")
        return redirect(url_for("login"))
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()


@app.route("/logout")
def logout():
    """Logout and clear session."""
    session.clear()
    flash("You have been logged out.", "info")
    return redirect(url_for("login"))


# ============================================================================
# Protected job routes
# ============================================================================


@app.route("/", methods=["GET"])
@login_required
def index():
    conn = None
    cursor = None
    jobs: list[dict] = []
    error_message = None
    today = date.today()

    # Get filter parameters from GET request
    filter_company = (request.args.get("company", "") or "").strip()
    filter_status = (request.args.get("status", "") or "").strip()

    # Validate status filter
    if filter_status and filter_status not in ALLOWED_STATUSES:
        filter_status = ""

    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)
        interviews_cols = get_interviews_columns()
        venue_expr = "i.interview_venue"
        if "venue" in interviews_cols and "interview_venue" in interviews_cols:
            venue_expr = "COALESCE(i.interview_venue, i.venue)"
        elif "venue" in interviews_cols and "interview_venue" not in interviews_cols:
            venue_expr = "i.venue"

        # Build WHERE clause based on filters
        where_conditions = []
        query_params = []

        if filter_company:
            where_conditions.append("j.company LIKE %s")
            query_params.append(f"%{filter_company}%")

        if filter_status:
            where_conditions.append("j.status = %s")
            query_params.append(filter_status)

        where_clause = ""
        if where_conditions:
            where_clause = "WHERE " + " AND ".join(where_conditions)

        query = f"""
            SELECT
              j.id, j.company, j.role, j.location, j.job_link, j.status, j.applied_date, j.notes,
              i.interview_date, i.interview_time, {venue_expr} AS interview_venue, i.interview_completed,
              i.interview_difficulty, i.interview_experience_notes
            FROM jobs j
            LEFT JOIN interviews i ON i.job_id = j.id
            {where_clause}
            ORDER BY j.applied_date DESC, j.id DESC
            """

        cursor.execute(query, query_params)
        jobs = cursor.fetchall() or []

        # Fetch jobs needing follow-up reminders (status="Applied" AND applied_date older than 3 days)
        # Using SQL date logic: applied_date < CURDATE() - INTERVAL 3 DAY
        cursor.execute(
            f"""
            SELECT
              j.id, j.company, j.role, j.location, j.job_link, j.status, j.applied_date, j.notes,
              DATEDIFF(CURDATE(), j.applied_date) AS days_ago
            FROM jobs j
            WHERE j.status = 'Applied'
              AND j.applied_date < DATE_SUB(CURDATE(), INTERVAL 3 DAY)
            ORDER BY j.applied_date ASC
            """
        )
        follow_up_reminders = cursor.fetchall() or []

        # Auto status reminder: Applied for 3+ days => show follow-up reminder (for individual job rows)
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
        filter_company=filter_company,
        filter_status=filter_status,
        allowed_statuses=ALLOWED_STATUSES,
        follow_up_reminders=follow_up_reminders,
    )


@app.route("/add", methods=["GET"])
@login_required
def add_job_form():
    # Provide a reasonable default date for the form
    return render_template("add_job.html", statuses=ALLOWED_STATUSES, today=date.today().isoformat())


@app.route("/add", methods=["POST"])
@login_required
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
@login_required
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
@login_required
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
@login_required
def confirm_interview(job_id: int):
    job = fetch_job(job_id)
    if not job:
        flash("Job not found.", "danger")
        return redirect(url_for("index"))

    if request.method == "GET":
        # Generate Google Calendar URL if interview details exist
        calendar_url = None
        if job.get("interview_date") and job.get("interview_time"):
            try:
                # Parse date and time
                interview_date_obj = job["interview_date"]
                if isinstance(interview_date_obj, str):
                    interview_date_obj = datetime.strptime(interview_date_obj, "%Y-%m-%d").date()
                elif isinstance(interview_date_obj, date):
                    pass
                else:
                    interview_date_obj = None

                interview_time_str = job.get("interview_time")
                if interview_time_str:
                    if isinstance(interview_time_str, str):
                        time_parts = interview_time_str.split(":")
                        if len(time_parts) >= 2:
                            start_datetime = datetime.combine(
                                interview_date_obj,
                                datetime.strptime(interview_time_str, "%H:%M:%S").time() if ":" in interview_time_str and len(interview_time_str.split(":")) == 3 else datetime.strptime(interview_time_str, "%H:%M").time(),
                            )
                            title = f"Interview: {job['company']} - {job['role']}"
                            description = f"Job Application Interview\n\nCompany: {job['company']}\nRole: {job['role']}\nLocation: {job.get('location', 'N/A')}"
                            location = job.get("interview_venue") or "Online"
                            calendar_url = generate_google_calendar_url(
                                title=title,
                                start_datetime=start_datetime,
                                description=description,
                                location=location,
                            )
            except Exception:
                calendar_url = None

        return render_template("confirm_interview.html", job=job, calendar_url=calendar_url)

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
        
        # Generate Google Calendar URL after saving
        calendar_url = None
        try:
            interview_date_obj = datetime.strptime(interview_date, "%Y-%m-%d").date()
            interview_time_obj = datetime.strptime(interview_time, "%H:%M").time()
            start_datetime = datetime.combine(interview_date_obj, interview_time_obj)
            
            # Fetch company and role for calendar event
            cursor.execute("SELECT company, role, location FROM jobs WHERE id=%s", (job_id,))
            job_info = cursor.fetchone()
            
            if job_info:
                title = f"Interview: {job_info[0]} - {job_info[1]}"
                description = f"Job Application Interview\n\nCompany: {job_info[0]}\nRole: {job_info[1]}\nLocation: {job_info[2] if job_info[2] else 'N/A'}"
                location = interview_venue
                calendar_url = generate_google_calendar_url(
                    title=title,
                    start_datetime=start_datetime,
                    description=description,
                    location=location,
                )
        except Exception:
            calendar_url = None
        
        flash("Interview details saved.", "success")
        if calendar_url:
            # Store calendar URL in session temporarily to show on redirect
            session["_interview_calendar_url"] = calendar_url
            session["_interview_calendar_job_id"] = job_id
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
@login_required
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
@login_required
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

        # Generate Google Calendar URLs for interviews
        for item in upcoming:
            item["is_today"] = bool(item.get("interview_date") == today)
            item["is_soon"] = bool(item.get("interview_date") and today <= item["interview_date"] <= soon_threshold)
            
            # Generate calendar URL
            calendar_url = None
            if item.get("interview_date") and item.get("interview_time"):
                try:
                    interview_date_obj = item["interview_date"]
                    if isinstance(interview_date_obj, str):
                        interview_date_obj = datetime.strptime(interview_date_obj, "%Y-%m-%d").date()
                    elif not isinstance(interview_date_obj, date):
                        interview_date_obj = None
                    
                    interview_time_str = item.get("interview_time")
                    if interview_date_obj and interview_time_str:
                        if isinstance(interview_time_str, str):
                            time_parts = interview_time_str.split(":")
                            if len(time_parts) >= 2:
                                try:
                                    if len(time_parts) == 3:
                                        time_obj = datetime.strptime(interview_time_str, "%H:%M:%S").time()
                                    else:
                                        time_obj = datetime.strptime(interview_time_str, "%H:%M").time()
                                    start_datetime = datetime.combine(interview_date_obj, time_obj)
                                    title = f"Interview: {item['company']} - {item['role']}"
                                    description = f"Job Application Interview\n\nCompany: {item['company']}\nRole: {item['role']}"
                                    location = item.get("interview_venue") or "Online"
                                    calendar_url = generate_google_calendar_url(
                                        title=title,
                                        start_datetime=start_datetime,
                                        description=description,
                                        location=location,
                                    )
                                except Exception:
                                    pass
                except Exception:
                    pass
            item["calendar_url"] = calendar_url

        for item in past:
            item["is_missed"] = bool(item.get("interview_completed") == 0 and item.get("interview_date") and item["interview_date"] < today)
    except mysql.connector.Error as e:
        error_message = f"Database error: {e}"
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()

    # Check for calendar URL in session (from redirect after saving interview)
    calendar_url = session.pop("_interview_calendar_url", None)
    calendar_job_id = session.pop("_interview_calendar_job_id", None)
    
    return render_template(
        "interviews.html",
        upcoming=upcoming,
        past=past,
        error_message=error_message,
        today=today,
        soon_threshold=soon_threshold,
        calendar_url=calendar_url,
        calendar_job_id=calendar_job_id,
    )


# ============================================================================
# Profile routes
# ============================================================================


@app.route("/about-me", methods=["GET"])
@login_required
def about_me():
    """Display user's profile for easy copy-paste."""
    conn = None
    cursor = None
    profile = None
    error_message = None

    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT name, age, bio, qualification, experience, projects, skills, achievements, portfolio_links, looking_for
            FROM profiles
            WHERE user_id = %s
            """,
            (session["user_id"],),
        )
        profile = cursor.fetchone()
    except mysql.connector.Error as e:
        error_message = f"Database error: {e}"
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()

    return render_template("about_me.html", profile=profile, error_message=error_message)


@app.route("/about-me/edit", methods=["GET", "POST"])
@login_required
def edit_profile():
    """Add or edit user profile."""
    user_id = session["user_id"]

    if request.method == "GET":
        # Fetch existing profile if any
        conn = None
        cursor = None
        profile = None
        try:
            conn = get_connection()
            cursor = conn.cursor(dictionary=True)
            cursor.execute(
                """
                SELECT name, age, bio, qualification, experience, projects, skills, achievements, portfolio_links, looking_for
                FROM profiles
                WHERE user_id = %s
                """,
                (user_id,),
            )
            profile = cursor.fetchone()
        except mysql.connector.Error as e:
            flash(f"Error loading profile: {e}", "danger")
        finally:
            if cursor is not None:
                cursor.close()
            if conn is not None:
                conn.close()

        return render_template("edit_profile.html", profile=profile)

    # POST: Save profile
    name = (request.form.get("name") or "").strip() or None
    age_str = (request.form.get("age") or "").strip()
    age = int(age_str) if age_str.isdigit() else None
    bio = (request.form.get("bio") or "").strip() or None
    qualification = (request.form.get("qualification") or "").strip() or None
    experience = (request.form.get("experience") or "").strip() or None
    projects = (request.form.get("projects") or "").strip() or None
    skills = (request.form.get("skills") or "").strip() or None
    achievements = (request.form.get("achievements") or "").strip() or None
    portfolio_links = (request.form.get("portfolio_links") or "").strip() or None
    looking_for = (request.form.get("looking_for") or "").strip() or None

    conn = None
    cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor()

        # Check if profile exists
        cursor.execute("SELECT id FROM profiles WHERE user_id = %s", (user_id,))
        exists = cursor.fetchone()

        if exists:
            # Update existing profile
            cursor.execute(
                """
                UPDATE profiles
                SET name=%s, age=%s, bio=%s, qualification=%s, experience=%s, projects=%s,
                    skills=%s, achievements=%s, portfolio_links=%s, looking_for=%s
                WHERE user_id=%s
                """,
                (name, age, bio, qualification, experience, projects, skills, achievements, portfolio_links, looking_for, user_id),
            )
        else:
            # Insert new profile
            cursor.execute(
                """
                INSERT INTO profiles (user_id, name, age, bio, qualification, experience, projects, skills, achievements, portfolio_links, looking_for)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (user_id, name, age, bio, qualification, experience, projects, skills, achievements, portfolio_links, looking_for),
            )

        conn.commit()
        flash("Profile saved successfully.", "success")
        return redirect(url_for("about_me"))
    except mysql.connector.Error as e:
        if conn is not None:
            conn.rollback()
        flash(f"Failed to save profile: {e}", "danger")
        return redirect(url_for("edit_profile"))
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    # Try to create the table at startup so the app works out-of-the-box.
    try:
        ensure_schema()
    except mysql.connector.Error as e:
        print(f"[WARN] Could not ensure schema: {e}")
        print("       Make sure MySQL is running and your DB credentials are correct.")

    # Disable the debug reloader on Windows to avoid confusing "starts then exits" behavior.
    app.run(debug=True, use_reloader=False)


