"""
Student Tracking and Competency Analytics System
Integrates with Career Coach for skill gap analysis and job recommendations
"""

import sqlite3
import json
from datetime import datetime, timedelta
from typing import Dict, List, Optional

# =============================================================================
# DATABASE SCHEMA FOR STUDENT TRACKING
# =============================================================================

TRACKING_SCHEMA = """
-- Student profiles
CREATE TABLE IF NOT EXISTS students (
    id TEXT PRIMARY KEY,
    name TEXT,
    email TEXT,
    phone TEXT,
    institution TEXT,
    program_id TEXT,
    enrollment_date TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Learning sessions
CREATE TABLE IF NOT EXISTS learning_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id TEXT,
    module_id TEXT,
    started_at TEXT,
    ended_at TEXT,
    duration_minutes INTEGER,
    messages_count INTEGER DEFAULT 0,
    FOREIGN KEY (student_id) REFERENCES students(id),
    FOREIGN KEY (module_id) REFERENCES modules(id)
);

-- Quiz attempts and scores
CREATE TABLE IF NOT EXISTS quiz_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id TEXT,
    module_id TEXT,
    quiz_type TEXT,  -- 'competency_check', 'practice', 'assessment'
    questions_total INTEGER,
    questions_correct INTEGER,
    score_percent REAL,
    passed INTEGER DEFAULT 0,  -- 1 if >= 80% (CBET standard)
    time_taken_seconds INTEGER,
    question_details TEXT,  -- JSON of individual question results
    attempted_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (student_id) REFERENCES students(id)
);

-- Competency achievements
CREATE TABLE IF NOT EXISTS competency_achievements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id TEXT,
    module_id TEXT,
    competency_code TEXT,
    competency_name TEXT,
    achieved INTEGER DEFAULT 0,
    attempts INTEGER DEFAULT 0,
    best_score REAL DEFAULT 0,
    achieved_at TEXT,
    FOREIGN KEY (student_id) REFERENCES students(id),
    UNIQUE(student_id, competency_code)
);

-- Skills inventory (aligned with Tabiya taxonomy)
CREATE TABLE IF NOT EXISTS student_skills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id TEXT,
    skill_name TEXT,
    skill_category TEXT,  -- 'technical', 'soft', 'safety', 'entrepreneurship'
    esco_code TEXT,  -- ESCO occupation/skill code if applicable
    proficiency_level INTEGER,  -- 1-5 scale
    evidence_source TEXT,  -- 'quiz', 'scenario', 'practical', 'instructor_verified'
    last_demonstrated TEXT,
    FOREIGN KEY (student_id) REFERENCES students(id),
    UNIQUE(student_id, skill_name)
);

-- Module progress tracking
CREATE TABLE IF NOT EXISTS module_progress (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id TEXT,
    module_id TEXT,
    status TEXT DEFAULT 'not_started',  -- 'not_started', 'in_progress', 'completed', 'mastered'
    progress_percent INTEGER DEFAULT 0,
    time_spent_minutes INTEGER DEFAULT 0,
    quizzes_attempted INTEGER DEFAULT 0,
    quizzes_passed INTEGER DEFAULT 0,
    competencies_achieved INTEGER DEFAULT 0,
    competencies_total INTEGER DEFAULT 0,
    started_at TEXT,
    completed_at TEXT,
    last_activity TEXT,
    FOREIGN KEY (student_id) REFERENCES students(id),
    UNIQUE(student_id, module_id)
);

-- Learning interactions (for AI analysis)
CREATE TABLE IF NOT EXISTS learning_interactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id TEXT,
    session_id INTEGER,
    interaction_type TEXT,  -- 'question', 'quiz_attempt', 'scenario', 'video_watched', 'diagram_viewed', 'career_inquiry'
    topic TEXT,
    details TEXT,  -- JSON with specific interaction data
    timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (student_id) REFERENCES students(id)
);

-- Skill gaps (for career coach integration)
CREATE TABLE IF NOT EXISTS skill_gaps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id TEXT,
    target_occupation TEXT,
    target_esco_code TEXT,
    gap_skill TEXT,
    gap_category TEXT,
    importance TEXT,  -- 'critical', 'important', 'nice_to_have'
    recommended_module TEXT,
    identified_at TEXT DEFAULT CURRENT_TIMESTAMP,
    addressed INTEGER DEFAULT 0,
    FOREIGN KEY (student_id) REFERENCES students(id)
);

-- Career recommendations
CREATE TABLE IF NOT EXISTS career_recommendations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id TEXT,
    occupation_title TEXT,
    esco_code TEXT,
    sector TEXT,
    strive_component TEXT,
    match_score REAL,
    zambian_employers TEXT,  -- JSON list
    salary_range TEXT,
    skills_matched TEXT,  -- JSON list
    skills_gap TEXT,  -- JSON list
    recommended_at TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (student_id) REFERENCES students(id)
);
"""

def init_tracking_db(db_path: str = "ai_tutor.db"):
    """Initialize tracking tables in database."""
    conn = sqlite3.connect(db_path)
    conn.executescript(TRACKING_SCHEMA)
    conn.commit()
    conn.close()
    print("Student tracking tables initialized.")


# =============================================================================
# STUDENT PROFILE MANAGEMENT
# =============================================================================

def create_student(db_path: str, student_id: str, name: str, email: str = None, 
                   phone: str = None, institution: str = None, program_id: str = None) -> bool:
    """Create a new student profile."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("""
            INSERT OR REPLACE INTO students (id, name, email, phone, institution, program_id, enrollment_date)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (student_id, name, email, phone, institution, program_id, datetime.now().isoformat()))
        conn.commit()
        return True
    except Exception as e:
        print(f"Error creating student: {e}")
        return False
    finally:
        conn.close()


def get_student_profile(db_path: str, student_id: str) -> Optional[Dict]:
    """Get complete student profile with progress summary."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    
    student = conn.execute("SELECT * FROM students WHERE id = ?", (student_id,)).fetchone()
    if not student:
        conn.close()
        return None
    
    # Get progress summary
    progress = conn.execute("""
        SELECT 
            COUNT(*) as modules_started,
            SUM(CASE WHEN status = 'completed' OR status = 'mastered' THEN 1 ELSE 0 END) as modules_completed,
            SUM(time_spent_minutes) as total_time_minutes,
            SUM(competencies_achieved) as total_competencies
        FROM module_progress WHERE student_id = ?
    """, (student_id,)).fetchone()
    
    # Get quiz stats
    quiz_stats = conn.execute("""
        SELECT 
            COUNT(*) as total_attempts,
            SUM(CASE WHEN passed = 1 THEN 1 ELSE 0 END) as passed,
            AVG(score_percent) as avg_score
        FROM quiz_attempts WHERE student_id = ?
    """, (student_id,)).fetchone()
    
    # Get skills count
    skills = conn.execute("""
        SELECT skill_category, COUNT(*) as count
        FROM student_skills WHERE student_id = ?
        GROUP BY skill_category
    """, (student_id,)).fetchall()
    
    conn.close()
    
    return {
        "profile": dict(student),
        "progress": {
            "modules_started": progress["modules_started"] or 0,
            "modules_completed": progress["modules_completed"] or 0,
            "total_time_hours": round((progress["total_time_minutes"] or 0) / 60, 1),
            "competencies_achieved": progress["total_competencies"] or 0
        },
        "quiz_performance": {
            "total_attempts": quiz_stats["total_attempts"] or 0,
            "passed": quiz_stats["passed"] or 0,
            "average_score": round(quiz_stats["avg_score"] or 0, 1)
        },
        "skills_by_category": {row["skill_category"]: row["count"] for row in skills}
    }


# =============================================================================
# PROGRESS TRACKING
# =============================================================================

def record_quiz_attempt(db_path: str, student_id: str, module_id: str, 
                        questions_total: int, questions_correct: int,
                        question_details: List[Dict], time_taken_seconds: int = 0,
                        quiz_type: str = "competency_check") -> Dict:
    """Record a quiz attempt and update competency status."""
    conn = sqlite3.connect(db_path)
    
    score_percent = (questions_correct / questions_total * 100) if questions_total > 0 else 0
    passed = 1 if score_percent >= 80 else 0  # CBET standard: 80% to pass
    
    # Record the attempt
    conn.execute("""
        INSERT INTO quiz_attempts (student_id, module_id, quiz_type, questions_total, 
                                   questions_correct, score_percent, passed, time_taken_seconds, question_details)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (student_id, module_id, quiz_type, questions_total, questions_correct, 
          score_percent, passed, time_taken_seconds, json.dumps(question_details)))
    
    # Update module progress
    conn.execute("""
        INSERT INTO module_progress (student_id, module_id, status, quizzes_attempted, quizzes_passed, last_activity)
        VALUES (?, ?, 'in_progress', 1, ?, ?)
        ON CONFLICT(student_id, module_id) DO UPDATE SET
            quizzes_attempted = quizzes_attempted + 1,
            quizzes_passed = quizzes_passed + excluded.quizzes_passed,
            last_activity = excluded.last_activity,
            status = CASE 
                WHEN excluded.quizzes_passed > 0 AND quizzes_passed + excluded.quizzes_passed >= 3 THEN 'mastered'
                WHEN excluded.quizzes_passed > 0 THEN 'completed'
                ELSE status 
            END
    """, (student_id, module_id, passed, datetime.now().isoformat()))
    
    conn.commit()
    conn.close()
    
    return {
        "score_percent": score_percent,
        "passed": bool(passed),
        "message": "Competency demonstrated!" if passed else "Keep practicing - you need 80% to pass"
    }


def update_learning_time(db_path: str, student_id: str, module_id: str, minutes: int):
    """Update time spent on a module."""
    conn = sqlite3.connect(db_path)
    conn.execute("""
        INSERT INTO module_progress (student_id, module_id, time_spent_minutes, last_activity)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(student_id, module_id) DO UPDATE SET
            time_spent_minutes = time_spent_minutes + excluded.time_spent_minutes,
            last_activity = excluded.last_activity
    """, (student_id, module_id, minutes, datetime.now().isoformat()))
    conn.commit()
    conn.close()


def record_skill(db_path: str, student_id: str, skill_name: str, 
                 category: str, proficiency: int, evidence: str, esco_code: str = None):
    """Record or update a student skill."""
    conn = sqlite3.connect(db_path)
    conn.execute("""
        INSERT INTO student_skills (student_id, skill_name, skill_category, esco_code, 
                                    proficiency_level, evidence_source, last_demonstrated)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(student_id, skill_name) DO UPDATE SET
            proficiency_level = MAX(proficiency_level, excluded.proficiency_level),
            evidence_source = excluded.evidence_source,
            last_demonstrated = excluded.last_demonstrated
    """, (student_id, skill_name, category, esco_code, proficiency, evidence, datetime.now().isoformat()))
    conn.commit()
    conn.close()


def record_interaction(db_path: str, student_id: str, session_id: int,
                       interaction_type: str, topic: str, details: Dict):
    """Record a learning interaction for analytics."""
    conn = sqlite3.connect(db_path)
    conn.execute("""
        INSERT INTO learning_interactions (student_id, session_id, interaction_type, topic, details)
        VALUES (?, ?, ?, ?, ?)
    """, (student_id, session_id, interaction_type, topic, json.dumps(details)))
    conn.commit()
    conn.close()


# =============================================================================
# ANALYTICS AND REPORTING
# =============================================================================

def get_student_analytics(db_path: str, student_id: str) -> Dict:
    """Get comprehensive analytics for a student."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    
    # Module progress
    modules = conn.execute("""
        SELECT mp.*, m.name as module_name, m.code as module_code, p.name as program_name
        FROM module_progress mp
        JOIN modules m ON mp.module_id = m.id
        JOIN programs p ON m.program_id = p.id
        WHERE mp.student_id = ?
        ORDER BY mp.last_activity DESC
    """, (student_id,)).fetchall()
    
    # Quiz performance over time
    quiz_history = conn.execute("""
        SELECT DATE(attempted_at) as date, AVG(score_percent) as avg_score, COUNT(*) as attempts
        FROM quiz_attempts
        WHERE student_id = ?
        GROUP BY DATE(attempted_at)
        ORDER BY date DESC
        LIMIT 30
    """, (student_id,)).fetchall()
    
    # Skills inventory
    skills = conn.execute("""
        SELECT * FROM student_skills
        WHERE student_id = ?
        ORDER BY skill_category, proficiency_level DESC
    """, (student_id,)).fetchall()
    
    # Competency achievements
    competencies = conn.execute("""
        SELECT * FROM competency_achievements
        WHERE student_id = ?
        ORDER BY achieved_at DESC
    """, (student_id,)).fetchall()
    
    # Learning patterns
    patterns = conn.execute("""
        SELECT 
            interaction_type,
            COUNT(*) as count,
            DATE(timestamp) as date
        FROM learning_interactions
        WHERE student_id = ?
        GROUP BY interaction_type, DATE(timestamp)
        ORDER BY date DESC
        LIMIT 100
    """, (student_id,)).fetchall()
    
    # Strengths (topics with high scores)
    strengths = conn.execute("""
        SELECT m.name as module_name, AVG(qa.score_percent) as avg_score
        FROM quiz_attempts qa
        JOIN modules m ON qa.module_id = m.id
        WHERE qa.student_id = ? AND qa.score_percent >= 80
        GROUP BY qa.module_id
        ORDER BY avg_score DESC
        LIMIT 5
    """, (student_id,)).fetchall()
    
    # Areas needing improvement
    weaknesses = conn.execute("""
        SELECT m.name as module_name, AVG(qa.score_percent) as avg_score, COUNT(*) as attempts
        FROM quiz_attempts qa
        JOIN modules m ON qa.module_id = m.id
        WHERE qa.student_id = ? AND qa.passed = 0
        GROUP BY qa.module_id
        HAVING attempts >= 2
        ORDER BY avg_score ASC
        LIMIT 5
    """, (student_id,)).fetchall()
    
    conn.close()
    
    return {
        "modules": [dict(m) for m in modules],
        "quiz_history": [dict(q) for q in quiz_history],
        "skills": [dict(s) for s in skills],
        "competencies": [dict(c) for c in competencies],
        "learning_patterns": [dict(p) for p in patterns],
        "strengths": [dict(s) for s in strengths],
        "areas_for_improvement": [dict(w) for w in weaknesses]
    }


def get_skill_gap_analysis(db_path: str, student_id: str, target_occupation: str) -> Dict:
    """Analyze skill gaps for a target occupation (CareerCoach integration)."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    
    # Get student's current skills
    current_skills = conn.execute("""
        SELECT skill_name, skill_category, proficiency_level
        FROM student_skills WHERE student_id = ?
    """, (student_id,)).fetchall()
    
    current_skill_names = {s["skill_name"].lower() for s in current_skills}
    
    # Define required skills by occupation (simplified - would connect to Tabiya taxonomy)
    occupation_skills = get_occupation_requirements(target_occupation)
    
    # Identify gaps
    gaps = []
    for skill in occupation_skills["required_skills"]:
        skill_name = skill["name"].lower()
        if skill_name not in current_skill_names:
            gaps.append({
                "skill": skill["name"],
                "category": skill["category"],
                "importance": skill["importance"],
                "recommended_module": skill.get("module"),
                "training_available": skill.get("training_available", True)
            })
    
    # Calculate match score
    matched = len(occupation_skills["required_skills"]) - len(gaps)
    total = len(occupation_skills["required_skills"])
    match_score = (matched / total * 100) if total > 0 else 0
    
    # Store gaps in database
    for gap in gaps:
        conn.execute("""
            INSERT OR REPLACE INTO skill_gaps 
            (student_id, target_occupation, gap_skill, gap_category, importance, recommended_module)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (student_id, target_occupation, gap["skill"], gap["category"], 
              gap["importance"], gap.get("recommended_module")))
    
    conn.commit()
    conn.close()
    
    return {
        "target_occupation": target_occupation,
        "match_score": round(match_score, 1),
        "skills_matched": matched,
        "skills_required": total,
        "gaps": gaps,
        "recommendation": get_gap_recommendation(match_score, gaps)
    }


def get_occupation_requirements(occupation: str) -> Dict:
    """Get required skills for an occupation (Tabiya taxonomy integration)."""
    # This would connect to Tabiya API/database in production
    # Simplified examples aligned with STRIVE sectors
    
    occupation_db = {
        "Electrician": {
            "sector": "Renewable Energy & Electrical",
            "strive_component": "Component 2",
            "required_skills": [
                {"name": "Electrical Safety", "category": "safety", "importance": "critical", "module": "PE-01"},
                {"name": "Ohm's Law", "category": "technical", "importance": "critical", "module": "PE-02"},
                {"name": "Circuit Analysis", "category": "technical", "importance": "critical", "module": "PE-02"},
                {"name": "Domestic Wiring", "category": "technical", "importance": "critical", "module": "PE-03"},
                {"name": "Motor Maintenance", "category": "technical", "importance": "important", "module": "PE-04"},
                {"name": "Solar PV Installation", "category": "technical", "importance": "important", "module": "PE-07"},
                {"name": "Multimeter Use", "category": "technical", "importance": "critical", "module": "PE-02"},
                {"name": "ZESCO Regulations", "category": "regulatory", "importance": "important", "module": "PE-03"},
                {"name": "Customer Service", "category": "soft", "importance": "important"},
                {"name": "Basic Business Skills", "category": "entrepreneurship", "importance": "nice_to_have", "module": "PE-08"}
            ],
            "zambian_employers": ["ZESCO", "CEC", "Solar companies", "Mining companies", "Contractors"],
            "salary_range": "K4,000 - K20,000/month"
        },
        "Chef": {
            "sector": "Tourism & Hospitality",
            "strive_component": "Component 2",
            "required_skills": [
                {"name": "Food Safety & Hygiene", "category": "safety", "importance": "critical", "module": "FP-01"},
                {"name": "HACCP Principles", "category": "regulatory", "importance": "critical", "module": "FP-01"},
                {"name": "Knife Skills", "category": "technical", "importance": "critical", "module": "FP-02"},
                {"name": "Cooking Methods", "category": "technical", "importance": "critical", "module": "FP-03"},
                {"name": "Menu Planning", "category": "technical", "importance": "important", "module": "FP-04"},
                {"name": "Kitchen Management", "category": "management", "importance": "important", "module": "FP-05"},
                {"name": "Zambian Cuisine", "category": "technical", "importance": "important"},
                {"name": "Cost Control", "category": "business", "importance": "important", "module": "FP-06"},
                {"name": "Team Leadership", "category": "soft", "importance": "important"},
                {"name": "Time Management", "category": "soft", "importance": "critical"}
            ],
            "zambian_employers": ["Radisson Blu", "Protea Hotels", "Sun International", "Safari Lodges", "Restaurants"],
            "salary_range": "K3,000 - K25,000/month"
        },
        "Automotive Technician": {
            "sector": "Manufacturing",
            "strive_component": "Component 2",
            "required_skills": [
                {"name": "Workshop Safety", "category": "safety", "importance": "critical", "module": "AM-01"},
                {"name": "Engine Systems", "category": "technical", "importance": "critical", "module": "AM-02"},
                {"name": "Brake Systems", "category": "technical", "importance": "critical", "module": "AM-03"},
                {"name": "Electrical Systems", "category": "technical", "importance": "critical", "module": "AM-04"},
                {"name": "Diagnostic Tools", "category": "technical", "importance": "critical", "module": "AM-05"},
                {"name": "Transmission Systems", "category": "technical", "importance": "important", "module": "AM-06"},
                {"name": "Air Conditioning", "category": "technical", "importance": "important"},
                {"name": "Customer Communication", "category": "soft", "importance": "important"},
                {"name": "Service Documentation", "category": "administrative", "importance": "important"},
                {"name": "Business Management", "category": "entrepreneurship", "importance": "nice_to_have", "module": "AM-08"}
            ],
            "zambian_employers": ["Toyota Zambia", "Mercedes-Benz Zambia", "CFAO Motors", "Mining companies", "Bus companies"],
            "salary_range": "K3,500 - K18,000/month"
        },
        "Welder": {
            "sector": "Mining & Manufacturing",
            "strive_component": "Component 2",
            "required_skills": [
                {"name": "Welding Safety", "category": "safety", "importance": "critical", "module": "WF-01"},
                {"name": "Arc Welding (SMAW)", "category": "technical", "importance": "critical", "module": "WF-03"},
                {"name": "MIG Welding", "category": "technical", "importance": "critical", "module": "WF-04"},
                {"name": "Blueprint Reading", "category": "technical", "importance": "critical", "module": "WF-07"},
                {"name": "Metal Fabrication", "category": "technical", "importance": "important", "module": "WF-06"},
                {"name": "TIG Welding", "category": "technical", "importance": "important", "module": "WF-05"},
                {"name": "Quality Inspection", "category": "technical", "importance": "important"},
                {"name": "Oxy-Acetylene", "category": "technical", "importance": "important", "module": "WF-02"},
                {"name": "Physical Fitness", "category": "physical", "importance": "important"},
                {"name": "Attention to Detail", "category": "soft", "importance": "critical"}
            ],
            "zambian_employers": ["KCM", "FQM", "Mopani", "Metal Fabricators", "Construction companies"],
            "salary_range": "K4,000 - K18,000/month"
        }
    }
    
    return occupation_db.get(occupation, {
        "sector": "General",
        "required_skills": [],
        "zambian_employers": [],
        "salary_range": "Varies"
    })


def get_gap_recommendation(match_score: float, gaps: List[Dict]) -> str:
    """Generate recommendation based on skill gap analysis."""
    if match_score >= 80:
        return "You're well-prepared for this role! Focus on gaining practical experience."
    elif match_score >= 60:
        critical_gaps = [g for g in gaps if g["importance"] == "critical"]
        if critical_gaps:
            modules = [g["recommended_module"] for g in critical_gaps if g.get("recommended_module")]
            return f"Good progress! Complete these critical modules first: {', '.join(modules)}"
        return "You're on track! Complete the remaining modules to strengthen your profile."
    elif match_score >= 40:
        return "You have a foundation. Enroll in a TEVETA program to build the required skills systematically."
    else:
        return "Consider starting with foundational modules. STRIVE Component 2 programs can help you build these skills."


def get_career_recommendations(db_path: str, student_id: str) -> List[Dict]:
    """Get personalized career recommendations based on student skills."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    
    # Get student's skills
    skills = conn.execute("""
        SELECT skill_name, skill_category, proficiency_level
        FROM student_skills WHERE student_id = ?
    """, (student_id,)).fetchall()
    
    # Get completed modules
    modules = conn.execute("""
        SELECT m.code, m.name, p.name as program_name
        FROM module_progress mp
        JOIN modules m ON mp.module_id = m.id
        JOIN programs p ON m.program_id = p.id
        WHERE mp.student_id = ? AND mp.status IN ('completed', 'mastered')
    """, (student_id,)).fetchall()
    
    conn.close()
    
    skill_names = {s["skill_name"].lower() for s in skills}
    module_codes = {m["code"] for m in modules}
    
    # Check against occupations
    recommendations = []
    occupations = ["Electrician", "Chef", "Automotive Technician", "Welder"]
    
    for occupation in occupations:
        reqs = get_occupation_requirements(occupation)
        if not reqs["required_skills"]:
            continue
            
        matched = sum(1 for s in reqs["required_skills"] 
                     if s["name"].lower() in skill_names or s.get("module") in module_codes)
        total = len(reqs["required_skills"])
        match_score = (matched / total * 100) if total > 0 else 0
        
        if match_score >= 30:  # Show if at least 30% match
            recommendations.append({
                "occupation": occupation,
                "sector": reqs.get("sector", ""),
                "strive_component": reqs.get("strive_component", ""),
                "match_score": round(match_score, 1),
                "skills_matched": matched,
                "skills_total": total,
                "zambian_employers": reqs.get("zambian_employers", []),
                "salary_range": reqs.get("salary_range", ""),
                "gap_count": total - matched
            })
    
    # Sort by match score
    recommendations.sort(key=lambda x: x["match_score"], reverse=True)
    
    return recommendations


# =============================================================================
# FLASK ROUTE HANDLERS
# =============================================================================

def register_tracking_routes(app, db_path: str = "ai_tutor.db"):
    """Register tracking API routes with Flask app."""
    from flask import request, jsonify, session
    
    def get_current_student_id():
        """Get student_id from session or return None."""
        return session.get('student_id')
    
    @app.route('/api/student/profile', methods=['GET', 'POST'])
    def student_profile():
        student_id = get_current_student_id()
        if not student_id:
            return jsonify({"error": "Not logged in"}), 401
        
        if request.method == 'POST':
            data = request.json
            success = create_student(
                db_path,
                student_id,
                data.get('name'),
                data.get('email'),
                data.get('phone'),
                data.get('institution'),
                data.get('program_id')
            )
            return jsonify({"success": success})
        else:
            profile = get_student_profile(db_path, student_id)
            return jsonify(profile or {"error": "Student not found"})
    
    @app.route('/api/student/analytics')
    def student_analytics():
        student_id = get_current_student_id()
        if not student_id:
            return jsonify({"error": "Not logged in"}), 401
        analytics = get_student_analytics(db_path, student_id)
        return jsonify(analytics)
    
    @app.route('/api/student/quiz', methods=['POST'])
    def record_quiz():
        student_id = get_current_student_id()
        if not student_id:
            return jsonify({"error": "Not logged in"}), 401
        
        data = request.json
        result = record_quiz_attempt(
            db_path,
            student_id,
            data.get('module_id'),
            data.get('questions_total'),
            data.get('questions_correct'),
            data.get('question_details', []),
            data.get('time_taken_seconds', 0),
            data.get('quiz_type', 'competency_check')
        )
        return jsonify(result)
    
    @app.route('/api/student/skill-gap')
    def skill_gap():
        student_id = get_current_student_id()
        if not student_id:
            return jsonify({"error": "Not logged in"}), 401
        occupation = request.args.get('occupation', 'Electrician')
        analysis = get_skill_gap_analysis(db_path, student_id, occupation)
        return jsonify(analysis)
    
    @app.route('/api/student/career-recommendations')
    def career_recs():
        student_id = get_current_student_id()
        if not student_id:
            return jsonify({"error": "Not logged in"}), 401
        recommendations = get_career_recommendations(db_path, student_id)
        return jsonify(recommendations)
    
    @app.route('/api/student/interaction', methods=['POST'])
    def log_interaction():
        student_id = get_current_student_id()
        if not student_id:
            return jsonify({"error": "Not logged in"}), 401
        
        data = request.json
        record_interaction(
            db_path,
            student_id,
            data.get('session_id', 0),
            data.get('interaction_type'),
            data.get('topic'),
            data.get('details', {})
        )
        return jsonify({"success": True})
    
    print("Student tracking routes registered.")


if __name__ == "__main__":
    # Initialize database with tracking tables
    init_tracking_db()
    print("Tracking database initialized successfully!")
