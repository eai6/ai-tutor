"""
Management command to seed Seychelles secondary school curriculum.
Based on actual Seychelles National Curriculum documents:
- SYLLABI_GEOGRAPHY.pdf (Cycle 4: S1-S3)
- Mathematics_Curriculum.docx (Cycle 4: S1-S2, Cycle 5: S3-S5)

Run with: python manage.py seed_seychelles
"""

from django.core.management.base import BaseCommand
from django.contrib.auth.models import User
from apps.accounts.models import Institution, Membership, StudentProfile
from apps.curriculum.models import Course, Unit, Lesson, LessonStep
from apps.llm.models import PromptPack, ModelConfig


# Science of Learning System Prompt
SYSTEM_PROMPT = """You are an expert AI tutor for secondary school students in Seychelles. Your name is "Tutor" and you guide students through their learning journey with patience, encouragement, and expertise.

CORE IDENTITY:
- You are warm, supportive, and genuinely invested in each student's success
- You use the student's name naturally in conversation
- You celebrate effort and progress, not just correct answers
- You create a safe space where mistakes are learning opportunities

LOCAL CONTEXT (SEYCHELLES):
- Use Seychelles place names: Victoria, Mahé, Praslin, La Digue, Anse Boileau, Beau Vallon
- Use local currency (Seychelles Rupee - SCR)
- Reference local industries: fishing, tourism, cinnamon, coconut, vanilla
- Use local names: Jean, Marie, Pierre, Ansel, Lisette, Antoine, Rosa
- Reference familiar contexts: island life, coral reefs, granite islands, tropical climate
- For Geography: Use Seychelles as primary case study when possible"""

TEACHING_STYLE_PROMPT = """TEACHING METHODOLOGY (Science of Learning):

You LEAD the tutoring session. The student does not choose what to do - you guide them through a structured learning experience.

SESSION FLOW:
1. RETRIEVAL PRACTICE (2-3 min)
   - Start by asking 1-2 quick questions about previously learned topics
   - "Before we begin today's lesson, let's warm up! Can you tell me..."
   - This activates prior knowledge and strengthens memory

2. EXPLICIT INSTRUCTION (5 min)
   - Clearly explain today's topic with a worked example
   - Break concepts into small, digestible pieces
   - Use concrete examples before abstract concepts
   - "Today we're learning about [topic]. Let me show you how this works..."

3. GUIDED PRACTICE (10-15 min)
   - Present problems one at a time
   - NEVER give direct answers - use scaffolded hints
   - Ask guiding questions: "What do you think the first step should be?"
   - Provide immediate, specific feedback on each response
   - If wrong: "Not quite. Let's think about this differently..."
   - If right: "Excellent! You've got it. Let's try a harder one..."

4. ASSESSMENT & ADJUSTMENT
   - If student struggles on 2+ problems, slow down and re-explain
   - If student succeeds easily, increase difficulty
   - Always diagnose WHY they got something wrong

5. EXIT TICKET (Required to complete module)
   - "Great work today! Let's finish with a quick check of what you learned."
   - Present 5 multiple choice questions
   - Student must get 4/5 correct to pass
   - If they fail, review the missed concepts and retry

6. REFLECTION & PRAISE
   - Summarize what they learned
   - Praise their effort specifically
   - "You worked really hard on [specific thing]. I'm proud of you!"

KEY PRINCIPLES:
- Active learning: Students should be solving problems within 2-3 minutes
- Scaffolding: Guide with hints, never give away answers
- Immediate feedback: Correct mistakes right away with explanation
- Mastery-based: Don't move on until they demonstrate understanding
- Interleaving: Mix in review of older topics"""

SAFETY_PROMPT = """SAFETY & BOUNDARIES:
- Keep all content age-appropriate for secondary students (ages 12-18)
- Never discuss violence, adult themes, or inappropriate content
- If asked off-topic questions, gently redirect to the lesson
- Do not ask for or store personal information beyond the lesson
- If a student seems distressed, be supportive and suggest talking to a teacher
- Maintain professional tutor-student boundaries at all times"""

FORMAT_RULES_PROMPT = """RESPONSE FORMAT:
- Keep explanations concise (2-4 sentences per concept)
- Use line breaks to separate ideas for readability
- Ask ONE question at a time, wait for response
- Use encouraging language naturally (not excessively)
- For math: Show step-by-step working
- For geography: Reference maps and real-world examples
- End each teaching moment with a check-in question

CONVERSATION STYLE:
- Warm but professional tone
- Use student's name 2-3 times per session (not every message)
- Celebrate specific achievements: "Your explanation of plate tectonics was spot-on!"
- Normalize mistakes: "That's a common error. Here's why..."
- Keep momentum: Don't let sessions drag"""


class Command(BaseCommand):
    help = 'Seeds Seychelles secondary school curriculum (Geography & Mathematics) from official curriculum docs'

    def handle(self, *args, **options):
        self.stdout.write('🇸🇨 Seeding Seychelles Secondary School Curriculum...\n')
        self.stdout.write('   Based on official Seychelles National Curriculum documents\n')
        
        # 1. Create Seychelles Institution
        institution, created = Institution.objects.update_or_create(
            slug='seychelles-secondary',
            defaults={
                'name': 'Seychelles Secondary Schools',
                'timezone': 'Indian/Mahe',
                'is_active': True,
            }
        )
        self.stdout.write(f'  ✓ Institution: {institution.name}')
        
        # 2. Create PromptPack with Science of Learning
        prompt_pack, _ = PromptPack.objects.update_or_create(
            institution=institution,
            name='Seychelles Secondary Tutor',
            version=1,
            defaults={
                'system_prompt': SYSTEM_PROMPT,
                'teaching_style_prompt': TEACHING_STYLE_PROMPT,
                'safety_prompt': SAFETY_PROMPT,
                'format_rules_prompt': FORMAT_RULES_PROMPT,
                'is_active': True,
            }
        )
        self.stdout.write(f'  ✓ PromptPack: {prompt_pack.name}')
        
        # 3. Create ModelConfig
        model_config, _ = ModelConfig.objects.update_or_create(
            institution=institution,
            name='Claude Sonnet',
            defaults={
                'provider': ModelConfig.Provider.ANTHROPIC,
                'model_name': 'claude-sonnet-4-20250514',
                'api_key_env_var': 'ANTHROPIC_API_KEY',
                'max_tokens': 2048,
                'temperature': 0.7,
                'is_active': True,
            }
        )
        self.stdout.write(f'  ✓ ModelConfig: {model_config.name}')
        
        # 4. Create test users
        self._create_users(institution)
        
        # 5. Create Geography Curriculum (from SYLLABI_GEOGRAPHY.pdf)
        self._create_geography_curriculum(institution)
        
        # 6. Create Mathematics Curriculum (from Mathematics_Curriculum.docx)
        self._create_math_curriculum(institution)
        
        self.stdout.write(self.style.SUCCESS('\n✅ Seychelles curriculum seeded successfully!'))
        self.stdout.write('\nTest accounts:')
        self.stdout.write('  - student1 / student123')
        self.stdout.write('  - teacher1 / teacher123')
    
    def _create_users(self, institution):
        """Create test users."""
        # Teacher
        teacher, _ = User.objects.update_or_create(
            username='teacher1',
            defaults={
                'email': 'teacher@seychelles.edu',
                'first_name': 'Marie',
                'last_name': 'Pierre',
            }
        )
        teacher.set_password('teacher123')
        teacher.save()
        Membership.objects.update_or_create(
            user=teacher,
            institution=institution,
            defaults={'role': Membership.Role.TEACHER, 'is_active': True}
        )
        
        # Student
        student, _ = User.objects.update_or_create(
            username='student1',
            defaults={
                'email': 'student@seychelles.edu',
                'first_name': 'Jean',
                'last_name': 'Michel',
            }
        )
        student.set_password('student123')
        student.save()
        Membership.objects.update_or_create(
            user=student,
            institution=institution,
            defaults={'role': Membership.Role.STUDENT, 'is_active': True}
        )
        # Create student profile
        StudentProfile.objects.update_or_create(
            user=student,
            defaults={
                'school': 'mont_fleuri',
                'grade_level': 'S1',
            }
        )
        self.stdout.write('  ✓ Users: teacher1, student1')
    
    def _create_geography_curriculum(self, institution):
        """
        Create Geography course based on SYLLABI_GEOGRAPHY.pdf
        Cycle 4: S1-S3 curriculum
        """
        course, _ = Course.objects.update_or_create(
            institution=institution,
            title='Geography',
            defaults={
                'description': 'Seychelles Secondary Geography Curriculum (Cycle 4: S1-S3). '
                              'Covers physical and human geography with emphasis on Seychelles context.',
                'grade_level': 'S1-S3',
                'is_published': True,
            }
        )
        
        # Geography Units from official syllabus - keeping it shorter for this example
        # Full version would have all units from the PDF
        units_data = [
            {
                'title': 'S1: Introduction to Geography',
                'order_index': 0,
                'lessons': [
                    {'title': 'What is Geography?', 'objective': 'Understand geography as study of earth, inhabitants, and their relationships', 'terminal_objectives': ['Define geography', 'Know fundamental concepts: location, pattern, process', 'Understand human-environment interaction']},
                    {'title': 'Physical and Human Geography', 'objective': 'Distinguish between physical and human geography themes', 'terminal_objectives': ['Identify physical geography topics', 'Identify human geography topics', 'Understand importance of graphicacy']},
                    {'title': 'The Earth', 'objective': 'Know main characteristics of the earth', 'terminal_objectives': ['Describe earth structure', 'Explain rotation and revolution', 'Know moon phases and effects']},
                ]
            },
            {
                'title': 'S1: Map Skills',
                'order_index': 1,
                'lessons': [
                    {'title': 'Types of Maps', 'objective': 'Understand different types of maps and uses', 'terminal_objectives': ['Identify physical, political, thematic maps', 'Read map symbols', 'Understand scale']},
                    {'title': 'Grid References', 'objective': 'Use four-figure and six-figure grid references', 'terminal_objectives': ['Give 4-figure grid references', 'Give 6-figure grid references', 'Locate places on Seychelles maps']},
                    {'title': 'Direction and Compass', 'objective': 'Give direction using compass points and bearings', 'terminal_objectives': ['Use 8-point compass', 'Measure bearings', 'Give directions on maps']},
                ]
            },
            {
                'title': 'S1: Weather and Climate',
                'order_index': 2,
                'lessons': [
                    {'title': 'Weather vs Climate', 'objective': 'Distinguish between weather and climate', 'terminal_objectives': ['Define weather', 'Define climate', 'Identify weather elements']},
                    {'title': 'Weather Instruments', 'objective': 'Describe weather instruments and measurements', 'terminal_objectives': ['Describe Stevenson Screen', 'Use thermometer, barometer, rain gauge', 'Calculate mean temperature']},
                    {'title': 'Types of Rainfall', 'objective': 'Explain formation of convectional, relief, and frontal rainfall', 'terminal_objectives': ['Describe convectional rainfall', 'Describe relief rainfall', 'Describe frontal rainfall']},
                ]
            },
            {
                'title': 'S1: Climate Change',
                'order_index': 3,
                'lessons': [
                    {'title': 'What is Climate Change?', 'objective': 'Understand climate change and its causes', 'terminal_objectives': ['Define climate change', 'Explain greenhouse effect', 'Identify human causes']},
                    {'title': 'Effects of Climate Change', 'objective': 'Recognise effects of climate change', 'terminal_objectives': ['Describe sea level rise (critical for Seychelles)', 'Describe weather pattern changes', 'Describe ecosystem impacts']},
                    {'title': 'Solutions to Climate Change', 'objective': 'Suggest solutions for climate change', 'terminal_objectives': ['Identify local solutions', 'Identify global solutions', 'Understand renewable energy']},
                ]
            },
            {
                'title': 'S1: Population Studies',
                'order_index': 4,
                'lessons': [
                    {'title': 'Population Terminology', 'objective': 'Understand population terminology', 'terminal_objectives': ['Define birth rate, death rate, natural increase', 'Define life expectancy, infant mortality', 'Calculate natural increase']},
                    {'title': 'Population Pyramids', 'objective': 'Recognise features of population pyramids', 'terminal_objectives': ['Identify pyramid features', 'Interpret different pyramid types', 'Describe Seychelles population pyramid']},
                    {'title': 'Population Distribution', 'objective': 'Know factors affecting population density', 'terminal_objectives': ['Calculate population density', 'Explain physical factors', 'Explain human factors']},
                ]
            },
            {
                'title': 'S2: Settlement Studies',
                'order_index': 5,
                'lessons': [
                    {'title': 'Types of Settlements', 'objective': 'Distinguish between villages, towns, cities', 'terminal_objectives': ['Define rural and urban', 'Classify by size and services', 'Identify Victoria as only city']},
                    {'title': 'Settlement Patterns', 'objective': 'Know settlement patterns in Seychelles', 'terminal_objectives': ['Identify linear patterns', 'Identify nucleated patterns', 'Identify dispersed patterns']},
                    {'title': 'Urban Land Use', 'objective': 'Understand Burgess concentric model', 'terminal_objectives': ['Describe Burgess model', 'Identify urban zones', 'Apply to Victoria']},
                ]
            },
            {
                'title': 'S2: Plate Tectonics',
                'order_index': 6,
                'lessons': [
                    {'title': 'Structure of the Earth', 'objective': 'Describe internal structure of Earth', 'terminal_objectives': ['Describe crust, mantle, core', 'Understand plate division', 'Know continental drift theory']},
                    {'title': 'Plate Boundaries', 'objective': 'Understand three types of plate margins', 'terminal_objectives': ['Define constructive boundaries', 'Define destructive boundaries', 'Define conservative boundaries']},
                    {'title': 'Volcanoes and Earthquakes', 'objective': 'Explain volcanoes and earthquakes', 'terminal_objectives': ['Explain volcano formation', 'Explain earthquake occurrence', 'Know why Seychelles has no earthquakes']},
                    {'title': 'Formation of Seychelles', 'objective': 'Understand how Seychelles formed', 'terminal_objectives': ['Explain granite island formation', 'Relate to Gondwanaland separation', 'Distinguish from coral atolls']},
                ]
            },
            {
                'title': 'S2: Tropical Ecosystems',
                'order_index': 7,
                'lessons': [
                    {'title': 'Tropical Rainforests', 'objective': 'Know features of tropical rainforest ecosystems', 'terminal_objectives': ['Describe climate characteristics', 'Describe vegetation structure', 'Explain plant and animal adaptations']},
                    {'title': 'Hot Deserts', 'objective': 'Know features of hot desert ecosystems', 'terminal_objectives': ['Describe desert climate', 'Describe vegetation adaptations', 'Explain animal adaptations']},
                    {'title': 'Managing Ecosystems', 'objective': 'Understand ecosystem management', 'terminal_objectives': ['Discuss deforestation impacts', 'Discuss conservation efforts', 'Relate to Seychelles']},
                ]
            },
            {
                'title': 'S3: Industry and Fishing',
                'order_index': 8,
                'lessons': [
                    {'title': 'Types of Industry', 'objective': 'Know different types of industry', 'terminal_objectives': ['Define primary, secondary, tertiary, quaternary', 'Give Seychelles examples', 'Understand industrial system']},
                    {'title': 'Fishing in Seychelles', 'objective': 'Recognise importance of fishing to Seychelles', 'terminal_objectives': ['Know types of fishing', 'Understand geographic advantages', 'Describe economic importance']},
                    {'title': 'The Blue Economy', 'objective': 'Understand Blue Economy importance', 'terminal_objectives': ['Define Blue Economy', 'Identify marine resources', 'Discuss sustainable development']},
                ]
            },
            {
                'title': 'S3: Coastal Landforms',
                'order_index': 9,
                'lessons': [
                    {'title': 'Wave Action', 'objective': 'Explain wave formation and characteristics', 'terminal_objectives': ['Explain wave formation', 'Describe wave structure', 'Explain swash and backwash']},
                    {'title': 'Erosional Landforms', 'objective': 'Describe coastal erosion features', 'terminal_objectives': ['Explain cliff formation', 'Explain caves, arches, stacks', 'Identify Seychelles examples']},
                    {'title': 'Depositional Landforms', 'objective': 'Describe coastal deposition features', 'terminal_objectives': ['Explain beach formation', 'Explain spits and bars', 'Identify Seychelles examples']},
                ]
            },
        ]
        
        self._create_units_and_lessons(course, units_data)
        lesson_count = Lesson.objects.filter(unit__course=course).count()
        self.stdout.write(f'  ✓ Geography: {len(units_data)} units, {lesson_count} lessons')
    
    def _create_math_curriculum(self, institution):
        """
        Create Mathematics course based on Mathematics_Curriculum.docx
        """
        course, _ = Course.objects.update_or_create(
            institution=institution,
            title='Mathematics',
            defaults={
                'description': 'Seychelles Secondary Mathematics Curriculum. '
                              'Five strands: Number, Algebra, Shape & Space, Measures, Handling Data.',
                'grade_level': 'S1-S5',
                'is_published': True,
            }
        )
        
        units_data = [
            {
                'title': 'Number: Whole Numbers',
                'order_index': 0,
                'lessons': [
                    {'title': 'Place Value and Estimation', 'objective': 'Make estimates of numbers and quantities', 'terminal_objectives': ['Understand place value', 'Round numbers', 'Check validity of calculations']},
                    {'title': 'Factors, Multiples, and Primes', 'objective': 'Apply multiples, factors, prime numbers in problem solving', 'terminal_objectives': ['Find factors and multiples', 'Identify prime numbers', 'Find HCF and LCM']},
                    {'title': 'Directed Numbers', 'objective': 'Use directed numbers including ordering, addition, subtraction', 'terminal_objectives': ['Order positive and negative numbers', 'Add and subtract directed numbers', 'Use in context']},
                ]
            },
            {
                'title': 'Fractions',
                'order_index': 1,
                'lessons': [
                    {'title': 'Equivalent Fractions', 'objective': 'Find equivalent fractions and simplify', 'terminal_objectives': ['Find equivalent fractions', 'Simplify to lowest terms', 'Convert improper to mixed']},
                    {'title': 'Adding and Subtracting Fractions', 'objective': 'Add and subtract fractions confidently', 'terminal_objectives': ['Add with same denominator', 'Add with different denominators', 'Subtract fractions']},
                    {'title': 'Multiplying and Dividing Fractions', 'objective': 'Use multiplication and division with fractions', 'terminal_objectives': ['Multiply fractions', 'Divide fractions', 'Solve word problems']},
                ]
            },
            {
                'title': 'Decimals',
                'order_index': 2,
                'lessons': [
                    {'title': 'Decimal Place Value', 'objective': 'Demonstrate understanding of decimal place value', 'terminal_objectives': ['Read and write decimals', 'Order decimals', 'Round decimals']},
                    {'title': 'Operations with Decimals', 'objective': 'Perform four operations on decimals', 'terminal_objectives': ['Add and subtract', 'Multiply decimals', 'Divide decimals']},
                ]
            },
            {
                'title': 'Percentages',
                'order_index': 3,
                'lessons': [
                    {'title': 'Understanding Percentages', 'objective': 'Appreciate use of percentage in everyday life', 'terminal_objectives': ['Convert between %, fractions, decimals', 'Calculate % of quantity', 'Express as percentage']},
                    {'title': 'Percentage Increase and Decrease', 'objective': 'Work out percentage changes', 'terminal_objectives': ['Calculate increase', 'Calculate decrease', 'Find original value']},
                    {'title': 'Simple Interest', 'objective': 'Calculate simple interest', 'terminal_objectives': ['Use I = P × R × T', 'Calculate interest in SCR', 'Find total amount']},
                ]
            },
            {
                'title': 'Ratio and Proportion',
                'order_index': 4,
                'lessons': [
                    {'title': 'Understanding Ratio', 'objective': 'Demonstrate understanding of ratio notation', 'terminal_objectives': ['Write ratios', 'Simplify ratios', 'Find equivalent ratios']},
                    {'title': 'Dividing in a Ratio', 'objective': 'Solve problems involving proportional division', 'terminal_objectives': ['Divide in given ratio', 'Solve sharing problems', 'Find missing parts']},
                    {'title': 'Scale and Maps', 'objective': 'Apply ratio to maps and plans', 'terminal_objectives': ['Understand map scales', 'Calculate real distances', 'Calculate map distances']},
                ]
            },
            {
                'title': 'Algebra',
                'order_index': 5,
                'lessons': [
                    {'title': 'Algebraic Expressions', 'objective': 'Form algebraic expressions', 'terminal_objectives': ['Use letters for unknowns', 'Write expressions', 'Simplify expressions']},
                    {'title': 'Solving Equations', 'objective': 'Construct and solve simple equations', 'terminal_objectives': ['Solve one-step equations', 'Solve two-step equations', 'Solve with unknowns on both sides']},
                    {'title': 'Sequences', 'objective': 'Determine rules for generating sequences', 'terminal_objectives': ['Continue sequences', 'Find nth term', 'Generate sequences']},
                ]
            },
            {
                'title': 'Geometry: 2D Shapes',
                'order_index': 6,
                'lessons': [
                    {'title': 'Triangles', 'objective': 'Understand properties of triangles', 'terminal_objectives': ['Classify triangles', 'Know angle sum = 180°', 'Construct triangles']},
                    {'title': 'Quadrilaterals', 'objective': 'Understand properties of quadrilaterals', 'terminal_objectives': ['Identify types', 'Know angle sum = 360°', 'Use properties']},
                    {'title': 'Circles', 'objective': 'Understand circle properties', 'terminal_objectives': ['Know terminology', 'Calculate circumference', 'Calculate area']},
                    {'title': 'Angles', 'objective': 'Recognise, draw, measure angles', 'terminal_objectives': ['Measure angles', 'Know angle facts', 'Calculate angles']},
                ]
            },
            {
                'title': 'Measures',
                'order_index': 7,
                'lessons': [
                    {'title': 'Units of Measurement', 'objective': 'Convert between units', 'terminal_objectives': ['Convert length units', 'Convert mass units', 'Convert capacity units']},
                    {'title': 'Perimeter and Area', 'objective': 'Calculate perimeter and area', 'terminal_objectives': ['Calculate perimeter', 'Calculate area of shapes', 'Calculate compound areas']},
                    {'title': 'Volume', 'objective': 'Calculate volume', 'terminal_objectives': ['Volume of cubes/cuboids', 'Volume of prisms', 'Convert volume units']},
                ]
            },
            {
                'title': 'Statistics',
                'order_index': 8,
                'lessons': [
                    {'title': 'Collecting Data', 'objective': 'Collect and organise data', 'terminal_objectives': ['Use tally charts', 'Create frequency tables', 'Group data']},
                    {'title': 'Averages and Range', 'objective': 'Calculate mean, median, mode, range', 'terminal_objectives': ['Calculate mean', 'Find median and mode', 'Calculate range']},
                    {'title': 'Representing Data', 'objective': 'Draw and interpret charts', 'terminal_objectives': ['Draw bar charts', 'Draw pie charts', 'Draw line graphs']},
                    {'title': 'Probability', 'objective': 'Give estimates of probability', 'terminal_objectives': ['Use probability scale', 'Calculate simple probability', 'Identify likely outcomes']},
                ]
            },
        ]
        
        self._create_units_and_lessons(course, units_data)
        lesson_count = Lesson.objects.filter(unit__course=course).count()
        self.stdout.write(f'  ✓ Mathematics: {len(units_data)} units, {lesson_count} lessons')
    
    def _create_units_and_lessons(self, course, units_data):
        """Create units and lessons for a course."""
        for unit_data in units_data:
            unit, _ = Unit.objects.update_or_create(
                course=course,
                title=unit_data['title'],
                defaults={'order_index': unit_data['order_index']}
            )
            
            for i, lesson_data in enumerate(unit_data['lessons']):
                lesson, created = Lesson.objects.update_or_create(
                    unit=unit,
                    title=lesson_data['title'],
                    defaults={
                        'objective': lesson_data['objective'],
                        'estimated_minutes': 20,
                        'mastery_rule': Lesson.MasteryRule.PASS_QUIZ,
                        'order_index': i,
                        'is_published': True,
                    }
                )
                
                if created:
                    objectives_text = '\n'.join(f"• {obj}" for obj in lesson_data.get('terminal_objectives', []))
                    
                    LessonStep.objects.update_or_create(
                        lesson=lesson,
                        order_index=0,
                        defaults={
                            'step_type': LessonStep.StepType.TEACH,
                            'teacher_script': f"""LESSON: {lesson_data['title']}

LEARNING OBJECTIVE: {lesson_data['objective']}

TERMINAL OBJECTIVES:
{objectives_text}

Begin this tutoring session following the structured flow:
1. Start with a retrieval question from a previous related topic
2. Introduce today's topic: {lesson_data['title']}
3. Explain concepts clearly with examples using Seychelles context
4. Guide the student through practice problems (scaffolded hints, no direct answers)
5. End with a 5-question multiple choice exit ticket (4/5 required to pass)
6. Praise their effort and summarize key learnings""",
                            'answer_type': LessonStep.AnswerType.NONE,
                        }
                    )
