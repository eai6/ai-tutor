"""
TEVETA Multimedia Learning Resources
Uses YouTube SEARCH links for reliability - always works!

Resources are mapped to curriculum topics for contextual learning.
"""

# =============================================================================
# YOUTUBE SEARCH QUERIES
# These generate YouTube search links - always valid!
# =============================================================================

YOUTUBE_SEARCHES = {
    # =========================================================================
    # FOOD PRODUCTION / HOSPITALITY
    # =========================================================================
    "food_safety": [
        {"query": "food+safety+training+basics", "title": "Food Safety Training Basics", "topic": "HACCP and food safety principles"},
        {"query": "hand+washing+technique+food+handlers", "title": "Hand Washing for Food Handlers", "topic": "Proper hand washing"},
        {"query": "HACCP+food+safety+explained", "title": "HACCP Food Safety System", "topic": "HACCP 7 principles"},
        {"query": "food+temperature+danger+zone", "title": "Temperature Danger Zone", "topic": "Safe food temperatures"},
    ],
    "cookery_basics": [
        {"query": "basic+knife+skills+cooking+tutorial", "title": "Basic Knife Skills Tutorial", "topic": "Knife cuts and techniques"},
        {"query": "5+mother+sauces+french+cooking", "title": "The 5 French Mother Sauces", "topic": "Béchamel, Velouté, Espagnole, Hollandaise, Tomato"},
        {"query": "how+to+make+stock+cooking", "title": "How to Make Stock", "topic": "White stock, brown stock, fish stock"},
        {"query": "cooking+methods+explained+boiling+frying", "title": "Cooking Methods Explained", "topic": "Boiling, frying, roasting, steaming"},
        {"query": "julienne+brunoise+knife+cuts", "title": "Professional Knife Cuts", "topic": "Julienne, brunoise, macedoine"},
    ],
    "kitchen_equipment": [
        {"query": "commercial+kitchen+equipment+tour", "title": "Commercial Kitchen Equipment", "topic": "Industrial kitchen equipment"},
        {"query": "kitchen+fire+safety+extinguisher", "title": "Kitchen Fire Safety", "topic": "Fire extinguishers, safety"},
    ],
    "food_commodities": [
        {"query": "meat+cuts+explained+beef+pork", "title": "Meat Cuts Explained", "topic": "Beef, pork, lamb cuts"},
        {"query": "how+to+fillet+fish+tutorial", "title": "How to Fillet Fish", "topic": "Fish preparation techniques"},
        {"query": "vegetable+cutting+techniques+professional", "title": "Vegetable Cutting Techniques", "topic": "Professional vegetable prep"},
    ],
    
    # =========================================================================
    # ELECTRICAL / POWER ELECTRICAL
    # =========================================================================
    "electrical_safety": [
        {"query": "electrical+safety+training+basics", "title": "Electrical Safety Basics", "topic": "Electrical hazards and prevention"},
        {"query": "lockout+tagout+LOTO+training", "title": "Lockout Tagout Training", "topic": "LOTO safety procedures"},
        {"query": "electrical+PPE+safety+equipment", "title": "Electrical PPE", "topic": "Personal protective equipment"},
    ],
    "electrical_theory": [
        {"query": "ohms+law+explained+simple", "title": "Ohm's Law Explained Simply", "topic": "V=IR calculations"},
        {"query": "series+parallel+circuits+explained", "title": "Series vs Parallel Circuits", "topic": "Circuit analysis fundamentals"},
        {"query": "AC+DC+current+difference+explained", "title": "AC vs DC Explained", "topic": "Alternating and direct current"},
        {"query": "how+to+use+multimeter+beginners", "title": "How to Use a Multimeter", "topic": "Measuring voltage, current, resistance"},
        {"query": "electrical+theory+basics+beginners", "title": "Electrical Theory Basics", "topic": "Voltage, current, resistance"},
    ],
    "domestic_wiring": [
        {"query": "house+wiring+basics+tutorial", "title": "House Wiring Basics", "topic": "Domestic electrical installation"},
        {"query": "how+circuit+breakers+work", "title": "How Circuit Breakers Work", "topic": "MCB, RCD protection"},
        {"query": "electrical+earthing+grounding+explained", "title": "Earthing and Grounding", "topic": "Electrical earthing systems"},
        {"query": "electrical+cable+sizing+guide", "title": "Cable Sizing Guide", "topic": "Selecting correct cable sizes"},
        {"query": "wiring+color+codes+electrical", "title": "Wire Color Codes", "topic": "Electrical wiring standards"},
    ],
    "motors_generators": [
        {"query": "how+electric+motors+work+animation", "title": "How Electric Motors Work", "topic": "AC and DC motor principles"},
        {"query": "three+phase+power+explained", "title": "Three Phase Power Explained", "topic": "3-phase power systems"},
        {"query": "motor+starters+DOL+star+delta", "title": "Motor Starters Explained", "topic": "Motor starting methods"},
    ],
    "solar_pv": [
        {"query": "how+solar+panels+work+explained", "title": "How Solar Panels Work", "topic": "Photovoltaic effect explained"},
        {"query": "solar+system+sizing+calculator+off+grid", "title": "Solar System Sizing Guide", "topic": "Calculating panel and battery sizes"},
        {"query": "solar+panel+installation+tutorial+DIY", "title": "Solar Panel Installation", "topic": "Solar PV installation guide"},
        {"query": "MPPT+vs+PWM+charge+controller", "title": "MPPT vs PWM Controllers", "topic": "Charge controller comparison"},
        {"query": "off+grid+solar+system+setup", "title": "Off-Grid Solar Setup", "topic": "Complete solar system"},
    ],
    
    # =========================================================================
    # CARPENTRY & JOINERY
    # =========================================================================
    "carpentry_safety": [
        {"query": "workshop+safety+rules+woodworking", "title": "Workshop Safety Rules", "topic": "Woodworking safety"},
        {"query": "power+tool+safety+training", "title": "Power Tool Safety", "topic": "Safe use of power tools"},
    ],
    "hand_tools": [
        {"query": "essential+hand+tools+woodworking", "title": "Essential Woodworking Hand Tools", "topic": "Measuring, marking, cutting tools"},
        {"query": "how+to+sharpen+chisels+woodworking", "title": "How to Sharpen Chisels", "topic": "Chisel sharpening techniques"},
        {"query": "how+to+use+hand+plane+woodworking", "title": "How to Use a Hand Plane", "topic": "Planing techniques"},
    ],
    "wood_joints": [
        {"query": "mortise+tenon+joint+tutorial", "title": "Mortise and Tenon Joint", "topic": "Making mortise and tenon joints"},
        {"query": "dovetail+joint+by+hand+tutorial", "title": "How to Cut Dovetail Joints", "topic": "Hand-cut dovetails"},
        {"query": "basic+wood+joints+woodworking", "title": "Basic Wood Joints", "topic": "Common woodworking joints"},
        {"query": "half+lap+joint+woodworking", "title": "Half Lap Joint", "topic": "Lap joint techniques"},
    ],
    "woodworking_machines": [
        {"query": "table+saw+basics+beginners", "title": "Table Saw Basics", "topic": "Safe table saw operation"},
        {"query": "planer+jointer+how+to+use", "title": "Planer and Jointer", "topic": "Surface planing techniques"},
        {"query": "band+saw+tips+tricks", "title": "Band Saw Tips", "topic": "Curved cutting on band saw"},
    ],
    "roof_construction": [
        {"query": "roof+framing+basics+tutorial", "title": "Roof Framing Basics", "topic": "Rafters, ridge boards, purlins"},
        {"query": "how+to+cut+roof+rafters", "title": "Cutting Roof Rafters", "topic": "Calculating and cutting rafters"},
        {"query": "roof+truss+construction", "title": "Roof Truss Construction", "topic": "Truss systems"},
    ],
    
    # =========================================================================
    # ICT / COMPUTER SYSTEMS
    # =========================================================================
    "computer_basics": [
        {"query": "how+computers+work+basics", "title": "How Computers Work", "topic": "Basic computer components"},
        {"query": "computer+hardware+explained+beginners", "title": "Computer Hardware Explained", "topic": "CPU, RAM, storage, motherboard"},
    ],
    "computer_assembly": [
        {"query": "how+to+build+PC+step+by+step", "title": "How to Build a PC", "topic": "Complete PC assembly guide"},
        {"query": "PC+building+guide+beginners", "title": "PC Building Guide", "topic": "PC assembly tutorial"},
    ],
    "networking": [
        {"query": "computer+networking+basics+tutorial", "title": "Computer Networking Basics", "topic": "IP addresses, routers, switches"},
        {"query": "OSI+model+explained+simple", "title": "OSI Model Explained", "topic": "7 layers of networking"},
        {"query": "how+to+make+ethernet+cable+RJ45", "title": "How to Make Ethernet Cable", "topic": "RJ45 cable termination"},
        {"query": "IP+address+explained+beginners", "title": "IP Addresses Explained", "topic": "IPv4, subnetting basics"},
    ],
    "troubleshooting": [
        {"query": "PC+troubleshooting+guide+no+display", "title": "PC Troubleshooting Guide", "topic": "Diagnosing common problems"},
        {"query": "computer+not+turning+on+fix", "title": "Computer Won't Turn On Fix", "topic": "Fixing no boot issues"},
    ],
    
    # =========================================================================
    # AUTOMOTIVE MECHANICS
    # =========================================================================
    "engine_systems": [
        {"query": "how+car+engine+works+animation", "title": "How Car Engines Work", "topic": "4-stroke engine cycle"},
        {"query": "car+cooling+system+explained", "title": "Cooling System Explained", "topic": "Radiator, thermostat, water pump"},
        {"query": "fuel+injection+system+explained", "title": "Fuel Injection Explained", "topic": "How fuel injection works"},
    ],
    "automotive_electrical": [
        {"query": "car+electrical+system+explained", "title": "Car Electrical System", "topic": "Battery, alternator, starter"},
        {"query": "how+alternator+works+car", "title": "How Alternators Work", "topic": "Alternator operation"},
    ],
    
    # =========================================================================
    # WELDING & FABRICATION
    # =========================================================================
    "welding_basics": [
        {"query": "stick+welding+beginners+tutorial", "title": "Stick Welding for Beginners", "topic": "SMAW/arc welding basics"},
        {"query": "MIG+welding+basics+tutorial", "title": "MIG Welding Basics", "topic": "MIG/GMAW welding techniques"},
        {"query": "TIG+welding+tutorial+beginners", "title": "TIG Welding Tutorial", "topic": "GTAW welding fundamentals"},
    ],
    "welding_safety": [
        {"query": "welding+safety+PPE+training", "title": "Welding Safety Training", "topic": "PPE and safety procedures"},
    ],
    
    # =========================================================================
    # AGRICULTURE
    # =========================================================================
    "agriculture_safety": [
        {"query": "farm+safety+training+video", "title": "Farm Safety Training", "topic": "Agricultural safety"},
        {"query": "tractor+safety+training", "title": "Tractor Safety", "topic": "Safe tractor operation"},
        {"query": "pesticide+safety+handling", "title": "Pesticide Safety", "topic": "Safe chemical handling"},
    ],
    "agriculture_basics": [
        {"query": "soil+science+basics+agriculture", "title": "Soil Science Basics", "topic": "Soil types and properties"},
        {"query": "irrigation+systems+explained", "title": "Irrigation Systems", "topic": "Types of irrigation"},
        {"query": "crop+production+basics", "title": "Crop Production Basics", "topic": "Planting and harvesting"},
    ],
    
    # =========================================================================
    # GENERAL / CROSS-CUTTING
    # =========================================================================
    "workplace_safety": [
        {"query": "PPE+personal+protective+equipment+training", "title": "PPE Training Video", "topic": "Personal protective equipment"},
        {"query": "fire+safety+training+workplace", "title": "Fire Safety Training", "topic": "Fire prevention and extinguishers"},
        {"query": "first+aid+basics+training", "title": "First Aid Basics", "topic": "Emergency first aid procedures"},
        {"query": "workplace+safety+orientation", "title": "Workplace Safety Orientation", "topic": "General safety principles"},
    ],
    "entrepreneurship": [
        {"query": "how+to+write+business+plan", "title": "How to Write a Business Plan", "topic": "Business plan essentials"},
        {"query": "starting+small+business+beginners", "title": "Starting a Small Business", "topic": "Steps to start a business"},
        {"query": "basic+bookkeeping+small+business", "title": "Basic Bookkeeping", "topic": "Financial record keeping"},
        {"query": "small+business+marketing+basics", "title": "Marketing Basics", "topic": "Marketing your business"},
    ],
}

# =============================================================================
# IMAGE RESOURCES (Using Wikipedia Commons - reliable URLs)
# =============================================================================

IMAGE_RESOURCES = {
    # Food Production
    "knife_cuts": [
        {"url": "https://upload.wikimedia.org/wikipedia/commons/thumb/a/a6/Cutting_board_with_chinese_cleaver.jpg/640px-Cutting_board_with_chinese_cleaver.jpg", "title": "Knife and Cutting Board", "alt": "Professional knife on cutting board", "diagram": False},
    ],
    "mother_sauces": [
        {"url": "https://upload.wikimedia.org/wikipedia/commons/thumb/6/6d/Sauce_Hollandaise.jpg/640px-Sauce_Hollandaise.jpg", "title": "Hollandaise Sauce", "alt": "Classic French sauce", "diagram": False},
    ],
    "haccp_principles": [
        {"url": "https://upload.wikimedia.org/wikipedia/commons/thumb/4/4d/HACCP.svg/640px-HACCP.svg.png", "title": "HACCP Principles", "alt": "HACCP food safety diagram", "diagram": True},
    ],
    
    # Electrical
    "ohms_law": [
        {"url": "https://upload.wikimedia.org/wikipedia/commons/thumb/2/2a/Ohm%27s_Law_Pie_chart.svg/480px-Ohm%27s_Law_Pie_chart.svg.png", "title": "Ohm's Law Triangle", "alt": "V=IR formula triangle diagram", "diagram": True},
    ],
    "circuit_types": [
        {"url": "https://upload.wikimedia.org/wikipedia/commons/thumb/e/eb/Resistors_in_series_and_parallel.svg/640px-Resistors_in_series_and_parallel.svg.png", "title": "Series and Parallel Circuits", "alt": "Circuit diagrams showing series and parallel", "diagram": True},
    ],
    "circuit_breaker": [
        {"url": "https://upload.wikimedia.org/wikipedia/commons/thumb/f/f9/Four-pole_circuit_breaker.jpg/480px-Four-pole_circuit_breaker.jpg", "title": "Circuit Breaker", "alt": "Miniature circuit breaker (MCB)", "diagram": False},
    ],
    "solar_pv_system": [
        {"url": "https://upload.wikimedia.org/wikipedia/commons/thumb/b/b8/Photovoltaik_Dachanlage_Hannover_-_Schwarze_Heide_-_1_MW.jpg/640px-Photovoltaik_Dachanlage_Hannover_-_Schwarze_Heide_-_1_MW.jpg", "title": "Solar PV Installation", "alt": "Rooftop solar panel installation", "diagram": False},
    ],
    "motor_types": [
        {"url": "https://upload.wikimedia.org/wikipedia/commons/thumb/8/89/Squirrel_cage.jpg/480px-Squirrel_cage.jpg", "title": "Induction Motor Rotor", "alt": "Squirrel cage motor rotor", "diagram": False},
    ],
    
    # Carpentry
    "wood_joints": [
        {"url": "https://upload.wikimedia.org/wikipedia/commons/thumb/7/7c/Woodworking_joint.JPG/640px-Woodworking_joint.JPG", "title": "Wood Joints", "alt": "Common woodworking joints", "diagram": False},
    ],
    "roof_components": [
        {"url": "https://upload.wikimedia.org/wikipedia/commons/thumb/4/45/Roof_diagram.svg/640px-Roof_diagram.svg.png", "title": "Roof Components", "alt": "Diagram of roof framing components", "diagram": True},
    ],
    "hand_tools": [
        {"url": "https://upload.wikimedia.org/wikipedia/commons/thumb/8/8a/Hand_tools.jpg/640px-Hand_tools.jpg", "title": "Carpentry Hand Tools", "alt": "Essential woodworking hand tools", "diagram": False},
    ],
    
    # ICT / Computers
    "computer_components": [
        {"url": "https://upload.wikimedia.org/wikipedia/commons/thumb/d/d8/AOpen_i965Gm-DF_Motherboard.jpg/640px-AOpen_i965Gm-DF_Motherboard.jpg", "title": "Computer Motherboard", "alt": "Motherboard with components labeled", "diagram": False},
    ],
    "network_topology": [
        {"url": "https://upload.wikimedia.org/wikipedia/commons/thumb/9/97/NetworkTopologies.svg/640px-NetworkTopologies.svg.png", "title": "Network Topologies", "alt": "Star, bus, ring, mesh network topologies", "diagram": True},
    ],
    "osi_model": [
        {"url": "https://upload.wikimedia.org/wikipedia/commons/thumb/8/8d/OSI_Model_v1.svg/480px-OSI_Model_v1.svg.png", "title": "OSI Model", "alt": "7 layers of OSI model", "diagram": True},
    ],
    
    # Automotive
    "engine_cycle": [
        {"url": "https://upload.wikimedia.org/wikipedia/commons/thumb/d/dc/4StrokeEngine_Ortho_3D_Small.gif/480px-4StrokeEngine_Ortho_3D_Small.gif", "title": "4-Stroke Engine Cycle", "alt": "Intake, compression, power, exhaust animation", "diagram": True},
    ],
    
    # Safety
    "ppe_equipment": [
        {"url": "https://upload.wikimedia.org/wikipedia/commons/thumb/7/7e/First_Aid_Kit.jpg/480px-First_Aid_Kit.jpg", "title": "First Aid Kit", "alt": "Workplace first aid kit", "diagram": False},
    ],
    "fire_extinguisher_types": [
        {"url": "https://upload.wikimedia.org/wikipedia/commons/thumb/4/4a/FireExtinguisherABC.jpg/360px-FireExtinguisherABC.jpg", "title": "Fire Extinguisher", "alt": "ABC fire extinguisher", "diagram": False},
    ],
    "safety_signs": [
        {"url": "https://upload.wikimedia.org/wikipedia/commons/thumb/4/4f/Dangclass2.svg/480px-Dangclass2.svg.png", "title": "Danger Sign", "alt": "Safety warning sign", "diagram": True},
    ],
}

# =============================================================================
# TOPIC TO RESOURCE MAPPING
# =============================================================================

TOPIC_RESOURCE_MAP = {
    # Food Production
    "kitchen safety": ["food_safety", "workplace_safety"],
    "food safety": ["food_safety", "haccp_principles"],
    "haccp": ["food_safety", "haccp_principles"],
    "hygiene": ["food_safety"],
    "cookery": ["cookery_basics", "knife_cuts"],
    "knife skills": ["cookery_basics", "knife_cuts"],
    "knife cuts": ["cookery_basics", "knife_cuts"],
    "stocks": ["cookery_basics"],
    "soups": ["cookery_basics"],
    "sauces": ["cookery_basics", "mother_sauces"],
    "mother sauces": ["cookery_basics", "mother_sauces"],
    "cooking methods": ["cookery_basics"],
    "food commodities": ["food_commodities"],
    "meat": ["food_commodities"],
    "fish": ["food_commodities"],
    "vegetables": ["food_commodities"],
    
    # Electrical
    "electrical safety": ["electrical_safety", "workplace_safety"],
    "lockout tagout": ["electrical_safety"],
    "loto": ["electrical_safety"],
    "ohm's law": ["electrical_theory", "ohms_law"],
    "ohms law": ["electrical_theory", "ohms_law"],
    "electrical theory": ["electrical_theory", "ohms_law", "circuit_types"],
    "circuits": ["electrical_theory", "circuit_types"],
    "series circuit": ["electrical_theory", "circuit_types"],
    "parallel circuit": ["electrical_theory", "circuit_types"],
    "multimeter": ["electrical_theory"],
    "domestic wiring": ["domestic_wiring", "circuit_breaker"],
    "wiring": ["domestic_wiring"],
    "circuit breaker": ["domestic_wiring", "circuit_breaker"],
    "earthing": ["domestic_wiring"],
    "grounding": ["domestic_wiring"],
    "motors": ["motors_generators", "motor_types"],
    "electric motors": ["motors_generators", "motor_types"],
    "generators": ["motors_generators"],
    "solar": ["solar_pv", "solar_pv_system"],
    "solar pv": ["solar_pv", "solar_pv_system"],
    "photovoltaic": ["solar_pv", "solar_pv_system"],
    
    # Carpentry
    "carpentry safety": ["carpentry_safety", "workplace_safety"],
    "workshop safety": ["carpentry_safety", "workplace_safety"],
    "hand tools": ["hand_tools"],
    "woodworking tools": ["hand_tools", "woodworking_machines"],
    "wood joints": ["wood_joints"],
    "mortise": ["wood_joints"],
    "tenon": ["wood_joints"],
    "dovetail": ["wood_joints"],
    "timber": ["hand_tools"],
    "wood": ["hand_tools", "wood_joints"],
    "woodworking machines": ["woodworking_machines"],
    "table saw": ["woodworking_machines"],
    "planer": ["woodworking_machines"],
    "roof": ["roof_construction", "roof_components"],
    "roof construction": ["roof_construction", "roof_components"],
    "rafters": ["roof_construction", "roof_components"],
    
    # ICT
    "computer hardware": ["computer_basics", "computer_assembly", "computer_components"],
    "computer": ["computer_basics", "computer_components"],
    "cpu": ["computer_basics", "computer_components"],
    "motherboard": ["computer_basics", "computer_components"],
    "ram": ["computer_basics", "computer_components"],
    "computer assembly": ["computer_assembly", "computer_components"],
    "build pc": ["computer_assembly"],
    "networking": ["networking", "network_topology", "osi_model"],
    "network": ["networking", "network_topology"],
    "osi model": ["networking", "osi_model"],
    "ip address": ["networking"],
    "ethernet": ["networking"],
    "rj45": ["networking"],
    "troubleshooting": ["troubleshooting"],
    
    # Automotive
    "engine": ["engine_systems", "engine_cycle"],
    "4 stroke": ["engine_systems", "engine_cycle"],
    "cooling system": ["engine_systems"],
    "automotive electrical": ["automotive_electrical"],
    "alternator": ["automotive_electrical"],
    "car battery": ["automotive_electrical"],
    
    # Welding
    "welding": ["welding_basics", "welding_safety"],
    "arc welding": ["welding_basics"],
    "mig welding": ["welding_basics"],
    "tig welding": ["welding_basics"],
    
    # Agriculture
    "farm safety": ["agriculture_safety", "workplace_safety"],
    "agriculture safety": ["agriculture_safety", "workplace_safety"],
    "tractor": ["agriculture_safety"],
    "pesticide": ["agriculture_safety"],
    "irrigation": ["agriculture_basics"],
    "crop": ["agriculture_basics"],
    "soil": ["agriculture_basics"],
    
    # General
    "safety": ["workplace_safety", "ppe_equipment", "fire_extinguisher_types"],
    "ppe": ["workplace_safety", "ppe_equipment"],
    "fire": ["workplace_safety", "fire_extinguisher_types"],
    "first aid": ["workplace_safety", "ppe_equipment"],
    "entrepreneurship": ["entrepreneurship"],
    "business": ["entrepreneurship"],
    "business plan": ["entrepreneurship"],
}


def get_resources_for_topic(topic: str, max_videos: int = 3, max_images: int = 2) -> dict:
    """
    Get multimedia resources relevant to a topic.
    Returns YouTube SEARCH links (always valid) and images.
    """
    topic_lower = topic.lower().strip()
    
    # Find matching resource keys
    matched_keys = set()
    for key, resource_keys in TOPIC_RESOURCE_MAP.items():
        if key in topic_lower or topic_lower in key:
            matched_keys.update(resource_keys)
    
    # If no direct match, try partial word matching
    if not matched_keys:
        words = topic_lower.split()
        for word in words:
            if len(word) > 3:
                for key, resource_keys in TOPIC_RESOURCE_MAP.items():
                    if word in key:
                        matched_keys.update(resource_keys)
    
    # Collect videos (as search queries)
    videos = []
    for key in matched_keys:
        if key in YOUTUBE_SEARCHES:
            videos.extend(YOUTUBE_SEARCHES[key])
    
    # Collect images
    images = []
    for key in matched_keys:
        if key in IMAGE_RESOURCES:
            images.extend(IMAGE_RESOURCES[key])
    
    # Deduplicate
    seen_queries = set()
    unique_videos = []
    for v in videos:
        if v['query'] not in seen_queries:
            seen_queries.add(v['query'])
            unique_videos.append(v)
    
    seen_urls = set()
    unique_images = []
    for img in images:
        if img['url'] not in seen_urls:
            seen_urls.add(img['url'])
            unique_images.append(img)
    
    return {
        "topic": topic,
        "videos": unique_videos[:max_videos],
        "images": unique_images[:max_images],
        "has_resources": len(unique_videos) > 0 or len(unique_images) > 0
    }


def get_youtube_search_url(query: str) -> str:
    """Get YouTube search URL for a query"""
    return f"https://www.youtube.com/results?search_query={query}"
