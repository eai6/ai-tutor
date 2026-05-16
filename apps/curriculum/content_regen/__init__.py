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
    clean: bool = False                      # True iff ALL judges pass
    cycles_run: int = 0
    picked_model: str = ""
    elapsed_seconds: float = 0.0
    final_violations: List[str] = field(default_factory=list)
    final_reasoning: str = ""
    audit: List[Dict[str, Any]] = field(default_factory=list)
    # Per-judge final verdicts (post-regen). Keys: judge_name → dict
    # with 'passed', 'violations', 'reasoning'. Used by the caller to
    # update step.judge_outputs so the UI shows the FINAL per-judge
    # state, not the original failing one.
    final_judge_results: Dict[str, Dict[str, Any]] = field(default_factory=dict)


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
    judge_results: Dict[str, Dict[str, Any]] = None,
    judge_result: Dict[str, Any] = None,  # Legacy single-verdict shape
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
    """Rewrite step.teacher_script until ALL enabled step judges pass
    OR the cycle cap is hit.

    Multi-judge orchestration: per cycle, run the rewrite prompt that
    targets the union of violations across factual_step + pedagogy_step
    + safety_content, then re-run all 3 judges concurrently. Step is
    `clean=True` only when every judge that ran passes. This guarantees
    the post-regen text isn't fixing one judge while breaking another.

    Args:
        step_text: The original teacher_script that failed at least
            one judge.
        judge_results: Mapping `{judge_name: verdict_dict}` covering
            factual_step / pedagogy_step / safety_content. Verdicts
            with `passed=True` or `skipped=True` are passed through
            (used as constraints in the rewrite prompt) but don't
            block the clean gate.
        judge_result: Legacy single-verdict shape (factual_step only).
            When supplied without `judge_results`, wrapped as
            `{'factual_step': judge_result}` for back-compat.
        lesson: Lesson instance (for KB scoping in re-judge).
        lesson_*/step_*: Context for the regen prompt. Auto-derived
            from `lesson` when not supplied.
        max_cycles: Hard cap on rewrite attempts. Default 2.
        temperature_start: Cycle-1 temperature. Subsequent cycles
            decay by `temperature_decay`.

    Returns:
        RegenResult with the picked text + per-judge final verdicts.
        `clean=True` iff every judge that ran (excluding skipped)
        passed on the picked candidate. Otherwise `clean=False` and
        the caller should set
        step.content_quality_status = 'auto_flagged'.
    """
    # Normalise legacy single-verdict shape to multi-judge dict.
    if judge_results is None:
        if judge_result is not None:
            judge_results = {'factual_step': judge_result}
        else:
            judge_results = {}
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

    import concurrent.futures
    from django.conf import settings as _s
    from apps.curriculum.content_judges.factual_step import (
        run_factual_step_judge,
    )
    from apps.curriculum.content_judges.pedagogy_step import (
        run_pedagogy_step_judge,
    )
    from apps.curriculum.content_judges.safety_content import (
        run_safety_content_judge,
    )
    from apps.curriculum.content_regen.prompt import (
        build_step_multi_judge_regen_prompt,
    )
    from apps.curriculum.content_regen.score import score_candidate

    pedagogy_enabled = getattr(_s, 'CONTENT_JUDGE_PEDAGOGY_STEP_ENABLED', True)
    safety_enabled = getattr(_s, 'CONTENT_JUDGE_SAFETY_CONTENT_ENABLED', True)

    # exclude_provider for re-judge: same as the original judge ran
    # against — exclude the generation provider so the judge stays on
    # a different vendor for cross-provider review.
    judge_exclude = (gen_config.provider or '').lower() or None

    def _serialize_verdict(verdict) -> Dict[str, Any]:
        """Render a JudgeResult into the shape the prompt builder expects."""
        return {
            'passed': bool(verdict.passed),
            'skipped': bool(verdict.skipped),
            'violations': list(verdict.violations or []),
            'reasoning': verdict.reasoning or '',
            'recommended_fix': verdict.recommended_fix or '',
            'provider': verdict.provider or '',
            'model_name': verdict.model_name or '',
            'skip_reason': verdict.skip_reason or '',
        }

    def _all_pass(verdict_dict: Dict[str, Dict[str, Any]]) -> bool:
        """All non-skipped judges pass."""
        for _, v in (verdict_dict or {}).items():
            if not isinstance(v, dict):
                continue
            if v.get('skipped'):
                continue
            if not v.get('passed'):
                return False
        return True

    def _union_violations(verdict_dict: Dict[str, Dict[str, Any]]) -> List[str]:
        out: List[str] = []
        for _, v in (verdict_dict or {}).items():
            if not isinstance(v, dict):
                continue
            if v.get('skipped') or v.get('passed'):
                continue
            for code in v.get('violations') or []:
                if code not in out:
                    out.append(code)
        return out

    def _rejudge_all(text: str) -> Dict[str, Dict[str, Any]]:
        """Run the 3 step judges in parallel and return per-judge dicts."""
        verdicts: Dict[str, Dict[str, Any]] = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pex:
            futures = {
                pex.submit(
                    run_factual_step_judge,
                    text,
                    lesson=lesson, exclude_provider=judge_exclude,
                ): 'factual_step',
            }
            if pedagogy_enabled:
                futures[pex.submit(
                    run_pedagogy_step_judge,
                    text,
                    lesson=lesson,
                    step_objective=step_objective,
                    step_concept_tag=step_concept_tag,
                    exclude_provider=judge_exclude,
                )] = 'pedagogy_step'
            if safety_enabled:
                futures[pex.submit(
                    run_safety_content_judge,
                    text,
                    lesson=lesson,
                    exclude_provider=judge_exclude,
                )] = 'safety_content'
            for fut in concurrent.futures.as_completed(futures):
                judge_name = futures[fut]
                try:
                    verdicts[judge_name] = _serialize_verdict(fut.result())
                except Exception as exc:
                    logger.warning(
                        f"[ContentRegen] re-judge {judge_name} failed: "
                        f"{type(exc).__name__}: {exc}"
                    )
                    verdicts[judge_name] = {
                        'passed': False, 'skipped': True,
                        'violations': [],
                        'skip_reason': f'rejudge_error: {type(exc).__name__}',
                    }
        return verdicts

    candidates: List[RegenCandidate] = []
    best: Optional[RegenCandidate] = None
    best_verdicts: Dict[str, Dict[str, Any]] = {}
    current_verdicts: Dict[str, Dict[str, Any]] = dict(judge_results or {})

    for cycle in range(max_cycles):
        temperature = max(
            0.0, temperature_start - cycle * temperature_decay,
        )

        c = RegenCandidate(
            cycle=cycle + 1,
            model_name=gen_config.model_name or '',
            temperature=temperature,
        )

        # Build the multi-judge regen prompt from the CURRENT verdicts.
        prompt = build_step_multi_judge_regen_prompt(
            original_text=step_text if cycle == 0 else (best.text if best else step_text),
            judge_results=current_verdicts,
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

        # Re-judge — run all 3 judges in parallel
        try:
            cycle_verdicts = _rejudge_all(candidate_text)
        except Exception as exc:
            logger.warning(
                f"[ContentRegen] cycle {cycle+1} re-judge orch failed: "
                f"{type(exc).__name__}: {exc}"
            )
            c.error = f"rejudge_orch_failed: {type(exc).__name__}"
            c.score = score_candidate({'violations': [], 'skipped': True})
            candidates.append(c)
            continue

        union_viols = _union_violations(cycle_verdicts)
        all_pass = _all_pass(cycle_verdicts)
        c.judge_passed = all_pass
        c.judge_violations = union_viols
        # Compose a short reasoning summary across judges for the audit.
        reasoning_bits = []
        for jname, v in cycle_verdicts.items():
            if not isinstance(v, dict) or v.get('skipped') or v.get('passed'):
                continue
            r = (v.get('reasoning') or '').strip()
            if r:
                reasoning_bits.append(f"{jname}: {r[:150]}")
        c.judge_reasoning = " | ".join(reasoning_bits)[:400]
        c.score = score_candidate({
            'violations': union_viols,
            'skipped': False,
            'passed': all_pass,
        })
        candidates.append(c)

        # Update the running "best" for fallback if no clean candidate
        # ever appears.
        if best is None or c.score > best.score:
            best = c
            best_verdicts = cycle_verdicts

        # Refresh the verdicts that drive the next cycle's prompt.
        current_verdicts = cycle_verdicts

        # Early-exit on first all-judges-clean candidate.
        if all_pass:
            best = c
            best_verdicts = cycle_verdicts
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
    result.final_judge_results = best_verdicts
    result.audit = [
        _candidate_to_audit(c, picked=(c is best)) for c in candidates
    ]
    result.elapsed_seconds = time.monotonic() - started

    logger.info(
        f"[ContentRegen] {'CLEAN' if result.clean else 'FLAGGED'} after "
        f"{result.cycles_run} cycle(s) — picked model={best.model_name} "
        f"final_violations={best.judge_violations} "
        f"elapsed={result.elapsed_seconds:.1f}s"
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


@dataclass
class ExitQuestionAutoRegenResult:
    """Outcome of judge-driven (auto) MCQ regen.

    Distinct from `ExitQuestionRegenResult` (teacher-prompt-driven) so
    we can carry the per-cycle audit + post-regen verdict without
    overloading the manual-UI shape.
    """
    question_text: str = ""
    option_a: str = ""
    option_b: str = ""
    option_c: str = ""
    option_d: str = ""
    correct_answer: str = ""
    explanation: str = ""
    clean: bool = False
    cycles_run: int = 0
    picked_model: str = ""
    elapsed_seconds: float = 0.0
    final_violations: List[str] = field(default_factory=list)
    final_reasoning: str = ""
    final_judge_result: Dict[str, Any] = field(default_factory=dict)
    audit: List[Dict[str, Any]] = field(default_factory=list)

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


def run_exit_question_regen(
    *,
    original_question: Dict[str, Any],
    judge_result: Dict[str, Any],
    lesson,
    lesson_subject: str = "",
    lesson_grade: str = "",
    lesson_title: str = "",
    lesson_objective: str = "",
    step_concept_tag: str = "",
    enabling_objective: str = "",
    max_cycles: int = DEFAULT_MAX_CYCLES,
    temperature_start: float = DEFAULT_TEMPERATURE_START,
    temperature_decay: float = DEFAULT_TEMPERATURE_DECAY,
) -> ExitQuestionAutoRegenResult:
    """Rewrite ONE MCQ until the exit_question judge passes OR the
    cycle cap is hit.

    Cycle = 1 generation call + 1 re-judge call. Hard cap matches
    `run_step_regen` (DEFAULT_MAX_CYCLES). Early-exit on first clean
    candidate. After cap with violations remaining: return the BEST
    candidate (fewest violations) and let caller flag
    content_quality_status='auto_flagged'.

    Args:
        original_question: Dict with question_text, option_a..d,
            correct_answer (single letter), explanation.
        judge_result: The exit_question verdict dict — provides the
            violations + reasoning + recommended_fix that drive the
            cycle-1 rewrite prompt.
        lesson: Lesson instance (for KB scoping in re-judge).
        lesson_*/step_*: Context for the regen prompt. Auto-derived
            from `lesson` when not supplied.

    Returns:
        ExitQuestionAutoRegenResult. `clean=True` iff the picked
        candidate's exit_question verdict passes.
    """
    started = time.monotonic()
    result = ExitQuestionAutoRegenResult(
        question_text=original_question.get('question_text') or '',
        option_a=original_question.get('option_a') or '',
        option_b=original_question.get('option_b') or '',
        option_c=original_question.get('option_c') or '',
        option_d=original_question.get('option_d') or '',
        correct_answer=original_question.get('correct_answer') or '',
        explanation=original_question.get('explanation') or '',
    )

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

    from apps.curriculum.content_judges.exit_question import (
        run_exit_question_judge,
    )
    from apps.curriculum.content_regen.prompt import (
        build_exit_q_auto_regen_prompt,
    )
    from apps.curriculum.content_regen.score import score_candidate
    from apps.curriculum.content_regen.schemas import MCQRewrite

    judge_exclude = (gen_config.provider or '').lower() or None

    # Instructor-wrap the generation client so each cycle's output is
    # schema-validated (eliminates the unparseable_json failure class
    # that previously caused entire 2-cycle runs to error out).
    instructor_client = None
    try:
        import instructor
        provider_map = {
            'anthropic': 'anthropic', 'openai': 'openai',
            'google': 'google', 'local_ollama': 'ollama',
        }
        provider_key = provider_map.get(
            str(gen_config.provider).lower(), str(gen_config.provider).lower()
        )
        instructor_client = instructor.from_provider(
            f"{provider_key}/{gen_config.model_name}",
            api_key=gen_config.get_api_key(),
        )
    except Exception as exc:
        logger.warning(
            f"[ContentRegen] exit-Q instructor init failed — falling back "
            f"to raw gen client: {type(exc).__name__}: {exc}"
        )

    def _coerce(parsed: MCQRewrite, fallback) -> ExitQuestionAutoRegenResult:
        """Build a result snapshot from a validated Pydantic MCQRewrite."""
        snap = ExitQuestionAutoRegenResult(
            question_text=(parsed.question_text or fallback.question_text)[:600],
            option_a=(parsed.option_a or fallback.option_a)[:200],
            option_b=(parsed.option_b or fallback.option_b)[:200],
            option_c=(parsed.option_c or fallback.option_c)[:200],
            option_d=(parsed.option_d or fallback.option_d)[:200],
            explanation=(parsed.explanation or fallback.explanation)[:500],
        )
        raw_correct = (parsed.correct_answer or fallback.correct_answer)[:1].upper()
        snap.correct_answer = (
            raw_correct if raw_correct in ('A', 'B', 'C', 'D')
            else fallback.correct_answer
        )
        return snap

    candidates: List[RegenCandidate] = []
    best: Optional[RegenCandidate] = None
    best_snap: Optional[ExitQuestionAutoRegenResult] = None
    best_verdict: Dict[str, Any] = {}
    current_judge = dict(judge_result or {})
    current_question: Dict[str, Any] = dict(original_question or {})

    for cycle in range(max_cycles):
        temperature = max(
            0.0, temperature_start - cycle * temperature_decay,
        )
        c = RegenCandidate(
            cycle=cycle + 1,
            model_name=gen_config.model_name or '',
            temperature=temperature,
        )

        prompt = build_exit_q_auto_regen_prompt(
            original_question=current_question,
            judge_result=current_judge,
            lesson_subject=lesson_subject,
            lesson_grade=lesson_grade,
            lesson_title=lesson_title,
            lesson_objective=lesson_objective,
            step_concept_tag=step_concept_tag,
            enabling_objective=enabling_objective,
        )

        parsed = None
        if instructor_client is not None:
            try:
                create_kwargs = dict(
                    response_model=MCQRewrite,
                    messages=[
                        {"role": "system", "content": prompt['system']},
                        {"role": "user", "content": prompt['user']},
                    ],
                    max_retries=2,
                )
                # Gemini 3 burns ~1500 tokens on internal thinking
                # before the function call returns; budget 4000 so the
                # MCQ payload itself has room. Anthropic/OpenAI don't
                # need the headroom but the higher cap is harmless.
                if str(gen_config.provider).lower() == 'google':
                    create_kwargs['generation_config'] = {'max_tokens': 4000}
                else:
                    create_kwargs['max_tokens'] = 4000
                parsed = instructor_client.chat.completions.create(**create_kwargs)
            except Exception as exc:
                logger.warning(
                    f"[ContentRegen] exit-Q cycle {cycle+1} instructor "
                    f"call failed: {type(exc).__name__}: {exc}"
                )
                c.error = f"gen_failed: {type(exc).__name__}"
                candidates.append(c)
                continue
        else:
            # Instructor unavailable (initialization failed). Skip the
            # cycle cleanly rather than falling back to brittle JSON
            # parsing — better to flag for human review than to ship
            # malformed content.
            c.error = "instructor_unavailable"
            candidates.append(c)
            continue

        snap = _coerce(parsed, result)
        c.text = (snap.question_text or '')[:200]

        # Re-judge
        try:
            verdict = run_exit_question_judge(
                question_text=snap.question_text,
                option_a=snap.option_a, option_b=snap.option_b,
                option_c=snap.option_c, option_d=snap.option_d,
                correct_answer=snap.correct_answer,
                explanation=snap.explanation,
                lesson=lesson,
                step_concept_tag=step_concept_tag,
                enabling_objective=enabling_objective,
                exclude_provider=judge_exclude,
            )
        except Exception as exc:
            logger.warning(
                f"[ContentRegen] exit-Q cycle {cycle+1} re-judge failed: "
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

        verdict_dict = {
            'passed': bool(verdict.passed),
            'skipped': bool(verdict.skipped),
            'violations': list(verdict.violations or []),
            'reasoning': verdict.reasoning or '',
            'recommended_fix': verdict.recommended_fix or '',
            'provider': verdict.provider or '',
            'model_name': verdict.model_name or '',
            'skip_reason': verdict.skip_reason or '',
        }

        if best is None or c.score > best.score:
            best = c
            best_snap = snap
            best_verdict = verdict_dict

        # Refresh inputs that drive the next cycle's prompt.
        current_judge = {
            'violations': c.judge_violations,
            'reasoning': c.judge_reasoning,
            'recommended_fix': verdict.recommended_fix or '',
        }
        current_question = snap.as_dict()

        if c.judge_passed and not c.judge_violations:
            best = c
            best_snap = snap
            best_verdict = verdict_dict
            break

    result.cycles_run = len(candidates)
    if best is None or best_snap is None:
        result.clean = False
        result.audit = [_candidate_to_audit(c, picked=False) for c in candidates]
        result.elapsed_seconds = time.monotonic() - started
        logger.warning(
            f"[ContentRegen] exit-Q all {len(candidates)} cycles errored — "
            f"keeping original"
        )
        return result

    # Promote best snapshot's fields onto the result.
    result.question_text = best_snap.question_text
    result.option_a = best_snap.option_a
    result.option_b = best_snap.option_b
    result.option_c = best_snap.option_c
    result.option_d = best_snap.option_d
    result.correct_answer = best_snap.correct_answer
    result.explanation = best_snap.explanation
    result.clean = bool(best.judge_passed and not best.judge_violations)
    result.picked_model = best.model_name
    result.final_violations = list(best.judge_violations)
    result.final_reasoning = best.judge_reasoning
    result.final_judge_result = best_verdict
    result.audit = [
        _candidate_to_audit(c, picked=(c is best)) for c in candidates
    ]
    result.elapsed_seconds = time.monotonic() - started

    logger.info(
        f"[ContentRegen] exit-Q {'CLEAN' if result.clean else 'FLAGGED'} after "
        f"{result.cycles_run} cycle(s) — picked={best.model_name} "
        f"final_violations={best.judge_violations} "
        f"elapsed={result.elapsed_seconds:.1f}s"
    )
    return result


@dataclass
class FillInBlankAutoRegenResult:
    """Outcome of judge-driven (auto) fill_in_blank regen."""
    question_text: str = ""
    text_template: str = ""
    blanks: List[str] = field(default_factory=list)
    accept_alternatives: List[List[str]] = field(default_factory=list)
    explanation: str = ""
    clean: bool = False
    cycles_run: int = 0
    picked_model: str = ""
    elapsed_seconds: float = 0.0
    final_violations: List[str] = field(default_factory=list)
    final_reasoning: str = ""
    final_judge_result: Dict[str, Any] = field(default_factory=dict)
    audit: List[Dict[str, Any]] = field(default_factory=list)

    def as_answer_data(self) -> Dict[str, Any]:
        return {
            'text_template': self.text_template,
            'blanks': list(self.blanks),
            'accept_alternatives': [list(a) for a in self.accept_alternatives],
        }


@dataclass
class ShortAnswerAutoRegenResult:
    """Outcome of judge-driven (auto) short_answer regen."""
    question_text: str = ""
    model_answer: str = ""
    keywords: List[str] = field(default_factory=list)
    min_keywords: int = 1
    explanation: str = ""
    clean: bool = False
    cycles_run: int = 0
    picked_model: str = ""
    elapsed_seconds: float = 0.0
    final_violations: List[str] = field(default_factory=list)
    final_reasoning: str = ""
    final_judge_result: Dict[str, Any] = field(default_factory=dict)
    audit: List[Dict[str, Any]] = field(default_factory=list)

    def as_answer_data(self) -> Dict[str, Any]:
        return {
            'model_answer': self.model_answer,
            'keywords': list(self.keywords),
            'min_keywords': int(self.min_keywords),
        }


@dataclass
class MatchingAutoRegenResult:
    """Outcome of judge-driven (auto) matching regen."""
    question_text: str = ""
    pairs: List[Dict[str, str]] = field(default_factory=list)
    distractor_rights: List[str] = field(default_factory=list)
    explanation: str = ""
    clean: bool = False
    cycles_run: int = 0
    picked_model: str = ""
    elapsed_seconds: float = 0.0
    final_violations: List[str] = field(default_factory=list)
    final_reasoning: str = ""
    final_judge_result: Dict[str, Any] = field(default_factory=dict)
    audit: List[Dict[str, Any]] = field(default_factory=list)

    def as_answer_data(self) -> Dict[str, Any]:
        return {
            'pairs': [dict(p) for p in self.pairs],
            'distractor_rights': list(self.distractor_rights),
        }


def _typed_question_regen_loop(
    *,
    original_question: Dict[str, Any],
    judge_result: Dict[str, Any],
    lesson,
    lesson_subject: str,
    lesson_grade: str,
    lesson_title: str,
    lesson_objective: str,
    step_concept_tag: str,
    enabling_objective: str,
    max_cycles: int,
    temperature_start: float,
    temperature_decay: float,
    rewrite_schema,
    prompt_builder,
    judge_fn,
    snapshot_from_parsed,
    result_class,
    judge_log_prefix: str,
):
    """Shared cycle loop for FIB / SA / matching regen.

    Each type-specific orchestrator passes its own:
      - rewrite_schema: Pydantic class for the LLM rewrite output
      - prompt_builder: fn taking (original_question, judge_result, ...) → {system, user}
      - judge_fn: re-judge function taking (question_text, answer_data, ...) → JudgeResult
      - snapshot_from_parsed: fn taking (parsed_pydantic, fallback_result) → typed_result_snapshot
      - result_class: dataclass for the final result (FillInBlankAutoRegenResult etc.)
    """
    started = time.monotonic()
    result = result_class(
        question_text=str(original_question.get('question_text') or ''),
        explanation=str(original_question.get('explanation') or ''),
    )

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
        get_llm_client(gen_config)  # validate the client constructs
    except Exception as exc:
        result.audit.append({
            'cycle': 0, 'error': f'client_construction_failed: {exc}',
            'picked': False,
        })
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

    from apps.curriculum.content_regen.score import score_candidate

    instructor_client = None
    try:
        import instructor
        provider_map = {
            'anthropic': 'anthropic', 'openai': 'openai',
            'google': 'google', 'local_ollama': 'ollama',
        }
        provider_key = provider_map.get(
            str(gen_config.provider).lower(), str(gen_config.provider).lower()
        )
        instructor_client = instructor.from_provider(
            f"{provider_key}/{gen_config.model_name}",
            api_key=gen_config.get_api_key(),
        )
    except Exception as exc:
        logger.warning(
            f"[ContentRegen] {judge_log_prefix} instructor init failed: "
            f"{type(exc).__name__}: {exc}"
        )

    judge_exclude = (gen_config.provider or '').lower() or None

    candidates: List[RegenCandidate] = []
    best: Optional[RegenCandidate] = None
    best_snap: Optional[Any] = None
    best_verdict: Dict[str, Any] = {}
    current_judge = dict(judge_result or {})
    current_question: Dict[str, Any] = dict(original_question or {})

    for cycle in range(max_cycles):
        temperature = max(
            0.0, temperature_start - cycle * temperature_decay,
        )
        c = RegenCandidate(
            cycle=cycle + 1,
            model_name=gen_config.model_name or '',
            temperature=temperature,
        )

        if instructor_client is None:
            c.error = "instructor_unavailable"
            candidates.append(c)
            continue

        prompt = prompt_builder(
            original_question=current_question,
            judge_result=current_judge,
            lesson_subject=lesson_subject,
            lesson_grade=lesson_grade,
            lesson_title=lesson_title,
            lesson_objective=lesson_objective,
            step_concept_tag=step_concept_tag,
            enabling_objective=enabling_objective,
        )

        create_kwargs = dict(
            response_model=rewrite_schema,
            messages=[
                {"role": "system", "content": prompt['system']},
                {"role": "user", "content": prompt['user']},
            ],
            max_retries=2,
        )
        # Gemini 3 thinking-budget safe (see
        # auto-memory/feedback_use_instructor_for_structured_output.md).
        if str(gen_config.provider).lower() == 'google':
            create_kwargs['generation_config'] = {'max_tokens': 4000}
        else:
            create_kwargs['max_tokens'] = 4000

        try:
            parsed = instructor_client.chat.completions.create(**create_kwargs)
        except Exception as exc:
            logger.warning(
                f"[ContentRegen] {judge_log_prefix} cycle {cycle+1} "
                f"instructor call failed: {type(exc).__name__}: {exc}"
            )
            c.error = f"gen_failed: {type(exc).__name__}"
            candidates.append(c)
            continue

        snap = snapshot_from_parsed(parsed, result)
        c.text = (snap.question_text or '')[:200]

        # Re-judge with the type-specific judge
        try:
            verdict = judge_fn(
                question_text=snap.question_text,
                answer_data=snap.as_answer_data(),
                explanation=snap.explanation,
                lesson=lesson,
                step_concept_tag=step_concept_tag,
                enabling_objective=enabling_objective,
                exclude_provider=judge_exclude,
            )
        except Exception as exc:
            logger.warning(
                f"[ContentRegen] {judge_log_prefix} cycle {cycle+1} "
                f"re-judge failed: {type(exc).__name__}: {exc}"
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

        verdict_dict = {
            'passed': bool(verdict.passed),
            'skipped': bool(verdict.skipped),
            'violations': list(verdict.violations or []),
            'reasoning': verdict.reasoning or '',
            'recommended_fix': verdict.recommended_fix or '',
            'provider': verdict.provider or '',
            'model_name': verdict.model_name or '',
            'skip_reason': verdict.skip_reason or '',
        }

        if best is None or c.score > best.score:
            best = c
            best_snap = snap
            best_verdict = verdict_dict

        current_judge = {
            'violations': c.judge_violations,
            'reasoning': c.judge_reasoning,
            'recommended_fix': verdict.recommended_fix or '',
        }
        # Refresh `current_question` so the next cycle's prompt sees
        # the latest snapshot's fields.
        current_question = {
            'question_text': snap.question_text,
            'explanation': snap.explanation,
            **snap.as_answer_data(),
        }

        if c.judge_passed and not c.judge_violations:
            best = c
            best_snap = snap
            best_verdict = verdict_dict
            break

    result.cycles_run = len(candidates)
    if best is None or best_snap is None:
        result.clean = False
        result.audit = [_candidate_to_audit(c, picked=False) for c in candidates]
        result.elapsed_seconds = time.monotonic() - started
        logger.warning(
            f"[ContentRegen] {judge_log_prefix} all {len(candidates)} "
            f"cycles errored — keeping original"
        )
        return result

    # Promote best snapshot's fields. Each result_class has its own
    # field set; the snap object is the same class, so we copy across.
    for fname in result.__dataclass_fields__:
        if fname in ('clean', 'cycles_run', 'picked_model',
                     'elapsed_seconds', 'final_violations',
                     'final_reasoning', 'final_judge_result', 'audit'):
            continue
        if hasattr(best_snap, fname):
            setattr(result, fname, getattr(best_snap, fname))

    result.clean = bool(best.judge_passed and not best.judge_violations)
    result.picked_model = best.model_name
    result.final_violations = list(best.judge_violations)
    result.final_reasoning = best.judge_reasoning
    result.final_judge_result = best_verdict
    result.audit = [
        _candidate_to_audit(c, picked=(c is best)) for c in candidates
    ]
    result.elapsed_seconds = time.monotonic() - started

    logger.info(
        f"[ContentRegen] {judge_log_prefix} "
        f"{'CLEAN' if result.clean else 'FLAGGED'} after {result.cycles_run} "
        f"cycle(s) — picked={best.model_name} "
        f"final_violations={best.judge_violations} "
        f"elapsed={result.elapsed_seconds:.1f}s"
    )
    return result


def _fib_snapshot(parsed, fallback) -> FillInBlankAutoRegenResult:
    return FillInBlankAutoRegenResult(
        question_text=(parsed.question_text or fallback.question_text)[:600],
        text_template=(parsed.text_template or '')[:1200],
        blanks=[str(b)[:200] for b in (parsed.blanks or [])],
        accept_alternatives=[
            [str(a)[:200] for a in (alts or [])]
            for alts in (parsed.accept_alternatives or [])
        ],
        explanation=(parsed.explanation or fallback.explanation or '')[:500],
    )


def _sa_snapshot(parsed, fallback) -> ShortAnswerAutoRegenResult:
    return ShortAnswerAutoRegenResult(
        question_text=(parsed.question_text or fallback.question_text)[:600],
        model_answer=(parsed.model_answer or '')[:800],
        keywords=[str(k)[:120] for k in (parsed.keywords or [])],
        min_keywords=int(parsed.min_keywords or 1),
        explanation=(parsed.explanation or fallback.explanation or '')[:500],
    )


def _match_snapshot(parsed, fallback) -> MatchingAutoRegenResult:
    return MatchingAutoRegenResult(
        question_text=(parsed.question_text or fallback.question_text)[:600],
        pairs=[
            {'left': str(p.left or '')[:200],
             'right': str(p.right or '')[:200]}
            for p in (parsed.pairs or [])
        ],
        distractor_rights=[
            str(d)[:200] for d in (parsed.distractor_rights or [])
        ],
        explanation=(parsed.explanation or fallback.explanation or '')[:500],
    )


def run_fill_in_blank_regen(
    *,
    original_question: Dict[str, Any],
    judge_result: Dict[str, Any],
    lesson,
    lesson_subject: str = "",
    lesson_grade: str = "",
    lesson_title: str = "",
    lesson_objective: str = "",
    step_concept_tag: str = "",
    enabling_objective: str = "",
    max_cycles: int = DEFAULT_MAX_CYCLES,
    temperature_start: float = DEFAULT_TEMPERATURE_START,
    temperature_decay: float = DEFAULT_TEMPERATURE_DECAY,
) -> FillInBlankAutoRegenResult:
    """Rewrite ONE fill-in-blank question until the fill_in_blank
    judge passes OR cycle cap hit. Mirrors run_exit_question_regen.
    """
    from apps.curriculum.content_judges.fill_in_blank import run_fill_in_blank_judge
    from apps.curriculum.content_regen.prompt import build_fib_auto_regen_prompt
    from apps.curriculum.content_regen.schemas import FillInBlankRewrite
    return _typed_question_regen_loop(
        original_question=original_question,
        judge_result=judge_result,
        lesson=lesson,
        lesson_subject=lesson_subject,
        lesson_grade=lesson_grade,
        lesson_title=lesson_title,
        lesson_objective=lesson_objective,
        step_concept_tag=step_concept_tag,
        enabling_objective=enabling_objective,
        max_cycles=max_cycles,
        temperature_start=temperature_start,
        temperature_decay=temperature_decay,
        rewrite_schema=FillInBlankRewrite,
        prompt_builder=build_fib_auto_regen_prompt,
        judge_fn=run_fill_in_blank_judge,
        snapshot_from_parsed=_fib_snapshot,
        result_class=FillInBlankAutoRegenResult,
        judge_log_prefix='fill_in_blank',
    )


def run_short_answer_regen(
    *,
    original_question: Dict[str, Any],
    judge_result: Dict[str, Any],
    lesson,
    lesson_subject: str = "",
    lesson_grade: str = "",
    lesson_title: str = "",
    lesson_objective: str = "",
    step_concept_tag: str = "",
    enabling_objective: str = "",
    max_cycles: int = DEFAULT_MAX_CYCLES,
    temperature_start: float = DEFAULT_TEMPERATURE_START,
    temperature_decay: float = DEFAULT_TEMPERATURE_DECAY,
) -> ShortAnswerAutoRegenResult:
    """Rewrite ONE short-answer question until the short_answer judge
    passes OR cycle cap hit.
    """
    from apps.curriculum.content_judges.short_answer import run_short_answer_judge
    from apps.curriculum.content_regen.prompt import build_sa_auto_regen_prompt
    from apps.curriculum.content_regen.schemas import ShortAnswerRewrite
    return _typed_question_regen_loop(
        original_question=original_question,
        judge_result=judge_result,
        lesson=lesson,
        lesson_subject=lesson_subject,
        lesson_grade=lesson_grade,
        lesson_title=lesson_title,
        lesson_objective=lesson_objective,
        step_concept_tag=step_concept_tag,
        enabling_objective=enabling_objective,
        max_cycles=max_cycles,
        temperature_start=temperature_start,
        temperature_decay=temperature_decay,
        rewrite_schema=ShortAnswerRewrite,
        prompt_builder=build_sa_auto_regen_prompt,
        judge_fn=run_short_answer_judge,
        snapshot_from_parsed=_sa_snapshot,
        result_class=ShortAnswerAutoRegenResult,
        judge_log_prefix='short_answer',
    )


def run_matching_regen(
    *,
    original_question: Dict[str, Any],
    judge_result: Dict[str, Any],
    lesson,
    lesson_subject: str = "",
    lesson_grade: str = "",
    lesson_title: str = "",
    lesson_objective: str = "",
    step_concept_tag: str = "",
    enabling_objective: str = "",
    max_cycles: int = DEFAULT_MAX_CYCLES,
    temperature_start: float = DEFAULT_TEMPERATURE_START,
    temperature_decay: float = DEFAULT_TEMPERATURE_DECAY,
) -> MatchingAutoRegenResult:
    """Rewrite ONE matching question until the matching judge passes
    OR cycle cap hit.
    """
    from apps.curriculum.content_judges.matching import run_matching_judge
    from apps.curriculum.content_regen.prompt import build_match_auto_regen_prompt
    from apps.curriculum.content_regen.schemas import MatchingRewrite
    return _typed_question_regen_loop(
        original_question=original_question,
        judge_result=judge_result,
        lesson=lesson,
        lesson_subject=lesson_subject,
        lesson_grade=lesson_grade,
        lesson_title=lesson_title,
        lesson_objective=lesson_objective,
        step_concept_tag=step_concept_tag,
        enabling_objective=enabling_objective,
        max_cycles=max_cycles,
        temperature_start=temperature_start,
        temperature_decay=temperature_decay,
        rewrite_schema=MatchingRewrite,
        prompt_builder=build_match_auto_regen_prompt,
        judge_fn=run_matching_judge,
        snapshot_from_parsed=_match_snapshot,
        result_class=MatchingAutoRegenResult,
        judge_log_prefix='matching',
    )


__all__ = [
    "DEFAULT_MAX_CYCLES",
    "DEFAULT_TEMPERATURE_START",
    "DEFAULT_TEMPERATURE_DECAY",
    "RegenCandidate",
    "RegenResult",
    "ExitQuestionRegenResult",
    "ExitQuestionAutoRegenResult",
    "FillInBlankAutoRegenResult",
    "ShortAnswerAutoRegenResult",
    "MatchingAutoRegenResult",
    "run_step_regen",
    "run_step_prompt_regen",
    "run_exit_question_prompt_regen",
    "run_exit_question_regen",
    "run_fill_in_blank_regen",
    "run_short_answer_regen",
    "run_matching_regen",
]
