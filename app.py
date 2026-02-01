import os
import sqlite3
from datetime import datetime
from flask import Flask, render_template, request, jsonify
from flask_cors import CORS
from dotenv import load_dotenv
import anthropic

# Load environment variables from .env file
load_dotenv()

app = Flask(__name__)
CORS(app)

# Get API key from environment
API_KEY = os.getenv("ANTHROPIC_API_KEY")

if not API_KEY:
    print("\n⚠️  ERROR: ANTHROPIC_API_KEY not found!")
    print("Create a .env file with: ANTHROPIC_API_KEY=your_key_here\n")

client = anthropic.Anthropic(api_key=API_KEY)

DB_PATH = "ai_tutor.db"

# Complete TEVETA Curriculum - All Program Areas
CURRICULUM = {
    "ENGINEERING": {
        "icon": "⚙️",
        "strive": "Mining, Energy, Construction",
        "programs": {
            "automotive": {
                "name": "Automotive Mechanics",
                "level": "Certificate Level 3",
                "modules": [
                    ("AM-01", "Workshop Safety", "PPE, hazard identification, first aid, fire safety", 20),
                    ("AM-02", "Engine Systems", "Engine construction, fuel systems, ignition, cooling, lubrication", 60),
                    ("AM-03", "Electrical Systems", "Batteries, starting systems, charging, lighting, wiring diagrams", 50),
                    ("AM-04", "Transmission Systems", "Manual gearbox, automatic transmission, clutch, driveline", 50),
                    ("AM-05", "Braking Systems", "Drum brakes, disc brakes, hydraulic systems, ABS", 40),
                    ("AM-06", "Suspension & Steering", "Suspension types, steering geometry, wheel alignment", 40),
                    ("AM-07", "Diagnostics", "Fault finding, diagnostic equipment, OBD systems, troubleshooting", 40),
                    ("AM-08", "Entrepreneurship", "Business planning, costing, customer service, workshop management", 30),
                ]
            },
            "electrical": {
                "name": "Power Electrical",
                "level": "Certificate Level 3",
                "modules": [
                    ("PE-01", "Electrical Safety", "Electrical hazards, safe work practices, lockout/tagout, PPE", 20),
                    ("PE-02", "Basic Circuits", "Ohm's law, series/parallel circuits, measurements, multimeters", 40),
                    ("PE-03", "Wiring Systems", "Domestic wiring, conduit, cable sizing, ZESCO regulations", 50),
                    ("PE-04", "Motors & Generators", "AC/DC motors, generators, maintenance, troubleshooting", 50),
                    ("PE-05", "Transformers", "Transformer principles, connections, testing, protection", 40),
                    ("PE-06", "Industrial Controls", "Contactors, relays, PLCs, motor starters, control circuits", 50),
                    ("PE-07", "Solar Systems", "PV installation, batteries, inverters, sizing, off-grid systems", 40),
                    ("PE-08", "Entrepreneurship", "Electrical contracting, estimating, licensing, business skills", 30),
                ]
            },
            "welding": {
                "name": "Welding & Fabrication",
                "level": "Certificate Level 3",
                "modules": [
                    ("WF-01", "Workshop Safety", "Welding hazards, PPE, fire prevention, ventilation", 20),
                    ("WF-02", "Oxy-Acetylene", "Gas welding, cutting, brazing, equipment setup", 40),
                    ("WF-03", "Arc Welding (SMAW)", "Shielded metal arc welding, electrode selection, positions", 50),
                    ("WF-04", "MIG Welding (GMAW)", "Gas metal arc welding, wire feed, shielding gases", 40),
                    ("WF-05", "TIG Welding (GTAW)", "Gas tungsten arc welding, aluminum, stainless steel", 40),
                    ("WF-06", "Metal Fabrication", "Layout, cutting, forming, assembly, finishing", 50),
                    ("WF-07", "Blueprint Reading", "Welding symbols, drawings, specifications, tolerances", 30),
                    ("WF-08", "Entrepreneurship", "Fabrication business, job costing, marketing, contracts", 30),
                ]
            },
            "plumbing": {
                "name": "Plumbing",
                "level": "Certificate Level 3",
                "modules": [
                    ("PL-01", "Plumbing Safety", "Hazards, tools, PPE, working at heights", 20),
                    ("PL-02", "Pipe Systems", "Pipe types, fittings, joining methods, materials", 40),
                    ("PL-03", "Water Supply", "Cold/hot water systems, pressure, storage tanks", 50),
                    ("PL-04", "Drainage Systems", "Waste pipes, traps, venting, septic systems", 40),
                    ("PL-05", "Sanitary Fixtures", "Installation, maintenance, repairs, water saving", 40),
                    ("PL-06", "Water Heating", "Geysers, solar heaters, heat pumps, safety devices", 30),
                    ("PL-07", "Pumps & Boreholes", "Pump types, borehole systems, irrigation, maintenance", 40),
                    ("PL-08", "Entrepreneurship", "Plumbing business, estimating, contracts, licensing", 30),
                ]
            },
            "refrigeration": {
                "name": "Refrigeration & Air Conditioning",
                "level": "Certificate Level 3",
                "modules": [
                    ("RA-01", "Safety & Environment", "Refrigerant handling, EPA regulations, PPE", 20),
                    ("RA-02", "Refrigeration Principles", "Thermodynamics, pressure-temperature, refrigeration cycle", 40),
                    ("RA-03", "System Components", "Compressors, condensers, evaporators, expansion devices", 50),
                    ("RA-04", "Domestic Refrigeration", "Fridges, freezers, troubleshooting, repair", 40),
                    ("RA-05", "Commercial Refrigeration", "Display cases, cold rooms, ice machines", 50),
                    ("RA-06", "Air Conditioning", "Split systems, window units, central AC, installation", 50),
                    ("RA-07", "Electrical Controls", "Thermostats, relays, capacitors, wiring diagrams", 40),
                    ("RA-08", "Entrepreneurship", "HVAC business, service contracts, customer relations", 30),
                ]
            },
        }
    },
    "AGRICULTURE": {
        "icon": "🌾",
        "strive": "Agri-processing",
        "programs": {
            "general_agric": {
                "name": "General Agriculture",
                "level": "Certificate Level 3",
                "modules": [
                    ("GA-01", "Farm Safety", "Agricultural hazards, chemical safety, first aid, PPE", 20),
                    ("GA-02", "Soil Science", "Soil types, fertility, pH, conservation, Zambian soils", 40),
                    ("GA-03", "Crop Production", "Maize, groundnuts, vegetables, planting, cultivation", 50),
                    ("GA-04", "Irrigation", "Water management, drip, sprinkler, furrow irrigation", 40),
                    ("GA-05", "Farm Machinery", "Tractors, implements, maintenance, operation safety", 40),
                    ("GA-06", "Pest Management", "Integrated pest management, pesticide application, storage", 40),
                    ("GA-07", "Post-Harvest", "Storage, drying, grading, FRA standards, marketing", 30),
                    ("GA-08", "Farm Business", "Record keeping, budgeting, FISP, cooperatives", 30),
                ]
            },
            "animal_husbandry": {
                "name": "Animal Husbandry",
                "level": "Certificate Level 3",
                "modules": [
                    ("AH-01", "Animal Safety", "Handling, biosecurity, zoonotic diseases, PPE", 20),
                    ("AH-02", "Cattle Production", "Breeds, feeding, breeding, dairy, beef production", 50),
                    ("AH-03", "Poultry Production", "Broilers, layers, housing, vaccination, management", 50),
                    ("AH-04", "Pig Production", "Breeds, housing, feeding, breeding, health", 40),
                    ("AH-05", "Small Stock", "Goats, sheep, rabbits, village chickens", 40),
                    ("AH-06", "Animal Health", "Common diseases, vaccination, treatment, vet services", 40),
                    ("AH-07", "Feed Management", "Nutrition, feed formulation, local ingredients, storage", 30),
                    ("AH-08", "Livestock Business", "Marketing, abattoirs, value addition, enterprise", 30),
                ]
            },
            "horticulture": {
                "name": "Horticulture",
                "level": "Certificate Level 3",
                "modules": [
                    ("HT-01", "Horticultural Safety", "Chemical handling, tool safety, sun protection", 20),
                    ("HT-02", "Plant Propagation", "Seeds, cuttings, grafting, nursery management", 40),
                    ("HT-03", "Vegetable Production", "Tomatoes, onions, cabbage, rape, protected cultivation", 50),
                    ("HT-04", "Fruit Production", "Citrus, mangoes, bananas, orchard management", 40),
                    ("HT-05", "Floriculture", "Cut flowers, ornamentals, landscaping, export standards", 40),
                    ("HT-06", "Greenhouse Technology", "Structures, climate control, hydroponics basics", 40),
                    ("HT-07", "Post-Harvest Handling", "Grading, packaging, cold chain, export requirements", 30),
                    ("HT-08", "Horticultural Business", "Market gardening, contracts, export procedures", 30),
                ]
            },
        }
    },
    "HOSPITALITY": {
        "icon": "🏨",
        "strive": "Tourism",
        "programs": {
            "food_production": {
                "name": "Food Production",
                "level": "Certificate Level 3",
                "modules": [
                    ("FP-01", "Kitchen Safety", "Food safety, hygiene, HACCP, personal hygiene", 20),
                    ("FP-02", "Food Preparation", "Knife skills, cooking methods, mise en place", 50),
                    ("FP-03", "Zambian Cuisine", "Nshima, traditional dishes, local ingredients", 40),
                    ("FP-04", "International Cuisine", "Continental, Asian, fusion, plating techniques", 50),
                    ("FP-05", "Baking & Pastry", "Breads, cakes, pastries, desserts, decoration", 40),
                    ("FP-06", "Menu Planning", "Nutrition, costing, menu design, special diets", 30),
                    ("FP-07", "Kitchen Management", "Inventory, ordering, staff supervision, FIFO", 30),
                    ("FP-08", "Catering Business", "Events, costing, entrepreneurship, food trucks", 30),
                ]
            },
            "hotel_operations": {
                "name": "Hotel Operations",
                "level": "Certificate Level 3",
                "modules": [
                    ("HO-01", "Hospitality Industry", "Tourism in Zambia, career paths, professionalism", 20),
                    ("HO-02", "Front Office", "Reservations, check-in/out, guest services, PMS", 50),
                    ("HO-03", "Housekeeping", "Room cleaning, laundry, inventory, standards", 40),
                    ("HO-04", "Food & Beverage Service", "Restaurant service, bar operations, wine service", 50),
                    ("HO-05", "Guest Relations", "Communication, complaints, VIP handling, upselling", 30),
                    ("HO-06", "Events Management", "Conferences, weddings, banquets, planning", 40),
                    ("HO-07", "Hotel Systems", "Property management, POS, booking platforms", 30),
                    ("HO-08", "Tourism Business", "Lodge management, tour operations, marketing", 30),
                ]
            },
            "tour_guiding": {
                "name": "Tour Guiding",
                "level": "Certificate Level 3",
                "modules": [
                    ("TG-01", "Tourism Foundations", "Zambian tourism, UNWTO, sustainable tourism", 20),
                    ("TG-02", "Zambian Geography", "National parks, Victoria Falls, cultural sites", 40),
                    ("TG-03", "Wildlife Knowledge", "Big 5, bird species, conservation, safari etiquette", 50),
                    ("TG-04", "Cultural Heritage", "Tribes, ceremonies, crafts, cultural sensitivity", 40),
                    ("TG-05", "Tour Management", "Itinerary planning, group dynamics, emergencies", 40),
                    ("TG-06", "Communication Skills", "Public speaking, storytelling, languages", 30),
                    ("TG-07", "First Aid & Safety", "Wilderness first aid, emergency procedures", 30),
                    ("TG-08", "Guiding Business", "Freelance guiding, licensing, marketing", 30),
                ]
            },
        }
    },
    "IT": {
        "icon": "💻",
        "strive": "Digital/ICT",
        "programs": {
            "computer_systems": {
                "name": "Computer Systems",
                "level": "Certificate Level 3",
                "modules": [
                    ("CS-01", "Computer Basics", "Hardware, software, operating systems, history", 30),
                    ("CS-02", "Hardware Maintenance", "PC assembly, troubleshooting, upgrades, tools", 50),
                    ("CS-03", "Operating Systems", "Windows, Linux, installation, configuration, CLI", 40),
                    ("CS-04", "Networking Basics", "LAN, WAN, IP addressing, cabling, switches", 50),
                    ("CS-05", "Network Administration", "Servers, Active Directory, DHCP, DNS", 50),
                    ("CS-06", "Cybersecurity", "Threats, protection, firewalls, best practices", 40),
                    ("CS-07", "Mobile Devices", "Smartphones, tablets, repair, data recovery", 30),
                    ("CS-08", "IT Business", "Technical support, freelancing, IT services", 30),
                ]
            },
            "software_dev": {
                "name": "Software Development",
                "level": "Certificate Level 3",
                "modules": [
                    ("SD-01", "Programming Logic", "Algorithms, flowcharts, problem-solving, pseudocode", 40),
                    ("SD-02", "Python Programming", "Syntax, data structures, functions, OOP basics", 50),
                    ("SD-03", "Web Development", "HTML, CSS, JavaScript, responsive design", 50),
                    ("SD-04", "Databases", "SQL, MySQL, database design, normalization", 40),
                    ("SD-05", "Web Applications", "Flask/Django basics, APIs, deployment", 50),
                    ("SD-06", "Mobile Development", "Android basics, Flutter introduction, app publishing", 40),
                    ("SD-07", "Version Control", "Git, GitHub, collaboration, branching", 20),
                    ("SD-08", "Tech Entrepreneurship", "Startups, freelancing, project management, Agile", 30),
                ]
            },
            "networking": {
                "name": "Computer Networking",
                "level": "Certificate Level 3",
                "modules": [
                    ("CN-01", "Network Fundamentals", "OSI model, TCP/IP, protocols, standards", 40),
                    ("CN-02", "Network Media", "Cabling, fiber optics, wireless, structured cabling", 40),
                    ("CN-03", "Switching & Routing", "VLANs, routing protocols, Cisco basics", 50),
                    ("CN-04", "Network Services", "DHCP, DNS, web servers, email servers", 50),
                    ("CN-05", "Network Security", "Firewalls, VPN, encryption, security policies", 50),
                    ("CN-06", "Wireless Networks", "WiFi standards, access points, site surveys", 40),
                    ("CN-07", "Cloud Computing", "AWS/Azure basics, virtualization, cloud services", 40),
                    ("CN-08", "Network Business", "ISP operations, ZICTA regulations, contracting", 30),
                ]
            },
        }
    },
    "CONSTRUCTION": {
        "icon": "🏗️",
        "strive": "Construction",
        "programs": {
            "bricklaying": {
                "name": "Bricklaying & Plastering",
                "level": "Certificate Level 3",
                "modules": [
                    ("BL-01", "Construction Safety", "Site hazards, scaffolding, PPE, first aid", 20),
                    ("BL-02", "Materials & Tools", "Bricks, blocks, cement, mortar mixing, tools", 30),
                    ("BL-03", "Basic Bricklaying", "Bonds, laying techniques, corners, levels", 50),
                    ("BL-04", "Advanced Bricklaying", "Arches, curves, decorative work, fireplaces", 40),
                    ("BL-05", "Plastering", "Render, skim coat, finishing, textures", 40),
                    ("BL-06", "Tiling", "Floor and wall tiling, cutting, grouting, patterns", 40),
                    ("BL-07", "Blueprint Reading", "Construction drawings, specifications, BOQ", 30),
                    ("BL-08", "Construction Business", "Estimating, contracts, supervision, tendering", 30),
                ]
            },
            "carpentry": {
                "name": "Carpentry & Joinery",
                "level": "Certificate Level 3",
                "modules": [
                    ("CJ-01", "Workshop Safety", "Tool safety, machine operation, dust extraction", 20),
                    ("CJ-02", "Hand Tools", "Measuring, marking, cutting, planing, chiseling", 40),
                    ("CJ-03", "Power Tools", "Circular saw, router, drill press, sanders", 40),
                    ("CJ-04", "Joinery", "Joints, doors, windows, frames, hardware", 50),
                    ("CJ-05", "Furniture Making", "Tables, chairs, cabinets, finishing, upholstery", 50),
                    ("CJ-06", "Roof Construction", "Trusses, rafters, roofing materials, waterproofing", 40),
                    ("CJ-07", "Technical Drawing", "Plans, elevations, details, CAD basics", 30),
                    ("CJ-08", "Carpentry Business", "Costing, workshop setup, marketing, contracts", 30),
                ]
            },
            "painting": {
                "name": "Painting & Decorating",
                "level": "Certificate Level 3",
                "modules": [
                    ("PD-01", "Safety & PPE", "Chemical hazards, ventilation, protective equipment", 20),
                    ("PD-02", "Surface Preparation", "Cleaning, sanding, filling, priming, sealing", 40),
                    ("PD-03", "Paint Application", "Brushes, rollers, spray painting, techniques", 50),
                    ("PD-04", "Interior Decorating", "Color schemes, finishes, wallpaper, textures", 40),
                    ("PD-05", "Exterior Painting", "Weather protection, scaffolding, surface types", 40),
                    ("PD-06", "Specialty Finishes", "Faux finishes, stenciling, murals, signwriting", 40),
                    ("PD-07", "Costing & Estimating", "Material calculation, labor, quotations", 30),
                    ("PD-08", "Painting Business", "Contracting, client relations, portfolio", 30),
                ]
            },
        }
    },
    "MINING": {
        "icon": "⛏️",
        "strive": "Mining",
        "programs": {
            "mining_ops": {
                "name": "Mining Operations",
                "level": "Certificate Level 3",
                "modules": [
                    ("MO-01", "Mining Safety", "Underground/surface hazards, emergency procedures", 30),
                    ("MO-02", "Geology Basics", "Rock types, ore bodies, mineral identification", 40),
                    ("MO-03", "Drilling & Blasting", "Drill operation, explosives safety, blast patterns", 50),
                    ("MO-04", "Earthmoving", "Excavators, loaders, dump trucks, haul roads", 50),
                    ("MO-05", "Ventilation", "Underground ventilation, dust control, gas detection", 40),
                    ("MO-06", "Ground Support", "Roof bolting, timber support, ground conditions", 40),
                    ("MO-07", "Mineral Processing", "Crushing, grinding, flotation, leaching basics", 40),
                    ("MO-08", "Mining Regulations", "Safety legislation, environmental compliance, ZEMA", 30),
                ]
            },
            "earthmoving": {
                "name": "Earthmoving Equipment",
                "level": "Certificate Level 3",
                "modules": [
                    ("EM-01", "Equipment Safety", "Pre-start checks, blind spots, communication", 30),
                    ("EM-02", "Excavator Operation", "Controls, digging, loading, trenching", 50),
                    ("EM-03", "Loader Operation", "Wheel loaders, track loaders, stockpiling", 50),
                    ("EM-04", "Dump Truck Operation", "Articulated, rigid, loading, dumping, roads", 50),
                    ("EM-05", "Grader Operation", "Road grading, leveling, maintenance", 40),
                    ("EM-06", "Dozer Operation", "Blade control, ripping, pushing, land clearing", 40),
                    ("EM-07", "Equipment Maintenance", "Daily checks, greasing, minor repairs", 40),
                    ("EM-08", "Operator Business", "Freelance operation, contracts, licensing", 30),
                ]
            },
        }
    },
    "BUSINESS": {
        "icon": "📊",
        "strive": "Cross-cutting",
        "programs": {
            "accounting": {
                "name": "Accounting",
                "level": "Certificate Level 3",
                "modules": [
                    ("AC-01", "Accounting Basics", "Double-entry, journals, ledgers, trial balance", 40),
                    ("AC-02", "Financial Statements", "Income statement, balance sheet, cash flow", 50),
                    ("AC-03", "Computerized Accounting", "Sage, QuickBooks, Pastel, Excel accounting", 40),
                    ("AC-04", "Taxation", "ZRA requirements, VAT, PAYE, returns, compliance", 40),
                    ("AC-05", "Cost Accounting", "Costing methods, budgeting, variance analysis", 40),
                    ("AC-06", "Payroll", "Salary calculations, NAPSA, deductions, payslips", 30),
                    ("AC-07", "Auditing Basics", "Internal controls, audit procedures, documentation", 30),
                    ("AC-08", "Accounting Business", "Bookkeeping services, ZICA registration, consulting", 30),
                ]
            },
            "secretarial": {
                "name": "Secretarial Studies",
                "level": "Certificate Level 3",
                "modules": [
                    ("SS-01", "Office Practice", "Filing, records management, office organization", 30),
                    ("SS-02", "Business Communication", "Letters, memos, reports, emails, etiquette", 40),
                    ("SS-03", "Keyboarding", "Touch typing, speed, accuracy, transcription", 40),
                    ("SS-04", "Computer Applications", "Word, Excel, PowerPoint, email, internet", 50),
                    ("SS-05", "Reception Skills", "Telephone, visitors, appointments, switchboard", 30),
                    ("SS-06", "Minutes & Meetings", "Agenda, minutes, meeting organization, protocols", 30),
                    ("SS-07", "Office Management", "Supervision, budgets, procurement, inventory", 30),
                    ("SS-08", "Virtual Assistance", "Remote work, online tools, freelancing, Upwork", 30),
                ]
            },
            "marketing": {
                "name": "Marketing",
                "level": "Certificate Level 3",
                "modules": [
                    ("MK-01", "Marketing Principles", "4Ps, market research, consumer behavior", 40),
                    ("MK-02", "Sales Techniques", "Selling process, customer relations, closing", 50),
                    ("MK-03", "Digital Marketing", "Social media, SEO, email marketing, analytics", 50),
                    ("MK-04", "Advertising", "Media planning, copywriting, design basics", 40),
                    ("MK-05", "Brand Management", "Brand building, positioning, reputation", 30),
                    ("MK-06", "Customer Service", "Service excellence, complaints, CRM systems", 30),
                    ("MK-07", "Market Research", "Surveys, data analysis, reporting, insights", 30),
                    ("MK-08", "Marketing Business", "Agency work, freelancing, consulting", 30),
                ]
            },
            "entrepreneurship": {
                "name": "Entrepreneurship",
                "level": "Certificate Level 3",
                "modules": [
                    ("EN-01", "Business Ideas", "Opportunity identification, creativity, validation", 30),
                    ("EN-02", "Business Planning", "Business model canvas, financial projections", 50),
                    ("EN-03", "Legal Requirements", "PACRA registration, licenses, ZRA, NAPSA", 40),
                    ("EN-04", "Financial Management", "Bookkeeping, cash flow, pricing, funding", 50),
                    ("EN-05", "Marketing & Sales", "Customer acquisition, branding, social media", 40),
                    ("EN-06", "Operations Management", "Suppliers, inventory, quality, processes", 30),
                    ("EN-07", "Human Resources", "Hiring, contracts, motivation, labor laws", 30),
                    ("EN-08", "Growth Strategies", "Scaling, partnerships, export, franchising", 30),
                ]
            },
        }
    },
}

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    
    c.execute('DROP TABLE IF EXISTS program_areas')
    c.execute('DROP TABLE IF EXISTS programs')
    c.execute('DROP TABLE IF EXISTS modules')
    c.execute('DROP TABLE IF EXISTS chat_history')
    
    c.execute('''CREATE TABLE program_areas (
        id TEXT PRIMARY KEY, name TEXT, icon TEXT, strive_alignment TEXT
    )''')
    
    c.execute('''CREATE TABLE programs (
        id TEXT PRIMARY KEY, area_id TEXT, name TEXT, level TEXT
    )''')
    
    c.execute('''CREATE TABLE modules (
        id TEXT PRIMARY KEY, program_id TEXT, code TEXT, name TEXT, description TEXT, hours INTEGER
    )''')
    
    c.execute('''CREATE TABLE chat_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id TEXT, module_id TEXT, role TEXT, content TEXT, timestamp TEXT
    )''')
    
    # Seed curriculum
    for area_id, area_data in CURRICULUM.items():
        c.execute("INSERT INTO program_areas VALUES (?, ?, ?, ?)",
            (area_id, area_id.title(), area_data["icon"], area_data["strive"]))
        
        for prog_id, prog_data in area_data["programs"].items():
            full_prog_id = f"{area_id.lower()}_{prog_id}"
            c.execute("INSERT INTO programs VALUES (?, ?, ?, ?)",
                (full_prog_id, area_id, prog_data["name"], prog_data["level"]))
            
            for code, name, desc, hours in prog_data["modules"]:
                module_id = code.lower().replace("-", "_")
                c.execute("INSERT INTO modules VALUES (?, ?, ?, ?, ?, ?)",
                    (module_id, full_prog_id, code, name, desc, hours))
    
    conn.commit()
    conn.close()
    print(f"Database initialized with {sum(len(a['programs']) for a in CURRICULUM.values())} programs")

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

SYSTEM_PROMPT = """You are an AI tutor for TEVETA (Technical Education, Vocational and Entrepreneurship Training Authority) in Zambia.

## Your Teaching Style:
- **Warm, friendly, and encouraging** - celebrate every bit of progress!
- Use **simple, clear language** appropriate for vocational students
- Give **practical Zambian examples**: Toyota Zambia, ZESCO, Copperbelt mines (KCM, FQM), Zambeef, Shoprite, local garages and workshops
- Use the **Socratic method** - ask guiding questions to help students discover answers
- Keep responses **concise** (2-3 paragraphs max)
- Use **bullet points** and **bold text** to highlight key concepts
- Include **safety reminders** where relevant

## Current Learning Context:
- **Program Area:** {area}
- **Program:** {program} ({level})
- **Module:** {module_code} - {module_name}
- **Module Topics:** {module_desc}

## Your Task:
Help the learner master the competencies in this specific module. Relate everything back to the module content. If they ask about something outside this module, gently guide them back or explain how it connects.

Remember: You're preparing students for real jobs in Zambia. Make it practical and relevant!"""

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/curriculum')
def get_curriculum():
    conn = get_db()
    
    areas = conn.execute("SELECT * FROM program_areas ORDER BY name").fetchall()
    result = []
    
    for area in areas:
        programs = conn.execute(
            "SELECT * FROM programs WHERE area_id = ? ORDER BY name", (area['id'],)
        ).fetchall()
        
        area_data = {
            "id": area['id'],
            "name": area['name'],
            "icon": area['icon'],
            "strive": area['strive_alignment'],
            "programs": []
        }
        
        for prog in programs:
            modules = conn.execute(
                "SELECT * FROM modules WHERE program_id = ? ORDER BY code", (prog['id'],)
            ).fetchall()
            
            area_data["programs"].append({
                "id": prog['id'],
                "name": prog['name'],
                "level": prog['level'],
                "modules": [dict(m) for m in modules]
            })
        
        result.append(area_data)
    
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
    
    # Get full module context with joins
    module = conn.execute("""
        SELECT m.*, p.name as program_name, p.level as program_level, a.name as area_name 
        FROM modules m
        JOIN programs p ON m.program_id = p.id
        JOIN program_areas a ON p.area_id = a.id
        WHERE m.id = ?
    """, (module_id,)).fetchone()
    
    # Get history for THIS module and session only
    history = conn.execute(
        "SELECT role, content FROM chat_history WHERE session_id = ? AND module_id = ? ORDER BY timestamp DESC LIMIT 10",
        (session_id, module_id)
    ).fetchall()
    history = list(reversed(history))
    
    # Save user message
    conn.execute(
        "INSERT INTO chat_history (session_id, module_id, role, content, timestamp) VALUES (?, ?, ?, ?, ?)",
        (session_id, module_id, 'user', user_msg, datetime.now().isoformat())
    )
    conn.commit()
    
    # Build system prompt with full context
    if module:
        system = SYSTEM_PROMPT.format(
            area=module['area_name'],
            program=module['program_name'],
            level=module['program_level'],
            module_code=module['code'],
            module_name=module['name'],
            module_desc=module['description']
        )
    else:
        system = "You are a helpful AI tutor for TEVETA vocational education in Zambia. Be friendly and encouraging!"
    
    messages = [{"role": h['role'], "content": h['content']} for h in history]
    messages.append({"role": "user", "content": user_msg})
    
    try:
        response = client.messages.create(
            model=os.getenv("CLAUDE_MODEL", "claude-3-5-sonnet-20241022"),
            max_tokens=1024,
            temperature=0.7,
            system=system,
            messages=messages
        )
        
        reply = response.content[0].text
        
        # Save assistant response
        conn.execute(
            "INSERT INTO chat_history (session_id, module_id, role, content, timestamp) VALUES (?, ?, ?, ?, ?)",
            (session_id, module_id, 'assistant', reply, datetime.now().isoformat())
        )
        conn.commit()
        conn.close()
        
        return jsonify({'response': reply, 'model': response.model})
        
    except Exception as e:
        conn.close()
        return jsonify({'error': str(e)}), 500

@app.route('/api/history/<session_id>/<module_id>', methods=['GET'])
def get_history(session_id, module_id):
    conn = get_db()
    history = conn.execute(
        "SELECT role, content, timestamp FROM chat_history WHERE session_id = ? AND module_id = ? ORDER BY timestamp",
        (session_id, module_id)
    ).fetchall()
    conn.close()
    return jsonify([dict(h) for h in history])

@app.route('/api/clear/<session_id>', methods=['POST'])
def clear_history(session_id):
    module_id = request.json.get('module_id') if request.json else None
    conn = get_db()
    if module_id:
        conn.execute("DELETE FROM chat_history WHERE session_id = ? AND module_id = ?", (session_id, module_id))
    else:
        conn.execute("DELETE FROM chat_history WHERE session_id = ?", (session_id,))
    conn.commit()
    conn.close()
    return jsonify({'status': 'cleared'})

if __name__ == '__main__':
    init_db()
    print("\n" + "="*50)
    print("🎓 AI Tutor - TEVETA")
    print("="*50)
    print(f"Programs: {sum(len(a['programs']) for a in CURRICULUM.values())}")
    print(f"Modules: {sum(len(m['modules']) for a in CURRICULUM.values() for m in a['programs'].values())}")
    print("="*50)
    print("Open: http://localhost:8080")
    print("="*50 + "\n")
    app.run(debug=True, port=8080)
