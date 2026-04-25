# Socratic Tutor Validator — Plan (2026-04-25)

## Problem

A live transcript showed three failure modes the math-tutor fix doesn't
catch because they're outside the math-only pipeline:

1. **Praise-without-working in non-math subjects.** Tutor said
   "Brilliant!" after a thin Geography answer ("Country B because, um,
   the money is going to the citizens..."). The math-tutor fix
   (M1–M9) only fires when `lesson.is_math` is True.
2. **Factual hallucination.** Tutor claimed Ghana has lower GNP than
   Seychelles. The student caught it. Tutor recovered, but a less alert
   student would have absorbed wrong info.
3. **Info-dump instead of Socratic dialogue.** After a student's
   answer, tutor delivered a paragraph introducing MEDC/LEDC, BRICS,
   and three specific numbers (Seychelles HDI 0.796, ranking 67/189)
   without checking comprehension between facts.

## Principle

**Assume the tutor is wrong until proven otherwise.** Every tutor
response runs through a validator pipeline before it's saved or sent
to the student. Issues are either soft-fixed (strip the offending
fragment) or hard-fail to a regeneration with constraints.

## Architecture

Cheap-to-expensive layered pipeline. Each layer fails fast.

```
Generated response
       ↓
[L1] Structural — does it end with a question? info-dump score?
       ↓
[L2] Pedagogical — praise present? gate it on a correctness check.
       ↓
[L3] Correctness — math: existing deterministic. Other subjects:
                   instructor-based LLM evaluator extended to all subjects.
       ↓
[L4] Factual claims — extract numeric/named claims, RAG-verify against
                      curriculum KB + Seychelles context library.
       ↓
[L5] Regenerate — if hard-fail (multiple issues OR L4 mismatch),
                  retry once with the issues injected as constraints.
       ↓
ValidatedResponse(content, issues_logged_to_metadata)
```

Most layers run locally (regex + parser + the deterministic check we
already have). LLM-based work happens only when needed:
- L3 (LLM correctness): already exists for math; extending to all
  subjects via the existing `_evaluate_step()` pipeline.
- L4 (RAG verification): only fires when response contains numeric
  claims or named comparisons.
- L5 (regenerate): capped at 1 retry per turn.

## Data persistence

`SessionTurn.metadata` already populated by the math fix. Extend with:

```json
{
  "validator_issues": ["unfounded_praise_stripped", "no_question"],
  "validator_passed": false,
  "validator_layers_run": ["structural", "pedagogical", "correctness"],
  "factual_claims_checked": 2,
  "factual_claims_unverified": []
}
```

Teacher dashboard "why was this marked correct" view (M4) extends to
"how was this validated" — making validator decisions auditable.

## Phased delivery

| Phase | Layers | Effort | Effect |
|---|---|---|---|
| **V1 — extend praise gate to all subjects** | L1 + L2 + L3 (extension) | 1d | Praise can no longer accompany an answer the LLM evaluator says is wrong, regardless of subject. End-of-response question check (soft warn). |
| **V2 — factual claim verification** | L4 | 2-3d | RAG-retrieves cited numbers / named entities from `KnowledgeBase` + `SeychellesContext`. Strips or flags unverified claims. |
| **V3 — regenerate on hard fail** | L5 | 1d | When ≥2 layers fail OR a fact mismatch is detected, regenerate once with `<validator_issues>` block in the system prompt. Cap retries to avoid latency runaway. |
| **V4 — teacher visibility** | dashboard | 0.5d | Validator badges in session_chat_history (alongside eval metadata). Surface flagged turns in flagged-sessions list. |

V1 ships first because it's the immediate fix for the chat the user
shared. V2 needs the curriculum KB, which exists. V3 is iteration. V4
is polish.

## File changes (V1)

**New file**: `apps/tutoring/validator.py`

```
class ValidationIssue(Enum):
    NO_QUESTION = "no_question"
    UNFOUNDED_PRAISE = "unfounded_praise_stripped"
    INFO_DUMP = "info_dump_warning"
    BARE_ANSWER_REWARD = "bare_answer_reward"
    ...

@dataclass
class ValidationResult:
    content: str
    issues: list[ValidationIssue]
    metadata: dict

def validate_tutor_response(
    response: str,
    student_input: str,
    is_correct: Optional[bool],
    bare_answer: bool,
    is_math: bool,
) -> ValidationResult:
    ...
```

**Modify**: `apps/tutoring/conversational_tutor.py`
- After `_parse_media_signal` + math praise filter, run
  `validate_tutor_response()`.
- Include the validator issues in `turn_metadata` written to
  `SessionTurn.metadata`.

**Modify**: math system prompt block — broaden the "no praise without
working" rule to all subjects (move it OUT of the math_teaching
block into a universal block).

**Tests**: `apps/tutoring/tests/test_validator.py`
- Praise present + is_correct=False → praise stripped, issue logged
- Praise present + is_correct=True → praise kept
- Response missing question + step is practice → soft warning logged
- Bare answer + praise → praise stripped (covered by existing math fix
  but now also for non-math)

## V2 plan sketch (for after V1)

- New helper `apps/tutoring/fact_verifier.py`
- Extract claim spans via regex (numbers, percentages, dollar figures,
  rankings) + named-entity heuristics
- For each claim, RAG-query the lesson's `KnowledgeBase` + the
  institution's `SeychellesContext` library
- If retrieved chunks contradict OR don't contain the claim, mark
  unverified. Strip or surround with `[unverified]` tag.

## Open questions / risks

1. **Latency budget**: V1 only adds local checks. V2 adds RAG (~50ms
   per claim against ChromaDB local mode). V3 adds a regeneration
   (1-3s). Stay under 5s total per turn.
2. **False positives** on praise stripping for non-math: the LLM
   evaluator (`_evaluate_step`) is not perfect. Risk of stripping
   legitimate "good thinking" feedback. Mitigation: only strip
   strong-affirmation patterns (already filtered list), keep mild
   acknowledgment.
3. **Tutor responses without questions**: not always wrong — the
   final wrap-up turn legitimately has no question. Layer 1 is a
   soft warning, not a hard fail.
4. **Cost**: V3 regeneration doubles LLM cost on a fail. Cap at 1
   retry; track regeneration rate as a key metric.
