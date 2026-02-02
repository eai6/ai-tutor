"""
Student Authentication System for TEVETA AI Tutor
Handles registration, login, sessions, and profile management
"""

import sqlite3
import hashlib
import secrets
import re
from datetime import datetime, timedelta
from functools import wraps
from flask import session, redirect, url_for, request, jsonify

# =============================================================================
# DATABASE SCHEMA
# =============================================================================

AUTH_SCHEMA = """
CREATE TABLE IF NOT EXISTS student_accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id TEXT UNIQUE NOT NULL,
    email TEXT UNIQUE NOT NULL,
    phone TEXT,
    password_hash TEXT NOT NULL,
    salt TEXT NOT NULL,
    first_name TEXT NOT NULL,
    last_name TEXT NOT NULL,
    date_of_birth TEXT,
    gender TEXT,
    national_id TEXT,
    institution_name TEXT,
    program_id TEXT,
    program_name TEXT,
    enrollment_year INTEGER,
    is_active INTEGER DEFAULT 1,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    last_login TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS student_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id TEXT NOT NULL,
    session_token TEXT UNIQUE NOT NULL,
    device_info TEXT,
    ip_address TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    expires_at TEXT NOT NULL,
    is_active INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS login_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT,
    success INTEGER,
    ip_address TEXT,
    failure_reason TEXT,
    attempted_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""

def init_auth_db(db_path: str = "ai_tutor.db"):
    """Initialize authentication tables."""
    conn = sqlite3.connect(db_path)
    conn.executescript(AUTH_SCHEMA)
    conn.commit()
    conn.close()
    print("Authentication tables initialized.")


# =============================================================================
# UTILITIES
# =============================================================================

def generate_salt():
    return secrets.token_hex(32)

def hash_password(password: str, salt: str) -> str:
    return hashlib.sha256((password + salt).encode()).hexdigest()

def verify_password(password: str, salt: str, password_hash: str) -> bool:
    return hash_password(password, salt) == password_hash

def generate_student_id() -> str:
    year = datetime.now().year
    random_part = secrets.token_hex(4).upper()
    return f"STU-{year}-{random_part}"

def generate_token() -> str:
    return secrets.token_urlsafe(32)

def validate_email(email: str) -> bool:
    pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    return re.match(pattern, email) is not None

def validate_password(password: str) -> tuple:
    if len(password) < 8:
        return False, "Password must be at least 8 characters"
    if not re.search(r'[A-Za-z]', password):
        return False, "Password must contain at least one letter"
    if not re.search(r'\d', password):
        return False, "Password must contain at least one number"
    return True, ""


# =============================================================================
# REGISTRATION & LOGIN
# =============================================================================

def register_student(db_path: str, email: str, password: str, first_name: str, 
                     last_name: str, phone: str = None, institution_name: str = None,
                     program_id: str = None, program_name: str = None) -> dict:
    """Register a new student account."""
    
    if not validate_email(email):
        return {"success": False, "error": "Invalid email format"}
    
    is_valid, error = validate_password(password)
    if not is_valid:
        return {"success": False, "error": error}
    
    if not first_name or not last_name:
        return {"success": False, "error": "First name and last name are required"}
    
    conn = sqlite3.connect(db_path)
    
    existing = conn.execute("SELECT id FROM student_accounts WHERE email = ?", 
                           (email.lower(),)).fetchone()
    if existing:
        conn.close()
        return {"success": False, "error": "Email already registered"}
    
    student_id = generate_student_id()
    salt = generate_salt()
    password_hash = hash_password(password, salt)
    
    try:
        conn.execute("""
            INSERT INTO student_accounts 
            (student_id, email, phone, password_hash, salt, first_name, last_name,
             institution_name, program_id, program_name)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (student_id, email.lower(), phone, password_hash, salt, 
              first_name.strip(), last_name.strip(), institution_name,
              program_id, program_name))
        conn.commit()
        conn.close()
        
        return {
            "success": True, 
            "student_id": student_id,
            "message": f"Registration successful! Your Student ID is {student_id}"
        }
    except Exception as e:
        conn.close()
        return {"success": False, "error": str(e)}


def login_student(db_path: str, email: str, password: str, 
                  ip_address: str = None, device_info: str = None) -> dict:
    """Authenticate student and create session."""
    
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    
    student = conn.execute("""
        SELECT * FROM student_accounts WHERE email = ? OR student_id = ?
    """, (email.lower(), email)).fetchone()
    
    if not student:
        conn.execute("""
            INSERT INTO login_history (email, success, ip_address, failure_reason)
            VALUES (?, 0, ?, 'Account not found')
        """, (email.lower(), ip_address))
        conn.commit()
        conn.close()
        return {"success": False, "error": "Invalid email/Student ID or password"}
    
    if not verify_password(password, student['salt'], student['password_hash']):
        conn.execute("""
            INSERT INTO login_history (email, success, ip_address, failure_reason)
            VALUES (?, 0, ?, 'Invalid password')
        """, (email.lower(), ip_address))
        conn.commit()
        conn.close()
        return {"success": False, "error": "Invalid email/Student ID or password"}
    
    if not student['is_active']:
        conn.close()
        return {"success": False, "error": "Account is deactivated"}
    
    session_token = generate_token()
    expires_at = (datetime.now() + timedelta(days=7)).isoformat()
    
    conn.execute("""
        INSERT INTO student_sessions (student_id, session_token, device_info, ip_address, expires_at)
        VALUES (?, ?, ?, ?, ?)
    """, (student['student_id'], session_token, device_info, ip_address, expires_at))
    
    conn.execute("UPDATE student_accounts SET last_login = ? WHERE student_id = ?",
                (datetime.now().isoformat(), student['student_id']))
    
    conn.execute("""
        INSERT INTO login_history (email, success, ip_address)
        VALUES (?, 1, ?)
    """, (email.lower(), ip_address))
    
    conn.commit()
    conn.close()
    
    return {
        "success": True,
        "student_id": student['student_id'],
        "first_name": student['first_name'],
        "last_name": student['last_name'],
        "email": student['email'],
        "institution": student['institution_name'],
        "program": student['program_name'],
        "program_id": student['program_id'],
        "session_token": session_token
    }


def logout_student(db_path: str, session_token: str) -> bool:
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE student_sessions SET is_active = 0 WHERE session_token = ?", 
                (session_token,))
    conn.commit()
    conn.close()
    return True


def validate_session(db_path: str, session_token: str) -> dict:
    if not session_token:
        return None
    
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    
    result = conn.execute("""
        SELECT ss.student_id, sa.first_name, sa.last_name, sa.email, 
               sa.institution_name, sa.program_name, sa.program_id
        FROM student_sessions ss
        JOIN student_accounts sa ON ss.student_id = sa.student_id
        WHERE ss.session_token = ? AND ss.is_active = 1 AND ss.expires_at > ?
    """, (session_token, datetime.now().isoformat())).fetchone()
    
    conn.close()
    
    if result:
        return dict(result)
    return None


def get_student_profile(db_path: str, student_id: str) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    
    student = conn.execute("""
        SELECT student_id, email, phone, first_name, last_name, date_of_birth, gender,
               institution_name, program_id, program_name, created_at, last_login
        FROM student_accounts WHERE student_id = ?
    """, (student_id,)).fetchone()
    
    conn.close()
    return dict(student) if student else None


def update_student_profile(db_path: str, student_id: str, **kwargs) -> dict:
    allowed = ['phone', 'first_name', 'last_name', 'date_of_birth', 'gender',
               'institution_name', 'program_id', 'program_name']
    
    updates = {k: v for k, v in kwargs.items() if k in allowed and v is not None}
    
    if not updates:
        return {"success": False, "error": "No valid fields to update"}
    
    conn = sqlite3.connect(db_path)
    set_clause = ", ".join([f"{k} = ?" for k in updates.keys()])
    values = list(updates.values()) + [student_id]
    
    conn.execute(f"UPDATE student_accounts SET {set_clause} WHERE student_id = ?", values)
    conn.commit()
    conn.close()
    
    return {"success": True, "message": "Profile updated"}


# =============================================================================
# FLASK ROUTES
# =============================================================================

def register_auth_routes(app, db_path: str = "ai_tutor.db"):
    """Register authentication API routes."""
    from flask import render_template
    
    @app.route('/api/auth/register', methods=['POST'])
    def api_register():
        data = request.json
        result = register_student(
            db_path,
            email=data.get('email'),
            password=data.get('password'),
            first_name=data.get('first_name'),
            last_name=data.get('last_name'),
            phone=data.get('phone'),
            institution_name=data.get('institution'),
            program_id=data.get('program_id'),
            program_name=data.get('program_name')
        )
        return jsonify(result)
    
    @app.route('/api/auth/login', methods=['POST'])
    def api_login():
        data = request.json
        result = login_student(
            db_path,
            email=data.get('email'),
            password=data.get('password'),
            ip_address=request.remote_addr,
            device_info=request.headers.get('User-Agent', '')
        )
        
        if result['success']:
            session['student_id'] = result['student_id']
            session['session_token'] = result['session_token']
            session['student_name'] = f"{result['first_name']} {result['last_name']}"
        
        return jsonify(result)
    
    @app.route('/api/auth/logout', methods=['POST'])
    def api_logout():
        token = session.get('session_token')
        if token:
            logout_student(db_path, token)
        session.clear()
        return jsonify({"success": True})
    
    @app.route('/api/auth/session')
    def api_session():
        token = session.get('session_token')
        if token:
            student = validate_session(db_path, token)
            if student:
                return jsonify({"authenticated": True, "student": student})
        return jsonify({"authenticated": False})
    
    @app.route('/api/auth/profile', methods=['GET', 'PUT'])
    def api_profile():
        student_id = session.get('student_id')
        if not student_id:
            return jsonify({"error": "Not authenticated"}), 401
        
        if request.method == 'GET':
            return jsonify(get_student_profile(db_path, student_id))
        else:
            return jsonify(update_student_profile(db_path, student_id, **request.json))
    
    print("Authentication routes registered.")


def login_required(db_path: str = "ai_tutor.db"):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            token = session.get('session_token')
            if not token or not validate_session(db_path, token):
                return redirect('/login')
            return f(*args, **kwargs)
        return decorated_function
    return decorator


if __name__ == "__main__":
    init_auth_db()
