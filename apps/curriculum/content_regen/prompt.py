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

from apps.curriculum.locale_prompts import locale_instruction_block


def _with_locale(system_text: str, locale: str) -> str:
    """Append the course's locale instruction block to a regen system
    prompt so the rewrite stays in the source language.

    Closes the bug Edward caught 2026-06-01: M5-prep made the
    *generator* locale-aware via apps/curriculum/locale_prompts.py,
    but the *regen* pipeline had its own prompts and was rewriting
    PT teacher_scripts back to English when a factual judge flagged
    them. Now every regen builder calls this helper before returning,
    threading lesson.unit.course.locale through the orchestrator.
    """
    return system_text + locale_instruction_block(locale or 'en-us')


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
    locale: str = 'en-us',
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
        "system": _with_locale(_REGEN_SYSTEM, locale),
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
    locale: str = 'en-us',
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
        "system": _with_locale(_MULTI_JUDGE_REGEN_SYSTEM, locale),
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
    locale: str = 'en-us',
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
        "system": _with_locale(_EXIT_Q_AUTO_REGEN_SYSTEM, locale),
        "user": user,
    }


_FIB_AUTO_REGEN_SYSTEM = """\
Rewrite ONE fill-in-the-blank exit-ticket question so the named \
violations are fixed while keeping the question pedagogically sound \
and matched to the lesson and enabling objective.

Output MUST conform to the FillInBlankRewrite schema (instructor will \
validate). Required fields: question_text, text_template (use ___ for \
each blank), blanks (canonical value per blank, in left-to-right \
order), accept_alternatives (list-of-lists, one inner list per blank).

Violation guidance:
  FIB_WRONG_ANSWER → replace the wrong canonical value with the \
correct one and update accept_alternatives accordingly.
  FIB_AMBIGUOUS_BLANK → tighten the surrounding context so ONE value \
is clearly expected (add a qualifier, a unit, a date, a place name).
  FIB_OFF_OBJECTIVE → rephrase the stem to test the enabling \
objective directly.
  FIB_MISSING_ALTERNATIVES → expand accept_alternatives to cover \
common variants (synonyms, abbreviations, alternate spellings).
  FIB_TRICK_WORDING → rewrite the stem in plain direct form; remove \
double-negatives / awkward word order.

Constraints:
  - Number of ___ markers in text_template must match len(blanks).
  - accept_alternatives outer list should match len(blanks); inner \
lists may be empty when no common variants exist.
  - Match the lesson grade band — vocabulary and complexity.
  - Stay on-topic for the enabling_objective.
"""


def build_fib_auto_regen_prompt(
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
    """Compose judge-driven regen prompt for one fill-in-blank question."""
    violations = judge_result.get('violations') or []
    reasoning = (judge_result.get('reasoning') or '').strip()
    recommended_fix = (judge_result.get('recommended_fix') or '').strip()

    orig_blanks = original_question.get('blanks') or []
    orig_alts = original_question.get('accept_alternatives') or []

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
        f"  <text_template>{(original_question.get('text_template') or '').strip()[:1200]}</text_template>\n"
        f"  <blanks>{', '.join(str(b)[:120] for b in orig_blanks[:6])}</blanks>\n"
        f"  <accept_alternatives>{orig_alts!r}</accept_alternatives>\n"
        f"  <explanation>{(original_question.get('explanation') or '').strip()[:300]}</explanation>\n"
        "</original_question>\n"
        "\n"
        "Based on the lesson context, the enabling objective, and the "
        "violation detail, produce a rewritten fill-in-the-blank "
        "question conforming to the FillInBlankRewrite schema."
    )

    return {
        "system": _FIB_AUTO_REGEN_SYSTEM,
        "user": user,
    }


_SA_AUTO_REGEN_SYSTEM = """\
Rewrite ONE short-answer exit-ticket question so the named violations \
are fixed while keeping the question pedagogically sound and matched \
to the lesson and enabling objective.

Output MUST conform to the ShortAnswerRewrite schema. Required fields: \
question_text, model_answer, keywords (list of expected terms), \
min_keywords (threshold for "correct"), explanation.

Violation guidance:
  SA_WRONG_MODEL_ANSWER → replace the wrong model_answer with the \
correct curriculum-supported answer.
  SA_VAGUE_QUESTION → tighten the stem so ONE focus is clearly \
expected (add a qualifier, narrow the scope).
  SA_OFF_OBJECTIVE → rephrase the stem to test the enabling \
objective directly.
  SA_KEYWORDS_INCOMPLETE → expand keywords to cover common variants \
(synonyms, alternate phrasings).
  SA_KEYWORDS_TOO_PERMISSIVE → replace generic / stopword keywords \
with specific terms tied to the model answer.
  SA_RUBRIC_MISMATCH → make sure the model_answer itself contains \
(or implies) the keywords the grader will require.

Constraints:
  - The rewritten model_answer must contain or directly imply each \
keyword.
  - min_keywords usually 1-3 (depends on keyword count).
  - Match the lesson grade band.
"""


def build_sa_auto_regen_prompt(
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
    """Compose judge-driven regen prompt for one short-answer question."""
    violations = judge_result.get('violations') or []
    reasoning = (judge_result.get('reasoning') or '').strip()
    recommended_fix = (judge_result.get('recommended_fix') or '').strip()

    orig_keywords = original_question.get('keywords') or []

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
        f"  <model_answer>{(original_question.get('model_answer') or '').strip()[:800]}</model_answer>\n"
        f"  <keywords>{', '.join(str(k)[:120] for k in orig_keywords[:15])}</keywords>\n"
        f"  <min_keywords>{original_question.get('min_keywords', 1)}</min_keywords>\n"
        f"  <explanation>{(original_question.get('explanation') or '').strip()[:300]}</explanation>\n"
        "</original_question>\n"
        "\n"
        "Based on the lesson context, the enabling objective, and the "
        "violation detail, produce a rewritten short-answer question "
        "conforming to the ShortAnswerRewrite schema."
    )

    return {
        "system": _SA_AUTO_REGEN_SYSTEM,
        "user": user,
    }


_MATCH_AUTO_REGEN_SYSTEM = """\
Rewrite ONE matching exit-ticket question so the named violations are \
fixed while keeping the question pedagogically sound and matched to \
the lesson and enabling objective.

Output MUST conform to the MatchingRewrite schema. Required fields: \
question_text, pairs (list of {"left": "...", "right": "..."} objects), \
distractor_rights (extra right-side options that are plausibly wrong), \
explanation.

Violation guidance:
  MATCH_WRONG_PAIR → replace the wrong pair with the correct \
curriculum-supported pairing.
  MATCH_AMBIGUOUS_PAIRING → tighten the left or right items so the \
mapping is unique (add a qualifier, scope down).
  MATCH_DISTRACTOR_VALID → replace the bad distractor with one that \
is genuinely WRONG for every left item.
  MATCH_OFF_OBJECTIVE → rebuild the pairs to test the enabling \
objective directly.
  MATCH_UNEVEN_DIFFICULTY → equalise pair difficulty (don't mix \
recall + analysis in the same set).
  MATCH_TRIVIAL_DISTRACTORS → swap absurd distractors for \
plausibly-wrong ones a student might actually pick.

Constraints:
  - Every left must have ONE and only one valid right under any \
reasonable reading.
  - No distractor_rights item may be a valid match for any left.
  - 2-8 canonical pairs; 0-6 distractor_rights.
  - Match the lesson grade band.
"""


def build_match_auto_regen_prompt(
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
    """Compose judge-driven regen prompt for one matching question."""
    violations = judge_result.get('violations') or []
    reasoning = (judge_result.get('reasoning') or '').strip()
    recommended_fix = (judge_result.get('recommended_fix') or '').strip()

    orig_pairs = original_question.get('pairs') or []
    orig_distractors = original_question.get('distractor_rights') or []
    pair_str = '; '.join(
        f"{(p.get('left') if isinstance(p, dict) else '')[:120]}"
        f" ↔ "
        f"{(p.get('right') if isinstance(p, dict) else '')[:120]}"
        for p in orig_pairs[:8]
    )

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
        f"  <pairs>{pair_str}</pairs>\n"
        f"  <distractor_rights>{', '.join(str(d)[:120] for d in orig_distractors[:8])}</distractor_rights>\n"
        f"  <explanation>{(original_question.get('explanation') or '').strip()[:300]}</explanation>\n"
        "</original_question>\n"
        "\n"
        "Based on the lesson context, the enabling objective, and the "
        "violation detail, produce a rewritten matching question "
        "conforming to the MatchingRewrite schema."
    )

    return {
        "system": _MATCH_AUTO_REGEN_SYSTEM,
        "user": user,
    }


_FIB_TEACHER_PROMPT_SYSTEM = """\
Rewrite ONE fill-in-the-blank exit-ticket question following the \
teacher's guidance. The teacher knows their class — apply their \
guidance literally while keeping the question pedagogically sound.

Output MUST conform to the FillInBlankRewrite schema. Required \
fields: question_text, text_template (use ___ for each blank), \
blanks (canonical value per blank, in left-to-right order), \
accept_alternatives.

Constraints:
  - Apply the teacher's guidance as the primary driver.
  - Number of ___ markers in text_template must match len(blanks).
  - Match the lesson grade band — vocabulary and complexity.
  - Stay on-topic for the lesson + enabling_objective UNLESS the \
guidance explicitly changes the focus.
"""


def build_fib_teacher_prompt(
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
    """Teacher-prompt-driven FIB rewrite. Single call, no judge gating."""
    orig_blanks = original_question.get('blanks') or []
    orig_alts = original_question.get('accept_alternatives') or []

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
        f"  <text_template>{(original_question.get('text_template') or '').strip()[:1200]}</text_template>\n"
        f"  <blanks>{', '.join(str(b)[:120] for b in orig_blanks[:6])}</blanks>\n"
        f"  <accept_alternatives>{orig_alts!r}</accept_alternatives>\n"
        f"  <explanation>{(original_question.get('explanation') or '').strip()[:300]}</explanation>\n"
        "</original_question>\n"
        "\n"
        "Based on the lesson context above and the teacher's guidance, "
        "produce a rewritten fill-in-the-blank question conforming to "
        "the FillInBlankRewrite schema."
    )

    return {
        "system": _FIB_TEACHER_PROMPT_SYSTEM,
        "user": user,
    }


_SA_TEACHER_PROMPT_SYSTEM = """\
Rewrite ONE short-answer exit-ticket question following the teacher's \
guidance. Apply their guidance literally.

Output MUST conform to the ShortAnswerRewrite schema. Required: \
question_text, model_answer, keywords, min_keywords, explanation.

Constraints:
  - The rewritten model_answer must contain or directly imply each \
keyword (so the grader's keyword-match correctly identifies a valid \
answer).
  - Apply the teacher's guidance as the primary driver.
  - Match the lesson grade band.
"""


def build_sa_teacher_prompt(
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
    """Teacher-prompt-driven SA rewrite."""
    orig_keywords = original_question.get('keywords') or []
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
        f"  <model_answer>{(original_question.get('model_answer') or '').strip()[:800]}</model_answer>\n"
        f"  <keywords>{', '.join(str(k)[:120] for k in orig_keywords[:15])}</keywords>\n"
        f"  <min_keywords>{original_question.get('min_keywords', 1)}</min_keywords>\n"
        f"  <explanation>{(original_question.get('explanation') or '').strip()[:300]}</explanation>\n"
        "</original_question>\n"
        "\n"
        "Based on the lesson context above and the teacher's guidance, "
        "produce a rewritten short-answer question conforming to the "
        "ShortAnswerRewrite schema."
    )
    return {
        "system": _SA_TEACHER_PROMPT_SYSTEM,
        "user": user,
    }


_MATCH_TEACHER_PROMPT_SYSTEM = """\
Rewrite ONE matching exit-ticket question following the teacher's \
guidance. Apply their guidance literally while keeping the question \
pedagogically sound.

Output MUST conform to the MatchingRewrite schema. Required: \
question_text, pairs (list of {left, right} objects), \
distractor_rights, explanation.

Constraints:
  - Every left must have ONE and only one valid right under any \
reasonable reading.
  - No distractor_rights item may be a valid match for any left.
  - 2-8 canonical pairs; 0-6 distractor_rights.
  - Apply the teacher's guidance as the primary driver.
  - Match the lesson grade band.
"""


def build_match_teacher_prompt(
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
    """Teacher-prompt-driven matching rewrite."""
    orig_pairs = original_question.get('pairs') or []
    orig_distractors = original_question.get('distractor_rights') or []
    pair_str = '; '.join(
        f"{(p.get('left') if isinstance(p, dict) else '')[:120]}"
        f" ↔ "
        f"{(p.get('right') if isinstance(p, dict) else '')[:120]}"
        for p in orig_pairs[:8]
    )
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
        f"  <pairs>{pair_str}</pairs>\n"
        f"  <distractor_rights>{', '.join(str(d)[:120] for d in orig_distractors[:8])}</distractor_rights>\n"
        f"  <explanation>{(original_question.get('explanation') or '').strip()[:300]}</explanation>\n"
        "</original_question>\n"
        "\n"
        "Based on the lesson context above and the teacher's guidance, "
        "produce a rewritten matching question conforming to the "
        "MatchingRewrite schema."
    )
    return {
        "system": _MATCH_TEACHER_PROMPT_SYSTEM,
        "user": user,
    }


__all__ = [
    "build_step_regen_prompt",
    "build_step_multi_judge_regen_prompt",
    "build_step_prompt_regen_prompt",
    "build_exit_q_prompt_regen_prompt",
    "build_exit_q_auto_regen_prompt",
    "build_fib_auto_regen_prompt",
    "build_sa_auto_regen_prompt",
    "build_match_auto_regen_prompt",
    "build_fib_teacher_prompt",
    "build_sa_teacher_prompt",
    "build_match_teacher_prompt",
]
