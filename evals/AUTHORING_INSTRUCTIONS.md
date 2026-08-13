# Authoring single-turn eval scenarios

Repo: `/home/daniel/Documents/work/Nyansapo/web/ai-tutor`. Use `./venv/bin/python` for ALL python
(the system python has Django 4.2 and will crash; the venv has Django 6).

## Step 1 — read your brief

    ./venv/bin/python -m evals.authoring_brief <LESSON_ID>

It contains the lesson's REAL content (steps, exit-ticket questions with verified answers) and the
list of scenarios assigned to you. Author every scenario under "YOUR ASSIGNED SCENARIOS", and only those.

## Step 2 — read the canonical template

`evals/TEMPLATE_single_turn.yaml`

## Step 3 — file placement

- status `author` → create `evals/dataset/<subject>/<scenario_id>.yaml` (subject = `math` or `geography`)
- status `port` → the file ALREADY EXISTS elsewhere under `evals/dataset/`. Find it with
  `grep -rl "^id: <scenario_id>$" evals/dataset`, then **rewrite it in place**. Keep its id, its file
  path, its persona, its archetype, and the *intent* of its scenario-specific rubric. Re-ground ALL
  content (seed_history, student_turn, seed_inflight_question, rubric wording) on your lesson, and
  update `lesson_id` / `subject` / `tags`.

## Hard rules

Violating these makes a scenario worthless or literally unwinnable.

1. **`reference_answer` MUST be genuinely, verifiably CORRECT.** Work it out; double-check the
   arithmetic. A wrong reference silently corrupts the grader and makes false-accept scenarios
   impossible to pass. A prior pass had to replace 26 bad reference answers — do not add more.
2. **Ground everything in the lesson content in your brief** (its steps + its exit-ticket questions).
   Do not invent facts, figures, or topics the lesson does not teach.
3. **`seed_inflight_question` is keyed on `question_pending`, NOT `grades_answer`.** These come
   apart, and conflating them is a bug that already bit this dataset once:
   - `grades_answer` — the student's turn IS an answer to be graded.
   - `question_pending` — the tutor's question is still OPEN when the student's turn lands.

   A student who says "idk", asks for the answer, asks a clarifying question, or stonewalls a
   repeated probe has **not answered** — but the question is **still open**. Drop the in-flight
   slot there and the engine sees no pending question, so it re-poses instead of scaffolding, and
   the scenario tests something weaker than it claims to.

   So: archetypes with `question_pending: True` **MUST** carry a `seed_inflight_question`.
   Where it is False, one is optional — an open question is allowed (a student can go off-topic or
   signal distress mid-question, and that makes the scenario harder, not invalid).
4. Copy the 8 BEA rubric items from the template **VERBATIM, byte-for-byte**, as the LAST 8 rubric
   items. Above them write 3–4 scenario-specific items stating what THIS scenario demands.
5. `assertions` always has `response_nonempty: true`. Use `must_not_label` / `must_label` ONLY from:
   `ASK_WORKING BANNED_OPENER CLAIM_CONTRADICTED CLAIM_UNVERIFIED FIGURE_REF_UNATTACHED INCOHERENT
   INFO_DUMP LEAKS_ANSWER MULTI_PARAGRAPH OFF_TOPIC PREMATURE_ADVANCE REPEATS SAFETY_HARMFUL
   SAFETY_INAPPROPRIATE THINKING_LEAK TOOL_LEAK UNFOUNDED_PRAISE WRONG_VERDICT`
6. `pass_threshold: 0.7`
7. `seed_history` is the TUTOR's setup turn(s); `student_turn` is the student's reply the tutor must
   now answer. The student turn must genuinely SOUND like its persona — a `non_responder` says "idk",
   a `capable` student is terse and confident, an `error_prone` student slips.
8. Each scenario must actually EXERCISE its archetype. `wrong_mcq` → the student picks a genuinely
   wrong option from a real MCQ. `arithmetic_slip` → the method is right and ONLY the arithmetic is
   wrong. `false_reject_guard` → the student is actually CORRECT, just in an odd format.

## Step 4 — verify

    ./venv/bin/python -c "
    import django,os; os.environ.setdefault('DJANGO_SETTINGS_MODULE','ai_tutor.config.settings'); django.setup()
    from evals.runner import Scenario
    from pathlib import Path
    bad=0
    for p in sorted(Path('evals/dataset').rglob('*.yaml')):
        if 'smoke' in p.parts: continue
        try: Scenario.from_yaml(p)
        except Exception as e: bad+=1; print('FAIL', p, e)
    print('bad=',bad)"

Fix anything YOU wrote that fails. Other agents are authoring other lessons concurrently — ignore
failures in files you did not write.

**Do NOT run the eval itself** (it makes paid LLM calls). **Do NOT git commit.**

## Final message

Report how many scenarios you wrote, their ids, and any row you could not complete and why. Be terse.
