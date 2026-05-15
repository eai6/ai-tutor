"""Bounded regen ensemble for generated content (lesson steps).

Mirrors `apps/tutoring/regen/` but adapted for content generation:
  - Tutor regen rewrites a live tutor turn that violated runtime rules.
  - Content regen rewrites a generated lesson step whose factual_step
    judge flagged contradicted/unsupported claims at content-gen time.

**Scope (v1):** lesson steps only. Image regen would re-pay $0.04 +
8-15s per cycle to the gen API; defer until image_prompt + figure_alignment
catch-rate stats justify that cost.

**Scope (v1):** single-model regen using the GENERATION ModelConfig
(not multi-model ensemble). Tutor turns benefit from diverse rewrites;
generated lesson content has one canonical generator. If we see a regen
cycle plateau we'll add a second model later (Rule of Three).

**Cycle behaviour:** mirror tutor regen — temperature decay 0.20 → 0.15
across 2 cycles. Early-exit on the first clean candidate. After cap with
violations remaining: return the BEST candidate (fewest violations) and
let the caller flag `content_quality_status='auto_flagged'` so a teacher
can address it. **No silent failure** — every flagged step bubbles up to
the lesson detail UI.

**Audit trail:** the orchestrator returns `RegenResult.audit` — a list of
per-cycle records with model_name, temperature, violations_before,
violations_after, candidate_text_preview, picked. Caller persists to
`step.judge_outputs['regen_audit']`.

Public API:
  run_step_regen(step_text, judge_result, lesson, ...) -> RegenResult
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ─── Cycle constants ───────────────────────────────────────────────────
# Hard cap on regen cycles. Each cycle = 1 generation call + 1 judge
# call. With cap=2 worst case = 2 gen + 2 judge calls per flagged step.
# Matches tutoring DEFAULT_MAX_CYCLES per CLAUDE.md (dropped 4→2 on
# 2026-05-12 — production logs showed cycle 3/4 converging identically).
DEFAULT_MAX_CYCLES = 2

# Starting temperature for cycle 1. Each subsequent cycle drops by
# DEFAULT_TEMPERATURE_DECAY so the model becomes more deterministic.
# Same shape as tutor regen.
DEFAULT_TEMPERATURE_START = 0.20
DEFAULT_TEMPERATURE_DECAY = 0.05


# ─── Public dataclasses ────────────────────────────────────────────────
@dataclass
class RegenCandidate:
    """One model's rewrite for one cycle."""
    cycle: int
    model_name: str
    temperature: float
    text: str = ""
    judge_passed: bool = False
    judge_violations: List[str] = field(default_factory=list)
    judge_reasoning: str = ""
    score: float = 0.0
    error: str = ""


@dataclass
class RegenResult:
    """Outcome of a full regen run for one piece of content."""
    text: str = ""                           # Final text to persist
    clean: bool = False                      # True iff no violations
    cycles_run: int = 0
    picked_model: str = ""
    elapsed_seconds: float = 0.0
    final_violations: List[str] = field(default_factory=list)
    final_reasoning: str = ""
    audit: List[Dict[str, Any]] = field(default_factory=list)


def _candidate_to_audit(c: RegenCandidate, *, picked: bool) -> Dict[str, Any]:
    return {
        'cycle': c.cycle,
        'model_name': c.model_name,
        'temperature': round(c.temperature, 3),
        'text_preview': (c.text or '')[:200],
        'text_chars': len(c.text or ''),
        'judge_passed': c.judge_passed,
        'judge_violations': list(c.judge_violations),
        'judge_reasoning': (c.judge_reasoning or '')[:200],
        'score': round(c.score, 3),
        'picked': picked,
        'error': c.error or '',
    }


# ─── Orchestrator ──────────────────────────────────────────────────────
def run_step_regen(
    *,
    step_text: str,
    judge_result: Dict[str, Any],
    lesson,
    lesson_subject: str = "",
    lesson_grade: str = "",
    lesson_title: str = "",
    lesson_objective: str = "",
    step_objective: str = "",
    step_concept_tag: str = "",
    max_cycles: int = DEFAULT_MAX_CYCLES,
    temperature_start: float = DEFAULT_TEMPERATURE_START,
    temperature_decay: float = DEFAULT_TEMPERATURE_DECAY,
) -> RegenResult:
    """Rewrite step.teacher_script until factual_step judge passes
    OR the cycle cap is hit.

    Args:
        step_text: The original teacher_script that failed the judge.
        judge_result: The factual_step verdict dict — provides
            violations + recommended_fix that drive the rewrite prompt.
        lesson: Lesson instance (for KB scoping in re-judge).
        lesson_*/step_*: Context for the regen prompt. Auto-derived
            from `lesson` when not supplied.
        max_cycles: Hard cap on rewrite attempts. Default 2.
        temperature_start: Cycle-1 temperature. Subsequent cycles
            decay by `temperature_decay`.

    Returns:
        RegenResult with the picked text + audit. `clean=True` iff a
        cycle produced a candidate the judge passed (no violations);
        otherwise `clean=False` and the caller should set
        step.content_quality_status = 'auto_flagged'.
    """
    started = time.monotonic()
    result = RegenResult(text=step_text or '', cycles_run=0)

    if not (step_text or '').strip():
        result.text = ''
        result.audit.append({
            'cycle': 0, 'error': 'empty_step_text', 'picked': False,
        })
        result.elapsed_seconds = time.monotonic() - started
        return result

    # Resolve the GENERATION ModelConfig + client. Single-model regen
    # for v1 — see module docstring for rationale.
    try:
        from apps.llm.models import ModelConfig
        from apps.llm.client import get_llm_client
        gen_config = ModelConfig.get_for('generation')
    except Exception as exc:
        logger.warning(f"[ContentRegen] ModelConfig lookup failed: {exc}")
        result.audit.append({
            'cycle': 0, 'error': f'config_lookup_failed: {exc}',
            'picked': False,
        })
        result.elapsed_seconds = time.monotonic() - started
        return result

    if gen_config is None:
        logger.warning("[ContentRegen] no generation ModelConfig — skip regen")
        result.audit.append({
            'cycle': 0, 'error': 'no_generation_config', 'picked': False,
        })
        result.elapsed_seconds = time.monotonic() - started
        return result

    try:
        gen_client = get_llm_client(gen_config)
    except Exception as exc:
        logger.warning(f"[ContentRegen] client construction failed: {exc}")
        result.audit.append({
            'cycle': 0, 'error': f'client_construction_failed: {exc}',
            'picked': False,
        })
        result.elapsed_seconds = time.monotonic() - started
        return result

    # Auto-derive lesson context from the Lesson instance when caller
    # didn't supply it.
    if lesson is not None and not (lesson_subject and lesson_title):
        try:
            lesson_title = lesson_title or str(getattr(lesson, 'title', '') or '')
            lesson_objective = lesson_objective or str(
                getattr(lesson, 'objective', '') or ''
            )
            unit = getattr(lesson, 'unit', None)
            course = getattr(unit, 'course', None) if unit else None
            if course is not None and not lesson_subject:
                subj_type = getattr(course, 'subject_type', '') or ''
                course_name = getattr(course, 'name', '') or ''
                lesson_subject = str(subj_type or course_name)
            if course is not None and not lesson_grade:
                grades = getattr(course, 'grade_levels', None) or []
                if isinstance(grades, list) and grades:
                    lesson_grade = ", ".join(str(g) for g in grades[:3])
                else:
                    lesson_grade = str(getattr(course, 'grade_level', '') or '')
        except Exception:
            pass

    from apps.curriculum.content_judges.factual_step import (
        run_factual_step_judge,
    )
    from apps.curriculum.content_regen.prompt import (
        build_step_regen_prompt,
    )
    from apps.curriculum.content_regen.score import score_candidate

    # exclude_provider for re-judge: same as the original judge ran
    # against — exclude the generation provider so the judge stays on
    # a different vendor for cross-provider review.
    judge_exclude = (gen_config.provider or '').lower() or None

    candidates: List[RegenCandidate] = []
    best: Optional[RegenCandidate] = None
    current_judge = dict(judge_result or {})

    for cycle in range(max_cycles):
        temperature = max(
            0.0, temperature_start - cycle * temperature_decay,
        )

        c = RegenCandidate(
            cycle=cycle + 1,
            model_name=gen_config.model_name or '',
            temperature=temperature,
        )

        # Build the focused regen prompt from the CURRENT verdict
        # (judge_result on cycle 0; the previous cycle's verdict on
        # cycle 1+).
        prompt = build_step_regen_prompt(
            original_text=step_text if cycle == 0 else (best.text if best else step_text),
            judge_result=current_judge,
            lesson_subject=lesson_subject,
            lesson_grade=lesson_grade,
            lesson_title=lesson_title,
            lesson_objective=lesson_objective,
            step_objective=step_objective,
            step_concept_tag=step_concept_tag,
        )

        # Generate
        try:
            response = gen_client.generate(
                messages=[{"role": "user", "content": prompt['user']}],
                system_prompt=prompt['system'],
                max_tokens=2000,
                temperature=temperature,
            )
        except Exception as exc:
            logger.warning(
                f"[ContentRegen] cycle {cycle+1} gen call failed: "
                f"{type(exc).__name__}: {exc}"
            )
            c.error = f"gen_failed: {type(exc).__name__}"
            candidates.append(c)
            continue

        candidate_text = (response.content or '').strip()
        # Strip any code-fence wrapping the model added.
        if candidate_text.startswith('```'):
            import re
            candidate_text = re.sub(
                r"^```[a-z]*\s*|\s*```$", "", candidate_text,
                flags=re.IGNORECASE,
            ).strip()
        c.text = candidate_text

        if not candidate_text:
            c.error = 'empty_response'
            candidates.append(c)
            continue

        # Re-judge
        try:
            verdict = run_factual_step_judge(
                candidate_text,
                lesson=lesson,
                exclude_provider=judge_exclude,
            )
        except Exception as exc:
            logger.warning(
                f"[ContentRegen] cycle {cycle+1} re-judge failed: "
                f"{type(exc).__name__}: {exc}"
            )
            c.error = f"judge_failed: {type(exc).__name__}"
            c.score = score_candidate({'violations': [], 'skipped': True})
            candidates.append(c)
            continue

        c.judge_passed = bool(verdict.passed)
        c.judge_violations = list(verdict.violations or [])
        c.judge_reasoning = verdict.reasoning or ''
        c.score = score_candidate({
            'violations': c.judge_violations,
            'skipped': verdict.skipped,
            'passed': verdict.passed,
        })
        candidates.append(c)

        # Update the running "best" for fallback if no clean candidate
        # ever appears.
        if best is None or c.score > best.score:
            best = c

        # Refresh the verdict that drives the next cycle's prompt.
        current_judge = {
            'violations': c.judge_violations,
            'reasoning': c.judge_reasoning,
            'recommended_fix': verdict.recommended_fix or '',
        }

        # Early-exit on first clean candidate (no violations).
        if c.judge_passed and not c.judge_violations:
            best = c
            break

    result.cycles_run = len(candidates)
    if best is None:
        # Every cycle errored — keep the original.
        result.text = step_text
        result.clean = False
        result.audit = [_candidate_to_audit(c, picked=False) for c in candidates]
        result.elapsed_seconds = time.monotonic() - started
        logger.warning(
            f"[ContentRegen] all {len(candidates)} cycles errored — "
            f"keeping original text"
        )
        return result

    result.text = best.text
    result.clean = bool(best.judge_passed and not best.judge_violations)
    result.picked_model = best.model_name
    result.final_violations = list(best.judge_violations)
    result.final_reasoning = best.judge_reasoning
    result.audit = [
        _candidate_to_audit(c, picked=(c is best)) for c in candidates
    ]
    result.elapsed_seconds = time.monotonic() - started

    logger.info(
        f"[ContentRegen] {'CLEAN' if result.clean else 'FLAGGED'} after "
        f"{result.cycles_run} cycle(s) — picked model={best.model_name} "
        f"violations={best.judge_violations} elapsed={result.elapsed_seconds:.1f}s"
    )
    return result


def run_step_prompt_regen(
    *,
    step_text: str,
    teacher_guidance: str,
    lesson,
    lesson_subject: str = "",
    lesson_grade: str = "",
    lesson_title: str = "",
    lesson_objective: str = "",
    step_objective: str = "",
    step_concept_tag: str = "",
    temperature: float = 0.30,
) -> RegenResult:
    """Teacher-prompt-driven rewrite. Single LLM call, no judge gating.

    Used by Q3 manual-regen UI's "Prompt mode" — the teacher says what
    they want changed, the model applies it. We don't run the
    factual_step judge here because the teacher's guidance is the
    source of truth for this rewrite (e.g. "make this shorter" doesn't
    fail factual judging). Runs ONCE; the teacher reviews + accepts
    the candidate or discards it.

    Args:
        step_text: The current step.teacher_script.
        teacher_guidance: Free-form instruction from the teacher
            ("make this shorter / less abstract / add a Seychelles
            example"). Empty/whitespace → returns the original
            unchanged with an audit note.
        lesson: Lesson instance (for context derivation).
        lesson_*/step_*: Context for the regen prompt.
        temperature: Generation temperature. Default 0.30 — slightly
            higher than auto-regen because teacher guidance often
            asks for stylistic shifts that benefit from variability.

    Returns:
        RegenResult with `clean=True` (single-pass; we treat the
        teacher's prompt as authoritative). `audit` carries the
        single cycle's record. Caller decides whether to persist.
    """
    started = time.monotonic()
    result = RegenResult(text=step_text or '', cycles_run=0)

    if not (step_text or '').strip():
        result.text = ''
        result.audit.append({
            'cycle': 0, 'error': 'empty_step_text', 'picked': False,
        })
        result.elapsed_seconds = time.monotonic() - started
        return result

    if not (teacher_guidance or '').strip():
        result.audit.append({
            'cycle': 0, 'error': 'empty_teacher_guidance', 'picked': False,
        })
        result.elapsed_seconds = time.monotonic() - started
        return result

    try:
        from apps.llm.models import ModelConfig
        from apps.llm.client import get_llm_client
        gen_config = ModelConfig.get_for('generation')
    except Exception as exc:
        result.audit.append({
            'cycle': 0, 'error': f'config_lookup_failed: {exc}',
            'picked': False,
        })
        result.elapsed_seconds = time.monotonic() - started
        return result

    if gen_config is None:
        result.audit.append({
            'cycle': 0, 'error': 'no_generation_config', 'picked': False,
        })
        result.elapsed_seconds = time.monotonic() - started
        return result

    try:
        gen_client = get_llm_client(gen_config)
    except Exception as exc:
        result.audit.append({
            'cycle': 0, 'error': f'client_construction_failed: {exc}',
            'picked': False,
        })
        result.elapsed_seconds = time.monotonic() - started
        return result

    # Auto-derive lesson context from the Lesson instance when caller
    # didn't supply it.
    if lesson is not None and not (lesson_subject and lesson_title):
        try:
            lesson_title = lesson_title or str(getattr(lesson, 'title', '') or '')
            lesson_objective = lesson_objective or str(
                getattr(lesson, 'objective', '') or ''
            )
            unit = getattr(lesson, 'unit', None)
            course = getattr(unit, 'course', None) if unit else None
            if course is not None and not lesson_subject:
                subj_type = getattr(course, 'subject_type', '') or ''
                course_name = getattr(course, 'name', '') or ''
                lesson_subject = str(subj_type or course_name)
            if course is not None and not lesson_grade:
                grades = getattr(course, 'grade_levels', None) or []
                if isinstance(grades, list) and grades:
                    lesson_grade = ", ".join(str(g) for g in grades[:3])
                else:
                    lesson_grade = str(getattr(course, 'grade_level', '') or '')
        except Exception:
            pass

    from apps.curriculum.content_regen.prompt import (
        build_step_prompt_regen_prompt,
    )

    prompt = build_step_prompt_regen_prompt(
        original_text=step_text,
        teacher_guidance=teacher_guidance,
        lesson_subject=lesson_subject,
        lesson_grade=lesson_grade,
        lesson_title=lesson_title,
        lesson_objective=lesson_objective,
        step_objective=step_objective,
        step_concept_tag=step_concept_tag,
    )

    candidate = RegenCandidate(
        cycle=1,
        model_name=gen_config.model_name or '',
        temperature=temperature,
    )

    try:
        response = gen_client.generate(
            messages=[{"role": "user", "content": prompt['user']}],
            system_prompt=prompt['system'],
            max_tokens=2000,
            temperature=temperature,
        )
    except Exception as exc:
        logger.warning(
            f"[ContentRegen] prompt-mode gen call failed: "
            f"{type(exc).__name__}: {exc}"
        )
        candidate.error = f"gen_failed: {type(exc).__name__}"
        result.audit.append(_candidate_to_audit(candidate, picked=False))
        result.elapsed_seconds = time.monotonic() - started
        result.text = step_text  # Keep original on error
        return result

    candidate_text = (response.content or '').strip()
    if candidate_text.startswith('```'):
        import re
        candidate_text = re.sub(
            r"^```[a-z]*\s*|\s*```$", "", candidate_text,
            flags=re.IGNORECASE,
        ).strip()
    candidate.text = candidate_text
    candidate.judge_passed = True   # No judge ran — teacher prompt is authority
    candidate.score = 1.0
    result.audit.append(_candidate_to_audit(candidate, picked=True))
    result.cycles_run = 1
    result.text = candidate_text or step_text
    result.clean = True
    result.picked_model = candidate.model_name
    result.elapsed_seconds = time.monotonic() - started

    logger.info(
        f"[ContentRegen] prompt-mode rewrite via {candidate.model_name} "
        f"({len(candidate_text)} chars) elapsed={result.elapsed_seconds:.1f}s"
    )
    return result


@dataclass
class ExitQuestionRegenResult:
    """Outcome of a single MCQ exit-question rewrite. Distinct from
    RegenResult because the candidate is structured (text + 4 options
    + correct + explanation), not a flat string."""
    question_text: str = ""
    option_a: str = ""
    option_b: str = ""
    option_c: str = ""
    option_d: str = ""
    correct_answer: str = ""
    explanation: str = ""
    picked_model: str = ""
    elapsed_seconds: float = 0.0
    error: str = ""

    def as_dict(self) -> Dict[str, str]:
        return {
            'question_text': self.question_text,
            'option_a': self.option_a,
            'option_b': self.option_b,
            'option_c': self.option_c,
            'option_d': self.option_d,
            'correct_answer': self.correct_answer,
            'explanation': self.explanation,
        }


def run_exit_question_prompt_regen(
    *,
    original_question: Dict[str, Any],
    teacher_guidance: str,
    lesson,
    lesson_subject: str = "",
    lesson_grade: str = "",
    lesson_title: str = "",
    lesson_objective: str = "",
    step_concept_tag: str = "",
    enabling_objective: str = "",
    temperature: float = 0.30,
) -> ExitQuestionRegenResult:
    """Teacher-prompt-driven rewrite of a single MCQ exit-ticket
    question. Single LLM call, no judge gating. Used by Q3.2 manual
    regen UI's prompt mode.

    Args:
        original_question: Dict with question_text, option_a..d,
            correct_answer (single letter), explanation. Missing keys
            render as empty.
        teacher_guidance: The teacher's free-form instruction
            ("simpler wording / harder distractors / focus on the
            scientific names").
        lesson: Lesson instance for context derivation.
        lesson_*/step_*: Context for the regen prompt. Auto-derived
            from `lesson` when not supplied.
        temperature: Generation temperature. Default 0.30.

    Returns:
        ExitQuestionRegenResult. On failure (no config / LLM error /
        parse error), returns the original fields unchanged with
        `error` populated. Caller decides whether to persist.
    """
    started = time.monotonic()
    result = ExitQuestionRegenResult(
        question_text=original_question.get('question_text') or '',
        option_a=original_question.get('option_a') or '',
        option_b=original_question.get('option_b') or '',
        option_c=original_question.get('option_c') or '',
        option_d=original_question.get('option_d') or '',
        correct_answer=original_question.get('correct_answer') or '',
        explanation=original_question.get('explanation') or '',
    )

    if not (teacher_guidance or '').strip():
        result.error = 'empty_teacher_guidance'
        result.elapsed_seconds = time.monotonic() - started
        return result

    try:
        from apps.llm.models import ModelConfig
        from apps.llm.client import get_llm_client
        gen_config = ModelConfig.get_for('generation')
    except Exception as exc:
        result.error = f'config_lookup_failed: {exc}'
        result.elapsed_seconds = time.monotonic() - started
        return result

    if gen_config is None:
        result.error = 'no_generation_config'
        result.elapsed_seconds = time.monotonic() - started
        return result

    try:
        gen_client = get_llm_client(gen_config)
    except Exception as exc:
        result.error = f'client_construction_failed: {exc}'
        result.elapsed_seconds = time.monotonic() - started
        return result

    if lesson is not None and not (lesson_subject and lesson_title):
        try:
            lesson_title = lesson_title or str(getattr(lesson, 'title', '') or '')
            lesson_objective = lesson_objective or str(
                getattr(lesson, 'objective', '') or ''
            )
            unit = getattr(lesson, 'unit', None)
            course = getattr(unit, 'course', None) if unit else None
            if course is not None and not lesson_subject:
                subj_type = getattr(course, 'subject_type', '') or ''
                course_name = getattr(course, 'name', '') or ''
                lesson_subject = str(subj_type or course_name)
            if course is not None and not lesson_grade:
                grades = getattr(course, 'grade_levels', None) or []
                if isinstance(grades, list) and grades:
                    lesson_grade = ", ".join(str(g) for g in grades[:3])
                else:
                    lesson_grade = str(getattr(course, 'grade_level', '') or '')
        except Exception:
            pass

    from apps.curriculum.content_regen.prompt import (
        build_exit_q_prompt_regen_prompt,
    )
    prompt = build_exit_q_prompt_regen_prompt(
        original_question=original_question,
        teacher_guidance=teacher_guidance,
        lesson_subject=lesson_subject,
        lesson_grade=lesson_grade,
        lesson_title=lesson_title,
        lesson_objective=lesson_objective,
        step_concept_tag=step_concept_tag,
        enabling_objective=enabling_objective,
    )

    result.picked_model = gen_config.model_name or ''

    try:
        response = gen_client.generate(
            messages=[{"role": "user", "content": prompt['user']}],
            system_prompt=prompt['system'],
            max_tokens=1500,
            temperature=temperature,
        )
    except Exception as exc:
        logger.warning(
            f"[ContentRegen] exit-Q prompt regen gen call failed: "
            f"{type(exc).__name__}: {exc}"
        )
        result.error = f"gen_failed: {type(exc).__name__}"
        result.elapsed_seconds = time.monotonic() - started
        return result

    raw = (response.content or '').strip()
    if raw.startswith('```'):
        import re
        raw = re.sub(r"^```[a-z]*\s*|\s*```$", "", raw,
                     flags=re.IGNORECASE).strip()

    import json as _json
    try:
        parsed = _json.loads(raw)
    except Exception:
        # Best-effort: pull the first {...} block
        import re
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            result.error = 'unparseable_json'
            result.elapsed_seconds = time.monotonic() - started
            return result
        try:
            parsed = _json.loads(m.group(0))
        except Exception:
            result.error = 'unparseable_json'
            result.elapsed_seconds = time.monotonic() - started
            return result

    if not isinstance(parsed, dict):
        result.error = 'verdict_not_dict'
        result.elapsed_seconds = time.monotonic() - started
        return result

    # Coerce + validate. Empty fields fall back to original.
    def _str(key, default=''):
        v = parsed.get(key)
        return str(v).strip() if v is not None else default

    result.question_text = _str('question_text', result.question_text)[:600]
    result.option_a = _str('option_a', result.option_a)[:200]
    result.option_b = _str('option_b', result.option_b)[:200]
    result.option_c = _str('option_c', result.option_c)[:200]
    result.option_d = _str('option_d', result.option_d)[:200]

    raw_correct = _str('correct_answer', result.correct_answer)[:1].upper()
    if raw_correct in ('A', 'B', 'C', 'D'):
        result.correct_answer = raw_correct

    result.explanation = _str('explanation', result.explanation)[:500]
    result.elapsed_seconds = time.monotonic() - started

    logger.info(
        f"[ContentRegen] exit-Q prompt-regen via {result.picked_model} "
        f"({len(result.question_text)} stem chars) "
        f"elapsed={result.elapsed_seconds:.1f}s"
    )
    return result


__all__ = [
    "DEFAULT_MAX_CYCLES",
    "DEFAULT_TEMPERATURE_START",
    "DEFAULT_TEMPERATURE_DECAY",
    "RegenCandidate",
    "RegenResult",
    "ExitQuestionRegenResult",
    "run_step_regen",
    "run_step_prompt_regen",
    "run_exit_question_prompt_regen",
]
