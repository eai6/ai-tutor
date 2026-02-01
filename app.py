"""
AI Tutor TEVETA - Agentic System with Claude Tools
"""

import os
import json
import sqlite3
import random
from datetime import datetime
from flask import Flask, render_template, request, jsonify
from flask_cors import CORS
from dotenv import load_dotenv
import anthropic

load_dotenv()

app = Flask(__name__)
CORS(app)

client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
MODEL = os.getenv("CLAUDE_MODEL", "claude-3-5-sonnet-20241022")
DB_PATH = "ai_tutor.db"

# =============================================================================
# TOOL DEFINITIONS
# =============================================================================

TOOLS = [
    {
        "name": "generate_quiz",
        "description": "Generate quiz questions to test student knowledge. Use when student wants to practice.",
        "input_schema": {
            "type": "object",
            "properties": {
                "topic": {"type": "string", "description": "Topic for questions"},
                "num_questions": {"type": "integer", "description": "Number of questions (1-5)", "default": 3},
                "difficulty": {"type": "string", "enum": ["easy", "medium", "hard"]},
                "question_type": {"type": "string", "enum": ["multiple_choice", "short_answer", "true_false"]}
            },
            "required": ["topic"]
        }
    },
    {
        "name": "check_answer",
        "description": "Evaluate a student's answer and provide feedback.",
        "input_schema": {
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "student_answer": {"type": "string"},
                "correct_answer": {"type": "string"},
                "module_context": {"type": "string"}
            },
            "required": ["question", "student_answer", "correct_answer"]
        }
    },
    {
        "name": "get_practical_example",
        "description": "Get a real-world Zambian workplace example to illustrate a concept.",
        "input_schema": {
            "type": "object",
            "properties": {
                "concept": {"type": "string", "description": "Concept to illustrate"},
                "industry": {"type": "string", "description": "Industry context (mining, hospitality, etc.)"}
            },
            "required": ["concept"]
        }
    },
    {
        "name": "track_progress",
        "description": "Record and retrieve student progress on competencies.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["record", "get_summary", "get_recommendations"]},
                "competency": {"type": "string"},
                "score": {"type": "integer"},
                "session_id": {"type": "string"},
                "module_id": {"type": "string"}
            },
            "required": ["action"]
        }
    },
    {
        "name": "get_career_pathway",
        "description": "Show career opportunities and employers for a program.",
        "input_schema": {
            "type": "object",
            "properties": {
                "program": {"type": "string", "description": "Program name"},
                "skills": {"type": "array", "items": {"type": "string"}}
            },
            "required": ["program"]
        }
    },
    {
        "name": "generate_scenario",
        "description": "Create a realistic workplace scenario for hands-on practice.",
        "input_schema": {
            "type": "object",
            "properties": {
                "topic": {"type": "string"},
                "workplace": {"type": "string", "description": "E.g., garage, hotel, mine"},
                "complexity": {"type": "string", "enum": ["simple", "moderate", "complex"]}
            },
            "required": ["topic"]
        }
    }
]

# =============================================================================
# TOOL IMPLEMENTATIONS
# =============================================================================

def execute_tool(tool_name: str, tool_input: dict, context: dict) -> str:
    """Execute a tool and return result."""
    
    if tool_name == "generate_quiz":
        return generate_quiz(tool_input, context)
    elif tool_name == "check_answer":
        return check_answer(tool_input)
    elif tool_name == "get_practical_example":
        return get_practical_example(tool_input, context)
    elif tool_name == "track_progress":
        return track_progress(tool_input, context)
    elif tool_name == "get_career_pathway":
        return get_career_pathway(tool_input, context)
    elif tool_name == "generate_scenario":
        return generate_scenario(tool_input, context)
    
    return json.dumps({"error": f"Unknown tool: {tool_name}"})


def generate_quiz(inputs: dict, context: dict) -> str:
    topic = inputs.get("topic", "general")
    num = min(inputs.get("num_questions", 3), 5)
    difficulty = inputs.get("difficulty", "medium")
    
    questions = []
    
    # Question banks by topic keywords
    q_bank = {
        "safety": [
            {"q": "What does PPE stand for?", "options": ["A) Personal Protective Equipment", "B) Professional Protection Essentials", "C) Proper Protective Elements", "D) Personal Protection Elements"], "answer": "A", "explanation": "PPE = Personal Protective Equipment"},
            {"q": "What should you do FIRST when entering a workshop?", "options": ["A) Start working", "B) Check for hazards", "C) Talk to colleagues", "D) Check your phone"], "answer": "B", "explanation": "Always assess hazards before starting work"},
            {"q": "Fire extinguishers should be checked:", "options": ["A) Never", "B) Monthly", "C) Yearly", "D) Only when used"], "answer": "B", "explanation": "Monthly checks ensure readiness"},
        ],
        "engine": [
            {"q": "How many strokes in a typical car engine cycle?", "options": ["A) 2", "B) 4", "C) 6", "D) 8"], "answer": "B", "explanation": "Most cars use 4-stroke engines: intake, compression, power, exhaust"},
            {"q": "What does the cooling system do?", "options": ["A) Makes the car faster", "B) Keeps engine at optimal temperature", "C) Cleans the engine", "D) Reduces noise"], "answer": "B", "explanation": "Cooling system maintains ~90°C operating temperature"},
            {"q": "What component pumps fuel in modern vehicles?", "options": ["A) Carburetor", "B) Electric fuel pump", "C) Radiator", "D) Alternator"], "answer": "B", "explanation": "Modern vehicles use electric fuel pumps with fuel injection"},
        ],
        "electrical": [
            {"q": "What is Ohm's Law formula?", "options": ["A) V = I × R", "B) V = I + R", "C) V = I / R", "D) V = I - R"], "answer": "A", "explanation": "V (Voltage) = I (Current) × R (Resistance)"},
            {"q": "What color is the earth wire in Zambia?", "options": ["A) Red", "B) Blue", "C) Green/Yellow", "D) Black"], "answer": "C", "explanation": "Green/Yellow striped wire is earth (ground)"},
            {"q": "A multimeter measures:", "options": ["A) Only voltage", "B) Only current", "C) Voltage, current, and resistance", "D) Only resistance"], "answer": "C", "explanation": "Multimeters measure V, I, and R"},
        ],
        "default": [
            {"q": f"What is a key skill in {topic}?", "options": ["A) Attention to detail", "B) Guessing", "C) Skipping steps", "D) Working alone"], "answer": "A", "explanation": "Attention to detail is essential in all vocational work"},
        ]
    }
    
    # Find matching questions
    selected = []
    for key, qs in q_bank.items():
        if key in topic.lower():
            selected.extend(qs)
    
    if not selected:
        selected = q_bank["default"]
    
    random.shuffle(selected)
    questions = selected[:num]
    
    return json.dumps({
        "quiz": {
            "topic": topic,
            "difficulty": difficulty,
            "questions": questions,
            "instructions": f"Answer these {len(questions)} questions about {topic}:"
        }
    }, indent=2)


def check_answer(inputs: dict) -> str:
    student = inputs.get("student_answer", "").strip().upper()
    correct = inputs.get("correct_answer", "").strip().upper()
    
    is_correct = student == correct or student in correct
    
    feedback = {
        "correct": is_correct,
        "student_answer": inputs.get("student_answer"),
        "correct_answer": inputs.get("correct_answer"),
        "feedback": "🎉 Correct! Well done!" if is_correct else f"Not quite. The answer is: {inputs.get('correct_answer')}",
        "encouragement": random.choice([
            "Keep up the great work!" if is_correct else "Don't give up - learning takes practice!",
            "You're making progress!" if is_correct else "Every mistake is a learning opportunity!",
            "Excellent understanding!" if is_correct else "Review this concept and try again!"
        ])
    }
    return json.dumps(feedback, indent=2)


def get_practical_example(inputs: dict, context: dict) -> str:
    concept = inputs.get("concept", "")
    industry = inputs.get("industry", context.get("area_name", "engineering")).lower()
    
    examples = {
        "mining": {
            "safety": "At KCM mines, workers do a 'Take 5' safety check before every task. This simple 5-point assessment has reduced accidents by 40%.",
            "electrical": "FQM's Kansanshi mine uses 11kV underground power. Electricians must follow strict lockout/tagout - one mistake could affect 2,000 workers.",
            "welding": "Lumwana mine's workshop repairs dump truck buckets daily. A single bucket costs $80,000 - skilled welders save the company millions yearly.",
            "equipment": "At Mopani, excavator operators earn K15,000+ monthly. The mine invests K50,000 training each operator on their Cat 6040 fleet.",
            "default": "Zambian copper mines employ thousands of skilled technicians. Your TEVETA qualification opens doors to these high-paying jobs."
        },
        "automotive": {
            "engine": "At Toyota Zambia, technicians use OBD-II scanners daily. Last week, a Hilux had rough idle - the scan showed a faulty MAF sensor, saving hours of guessing.",
            "electrical": "Bus companies like Mazhandu service 200+ vehicles. A dead battery can strand 70 passengers - that's why battery testing is critical!",
            "brakes": "ZESCO's fleet drives on rural roads daily. Proper brake maintenance prevents accidents on Zambia's challenging terrain.",
            "transmission": "At Mercedes-Benz Lusaka, automatic transmission rebuilds cost K40,000+. Skilled technicians who can diagnose transmission issues are highly valued.",
            "default": "From Toyota dealerships to local garages, skilled mechanics are always in demand across Zambia."
        },
        "hospitality": {
            "food": "At Radisson Blu, chefs follow strict HACCP. One food poisoning case could close the restaurant and cost millions in reputation.",
            "service": "Protea Hotels train staff to remember guest names. This personal touch brings repeat customers worth thousands of kwacha.",
            "kitchen": "Intercontinental Hotel's kitchen serves 500 meals daily during conferences. Mise en place and teamwork are essential.",
            "housekeeping": "At Sun International, a perfectly made bed takes 3 minutes. Housekeepers clean 15 rooms per shift - efficiency matters!",
            "default": "Zambia's tourism industry is booming - hotels, lodges, and restaurants all need trained hospitality professionals."
        },
        "it": {
            "network": "Airtel Zambia's engineers maintain towers serving millions. One tower down affects thousands of customers instantly.",
            "security": "Banks like Zanaco invest heavily in cybersecurity. A single breach could cost millions - that's why IT security skills pay well.",
            "software": "Mobile money apps like Airtel Money handle millions of transactions. Developers who build reliable systems earn K20,000+ monthly.",
            "hardware": "Computer Village in Lusaka repairs 100+ devices daily. Skilled technicians earn well from both repairs and training others.",
            "default": "From telecoms to fintech, Zambia's digital economy needs skilled IT professionals."
        },
        "construction": {
            "bricklaying": "On the Lusaka-Ndola dual carriageway, masons built drainage structures that handle millions of liters of rainwater. Quality work prevents road damage.",
            "carpentry": "Furniture makers in Kabwata Market earn K10,000+ monthly making custom pieces. Quality joinery commands premium prices.",
            "painting": "High-rise buildings in Lusaka need repainting every 5 years. Commercial painters working at heights earn double the normal rate.",
            "surveying": "Before any building starts, surveyors establish exact positions. A 1cm error on a 10-story building could be catastrophic.",
            "default": "Zambia's construction industry is booming. Housing projects, roads, and commercial buildings all need skilled workers."
        },
        "agriculture": {
            "crop": "Zambeef's farms use GPS-guided tractors for precise planting. One degree off wastes thousands in seeds and fertilizer.",
            "livestock": "At Palabana Dairy, workers manage 500 cows with digital ear tags. Each cow's production is tracked for optimal herd management.",
            "machinery": "During harvest, FRA depots receive grain from thousands of farmers. Equipment operators work 12-hour shifts - skills mean efficiency.",
            "irrigation": "Commercial farms in Mkushi use center pivot irrigation. A well-maintained system saves 30% water compared to flood irrigation.",
            "default": "Agriculture is Zambia's backbone. Commercial farms, Zambeef, and FRA all need skilled agricultural workers."
        },
        "business": {
            "accounting": "At EY Zambia, accountants handle audits for mining companies worth billions. Accuracy is everything - one error could cost the firm millions.",
            "banking": "Tellers at Stanbic process thousands of transactions daily. A single mistake in a K1 million transfer could be career-ending.",
            "marketing": "Zambeef's marketing team uses social media to reach millions. A viral post can boost sales by 20% in a single day.",
            "entrepreneurship": "Young entrepreneurs in Lusaka's tech scene are building apps that serve millions. Some startups have raised millions in funding.",
            "default": "Business skills are essential in every industry. From accounting to marketing, these skills open doors everywhere."
        },
        "health": {
            "community": "Community Health Workers in rural Zambia vaccinate thousands of children yearly. Your work directly saves lives.",
            "pharmacy": "Pharmacies handle controlled substances carefully. Proper inventory management prevents diversion and ensures patients get their medicines.",
            "default": "Healthcare workers are heroes. Every day you make a difference in people's lives."
        },
        "garments": {
            "fashion": "Chitenge fashion designers in Lusaka sell pieces for K500+. Creative designs mixing traditional and modern styles are popular.",
            "tailoring": "Wedding season means tailors work overtime. A skilled bridal gown maker can earn K5,000+ per dress.",
            "default": "Fashion and tailoring skills are always in demand - from everyday clothing to special occasions."
        },
        "engineering": {
            "default": "Engineering skills power Zambia's economy - from mines to manufacturing, your skills are valuable."
        }
    }
    
    industry_examples = examples.get(industry, examples.get("engineering", {}))
    
    # Find matching example
    for key, ex in industry_examples.items():
        if key in concept.lower():
            return json.dumps({"concept": concept, "example": ex, "industry": industry})
    
    return json.dumps({"concept": concept, "example": industry_examples.get("default", "Your skills are valuable!"), "industry": industry})


def track_progress(inputs: dict, context: dict) -> str:
    action = inputs.get("action", "get_summary")
    session_id = inputs.get("session_id", context.get("session_id", "default"))
    module_id = inputs.get("module_id", context.get("module_id", ""))
    
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    c.execute('''CREATE TABLE IF NOT EXISTS student_progress (
        id INTEGER PRIMARY KEY, session_id TEXT, module_id TEXT, 
        competency TEXT, score INTEGER, timestamp TEXT
    )''')
    
    if action == "record":
        competency = inputs.get("competency", "General")
        score = inputs.get("score", 0)
        c.execute("INSERT INTO student_progress (session_id, module_id, competency, score, timestamp) VALUES (?, ?, ?, ?, ?)",
            (session_id, module_id, competency, score, datetime.now().isoformat()))
        conn.commit()
        result = {"recorded": True, "competency": competency, "score": score}
    
    elif action == "get_summary":
        c.execute("""SELECT competency, AVG(score) as avg, COUNT(*) as attempts 
                     FROM student_progress WHERE session_id = ? GROUP BY competency""", (session_id,))
        progress = [{"competency": r[0], "average": round(r[1], 1), "attempts": r[2]} for r in c.fetchall()]
        result = {"progress": progress, "total_activities": len(progress)}
    
    else:  # get_recommendations
        c.execute("""SELECT competency FROM student_progress WHERE session_id = ? 
                     GROUP BY competency HAVING AVG(score) < 70 ORDER BY AVG(score) LIMIT 3""", (session_id,))
        weak = [r[0] for r in c.fetchall()]
        result = {"areas_to_improve": weak, "message": "Focus on these areas" if weak else "Great progress!"}
    
    conn.close()
    return json.dumps(result, indent=2)


def get_career_pathway(inputs: dict, context: dict) -> str:
    program = inputs.get("program", context.get("program_name", ""))
    
    pathways = {
        "Automotive Mechanics": {
            "sector": "Transport/Manufacturing",
            "employers": ["Toyota Zambia", "Mercedes-Benz Zambia", "KCM", "FQM", "Bus companies", "Government fleet"],
            "entry_jobs": ["Apprentice Mechanic", "Service Technician", "Parts Advisor"],
            "senior_jobs": ["Workshop Supervisor", "Service Manager", "Own Business"],
            "salary": "K3,000 - K15,000/month"
        },
        "Power Electrical": {
            "sector": "Energy/Mining",
            "employers": ["ZESCO", "CEC", "Mining companies", "Solar companies", "Contractors"],
            "entry_jobs": ["Electrical Assistant", "Maintenance Electrician", "Solar Installer"],
            "senior_jobs": ["Senior Electrician", "Electrical Supervisor", "Contractor"],
            "salary": "K4,000 - K20,000/month"
        },
        "Welding & Fabrication": {
            "sector": "Mining/Manufacturing",
            "employers": ["Mining companies", "Steel fabricators", "Construction firms", "Shipyards"],
            "entry_jobs": ["Welder Assistant", "Fabricator", "Boilermaker Helper"],
            "senior_jobs": ["Senior Welder", "Welding Inspector", "Fabrication Supervisor"],
            "salary": "K4,000 - K18,000/month"
        },
        "Plumbing": {
            "sector": "Construction",
            "employers": ["Construction companies", "Hotels", "Property management", "Self-employed"],
            "entry_jobs": ["Plumber Assistant", "Maintenance Plumber"],
            "senior_jobs": ["Master Plumber", "Plumbing Contractor", "Project Supervisor"],
            "salary": "K3,500 - K15,000/month"
        },
        "Refrigeration & Air Conditioning": {
            "sector": "HVAC/Retail",
            "employers": ["Shoprite", "Pick n Pay", "Hotels", "Cold storage facilities", "HVAC companies"],
            "entry_jobs": ["HVAC Technician", "Refrigeration Assistant"],
            "senior_jobs": ["Senior Technician", "HVAC Supervisor", "Contractor"],
            "salary": "K4,000 - K18,000/month"
        },
        "Food Production": {
            "sector": "Tourism/Hospitality",
            "employers": ["Radisson Blu", "Protea Hotels", "Safari lodges", "Airlines", "Restaurants"],
            "entry_jobs": ["Commis Chef", "Kitchen Assistant", "Pastry Helper"],
            "senior_jobs": ["Sous Chef", "Executive Chef", "Restaurant Owner"],
            "salary": "K2,500 - K25,000/month"
        },
        "Hotel Operations": {
            "sector": "Tourism/Hospitality",
            "employers": ["Hotels", "Lodges", "Resorts", "Guest houses", "Cruise ships"],
            "entry_jobs": ["Receptionist", "Housekeeper", "Waiter"],
            "senior_jobs": ["Front Office Manager", "Hotel Manager", "Own Lodge"],
            "salary": "K2,500 - K20,000/month"
        },
        "Tour Guiding": {
            "sector": "Tourism",
            "employers": ["Safari companies", "National Parks", "Tour operators", "Lodges"],
            "entry_jobs": ["Tour Guide", "Safari Guide Assistant", "Activity Coordinator"],
            "senior_jobs": ["Senior Guide", "Operations Manager", "Own Tour Company"],
            "salary": "K3,000 - K15,000/month + tips"
        },
        "Computer Systems": {
            "sector": "Digital/ICT",
            "employers": ["Airtel", "MTN", "Banks", "Government", "IT companies"],
            "entry_jobs": ["IT Support", "Help Desk", "Network Technician"],
            "senior_jobs": ["Systems Admin", "Network Engineer", "IT Manager"],
            "salary": "K5,000 - K30,000/month"
        },
        "Software Development": {
            "sector": "Digital/ICT",
            "employers": ["Banks", "Fintechs", "Tech startups", "Government", "Freelance"],
            "entry_jobs": ["Junior Developer", "Web Developer", "QA Tester"],
            "senior_jobs": ["Senior Developer", "Tech Lead", "CTO", "Startup Founder"],
            "salary": "K6,000 - K40,000/month"
        },
        "Graphics Design": {
            "sector": "Creative/Media",
            "employers": ["Advertising agencies", "Media houses", "Print shops", "Freelance"],
            "entry_jobs": ["Junior Designer", "Production Artist"],
            "senior_jobs": ["Art Director", "Creative Director", "Own Agency"],
            "salary": "K3,500 - K20,000/month"
        },
        "General Agriculture": {
            "sector": "Agri-processing",
            "employers": ["Zambeef", "Commercial farms", "FRA", "NGOs", "Own farm"],
            "entry_jobs": ["Farm Worker", "Extension Assistant", "Agro-dealer"],
            "senior_jobs": ["Farm Manager", "Agricultural Officer", "Farm Owner"],
            "salary": "K2,500 - K15,000/month"
        },
        "Mining Operations": {
            "sector": "Mining",
            "employers": ["KCM", "FQM", "Mopani", "Barrick", "Junior miners"],
            "entry_jobs": ["Miner", "Drill Operator", "Blaster Assistant"],
            "senior_jobs": ["Shift Boss", "Mine Captain", "Operations Manager"],
            "salary": "K5,000 - K30,000/month"
        },
        "Earthmoving Equipment": {
            "sector": "Mining/Construction",
            "employers": ["Mining companies", "Road contractors", "Quarries"],
            "entry_jobs": ["Equipment Operator", "Driver"],
            "senior_jobs": ["Senior Operator", "Trainer", "Fleet Supervisor"],
            "salary": "K4,000 - K20,000/month"
        },
        "Bricklaying & Plastering": {
            "sector": "Construction",
            "employers": ["Construction companies", "Contractors", "Self-employed"],
            "entry_jobs": ["Mason", "Plasterer", "Tiler"],
            "senior_jobs": ["Foreman", "Site Supervisor", "Contractor"],
            "salary": "K3,000 - K15,000/month"
        },
        "Carpentry & Joinery": {
            "sector": "Construction/Furniture",
            "employers": ["Construction firms", "Furniture factories", "Self-employed"],
            "entry_jobs": ["Carpenter", "Joiner", "Furniture Maker"],
            "senior_jobs": ["Master Carpenter", "Production Manager", "Business Owner"],
            "salary": "K3,000 - K15,000/month"
        },
        "Accounting": {
            "sector": "Finance",
            "employers": ["Banks", "Accounting firms", "Companies", "Government"],
            "entry_jobs": ["Accounts Clerk", "Bookkeeper", "Audit Assistant"],
            "senior_jobs": ["Accountant", "Finance Manager", "CFO"],
            "salary": "K4,000 - K35,000/month"
        },
        "Entrepreneurship": {
            "sector": "Cross-cutting",
            "employers": ["Self-employed", "Startups", "Family business"],
            "entry_jobs": ["Business Owner", "Consultant"],
            "senior_jobs": ["Successful Entrepreneur", "Investor", "Mentor"],
            "salary": "Variable - unlimited potential!"
        },
        "Fashion Design & Tailoring": {
            "sector": "Manufacturing/Retail",
            "employers": ["Fashion houses", "Boutiques", "Self-employed"],
            "entry_jobs": ["Tailor", "Seamstress", "Design Assistant"],
            "senior_jobs": ["Fashion Designer", "Boutique Owner", "Brand Owner"],
            "salary": "K2,500 - K20,000/month"
        },
        "Community Health": {
            "sector": "Health",
            "employers": ["Clinics", "Hospitals", "NGOs", "Government"],
            "entry_jobs": ["Community Health Worker", "Health Assistant"],
            "senior_jobs": ["Senior CHW", "Health Program Coordinator"],
            "salary": "K2,500 - K12,000/month"
        },
    }
    
    pathway = pathways.get(program, {"sector": "Various", "employers": ["Many opportunities"], "entry_jobs": ["Entry level"], "senior_jobs": ["With experience"], "salary": "Varies"})
    
    return json.dumps({"program": program, "career_pathway": pathway}, indent=2)


def generate_scenario(inputs: dict, context: dict) -> str:
    topic = inputs.get("topic", "")
    workplace = inputs.get("workplace", "garage")
    
    scenarios = {
        "garage": {
            "title": "Customer Problem: Engine Overheating",
            "situation": "A customer brings their 2018 Toyota Corolla. Temperature gauge goes to HOT in traffic. Car has 85,000 km.",
            "your_task": "Diagnose and fix the problem",
            "questions": [
                "What are 3 possible causes?",
                "What diagnostic steps would you take?",
                "What questions would you ask the customer?"
            ],
            "hints": ["Check coolant level", "Consider thermostat, water pump, radiator"]
        },
        "kitchen": {
            "title": "Busy Lunch Rush",
            "situation": "It's 12:30 PM. Restaurant is full. 15 main courses needed in 20 minutes.",
            "your_task": "Manage your station efficiently",
            "questions": [
                "How do you prioritize orders?",
                "What mise en place should be ready?",
                "How do you maintain quality under pressure?"
            ],
            "hints": ["Organize by cooking time", "Communicate with team"]
        },
        "mine": {
            "title": "Equipment Breakdown Underground",
            "situation": "A loader has stopped working 800m underground. Shift ends in 2 hours.",
            "your_task": "Diagnose and repair quickly and safely",
            "questions": [
                "What safety checks come first?",
                "What are common loader failures?",
                "How do you communicate with surface?"
            ],
            "hints": ["Isolate power first", "Check hydraulics, electrical, fuel"]
        },
        "hotel": {
            "title": "VIP Guest Complaint",
            "situation": "A corporate guest is unhappy. Room wasn't ready at check-in. They have a meeting in 1 hour.",
            "your_task": "Resolve the situation and recover the guest's satisfaction",
            "questions": [
                "What do you say first?",
                "What compensation can you offer?",
                "How do you prevent this happening again?"
            ],
            "hints": ["Apologize sincerely", "Offer upgrade or amenity", "Follow up"]
        },
        "construction": {
            "title": "Foundation Problem",
            "situation": "You notice a crack forming in freshly laid foundation wall. Concrete was poured yesterday.",
            "your_task": "Assess the situation and decide on corrective action",
            "questions": [
                "What could have caused the crack?",
                "Is it structural or cosmetic?",
                "What should you report to the supervisor?"
            ],
            "hints": ["Check mix ratio", "Consider curing conditions", "Assess crack width"]
        },
        "farm": {
            "title": "Crop Disease Outbreak",
            "situation": "Your maize field shows yellow leaves and stunted growth in one section. It's 6 weeks after planting.",
            "your_task": "Identify the problem and recommend treatment",
            "questions": [
                "What diseases cause these symptoms?",
                "How do you confirm the diagnosis?",
                "What treatment options exist?"
            ],
            "hints": ["Check for pests", "Consider nutrient deficiency", "Look at soil moisture"]
        },
        "office": {
            "title": "Month-End Closing",
            "situation": "It's month-end. The trial balance doesn't balance. There's a K5,000 difference. Deadline is 5 PM today.",
            "your_task": "Find and correct the error",
            "questions": [
                "What are common causes of imbalance?",
                "How do you systematically find the error?",
                "What controls prevent this in future?"
            ],
            "hints": ["Check transposition errors", "Verify postings", "Review recent entries"]
        },
        "it": {
            "title": "Network Down",
            "situation": "The office network is down. 50 employees can't work. Boss wants it fixed NOW.",
            "your_task": "Troubleshoot and restore connectivity",
            "questions": [
                "What do you check first?",
                "How do you isolate the problem?",
                "What's your communication plan?"
            ],
            "hints": ["Check switch/router status", "Verify physical connections", "Test incrementally"]
        },
        "tailor": {
            "title": "Rush Wedding Order",
            "situation": "A customer needs 5 bridesmaid dresses for Saturday. It's Wednesday. Fabric just arrived.",
            "your_task": "Plan and execute the order on time",
            "questions": [
                "How do you organize the work?",
                "What shortcuts can you safely take?",
                "How do you handle last-minute changes?"
            ],
            "hints": ["Cut all pieces first", "Use efficient assembly line", "Confirm measurements early"]
        },
        "clinic": {
            "title": "Health Education Session",
            "situation": "You're conducting a malaria prevention talk. 30 villagers attend but seem uninterested. Some are leaving.",
            "your_task": "Engage the audience and deliver effective health education",
            "questions": [
                "How do you capture attention?",
                "What visual aids would help?",
                "How do you make it relevant to them?"
            ],
            "hints": ["Use local examples", "Make it interactive", "Speak in local language"]
        }
    }
    
    scenario = scenarios.get(workplace.lower(), scenarios["garage"])
    return json.dumps({"topic": topic, "workplace": workplace, "scenario": scenario}, indent=2)


# =============================================================================
# CURRICULUM
# =============================================================================

CURRICULUM = {
    "ENGINEERING": {
        "icon": "⚙️", "strive": "Mining, Energy, Construction",
        "programs": {
            "automotive": {
                "name": "Automotive Mechanics", "level": "Certificate Level 3",
                "modules": [
                    ("AM-01", "Workshop Safety", "PPE, hazards, first aid, fire safety", 20),
                    ("AM-02", "Engine Systems", "Engine construction, fuel, ignition, cooling, lubrication", 60),
                    ("AM-03", "Electrical Systems", "Batteries, starting, charging, lighting, wiring", 50),
                    ("AM-04", "Transmission", "Manual gearbox, automatic, clutch, driveline", 50),
                    ("AM-05", "Braking Systems", "Disc, drum, hydraulic, ABS, servicing", 40),
                    ("AM-06", "Suspension & Steering", "Suspension types, geometry, alignment", 40),
                    ("AM-07", "Diagnostics", "Fault finding, OBD, troubleshooting", 40),
                    ("AM-08", "Entrepreneurship", "Business planning, costing, customer service", 30),
                ]
            },
            "electrical": {
                "name": "Power Electrical", "level": "Certificate Level 3",
                "modules": [
                    ("PE-01", "Electrical Safety", "Hazards, lockout/tagout, PPE", 20),
                    ("PE-02", "Basic Circuits", "Ohm's law, series/parallel, multimeters", 40),
                    ("PE-03", "Wiring Systems", "Domestic wiring, conduit, ZESCO regulations", 50),
                    ("PE-04", "Motors & Generators", "AC/DC motors, generators, maintenance", 50),
                    ("PE-05", "Transformers", "Principles, connections, testing, protection", 40),
                    ("PE-06", "Industrial Controls", "PLCs, contactors, relays, motor starters", 50),
                    ("PE-07", "Solar Systems", "PV installation, batteries, inverters, sizing", 40),
                    ("PE-08", "Entrepreneurship", "Contracting, estimating, licensing", 30),
                ]
            },
            "welding": {
                "name": "Welding & Fabrication", "level": "Certificate Level 3",
                "modules": [
                    ("WF-01", "Workshop Safety", "Welding hazards, PPE, fire prevention", 20),
                    ("WF-02", "Oxy-Acetylene", "Gas welding, cutting, brazing", 40),
                    ("WF-03", "Arc Welding (SMAW)", "Shielded metal arc, electrodes, positions", 50),
                    ("WF-04", "MIG Welding", "GMAW process, wire feed, shielding gases", 40),
                    ("WF-05", "TIG Welding", "GTAW process, aluminum, stainless steel", 40),
                    ("WF-06", "Metal Fabrication", "Layout, cutting, forming, assembly", 50),
                    ("WF-07", "Blueprint Reading", "Welding symbols, drawings, specs", 30),
                    ("WF-08", "Entrepreneurship", "Fabrication business, costing", 30),
                ]
            },
            "plumbing": {
                "name": "Plumbing", "level": "Certificate Level 3",
                "modules": [
                    ("PL-01", "Plumbing Safety", "Hazards, tools, PPE", 20),
                    ("PL-02", "Pipe Systems", "Pipe types, fittings, joining methods", 40),
                    ("PL-03", "Water Supply", "Cold/hot water systems, pressure, tanks", 50),
                    ("PL-04", "Drainage Systems", "Waste pipes, traps, venting, septic", 40),
                    ("PL-05", "Sanitary Fixtures", "Installation, maintenance, repairs", 40),
                    ("PL-06", "Water Heating", "Geysers, solar heaters, safety devices", 30),
                    ("PL-07", "Pumps & Boreholes", "Pump types, borehole systems, irrigation", 40),
                    ("PL-08", "Entrepreneurship", "Plumbing business, estimating, contracts", 30),
                ]
            },
            "refrigeration": {
                "name": "Refrigeration & Air Conditioning", "level": "Certificate Level 3",
                "modules": [
                    ("RA-01", "Safety & Environment", "Refrigerant handling, EPA, PPE", 20),
                    ("RA-02", "Refrigeration Principles", "Thermodynamics, refrigeration cycle", 40),
                    ("RA-03", "System Components", "Compressors, condensers, evaporators", 50),
                    ("RA-04", "Domestic Refrigeration", "Fridges, freezers, troubleshooting", 40),
                    ("RA-05", "Commercial Refrigeration", "Display cases, cold rooms, ice machines", 50),
                    ("RA-06", "Air Conditioning", "Split systems, window units, central AC", 50),
                    ("RA-07", "Electrical Controls", "Thermostats, relays, capacitors, wiring", 40),
                    ("RA-08", "Entrepreneurship", "HVAC business, service contracts", 30),
                ]
            },
            "fitting": {
                "name": "Fitting & Machining", "level": "Certificate Level 3",
                "modules": [
                    ("FM-01", "Workshop Safety", "Machine hazards, PPE, housekeeping", 20),
                    ("FM-02", "Bench Work", "Filing, sawing, drilling, tapping, threading", 50),
                    ("FM-03", "Lathe Operations", "Turning, facing, boring, threading", 60),
                    ("FM-04", "Milling Operations", "Horizontal, vertical milling, indexing", 50),
                    ("FM-05", "Grinding", "Surface, cylindrical, tool grinding", 40),
                    ("FM-06", "Measurements", "Precision measuring, tolerances, fits", 30),
                    ("FM-07", "Technical Drawing", "Engineering drawings, GD&T basics", 40),
                    ("FM-08", "Entrepreneurship", "Machine shop business, job costing", 30),
                ]
            },
        }
    },
    "AGRICULTURE": {
        "icon": "🌾", "strive": "Agri-processing",
        "programs": {
            "general_agric": {
                "name": "General Agriculture", "level": "Certificate Level 3",
                "modules": [
                    ("GA-01", "Farm Safety", "Agricultural hazards, chemical safety, first aid", 20),
                    ("GA-02", "Soil Science", "Soil types, fertility, pH, Zambian soils", 40),
                    ("GA-03", "Crop Production", "Maize, groundnuts, vegetables, cultivation", 50),
                    ("GA-04", "Irrigation", "Water management, drip, sprinkler, furrow", 40),
                    ("GA-05", "Farm Machinery", "Tractors, implements, maintenance, safety", 40),
                    ("GA-06", "Pest Management", "IPM, pesticide application, storage", 40),
                    ("GA-07", "Post-Harvest", "Storage, drying, grading, FRA standards", 30),
                    ("GA-08", "Farm Business", "Record keeping, budgeting, FISP, cooperatives", 30),
                ]
            },
            "animal_husbandry": {
                "name": "Animal Husbandry", "level": "Certificate Level 3",
                "modules": [
                    ("AH-01", "Animal Safety", "Handling, biosecurity, zoonotic diseases", 20),
                    ("AH-02", "Cattle Production", "Breeds, feeding, breeding, dairy, beef", 50),
                    ("AH-03", "Poultry Production", "Broilers, layers, housing, vaccination", 50),
                    ("AH-04", "Pig Production", "Breeds, housing, feeding, breeding, health", 40),
                    ("AH-05", "Small Stock", "Goats, sheep, rabbits, village chickens", 40),
                    ("AH-06", "Animal Health", "Common diseases, vaccination, treatment", 40),
                    ("AH-07", "Feed Management", "Nutrition, feed formulation, local feeds", 30),
                    ("AH-08", "Livestock Business", "Marketing, abattoirs, value addition", 30),
                ]
            },
            "horticulture": {
                "name": "Horticulture", "level": "Certificate Level 3",
                "modules": [
                    ("HT-01", "Horticultural Safety", "Chemical handling, tool safety", 20),
                    ("HT-02", "Plant Propagation", "Seeds, cuttings, grafting, nursery", 40),
                    ("HT-03", "Vegetable Production", "Tomatoes, onions, cabbage, rape", 50),
                    ("HT-04", "Fruit Production", "Citrus, mangoes, bananas, orchards", 40),
                    ("HT-05", "Floriculture", "Cut flowers, ornamentals, landscaping", 40),
                    ("HT-06", "Greenhouse Technology", "Structures, climate control, hydroponics", 40),
                    ("HT-07", "Post-Harvest Handling", "Grading, packaging, cold chain, export", 30),
                    ("HT-08", "Horticultural Business", "Market gardening, contracts, export", 30),
                ]
            },
            "agriprocessing": {
                "name": "Agri-Processing", "level": "Certificate Level 3",
                "modules": [
                    ("AP-01", "Food Safety", "HACCP, hygiene, contamination prevention", 20),
                    ("AP-02", "Grain Processing", "Milling, storage, quality control", 50),
                    ("AP-03", "Fruit & Vegetable Processing", "Drying, canning, juicing, packaging", 50),
                    ("AP-04", "Dairy Processing", "Milk handling, pasteurization, yogurt, cheese", 50),
                    ("AP-05", "Meat Processing", "Slaughter, butchery, preservation, packaging", 50),
                    ("AP-06", "Oil Extraction", "Sunflower, groundnut, soya processing", 40),
                    ("AP-07", "Quality Control", "Testing, standards, ZABS compliance", 30),
                    ("AP-08", "Agribusiness", "Value chain, marketing, export procedures", 30),
                ]
            },
        }
    },
    "HOSPITALITY": {
        "icon": "🏨", "strive": "Tourism",
        "programs": {
            "food_production": {
                "name": "Food Production", "level": "Certificate Level 3",
                "modules": [
                    ("FP-01", "Kitchen Safety", "Food safety, HACCP, personal hygiene", 20),
                    ("FP-02", "Food Preparation", "Knife skills, cooking methods, mise en place", 50),
                    ("FP-03", "Zambian Cuisine", "Nshima, traditional dishes, local ingredients", 40),
                    ("FP-04", "International Cuisine", "Continental, Asian, fusion, plating", 50),
                    ("FP-05", "Baking & Pastry", "Breads, cakes, pastries, desserts", 40),
                    ("FP-06", "Menu Planning", "Nutrition, costing, menu design, diets", 30),
                    ("FP-07", "Kitchen Management", "Inventory, ordering, staff supervision", 30),
                    ("FP-08", "Catering Business", "Events, costing, food trucks, entrepreneurship", 30),
                ]
            },
            "hotel_operations": {
                "name": "Hotel Operations", "level": "Certificate Level 3",
                "modules": [
                    ("HO-01", "Hospitality Industry", "Tourism in Zambia, career paths", 20),
                    ("HO-02", "Front Office", "Reservations, check-in/out, guest services", 50),
                    ("HO-03", "Housekeeping", "Room cleaning, laundry, inventory, standards", 40),
                    ("HO-04", "Food & Beverage Service", "Restaurant service, bar, wine service", 50),
                    ("HO-05", "Guest Relations", "Communication, complaints, VIP handling", 30),
                    ("HO-06", "Events Management", "Conferences, weddings, banquets", 40),
                    ("HO-07", "Hotel Systems", "PMS, POS, booking platforms", 30),
                    ("HO-08", "Tourism Business", "Lodge management, tour operations", 30),
                ]
            },
            "tour_guiding": {
                "name": "Tour Guiding", "level": "Certificate Level 3",
                "modules": [
                    ("TG-01", "Tourism Foundations", "Zambian tourism, UNWTO, sustainability", 20),
                    ("TG-02", "Zambian Geography", "National parks, Victoria Falls, sites", 40),
                    ("TG-03", "Wildlife Knowledge", "Big 5, birds, conservation, safari etiquette", 50),
                    ("TG-04", "Cultural Heritage", "Tribes, ceremonies, crafts, sensitivity", 40),
                    ("TG-05", "Tour Management", "Itinerary planning, group dynamics, emergencies", 40),
                    ("TG-06", "Communication Skills", "Public speaking, storytelling, languages", 30),
                    ("TG-07", "First Aid & Safety", "Wilderness first aid, emergency procedures", 30),
                    ("TG-08", "Guiding Business", "Freelance guiding, licensing, marketing", 30),
                ]
            },
            "travel_tourism": {
                "name": "Travel & Tourism", "level": "Certificate Level 3",
                "modules": [
                    ("TT-01", "Tourism Industry", "Global tourism, trends, Zambia's position", 20),
                    ("TT-02", "Travel Geography", "World destinations, time zones, climate", 40),
                    ("TT-03", "Ticketing & Reservations", "GDS systems, airline codes, bookings", 50),
                    ("TT-04", "Tour Packaging", "Itinerary design, costing, suppliers", 50),
                    ("TT-05", "Travel Documentation", "Visas, passports, travel insurance", 30),
                    ("TT-06", "Customer Service", "Client relations, handling complaints", 30),
                    ("TT-07", "Marketing Tourism", "Digital marketing, social media, branding", 30),
                    ("TT-08", "Travel Agency Business", "Starting an agency, IATA, regulations", 30),
                ]
            },
        }
    },
    "IT": {
        "icon": "💻", "strive": "Digital/ICT",
        "programs": {
            "computer_systems": {
                "name": "Computer Systems", "level": "Certificate Level 3",
                "modules": [
                    ("CS-01", "Computer Basics", "Hardware, software, OS, history", 30),
                    ("CS-02", "Hardware Maintenance", "PC assembly, troubleshooting, upgrades", 50),
                    ("CS-03", "Operating Systems", "Windows, Linux, installation, CLI", 40),
                    ("CS-04", "Networking Basics", "LAN, WAN, IP addressing, cabling", 50),
                    ("CS-05", "Network Administration", "Servers, Active Directory, DHCP, DNS", 50),
                    ("CS-06", "Cybersecurity", "Threats, protection, firewalls, best practices", 40),
                    ("CS-07", "Mobile Devices", "Smartphones, tablets, repair, data recovery", 30),
                    ("CS-08", "IT Business", "Technical support, freelancing, IT services", 30),
                ]
            },
            "software_dev": {
                "name": "Software Development", "level": "Certificate Level 3",
                "modules": [
                    ("SD-01", "Programming Logic", "Algorithms, flowcharts, problem-solving", 40),
                    ("SD-02", "Python Programming", "Syntax, data structures, functions, OOP", 50),
                    ("SD-03", "Web Development", "HTML, CSS, JavaScript, responsive design", 50),
                    ("SD-04", "Databases", "SQL, MySQL, database design, normalization", 40),
                    ("SD-05", "Web Applications", "Flask/Django basics, APIs, deployment", 50),
                    ("SD-06", "Mobile Development", "Android basics, Flutter, app publishing", 40),
                    ("SD-07", "Version Control", "Git, GitHub, collaboration, branching", 20),
                    ("SD-08", "Tech Entrepreneurship", "Startups, freelancing, Agile, projects", 30),
                ]
            },
            "networking": {
                "name": "Computer Networking", "level": "Certificate Level 3",
                "modules": [
                    ("CN-01", "Network Fundamentals", "OSI model, TCP/IP, protocols", 40),
                    ("CN-02", "Network Media", "Cabling, fiber optics, wireless, structured", 40),
                    ("CN-03", "Switching & Routing", "VLANs, routing protocols, Cisco basics", 50),
                    ("CN-04", "Network Services", "DHCP, DNS, web servers, email servers", 50),
                    ("CN-05", "Network Security", "Firewalls, VPN, encryption, policies", 50),
                    ("CN-06", "Wireless Networks", "WiFi standards, access points, surveys", 40),
                    ("CN-07", "Cloud Computing", "AWS/Azure basics, virtualization, SaaS", 40),
                    ("CN-08", "Network Business", "ISP operations, ZICTA regulations", 30),
                ]
            },
            "graphics": {
                "name": "Graphics Design", "level": "Certificate Level 3",
                "modules": [
                    ("GD-01", "Design Principles", "Color theory, typography, composition", 30),
                    ("GD-02", "Adobe Photoshop", "Image editing, manipulation, effects", 50),
                    ("GD-03", "Adobe Illustrator", "Vector graphics, logos, illustrations", 50),
                    ("GD-04", "Adobe InDesign", "Layout design, publications, print prep", 40),
                    ("GD-05", "Branding & Identity", "Logo design, brand guidelines, mockups", 40),
                    ("GD-06", "Print Design", "Flyers, brochures, packaging, prepress", 40),
                    ("GD-07", "Digital Design", "Social media, web graphics, UI basics", 40),
                    ("GD-08", "Design Business", "Freelancing, client management, portfolio", 30),
                ]
            },
        }
    },
    "CONSTRUCTION": {
        "icon": "🏗️", "strive": "Construction",
        "programs": {
            "bricklaying": {
                "name": "Bricklaying & Plastering", "level": "Certificate Level 3",
                "modules": [
                    ("BL-01", "Construction Safety", "Site hazards, scaffolding, PPE, first aid", 20),
                    ("BL-02", "Materials & Tools", "Bricks, blocks, cement, mortar, tools", 30),
                    ("BL-03", "Basic Bricklaying", "Bonds, laying techniques, corners, levels", 50),
                    ("BL-04", "Advanced Bricklaying", "Arches, curves, decorative work", 40),
                    ("BL-05", "Plastering", "Render, skim coat, finishing, textures", 40),
                    ("BL-06", "Tiling", "Floor/wall tiling, cutting, grouting, patterns", 40),
                    ("BL-07", "Blueprint Reading", "Construction drawings, specs, BOQ", 30),
                    ("BL-08", "Construction Business", "Estimating, contracts, supervision", 30),
                ]
            },
            "carpentry": {
                "name": "Carpentry & Joinery", "level": "Certificate Level 3",
                "modules": [
                    ("CJ-01", "Workshop Safety", "Tool safety, machine operation, dust", 20),
                    ("CJ-02", "Hand Tools", "Measuring, marking, cutting, planing", 40),
                    ("CJ-03", "Power Tools", "Circular saw, router, drill press, sanders", 40),
                    ("CJ-04", "Joinery", "Joints, doors, windows, frames, hardware", 50),
                    ("CJ-05", "Furniture Making", "Tables, chairs, cabinets, finishing", 50),
                    ("CJ-06", "Roof Construction", "Trusses, rafters, roofing, waterproofing", 40),
                    ("CJ-07", "Technical Drawing", "Plans, elevations, details, CAD basics", 30),
                    ("CJ-08", "Carpentry Business", "Costing, workshop setup, marketing", 30),
                ]
            },
            "painting": {
                "name": "Painting & Decorating", "level": "Certificate Level 3",
                "modules": [
                    ("PD-01", "Safety & PPE", "Chemical hazards, ventilation, protection", 20),
                    ("PD-02", "Surface Preparation", "Cleaning, sanding, filling, priming", 40),
                    ("PD-03", "Paint Application", "Brushes, rollers, spray painting", 50),
                    ("PD-04", "Interior Decorating", "Color schemes, finishes, wallpaper", 40),
                    ("PD-05", "Exterior Painting", "Weather protection, scaffolding, surfaces", 40),
                    ("PD-06", "Specialty Finishes", "Faux finishes, stenciling, murals", 40),
                    ("PD-07", "Costing & Estimating", "Material calculation, labor, quotations", 30),
                    ("PD-08", "Painting Business", "Contracting, client relations, portfolio", 30),
                ]
            },
            "surveying": {
                "name": "Land Surveying", "level": "Certificate Level 3",
                "modules": [
                    ("LS-01", "Surveying Safety", "Field hazards, equipment care, PPE", 20),
                    ("LS-02", "Surveying Basics", "Measurements, bearings, coordinates", 40),
                    ("LS-03", "Leveling", "Dumpy level, staff readings, contouring", 50),
                    ("LS-04", "Total Station", "Electronic measurements, data collection", 50),
                    ("LS-05", "GPS Surveying", "GNSS principles, RTK, applications", 40),
                    ("LS-06", "Setting Out", "Building layout, road alignment, curves", 40),
                    ("LS-07", "Survey Computations", "Area, volume, earthworks calculations", 40),
                    ("LS-08", "Surveying Business", "Private practice, licensing, contracts", 30),
                ]
            },
        }
    },
    "MINING": {
        "icon": "⛏️", "strive": "Mining",
        "programs": {
            "mining_ops": {
                "name": "Mining Operations", "level": "Certificate Level 3",
                "modules": [
                    ("MO-01", "Mining Safety", "Underground/surface hazards, emergency", 30),
                    ("MO-02", "Geology Basics", "Rock types, ore bodies, minerals", 40),
                    ("MO-03", "Drilling & Blasting", "Drill operation, explosives, blast patterns", 50),
                    ("MO-04", "Earthmoving", "Excavators, loaders, dump trucks, roads", 50),
                    ("MO-05", "Ventilation", "Underground ventilation, dust, gas detection", 40),
                    ("MO-06", "Ground Support", "Roof bolting, timber support, conditions", 40),
                    ("MO-07", "Mineral Processing", "Crushing, grinding, flotation, leaching", 40),
                    ("MO-08", "Mining Regulations", "Safety legislation, ZEMA, environment", 30),
                ]
            },
            "earthmoving": {
                "name": "Earthmoving Equipment", "level": "Certificate Level 3",
                "modules": [
                    ("EM-01", "Equipment Safety", "Pre-start checks, blind spots, signals", 30),
                    ("EM-02", "Excavator Operation", "Controls, digging, loading, trenching", 50),
                    ("EM-03", "Loader Operation", "Wheel loaders, track loaders, stockpiling", 50),
                    ("EM-04", "Dump Truck Operation", "Articulated, rigid, loading, dumping", 50),
                    ("EM-05", "Grader Operation", "Road grading, leveling, maintenance", 40),
                    ("EM-06", "Dozer Operation", "Blade control, ripping, pushing, clearing", 40),
                    ("EM-07", "Equipment Maintenance", "Daily checks, greasing, minor repairs", 40),
                    ("EM-08", "Operator Business", "Freelance operation, contracts, licensing", 30),
                ]
            },
            "mineral_processing": {
                "name": "Mineral Processing", "level": "Certificate Level 3",
                "modules": [
                    ("MP-01", "Plant Safety", "Chemical hazards, machinery, PPE", 30),
                    ("MP-02", "Ore Handling", "Stockpiles, feeders, conveyors, bins", 40),
                    ("MP-03", "Comminution", "Crushing, grinding, ball mills, SAG mills", 50),
                    ("MP-04", "Flotation", "Reagents, cells, froth, concentrate", 50),
                    ("MP-05", "Leaching", "Heap leaching, tank leaching, SX-EW", 50),
                    ("MP-06", "Dewatering", "Thickeners, filters, tailings management", 40),
                    ("MP-07", "Process Control", "Instrumentation, sampling, assaying", 40),
                    ("MP-08", "Environmental Compliance", "Effluent treatment, ZEMA regulations", 30),
                ]
            },
        }
    },
    "BUSINESS": {
        "icon": "📊", "strive": "Cross-cutting",
        "programs": {
            "accounting": {
                "name": "Accounting", "level": "Certificate Level 3",
                "modules": [
                    ("AC-01", "Accounting Basics", "Double-entry, journals, ledgers, trial balance", 40),
                    ("AC-02", "Financial Statements", "Income statement, balance sheet, cash flow", 50),
                    ("AC-03", "Computerized Accounting", "Sage, QuickBooks, Pastel, Excel", 40),
                    ("AC-04", "Taxation", "ZRA requirements, VAT, PAYE, returns", 40),
                    ("AC-05", "Cost Accounting", "Costing methods, budgeting, variance", 40),
                    ("AC-06", "Payroll", "Salary calculations, NAPSA, deductions", 30),
                    ("AC-07", "Auditing Basics", "Internal controls, audit procedures", 30),
                    ("AC-08", "Accounting Business", "Bookkeeping services, ZICA, consulting", 30),
                ]
            },
            "secretarial": {
                "name": "Secretarial Studies", "level": "Certificate Level 3",
                "modules": [
                    ("SS-01", "Office Practice", "Filing, records management, organization", 30),
                    ("SS-02", "Business Communication", "Letters, memos, reports, emails", 40),
                    ("SS-03", "Keyboarding", "Touch typing, speed, accuracy, transcription", 40),
                    ("SS-04", "Computer Applications", "Word, Excel, PowerPoint, email", 50),
                    ("SS-05", "Reception Skills", "Telephone, visitors, appointments", 30),
                    ("SS-06", "Minutes & Meetings", "Agenda, minutes, meeting organization", 30),
                    ("SS-07", "Office Management", "Supervision, budgets, procurement", 30),
                    ("SS-08", "Virtual Assistance", "Remote work, online tools, Upwork", 30),
                ]
            },
            "marketing": {
                "name": "Marketing", "level": "Certificate Level 3",
                "modules": [
                    ("MK-01", "Marketing Principles", "4Ps, market research, consumer behavior", 40),
                    ("MK-02", "Sales Techniques", "Selling process, customer relations, closing", 50),
                    ("MK-03", "Digital Marketing", "Social media, SEO, email marketing", 50),
                    ("MK-04", "Advertising", "Media planning, copywriting, design basics", 40),
                    ("MK-05", "Brand Management", "Brand building, positioning, reputation", 30),
                    ("MK-06", "Customer Service", "Service excellence, complaints, CRM", 30),
                    ("MK-07", "Market Research", "Surveys, data analysis, reporting", 30),
                    ("MK-08", "Marketing Business", "Agency work, freelancing, consulting", 30),
                ]
            },
            "entrepreneurship": {
                "name": "Entrepreneurship", "level": "Certificate Level 3",
                "modules": [
                    ("EN-01", "Business Ideas", "Opportunity identification, creativity, validation", 30),
                    ("EN-02", "Business Planning", "Business model canvas, financial projections", 50),
                    ("EN-03", "Legal Requirements", "PACRA, licenses, ZRA, NAPSA, permits", 40),
                    ("EN-04", "Financial Management", "Bookkeeping, cash flow, pricing, funding", 50),
                    ("EN-05", "Marketing & Sales", "Customer acquisition, branding, social media", 40),
                    ("EN-06", "Operations Management", "Suppliers, inventory, quality, processes", 30),
                    ("EN-07", "Human Resources", "Hiring, contracts, motivation, labor laws", 30),
                    ("EN-08", "Growth Strategies", "Scaling, partnerships, export, franchising", 30),
                ]
            },
            "banking": {
                "name": "Banking & Finance", "level": "Certificate Level 3",
                "modules": [
                    ("BF-01", "Banking Fundamentals", "Financial system, BOZ, types of banks", 30),
                    ("BF-02", "Bank Operations", "Account opening, deposits, withdrawals, KYC", 50),
                    ("BF-03", "Lending", "Loan types, credit assessment, collateral", 50),
                    ("BF-04", "Electronic Banking", "ATMs, mobile money, internet banking", 40),
                    ("BF-05", "Foreign Exchange", "Currency trading, remittances, documentation", 40),
                    ("BF-06", "Customer Service", "Client relations, complaints, upselling", 30),
                    ("BF-07", "Compliance", "AML, fraud prevention, banking regulations", 30),
                    ("BF-08", "Financial Planning", "Personal finance, investments, insurance", 30),
                ]
            },
        }
    },
    "HEALTH": {
        "icon": "🏥", "strive": "Health",
        "programs": {
            "community_health": {
                "name": "Community Health", "level": "Certificate Level 3",
                "modules": [
                    ("CH-01", "Health & Safety", "Infection control, PPE, waste disposal", 20),
                    ("CH-02", "Human Body", "Anatomy, physiology, common diseases", 40),
                    ("CH-03", "Maternal Health", "Pregnancy, childbirth, postnatal care", 50),
                    ("CH-04", "Child Health", "Immunization, nutrition, growth monitoring", 50),
                    ("CH-05", "Disease Prevention", "HIV/AIDS, malaria, TB, health education", 50),
                    ("CH-06", "First Aid", "Emergency response, CPR, wound care", 40),
                    ("CH-07", "Health Promotion", "Community education, behavior change", 30),
                    ("CH-08", "Health Systems", "PHC, referrals, health records, reporting", 30),
                ]
            },
            "pharmacy_assistant": {
                "name": "Pharmacy Assistant", "level": "Certificate Level 3",
                "modules": [
                    ("PA-01", "Pharmacy Safety", "Handling medicines, storage, disposal", 20),
                    ("PA-02", "Pharmacology Basics", "Drug classifications, dosage forms", 40),
                    ("PA-03", "Dispensing", "Prescription interpretation, labeling, counseling", 50),
                    ("PA-04", "Inventory Management", "Stock control, ordering, expiry tracking", 40),
                    ("PA-05", "OTC Medications", "Common ailments, self-medication guidance", 40),
                    ("PA-06", "Pharmaceutical Calculations", "Doses, dilutions, conversions", 40),
                    ("PA-07", "Pharmacy Law", "ZAMRA regulations, controlled substances", 30),
                    ("PA-08", "Pharmacy Business", "Retail pharmacy operations, customer service", 30),
                ]
            },
        }
    },
    "GARMENTS": {
        "icon": "👗", "strive": "Manufacturing",
        "programs": {
            "fashion_design": {
                "name": "Fashion Design & Tailoring", "level": "Certificate Level 3",
                "modules": [
                    ("FD-01", "Workshop Safety", "Machine safety, ergonomics, first aid", 20),
                    ("FD-02", "Sewing Basics", "Hand sewing, machine operation, seams", 40),
                    ("FD-03", "Pattern Making", "Body measurements, drafting, grading", 50),
                    ("FD-04", "Garment Construction", "Cutting, assembly, finishing techniques", 50),
                    ("FD-05", "Fashion Design", "Design principles, sketching, trends", 40),
                    ("FD-06", "African Fashion", "Chitenge designs, traditional styles, modern fusion", 40),
                    ("FD-07", "Alterations & Repairs", "Fitting adjustments, mending, restyling", 30),
                    ("FD-08", "Fashion Business", "Pricing, marketing, boutique management", 30),
                ]
            },
            "textile": {
                "name": "Textile Technology", "level": "Certificate Level 3",
                "modules": [
                    ("TX-01", "Textile Safety", "Chemical handling, machine guards, PPE", 20),
                    ("TX-02", "Fiber Science", "Natural, synthetic fibers, properties", 40),
                    ("TX-03", "Yarn Production", "Spinning, yarn types, quality control", 40),
                    ("TX-04", "Weaving", "Loom operation, fabric structures, patterns", 50),
                    ("TX-05", "Knitting", "Warp/weft knitting, machine operation", 40),
                    ("TX-06", "Dyeing & Printing", "Dye types, printing methods, finishing", 50),
                    ("TX-07", "Quality Control", "Testing, standards, defect identification", 30),
                    ("TX-08", "Textile Business", "Manufacturing, sourcing, export", 30),
                ]
            },
        }
    },
}


def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    for table in ['program_areas', 'programs', 'modules', 'chat_history', 'student_progress']:
        c.execute(f'DROP TABLE IF EXISTS {table}')
    
    c.execute('CREATE TABLE program_areas (id TEXT PRIMARY KEY, name TEXT, icon TEXT, strive TEXT)')
    c.execute('CREATE TABLE programs (id TEXT PRIMARY KEY, area_id TEXT, name TEXT, level TEXT)')
    c.execute('CREATE TABLE modules (id TEXT PRIMARY KEY, program_id TEXT, code TEXT, name TEXT, description TEXT, hours INTEGER)')
    c.execute('CREATE TABLE chat_history (id INTEGER PRIMARY KEY, session_id TEXT, module_id TEXT, role TEXT, content TEXT, timestamp TEXT)')
    c.execute('CREATE TABLE student_progress (id INTEGER PRIMARY KEY, session_id TEXT, module_id TEXT, competency TEXT, score INTEGER, timestamp TEXT)')
    
    for area_id, area in CURRICULUM.items():
        c.execute("INSERT INTO program_areas VALUES (?, ?, ?, ?)", (area_id, area_id.title(), area["icon"], area["strive"]))
        for prog_id, prog in area["programs"].items():
            full_id = f"{area_id.lower()}_{prog_id}"
            c.execute("INSERT INTO programs VALUES (?, ?, ?, ?)", (full_id, area_id, prog["name"], prog["level"]))
            for code, name, desc, hours in prog["modules"]:
                mod_id = code.lower().replace("-", "_")
                c.execute("INSERT INTO modules VALUES (?, ?, ?, ?, ?, ?)", (mod_id, full_id, code, name, desc, hours))
    
    conn.commit()
    conn.close()
    print("✅ Database initialized with agentic tools support!")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# =============================================================================
# SYSTEM PROMPT
# =============================================================================

SYSTEM_PROMPT = """You are an AI tutor for TEVETA Zambia with special tools to enhance learning.

## Your Tools:
1. **generate_quiz** - Create practice questions
2. **check_answer** - Evaluate student answers  
3. **get_practical_example** - Real Zambian workplace examples
4. **track_progress** - Track learning progress
5. **get_career_pathway** - Show job opportunities
6. **generate_scenario** - Hands-on practice problems

## When to Use Tools:
- Student asks to practice/quiz → generate_quiz
- Student answers a question → check_answer
- Explaining a concept → get_practical_example
- Student asks about jobs → get_career_pathway
- Student wants real problems → generate_scenario

## Teaching Style:
- Warm, encouraging, celebrate progress 🎉
- Simple language, practical focus
- Zambian examples (Toyota Zambia, ZESCO, mines)
- Socratic method - guide with questions
- Keep responses concise (2-3 paragraphs)

## Current Context:
- Area: {area}
- Program: {program} ({level})
- Module: {module_code} - {module_name}
- Topics: {module_desc}

Use tools proactively to make learning engaging and practical!"""


# =============================================================================
# ROUTES
# =============================================================================

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/curriculum')
def get_curriculum():
    conn = get_db()
    result = []
    for area in conn.execute("SELECT * FROM program_areas ORDER BY name"):
        programs = []
        for prog in conn.execute("SELECT * FROM programs WHERE area_id = ?", (area['id'],)):
            modules = [dict(m) for m in conn.execute("SELECT * FROM modules WHERE program_id = ? ORDER BY code", (prog['id'],))]
            programs.append({"id": prog['id'], "name": prog['name'], "level": prog['level'], "modules": modules})
        result.append({"id": area['id'], "name": area['name'], "icon": area['icon'], "strive": area['strive'], "programs": programs})
    conn.close()
    return jsonify(result)

@app.route('/api/chat', methods=['POST'])
def chat():
    data = request.json
    user_msg = data.get('message', '')
    module_id = data.get('module_id', '')
    session_id = data.get('session_id', 'default')
    
    if not user_msg:
        return jsonify({'error': 'No message'}), 400
    
    conn = get_db()
    
    module = conn.execute("""
        SELECT m.*, p.name as program_name, p.level as program_level, a.name as area_name 
        FROM modules m JOIN programs p ON m.program_id = p.id JOIN program_areas a ON p.area_id = a.id
        WHERE m.id = ?
    """, (module_id,)).fetchone()
    
    history = list(reversed(conn.execute(
        "SELECT role, content FROM chat_history WHERE session_id = ? AND module_id = ? ORDER BY timestamp DESC LIMIT 10",
        (session_id, module_id)
    ).fetchall()))
    
    conn.execute("INSERT INTO chat_history (session_id, module_id, role, content, timestamp) VALUES (?, ?, ?, ?, ?)",
        (session_id, module_id, 'user', user_msg, datetime.now().isoformat()))
    conn.commit()
    
    context = {
        "session_id": session_id, "module_id": module_id,
        "module_name": module['name'] if module else "",
        "program_name": module['program_name'] if module else "",
        "area_name": module['area_name'] if module else ""
    }
    
    system = SYSTEM_PROMPT.format(
        area=module['area_name'] if module else "General",
        program=module['program_name'] if module else "General",
        level=module['program_level'] if module else "",
        module_code=module['code'] if module else "",
        module_name=module['name'] if module else "",
        module_desc=module['description'] if module else ""
    ) if module else "You are a helpful TEVETA tutor."
    
    messages = [{"role": h['role'], "content": h['content']} for h in history]
    messages.append({"role": "user", "content": user_msg})
    
    try:
        response = client.messages.create(model=MODEL, max_tokens=2048, temperature=0.7, system=system, tools=TOOLS, messages=messages)
        final_response = process_response(response, messages, system, context)
        
        conn.execute("INSERT INTO chat_history (session_id, module_id, role, content, timestamp) VALUES (?, ?, ?, ?, ?)",
            (session_id, module_id, 'assistant', final_response, datetime.now().isoformat()))
        conn.commit()
        conn.close()
        
        return jsonify({'response': final_response, 'model': MODEL})
    except Exception as e:
        conn.close()
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


def process_response(response, messages, system, context, depth=0):
    """Handle tool calls recursively."""
    if depth > 5:
        return "Let me try a different approach."
    
    if response.stop_reason == "tool_use":
        tool_results = []
        assistant_content = []
        
        for block in response.content:
            if block.type == "tool_use":
                print(f"🔧 Tool: {block.name}")
                result = execute_tool(block.name, block.input, context)
                tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
                assistant_content.append(block)
            elif block.type == "text":
                assistant_content.append(block)
        
        messages.append({"role": "assistant", "content": assistant_content})
        messages.append({"role": "user", "content": tool_results})
        
        follow_up = client.messages.create(model=MODEL, max_tokens=2048, temperature=0.7, system=system, tools=TOOLS, messages=messages)
        return process_response(follow_up, messages, system, context, depth + 1)
    
    return "".join(block.text for block in response.content if hasattr(block, 'text'))


@app.route('/api/tools')
def get_tools():
    return jsonify([{"name": t["name"], "description": t["description"]} for t in TOOLS])

@app.route('/api/clear/<session_id>', methods=['POST'])
def clear_history(session_id):
    conn = get_db()
    module_id = request.json.get('module_id') if request.json else None
    if module_id:
        conn.execute("DELETE FROM chat_history WHERE session_id = ? AND module_id = ?", (session_id, module_id))
    else:
        conn.execute("DELETE FROM chat_history WHERE session_id = ?", (session_id,))
    conn.commit()
    conn.close()
    return jsonify({'status': 'cleared'})


if __name__ == '__main__':
    init_db()
    print("\n" + "="*60)
    print("🎓 AI Tutor - TEVETA (Agentic System)")
    print("="*60)
    print(f"Model: {MODEL}")
    print(f"Tools: {', '.join(t['name'] for t in TOOLS)}")
    print("="*60)
    print("Open: http://localhost:8080")
    print("="*60 + "\n")
    app.run(debug=True, port=8080)
