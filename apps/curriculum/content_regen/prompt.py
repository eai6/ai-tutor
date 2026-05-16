"""Focused regen prompt for lesson-step rewrites.

Single LLM call: rewrite a flagged step so the named factual
violations are fixed while preserving the original pedagogical intent
and the lesson context.

Why this lives separately (mirrors `apps/tutoring/regen/prompt.py`):
  - The regen prompt is decoupled from the orchestrator so we can A/B
    different prompts against the benchmark without touching cycle
    logic.
  - The original generation prompt (`generate_lesson_content`) is
    ~30KB and oriented toward whole-lesson generation — terrible for
    a focused single-step rewrite. This prompt is ~1KB and ONLY says:
    here's the bad text, here's why it's bad, here's the lesson
    context, give me a rewrite of just this step.

Prompting design (per gemini-prompting-expert + prompting-fundamentals):
  - Direct task statement, no flowery persona
  - Positive framing — "Produce a rewrite that..." not "Don't..."
  - Quantified caps (~80-150 words for the rewrite)
  - Long-context query-last: original text + violations come AFTER
    the lesson context
  - XML structure for the input blocks (cross-provider portable)
  - No CoT scaffolding — the generator is a reasoning model that
    handles internal planning; we just want clean output
"""

from __future__ import annotations

from typing import Any, Dict


_REGEN_SYSTEM = """\
Rewrite one lesson step's narrative text so the named factual issues \
are fixed while preserving the step's pedagogical intent and grade level.

Output ONLY the rewritten narrative — no preamble, no explanation, no \
JSON wrapper, no XML tags, no code fences. The rewrite goes directly \
into the lesson and will be read aloud by the AI tutor.

Constraints on the rewrite:
  - Same length and pedagogical role as the original (open with the \
same kind of hook, end with the same kind of transition).
  - Address every claim listed under <violations>: contradicted claims \
must be REPLACED with correct values from <recommended_fix>; \
unsupported claims must be either removed or rephrased to avoid \
asserting a fact the curriculum doesn't support.
  - Preserve any place names, vocabulary, or examples that are \
correct in the original.
  - Match the grade level described under <lesson> — same vocabulary \
band, same sentence complexity.
  - Use the lesson and step objectives to keep the narrative on-topic.
"""


def build_step_regen_prompt(
    *,
    original_text: str,
    judge_result: Dict[str, Any],
    lesson_subject: str = "",
    lesson_grade: str = "",
    lesson_title: str = "",
    lesson_objective: str = "",
    step_objective: str = "",
    step_concept_tag: str = "",
) -> Dict[str, str]:
    """Compose the regen prompt.

    Returns a dict with `system` + `user` keys ready to feed
    `BaseLLMClient.generate(messages=[{role: user, content: user}],
    system_prompt=system, ...)`.

    Long-context query-last: lesson context first, then violation
    detail, then the original text, with the imperative ("rewrite")
    at the very bottom.
    """
    violations = judge_result.get('violations') or []
    reasoning = (judge_result.get('reasoning') or '').strip()
    recommended_fix = (judge_result.get('recommended_fix') or '').strip()

    user = (
        "<lesson>\n"
        f"  <subject>{(lesson_subject or '(unspecified)').strip()[:120]}</subject>\n"
        f"  <grade>{(lesson_grade or '(unspecified)').strip()[:80]}</grade>\n"
        f"  <title>{(lesson_title or '(unspecified)').strip()[:200]}</title>\n"
        f"  <overall_objective>{(lesson_objective or '(none)').strip()[:400]}</overall_objective>\n"
        "</lesson>\n"
        "<step>\n"
        f"  <concept>{(step_concept_tag or '(unspecified)').strip()[:200]}</concept>\n"
        f"  <objective>{(step_objective or '(none)').strip()[:400]}</objective>\n"
        "</step>\n"
        "<violations>\n"
        f"  <codes>{', '.join(violations) if violations else '(none)'}</codes>\n"
        f"  <judge_reasoning>{reasoning[:400]}</judge_reasoning>\n"
        f"  <recommended_fix>{recommended_fix[:600]}</recommended_fix>\n"
        "</violations>\n"
        "<original_text>\n"
        f"{(original_text or '').strip()[:2000]}\n"
        "</original_text>\n"
        "\n"
        "Based on the lesson context, the violations to address, and "
        "the original text above, produce a rewritten narrative that "
        "(a) corrects every contradicted claim using the recommended_fix "
        "guidance, (b) removes or rephrases unsupported claims, "
        "(c) preserves the original pedagogical structure and grade "
        "level. Output the rewritten narrative ONLY — nothing else."
    )

    return {
        "system": _REGEN_SYSTEM,
        "user": user,
    }


_PROMPT_REGEN_SYSTEM = """\
Rewrite one lesson step's narrative text following the teacher's \
guidance. The teacher knows the class and what change they want — \
apply their guidance literally.

Output ONLY the rewritten narrative — no preamble, no explanation, no \
JSON wrapper, no XML tags, no code fences. The rewrite goes directly \
into the lesson and will be read aloud by the AI tutor.

Constraints on the rewrite:
  - Apply the teacher's guidance as the primary driver of changes.
  - Preserve the step's pedagogical role (open with the same kind of \
hook, end with the same kind of transition) UNLESS the guidance \
explicitly changes them.
  - Match the lesson grade band — same vocabulary, same sentence \
complexity — UNLESS the guidance asks for a shift.
  - Stay on-topic for the lesson and step objectives.
  - Preserve any place names, vocabulary, or examples that the \
guidance does not explicitly remove.
"""


def build_step_prompt_regen_prompt(
    *,
    original_text: str,
    teacher_guidance: str,
    lesson_subject: str = "",
    lesson_grade: str = "",
    lesson_title: str = "",
    lesson_objective: str = "",
    step_objective: str = "",
    step_concept_tag: str = "",
) -> Dict[str, str]:
    """Compose the teacher-prompt-driven regen prompt.

    Returns a dict with `system` + `user` keys for
    `BaseLLMClient.generate(messages=[{role: user, content: user}],
    system_prompt=system, ...)`.

    Long-context query-last: lesson context first, then teacher
    guidance, then the original text, with the imperative ("rewrite")
    at the very bottom.
    """
    user = (
        "<lesson>\n"
        f"  <subject>{(lesson_subject or '(unspecified)').strip()[:120]}</subject>\n"
        f"  <grade>{(lesson_grade or '(unspecified)').strip()[:80]}</grade>\n"
        f"  <title>{(lesson_title or '(unspecified)').strip()[:200]}</title>\n"
        f"  <overall_objective>{(lesson_objective or '(none)').strip()[:400]}</overall_objective>\n"
        "</lesson>\n"
        "<step>\n"
        f"  <concept>{(step_concept_tag or '(unspecified)').strip()[:200]}</concept>\n"
        f"  <objective>{(step_objective or '(none)').strip()[:400]}</objective>\n"
        "</step>\n"
        "<teacher_guidance>\n"
        f"{(teacher_guidance or '').strip()[:600]}\n"
        "</teacher_guidance>\n"
        "<original_text>\n"
        f"{(original_text or '').strip()[:2000]}\n"
        "</original_text>\n"
        "\n"
        "Based on the lesson context above and the teacher's guidance, "
        "produce a rewritten narrative of the original_text. Apply the "
        "guidance literally. Output the rewritten narrative ONLY — "
        "nothing else."
    )

    return {
        "system": _PROMPT_REGEN_SYSTEM,
        "user": user,
    }


_EXIT_Q_PROMPT_REGEN_SYSTEM = """\
Rewrite ONE multiple-choice exit-ticket question following the \
teacher's guidance. Keep the question pedagogically sound and \
matched to the lesson and step objectives.

Output MUST be a single JSON object — no preamble, no explanation, \
no XML tags, no code fences. Schema:

{
  "question_text": "<the question stem>",
  "option_a": "<answer choice A>",
  "option_b": "<answer choice B>",
  "option_c": "<answer choice C>",
  "option_d": "<answer choice D>",
  "correct_answer": "A" | "B" | "C" | "D",
  "explanation": "<one short sentence on why the correct option is right>"
}

Constraints on the rewrite:
  - Apply the teacher's guidance literally.
  - Keep all four options non-empty and meaningfully distinct — \
distractors must be plausible-but-wrong, not absurd or off-topic.
  - The correct_answer letter must point to the option that is \
actually correct given the rewritten stem.
  - Match the lesson grade band — vocabulary and complexity.
  - Stay on-topic for the lesson and step objectives.
  - Preserve the original question_type (this is an MCQ).
"""


def build_exit_q_prompt_regen_prompt(
    *,
    original_question: Dict[str, Any],
    teacher_guidance: str,
    lesson_subject: str = "",
    lesson_grade: str = "",
    lesson_title: str = "",
    lesson_objective: str = "",
    step_concept_tag: str = "",
    enabling_objective: str = "",
) -> Dict[str, str]:
    """Compose the teacher-prompt-driven regen prompt for one MCQ.

    `original_question` should be a dict with: question_text, option_a,
    option_b, option_c, option_d, correct_answer, explanation. Missing
    keys render as empty.
    """
    user = (
        "<lesson>\n"
        f"  <subject>{(lesson_subject or '(unspecified)').strip()[:120]}</subject>\n"
        f"  <grade>{(lesson_grade or '(unspecified)').strip()[:80]}</grade>\n"
        f"  <title>{(lesson_title or '(unspecified)').strip()[:200]}</title>\n"
        f"  <overall_objective>{(lesson_objective or '(none)').strip()[:400]}</overall_objective>\n"
        "</lesson>\n"
        "<question_context>\n"
        f"  <concept>{(step_concept_tag or '(unspecified)').strip()[:200]}</concept>\n"
        f"  <enabling_objective>{(enabling_objective or '(none)').strip()[:400]}</enabling_objective>\n"
        "</question_context>\n"
        "<teacher_guidance>\n"
        f"{(teacher_guidance or '').strip()[:600]}\n"
        "</teacher_guidance>\n"
        "<original_question>\n"
        f"  <question_text>{(original_question.get('question_text') or '').strip()[:600]}</question_text>\n"
        f"  <option_a>{(original_question.get('option_a') or '').strip()[:200]}</option_a>\n"
        f"  <option_b>{(original_question.get('option_b') or '').strip()[:200]}</option_b>\n"
        f"  <option_c>{(original_question.get('option_c') or '').strip()[:200]}</option_c>\n"
        f"  <option_d>{(original_question.get('option_d') or '').strip()[:200]}</option_d>\n"
        f"  <correct_answer>{(original_question.get('correct_answer') or '').strip()[:1]}</correct_answer>\n"
        f"  <explanation>{(original_question.get('explanation') or '').strip()[:300]}</explanation>\n"
        "</original_question>\n"
        "\n"
        "Based on the lesson context above and the teacher's guidance, "
        "produce a rewritten MCQ as a single JSON object matching the "
        "schema in the system instruction. Output the JSON ONLY — "
        "nothing else, no fences, no preamble."
    )

    return {
        "system": _EXIT_Q_PROMPT_REGEN_SYSTEM,
        "user": user,
    }


_MULTI_JUDGE_REGEN_SYSTEM = """\
Rewrite one lesson step's narrative text so EVERY violation listed by \
EVERY judge is fixed simultaneously, while preserving the step's \
pedagogical intent and grade level.

Output ONLY the rewritten narrative — no preamble, no explanation, no \
JSON wrapper, no XML tags, no code fences. The rewrite goes directly \
into the lesson and will be read aloud by the AI tutor.

Each <violations> block names a judge and lists what it flagged. Treat \
the union of all flagged issues as a single rewrite brief — do not \
fix one judge while introducing a new violation for another.

Constraints on the rewrite:
  - Same length and pedagogical role as the original (open with the \
same kind of hook, end with the same kind of transition).
  - factual_step violations: contradicted claims must be REPLACED with \
correct values; unsupported claims must be removed or rephrased to \
avoid asserting facts the curriculum doesn't support.
  - pedagogy_step violations:
      PEDAGOGY_OFF_OBJECTIVE → tighten the narrative to the step \
objective; remove tangents.
      PEDAGOGY_DOK_MISMATCH → adjust cognitive demand to the step's \
DOK level (e.g. recall vs apply vs analyse).
      PEDAGOGY_NO_LEARNING_PROMPT → end with a clear prompt for the \
student to think, predict, or attempt something.
      PEDAGOGY_VOCAB_MISMATCH → match the grade-level vocabulary band.
  - safety_content violations: remove the flagged content cleanly; \
do not preach about it.
  - Preserve correct place names, vocabulary, and examples from the \
original.
  - Match the grade level — same vocabulary band, same sentence \
complexity.
"""


def build_step_multi_judge_regen_prompt(
    *,
    original_text: str,
    judge_results: Dict[str, Dict[str, Any]],
    lesson_subject: str = "",
    lesson_grade: str = "",
    lesson_title: str = "",
    lesson_objective: str = "",
    step_objective: str = "",
    step_concept_tag: str = "",
) -> Dict[str, str]:
    """Compose a step-regen prompt that addresses violations from
    multiple judges in one rewrite.

    `judge_results` is a mapping `{judge_name: verdict_dict}` where
    each verdict_dict has keys `violations`, `reasoning`,
    `recommended_fix`, `passed`. Only judges with `passed=False AND
    not skipped` are included in the <violations> block — clean
    judges are mentioned only as "no issues" so the model knows the
    constraint exists but doesn't have to act on it.

    Long-context query-last: lesson context first, then per-judge
    violation detail, then the original text, with the imperative at
    the bottom.
    """
    judge_blocks = []
    for judge_name, verdict in (judge_results or {}).items():
        if not isinstance(verdict, dict):
            continue
        if verdict.get('skipped'):
            continue
        if verdict.get('passed'):
            continue
        violations = verdict.get('violations') or []
        if not violations:
            continue
        reasoning = (verdict.get('reasoning') or '').strip()
        recommended_fix = (verdict.get('recommended_fix') or '').strip()
        judge_blocks.append(
            f"  <{judge_name}>\n"
            f"    <codes>{', '.join(violations)}</codes>\n"
            f"    <judge_reasoning>{reasoning[:400]}</judge_reasoning>\n"
            f"    <recommended_fix>{recommended_fix[:600]}</recommended_fix>\n"
            f"  </{judge_name}>"
        )
    violations_xml = (
        "\n".join(judge_blocks) if judge_blocks else "  (none — already clean)"
    )

    user = (
        "<lesson>\n"
        f"  <subject>{(lesson_subject or '(unspecified)').strip()[:120]}</subject>\n"
        f"  <grade>{(lesson_grade or '(unspecified)').strip()[:80]}</grade>\n"
        f"  <title>{(lesson_title or '(unspecified)').strip()[:200]}</title>\n"
        f"  <overall_objective>{(lesson_objective or '(none)').strip()[:400]}</overall_objective>\n"
        "</lesson>\n"
        "<step>\n"
        f"  <concept>{(step_concept_tag or '(unspecified)').strip()[:200]}</concept>\n"
        f"  <objective>{(step_objective or '(none)').strip()[:400]}</objective>\n"
        "</step>\n"
        "<violations>\n"
        f"{violations_xml}\n"
        "</violations>\n"
        "<original_text>\n"
        f"{(original_text or '').strip()[:2000]}\n"
        "</original_text>\n"
        "\n"
        "Based on the lesson context above and the per-judge violations, "
        "produce a single rewritten narrative that addresses EVERY "
        "violation across ALL judges in one pass. Do not fix one judge "
        "while introducing a new violation for another. Output the "
        "rewritten narrative ONLY — nothing else."
    )

    return {
        "system": _MULTI_JUDGE_REGEN_SYSTEM,
        "user": user,
    }


_EXIT_Q_AUTO_REGEN_SYSTEM = """\
Rewrite ONE multiple-choice exit-ticket question so the named \
violations are fixed while keeping the question pedagogically sound \
and matched to the lesson and step objectives.

Output MUST be a single JSON object — no preamble, no explanation, \
no XML tags, no code fences. Schema:

{
  "question_text": "<the question stem>",
  "option_a": "<answer choice A>",
  "option_b": "<answer choice B>",
  "option_c": "<answer choice C>",
  "option_d": "<answer choice D>",
  "correct_answer": "A" | "B" | "C" | "D",
  "explanation": "<one short sentence on why the correct option is right>"
}

Violation guidance:
  EXITQ_OFF_OBJECTIVE → re-anchor the stem to the enabling_objective; \
remove tangents.
  EXITQ_AMBIGUOUS_KEY → make exactly one option clearly correct; \
sharpen distractors so they're plausibly wrong, not also-correct.
  EXITQ_TRIVIAL_DISTRACTORS → replace absurd or off-topic distractors \
with plausible-but-wrong choices a student might pick.
  EXITQ_DOK_MISMATCH → adjust cognitive demand to the lesson's DOK level.
  EXITQ_FACT_CONTRADICTED → replace incorrect claims in the stem or \
explanation with curriculum-supported values.

Constraints on the rewrite:
  - Keep all four options non-empty and meaningfully distinct.
  - The correct_answer letter must point to the option that is \
actually correct given the rewritten stem.
  - Match the lesson grade band — vocabulary and complexity.
  - Stay on-topic for the lesson and enabling_objective.
"""


def build_exit_q_auto_regen_prompt(
    *,
    original_question: Dict[str, Any],
    judge_result: Dict[str, Any],
    lesson_subject: str = "",
    lesson_grade: str = "",
    lesson_title: str = "",
    lesson_objective: str = "",
    step_concept_tag: str = "",
    enabling_objective: str = "",
) -> Dict[str, str]:
    """Compose a judge-driven (not teacher-driven) regen prompt for
    one MCQ.

    `original_question` is a dict with `question_text`, `option_a`..d,
    `correct_answer`, `explanation`. `judge_result` is the
    exit_question verdict dict — its `violations`, `reasoning`,
    `recommended_fix` drive the rewrite brief.
    """
    violations = judge_result.get('violations') or []
    reasoning = (judge_result.get('reasoning') or '').strip()
    recommended_fix = (judge_result.get('recommended_fix') or '').strip()

    user = (
        "<lesson>\n"
        f"  <subject>{(lesson_subject or '(unspecified)').strip()[:120]}</subject>\n"
        f"  <grade>{(lesson_grade or '(unspecified)').strip()[:80]}</grade>\n"
        f"  <title>{(lesson_title or '(unspecified)').strip()[:200]}</title>\n"
        f"  <overall_objective>{(lesson_objective or '(none)').strip()[:400]}</overall_objective>\n"
        "</lesson>\n"
        "<question_context>\n"
        f"  <concept>{(step_concept_tag or '(unspecified)').strip()[:200]}</concept>\n"
        f"  <enabling_objective>{(enabling_objective or '(none)').strip()[:400]}</enabling_objective>\n"
        "</question_context>\n"
        "<violations>\n"
        f"  <codes>{', '.join(violations) if violations else '(none)'}</codes>\n"
        f"  <judge_reasoning>{reasoning[:400]}</judge_reasoning>\n"
        f"  <recommended_fix>{recommended_fix[:600]}</recommended_fix>\n"
        "</violations>\n"
        "<original_question>\n"
        f"  <question_text>{(original_question.get('question_text') or '').strip()[:600]}</question_text>\n"
        f"  <option_a>{(original_question.get('option_a') or '').strip()[:200]}</option_a>\n"
        f"  <option_b>{(original_question.get('option_b') or '').strip()[:200]}</option_b>\n"
        f"  <option_c>{(original_question.get('option_c') or '').strip()[:200]}</option_c>\n"
        f"  <option_d>{(original_question.get('option_d') or '').strip()[:200]}</option_d>\n"
        f"  <correct_answer>{(original_question.get('correct_answer') or '').strip()[:1]}</correct_answer>\n"
        f"  <explanation>{(original_question.get('explanation') or '').strip()[:300]}</explanation>\n"
        "</original_question>\n"
        "\n"
        "Based on the lesson context, the enabling objective, and the "
        "violation detail, produce a rewritten MCQ as a single JSON "
        "object matching the schema in the system instruction. Output "
        "the JSON ONLY — no preamble, no fences."
    )

    return {
        "system": _EXIT_Q_AUTO_REGEN_SYSTEM,
        "user": user,
    }


__all__ = [
    "build_step_regen_prompt",
    "build_step_multi_judge_regen_prompt",
    "build_step_prompt_regen_prompt",
    "build_exit_q_prompt_regen_prompt",
    "build_exit_q_auto_regen_prompt",
]
