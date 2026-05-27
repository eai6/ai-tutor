"""Prompt-rule ↔ eval-check coverage registry.

Each behavioural rule in the simple_tutor system prompt is registered
here with the eval checks that policed it. The audit + iteration loop
relies on this contract:

  *A rule without at least one check is a process bug.*

If we add a rule to the prompt without wiring an eval check, regressions
land in production without any tripwire. If we drop a rule, the check
should drop with it — orphan checks scoring against rules the prompt
no longer enforces are just noise.

This module is a registry, NOT a scorer. It produces a coverage report
that ``evals.report`` consumes. The actual rule-policing happens in:

  - ``evals.scorers.deterministic`` (regex-based, no LLM call)
  - ``evals.scorers.llm_rubric.score_pedagogical_dimensions`` (single
    judge call returning per-dimension yes/no verdicts)
  - Per-scenario ``rubric:`` blocks (free-form judge items, for the
    narrow scenarios that need them)

Rule IDs match the audit doc (``evals/reports/prompt_audit_2026-05-27.md``)
so a single grep over a rule ID lights up the prompt, the audit, the
registry, and the test.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from evals.scorers import deterministic as _det
from evals.scorers.llm_rubric import PEDAGOGICAL_DIMENSIONS


@dataclass
class RuleEntry:
    """One prompt rule + the eval checks that police it."""

    id: str
    name: str                   # short human label
    location: str               # 'prompt:R02' / 'tool:pose_question' / etc.
    summary: str                # one-line description of the rule
    deterministic_verbs: list[str] = field(default_factory=list)
    judge_dimensions: list[str] = field(default_factory=list)
    notes: str = ''

    @property
    def is_covered(self) -> bool:
        """A rule is covered when AT LEAST ONE check claims it."""
        return bool(self.deterministic_verbs) or bool(self.judge_dimensions)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
#
# Order matches the audit. Keep new entries in audit-doc order so a
# reader can scan both side-by-side.

RULES: list[RuleEntry] = [
    RuleEntry(
        id='R01',
        name='REMEDIATION mode',
        location='prompt:dynamic_block (conditional on exit_ticket_review)',
        summary=(
            'When an <exit_ticket_review> block is present, target the '
            'failing objectives and end with advance_step so the platform '
            're-fires the exit ticket.'
        ),
        judge_dimensions=['coherence', 'actionability'],
        notes=(
            'Conditionally rendered — only appears when exit_ticket_review '
            'is populated. Multi-turn remediation scenarios exercise the '
            'end-to-end behaviour; the dimensions judge polices each '
            'individual turn.'
        ),
    ),
    RuleEntry(
        id='R02',
        name='GRADE/POSE mode dispatcher',
        location='prompt:rules',
        summary=(
            'When <in_flight_question> is present: call record_answer. '
            'When absent: teach or pose. Do NOT pose a NEW question on a '
            'WRONG verdict in the same turn.'
        ),
        deterministic_verbs=['response_nonempty', 'must_not_label'],
        judge_dimensions=['coherence', 'mistake_identification'],
    ),
    RuleEntry(
        id='R04',
        name='5E phase adaptation',
        location='prompt:rules',
        summary=(
            'Adapt to the active 5E phase (Engage/Explore/Explain/'
            'Elaborate/Evaluate). On Explain turns, deliver content AND '
            'end with one check-for-understanding question.'
        ),
        judge_dimensions=['providing_guidance', 'actionability'],
    ),
    RuleEntry(
        id='R05',
        name='Deliver content on Explain',
        location='prompt:rules',
        summary='Give step-by-step procedures, not only questions.',
        judge_dimensions=['providing_guidance'],
    ),
    RuleEntry(
        id='R07',
        name='Tutor-driven and actionable',
        location='prompt:rules',
        summary=(
            'Every reply ends with ONE concrete action for the student. '
            'Banned passive endings: "take your time", "ready for the '
            'next one?", etc.'
        ),
        deterministic_verbs=['passive_ending'],
        judge_dimensions=['actionability'],
    ),
    RuleEntry(
        id='R08',
        name='Question-type allowlist',
        location='tool:pose_question (question_type enum + description)',
        summary=(
            'Only mcq / short_numeric / short_answer. No fill_in_blank or '
            'matching.'
        ),
        notes=(
            'Migrated from prompt to tool description per 2026-05-27 '
            'audit. Enforcement is at the schema level — invalid '
            'question_type values are rejected by Anthropic strict tool '
            'mode before they reach the handler. No eval check needed.'
        ),
    ),
    RuleEntry(
        id='R09',
        name='Reason carefully about reference_answer (INTERNAL only)',
        location='tool:pose_question (description)',
        summary=(
            'Before pose_question, mentally walk through the question '
            'and commit to the reference. This reasoning is INTERNAL — '
            'it does NOT appear in the visible text reply.'
        ),
        deterministic_verbs=['meta_reasoning_leak'],
        notes=(
            'The "INTERNAL only" half is the conflict resolution with '
            'R15 — see Conflict 3 in the audit. The meta_reasoning_leak '
            'regex catches the prose-leak failure mode observed in prod.'
        ),
    ),
    RuleEntry(
        id='R10',
        name='Literal extracted_answer (no auto-correct)',
        location='tool:record_answer (extracted_answer description)',
        summary=(
            'Pass what the STUDENT typed, never auto-corrected to the '
            'right answer.'
        ),
        notes=(
            'Migrated from prompt to tool description per 2026-05-27 '
            'audit. Polised indirectly via the must_not_label '
            "FALSE_POSITIVE_GRADING tag on the math_false_accept_numeric "
            'scenario.'
        ),
    ),
    RuleEntry(
        id='R11',
        name='Call advance_step when ready',
        location='tool:advance_step (description)',
        summary=(
            'Soft hint — call once content delivered and student shows '
            'understanding. Platform also auto-advances.'
        ),
        notes=(
            'Migrated from prompt to tool description per 2026-05-27 '
            'audit. Multi-turn scenarios exercise the advance_step '
            'pathway; per-turn dimensions cover the related "ready to '
            'move" judgement.'
        ),
    ),
    RuleEntry(
        id='R12',
        name='Figure rule (request_figure only with catalog ids)',
        location='prompt:rules (templated, drops when images disabled)',
        summary=(
            'Reference figures only via request_figure(figure_id) using '
            'ids from <figure_catalog>. Do not invent ids.'
        ),
        notes=(
            'Enforced at handler level (handle_request_figure rejects '
            'unknown ids). Image-disabled lessons also strip the tool '
            'from the toolset entirely.'
        ),
    ),
    RuleEntry(
        id='R13',
        name='Off-topic redirect after 2 consecutive turns',
        location='tool:redirect_off_topic (description)',
        summary=(
            'Call after two off-topic student turns. Handler increments '
            'engine_state["off_topic_count"].'
        ),
        notes='Migrated from prompt to tool description per 2026-05-27 audit.',
    ),
    RuleEntry(
        id='R14',
        name='Do not reveal reference answers',
        location='prompt:rules',
        summary=(
            'Reference answer is for grading only. Banned phrasings: '
            '"the answer is X", "the correct option is X", etc.'
        ),
        deterministic_verbs=['must_not_label'],
        judge_dimensions=['reveals_answer'],
    ),
    RuleEntry(
        id='R15',
        name='Speak to the student, not about them',
        location='prompt:rules',
        summary=(
            'Reply is in 2nd person. No "the student…", "I shouldn\'t…", '
            '"let me prompt…". Reasoning stays internal.'
        ),
        deterministic_verbs=['meta_reasoning_leak'],
        judge_dimensions=['human_likeness'],
    ),
    RuleEntry(
        id='R16',
        name='Wrong-answer hint ladder',
        location='prompt:rules',
        summary=(
            'attempt_count = 0 → small hint. 1 → deeper. >= 2 → keep '
            'scaffolding (worked micro-example) over revealing. Pivot '
            'only when hints stall.'
        ),
        judge_dimensions=[
            'providing_guidance', 'reveals_answer', 'mistake_identification',
        ],
    ),
    RuleEntry(
        id='R17',
        name='Trust the grader (anti-sycophancy)',
        location='prompt:rules',
        summary=(
            'Student may sound confident about a wrong answer. Trust '
            'the grader\'s verdict, not the student\'s tone.'
        ),
        judge_dimensions=['mistake_identification'],
    ),
]


# ---------------------------------------------------------------------------
# Coverage check
# ---------------------------------------------------------------------------


def _known_deterministic_verbs() -> set[str]:
    """Verbs the deterministic scorer actually handles. Anything in the
    registry's ``deterministic_verbs`` field that's not in this set is a
    typo / orphan reference.
    """
    return set(_det._HANDLERS.keys())


def _known_judge_dimensions() -> set[str]:
    return {name for name, _q, _d in PEDAGOGICAL_DIMENSIONS}


@dataclass
class CoverageReport:
    rules: list[RuleEntry]
    uncovered: list[RuleEntry]           # rule with zero checks
    unknown_verbs: list[tuple[str, str]] # (rule_id, verb)
    unknown_dimensions: list[tuple[str, str]]  # (rule_id, dim_name)

    @property
    def is_clean(self) -> bool:
        """A clean coverage report has no uncovered rules and no
        registry references to verbs/dimensions that don't exist in
        the scoring layer.
        """
        return (
            not self.uncovered
            and not self.unknown_verbs
            and not self.unknown_dimensions
        )


def build_coverage_report() -> CoverageReport:
    """Cross-check ``RULES`` against the scoring layer.

    Returns a populated ``CoverageReport``. Callers (the report renderer,
    a test, or the developer at a REPL) inspect ``is_clean`` and the
    three lists to diagnose problems.
    """
    known_verbs = _known_deterministic_verbs()
    known_dims = _known_judge_dimensions()

    uncovered: list[RuleEntry] = []
    unknown_verbs: list[tuple[str, str]] = []
    unknown_dims: list[tuple[str, str]] = []

    for rule in RULES:
        if not rule.is_covered:
            # A rule with notes that explicitly say "no check needed"
            # (schema-level, handler-level, migrated to tool) is still
            # "uncovered" by the eval layer but acknowledged. We surface
            # it; the human reading the report decides whether to add
            # one or not.
            uncovered.append(rule)
        for verb in rule.deterministic_verbs:
            if verb not in known_verbs:
                unknown_verbs.append((rule.id, verb))
        for dim in rule.judge_dimensions:
            if dim not in known_dims:
                unknown_dims.append((rule.id, dim))

    return CoverageReport(
        rules=list(RULES),
        uncovered=uncovered,
        unknown_verbs=unknown_verbs,
        unknown_dimensions=unknown_dims,
    )


def format_coverage_report(report: CoverageReport) -> str:
    """Render the coverage report as plain text for the eval summary."""
    lines: list[str] = []
    lines.append("PROMPT-RULE COVERAGE")
    total = len(report.rules)
    covered = sum(1 for r in report.rules if r.is_covered)
    lines.append(f"  {covered}/{total} rules have at least one eval check")

    if report.uncovered:
        lines.append('')
        lines.append("  Rules with NO eval check (accept-with-notes only):")
        id_width = max(len(r.id) for r in report.uncovered)
        for r in report.uncovered:
            lines.append(f"    {r.id:<{id_width}}  {r.name}")
            if r.notes:
                first_note_line = r.notes.split('\n')[0]
                lines.append(f"      └─ {first_note_line[:90]}")

    if report.unknown_verbs:
        lines.append('')
        lines.append("  ⚠ Registry references unknown deterministic verbs:")
        for rid, verb in report.unknown_verbs:
            lines.append(f"    {rid}: {verb!r}")

    if report.unknown_dimensions:
        lines.append('')
        lines.append("  ⚠ Registry references unknown judge dimensions:")
        for rid, dim in report.unknown_dimensions:
            lines.append(f"    {rid}: {dim!r}")

    return '\n'.join(lines)
