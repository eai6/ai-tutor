"""
Learning Session Management for TEVETA AI Tutor
Handles structured learning flow: Diagnostic → Teaching → Competency Check → Certificate
"""

import sqlite3
import json
import secrets
from datetime import datetime
from typing import Dict, List, Optional

# =============================================================================
# DATABASE SCHEMA
# =============================================================================

SESSION_SCHEMA = """
-- Learning sessions with structured phases
CREATE TABLE IF NOT EXISTS learning_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT UNIQUE NOT NULL,
    student_id TEXT NOT NULL,
    module_id TEXT NOT NULL,
    module_code TEXT,
    module_name TEXT,
    
    -- Session phases: 'diagnostic', 'teaching', 'competency_check', 'completed'
    current_phase TEXT DEFAULT 'diagnostic',
    
    -- Diagnostic results (5 questions)
    diagnostic_questions TEXT,  -- JSON: stores the 5 questions
    diagnostic_answers TEXT,    -- JSON: student answers
    diagnostic_score INTEGER DEFAULT 0,
    diagnostic_total INTEGER DEFAULT 5,
    knowledge_gaps TEXT,        -- JSON: topics student got wrong
    
    -- Teaching progress
    topics_to_teach TEXT,       -- JSON: topics from gaps
    topics_taught TEXT,         -- JSON: topics covered
    
    -- Competency check results (10 questions = 5 original + 5 new)
    competency_questions TEXT,  -- JSON: all 10 questions
    competency_answers TEXT,    -- JSON: student answers
    competency_score INTEGER DEFAULT 0,
    competency_total INTEGER DEFAULT 10,
    passed INTEGER DEFAULT 0,
    
    -- Certificate
    certificate_id TEXT,
    
    -- Timestamps
    started_at TEXT DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT,
    last_activity TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Chat history for sessions
CREATE TABLE IF NOT EXISTS session_chat (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Certificates
CREATE TABLE IF NOT EXISTS certificates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    certificate_id TEXT UNIQUE NOT NULL,
    student_id TEXT NOT NULL,
    student_name TEXT,
    module_id TEXT NOT NULL,
    module_code TEXT,
    module_name TEXT,
    
    -- Scores
    diagnostic_score INTEGER,
    competency_score INTEGER,
    score_percent REAL,
    improvement_percent REAL,
    
    -- Certificate info
    competency_level TEXT,  -- 'competent', 'proficient', 'expert'
    issued_at TEXT DEFAULT CURRENT_TIMESTAMP,
    verification_code TEXT UNIQUE
);

-- Student skills inventory
CREATE TABLE IF NOT EXISTS student_skills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id TEXT NOT NULL,
    skill_name TEXT NOT NULL,
    skill_category TEXT,
    proficiency_level INTEGER DEFAULT 1,
    evidence_module TEXT,
    evidence_score REAL,
    last_demonstrated TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(student_id, skill_name)
);
"""


def init_session_db(db_path: str = "ai_tutor.db"):
    """Initialize learning session tables."""
    conn = sqlite3.connect(db_path)
    conn.executescript(SESSION_SCHEMA)
    conn.commit()
    conn.close()
    print("Learning session tables initialized.")


# =============================================================================
# SESSION MANAGEMENT
# =============================================================================

def create_session(db_path: str, student_id: str, module_id: str, 
                   module_code: str = None, module_name: str = None) -> Dict:
    """Create or resume a learning session."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    
    # Check for existing incomplete session
    existing = conn.execute("""
        SELECT session_id, current_phase, diagnostic_score, knowledge_gaps
        FROM learning_sessions
        WHERE student_id = ? AND module_id = ? AND current_phase != 'completed'
        ORDER BY started_at DESC LIMIT 1
    """, (student_id, module_id)).fetchone()
    
    if existing:
        conn.close()
        return {
            "session_id": existing['session_id'],
            "current_phase": existing['current_phase'],
            "resumed": True,
            "diagnostic_score": existing['diagnostic_score'],
            "knowledge_gaps": json.loads(existing['knowledge_gaps']) if existing['knowledge_gaps'] else []
        }
    
    # Create new session
    session_id = f"LS-{secrets.token_hex(6).upper()}"
    
    conn.execute("""
        INSERT INTO learning_sessions (session_id, student_id, module_id, module_code, module_name)
        VALUES (?, ?, ?, ?, ?)
    """, (session_id, student_id, module_id, module_code, module_name))
    
    conn.commit()
    conn.close()
    
    return {
        "session_id": session_id,
        "current_phase": "diagnostic",
        "resumed": False
    }


def get_session(db_path: str, session_id: str) -> Optional[Dict]:
    """Get session details."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    
    session = conn.execute("SELECT * FROM learning_sessions WHERE session_id = ?", (session_id,)).fetchone()
    conn.close()
    
    if not session:
        return None
    
    result = dict(session)
    # Parse JSON fields
    for field in ['diagnostic_questions', 'diagnostic_answers', 'knowledge_gaps', 
                  'topics_to_teach', 'topics_taught', 'competency_questions', 'competency_answers']:
        if result.get(field):
            try:
                result[field] = json.loads(result[field])
            except:
                pass
    return result


def save_diagnostic_results(db_path: str, session_id: str, questions: List[Dict],
                           answers: List[Dict], score: int, gaps: List[str]) -> Dict:
    """Save diagnostic assessment results and move to teaching phase."""
    conn = sqlite3.connect(db_path)
    
    conn.execute("""
        UPDATE learning_sessions SET
            diagnostic_questions = ?,
            diagnostic_answers = ?,
            diagnostic_score = ?,
            knowledge_gaps = ?,
            topics_to_teach = ?,
            current_phase = 'teaching',
            last_activity = ?
        WHERE session_id = ?
    """, (json.dumps(questions), json.dumps(answers), score, json.dumps(gaps),
          json.dumps(gaps), datetime.now().isoformat(), session_id))
    
    conn.commit()
    conn.close()
    
    return {
        "success": True,
        "score": score,
        "total": 5,
        "percent": (score / 5) * 100,
        "gaps": gaps,
        "next_phase": "teaching"
    }


def save_competency_results(db_path: str, session_id: str, questions: List[Dict],
                           answers: List[Dict], score: int) -> Dict:
    """Save competency check results, issue certificate if passed."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    
    # Get session info
    session = conn.execute("""
        SELECT student_id, module_id, module_code, module_name, diagnostic_score
        FROM learning_sessions WHERE session_id = ?
    """, (session_id,)).fetchone()
    
    score_percent = (score / 10) * 100
    passed = score_percent >= 80  # CBET standard
    
    certificate_id = None
    improvement = score_percent - ((session['diagnostic_score'] / 5) * 100)
    
    if passed:
        # Generate certificate
        certificate_id = f"CERT-{secrets.token_hex(6).upper()}"
        verification_code = secrets.token_hex(4).upper()
        
        # Determine level
        if score_percent >= 95:
            level = "expert"
        elif score_percent >= 85:
            level = "proficient"
        else:
            level = "competent"
        
        # Get student name
        student = conn.execute("""
            SELECT first_name, last_name FROM student_accounts WHERE student_id = ?
        """, (session['student_id'],)).fetchone()
        
        student_name = f"{student['first_name']} {student['last_name']}" if student else "Student"
        
        conn.execute("""
            INSERT INTO certificates 
            (certificate_id, student_id, student_name, module_id, module_code, module_name,
             diagnostic_score, competency_score, score_percent, improvement_percent,
             competency_level, verification_code)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (certificate_id, session['student_id'], student_name, session['module_id'],
              session['module_code'], session['module_name'], session['diagnostic_score'],
              score, score_percent, improvement, level, verification_code))
    
    # Update session
    conn.execute("""
        UPDATE learning_sessions SET
            competency_questions = ?,
            competency_answers = ?,
            competency_score = ?,
            passed = ?,
            certificate_id = ?,
            current_phase = 'completed',
            completed_at = ?,
            last_activity = ?
        WHERE session_id = ?
    """, (json.dumps(questions), json.dumps(answers), score, 1 if passed else 0,
          certificate_id, datetime.now().isoformat(), datetime.now().isoformat(), session_id))
    
    conn.commit()
    conn.close()
    
    return {
        "success": True,
        "score": score,
        "total": 10,
        "percent": score_percent,
        "passed": passed,
        "certificate_id": certificate_id,
        "improvement": improvement,
        "diagnostic_score": session['diagnostic_score']
    }


def save_chat_message(db_path: str, session_id: str, role: str, content: str):
    """Save chat message to session history."""
    conn = sqlite3.connect(db_path)
    conn.execute("""
        INSERT INTO session_chat (session_id, role, content) VALUES (?, ?, ?)
    """, (session_id, role, content))
    conn.execute("""
        UPDATE learning_sessions SET last_activity = ? WHERE session_id = ?
    """, (datetime.now().isoformat(), session_id))
    conn.commit()
    conn.close()


def get_chat_history(db_path: str, session_id: str) -> List[Dict]:
    """Get chat history for session."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    
    messages = conn.execute("""
        SELECT role, content FROM session_chat WHERE session_id = ? ORDER BY created_at
    """, (session_id,)).fetchall()
    
    conn.close()
    return [{"role": m['role'], "content": m['content']} for m in messages]


def mark_topic_taught(db_path: str, session_id: str, topic: str):
    """Mark a topic as taught."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    
    session = conn.execute("SELECT topics_taught FROM learning_sessions WHERE session_id = ?", 
                          (session_id,)).fetchone()
    
    topics = json.loads(session['topics_taught']) if session['topics_taught'] else []
    if topic not in topics:
        topics.append(topic)
    
    conn.execute("UPDATE learning_sessions SET topics_taught = ? WHERE session_id = ?",
                (json.dumps(topics), session_id))
    conn.commit()
    conn.close()


# =============================================================================
# CERTIFICATES
# =============================================================================

def get_student_certificates(db_path: str, student_id: str) -> List[Dict]:
    """Get all certificates for a student."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    
    certs = conn.execute("""
        SELECT * FROM certificates WHERE student_id = ? ORDER BY issued_at DESC
    """, (student_id,)).fetchall()
    
    conn.close()
    return [dict(c) for c in certs]


def verify_certificate(db_path: str, code: str) -> Dict:
    """Verify a certificate by ID or verification code."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    
    cert = conn.execute("""
        SELECT * FROM certificates WHERE certificate_id = ? OR verification_code = ?
    """, (code, code)).fetchone()
    
    conn.close()
    
    if cert:
        return {"valid": True, "certificate": dict(cert)}
    return {"valid": False, "error": "Certificate not found"}


# =============================================================================
# SKILLS
# =============================================================================

def record_skill(db_path: str, student_id: str, skill_name: str, category: str,
                 module_code: str, score: float):
    """Record or update a student skill."""
    # Determine proficiency from score
    if score >= 95:
        level = 5
    elif score >= 85:
        level = 4
    elif score >= 80:
        level = 3
    elif score >= 60:
        level = 2
    else:
        level = 1
    
    conn = sqlite3.connect(db_path)
    conn.execute("""
        INSERT INTO student_skills (student_id, skill_name, skill_category, proficiency_level,
                                    evidence_module, evidence_score)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(student_id, skill_name) DO UPDATE SET
            proficiency_level = MAX(proficiency_level, excluded.proficiency_level),
            evidence_module = excluded.evidence_module,
            evidence_score = excluded.evidence_score,
            last_demonstrated = CURRENT_TIMESTAMP
    """, (student_id, skill_name, category, level, module_code, score))
    conn.commit()
    conn.close()


def get_student_skills(db_path: str, student_id: str) -> List[Dict]:
    """Get all skills for a student."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    
    skills = conn.execute("""
        SELECT * FROM student_skills WHERE student_id = ?
        ORDER BY skill_category, proficiency_level DESC
    """, (student_id,)).fetchall()
    
    conn.close()
    return [dict(s) for s in skills]


# =============================================================================
# LEARNING SUMMARY
# =============================================================================

def get_student_learning_summary(db_path: str, student_id: str) -> Dict:
    """Get comprehensive learning summary."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    
    # Session stats
    stats = conn.execute("""
        SELECT 
            COUNT(*) as total_sessions,
            SUM(CASE WHEN current_phase = 'completed' THEN 1 ELSE 0 END) as completed,
            SUM(CASE WHEN passed = 1 THEN 1 ELSE 0 END) as passed,
            AVG(CASE WHEN competency_score > 0 THEN (competency_score * 10.0) END) as avg_score
        FROM learning_sessions WHERE student_id = ?
    """, (student_id,)).fetchone()
    
    # Recent sessions
    recent = conn.execute("""
        SELECT session_id, module_name, current_phase, diagnostic_score, competency_score,
               passed, certificate_id, started_at, completed_at
        FROM learning_sessions WHERE student_id = ?
        ORDER BY last_activity DESC LIMIT 10
    """, (student_id,)).fetchall()
    
    # Certificates count
    cert_count = conn.execute("""
        SELECT COUNT(*) as count FROM certificates WHERE student_id = ?
    """, (student_id,)).fetchone()
    
    # Skills count
    skill_count = conn.execute("""
        SELECT COUNT(*) as count FROM student_skills WHERE student_id = ?
    """, (student_id,)).fetchone()
    
    conn.close()
    
    return {
        "statistics": {
            "total_sessions": stats['total_sessions'] or 0,
            "completed_sessions": stats['completed'] or 0,
            "passed_sessions": stats['passed'] or 0,
            "avg_score": round(stats['avg_score'] or 0, 1),
            "certificates_earned": cert_count['count'] or 0,
            "skills_acquired": skill_count['count'] or 0
        },
        "recent_sessions": [dict(r) for r in recent]
    }


# =============================================================================
# FLASK ROUTES
# =============================================================================

def register_session_routes(app, db_path: str = "ai_tutor.db"):
    """Register learning session API routes."""
    from flask import request, jsonify, session
    
    @app.route('/api/learning/session', methods=['POST'])
    def api_create_session():
        student_id = session.get('student_id')
        if not student_id:
            return jsonify({"error": "Not logged in"}), 401
        
        data = request.json
        result = create_session(db_path, student_id, data.get('module_id'),
                               data.get('module_code'), data.get('module_name'))
        return jsonify(result)
    
    @app.route('/api/learning/session/<session_id>')
    def api_get_session(session_id):
        student_id = session.get('student_id')
        if not student_id:
            return jsonify({"error": "Not logged in"}), 401
        
        sess = get_session(db_path, session_id)
        if not sess:
            return jsonify({"error": "Session not found"}), 404
        if sess['student_id'] != student_id:
            return jsonify({"error": "Unauthorized"}), 403
        
        return jsonify(sess)
    
    @app.route('/api/learning/session/<session_id>/diagnostic', methods=['POST'])
    def api_save_diagnostic(session_id):
        student_id = session.get('student_id')
        if not student_id:
            return jsonify({"error": "Not logged in"}), 401
        
        data = request.json
        result = save_diagnostic_results(db_path, session_id, 
                                        data.get('questions', []),
                                        data.get('answers', []),
                                        data.get('score', 0),
                                        data.get('gaps', []))
        return jsonify(result)
    
    @app.route('/api/learning/session/<session_id>/competency', methods=['POST'])
    def api_save_competency(session_id):
        student_id = session.get('student_id')
        if not student_id:
            return jsonify({"error": "Not logged in"}), 401
        
        data = request.json
        result = save_competency_results(db_path, session_id,
                                        data.get('questions', []),
                                        data.get('answers', []),
                                        data.get('score', 0))
        return jsonify(result)
    
    @app.route('/api/learning/session/<session_id>/chat', methods=['GET', 'POST'])
    def api_session_chat(session_id):
        student_id = session.get('student_id')
        if not student_id:
            return jsonify({"error": "Not logged in"}), 401
        
        if request.method == 'POST':
            data = request.json
            save_chat_message(db_path, session_id, data.get('role'), data.get('content'))
            return jsonify({"success": True})
        else:
            history = get_chat_history(db_path, session_id)
            return jsonify({"history": history})
    
    @app.route('/api/learning/certificates')
    def api_certificates():
        student_id = session.get('student_id')
        if not student_id:
            return jsonify({"error": "Not logged in"}), 401
        
        certs = get_student_certificates(db_path, student_id)
        return jsonify({"certificates": certs})
    
    @app.route('/api/learning/certificates/verify/<code>')
    def api_verify_certificate(code):
        result = verify_certificate(db_path, code)
        return jsonify(result)
    
    @app.route('/api/learning/skills')
    def api_skills():
        student_id = session.get('student_id')
        if not student_id:
            return jsonify({"error": "Not logged in"}), 401
        
        skills = get_student_skills(db_path, student_id)
        return jsonify({"skills": skills})
    
    @app.route('/api/learning/summary')
    def api_learning_summary():
        student_id = session.get('student_id')
        if not student_id:
            return jsonify({"error": "Not logged in"}), 401
        
        summary = get_student_learning_summary(db_path, student_id)
        return jsonify(summary)
    
    print("Learning session routes registered.")


if __name__ == "__main__":
    init_session_db()
    print("Session DB initialized!")
