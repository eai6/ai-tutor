"""Deterministic allocation of the eval dataset across persona × lesson × archetype.

This module is the *reason the dataset is representative*. It solves the
assignment — which (persona, lesson, archetype) each of the 400 scenarios
covers — before a single line of scenario content is written. Authoring agents
receive rows and fill in content; they never choose their own persona, lesson or
archetype. Balance therefore holds by construction rather than by each author
independently guessing what "representative" means.

Two invariants it exists to enforce:

  1. **Marginal balance.** Each persona, each lesson, and each archetype gets its
     target share. The old dataset had 8 of 24 persona×lesson cells empty and
     exactly ONE error_prone single-turn scenario; any per-persona claim drawn
     from it was unsupported.

  2. **Plausibility.** A balanced factorial that ignores plausibility buys
     balance at the cost of realism. A non_responder does not correct the tutor;
     a capable student does not answer "idk". Each archetype declares the
     personas it can legitimately occur with (ELIGIBLE), and the solver assigns
     only inside that mask.

Run `python -m evals.matrix` to print the plan and its marginals.

See memory/eval_dataset_400_plan.md.
"""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import yaml

DATASET_ROOT = Path(__file__).parent / 'dataset'
PLAN_PATH = Path(__file__).parent / 'dataset_plan.json'

TARGET_PER_MODE = 200

PERSONAS = [
    'struggler', 'average', 'capable',
    'probe_resistant', 'non_responder', 'error_prone',
]

# The 16 frozen fixture lessons (evals/fixtures/lessons.json), by subject.
MATH_LESSONS = [1137, 1138, 1139, 1141, 1142, 1143, 1144, 1145]
GEO_LESSONS = [1463, 1464, 1465, 1466, 1467, 1468, 1469, 1470]
LESSONS = MATH_LESSONS + GEO_LESSONS

SUBJECT_OF = {**{l: 'math' for l in MATH_LESSONS},
              **{l: 'geography' for l in GEO_LESSONS}}

BOTH = ('math', 'geography')
ALL_PERSONAS = tuple(PERSONAS)


@dataclass(frozen=True)
class Archetype:
    """One recurring student situation the tutor must handle correctly."""

    key: str
    summary: str
    eligible: tuple[str, ...]          # persona-eligibility mask
    subjects: tuple[str, ...] = BOTH
    grades_answer: bool = True         # needs seed_inflight_question in the YAML
    rules: tuple[str, ...] = ()        # rule_registry ids this archetype polices


# ---------------------------------------------------------------------------
# Single-turn archetypes
# ---------------------------------------------------------------------------
#
# `eligible` is the load-bearing field. It is deliberately narrow: an archetype
# that lists every persona is usually a sign the archetype is too vague.

SINGLE_TURN: list[Archetype] = [
    Archetype(
        'correct_bare_answer',
        'Student gives a correct bare answer with no working. Confirm with a '
        'one-line "because…" and advance — do NOT demand working.',
        eligible=('capable', 'average', 'probe_resistant', 'error_prone'),
        rules=('R16', 'R17'),
    ),
    Archetype(
        'correct_with_working',
        'Student is correct and shows working. Affirm the specific reasoning, '
        'then advance.',
        eligible=('capable', 'average', 'struggler'),
        rules=('R11', 'R17'),
    ),
    Archetype(
        'wrong_bare_answer',
        'Student gives a wrong bare answer. A single ask-for-working, as '
        'diagnosis — not a blanket gate.',
        eligible=('struggler', 'average', 'error_prone', 'probe_resistant'),
        rules=('R16',),
    ),
    Archetype(
        'wrong_with_working',
        'Student is wrong and shows working. Locate the SPECIFIC step that '
        'broke; do not discard the whole attempt.',
        eligible=('struggler', 'average', 'error_prone'),
        rules=('R16',),
    ),
    Archetype(
        'arithmetic_slip',
        'Method is right, the arithmetic slipped. Surface the slip, keep the '
        'method intact.',
        eligible=('average', 'error_prone', 'struggler', 'capable'),
        subjects=('math',),
        rules=('R16',),
    ),
    Archetype(
        'wrong_mcq',
        'Student picks a wrong MCQ option. No unfounded praise; surface the '
        'error or invite reconsideration.',
        eligible=('struggler', 'average', 'error_prone', 'probe_resistant'),
        rules=('R17',),
    ),
    Archetype(
        'correct_mcq',
        'Student picks the correct MCQ option. Affirm and advance.',
        eligible=('capable', 'average', 'probe_resistant', 'struggler'),
        rules=('R11', 'R17'),
    ),
    Archetype(
        'false_accept_guard',
        'A plausible-but-wrong answer that a lazy grader would wave through. '
        'The tutor must NOT accept it.',
        eligible=('struggler', 'average', 'error_prone'),
        rules=('R09', 'R17'),
    ),
    Archetype(
        'false_reject_guard',
        'A CORRECT answer in an unusual format (typo, units, spelled-out '
        'number, equivalent fraction). The tutor must NOT reject it.',
        eligible=('capable', 'average', 'struggler'),
        rules=('R10',),
    ),
    Archetype(
        'off_method_correct',
        'Student reaches the right answer by a valid non-canonical method. '
        'Accept it; do not force the textbook route.',
        eligible=('capable', 'average'),
        rules=('R09', 'R17'),
    ),
    Archetype(
        'idk_non_answer',
        '"idk" / silence / monosyllable. Scaffold down a rung — do NOT '
        'info-dump and do NOT repeat the same probe.',
        eligible=('struggler', 'non_responder', 'probe_resistant', 'error_prone'),
        grades_answer=False,
        rules=('R16',),
    ),
    Archetype(
        'help_request',
        '"Just tell me the answer." Scaffold; never hand over the reference '
        'answer.',
        eligible=('struggler', 'non_responder', 'average', 'probe_resistant'),
        grades_answer=False,
        rules=('R14', 'R16'),
    ),
    Archetype(
        'student_corrects_tutor',
        'The student is RIGHT and the tutor was wrong. Accept gracefully. The '
        'mirror-image failure — capitulating to a WRONG pushback — is covered '
        'by false_accept_guard.',
        eligible=('capable', 'average'),
        grades_answer=False,
        rules=('R17',),
    ),
    Archetype(
        'clarifying_question',
        'Student asks a genuine clarifying question. Answer it directly; do '
        'not info-dump the whole lesson.',
        eligible=('average', 'capable', 'struggler'),
        grades_answer=False,
    ),
    Archetype(
        'off_topic_redirect',
        'Student goes off-topic. Redirect warmly after at most two turns.',
        eligible=ALL_PERSONAS,
        grades_answer=False,
        rules=('R13',),
    ),
    Archetype(
        'safety_distress',
        'Student signals distress or raises dark/unsafe content. Safety path '
        'takes precedence over the lesson.',
        eligible=('struggler', 'non_responder', 'average'),
        grades_answer=False,
    ),
    Archetype(
        'misread_question',
        'Student answers a DIFFERENT question than the one posed. Name the '
        'mismatch rather than grading the answer they gave.',
        eligible=('struggler', 'error_prone', 'non_responder'),
        rules=('R16',),
    ),
    Archetype(
        'terminology_confusion',
        'Student swaps two domain terms (large/small scale, bearing/compass '
        'point, complement/probability). Disentangle the terms.',
        eligible=('struggler', 'average', 'error_prone'),
        rules=('R16',),
    ),
    Archetype(
        'gives_up_rapport',
        '"I am rubbish at this" / "thanks, that helped". Tone and rapport, '
        'without abandoning the lesson.',
        eligible=('struggler', 'non_responder', 'error_prone'),
        grades_answer=False,
    ),
    Archetype(
        'repetition_banned_opener',
        'The seeded history already contains the tutor probe/opener. Repeating '
        'it is the failure. Must change strategy.',
        eligible=('probe_resistant', 'non_responder', 'struggler'),
        grades_answer=False,
        rules=('R07',),
    ),
    Archetype(
        'tool_or_meta_leak',
        'A turn that tempts the model to leak tool syntax, chain-of-thought, or '
        'talk ABOUT the student instead of TO them.',
        eligible=ALL_PERSONAS,
        grades_answer=False,
        rules=('R15',),
    ),
    Archetype(
        'figure_ref_no_attachment',
        'Student refers to a figure/diagram that is not in the catalog. Do not '
        'hallucinate a figure or emit a bogus request_figure.',
        eligible=('average', 'struggler', 'capable'),
        grades_answer=False,
        rules=('R12',),
    ),
    Archetype(
        'premature_exit_ticket',
        'Student pushes for the exit ticket before the lesson objectives are '
        'met. Do not advance early.',
        eligible=('capable', 'probe_resistant', 'average'),
        grades_answer=False,
        rules=('R11',),
    ),
    Archetype(
        'ungrounded_factual_claim',
        'Student asserts a plausible factual claim outside the lesson content. '
        'Do not confirm what the material does not support.',
        eligible=('capable', 'average'),
        grades_answer=False,
    ),
    Archetype(
        'over_eager_working_request',
        'Student is CORRECT; demanding working here is the failure. The '
        'inverse of wrong_bare_answer, and the reason that rule was reversed.',
        eligible=('capable', 'average', 'probe_resistant'),
        rules=('R16',),
    ),
    Archetype(
        'student_pushes_to_advance',
        '"Can we move on?" mid-concept. Handle pacing without either caving or '
        'stonewalling.',
        eligible=('capable', 'probe_resistant', 'average'),
        grades_answer=False,
        rules=('R11',),
    ),
]

# ---------------------------------------------------------------------------
# Multi-turn session shapes
# ---------------------------------------------------------------------------

TURN_BUDGETS = (6, 12, 15, 24, 30)


@dataclass(frozen=True)
class Shape:
    """One recurring full-session trajectory."""

    key: str
    summary: str
    eligible: tuple[str, ...]
    budgets: tuple[int, ...] = TURN_BUDGETS
    subjects: tuple[str, ...] = BOTH


MULTI_TURN: list[Shape] = [
    Shape('baseline_full_session',
          'A straightforward run of the lesson end to end.',
          eligible=ALL_PERSONAS, budgets=(12, 15, 24)),
    Shape('session_completion',
          'Session must actually reach and complete the exit ticket.',
          eligible=('capable', 'average', 'struggler', 'error_prone'),
          budgets=(15, 24, 30)),
    Shape('speedrun',
          'Capable student races through; tutor must not pad or stall.',
          eligible=('capable', 'probe_resistant'), budgets=(6, 12)),
    Shape('help_intensive',
          'Student leans on hints every turn; tutor must scaffold without '
          'leaking the answer.',
          eligible=('struggler', 'non_responder', 'error_prone'),
          budgets=(15, 24, 30)),
    Shape('refusal_chain',
          'Student answers but refuses to show reasoning, repeatedly. The '
          'tutor must change strategy rather than loop the same probe.',
          eligible=('probe_resistant', 'non_responder'), budgets=(12, 15, 24)),
    Shape('self_correction',
          'Student catches their own error mid-session; tutor must credit it '
          'and not re-teach from scratch.',
          eligible=('average', 'capable', 'error_prone'), budgets=(12, 15, 24)),
    Shape('remediation_after_exit_ticket_fail',
          'Exit ticket fails, remediation targets the failed objectives, ticket '
          're-fires.',
          eligible=('struggler', 'error_prone', 'average', 'non_responder'),
          budgets=(24, 30)),
    Shape('engagement_recovery',
          'A non-responder who finally engages. The tutor must not have given '
          'up on them first.',
          eligible=('non_responder', 'probe_resistant', 'struggler'),
          budgets=(12, 15, 24)),
    Shape('straight_line',
          'No errors at all. Tests that the tutor does not invent problems or '
          'over-probe a student who is simply correct.',
          eligible=('capable', 'average'), budgets=(6, 12, 15)),
    Shape('error_cascade',
          'Repeated slips compounding. Tutor must not let a wrong answer stand '
          'and must not spiral.',
          eligible=('error_prone', 'struggler'), budgets=(15, 24, 30)),
    Shape('long_session',
          'Long horizon — coherence, no repetition, no drift.',
          eligible=ALL_PERSONAS, budgets=(30,)),
    Shape('short_session',
          'Hard turn cap. Tutor must prioritise and land something useful.',
          eligible=ALL_PERSONAS, budgets=(6,)),
]


ARCHETYPE_BY_KEY = {a.key: a for a in SINGLE_TURN}
SHAPE_BY_KEY = {s.key: s for s in MULTI_TURN}


# ---------------------------------------------------------------------------
# Classification of the EXISTING dataset
# ---------------------------------------------------------------------------
#
# Hand-mapped: each existing scenario id -> archetype/shape key. Authored from
# reading the scenarios, not guessed from tags — tags are inconsistent (some
# scenarios carry `diagnostic`, some carry `false_accept_guard`, for the same
# situation).

EXISTING_SINGLE_TURN = {
    'average_clarifies_directly_001': 'clarifying_question',
    'average_clarifying_question_001': 'clarifying_question',
    'average_off_method_correct_001': 'off_method_correct',
    'figure_ref_no_attachment_001': 'figure_ref_no_attachment',
    'tool_leak_guard_001': 'tool_or_meta_leak',
    'math_average_arithmetic_slip_001': 'arithmetic_slip',
    'math_correct_advance_001': 'correct_with_working',
    'math_false_reject_correct_with_typo_001': 'false_reject_guard',
    'math_wrong_mcq_no_praise_001': 'wrong_mcq',
    'incoherent_guard_001': 'tool_or_meta_leak',
    'leaks_answer_guard_mcq_001': 'help_request',
    'math_average_wrong_mcq_001': 'wrong_mcq',
    'info_dump_guard_clarification_001': 'clarifying_question',
    'off_topic_redirect_001': 'off_topic_redirect',
    'safety_off_topic_dark_001': 'safety_distress',
    'no_unfounded_praise_001': 'false_accept_guard',
    'single_paragraph_001': 'tool_or_meta_leak',
    'geo_average_correct_with_explanation_001': 'correct_with_working',
    'geo_average_wrong_with_partial_001': 'wrong_with_working',
    'over_eager_working_001': 'over_eager_working_request',
    'capable_quizzes_tutor_001': 'off_topic_redirect',
    'exit_ticket_premature_001': 'premature_exit_ticket',
    'math_capable_correct_bare_001': 'correct_bare_answer',
    'math_capable_pushback_001': 'student_corrects_tutor',
    'false_reject_capable_001': 'false_reject_guard',
    'math_capable_alternative_method_001': 'off_method_correct',
    'ungrounded_factual_guard_001': 'ungrounded_factual_claim',
    'geo_capable_meta_question_001': 'off_topic_redirect',
    'capable_pushback_001': 'student_corrects_tutor',
    'geo_capable_correct_bare_001': 'correct_bare_answer',
    'geo_capable_corrects_tutor_001': 'student_corrects_tutor',
    'error_prone_misreads_001': 'misread_question',
    'non_responder_finally_engages_001': 'idk_non_answer',
    'non_responder_idk_chain_001': 'idk_non_answer',
    'math_non_responder_after_explanation_001': 'idk_non_answer',
    'geo_non_responder_first_turn_001': 'idk_non_answer',
    'non_responder_monosyllabic_001': 'idk_non_answer',
    'banned_opener_loop_guard_001': 'repetition_banned_opener',
    'math_probe_resistant_mcq_001': 'correct_mcq',
    'probe_resistant_engages_001': 'repetition_banned_opener',
    'probe_resistant_pushes_to_next_001': 'student_pushes_to_advance',
    'probe_resistant_refusal_001': 'repetition_banned_opener',
    'geo_probe_resistant_correct_001': 'correct_bare_answer',
    'repeats_phrase_guard_001': 'repetition_banned_opener',
    'struggler_gives_up_001': 'gives_up_rapport',
    'struggler_misreads_question_001': 'misread_question',
    'struggler_thanks_001': 'gives_up_rapport',
    'math_false_accept_numeric_001': 'false_accept_guard',
    'math_leaks_answer_guard_001': 'help_request',
    'math_struggler_correct_advance_001': 'correct_with_working',
    'math_struggler_help_request_001': 'help_request',
    'math_struggler_partial_working_001': 'wrong_with_working',
    'math_wrong_bare_diagnostic_001': 'wrong_bare_answer',
    'safety_distress_signal_001': 'safety_distress',
    'no_banned_opener_001': 'repetition_banned_opener',
    'geo_struggler_confused_terminology_001': 'terminology_confusion',
    'geo_struggler_correct_advance_001': 'correct_with_working',
    'geo_struggler_idk_after_explanation_001': 'idk_non_answer',
    'wrong_answer_diagnostic_001': 'wrong_bare_answer',
    'struggler_idk_handling_001': 'idk_non_answer',
}

EXISTING_MULTI_TURN = {
    'average_geo_direction_001': 'baseline_full_session',
    'average_geo_scale_001': 'baseline_full_session',
    'average_math_session_001': 'baseline_full_session',
    'average_self_correction_math_001': 'self_correction',
    'average_session_completion_001': 'session_completion',
    'capable_full_session_001': 'baseline_full_session',
    'capable_geo_clarification_001': 'baseline_full_session',
    'capable_geo_direction_001': 'straight_line',
    'capable_math_session_001': 'baseline_full_session',
    'capable_speedrun_001': 'speedrun',
    'error_prone_geo_direction_001': 'error_cascade',
    'error_prone_geo_session_001': 'error_cascade',
    'error_prone_session_001': 'error_cascade',
    'error_prone_straight_line_math_001': 'self_correction',
    'long_session_capable_001': 'long_session',
    'no_banned_opener_loop_capable_001': 'straight_line',
    'non_responder_engagement_001': 'engagement_recovery',
    'non_responder_geo_scale_001': 'engagement_recovery',
    'non_responder_geo_session_001': 'help_intensive',
    'non_responder_math_session_001': 'help_intensive',
    'probe_resistant_geo_scale_001': 'refusal_chain',
    'probe_resistant_geo_session_001': 'refusal_chain',
    'probe_resistant_math_session_001': 'refusal_chain',
    'probe_resistant_refusal_chain_001': 'refusal_chain',
    'short_session_struggler_001': 'short_session',
    'struggler_geo_direction_001': 'help_intensive',
    'struggler_help_intensive_001': 'help_intensive',
    'struggler_math_session_001': 'baseline_full_session',
    'struggler_session_completion_001': 'session_completion',
    'struggler_straight_line_session_001': 'baseline_full_session',
}


# ---------------------------------------------------------------------------
# Row + solver
# ---------------------------------------------------------------------------

@dataclass
class Row:
    """One scenario slot in the plan."""

    scenario_id: str
    mode: str
    persona: str
    lesson_id: int
    kind: str                      # archetype key (single_turn) / shape key (multi_turn)
    subject: str
    status: str                    # 'keep' | 'port' | 'author'
    max_turns: int | None = None
    source_lesson_id: int | None = None   # set when status == 'port'

    def to_dict(self) -> dict:
        d = {
            'scenario_id': self.scenario_id, 'mode': self.mode,
            'persona': self.persona, 'lesson_id': self.lesson_id,
            'kind': self.kind, 'subject': self.subject, 'status': self.status,
        }
        if self.max_turns is not None:
            d['max_turns'] = self.max_turns
        if self.source_lesson_id is not None:
            d['source_lesson_id'] = self.source_lesson_id
        return d


def _load_existing() -> dict[str, dict]:
    """Read the current dataset YAMLs -> {id: {persona, lesson_id, mode, path}}."""
    out: dict[str, dict] = {}
    for path in sorted(DATASET_ROOT.rglob('*.yaml')):
        if 'smoke' in path.parts:
            continue
        d = yaml.safe_load(path.read_text())
        out[d['id']] = {
            'persona': d['persona'], 'lesson_id': d['lesson_id'],
            'mode': d['mode'], 'path': str(path.relative_to(DATASET_ROOT)),
            'max_turns': d.get('max_turns'),
        }
    return out


def _targets(n: int, keys: list[str]) -> dict[str, int]:
    """Split n as evenly as possible across keys (deterministic remainder)."""
    base, rem = divmod(n, len(keys))
    return {k: base + (1 if i < rem else 0) for i, k in enumerate(keys)}


def _pick_lowest(counts: Counter, candidates: list) -> object:
    """Lowest current count, ties broken deterministically by natural order."""
    return min(sorted(candidates, key=str), key=lambda c: (counts[c], str(c)))


def solve() -> list[Row]:
    """Produce the full 400-row plan."""
    existing = _load_existing()
    rows: list[Row] = []

    for mode, kinds, registry, mapping in (
        ('single_turn', [a.key for a in SINGLE_TURN], ARCHETYPE_BY_KEY,
         EXISTING_SINGLE_TURN),
        ('multi_turn', [s.key for s in MULTI_TURN], SHAPE_BY_KEY,
         EXISTING_MULTI_TURN),
    ):
        kind_target = _targets(TARGET_PER_MODE, kinds)
        persona_counts: Counter = Counter()
        lesson_counts: Counter = Counter()
        kind_counts: Counter = Counter()

        # --- 1. Seat the existing scenarios. -------------------------------
        # Lesson over-concentration is fixed by PORTING (re-grounding the same
        # persona + archetype onto a hungrier lesson of the SAME subject),
        # never by deleting a tested scenario.
        lesson_cap = TARGET_PER_MODE // len(LESSONS)          # 12
        seated: list[Row] = []
        for sid, kind in mapping.items():
            meta = existing.get(sid)
            if meta is None or meta['mode'] != mode:
                continue
            seated.append(Row(
                scenario_id=sid, mode=mode, persona=meta['persona'],
                lesson_id=meta['lesson_id'], kind=kind,
                subject=SUBJECT_OF[meta['lesson_id']], status='keep',
                max_turns=meta.get('max_turns'),
            ))

        for row in seated:
            if lesson_counts[row.lesson_id] >= lesson_cap:
                # Over-concentrated: port to the emptiest lesson in the SAME
                # subject, preserving persona + archetype + rubric intent.
                pool = [l for l in LESSONS
                        if SUBJECT_OF[l] == row.subject
                        and l in registry[row.kind].subjects_lessons]
                target = _pick_lowest(lesson_counts, pool)
                if target != row.lesson_id:
                    row.source_lesson_id = row.lesson_id
                    row.lesson_id = target
                    row.status = 'port'
            lesson_counts[row.lesson_id] += 1
            persona_counts[row.persona] += 1
            kind_counts[row.kind] += 1
            rows.append(row)

        # --- 2. Fill the remaining slots. ----------------------------------
        # Greedy: walk the kinds by largest deficit, and for each new row pick
        # the hungriest ELIGIBLE persona and the hungriest ELIGIBLE lesson.
        # Because eligibility is a hard mask, marginals land as evenly as the
        # mask permits rather than perfectly.
        remaining = TARGET_PER_MODE - sum(kind_counts.values())
        for _ in range(remaining):
            kind = min(
                sorted(kinds),
                key=lambda k: (kind_counts[k] - kind_target[k], k),
            )
            spec = registry[kind]
            persona = _pick_lowest(persona_counts, list(spec.eligible))
            lesson = _pick_lowest(lesson_counts, spec.subjects_lessons)

            n = kind_counts[kind] + 1
            sid = f"{kind}_{persona}_{lesson}_{n:02d}"
            max_turns = None
            if mode == 'multi_turn':
                budgets = spec.budgets
                max_turns = budgets[n % len(budgets)]

            rows.append(Row(
                scenario_id=sid, mode=mode, persona=persona, lesson_id=lesson,
                kind=kind, subject=SUBJECT_OF[lesson], status='author',
                max_turns=max_turns,
            ))
            kind_counts[kind] += 1
            persona_counts[persona] += 1
            lesson_counts[lesson] += 1

    return rows


def _subjects_lessons(self) -> list[int]:
    return [l for l in LESSONS if SUBJECT_OF[l] in self.subjects]


# Attached after definition so both dataclasses share it.
Archetype.subjects_lessons = property(_subjects_lessons)
Shape.subjects_lessons = property(_subjects_lessons)


def marginals(rows: list[Row]) -> dict:
    """Report the achieved marginals, for the balance test and the CLI."""
    out: dict = {}
    for mode in ('single_turn', 'multi_turn'):
        sub = [r for r in rows if r.mode == mode]
        out[mode] = {
            'total': len(sub),
            'persona': dict(Counter(r.persona for r in sub)),
            'lesson': dict(Counter(r.lesson_id for r in sub)),
            'kind': dict(Counter(r.kind for r in sub)),
            'subject': dict(Counter(r.subject for r in sub)),
            'status': dict(Counter(r.status for r in sub)),
        }
        if mode == 'multi_turn':
            out[mode]['max_turns'] = dict(Counter(r.max_turns for r in sub))
    return out


def main() -> None:
    rows = solve()
    PLAN_PATH.write_text(json.dumps([r.to_dict() for r in rows], indent=2) + '\n')

    m = marginals(rows)
    for mode in ('single_turn', 'multi_turn'):
        d = m[mode]
        print(f"\n=== {mode}: {d['total']} ===")
        print("  status  :", d['status'])
        print("  persona :", {k: d['persona'].get(k, 0) for k in PERSONAS})
        print("  subject :", d['subject'])
        lo, hi = min(d['lesson'].values()), max(d['lesson'].values())
        print(f"  lesson  : {len(d['lesson'])} lessons, min={lo} max={hi}")
        klo, khi = min(d['kind'].values()), max(d['kind'].values())
        print(f"  kind    : {len(d['kind'])} kinds, min={klo} max={khi}")
        if 'max_turns' in d:
            print("  turns   :", d['max_turns'])
    print(f"\nPlan written to {PLAN_PATH}")


if __name__ == '__main__':
    main()
