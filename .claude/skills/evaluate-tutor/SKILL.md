---
name: evaluate-tutor
description: Use this skill to evaluate the quality of responses from the AI Tutor in adherence with the principles of the science of learning.
---

# Evaluation Workflow

## Distilled Principles from Science of Learning
Ten core distilled principles from the science of learning:
1. **Active Learning** - Instructional explanations must be a minimum effective dose — just enough for the student to begin solving problems within minutes. The majority of session time should be spent on the student doing something (answering, computing, explaining back) rather than reading script.
2. **Direct Instruction + Active Practice** - Every teaching segment should be immediately followed by a student action. The tutor should never present two consecutive blocks of pure instruction without a practice opportunity in between.
3. **Deliberate Practice** - Practice questions must be calibrated to the student's demonstrated level — not too easy (mindless) and not too far beyond mastery (frustrating). After errors, the tutor should provide focused corrective feedback on the specific skill that failed, then offer a similar but slightly varied problem.
4. **Mastery Learning** - The engine should gate progression on demonstrated mastery, not on step count. If a student consistently fails practice problems, the session should diagnose which prerequisite is the bottleneck rather than simply revealing the answer and moving on.
5. **Minimising Cognitive Load** - Tutor responses should present one idea at a time. Worked examples should appear before any practice on a new concept. Each explanation should name its subgoals explicitly. Diagrams and visual media should be surfaced inline, not deferred.
6. **Layering** - Practice problems should authentically require prerequisite skills. Explanations should explicitly link new concepts to previously mastered ones ("This is like the fraction division you already know, but now the numerator is an expression").
7. **Non-Interference** - When serving lessons, the system should avoid placing confusable topics back-to-back. Within a single lesson, the tutor should make discriminating features explicit when a concept could be confused with a related one.
8. **Interleaving / Mixed Practice** - Exit tickets and review tasks should draw from a mix of topics, not just the lesson just taught. Within a session's practice phase, problem types should vary enough that the student cannot mindlessly repeat one procedure.
9. **The Testing Effect / Retrieval Practice** - The tutor should not offer hints too eagerly; the student should first attempt genuine retrieval. Scaffolding should be stripped during review so the student must recall rather than recognise.
10. **Targeted Remediation** - The engine needs a mapping from each lesson/skill to its key prerequisites. When a student fails repeatedly, the system should serve remedial practice on those prerequisites rather than recycling the same unsolvable problem or simply revealing the answer.


## When to Activate

- AI Tutor response quality evaluation
- Evaluating how the AI Tutor adheres to Any OR All of the 10 core distilled principles of the science of learning.

## Tutor Environment
**NOTE: File paths are relative to project folder.**
- Tutor Frontend URL: `http://localhost:8000/`
- Tutor Core Implementation: `apps/`
- Test Tool: `Use Playwright MCP`
- Test Accounts:
  - Student: `username: student1 / password: student123`
  - Teacher: `username: teacher1 / password: teacher123`
- Tutor Curriculum **(Seychelles National Curriculum)** covers:
  - **Mathematics**: 
    - `Cycle: 1. Learner's Standard: Creche – P2, Year Level: C – 2`
    - `Cycle: 2. Learner's Standard: P3 – P4, Year Level: 3 – 4`
    - `Cycle: 3. Learner's Standard: P5 – P5, Year Level: 5 – 6`
    - `Cycle: 4. Learner's Standard: S1 – S2, Year Level: 7 – 8`
    - `Cycle: 5. Learner's Standard: S3 – S5, Year Level: 9 – 11`
  - **Geography**: 
    - `Cycle: 4, Learner's Standard: S1 – S2, Year Level: 7 – 8`
  
## Core Principles

### 1. Always analyze the Tutor responses carefully
- DO NOT make assumptions. Review the Tutor responses or outputs carefully.

### 2. Focus on Response Quality
ALWAYS pay special attention to Tutor response quality and in particular how well the Tutor adheres to the core principles of the science of learning.

### 3. Complete ALL Evaluation Workflows
ALWAYS complete workflow for each scenario. **DO NOT** skip any part/step of `Evaluation Workflow` Steps unless explicitly instructed to do so.


## Workflows

### Scenario: MATHS-S1: Struggling Maths Student

**Adopt Persona**:
- You are Maths student in Seychelles in `S1` i.e `Cycle 4` struggling with `Fractions`

**Workflow 1**: 
  1. Navigate to Tutor Frontend URL using Playwright MCP tools and use Test Student credentials to Sign In.
  2. Choose `Mathematics` and navigate to `Fractions` section
  3. Click `Multiplying and Dividing Fractions`
  4. Engage in a multi-turn tutoring session (> 5 turns) with Tutor where you consistently provide **wrong answers**
  5. After the tutoring session:
    - **Curriculum Adherence**:
      - Review the Seychelles Maths curriculum: `.claude/skills/evaluate-tutor/Seychelles-Mathematics-Curriculum.md`
      - Are the guided examples, practice questions etc. in the tutoring session consistent with the learning objectives and skills for `Cycle 4`? Do they match the expected scope and sequence for `Cycle 4`?
    - **Science of Learning** - Evaluate the tutoring session against the 10 principles of science of learning above (what's working, what's not working, areas for improvement)
  6. Review the Tutor prompts in `apps/tutoring/conversational_tutor.py` and recommend specific improvements.
  7. Compile your results from Steps 5 and 6 above into an evaluation report and save it to `test-reports/` folder.

### Scenario: MATHS-S5: Advanced Maths Student

**Adopt Persona**:
- You are Maths student in Seychelles in `S5` i.e `Cycle 5` struggling with `Fractions`

**Workflow 1**: 
  1. Navigate to Tutor Frontend URL using Playwright MCP tools and use Test Student credentials to Sign In.
  2. Choose `Mathematics` and navigate to `Fractions` section
  3. Click `Multiplying and Dividing Fractions`
  4. Engage in a multi-turn tutoring session (> 5 turns) with Tutor where you always provide **correct answers**
  5. After the tutoring session:
    - **Curriculum Adherence**:
      - Review the Seychelles Maths curriculum: `.claude/skills/evaluate-tutor/Seychelles-Mathematics-Curriculum.md`
      - Are the guided examples, practice questions etc. in the tutoring session consistent with the learning objectives and skills for `Cycle 5`? Do they match the expected scope and sequence for `Cycle 5`?
    - **Science of Learning** - Evaluate the tutoring session against the 10 principles of science of learning above (what's working, what's not working, areas for improvement)
  6. Review the Tutor prompts in `apps/tutoring/conversational_tutor.py` and recommend specific improvements.
  7. Compile your results from Steps 5 and 6 above into an evaluation report and save it to `test-reports/` folder.