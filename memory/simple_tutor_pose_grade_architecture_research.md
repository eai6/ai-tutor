# Simple Tutor: Pose vs Grade Architecture Research

Started 2026-05-26. Owner: Edward. Pairs with `simple_tutor_engine_plan.md`,
`simple_tutor_engine_milestones.md`. Sits between M11.3 (LLM-provided
reference grading, just shipped) and the next architectural decision.

## Problem statement

A single tutor LLM call regularly produces a turn that contains **both**
(a) a confirmation of the student's previous answer **and** (b) a new
question. On the next student turn, that same LLM has to decide which
question is "in flight" and grade the new student utterance against it —
and it consistently mis-identifies, grading the new answer against the
just-confirmed previous reference. Server-side anchors, attribute markers
on turns, tool-result reformatting, and pool filtering each chip away at
some failure modes but never eliminate the drift. Root cause: the LLM
owns both **posing** and **identifying-what-to-grade**, and these
responsibilities cross-contaminate inside one attention pass.

## Survey — how production systems handle pose / grade

**Khan Academy Khanmigo.** Public posts confirm a hybrid: the LLM
generates Socratic prompts, but a separate **"math agent"** verifies
calculations in real time, and the dialogue layer fetches
**pre-authored exercises, steps, hints, and solutions** before
responding. Khan's blog explicitly flags that accuracy improved when
they **forced Khanmigo to gather context from these structured sources
prior to responding**. Translation: question authoring is *not* free —
the LLM consumes pre-authored items and the grading path is detached.
([Khan Academy blog, 2025](https://blog.khanacademy.org/khanmigo-math-computation-and-tutoring-updates/),
[Khanmigo LLM overview](https://support.khanacademy.org/hc/en-us/articles/13888935335309))

**Duolingo Max — Roleplay & Explain My Answer.** Roleplay scenarios
and initial prompts are **human-authored**; GPT-4 only generates
intra-conversation variation. Grading lives in Duolingo's existing NLP
pipeline (the same one that grades the non-Max lesson). "Explain My
Answer" *activates on errors* — i.e., the grade verdict from the
deterministic pipeline triggers the LLM, not the other way around.
Pose and grade are in completely separate systems.
([Duolingo blog](https://blog.duolingo.com/duolingo-max/),
[OpenAI Duolingo case study](https://openai.com/index/duolingo/))

**OpenAI Study Mode.** Pure system-prompt approach — no tool calls,
no persisted question slot, designed for guided discovery not
assessment. Confirms by negative result that prompt-only doesn't fix
the bug we're hitting — Study Mode hallucinates verdicts because it
has no anchor. ([OpenAI Study Mode](https://openai.com/index/chatgpt-study-mode/))

**MWPTutor (Chowdhury et al., BEA 2024).** The closest published
architectural match. Pre-defined **finite-state transducer** with four
dialogue moves: **pump → hint → prompt → assertion**. Each FST state
maps to a **separate LLM call**. A solution tree holds the current
expectation; **Algorithm 2** does numerical matching of student
utterance against the expected RHS — *not the LLM*. Validation gates
regenerate the LLM output if a "hint" leaks the answer or an
"assertion" omits it. Pose and grade are structurally separate;
the LLM never gets to mis-identify which step is in flight because
the FST state IS the in-flight step.
([arxiv 2402.09216](https://arxiv.org/abs/2402.09216))

**Training Turn-by-Turn Verifiers (Mroueh et al., EMNLP 2024
Findings).** Generator-verifier split. Generator (LLM) emits *N*
candidate utterances; a separately fine-tuned Mistral-7B **verifier**
scores each on dialogue-progress reward. Verifier sees task + dialogue
context + candidate utterance; generator never sees verifier rationale.
Multi-call architecture: per turn = N generations + N verifier passes.
Relevant because it confirms the production pattern of *quality
judgment as a distinct LLM call from generation*.
([arxiv 2502.13311](https://arxiv.org/html/2502.13311))

**Anthropic Claude / Managed Agents.** No published educational
dialogue architecture, but Managed Agents (2026) keeps session state
*outside* the model — append-only log, harness-owned tool routing.
"Building Effective Agents" recommends routing (classify → specialized
prompt per phase) and notes "LLMs generally perform better when each
consideration is handled by a separate LLM call." Direct read on our
bug: posing and identifying are two considerations; one call is
structurally wrong.
([Building Effective Agents](https://www.anthropic.com/engineering/building-effective-agents),
[Managed Agents](https://www.anthropic.com/engineering/managed-agents))

## Relevant recent papers

- **MWPTutor / AutoTutor-LLM (Chowdhury et al., 2024)** — covered above.
  Handcrafted FST + LLM-as-slot-filler beats free-form LLM at avoiding
  answer leakage and step-confusion. Our content-generation pipeline
  already produces structured step+question records, so we have the
  FST data for free. ([arxiv 2402.09216](https://arxiv.org/abs/2402.09216))

- **Training Turn-by-Turn Verifiers (EMNLP 2024 Findings)** —
  generator/verifier split as a standard pattern.
  ([arxiv 2502.13311](https://arxiv.org/html/2502.13311))

- **Knowledge Tracing in Tutor-Student Dialogues (LAK 2025, Scarlatos
  et al.)** — CoMTA dataset (188 Khanmigo transcripts). Knowledge-
  component identification per turn requires a *separate* LLM annotator,
  never the tutor itself.
  ([LAK 2025](https://learninganalytics.upenn.edu/ryanbaker/Dialogue_KT_LAK_25-2.pdf))

- **Pedagogical Ability Assessment of AI Tutors (BEA 2025 Shared
  Task)** — submissions converged on dual-encoder / classifier
  architectures separate from the tutor model. The thing judging the
  turn is not the thing producing the turn.
  ([BEA 2025](https://arxiv.org/html/2507.10579v1))

## Anthropic-specific patterns relevant to our case

**`tool_choice` forcing.** Three modes matter for us:

- `auto` (current): Claude decides whether to call a tool. We've seen
  it skip `record_answer` and just emit a question.
- `any`: Claude *must* call one of the provided tools but picks which.
  Useful if "ask question" and "grade answer" are both tools.
- `tool` (specific): Forces a named tool. Cookbook example shows
  `stop_reason` becomes `tool_use` and **all communication flows
  through the tool's parameters** — Claude can't emit standalone text
  outside the tool call.
  ([Claude tool_choice cookbook](https://platform.claude.com/cookbook/tool-use-tool-choice))

This is the architectural lever: if posing a question is a forced
`pose_question` tool call, the question text + reference answer +
type land in a structured record the server persists, *not* in
free prose that the next turn's LLM has to re-parse.

**Tool naming + unambiguous schemas.** Anthropic's "Writing Tools for
Agents" essay: parameter names should be self-disambiguating
(`user_id` not `user`), tools should be namespaced. Applied to us:
`pose_question` (intent: write new in-flight question) and
`record_answer` (intent: grade student input against the
already-persisted in-flight question) are unambiguous; today's
`record_answer(question_text, reference_answer)` is ambiguous
because the LLM picks which question to put there.
([Writing tools for agents](https://www.anthropic.com/engineering/writing-tools-for-agents))

**Routing / separation of concerns.** "Building Effective Agents"
recommends per-phase prompts. Maps cleanly to: a "narrator" prompt
generates the conversational reply; the act of posing/grading is
structured tool I/O around it.

## Proposed architecture: `pose_question` tool

### Tool schemas

```python
TOOLS = [
    {
        "name": "pose_question",
        "description": (
            "Call when you want to ask the student a question that "
            "will be graded. After this call returns, the question is "
            "persisted as the session's in-flight question; the "
            "student's next reply will be graded against the "
            "reference_answer you provide here. Do NOT also include "
            "the question in your text reply — the engine will render "
            "it from this tool call."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "question_text": {"type": "string"},
                "question_type": {"enum": ["mcq","numeric","math","short","matching"]},
                "options": {"type": "array", "items": {"type": "string"}},
                "reference_answer": {"type": "string"},
                "source": {"enum": ["catalog", "inline_authored"]},
                "catalog_question_id": {"type": ["integer", "null"]},
            },
            "required": ["question_text", "question_type",
                         "reference_answer", "source"],
        },
    },
    {
        "name": "record_answer",
        "description": (
            "Call when the student has answered the current in-flight "
            "question. Pass the extracted answer text. The engine "
            "grades it against the persisted in-flight question — you "
            "do NOT supply the reference here."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "extracted_answer": {"type": "string"},
            },
            "required": ["extracted_answer"],
        },
    },
    # request_figure, redirect_off_topic, advance_step unchanged
]
```

### Engine flow

```python
def respond(session, user_input):
    # 1. If there's an in-flight question, the LLM is in "grade" mode.
    #    If not, the LLM is in "pose" mode. System prompt is templated
    #    on this state.
    in_flight = session.in_flight_question  # nullable FK / JSON

    # 2. Build prompt:
    #    - If in_flight: include the persisted question + reference
    #      verbatim in the prompt; instruct LLM "the student is
    #      replying to THIS question; call record_answer if they
    #      answered it."
    #    - If no in_flight: instruct LLM "no question is in flight;
    #      teach / scaffold / pose the next question via pose_question."

    # 3. Single LLM call. tool_choice='auto' (not forced) — text replies
    #    are still valid when teaching/scaffolding without grading.

    # 4. Dispatch tool calls:
    for tc in response.tool_calls:
        if tc.name == 'pose_question':
            # MUST clear in_flight first, then set the new one.
            # If in_flight was non-null and not yet graded, log
            # 'posed_without_grading' analytics event.
            session.in_flight_question = create_question(**tc.input)
            session.save()
        elif tc.name == 'record_answer':
            q = session.in_flight_question
            if q is None:
                # LLM called record_answer with nothing in flight -
                # safe-fail: return error tool_result so LLM corrects.
                emit_tool_result(tc, {"error": "no in-flight question"})
            else:
                verdict = grader.grade(question=q,
                                       student_answer=tc.input['extracted_answer'])
                persist_verdict(session, q, verdict)
                session.in_flight_question = None  # cleared
                session.save()
                emit_tool_result(tc, verdict.to_dict())

    # 5. Auto-fallback unchanged: if no record_answer but in_flight
    #    is set and user_input looks answer-y, grade it server-side.

    # 6. Auto-advance step unchanged.
```

### What's persisted

A new model or JSON slot:

```python
class InFlightQuestion(models.Model):
    session = models.OneToOneField(TutorSession, ...)
    question_text = models.TextField()
    question_type = models.CharField(...)
    options = models.JSONField(default=list)
    reference_answer = models.TextField()
    source = models.CharField(choices=[('catalog','catalog'),
                                       ('inline_authored','inline_authored')])
    catalog_question_id = models.IntegerField(null=True)
    posed_at_turn_id = models.IntegerField()
    posed_at = models.DateTimeField(auto_now_add=True)
```

Single-row per session. Cleared on `record_answer`, on `pose_question`
(which overwrites), or on `advance_step`. The `posed_at_turn_id` is the
audit trail — every verdict cites the turn that posed.

### Subsequent-turn invariant

When the LLM is called for turn N+1 and `in_flight_question` is set:
- The system prompt contains the persisted question text + reference
  verbatim, framed as "**THIS** is the question the student is replying
  to."
- The LLM never has to re-identify which question is in flight — the
  server already did, and the prompt is unambiguous.
- The LLM still has discretion: it can call `record_answer` (student
  answered), emit text only (student asked a clarifier), or call
  `pose_question` to abandon the in-flight question and ask a new one
  (with the `posed_without_grading` analytics flag).

## Comparison to M11.3 (current state)

**M11.3 today.** `record_answer(extracted_answer, reference_answer,
question_type, question_text)` — LLM supplies all four. No server-side
in-flight question. Pose is implicit in free prose.

| Dimension | M11.3 (now) | Proposed (`pose_question`) |
|---|---|---|
| In-flight question source of truth | LLM's prose memory | Server DB row |
| Reference answer per grade | LLM-supplied per call | Persisted from posing turn |
| Risk of grading-against-wrong-question | High (the bug) | Eliminated structurally |
| Inline-authored questions | Already handled (LLM writes the ref) | Still handled (`source='inline_authored'`) |
| LLM cognitive load | Pose + grade + identify in one turn | Pose XOR grade per turn |
| Schema rigidity | Loose | Stricter — requires every grade to cite a posed question |
| Migration cost | None — already shipped | One model + one migration + engine refactor + prompt rewrite |
| Auto-fallback grading | Works (uses LLM-supplied ref) | Works better (uses persisted ref) |
| Debuggability | "Why did it grade against X?" hard to answer | `posed_at_turn_id` audit trail |

**Pros.** Eliminates the identification step the LLM keeps mis-doing.
Persisted ref survives retries / regen / context churn. Inline-authoring
becomes a structured signal (`source='inline_authored'`), not a
heuristic. Every verdict has an audit pointer to the posing turn.

**Cons.** More moving parts: one model, one migration, prompt rewrite,
two handlers instead of one. LLM may still pose in prose (mitigation:
post-check text for question marks when no `pose_question` fired).
Inline-authored references can still be bad (same risk as M11.3).
Multi-question turns become structurally impossible — probably a feature
for pedagogy.

## Open questions / risks of the new design

1. **Rhetorical vs graded questions.** Tool description must make
   `pose_question` = "graded" vs prose questions = "rhetorical/Socratic"
   sharp. Anti-pattern: LLM uses the tool for rhetoricals and clutters
   the in-flight slot.

2. **Catalog vs inline split.** Keep allowing inline-authored, gated
   by `source`. Track inline-rate per lesson — high rate signals
   curriculum gaps.

3. **Force the tool via `tool_choice='any'`?** Risky — blocks pure-
   teaching turns. Keep `auto`, prompt strongly, monitor "posed in
   prose" rate via post-check.

4. **Migration path.** Ship behind a per-session flag (mirrors the
   existing `engine` field). Shadow-grade on the eval benchmark:
   M11.3 grader vs new persisted-ref grader on identical sessions,
   compare verdicts before flipping default.

5. **Verifier scope unchanged.** The Tier-2 verifier LLM still
   receives only persisted question + reference + extracted_answer,
   never the conversation (`feedback_grading_design_rules`).

6. **Don't over-FSM.** MWPTutor's authors flag rule-authoring as the
   scaling pain. One in-flight slot + one pose tool + one record tool
   is the right amount of structure. Resist adding more first-class
   states ("clarifying", "remediating", "summarizing") — let the LLM
   narrate those in prose.

---

Refs: `simple_tutor_engine_plan.md`, `simple_tutor_engine_milestones.md`,
`grading_system_research.md`, `auto-memory/feedback_grading_design_rules.md`,
`auto-memory/feedback_server_owns_question_state.md`,
`auto-memory/feedback_deterministic_grading.md`.
