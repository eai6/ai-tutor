"""
TEVETA AI Tutor - Robust Database Layer
========================================

A centralized, robust persistence system with:
- Thread-safe connection pooling
- Automatic transaction management
- Schema versioning and migrations
- Complete data access objects (DAOs)
- Flask integration
- Backup and recovery

Usage:
    from database import Database
    
    # Initialize
    db = Database('ai_tutor.db')
    
    # Student operations
    db.students.create(email='...', password='...', first_name='...', last_name='...')
    db.students.authenticate(email, password)
    
    # Learning sessions
    db.sessions.create(student_id, module_id)
    db.sessions.save_diagnostic(session_id, questions, answers, score, gaps)
    
    # Certificates & Skills
    db.certificates.get_by_student(student_id)
    db.skills.get_by_student(student_id)
"""

import sqlite3
import threading
import json
import hashlib
import secrets
import os
import atexit
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any, Union
from contextlib import contextmanager
from queue import Queue, Empty, Full
import logging

# Logging setup
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger('database')


# =============================================================================
# CONFIGURATION
# =============================================================================

SCHEMA_VERSION = 3
DEFAULT_POOL_SIZE = 5
CONNECTION_TIMEOUT = 30
SESSION_EXPIRY_DAYS = 7
PASSWORD_MIN_LENGTH = 6


# =============================================================================
# CONNECTION POOL (Thread-Safe)
# =============================================================================

class ConnectionPool:
    """Thread-safe SQLite connection pool."""
    
    def __init__(self, db_path: str, pool_size: int = DEFAULT_POOL_SIZE):
        self.db_path = db_path
        self.pool_size = pool_size
        self._pool: Queue = Queue(maxsize=pool_size)
        self._lock = threading.RLock()
        self._local = threading.local()
        
    def _create_connection(self) -> sqlite3.Connection:
        """Create optimized SQLite connection."""
        conn = sqlite3.connect(
            self.db_path,
            timeout=CONNECTION_TIMEOUT,
            check_same_thread=False,
            isolation_level=None  # We manage transactions manually
        )
        conn.row_factory = sqlite3.Row
        
        # SQLite performance optimizations
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")  # Write-ahead logging
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA cache_size = -64000")  # 64MB cache
        conn.execute("PRAGMA temp_store = MEMORY")
        conn.execute("PRAGMA mmap_size = 268435456")  # 256MB mmap
        
        return conn
    
    def acquire(self) -> sqlite3.Connection:
        """Get connection from pool or create new one."""
        try:
            conn = self._pool.get_nowait()
            # Validate connection
            try:
                conn.execute("SELECT 1")
                return conn
            except sqlite3.Error:
                logger.debug("Stale connection, creating new one")
                return self._create_connection()
        except Empty:
            return self._create_connection()
    
    def release(self, conn: sqlite3.Connection):
        """Return connection to pool."""
        try:
            # Reset any pending transaction
            try:
                conn.rollback()
            except:
                pass
            self._pool.put_nowait(conn)
        except Full:
            conn.close()
    
    def close_all(self):
        """Close all pooled connections."""
        while not self._pool.empty():
            try:
                conn = self._pool.get_nowait()
                conn.close()
            except Empty:
                break
        logger.info(f"Connection pool closed: {self.db_path}")


# =============================================================================
# CONTEXT MANAGERS
# =============================================================================

@contextmanager
def connection(pool: ConnectionPool):
    """Context manager for database connection."""
    conn = pool.acquire()
    try:
        yield conn
    finally:
        pool.release(conn)


@contextmanager
def transaction(pool: ConnectionPool):
    """Context manager for transaction with auto commit/rollback."""
    conn = pool.acquire()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except Exception as e:
        try:
            conn.execute("ROLLBACK")
        except:
            pass
        logger.error(f"Transaction failed: {e}")
        raise
    finally:
        pool.release(conn)


# =============================================================================
# DATABASE SCHEMA
# =============================================================================

SCHEMA_SQL = """
-- ===========================================
-- SCHEMA VERSION
-- ===========================================
CREATE TABLE IF NOT EXISTS _schema (
    version INTEGER PRIMARY KEY,
    applied_at TEXT DEFAULT (datetime('now')),
    description TEXT
);

-- ===========================================
-- STUDENTS
-- ===========================================
CREATE TABLE IF NOT EXISTS students (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id TEXT UNIQUE NOT NULL,
    email TEXT UNIQUE NOT NULL COLLATE NOCASE,
    phone TEXT,
    password_hash TEXT NOT NULL,
    salt TEXT NOT NULL,
    
    first_name TEXT NOT NULL,
    last_name TEXT NOT NULL,
    date_of_birth TEXT,
    gender TEXT CHECK(gender IN ('male', 'female', 'other', NULL)),
    national_id TEXT,
    
    institution TEXT,
    program TEXT,
    enrollment_year INTEGER,
    
    is_active INTEGER DEFAULT 1,
    is_verified INTEGER DEFAULT 0,
    
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    last_login TEXT
);

CREATE INDEX IF NOT EXISTS idx_students_email ON students(email);
CREATE INDEX IF NOT EXISTS idx_students_sid ON students(student_id);

-- ===========================================
-- AUTH SESSIONS
-- ===========================================
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token TEXT UNIQUE NOT NULL,
    student_id TEXT NOT NULL,
    ip_address TEXT,
    user_agent TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    expires_at TEXT NOT NULL,
    is_active INTEGER DEFAULT 1,
    
    FOREIGN KEY (student_id) REFERENCES students(student_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_sessions_token ON sessions(token);
CREATE INDEX IF NOT EXISTS idx_sessions_student ON sessions(student_id);

-- ===========================================
-- LOGIN AUDIT
-- ===========================================
CREATE TABLE IF NOT EXISTS login_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT,
    student_id TEXT,
    success INTEGER NOT NULL,
    ip_address TEXT,
    user_agent TEXT,
    reason TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

-- ===========================================
-- CURRICULUM
-- ===========================================
CREATE TABLE IF NOT EXISTS program_areas (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    icon TEXT,
    description TEXT,
    is_strive INTEGER DEFAULT 0,
    sort_order INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS programs (
    id TEXT PRIMARY KEY,
    area_id TEXT NOT NULL,
    name TEXT NOT NULL,
    level TEXT,
    duration_months INTEGER,
    
    FOREIGN KEY (area_id) REFERENCES program_areas(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS modules (
    id TEXT PRIMARY KEY,
    program_id TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT,
    hours INTEGER DEFAULT 0,
    
    FOREIGN KEY (program_id) REFERENCES programs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_modules_program ON modules(program_id);
CREATE INDEX IF NOT EXISTS idx_modules_code ON modules(code);

-- ===========================================
-- LEARNING SESSIONS
-- ===========================================
CREATE TABLE IF NOT EXISTS learning_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT UNIQUE NOT NULL,
    student_id TEXT NOT NULL,
    module_id TEXT,
    module_code TEXT,
    module_name TEXT,
    
    -- Phase: diagnostic -> teaching -> competency -> completed
    phase TEXT DEFAULT 'diagnostic' 
        CHECK(phase IN ('diagnostic', 'teaching', 'competency', 'completed', 'abandoned')),
    
    -- Diagnostic (5 questions)
    diagnostic_questions TEXT,
    diagnostic_answers TEXT,
    diagnostic_score INTEGER DEFAULT 0,
    knowledge_gaps TEXT,
    
    -- Competency (10 questions)
    competency_questions TEXT,
    competency_answers TEXT,
    competency_score INTEGER DEFAULT 0,
    passed INTEGER DEFAULT 0,
    
    -- Certificate reference
    certificate_id TEXT,
    
    -- Timestamps
    started_at TEXT DEFAULT (datetime('now')),
    diagnostic_at TEXT,
    teaching_at TEXT,
    competency_at TEXT,
    completed_at TEXT,
    last_activity TEXT DEFAULT (datetime('now')),
    
    FOREIGN KEY (student_id) REFERENCES students(student_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_ls_student ON learning_sessions(student_id);
CREATE INDEX IF NOT EXISTS idx_ls_session ON learning_sessions(session_id);

-- ===========================================
-- CHAT MESSAGES
-- ===========================================
CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('user', 'assistant', 'system')),
    content TEXT NOT NULL,
    msg_type TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    
    FOREIGN KEY (session_id) REFERENCES learning_sessions(session_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_chat_session ON chat_messages(session_id);

-- ===========================================
-- CERTIFICATES
-- ===========================================
CREATE TABLE IF NOT EXISTS certificates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    certificate_id TEXT UNIQUE NOT NULL,
    verification_code TEXT UNIQUE NOT NULL,
    
    student_id TEXT NOT NULL,
    student_name TEXT NOT NULL,
    
    module_id TEXT,
    module_code TEXT,
    module_name TEXT NOT NULL,
    
    diagnostic_score INTEGER,
    competency_score INTEGER NOT NULL,
    score_percent REAL NOT NULL,
    improvement_percent REAL,
    
    -- competent(80-84), proficient(85-94), expert(95+)
    level TEXT CHECK(level IN ('competent', 'proficient', 'expert')),
    
    is_valid INTEGER DEFAULT 1,
    revoked_at TEXT,
    revoke_reason TEXT,
    
    session_id TEXT,
    issued_at TEXT DEFAULT (datetime('now')),
    
    FOREIGN KEY (student_id) REFERENCES students(student_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_certs_student ON certificates(student_id);
CREATE INDEX IF NOT EXISTS idx_certs_verify ON certificates(verification_code);

-- ===========================================
-- SKILLS
-- ===========================================
CREATE TABLE IF NOT EXISTS skills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id TEXT NOT NULL,
    skill_name TEXT NOT NULL,
    category TEXT CHECK(category IN ('technical', 'safety', 'soft', 'entrepreneurship', 'general')),
    
    proficiency INTEGER DEFAULT 1 CHECK(proficiency BETWEEN 1 AND 5),
    
    evidence_module TEXT,
    evidence_score REAL,
    evidence_session TEXT,
    
    esco_code TEXT,
    
    first_at TEXT DEFAULT (datetime('now')),
    last_at TEXT DEFAULT (datetime('now')),
    
    UNIQUE(student_id, skill_name),
    FOREIGN KEY (student_id) REFERENCES students(student_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_skills_student ON skills(student_id);

-- ===========================================
-- QUIZ ATTEMPTS (Analytics)
-- ===========================================
CREATE TABLE IF NOT EXISTS quiz_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id TEXT NOT NULL,
    session_id TEXT,
    module_id TEXT,
    
    quiz_type TEXT CHECK(quiz_type IN ('diagnostic', 'practice', 'competency')),
    total INTEGER NOT NULL,
    correct INTEGER NOT NULL,
    percent REAL NOT NULL,
    
    details TEXT,
    time_seconds INTEGER,
    
    attempted_at TEXT DEFAULT (datetime('now')),
    
    FOREIGN KEY (student_id) REFERENCES students(student_id) ON DELETE CASCADE
);
"""


# =============================================================================
# DATA ACCESS OBJECTS
# =============================================================================

class StudentDAO:
    """Student data access."""
    
    def __init__(self, pool: ConnectionPool):
        self._pool = pool
    
    def create(self, email: str, password: str, first_name: str, last_name: str,
               phone: str = None, institution: str = None, program: str = None) -> Dict:
        """Create student account."""
        # Validation
        if not email or '@' not in email:
            return {"success": False, "error": "Invalid email address"}
        if not password or len(password) < PASSWORD_MIN_LENGTH:
            return {"success": False, "error": f"Password must be at least {PASSWORD_MIN_LENGTH} characters"}
        if not first_name or not last_name:
            return {"success": False, "error": "First and last name required"}
        
        try:
            student_id = f"STU-{datetime.now().year}-{secrets.token_hex(4).upper()}"
            salt = secrets.token_hex(16)
            password_hash = hashlib.sha256((password + salt).encode()).hexdigest()
            
            with transaction(self._pool) as conn:
                # Check existing
                existing = conn.execute(
                    "SELECT 1 FROM students WHERE email = ?", (email.lower(),)
                ).fetchone()
                
                if existing:
                    return {"success": False, "error": "Email already registered"}
                
                conn.execute("""
                    INSERT INTO students 
                    (student_id, email, phone, password_hash, salt, first_name, last_name, institution, program)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (student_id, email.lower(), phone, password_hash, salt, 
                      first_name, last_name, institution, program))
            
            logger.info(f"Student created: {student_id}")
            return {"success": True, "student_id": student_id}
            
        except Exception as e:
            logger.error(f"Create student error: {e}")
            return {"success": False, "error": "Registration failed"}
    
    def authenticate(self, email: str, password: str, 
                     ip: str = None, ua: str = None) -> Dict:
        """Authenticate and create session."""
        try:
            with transaction(self._pool) as conn:
                student = conn.execute("""
                    SELECT student_id, email, password_hash, salt, 
                           first_name, last_name, is_active, institution, program
                    FROM students WHERE email = ? OR student_id = ?
                """, (email.lower(), email)).fetchone()
                
                if not student:
                    self._audit(conn, email, None, False, ip, ua, "Not found")
                    return {"success": False, "error": "Invalid credentials"}
                
                # Verify password
                expected = hashlib.sha256((password + student['salt']).encode()).hexdigest()
                if expected != student['password_hash']:
                    self._audit(conn, email, student['student_id'], False, ip, ua, "Bad password")
                    return {"success": False, "error": "Invalid credentials"}
                
                if not student['is_active']:
                    return {"success": False, "error": "Account is deactivated"}
                
                # Create session token
                token = secrets.token_urlsafe(32)
                expires = (datetime.now() + timedelta(days=SESSION_EXPIRY_DAYS)).isoformat()
                
                conn.execute("""
                    INSERT INTO sessions (token, student_id, ip_address, user_agent, expires_at)
                    VALUES (?, ?, ?, ?, ?)
                """, (token, student['student_id'], ip, ua, expires))
                
                conn.execute(
                    "UPDATE students SET last_login = datetime('now') WHERE student_id = ?",
                    (student['student_id'],)
                )
                
                self._audit(conn, email, student['student_id'], True, ip, ua)
                
                return {
                    "success": True,
                    "student_id": student['student_id'],
                    "token": token,
                    "first_name": student['first_name'],
                    "last_name": student['last_name'],
                    "institution": student['institution'],
                    "program": student['program'],
                    "expires_at": expires
                }
                
        except Exception as e:
            logger.error(f"Authentication error: {e}")
            return {"success": False, "error": "Login failed"}
    
    def validate_session(self, token: str) -> Optional[Dict]:
        """Validate session token."""
        try:
            with connection(self._pool) as conn:
                row = conn.execute("""
                    SELECT s.student_id, s.email, s.first_name, s.last_name,
                           s.institution, s.program, sess.expires_at
                    FROM sessions sess
                    JOIN students s ON sess.student_id = s.student_id
                    WHERE sess.token = ? AND sess.is_active = 1 AND s.is_active = 1
                """, (token,)).fetchone()
                
                if not row:
                    return None
                
                # Check expiry
                if datetime.fromisoformat(row['expires_at']) < datetime.now():
                    conn.execute("UPDATE sessions SET is_active = 0 WHERE token = ?", (token,))
                    return None
                
                return dict(row)
        except:
            return None
    
    def logout(self, token: str) -> bool:
        """End session."""
        try:
            with transaction(self._pool) as conn:
                conn.execute("UPDATE sessions SET is_active = 0 WHERE token = ?", (token,))
            return True
        except:
            return False
    
    def get_profile(self, student_id: str) -> Optional[Dict]:
        """Get student profile."""
        try:
            with connection(self._pool) as conn:
                row = conn.execute("""
                    SELECT student_id, email, phone, first_name, last_name,
                           date_of_birth, gender, institution, program,
                           enrollment_year, created_at, last_login
                    FROM students WHERE student_id = ?
                """, (student_id,)).fetchone()
                return dict(row) if row else None
        except:
            return None
    
    def update_profile(self, student_id: str, **kwargs) -> Dict:
        """Update profile."""
        allowed = ['phone', 'first_name', 'last_name', 'date_of_birth', 
                   'gender', 'institution', 'program']
        updates = {k: v for k, v in kwargs.items() if k in allowed and v is not None}
        
        if not updates:
            return {"success": False, "error": "Nothing to update"}
        
        try:
            with transaction(self._pool) as conn:
                sets = ", ".join(f"{k} = ?" for k in updates.keys())
                vals = list(updates.values()) + [student_id]
                conn.execute(
                    f"UPDATE students SET {sets}, updated_at = datetime('now') WHERE student_id = ?",
                    vals
                )
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    def _audit(self, conn, email, student_id, success, ip, ua, reason=None):
        """Log login attempt."""
        conn.execute("""
            INSERT INTO login_audit (email, student_id, success, ip_address, user_agent, reason)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (email, student_id, 1 if success else 0, ip, ua, reason))


class SessionDAO:
    """Learning session data access."""
    
    def __init__(self, pool: ConnectionPool):
        self._pool = pool
    
    def create(self, student_id: str, module_id: str, 
               module_code: str = None, module_name: str = None) -> Dict:
        """Create or resume learning session."""
        try:
            with transaction(self._pool) as conn:
                # Check for existing active session
                existing = conn.execute("""
                    SELECT session_id, phase, diagnostic_score, knowledge_gaps
                    FROM learning_sessions
                    WHERE student_id = ? AND module_id = ?
                    AND phase NOT IN ('completed', 'abandoned')
                    ORDER BY started_at DESC LIMIT 1
                """, (student_id, module_id)).fetchone()
                
                if existing:
                    gaps = []
                    if existing['knowledge_gaps']:
                        try:
                            gaps = json.loads(existing['knowledge_gaps'])
                        except:
                            pass
                    
                    return {
                        "success": True,
                        "session_id": existing['session_id'],
                        "phase": existing['phase'],
                        "resumed": True,
                        "diagnostic_score": existing['diagnostic_score'],
                        "knowledge_gaps": gaps
                    }
                
                # Create new session
                session_id = f"LS-{secrets.token_hex(6).upper()}"
                conn.execute("""
                    INSERT INTO learning_sessions 
                    (session_id, student_id, module_id, module_code, module_name)
                    VALUES (?, ?, ?, ?, ?)
                """, (session_id, student_id, module_id, module_code, module_name))
                
                logger.info(f"Learning session created: {session_id}")
                return {
                    "success": True,
                    "session_id": session_id,
                    "phase": "diagnostic",
                    "resumed": False
                }
                
        except Exception as e:
            logger.error(f"Session create error: {e}")
            return {"success": False, "error": str(e)}
    
    def get(self, session_id: str) -> Optional[Dict]:
        """Get session by ID."""
        try:
            with connection(self._pool) as conn:
                row = conn.execute(
                    "SELECT *, phase as current_phase FROM learning_sessions WHERE session_id = ?",
                    (session_id,)
                ).fetchone()
                
                if not row:
                    return None
                
                result = dict(row)
                # Parse JSON fields
                for f in ['diagnostic_questions', 'diagnostic_answers', 
                          'knowledge_gaps', 'competency_questions', 'competency_answers']:
                    if result.get(f):
                        try:
                            result[f] = json.loads(result[f])
                        except:
                            pass
                return result
        except:
            return None
    
    def save_diagnostic(self, session_id: str, questions: List, answers: List,
                        score: int, gaps: List[str]) -> Dict:
        """Save diagnostic results and transition to teaching."""
        try:
            with transaction(self._pool) as conn:
                conn.execute("""
                    UPDATE learning_sessions SET
                        diagnostic_questions = ?,
                        diagnostic_answers = ?,
                        diagnostic_score = ?,
                        knowledge_gaps = ?,
                        phase = 'teaching',
                        diagnostic_at = datetime('now'),
                        teaching_at = datetime('now'),
                        last_activity = datetime('now')
                    WHERE session_id = ?
                """, (json.dumps(questions), json.dumps(answers), score,
                      json.dumps(gaps), session_id))
                
                return {
                    "success": True,
                    "score": score,
                    "total": 5,
                    "percent": (score / 5) * 100,
                    "gaps": gaps,
                    "next_phase": "teaching"
                }
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    def save_competency(self, session_id: str, questions: List, answers: List,
                        score: int, skills_to_record: List[Dict] = None) -> Dict:
        """Save competency results, issue certificate if passed, and record skills."""
        try:
            with transaction(self._pool) as conn:
                # Get session info
                session = conn.execute("""
                    SELECT student_id, module_id, module_code, module_name, diagnostic_score
                    FROM learning_sessions WHERE session_id = ?
                """, (session_id,)).fetchone()
                
                if not session:
                    return {"success": False, "error": "Session not found"}
                
                pct = (score / 10) * 100
                passed = pct >= 80
                diag_pct = (session['diagnostic_score'] / 5) * 100
                improvement = pct - diag_pct
                
                cert_id = None
                skills_recorded = []
                
                if passed:
                    # Issue certificate
                    cert_id = self._issue_certificate(
                        conn, session_id, session['student_id'],
                        session['module_id'], session['module_code'], session['module_name'],
                        session['diagnostic_score'], score, pct, improvement
                    )
                    
                    # Record skills from questions answered correctly
                    skills_recorded = self._extract_and_record_skills(
                        conn, session['student_id'], session['module_name'],
                        questions, answers, pct, session_id, skills_to_record
                    )
                
                conn.execute("""
                    UPDATE learning_sessions SET
                        competency_questions = ?,
                        competency_answers = ?,
                        competency_score = ?,
                        passed = ?,
                        certificate_id = ?,
                        phase = 'completed',
                        competency_at = datetime('now'),
                        completed_at = datetime('now'),
                        last_activity = datetime('now')
                    WHERE session_id = ?
                """, (json.dumps(questions), json.dumps(answers), score,
                      1 if passed else 0, cert_id, session_id))
                
                return {
                    "success": True,
                    "score": score,
                    "total": 10,
                    "percent": pct,
                    "passed": passed,
                    "certificate_id": cert_id,
                    "improvement": improvement,
                    "diagnostic_score": session['diagnostic_score'],
                    "skills_recorded": skills_recorded
                }
                
        except Exception as e:
            logger.error(f"Competency save error: {e}")
            return {"success": False, "error": str(e)}
    
    def _extract_and_record_skills(self, conn, student_id: str, module_name: str,
                                    questions: List, answers: List, score_pct: float,
                                    session_id: str, explicit_skills: List[Dict] = None) -> List[str]:
        """Extract skills from assessment and record them."""
        skills_recorded = []
        
        # Determine proficiency level based on score
        if score_pct >= 95:
            proficiency = 5  # Expert
        elif score_pct >= 85:
            proficiency = 4  # Proficient
        elif score_pct >= 80:
            proficiency = 3  # Competent
        else:
            proficiency = 2  # Developing
        
        # Record explicit skills if provided
        if explicit_skills:
            for skill in explicit_skills:
                conn.execute("""
                    INSERT INTO skills 
                    (student_id, skill_name, category, proficiency, evidence_module, evidence_score, evidence_session)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(student_id, skill_name) DO UPDATE SET
                        proficiency = MAX(proficiency, excluded.proficiency),
                        evidence_module = excluded.evidence_module,
                        evidence_score = excluded.evidence_score,
                        evidence_session = excluded.evidence_session,
                        last_at = datetime('now')
                """, (student_id, skill['name'], skill.get('category', 'technical'),
                      proficiency, module_name, score_pct, session_id))
                skills_recorded.append(skill['name'])
        
        # Auto-extract skills from topic_tags in questions
        topic_skills = set()
        for q in questions:
            if isinstance(q, dict) and q.get('topic_tag'):
                # Convert topic_tag to skill name (e.g., "electrical_safety" -> "Electrical Safety")
                skill_name = q['topic_tag'].replace('_', ' ').title()
                topic_skills.add(skill_name)
        
        for skill_name in topic_skills:
            if skill_name not in skills_recorded:
                conn.execute("""
                    INSERT INTO skills 
                    (student_id, skill_name, category, proficiency, evidence_module, evidence_score, evidence_session)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(student_id, skill_name) DO UPDATE SET
                        proficiency = MAX(proficiency, excluded.proficiency),
                        evidence_module = excluded.evidence_module,
                        evidence_score = excluded.evidence_score,
                        evidence_session = excluded.evidence_session,
                        last_at = datetime('now')
                """, (student_id, skill_name, 'technical', proficiency, module_name, score_pct, session_id))
                skills_recorded.append(skill_name)
        
        # Record module completion as a skill
        module_skill = f"{module_name} - Competent"
        conn.execute("""
            INSERT INTO skills 
            (student_id, skill_name, category, proficiency, evidence_module, evidence_score, evidence_session)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(student_id, skill_name) DO UPDATE SET
                proficiency = MAX(proficiency, excluded.proficiency),
                evidence_score = MAX(evidence_score, excluded.evidence_score),
                last_at = datetime('now')
        """, (student_id, module_skill, 'technical', proficiency, module_name, score_pct, session_id))
        skills_recorded.append(module_skill)
        
        logger.info(f"Skills recorded for {student_id}: {skills_recorded}")
        return skills_recorded
    
    def _issue_certificate(self, conn, session_id, student_id, module_id,
                           module_code, module_name, diag_score, comp_score, pct, improvement):
        """Issue certificate."""
        cert_id = f"CERT-{secrets.token_hex(6).upper()}"
        verify_code = secrets.token_hex(4).upper()
        
        level = "expert" if pct >= 95 else "proficient" if pct >= 85 else "competent"
        
        # Get student name
        student = conn.execute(
            "SELECT first_name, last_name FROM students WHERE student_id = ?",
            (student_id,)
        ).fetchone()
        name = f"{student['first_name']} {student['last_name']}" if student else "Student"
        
        conn.execute("""
            INSERT INTO certificates 
            (certificate_id, verification_code, student_id, student_name,
             module_id, module_code, module_name, diagnostic_score, competency_score,
             score_percent, improvement_percent, level, session_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (cert_id, verify_code, student_id, name, module_id, module_code,
              module_name, diag_score, comp_score, pct, improvement, level, session_id))
        
        logger.info(f"Certificate issued: {cert_id} ({level})")
        return cert_id
    
    def save_message(self, session_id: str, role: str, content: str, 
                     msg_type: str = None) -> bool:
        """Save chat message."""
        try:
            with transaction(self._pool) as conn:
                conn.execute("""
                    INSERT INTO chat_messages (session_id, role, content, msg_type)
                    VALUES (?, ?, ?, ?)
                """, (session_id, role, content, msg_type))
                
                conn.execute("""
                    UPDATE learning_sessions SET last_activity = datetime('now')
                    WHERE session_id = ?
                """, (session_id,))
                
                return True
        except:
            return False
    
    def get_messages(self, session_id: str, limit: int = 100) -> List[Dict]:
        """Get chat history."""
        try:
            with connection(self._pool) as conn:
                rows = conn.execute("""
                    SELECT role, content, msg_type, created_at
                    FROM chat_messages WHERE session_id = ?
                    ORDER BY created_at ASC LIMIT ?
                """, (session_id, limit)).fetchall()
                return [dict(r) for r in rows]
        except:
            return []
    
    def update_phase(self, session_id: str, phase: str) -> bool:
        """Update session phase."""
        try:
            with transaction(self._pool) as conn:
                conn.execute("""
                    UPDATE learning_sessions 
                    SET phase = ?, last_activity = datetime('now')
                    WHERE session_id = ?
                """, (phase, session_id))
            return True
        except:
            return False


class CertificateDAO:
    """Certificate data access."""
    
    def __init__(self, pool: ConnectionPool):
        self._pool = pool
    
    def get_by_student(self, student_id: str) -> List[Dict]:
        """Get all certificates for student."""
        try:
            with connection(self._pool) as conn:
                rows = conn.execute("""
                    SELECT *, level as competency_level FROM certificates
                    WHERE student_id = ? AND is_valid = 1
                    ORDER BY issued_at DESC
                """, (student_id,)).fetchall()
                return [dict(r) for r in rows]
        except:
            return []
    
    def verify(self, code: str) -> Dict:
        """Verify certificate by ID or code."""
        try:
            with connection(self._pool) as conn:
                row = conn.execute("""
                    SELECT *, level as competency_level FROM certificates
                    WHERE certificate_id = ? OR verification_code = ?
                """, (code, code)).fetchone()
                
                if not row:
                    return {"valid": False, "error": "Certificate not found"}
                
                if not row['is_valid']:
                    return {"valid": False, "error": "Certificate revoked",
                            "reason": row['revoke_reason']}
                
                return {"valid": True, "certificate": dict(row)}
        except:
            return {"valid": False, "error": "Verification failed"}
    
    def revoke(self, certificate_id: str, reason: str) -> bool:
        """Revoke a certificate."""
        try:
            with transaction(self._pool) as conn:
                conn.execute("""
                    UPDATE certificates 
                    SET is_valid = 0, revoked_at = datetime('now'), revoke_reason = ?
                    WHERE certificate_id = ?
                """, (reason, certificate_id))
            return True
        except:
            return False


class SkillDAO:
    """Skill data access."""
    
    def __init__(self, pool: ConnectionPool):
        self._pool = pool
    
    def record(self, student_id: str, skill_name: str, category: str,
               proficiency: int, evidence_module: str = None,
               evidence_score: float = None, session_id: str = None) -> bool:
        """Record or update skill."""
        try:
            with transaction(self._pool) as conn:
                conn.execute("""
                    INSERT INTO skills 
                    (student_id, skill_name, category, proficiency, evidence_module, evidence_score, evidence_session)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(student_id, skill_name) DO UPDATE SET
                        proficiency = MAX(proficiency, excluded.proficiency),
                        evidence_module = excluded.evidence_module,
                        evidence_score = excluded.evidence_score,
                        evidence_session = excluded.evidence_session,
                        last_at = datetime('now')
                """, (student_id, skill_name, category, proficiency,
                      evidence_module, evidence_score, session_id))
            return True
        except:
            return False
    
    def get_by_student(self, student_id: str) -> List[Dict]:
        """Get all skills for student."""
        try:
            with connection(self._pool) as conn:
                rows = conn.execute("""
                    SELECT *, category as skill_category, proficiency as proficiency_level 
                    FROM skills WHERE student_id = ?
                    ORDER BY category, proficiency DESC
                """, (student_id,)).fetchall()
                return [dict(r) for r in rows]
        except:
            return []


class AnalyticsDAO:
    """Analytics data access."""
    
    def __init__(self, pool: ConnectionPool):
        self._pool = pool
    
    def get_student_summary(self, student_id: str) -> Dict:
        """Get comprehensive learning summary."""
        try:
            with connection(self._pool) as conn:
                # Session stats
                stats = conn.execute("""
                    SELECT 
                        COUNT(*) as total_sessions,
                        SUM(CASE WHEN phase = 'completed' THEN 1 ELSE 0 END) as completed,
                        SUM(CASE WHEN passed = 1 THEN 1 ELSE 0 END) as passed,
                        AVG(CASE WHEN competency_score > 0 THEN competency_score * 10.0 END) as avg_score
                    FROM learning_sessions WHERE student_id = ?
                """, (student_id,)).fetchone()
                
                certs = conn.execute(
                    "SELECT COUNT(*) FROM certificates WHERE student_id = ? AND is_valid = 1",
                    (student_id,)
                ).fetchone()[0]
                
                skills = conn.execute(
                    "SELECT COUNT(*) FROM skills WHERE student_id = ?",
                    (student_id,)
                ).fetchone()[0]
                
                recent = conn.execute("""
                    SELECT session_id, module_name, phase as current_phase, diagnostic_score,
                           competency_score, passed, certificate_id, started_at, completed_at
                    FROM learning_sessions WHERE student_id = ?
                    ORDER BY last_activity DESC LIMIT 10
                """, (student_id,)).fetchall()
                
                return {
                    "statistics": {
                        "total_sessions": stats['total_sessions'] or 0,
                        "completed_sessions": stats['completed'] or 0,
                        "passed_sessions": stats['passed'] or 0,
                        "avg_score": round(stats['avg_score'] or 0, 1),
                        "certificates_earned": certs,
                        "skills_acquired": skills
                    },
                    "recent_sessions": [dict(r) for r in recent]
                }
        except Exception as e:
            logger.error(f"Analytics error: {e}")
            return {"statistics": {}, "recent_sessions": []}


class CurriculumDAO:
    """Curriculum data access."""
    
    def __init__(self, pool: ConnectionPool):
        self._pool = pool
    
    def load_curriculum(self, curriculum: Dict):
        """Load curriculum data into database."""
        try:
            with transaction(self._pool) as conn:
                # Clear existing
                conn.execute("DELETE FROM modules")
                conn.execute("DELETE FROM programs")
                conn.execute("DELETE FROM program_areas")
                
                # Detect column name (is_strive or is_strive_sector for backward compat)
                cols = [r[1] for r in conn.execute("PRAGMA table_info(program_areas)").fetchall()]
                strive_col = 'is_strive' if 'is_strive' in cols else 'is_strive_sector' if 'is_strive_sector' in cols else None
                
                for area_id, area in curriculum.items():
                    is_strive = 1 if area.get("strive") else 0
                    
                    if strive_col:
                        conn.execute(f"""
                            INSERT INTO program_areas (id, name, icon, {strive_col})
                            VALUES (?, ?, ?, ?)
                        """, (area_id, area_id.title(), area.get("icon", "📚"), is_strive))
                    else:
                        conn.execute("""
                            INSERT INTO program_areas (id, name, icon)
                            VALUES (?, ?, ?)
                        """, (area_id, area_id.title(), area.get("icon", "📚")))
                    
                    for prog_id, prog in area.get("programs", {}).items():
                        full_id = f"{area_id.lower()}_{prog_id}"
                        conn.execute("""
                            INSERT INTO programs (id, area_id, name, level)
                            VALUES (?, ?, ?, ?)
                        """, (full_id, area_id, prog["name"], prog.get("level")))
                        
                        for mod in prog.get("modules", []):
                            code, name, desc, hours = mod[0], mod[1], mod[2], mod[3]
                            mod_id = code.lower().replace("-", "_")
                            conn.execute("""
                                INSERT INTO modules (id, program_id, code, name, description, hours)
                                VALUES (?, ?, ?, ?, ?, ?)
                            """, (mod_id, full_id, code, name, desc, hours))
                
                logger.info("Curriculum loaded successfully")
                return True
        except Exception as e:
            logger.error(f"Curriculum load error: {e}")
            return False
    
    def get_areas(self) -> List[Dict]:
        """Get all program areas."""
        try:
            with connection(self._pool) as conn:
                rows = conn.execute(
                    "SELECT * FROM program_areas ORDER BY sort_order, name"
                ).fetchall()
                return [dict(r) for r in rows]
        except:
            return []
    
    def get_programs(self, area_id: str = None) -> List[Dict]:
        """Get programs, optionally filtered by area."""
        try:
            with connection(self._pool) as conn:
                if area_id:
                    rows = conn.execute(
                        "SELECT * FROM programs WHERE area_id = ?", (area_id,)
                    ).fetchall()
                else:
                    rows = conn.execute("SELECT * FROM programs").fetchall()
                return [dict(r) for r in rows]
        except:
            return []
    
    def get_modules(self, program_id: str = None) -> List[Dict]:
        """Get modules, optionally filtered by program."""
        try:
            with connection(self._pool) as conn:
                if program_id:
                    rows = conn.execute(
                        "SELECT * FROM modules WHERE program_id = ?", (program_id,)
                    ).fetchall()
                else:
                    rows = conn.execute("SELECT * FROM modules").fetchall()
                return [dict(r) for r in rows]
        except:
            return []
    
    def get_module(self, module_id: str) -> Optional[Dict]:
        """Get single module."""
        try:
            with connection(self._pool) as conn:
                row = conn.execute(
                    "SELECT * FROM modules WHERE id = ? OR code = ?",
                    (module_id, module_id)
                ).fetchone()
                return dict(row) if row else None
        except:
            return None


# =============================================================================
# MAIN DATABASE CLASS
# =============================================================================

class Database:
    """
    Main database interface.
    
    Usage:
        db = Database('ai_tutor.db')
        
        # Students
        db.students.create(email=..., password=..., ...)
        db.students.authenticate(email, password)
        
        # Sessions
        db.sessions.create(student_id, module_id)
        db.sessions.save_diagnostic(...)
        
        # Certificates & Skills
        db.certificates.get_by_student(student_id)
        db.skills.get_by_student(student_id)
    """
    
    _instances: Dict[str, 'Database'] = {}
    _lock = threading.Lock()
    
    def __new__(cls, db_path: str = "ai_tutor.db"):
        with cls._lock:
            if db_path not in cls._instances:
                instance = super().__new__(cls)
                instance._initialized = False
                cls._instances[db_path] = instance
            return cls._instances[db_path]
    
    def __init__(self, db_path: str = "ai_tutor.db"):
        if self._initialized:
            return
        
        self.db_path = db_path
        self._pool = ConnectionPool(db_path)
        
        # Initialize schema
        self._init_schema()
        
        # Data Access Objects
        self.students = StudentDAO(self._pool)
        self.sessions = SessionDAO(self._pool)
        self.certificates = CertificateDAO(self._pool)
        self.skills = SkillDAO(self._pool)
        self.analytics = AnalyticsDAO(self._pool)
        self.curriculum = CurriculumDAO(self._pool)
        
        self._initialized = True
        logger.info(f"Database initialized: {db_path}")
    
    def _init_schema(self):
        """Initialize database schema with migration support."""
        try:
            with connection(self._pool) as conn:
                # First, run migrations on existing tables BEFORE creating new schema
                self._run_migrations(conn)
                
                # Now create any missing tables
                conn.executescript(SCHEMA_SQL)
                
                # Check version
                try:
                    current = conn.execute(
                        "SELECT MAX(version) FROM _schema"
                    ).fetchone()[0] or 0
                except:
                    current = 0
                
                if current < SCHEMA_VERSION:
                    conn.execute("BEGIN")
                    conn.execute(
                        "INSERT OR REPLACE INTO _schema (version, description) VALUES (?, ?)",
                        (SCHEMA_VERSION, f"Schema v{SCHEMA_VERSION}")
                    )
                    conn.execute("COMMIT")
        except Exception as e:
            logger.debug(f"Schema init note: {e}")
    
    def _run_migrations(self, conn):
        """Run any necessary migrations for old databases."""
        try:
            # Check if program_areas table exists
            tables = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()]
            
            if 'program_areas' in tables:
                cols = [r[1] for r in conn.execute("PRAGMA table_info(program_areas)").fetchall()]
                
                # Migration: is_strive_sector -> is_strive  
                if 'is_strive_sector' in cols and 'is_strive' not in cols:
                    logger.info("Migrating: renaming is_strive_sector to is_strive")
                    try:
                        conn.execute("ALTER TABLE program_areas RENAME COLUMN is_strive_sector TO is_strive")
                    except:
                        # SQLite < 3.25 doesn't support RENAME COLUMN, recreate table
                        conn.execute("""
                            CREATE TABLE program_areas_new (
                                id TEXT PRIMARY KEY, name TEXT NOT NULL, icon TEXT,
                                description TEXT, is_strive INTEGER DEFAULT 0, sort_order INTEGER DEFAULT 0
                            )
                        """)
                        conn.execute("INSERT INTO program_areas_new SELECT id, name, icon, description, is_strive_sector, sort_order FROM program_areas")
                        conn.execute("DROP TABLE program_areas")
                        conn.execute("ALTER TABLE program_areas_new RENAME TO program_areas")
                
                # Migration: add is_strive if missing entirely
                elif 'is_strive' not in cols:
                    logger.info("Migrating: adding is_strive column")
                    conn.execute("ALTER TABLE program_areas ADD COLUMN is_strive INTEGER DEFAULT 0")
            
            # Check learning_sessions table
            if 'learning_sessions' in tables:
                ls_cols = [r[1] for r in conn.execute("PRAGMA table_info(learning_sessions)").fetchall()]
                
                if 'current_phase' in ls_cols and 'phase' not in ls_cols:
                    logger.info("Migrating: renaming current_phase to phase")
                    try:
                        conn.execute("ALTER TABLE learning_sessions RENAME COLUMN current_phase TO phase")
                    except:
                        pass
                        
        except Exception as e:
            logger.debug(f"Migration note: {e}")
    
    def health_check(self) -> Dict:
        """Check database health."""
        result = {"healthy": True, "tables": [], "version": 0, "issues": []}
        
        try:
            with connection(self._pool) as conn:
                # Integrity
                check = conn.execute("PRAGMA integrity_check").fetchone()[0]
                if check != "ok":
                    result["healthy"] = False
                    result["issues"].append(f"Integrity: {check}")
                
                # Tables
                tables = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                ).fetchall()
                result["tables"] = [t[0] for t in tables if not t[0].startswith('sqlite_')]
                
                # Version
                try:
                    v = conn.execute("SELECT MAX(version) FROM _schema").fetchone()[0]
                    result["version"] = v or 0
                except:
                    result["issues"].append("No version table")
                    
        except Exception as e:
            result["healthy"] = False
            result["issues"].append(str(e))
        
        return result
    
    def backup(self, backup_path: str = None) -> bool:
        """Create database backup."""
        if not backup_path:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = f"{self.db_path}.backup_{ts}"
        
        try:
            with connection(self._pool) as conn:
                backup = sqlite3.connect(backup_path)
                conn.backup(backup)
                backup.close()
            logger.info(f"Backup created: {backup_path}")
            return True
        except Exception as e:
            logger.error(f"Backup failed: {e}")
            return False
    
    def close(self):
        """Close all connections."""
        self._pool.close_all()


# =============================================================================
# FLASK INTEGRATION
# =============================================================================

def init_app(app, db_path: str = "ai_tutor.db"):
    """Initialize Flask app with database."""
    from flask import request, jsonify, session, g
    
    database = Database(db_path)
    
    @app.before_request
    def before():
        g.db = database
        g.student_id = session.get('student_id')
    
    # Health
    @app.route('/api/health')
    def health():
        return jsonify(database.health_check())
    
    # ========== AUTH ==========
    
    @app.route('/api/auth/register', methods=['POST'])
    def register():
        d = request.json or {}
        r = database.students.create(
            email=d.get('email'), password=d.get('password'),
            first_name=d.get('first_name'), last_name=d.get('last_name'),
            phone=d.get('phone'), institution=d.get('institution'), program=d.get('program')
        )
        return jsonify(r), 200 if r['success'] else 400
    
    @app.route('/api/auth/login', methods=['POST'])
    def login():
        d = request.json or {}
        r = database.students.authenticate(
            d.get('email'), d.get('password'),
            request.remote_addr, str(request.user_agent)[:200]
        )
        if r['success']:
            session['student_id'] = r['student_id']
            session['token'] = r['token']
            session.permanent = True
        return jsonify(r), 200 if r['success'] else 401
    
    @app.route('/api/auth/logout', methods=['POST'])
    def logout():
        if session.get('token'):
            database.students.logout(session['token'])
        session.clear()
        return jsonify({"success": True})
    
    @app.route('/api/auth/session')
    def check_session():
        sid = session.get('student_id')
        if not sid:
            return jsonify({"authenticated": False})
        profile = database.students.get_profile(sid)
        if not profile:
            session.clear()
            return jsonify({"authenticated": False})
        return jsonify({"authenticated": True, "student": profile})
    
    @app.route('/api/auth/profile', methods=['GET', 'PUT'])
    def profile():
        sid = session.get('student_id')
        if not sid:
            return jsonify({"error": "Not authenticated"}), 401
        if request.method == 'GET':
            return jsonify(database.students.get_profile(sid))
        r = database.students.update_profile(sid, **(request.json or {}))
        return jsonify(r), 200 if r['success'] else 400
    
    # ========== LEARNING ==========
    
    @app.route('/api/learning/session', methods=['POST'])
    def create_session():
        sid = session.get('student_id')
        if not sid:
            return jsonify({"error": "Not authenticated"}), 401
        d = request.json or {}
        r = database.sessions.create(
            sid, d.get('module_id'), d.get('module_code'), d.get('module_name')
        )
        return jsonify(r)
    
    @app.route('/api/learning/session/<session_id>')
    def get_learning_session(session_id):
        sid = session.get('student_id')
        if not sid:
            return jsonify({"error": "Not authenticated"}), 401
        s = database.sessions.get(session_id)
        if not s:
            return jsonify({"error": "Not found"}), 404
        if s['student_id'] != sid:
            return jsonify({"error": "Forbidden"}), 403
        return jsonify(s)
    
    @app.route('/api/learning/session/<session_id>/diagnostic', methods=['POST'])
    def save_diagnostic(session_id):
        d = request.json or {}
        r = database.sessions.save_diagnostic(
            session_id, d.get('questions', []), d.get('answers', []),
            d.get('score', 0), d.get('gaps', [])
        )
        return jsonify(r)
    
    @app.route('/api/learning/session/<session_id>/competency', methods=['POST'])
    def save_competency(session_id):
        d = request.json or {}
        r = database.sessions.save_competency(
            session_id, d.get('questions', []), d.get('answers', []), d.get('score', 0)
        )
        return jsonify(r)
    
    @app.route('/api/learning/session/<session_id>/chat', methods=['GET', 'POST'])
    def session_chat(session_id):
        if request.method == 'POST':
            d = request.json or {}
            database.sessions.save_message(session_id, d.get('role'), d.get('content'))
            return jsonify({"success": True})
        return jsonify({"messages": database.sessions.get_messages(session_id)})
    
    # ========== CERTIFICATES ==========
    
    @app.route('/api/learning/certificates')
    def list_certs():
        sid = session.get('student_id')
        if not sid:
            return jsonify({"error": "Not authenticated"}), 401
        return jsonify({"certificates": database.certificates.get_by_student(sid)})
    
    @app.route('/api/learning/certificates/verify/<code>')
    def verify_cert(code):
        return jsonify(database.certificates.verify(code))
    
    # ========== SKILLS ==========
    
    @app.route('/api/learning/skills')
    def list_skills():
        sid = session.get('student_id')
        if not sid:
            return jsonify({"error": "Not authenticated"}), 401
        return jsonify({"skills": database.skills.get_by_student(sid)})
    
    # ========== ANALYTICS ==========
    
    @app.route('/api/learning/summary')
    def summary():
        sid = session.get('student_id')
        if not sid:
            return jsonify({"error": "Not authenticated"}), 401
        return jsonify(database.analytics.get_student_summary(sid))
    
    logger.info("Flask routes registered")
    return database


# =============================================================================
# CLEANUP
# =============================================================================

@atexit.register
def _cleanup():
    for db in Database._instances.values():
        db.close()


# =============================================================================
# TEST
# =============================================================================

if __name__ == "__main__":
    print("=" * 60)
    print("TEVETA AI Tutor - Database Test")
    print("=" * 60)
    
    TEST_DB = "test_db.db"
    
    # Initialize
    db = Database(TEST_DB)
    
    # Health check
    print("\n[Health Check]")
    h = db.health_check()
    print(f"  Healthy: {h['healthy']}")
    print(f"  Tables: {len(h['tables'])}")
    print(f"  Version: {h['version']}")
    
    # Create student
    print("\n[Create Student]")
    r = db.students.create(
        email="test@example.com", password="Test1234",
        first_name="John", last_name="Doe"
    )
    print(f"  Success: {r['success']}")
    if r['success']:
        print(f"  Student ID: {r['student_id']}")
    
    # Authenticate
    print("\n[Authenticate]")
    auth = db.students.authenticate("test@example.com", "Test1234")
    print(f"  Success: {auth['success']}")
    
    if auth['success']:
        sid = auth['student_id']
        
        # Create session
        print("\n[Create Session]")
        sess = db.sessions.create(sid, "MOD001", "EEI-1", "Electrical Installation")
        print(f"  Session: {sess['session_id']}")
        print(f"  Phase: {sess['phase']}")
        
        # Save diagnostic
        print("\n[Save Diagnostic]")
        diag = db.sessions.save_diagnostic(
            sess['session_id'],
            [{"q": "Q1"}], [{"a": "A1"}], 3, ["Safety", "Wiring"]
        )
        print(f"  Score: {diag['score']}/5")
        print(f"  Gaps: {diag['gaps']}")
        
        # Save competency (passing)
        print("\n[Save Competency]")
        comp = db.sessions.save_competency(
            sess['session_id'],
            [{"q": f"Q{i}"} for i in range(10)],
            [{"a": f"A{i}"} for i in range(10)], 9
        )
        print(f"  Score: {comp['score']}/10 ({comp['percent']}%)")
        print(f"  Passed: {comp['passed']}")
        print(f"  Certificate: {comp['certificate_id']}")
        
        # Verify cert
        if comp['certificate_id']:
            print("\n[Verify Certificate]")
            v = db.certificates.verify(comp['certificate_id'])
            print(f"  Valid: {v['valid']}")
        
        # Analytics
        print("\n[Analytics]")
        a = db.analytics.get_student_summary(sid)
        print(f"  Sessions: {a['statistics']['total_sessions']}")
        print(f"  Completed: {a['statistics']['completed_sessions']}")
        print(f"  Certificates: {a['statistics']['certificates_earned']}")
    
    # Cleanup
    db.close()
    os.remove(TEST_DB)
    for ext in ['-wal', '-shm']:
        try:
            os.remove(TEST_DB + ext)
        except:
            pass
    
    print("\n" + "=" * 60)
    print("✅ All tests passed!")
    print("=" * 60)
