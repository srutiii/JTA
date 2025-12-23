from __future__ import annotations

import os
import re
import json
from datetime import date, datetime, timedelta
from functools import wraps
from urllib.parse import quote, urlparse

import mysql.connector
from flask import Flask, flash, jsonify, redirect, render_template, request, session, url_for, send_from_directory
from werkzeug.exceptions import NotFound
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

# CV parsing imports (with fallback if not installed)
try:
    import PyPDF2
    PDF_AVAILABLE = True
except ImportError:
    PDF_AVAILABLE = False

try:
    from docx import Document
    DOCX_AVAILABLE = True
except ImportError:
    DOCX_AVAILABLE = False

# Import AI service module
try:
    from ai_service import (
        is_ai_available,
        extract_cv_data_deep,
        generate_cover_letter,
        generate_application_email,
        match_jd_cv
    )
    AI_SERVICE_AVAILABLE = True
except ImportError:
    AI_SERVICE_AVAILABLE = False
    # Fallback functions
    def is_ai_available():
        return False
    def extract_cv_data_deep(text):
        return {}
    def generate_cover_letter(*args, **kwargs):
        return "AI service not available"
    def generate_application_email(*args, **kwargs):
        return {"subject": "", "body": ""}
    def match_jd_cv(*args, **kwargs):
        return {"match_score": 0, "matched_skills": [], "missing_skills": [], "summary": ""}

app = Flask(__name__)
app.secret_key = "change-me"  # needed for flash messages

# File upload configuration
UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'uploads', 'cv')
ALLOWED_EXTENSIONS = {'pdf', 'doc', 'docx'}
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB

# Ensure upload directory exists
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = MAX_FILE_SIZE

# AI service availability
GEMINI_CONFIGURED = is_ai_available() if AI_SERVICE_AVAILABLE else False


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


def allowed_file(filename: str) -> bool:
    """Check if file extension is allowed."""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def get_file_size_mb(file_size: int) -> str:
    """Convert file size to MB string."""
    return f"{file_size / (1024 * 1024):.2f} MB"


def extract_text_from_pdf(file_path: str) -> str:
    """Extract text from PDF file."""
    if not PDF_AVAILABLE:
        return ""
    
    try:
        text = ""
        with open(file_path, 'rb') as file:
            pdf_reader = PyPDF2.PdfReader(file)
            for page in pdf_reader.pages:
                text += page.extract_text() + "\n"
        return text
    except Exception:
        return ""


def extract_text_from_docx(file_path: str) -> str:
    """Extract text from DOCX file."""
    if not DOCX_AVAILABLE:
        return ""
    
    try:
        doc = Document(file_path)
        text = "\n".join([paragraph.text for paragraph in doc.paragraphs])
        return text
    except Exception:
        return ""


def extract_cv_data(text: str) -> dict:
    """
    Extract structured data from CV text using pattern matching.
    Returns a dictionary with extracted information.
    """
    data = {}
    
    if not text or not text.strip():
        return data
    
    text_lower = text.lower()
    lines = text.split('\n')
    non_empty_lines = [line.strip() for line in lines if line.strip()]
    
    # Extract name (usually first line or after "Name:" or "Full Name:")
    name_patterns = [
        r'name[:\s]+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)',
        r'full\s+name[:\s]+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)',
    ]
    for pattern in name_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            name_value = match.group(1).strip()
            if name_value:
                data["name"] = name_value
                break
    
    if "name" not in data and non_empty_lines:
        # Try first line if it looks like a name
        first_line = non_empty_lines[0]
        if re.match(r'^[A-Z][a-z]+(?:\s+[A-Z][a-z]+)+$', first_line) and len(first_line.split()) <= 4:
            data["name"] = first_line
    
    # Extract skills (look for "Skills:", "Technical Skills:", etc.)
    skills_section = extract_section(text, ["skills", "technical skills", "core competencies", "proficiencies"])
    if skills_section:
        skills_text = clean_section_text(skills_section)
        if skills_text:
            data["skills"] = skills_text
    
    # Extract education/qualification
    education_section = extract_section(text, ["education", "qualification", "academic background", "degrees"])
    if education_section:
        qual_text = clean_section_text(education_section)
        if qual_text:
            data["qualification"] = qual_text
    
    # Extract experience
    experience_section = extract_section(text, ["experience", "work experience", "employment", "professional experience", "career"])
    if experience_section:
        exp_text = clean_section_text(experience_section)
        if exp_text:
            data["experience"] = exp_text
    
    # Extract achievements/awards
    achievements_section = extract_section(text, ["achievements", "awards", "honors", "accomplishments", "recognition"])
    if achievements_section:
        ach_text = clean_section_text(achievements_section)
        if ach_text:
            data["achievements"] = ach_text
    
    # Extract portfolio links (GitHub, LinkedIn, websites)
    url_pattern = r'https?://[^\s]+|www\.[^\s]+|github\.com/[^\s]+|linkedin\.com/[^\s]+'
    urls = re.findall(url_pattern, text, re.IGNORECASE)
    if urls:
        portfolio_text = "\n".join(urls[:5])  # Limit to 5 links
        if portfolio_text:
            data["portfolio_links"] = portfolio_text
    
    # Extract bio/summary (usually at the top)
    summary_section = extract_section(text, ["summary", "profile", "about", "objective", "professional summary"])
    if summary_section:
        bio_text = clean_section_text(summary_section)
        if bio_text:
            data["bio"] = bio_text
    
    return data


def extract_section(text: str, keywords: list) -> str:
    """Extract a section from text based on keywords."""
    text_lower = text.lower()
    lines = text.split('\n')
    
    for keyword in keywords:
        # Find section header
        for i, line in enumerate(lines):
            if keyword in line.lower() and len(line.strip()) < 100:
                # Extract content until next section or end
                section_lines = []
                for j in range(i + 1, len(lines)):
                    next_line = lines[j].strip()
                    # Stop at next section (all caps or contains common section keywords)
                    if next_line and (
                        next_line.isupper() and len(next_line) > 3 and len(next_line) < 50
                        or any(kw in next_line.lower() for kw in ["experience", "education", "skills", "projects", "contact"])
                    ):
                        break
                    if next_line:
                        section_lines.append(next_line)
                
                if section_lines:
                    return "\n".join(section_lines[:20])  # Limit to 20 lines
    
    return ""


def clean_section_text(text: str) -> str:
    """Clean and format extracted section text."""
    if not text:
        return ""
    
    # Remove excessive whitespace
    text = re.sub(r'\s+', ' ', text)
    # Remove bullet points and special characters at start of lines
    text = re.sub(r'^[•\-\*\d+\.\)]\s*', '', text, flags=re.MULTILINE)
    # Remove email addresses (keep URLs)
    text = re.sub(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b', '', text)
    # Clean up
    text = text.strip()
    
    return text


# extract_cv_data_with_gemini moved to ai_service.py

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


def claim_orphaned_records(user_id: int):
    """
    Claims orphaned records (jobs/interviews with NULL user_id) for the current user.
    This helps migrate existing data to user-based isolation.
    """
    conn = None
    cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        # Claim orphaned jobs
        cursor.execute("UPDATE jobs SET user_id=%s WHERE user_id IS NULL", (user_id,))
        jobs_claimed = cursor.rowcount
        
        # Claim orphaned interviews (update based on job_id)
        cursor.execute("""
            UPDATE interviews i
            JOIN jobs j ON j.id = i.job_id
            SET i.user_id = j.user_id
            WHERE i.user_id IS NULL AND j.user_id = %s
        """, (user_id,))
        interviews_claimed = cursor.rowcount
        
        conn.commit()
        
        if jobs_claimed > 0 or interviews_claimed > 0:
            return jobs_claimed, interviews_claimed
        return 0, 0
    except mysql.connector.Error:
        if conn is not None:
            conn.rollback()
        return 0, 0
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()


def validate_job_ownership(job_id: int, user_id: int) -> bool:
    """
    Validates that a job belongs to the specified user.
    Returns True if owned, False otherwise.
    SECURITY: This function MUST be called before any edit/delete operation.
    """
    conn = None
    cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM jobs WHERE id=%s AND user_id=%s", (job_id, user_id))
        return cursor.fetchone() is not None
    except mysql.connector.Error:
        return False
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()


def validate_interview_ownership(job_id: int, user_id: int) -> bool:
    """
    Validates that an interview (via job_id) belongs to the specified user.
    Returns True if owned, False otherwise.
    SECURITY: This function MUST be called before any edit/delete operation.
    """
    conn = None
    cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM interviews WHERE job_id=%s AND user_id=%s", (job_id, user_id))
        return cursor.fetchone() is not None
    except mysql.connector.Error:
        return False
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()


def ensure_schema():
    """
    Creates (or migrates) the jobs table if needed.
    Assumes the database (job_tracker) already exists.
    """
    create_jobs_table_sql = """
    CREATE TABLE IF NOT EXISTS jobs (
        id INT AUTO_INCREMENT PRIMARY KEY,
        user_id INT NOT NULL,
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
        interview_experience_notes TEXT NULL,
        CONSTRAINT fk_jobs_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
    );
    """

    create_interviews_table_sql = """
    CREATE TABLE IF NOT EXISTS interviews (
        id INT AUTO_INCREMENT PRIMARY KEY,
        job_id INT NOT NULL,
        user_id INT NOT NULL,
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
        CONSTRAINT fk_interviews_job FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE,
        CONSTRAINT fk_interviews_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
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
        -- Legacy fields (kept for backward compatibility)
        name VARCHAR(255) NULL,
        age INT NULL,
        email VARCHAR(255) NULL,
        phone VARCHAR(50) NULL,
        bio TEXT NULL,
        qualification TEXT NULL,
        experience TEXT NULL,
        projects TEXT NULL,
        skills TEXT NULL,
        achievements TEXT NULL,
        portfolio_links TEXT NULL,
        looking_for VARCHAR(255) NULL,
        -- New JSON-based structure
        identity JSON NULL,
        career_intent JSON NULL,
        professional_summary TEXT NULL,
        skills_json JSON NULL,
        experience_json JSON NULL,
        education_json JSON NULL,
        projects_json JSON NULL,
        achievements_json JSON NULL,
        -- CV tracking (for extraction only, not display)
        cv_file_path VARCHAR(500) NULL,
        cv_file_name VARCHAR(255) NULL,
        cv_uploaded_at TIMESTAMP NULL,
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
            "ALTER TABLE jobs ADD COLUMN user_id INT NULL",
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
        
        # After adding user_id column, try to assign existing rows to first user
        # (This is a one-time migration; ongoing orphaned records are handled on login)
        try:
            cursor.execute("SELECT id FROM users ORDER BY id LIMIT 1")
            first_user = cursor.fetchone()
            if first_user:
                default_user_id = first_user[0]
                cursor.execute("UPDATE jobs SET user_id=%s WHERE user_id IS NULL", (default_user_id,))
                conn.commit()
        except mysql.connector.Error:
            pass
        
        # Try to make user_id NOT NULL (will fail if there are still NULL values, which is OK)
        # We'll handle orphaned records dynamically on login
        try:
            cursor.execute("ALTER TABLE jobs MODIFY COLUMN user_id INT NOT NULL")
        except mysql.connector.Error:
            # If there are still NULL values, that's OK - they'll be claimed on next login
            pass
        
        try:
            cursor.execute("ALTER TABLE jobs ADD CONSTRAINT fk_jobs_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE")
        except mysql.connector.Error:
            # Constraint might already exist
            pass

        alter_interviews_statements = [
            "ALTER TABLE interviews ADD COLUMN user_id INT NULL",
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
        
        # Backfill user_id for existing interviews from jobs table
        try:
            cursor.execute("""
                UPDATE interviews i
                JOIN jobs j ON j.id = i.job_id
                SET i.user_id = j.user_id
                WHERE i.user_id IS NULL AND j.user_id IS NOT NULL
            """)
            conn.commit()
        except mysql.connector.Error:
            pass
        
        # Try to make user_id NOT NULL (will fail if there are still NULL values, which is OK)
        # We'll handle orphaned records dynamically on login
        try:
            cursor.execute("ALTER TABLE interviews MODIFY COLUMN user_id INT NOT NULL")
        except mysql.connector.Error:
            # If there are still NULL values, that's OK - they'll be claimed on next login
            pass
        
        try:
            cursor.execute("ALTER TABLE interviews ADD CONSTRAINT fk_interviews_user FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE")
        except mysql.connector.Error:
            # Constraint might already exist
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

        # Add new JSON columns to profiles table if they don't exist
        alter_profiles_statements = [
            "ALTER TABLE profiles ADD COLUMN cv_file_path VARCHAR(500) NULL",
            "ALTER TABLE profiles ADD COLUMN cv_file_name VARCHAR(255) NULL",
            "ALTER TABLE profiles ADD COLUMN cv_uploaded_at TIMESTAMP NULL",
            "ALTER TABLE profiles ADD COLUMN email VARCHAR(255) NULL",
            "ALTER TABLE profiles ADD COLUMN phone VARCHAR(50) NULL",
            "ALTER TABLE profiles ADD COLUMN identity JSON NULL",
            "ALTER TABLE profiles ADD COLUMN career_intent JSON NULL",
            "ALTER TABLE profiles ADD COLUMN professional_summary TEXT NULL",
            "ALTER TABLE profiles ADD COLUMN skills_json JSON NULL",
            "ALTER TABLE profiles ADD COLUMN experience_json JSON NULL",
            "ALTER TABLE profiles ADD COLUMN education_json JSON NULL",
            "ALTER TABLE profiles ADD COLUMN projects_json JSON NULL",
            "ALTER TABLE profiles ADD COLUMN achievements_json JSON NULL",
        ]
        for stmt in alter_profiles_statements:
            try:
                cursor.execute(stmt)
            except mysql.connector.Error:
                # Column might already exist
                pass

        # One-time-ish migration: copy legacy interview_* data from jobs into interviews
        # if the interviews row doesn't exist yet.
        cursor.execute("SHOW COLUMNS FROM interviews")
        interviews_cols = {row[0] for row in (cursor.fetchall() or [])}

        cursor.execute(
            """
            SELECT j.id AS job_id, j.user_id, j.company, j.role, j.interview_date, j.interview_time, j.interview_venue,
                   j.interview_completed, j.interview_difficulty, j.interview_experience_notes
            FROM jobs j
            LEFT JOIN interviews i ON i.job_id = j.id
            WHERE i.job_id IS NULL
              AND j.interview_date IS NOT NULL
            """
        )
        rows = cursor.fetchall() or []
        for (job_id, user_id, company, role, i_date, i_time, i_venue, i_completed, i_diff, i_notes) in rows:
            venue_value = i_venue or "Online"
            if "venue" in interviews_cols:
                cursor.execute(
                    """
                    INSERT INTO interviews (
                      job_id, user_id, company_tag, role_tag,
                      interview_date, interview_time, venue,
                      interview_completed, interview_difficulty, interview_experience_notes
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        job_id,
                        user_id,
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
                        "UPDATE interviews SET interview_venue=%s WHERE job_id=%s AND user_id=%s",
                        (venue_value, job_id, user_id),
                    )
            else:
                cursor.execute(
                    """
                    INSERT INTO interviews (
                      job_id, user_id, company_tag, role_tag,
                      interview_date, interview_time, interview_venue,
                      interview_completed, interview_difficulty, interview_experience_notes
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        job_id,
                        user_id,
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
# Public routes
# ============================================================================


@app.route("/", methods=["GET"])
def landing():
    """Landing page for non-authenticated users."""
    # If already logged in, redirect to dashboard
    if "user_id" in session:
        return redirect(url_for("index"))
    return render_template("landing.html")


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
            
            # Claim any orphaned records (jobs/interviews without user_id) for this user
            jobs_claimed, interviews_claimed = claim_orphaned_records(user["id"])
            if jobs_claimed > 0 or interviews_claimed > 0:
                flash(f"Welcome back, {user['name']}! Migrated {jobs_claimed} job(s) and {interviews_claimed} interview(s) to your account.", "info")
            else:
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


@app.route("/dashboard", methods=["GET"])
@login_required
def index():
    # Ensure user_id exists in session (should be guaranteed by @login_required, but double-check)
    if "user_id" not in session:
        flash("Please log in to access this page.", "info")
        return redirect(url_for("login"))
    
    # Claim any orphaned records for the current user (safety check)
    try:
        claim_orphaned_records(session["user_id"])
    except Exception as e:
        # Log error but don't block page load
        print(f"Warning: Could not claim orphaned records: {e}")
    
    conn = None
    cursor = None
    jobs: list[dict] = []
    follow_up_reminders: list[dict] = []
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

        # CRITICAL: Always filter by user_id for security
        where_conditions.append("j.user_id = %s")
        query_params.append(session["user_id"])

        where_clause = "WHERE " + " AND ".join(where_conditions)

        # CRITICAL: LEFT JOIN must also filter by user_id to prevent cross-user data leaks
        query = f"""
            SELECT
              j.id, j.company, j.role, j.location, j.job_link, j.status, j.applied_date, j.notes,
              i.interview_date, i.interview_time, {venue_expr} AS interview_venue, i.interview_completed,
              i.interview_difficulty, i.interview_experience_notes
            FROM jobs j
            LEFT JOIN interviews i ON i.job_id = j.id AND i.user_id = j.user_id
            {where_clause}
            ORDER BY j.applied_date DESC, j.id DESC
            """

        cursor.execute(query, query_params)
        jobs = cursor.fetchall() or []

        # Fetch jobs needing follow-up reminders (status="Applied" AND applied_date older than 3 days)
        # Using SQL date logic: applied_date < CURDATE() - INTERVAL 3 DAY
        # CRITICAL: Filter by user_id for security
        cursor.execute(
            f"""
            SELECT
              j.id, j.company, j.role, j.location, j.job_link, j.status, j.applied_date, j.notes,
              DATEDIFF(CURDATE(), j.applied_date) AS days_ago
            FROM jobs j
            WHERE j.status = 'Applied'
              AND j.applied_date < DATE_SUB(CURDATE(), INTERVAL 3 DAY)
              AND j.user_id = %s
            ORDER BY j.applied_date ASC
            """,
            (session["user_id"],),
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
    """
    SECURITY: Creates a new job for the logged-in user.
    - user_id is ALWAYS taken from session (never from form input)
    - Route is protected by @login_required decorator
    """
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
        # SECURITY: user_id is ALWAYS from session, NEVER from form input
        cursor.execute(
            """
            INSERT INTO jobs (user_id, company, role, location, job_link, status, applied_date, notes)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (session["user_id"], company, role, location, job_link, status, applied_date_str, notes),
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
    """
    Fetch a job by ID, ensuring it belongs to the current user.
    Returns None if job doesn't exist or doesn't belong to user.
    """
    if "user_id" not in session:
        return None
    
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

        # CRITICAL: LEFT JOIN must also filter by user_id to prevent cross-user data leaks
        cursor.execute(
            f"""
            SELECT
              j.id, j.company, j.role, j.location, j.job_link, j.status, j.applied_date, j.notes,
              i.interview_date, i.interview_time, {venue_expr} AS interview_venue, i.interview_completed,
              i.interview_difficulty, i.interview_experience_notes, j.user_id
            FROM jobs j
            LEFT JOIN interviews i ON i.job_id = j.id AND i.user_id = j.user_id
            WHERE j.id = %s AND j.user_id = %s
            """,
            (job_id, session["user_id"]),
        )
        return cursor.fetchone()
    except mysql.connector.Error as e:
        print(f"Error fetching job: {e}")
        return None
    except Exception as e:
        print(f"Unexpected error fetching job: {e}")
        return None
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()


@app.route("/jobs/<int:job_id>/edit", methods=["GET"])
@login_required
def edit_job_form(job_id: int):
    # CRITICAL: Ownership validation - fetch_job already filters by user_id
    job = fetch_job(job_id)
    if not job:
        flash("Job not found or you don't have permission to access it.", "danger")
        return redirect(url_for("index"))

    return render_template(
        "edit_job.html",
        job=job,
        statuses=ALLOWED_STATUSES,
    )


@app.route("/jobs/<int:job_id>/edit", methods=["POST"])
@login_required
def edit_job_submit(job_id: int):
    # CRITICAL: Validate ownership FIRST before processing any data
    if not validate_job_ownership(job_id, session["user_id"]):
        flash("Job not found or you don't have permission to edit it.", "danger")
        return redirect(url_for("index"))
    
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
        # CRITICAL: Filter by user_id in UPDATE to prevent unauthorized access
        cursor.execute(
            """
            UPDATE jobs
            SET company=%s, role=%s, location=%s, job_link=%s, status=%s, applied_date=%s, notes=%s
            WHERE id=%s AND user_id=%s
            """,
            (company, role, location, job_link, status, applied_date_str, notes, job_id, session["user_id"]),
        )
        if cursor.rowcount == 0:
            flash("Job not found or you don't have permission to edit it.", "danger")
            return redirect(url_for("index"))
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


@app.route("/jobs/<int:job_id>/delete", methods=["POST"], endpoint="delete_job")
@login_required
def delete_job(job_id: int):
    """
    Delete a job and its associated interviews.
    SECURITY: Validates ownership before deletion.
    """
    # CRITICAL: Validate ownership FIRST before processing any data
    if not validate_job_ownership(job_id, session["user_id"]):
        flash("Job not found or you don't have permission to delete it.", "danger")
        return redirect(url_for("index"))
    
    conn = None
    cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        # Fetch job details for flash message
        cursor.execute("SELECT company, role FROM jobs WHERE id=%s AND user_id=%s", (job_id, session["user_id"]))
        job = cursor.fetchone()
        
        if not job:
            flash("Job not found or you don't have permission to delete it.", "danger")
            return redirect(url_for("index"))
        
        # Delete the job (cascading foreign keys will automatically delete associated interviews)
        cursor.execute("DELETE FROM jobs WHERE id=%s AND user_id=%s", (job_id, session["user_id"]))
        
        if cursor.rowcount == 0:
            flash("Job not found or you don't have permission to delete it.", "danger")
            return redirect(url_for("index"))
        
        conn.commit()
        flash(f"Job '{job[0]} - {job[1]}' has been deleted.", "success")
        return redirect(url_for("index"))
    except mysql.connector.Error as e:
        if conn is not None:
            conn.rollback()
        flash(f"Failed to delete job: {e}", "danger")
        return redirect(url_for("index"))
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()


@app.route("/jobs/<int:job_id>/interview/confirm", methods=["GET", "POST"])
@login_required
def confirm_interview(job_id: int):
    # CRITICAL: Validate ownership FIRST before processing any data
    if not validate_job_ownership(job_id, session["user_id"]):
        flash("Job not found or you don't have permission to access it.", "danger")
        return redirect(url_for("index"))
    
    job = fetch_job(job_id)
    if not job:
        flash("Job not found or you don't have permission to access it.", "danger")
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
        # CRITICAL: Ensure job status is Interview and validate ownership
        cursor.execute("UPDATE jobs SET status='Interview' WHERE id=%s AND user_id=%s", (job_id, session["user_id"]))
        if cursor.rowcount == 0:
            flash("Job not found or you don't have permission to access it.", "danger")
            return redirect(url_for("index"))

        # Store interview in interviews table (one interview per job for now).
        # CRITICAL: Include user_id for security
        if "venue" in interviews_cols:
            cursor.execute(
                """
                INSERT INTO interviews (
                  job_id, user_id, company_tag, role_tag,
                  interview_date, interview_time, venue,
                  interview_completed
                )
                VALUES (
                  %s, %s,
                  (SELECT company FROM jobs WHERE id=%s AND user_id=%s),
                  (SELECT role FROM jobs WHERE id=%s AND user_id=%s),
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
                (job_id, session["user_id"], job_id, session["user_id"], job_id, session["user_id"], interview_date, interview_time, interview_venue),
            )
            if "interview_venue" in interviews_cols:
                cursor.execute(
                    "UPDATE interviews SET interview_venue=%s WHERE job_id=%s AND user_id=%s",
                    (interview_venue, job_id, session["user_id"]),
                )
        else:
            cursor.execute(
                """
                INSERT INTO interviews (
                  job_id, user_id, company_tag, role_tag,
                  interview_date, interview_time, interview_venue,
                  interview_completed
                )
                VALUES (
                  %s, %s,
                  (SELECT company FROM jobs WHERE id=%s AND user_id=%s),
                  (SELECT role FROM jobs WHERE id=%s AND user_id=%s),
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
                (job_id, session["user_id"], job_id, session["user_id"], job_id, session["user_id"], interview_date, interview_time, interview_venue),
            )
        conn.commit()
        
        # Generate Google Calendar URL after saving
        calendar_url = None
        try:
            interview_date_obj = datetime.strptime(interview_date, "%Y-%m-%d").date()
            interview_time_obj = datetime.strptime(interview_time, "%H:%M").time()
            start_datetime = datetime.combine(interview_date_obj, interview_time_obj)
            
            # Fetch company and role for calendar event - CRITICAL: Filter by user_id
            cursor.execute("SELECT company, role, location FROM jobs WHERE id=%s AND user_id=%s", (job_id, session["user_id"]))
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
    # CRITICAL: Validate ownership FIRST before processing any data
    if not validate_job_ownership(job_id, session["user_id"]):
        flash("Job not found or you don't have permission to access it.", "danger")
        return redirect(url_for("index"))
    
    job = fetch_job(job_id)
    if not job:
        flash("Job not found or you don't have permission to access it.", "danger")
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
        # CRITICAL: Filter by user_id to prevent unauthorized access
        cursor.execute(
            """
            UPDATE interviews
            SET interview_completed=1,
                interview_difficulty=%s,
                interview_experience_notes=%s
            WHERE job_id=%s AND user_id=%s
            """,
            (difficulty or None, notes, job_id, session["user_id"]),
        )
        if cursor.rowcount == 0:
            flash("Interview not found or you don't have permission to access it.", "danger")
            return redirect(url_for("index"))
        if "status" in interviews_cols:
            cursor.execute("UPDATE interviews SET status='Completed' WHERE job_id=%s AND user_id=%s", (job_id, session["user_id"]))
        if "experience" in interviews_cols:
            cursor.execute("UPDATE interviews SET experience=%s WHERE job_id=%s AND user_id=%s", (notes, job_id, session["user_id"]))
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

        # CRITICAL: Filter by user_id on BOTH tables for security - validate ownership on both
        cursor.execute(
            f"""
            SELECT
              i.job_id AS id,
              i.company_tag AS company,
              i.role_tag AS role,
              i.interview_date, i.interview_time, {venue_expr} AS interview_venue,
              i.interview_completed, i.interview_difficulty, i.interview_experience_notes
            FROM interviews i
            JOIN jobs j ON j.id = i.job_id AND j.user_id = i.user_id
            WHERE j.status='Interview'
              AND i.interview_completed=0
              AND i.interview_date >= CURDATE()
              AND i.user_id = %s
              AND j.user_id = %s
            ORDER BY i.interview_date ASC, i.interview_time ASC
            """,
            (session["user_id"], session["user_id"]),
        )
        upcoming = cursor.fetchall() or []

        # CRITICAL: Filter by user_id on BOTH tables for security - validate ownership on both
        cursor.execute(
            f"""
            SELECT
              i.job_id AS id,
              i.company_tag AS company,
              i.role_tag AS role,
              i.interview_date, i.interview_time, {venue_expr} AS interview_venue,
              i.interview_completed, i.interview_difficulty, i.interview_experience_notes
            FROM interviews i
            JOIN jobs j ON j.id = i.job_id AND j.user_id = i.user_id
            WHERE j.status='Interview'
              AND (i.interview_completed=1 OR i.interview_date < CURDATE())
              AND i.user_id = %s
              AND j.user_id = %s
            ORDER BY i.interview_date DESC, i.interview_time DESC
            """,
            (session["user_id"], session["user_id"]),
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
    """Display user's complete About Me profile with structured sections."""
    conn = None
    cursor = None
    profile = None
    error_message = None

    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            """
            SELECT 
                identity, career_intent, professional_summary,
                skills_json, experience_json, education_json, projects_json, achievements_json,
                name, age, bio, qualification, experience, projects, skills, achievements, portfolio_links, looking_for
            FROM profiles
            WHERE user_id = %s
            """,
            (session["user_id"],),
        )
        profile = cursor.fetchone()
        
        # Parse JSON fields
        if profile:
            # Parse JSON fields if they exist
            for json_field in ['identity', 'career_intent', 'skills_json', 'experience_json', 'education_json', 'projects_json', 'achievements_json']:
                if profile.get(json_field) and isinstance(profile[json_field], str):
                    try:
                        profile[json_field] = json.loads(profile[json_field])
                    except (json.JSONDecodeError, TypeError):
                        profile[json_field] = None
                elif profile.get(json_field) is None:
                    profile[json_field] = None
            
            # Build structured profile data
            profile_data = {
                "identity": profile.get("identity") or {},
                "career_intent": profile.get("career_intent") or {},
                "professional_summary": profile.get("professional_summary") or "",
                "skills": profile.get("skills_json") or {},
                "experience": profile.get("experience_json") or [],
                "education": profile.get("education_json") or [],
                "projects": profile.get("projects_json") or [],
                "achievements": profile.get("achievements_json") or []
            }
            profile["profile_data"] = profile_data
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
    """Add or edit user profile - supports both legacy fields and JSON structure."""
    user_id = session["user_id"]

    if request.method == "GET":
        # Fetch existing profile if any
        conn = None
        cursor = None
        profile = None
        profile_data = None
        has_cv = False
        
        try:
            conn = get_connection()
            cursor = conn.cursor(dictionary=True)
            
            # Fetch profile with all fields
            cursor.execute(
                """
                SELECT 
                    identity, career_intent, professional_summary,
                    skills_json, experience_json, education_json, projects_json, achievements_json,
                    name, age, email, phone, bio, qualification, experience, projects, skills, achievements, portfolio_links, looking_for
                FROM profiles
                WHERE user_id = %s
                """,
                (user_id,),
            )
            profile = cursor.fetchone()
            
            # Parse JSON fields if they exist
            if profile:
                profile_data = {}
                for json_field in ['identity', 'career_intent', 'skills_json', 'experience_json', 'education_json', 'projects_json', 'achievements_json']:
                    if profile.get(json_field) and isinstance(profile[json_field], str):
                        try:
                            profile_data[json_field] = json.loads(profile[json_field])
                        except (json.JSONDecodeError, TypeError):
                            profile_data[json_field] = None
                    elif profile.get(json_field) is None:
                        profile_data[json_field] = None
                    else:
                        profile_data[json_field] = profile[json_field]
                
                profile_data['professional_summary'] = profile.get('professional_summary') or ""
            
            # Check if user has a CV uploaded
            cursor.execute(
                "SELECT cv_file_path FROM profiles WHERE user_id = %s AND cv_file_path IS NOT NULL AND cv_file_path != ''",
                (user_id,)
            )
            cv_result = cursor.fetchone()
            if cv_result and cv_result.get("cv_file_path"):
                # Also verify file exists
                file_path = os.path.join(UPLOAD_FOLDER, os.path.basename(cv_result["cv_file_path"]))
                has_cv = os.path.exists(file_path)
                
        except mysql.connector.Error as e:
            flash(f"Error loading profile: {e}", "danger")
        except Exception as e:
            # Handle file system errors gracefully
            has_cv = False
            print(f"Error checking CV: {e}")
        finally:
            if cursor is not None:
                cursor.close()
            if conn is not None:
                conn.close()
        
        return render_template("edit_profile.html", profile=profile, profile_data=profile_data, has_cv=has_cv)

    # POST: Save profile - handle both JSON structure and legacy fields
    conn = None
    cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor()
        
        # Check if profile exists
        cursor.execute("SELECT id FROM profiles WHERE user_id = %s", (user_id,))
        exists = cursor.fetchone()

        # Build JSON structures from form data
        # Identity
        identity = {
            "name": (request.form.get("identity_name") or "").strip(),
            "email": (request.form.get("identity_email") or "").strip(),
            "phone": (request.form.get("identity_phone") or "").strip(),
            "location": (request.form.get("identity_location") or "").strip(),
            "links": [link.strip() for link in (request.form.get("identity_links") or "").strip().split("\n") if link.strip()]
        }
        identity_json = json.dumps(identity) if any(identity.values()) else None

        # Career Intent
        career_intent = {
            "current_status": (request.form.get("career_current_status") or "").strip(),
            "target_roles": [role.strip() for role in (request.form.get("career_target_roles") or "").strip().split(",") if role.strip()],
            "industry": (request.form.get("career_industry") or "").strip()
        }
        career_intent_json = json.dumps(career_intent) if any(career_intent.values()) else None

        # Professional Summary
        professional_summary = (request.form.get("professional_summary") or "").strip() or None

        # Skills
        skills = {
            "technical": [s.strip() for s in (request.form.get("skills_technical") or "").strip().split(",") if s.strip()],
            "tools": [s.strip() for s in (request.form.get("skills_tools") or "").strip().split(",") if s.strip()],
            "soft": [s.strip() for s in (request.form.get("skills_soft") or "").strip().split(",") if s.strip()]
        }
        skills_json = json.dumps(skills) if any(skills.values()) else None

        # Experience
        experience_list = []
        exp_count = int(request.form.get("experience_count", "0") or "0")
        for i in range(exp_count):
            exp = {
                "company": (request.form.get(f"exp_{i}_company") or "").strip(),
                "role": (request.form.get(f"exp_{i}_role") or "").strip(),
                "duration": (request.form.get(f"exp_{i}_duration") or "").strip(),
                "responsibilities": [r.strip() for r in (request.form.get(f"exp_{i}_responsibilities") or "").strip().split("\n") if r.strip()]
            }
            if exp["company"] or exp["role"]:
                experience_list.append(exp)
        experience_json = json.dumps(experience_list) if experience_list else None

        # Education
        education_list = []
        edu_count = int(request.form.get("education_count", "0") or "0")
        for i in range(edu_count):
            edu = {
                "degree": (request.form.get(f"edu_{i}_degree") or "").strip(),
                "institution": (request.form.get(f"edu_{i}_institution") or "").strip(),
                "year": (request.form.get(f"edu_{i}_year") or "").strip(),
                "specialization": (request.form.get(f"edu_{i}_specialization") or "").strip()
            }
            if edu["degree"] or edu["institution"]:
                education_list.append(edu)
        education_json = json.dumps(education_list) if education_list else None

        # Projects
        projects_list = []
        proj_count = int(request.form.get("projects_count", "0") or "0")
        for i in range(proj_count):
            proj = {
                "name": (request.form.get(f"proj_{i}_name") or "").strip(),
                "tech_stack": (request.form.get(f"proj_{i}_tech_stack") or "").strip(),
                "impact": (request.form.get(f"proj_{i}_impact") or "").strip()
            }
            if proj["name"]:
                projects_list.append(proj)
        projects_json = json.dumps(projects_list) if projects_list else None

        # Achievements
        achievements_list = [a.strip() for a in (request.form.get("achievements") or "").strip().split("\n") if a.strip()]
        achievements_json = json.dumps(achievements_list) if achievements_list else None

        # Legacy fields (for backward compatibility)
        name = identity.get("name") or (request.form.get("name") or "").strip() or None
        email = identity.get("email") or (request.form.get("email") or "").strip() or None
        phone = identity.get("phone") or (request.form.get("phone") or "").strip() or None
        bio = professional_summary or (request.form.get("bio") or "").strip() or None
        looking_for = ", ".join(career_intent.get("target_roles", [])) or (request.form.get("looking_for") or "").strip() or None
        skills_legacy = ", ".join(skills.get("technical", []) + skills.get("tools", [])) or (request.form.get("skills") or "").strip() or None

        if exists:
            # Update existing profile
            cursor.execute(
                """
                UPDATE profiles
                SET identity=%s, career_intent=%s, professional_summary=%s,
                    skills_json=%s, experience_json=%s, education_json=%s, projects_json=%s, achievements_json=%s,
                    name=%s, email=%s, phone=%s, bio=%s, looking_for=%s, skills=%s,
                    updated_at=NOW()
                WHERE user_id=%s
                """,
                (identity_json, career_intent_json, professional_summary, skills_json, experience_json, 
                 education_json, projects_json, achievements_json, name, email, phone, bio, looking_for, 
                 skills_legacy, user_id),
            )
        else:
            # Insert new profile
            cursor.execute(
                """
                INSERT INTO profiles (
                    user_id, identity, career_intent, professional_summary,
                    skills_json, experience_json, education_json, projects_json, achievements_json,
                    name, email, phone, bio, looking_for, skills
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (user_id, identity_json, career_intent_json, professional_summary, skills_json, 
                 experience_json, education_json, projects_json, achievements_json, name, email, phone, 
                 bio, looking_for, skills_legacy),
            )

        conn.commit()
        flash("Profile saved successfully.", "success")
        return redirect(url_for("about_me"))
    except mysql.connector.Error as e:
        if conn is not None:
            conn.rollback()
        flash(f"Failed to save profile: {e}", "danger")
        return redirect(url_for("edit_profile"))
    except Exception as e:
        if conn is not None:
            conn.rollback()
        flash(f"Failed to save profile: {e}", "danger")
        return redirect(url_for("edit_profile"))
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()


# ============================================================================
# CV Upload & Management Routes
# ============================================================================


@app.route("/cv/upload", methods=["GET", "POST"])
@login_required
def upload_cv():
    """
    CV upload - extracts data silently and redirects to About Me.
    CV is only an input, not a visible feature.
    """
    if request.method == "GET":
        # Simple upload page - no CV management UI
        return render_template("upload_cv.html")
    
    # POST: Handle file upload and auto-extract
    if 'cv_file' not in request.files:
        return redirect(url_for("about_me"))
    
    file = request.files['cv_file']
    
    if file.filename == '':
        return redirect(url_for("about_me"))
    
    if not allowed_file(file.filename):
        return redirect(url_for("about_me"))
    
    # Check file size
    file.seek(0, os.SEEK_END)
    file_size = file.tell()
    file.seek(0)
    
    if file_size > MAX_FILE_SIZE:
        return redirect(url_for("about_me"))
    
    # Generate secure filename
    filename = secure_filename(file.filename)
    user_filename = f"{session['user_id']}_{int(datetime.now().timestamp())}_{filename}"
    file_path = os.path.join(UPLOAD_FOLDER, user_filename)
    
    conn = None
    cursor = None
    try:
        # Save file temporarily
        file.save(file_path)
        
        # Extract text from CV
        file_ext = os.path.splitext(file_path)[1].lower()
        text = ""
        
        if file_ext == '.pdf':
            if PDF_AVAILABLE:
                text = extract_text_from_pdf(file_path)
        elif file_ext in ['.docx']:
            if DOCX_AVAILABLE:
                text = extract_text_from_docx(file_path)
        
        # Deep extraction using Gemini
        profile_data = {}
        if text and len(text.strip()) >= 50 and AI_SERVICE_AVAILABLE:
            try:
                profile_data = extract_cv_data_deep(text)
            except Exception as e:
                print(f"Error in deep extraction: {e}")
        
        # Save to database
        conn = get_connection()
        cursor = conn.cursor()
        
        # Delete old CV file if exists
        cursor.execute("SELECT cv_file_path FROM profiles WHERE user_id = %s", (session["user_id"],))
        old_profile = cursor.fetchone()
        if old_profile and old_profile[0]:
            old_file_path = os.path.join(UPLOAD_FOLDER, os.path.basename(old_profile[0]))
            if os.path.exists(old_file_path):
                try:
                    os.remove(old_file_path)
                except OSError:
                    pass
        
        # Store CV file reference and extracted data
        if profile_data:
            # Convert profile_data to JSON strings for storage
            identity_json = json.dumps(profile_data.get("identity", {}))
            career_intent_json = json.dumps(profile_data.get("career_intent", {}))
            skills_json = json.dumps(profile_data.get("skills", {}))
            experience_json = json.dumps(profile_data.get("experience", []))
            education_json = json.dumps(profile_data.get("education", []))
            projects_json = json.dumps(profile_data.get("projects", []))
            achievements_json = json.dumps(profile_data.get("achievements", []))
            
            # Also populate legacy fields for backward compatibility
            identity = profile_data.get("identity", {})
            career_intent = profile_data.get("career_intent", {})
            
            cursor.execute(
                """
                INSERT INTO profiles (
                    user_id, cv_file_path, cv_file_name, cv_uploaded_at,
                    identity, career_intent, professional_summary,
                    skills_json, experience_json, education_json, projects_json, achievements_json,
                    name, email, phone, bio, looking_for, skills
                )
                VALUES (%s, %s, %s, NOW(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    cv_file_path = VALUES(cv_file_path),
                    cv_file_name = VALUES(cv_file_name),
                    cv_uploaded_at = VALUES(cv_uploaded_at),
                    identity = VALUES(identity),
                    career_intent = VALUES(career_intent),
                    professional_summary = VALUES(professional_summary),
                    skills_json = VALUES(skills_json),
                    experience_json = VALUES(experience_json),
                    education_json = VALUES(education_json),
                    projects_json = VALUES(projects_json),
                    achievements_json = VALUES(achievements_json),
                    name = VALUES(name),
                    email = VALUES(email),
                    phone = VALUES(phone),
                    bio = VALUES(bio),
                    looking_for = VALUES(looking_for),
                    skills = VALUES(skills),
                    updated_at = NOW()
                """,
                (
                    session["user_id"], user_filename, filename,
                    identity_json, career_intent_json, profile_data.get("professional_summary", ""),
                    skills_json, experience_json, education_json, projects_json, achievements_json,
                    identity.get("name", ""), identity.get("email", ""), identity.get("phone", ""),
                    profile_data.get("professional_summary", ""),
                    ", ".join(career_intent.get("target_roles", [])) if career_intent.get("target_roles") else "",
                    ", ".join(profile_data.get("skills", {}).get("technical", [])) if profile_data.get("skills", {}).get("technical") else ""
                )
            )
        else:
            # Just store CV file reference if extraction failed
            cursor.execute(
                """
                INSERT INTO profiles (user_id, cv_file_path, cv_file_name, cv_uploaded_at)
                VALUES (%s, %s, %s, NOW())
                ON DUPLICATE KEY UPDATE
                    cv_file_path = VALUES(cv_file_path),
                    cv_file_name = VALUES(cv_file_name),
                    cv_uploaded_at = VALUES(cv_uploaded_at),
                    updated_at = NOW()
                """,
                (session["user_id"], user_filename, filename)
            )
        
        conn.commit()
        
        # Show success message and redirect to About Me
        if profile_data:
            flash("CV uploaded and profile created successfully! You can now view and edit your profile.", "success")
        else:
            flash("CV uploaded successfully. Profile extraction will be available once AI service is configured.", "info")
        return redirect(url_for("about_me"))
    
    except Exception as e:
        if conn is not None:
            conn.rollback()
        # Delete file if processing failed
        if os.path.exists(file_path):
            try:
                os.remove(file_path)
            except OSError:
                pass
        print(f"Error processing CV: {e}")
        # Still redirect silently
        return redirect(url_for("about_me"))
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()


# CV download and delete routes removed - CV is only an input, not a visible feature


@app.route("/cv/extract", methods=["POST"])
@login_required
def extract_cv_data_route():
    """Extract data from user's uploaded CV and return as JSON."""
    conn = None
    cursor = None
    try:
        conn = get_connection()
        cursor = conn.cursor(dictionary=True)
        cursor.execute(
            "SELECT cv_file_path FROM profiles WHERE user_id = %s",
            (session["user_id"],)
        )
        profile = cursor.fetchone()
        
        if not profile or not profile.get("cv_file_path"):
            return jsonify({"error": "No CV file found. Please upload a CV first."}), 404
        
        file_path = os.path.join(UPLOAD_FOLDER, profile["cv_file_path"])
        if not os.path.exists(file_path):
            return jsonify({"error": "CV file not found on server."}), 404
        
        # Extract text based on file type
        file_ext = os.path.splitext(file_path)[1].lower()
        text = ""
        
        if file_ext == '.pdf':
            if not PDF_AVAILABLE:
                return jsonify({"error": "PDF parsing not available. Please install PyPDF2: pip install PyPDF2"}), 500
            text = extract_text_from_pdf(file_path)
        elif file_ext in ['.docx']:
            if not DOCX_AVAILABLE:
                return jsonify({"error": "DOCX parsing not available. Please install python-docx: pip install python-docx"}), 500
            text = extract_text_from_docx(file_path)
        elif file_ext == '.doc':
            return jsonify({"error": "DOC files are not supported. Please convert to PDF or DOCX."}), 400
        else:
            return jsonify({"error": "Unsupported file type."}), 400
        
        if not text or len(text.strip()) < 50:
            return jsonify({"error": "Could not extract sufficient text from CV. The file might be corrupted or image-based."}), 400
        
        # Extract structured data using Gemini AI (fallback to regex if not available)
        if AI_SERVICE_AVAILABLE and is_ai_available():
            extracted_data = extract_cv_data_deep(text)
            
            # Convert Gemini format to form field format
            filtered_data = {}
            if extracted_data.get("name"):
                filtered_data["name"] = extracted_data["name"]
            if extracted_data.get("bio"):
                filtered_data["bio"] = extracted_data["bio"]
            if extracted_data.get("looking_for"):
                filtered_data["looking_for"] = extracted_data["looking_for"]
            if extracted_data.get("skills"):
                # Convert skills array to text format
                if isinstance(extracted_data["skills"], list):
                    filtered_data["skills"] = ", ".join(extracted_data["skills"])
                else:
                    filtered_data["skills"] = str(extracted_data["skills"])
            if extracted_data.get("experience_summary"):
                filtered_data["experience"] = extracted_data["experience_summary"]
            
            # Store the full Gemini-extracted data in database for future use
            try:
                cursor.execute(
                    """
                    UPDATE profiles 
                    SET name = COALESCE(NULLIF(%s, ''), name),
                        email = COALESCE(NULLIF(%s, ''), email),
                        phone = COALESCE(NULLIF(%s, ''), phone),
                        bio = COALESCE(NULLIF(%s, ''), bio),
                        looking_for = COALESCE(NULLIF(%s, ''), looking_for),
                        skills = COALESCE(NULLIF(%s, ''), skills),
                        experience = COALESCE(NULLIF(%s, ''), experience)
                    WHERE user_id = %s
                    """,
                    (
                        extracted_data.get("name", ""),
                        extracted_data.get("email", ""),
                        extracted_data.get("phone", ""),
                        extracted_data.get("bio", ""),
                        extracted_data.get("looking_for", ""),
                        ", ".join(extracted_data.get("skills", [])) if extracted_data.get("skills") else "",
                        extracted_data.get("experience_summary", ""),
                        session["user_id"]
                    )
                )
                conn.commit()
            except Exception as e:
                print(f"Error storing extracted data: {e}")
                # Continue even if storage fails
            
            message = f"CV data extracted using AI. Found {len([v for v in filtered_data.values() if v])} field(s) with data."
        else:
            # Fallback to regex-based extraction
            extracted_data = extract_cv_data(text)
            
            # Filter out empty values and ensure no None values
            filtered_data = {}
            for key, value in extracted_data.items():
                if value and str(value).strip() and str(value).strip().lower() != 'none':
                    filtered_data[key] = str(value).strip()
            
            message = f"CV data extracted successfully. Found {len(filtered_data)} field(s) with data. (Note: Install Google Generative AI for better extraction)"
        
        return jsonify({
            "success": True,
            "data": filtered_data,
            "message": message
        })
    
    except Exception as e:
        return jsonify({"error": f"Error extracting CV data: {str(e)}"}), 500
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()


@app.route("/cv/extract-text", methods=["POST"])
@login_required
def extract_cv_text_route():
    """Extract data from pasted CV text using Gemini AI."""
    try:
        data = request.get_json()
        if not data or 'text' not in data:
            return jsonify({"error": "No text provided."}), 400
        
        cv_text = data['text'].strip()
        if len(cv_text) < 50:
            return jsonify({"error": "Text too short. Please provide at least 50 characters."}), 400
        
        # Extract using Gemini
        if AI_SERVICE_AVAILABLE and is_ai_available():
            extracted_data = extract_cv_data_deep(cv_text)
            
            # Convert Gemini format to form field format
            filtered_data = {}
            if extracted_data.get("name"):
                filtered_data["name"] = extracted_data["name"]
            if extracted_data.get("bio"):
                filtered_data["bio"] = extracted_data["bio"]
            if extracted_data.get("looking_for"):
                filtered_data["looking_for"] = extracted_data["looking_for"]
            if extracted_data.get("skills"):
                if isinstance(extracted_data["skills"], list):
                    filtered_data["skills"] = ", ".join(extracted_data["skills"])
                else:
                    filtered_data["skills"] = str(extracted_data["skills"])
            if extracted_data.get("experience_summary"):
                filtered_data["experience"] = extracted_data["experience_summary"]
            
            return jsonify({
                "success": True,
                "data": filtered_data,
                "gemini_data": extracted_data,  # Include full Gemini response
                "message": f"CV data extracted using AI. Found {len([v for v in filtered_data.values() if v])} field(s) with data."
            })
        else:
            return jsonify({
                "error": "Gemini API not configured. Please set GEMINI_API_KEY environment variable."
            }), 500
    
    except Exception as e:
        return jsonify({"error": f"Error extracting CV data: {str(e)}"}), 500


# ============================================================================
# Common browser request handlers (to prevent 404 logs)
# ============================================================================


@app.route("/favicon.ico")
def favicon():
    """Handle favicon requests silently."""
    return "", 204  # No Content - appropriate for missing favicon


@app.route("/.well-known/appspecific/com.chrome.devtools.json")
def chrome_devtools():
    """Handle Chrome DevTools requests silently."""
    return "", 204  # No Content


@app.errorhandler(404)
def not_found(error):
    """Handle 404 Not Found errors gracefully."""
    # For 404s, redirect to home if logged in, otherwise login
    try:
        if "user_id" in session:
            flash("Page not found.", "info")
            return redirect(url_for("index")), 404
    except Exception:
        pass
    return redirect(url_for("login")), 404


@app.errorhandler(500)
def internal_error(error):
    """Handle 500 Internal Server Errors gracefully."""
    try:
        print(f"[ERROR] 500 Internal Server Error: {error}")
        flash("An internal server error occurred. Please try again later.", "danger")
        # Safely check session and redirect
        try:
            if "user_id" in session:
                return redirect(url_for("index")), 500
        except Exception:
            pass
        return redirect(url_for("login")), 500
    except Exception as e:
        # If error handler itself fails, return simple error page
        print(f"[CRITICAL] Error handler failed: {e}")
        return "<h1>500 Internal Server Error</h1><p>An error occurred. Please try again later.</p>", 500


@app.errorhandler(Exception)
def handle_exception(e):
    """Handle all unhandled exceptions (excluding 404 which has its own handler)."""
    # Let 404 errors be handled by the specific 404 handler
    if isinstance(e, NotFound):
        raise
    
    try:
        print(f"[ERROR] Unhandled exception: {e}")
        print(f"[ERROR] Exception type: {type(e).__name__}")
        import traceback
        traceback.print_exc()
        
        # Safely check session and redirect
        try:
            if "user_id" in session:
                flash("An error occurred. Please try again.", "danger")
                return redirect(url_for("index")), 500
        except Exception:
            pass
        
        flash("An error occurred. Please try logging in again.", "danger")
        return redirect(url_for("login")), 500
    except Exception as handler_error:
        # If error handler itself fails, return simple error page
        print(f"[CRITICAL] Error handler failed: {handler_error}")
        return "<h1>500 Internal Server Error</h1><p>An error occurred. Please try again later.</p>", 500


if __name__ == "__main__":
    # Try to create the table at startup so the app works out-of-the-box.
    try:
        ensure_schema()
    except mysql.connector.Error as e:
        print(f"[WARN] Could not ensure schema: {e}")
        print("       Make sure MySQL is running and your DB credentials are correct.")

    # Disable the debug reloader on Windows to avoid confusing "starts then exits" behavior.
    app.run(debug=True, use_reloader=False)


