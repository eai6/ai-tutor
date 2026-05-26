---
name: evaluate-tutor
description: Use this skill to evaluate the quality of responses from the AI Tutor in adherence with the principles of the science of learning.
---

# Evaluation Workflow

## Distilled Principles from Science of Learning
Refer to: `/Users/roy.manzi/WorldBank/AfricaTutor/ai-tutor/design/science-principles.md`

## When to Activate

- AI Tutor response quality evaluation
- Evaluating how the AI Tutor adheres to core distilled principles of the science of learning.

## Tutor Environment
**NOTE: Some File paths are relative to project folder.**
1. Launch the Tutor locally - `/Users/roy.manzi/WorldBank/AfricaTutor/ai-tutor/design/LOCAL_TESTING_GUIDE.md`
2. Tutor Core Implementation: `apps/`
3. Test Tool: Choose `curl` OR `Use Playwright MCP` tool to navigate Tutor app
4. Login using Test Accounts (create a test user if necessary):
  - Student: `username: student1 / password: student123`
  - Teacher: `username: teacher1 / password: teacher123`
  
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
- You are struggling Maths student in Seychelles in `S1` i.e `Cycle 4`

**Workflow 1**: 
  1. Navigate to Tutor Frontend URL using `curl` OR `Playwright MCP` tools and use Test Student credentials to Sign In.
  2. Choose `Mathematics` and select a `Lesson`
  3. Engage in a multi-turn tutoring session (> 5 turns) with Tutor where you consistently provide **wrong answers**
  4. After the tutoring session:
    - **No P1 unacceptable errors**:
      1. Tutor says a student's correct answer is wrong.
      2. Tutor says a student's wrong answer is correct.
      3. Posing incomplete questions (missing crucial info needed to answer).
    - **Science of Learning** - Evaluate the tutoring session against the principles of science of learning above (what's working, what's not working, areas for improvement)
  5. Review the Tutor prompts in `apps/tutoring/v2/services/move_prompts.py` and recommend specific improvements.
  6. Compile your results from Steps 4 and 5 above into an evaluation report and save it to `test-reports/` folder.

### Scenario: MATHS-S5: Advanced Geopgraphy Student

**Adopt Persona**:
- You are an advanced Geography student in Seychelles in `S5` i.e `Cycle 5`

**Workflow 1**: 
  1. Navigate to Tutor Frontend URL using `curl` OR `Playwright MCP` tools and use Test Student credentials to Sign In.
  2. Choose `Geography` and select a `Lesson`
  3. Engage in a multi-turn tutoring session (> 5 turns) with Tutor where you mostly provide **correct answers**
  4. After the tutoring session:
    - **No P1 unacceptable errors**:
      1. Tutor says a student's correct answer is wrong.
      2. Tutor says a student's wrong answer is correct.
      3. Posing incomplete questions (missing crucial info needed to answer).
    - **Science of Learning** - Evaluate the tutoring session against the principles of science of learning above (what's working, what's not working, areas for improvement)
  5. Review the Tutor prompts in `apps/tutoring/v2/services/move_prompts.py` and recommend specific improvements.
  6. Compile your results from Steps 4 and 5 above into an evaluation report and save it to `test-reports/` folder.