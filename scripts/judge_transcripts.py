"""Combined tutor-transcript judge — 10-principle + BEA-2025.

Single Opus 4.7 call per transcript that produces BOTH:

1. **Per-session 10-principle scoring** (0-5 per principle) plus structured
   prompt-edit / engine-flow / experience recommendations. This is the
   prompt-engineering deliverable inherited from PR #7's A/B harness — used
   to surface concrete edits like "Forbid referencing a diagram unless one
   is rendered".

2. **Per-tutor-turn BEA-2025 evaluation** (Maurya et al. 2025, NAACL).
   Four dimensions × three classes (``Yes`` / ``To some extent`` / ``No``)
   applied only to tutor turns whose preceding student turn contains a
   mistake or expresses confusion. This is the paper-aligned metric
   directly comparable to the BEA-2025 Shared Task framework.

The two are merged into a single LLM call (one round trip, cheaper and
more internally consistent than running two judges).

Output schema (one JSON object per transcript):

  {
    "scores": {                 # 10-principle (per-session)
      "<principle>": {"score": int, "evidence": str},
      ...
    },
    "strongest_behaviors": [str, str],
    "weakest_behaviors":  [str, str],
    "prompt_recommendations":     [Recommendation, ...],
    "flow_recommendations":       [Recommendation, ...],
    "experience_recommendations": [Recommendation, ...],
    "overall_summary": str,

    "bea_evaluations": [        # BEA (per tutor turn after a mistake)
      {
        "tutor_turn_id": str,
        "preceding_student_turn_id": str | null,
        "student_made_mistake": bool,
        "skip_reason": str | null,
        "mistake_description": str | null,
        "tutor_excerpt": str | null,
        "mistake_identification": "Yes" | "To some extent" | "No" | null,
        "mistake_location":       "Yes" | "To some extent" | "No" | null,
        "providing_guidance":     "Yes" | "To some extent" | "No" | null,
        "actionability":          "Yes" | "To some extent" | "No" | null,
        "rationale": str | null
      },
      ...
    ]
  }

Cost: ~$0.50-1 per transcript (one Opus call, ~3-5k tokens in / ~3-6k
tokens out). Same cost as the previous 10-principle-only judge.

Run with:

    AB_REPORT_DIR=<dir> python scripts/judge_transcripts.py

Idempotent: skips transcripts whose <stem>.json already exists in
``judge_scores/``. Delete the score file to force re-judge.

Refs:
  - BEA 2025 Shared Task: https://sig-edu.org/sharedtask/2025
  - Maurya et al. 2025 (dataset paper). "Unifying AI Tutor Evaluation:
    An Evaluation Taxonomy for Pedagogical Ability Assessment of
    LLM-Powered AI Tutors". NAACL 2025.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import List, Tuple

import django

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

# Load .env manually — Anthropic SDK reads ANTHROPIC_API_KEY from os.environ.
_env = Path(__file__).resolve().parents[1] / '.env'
if _env.exists():
    for _line in _env.read_text().splitlines():
        if '=' in _line and not _line.startswith('#'):
            _k, _v = _line.split('=', 1)
            os.environ.setdefault(_k.strip(), _v.strip())

django.setup()

from anthropic import Anthropic  # noqa: E402


_REPORT_DIR = Path(os.environ.get('AB_REPORT_DIR', 'ab-test-reports'))
TRANSCRIPTS_DIR = _REPORT_DIR / 'raw_transcripts'
SCORES_DIR = _REPORT_DIR / 'judge_scores'
ALL_SCORES = SCORES_DIR / '_all_scores.jsonl'

JUDGE_MODEL = 'claude-opus-4-7'
JUDGE_MAX_TOKENS = 16384
# Note: `temperature` is deprecated for Opus 4.7 and rejected with 400.
# The model is deterministic-ish by default; we omit the kwarg entirely.


# ─── 10-principle rubric ────────────────────────────────────────────────

PRINCIPLES: List[Tuple[str, str]] = [
    ('active_learning',
     'Active Learning — minimum effective dose of explanation; majority of session is student doing, not reading.'),
    ('direct_instruction_active_practice',
     'Direct Instruction + Active Practice — every teaching segment is immediately followed by a student action; never two consecutive instruction blocks.'),
    ('deliberate_practice',
     'Deliberate Practice — practice calibrated to student level; on errors, focused corrective feedback on the specific skill, then a similar but varied problem.'),
    ('mastery_learning',
     'Mastery Learning — progression gated on demonstrated mastery, not step count; failing students get diagnosis of bottleneck prereq, not answer reveal.'),
    ('cognitive_load',
     'Minimising Cognitive Load — one idea at a time; worked examples before practice on new concepts; explicit subgoals; inline (not deferred) media.'),
    ('layering',
     'Layering — practice authentically requires prerequisite skills; explanations explicitly link new to previously mastered concepts.'),
    ('non_interference',
     'Non-Interference — confusable topics not back-to-back; discriminating features made explicit when concepts can be confused.'),
    ('interleaving',
     'Interleaving / Mixed Practice — problem types vary enough that the student cannot mindlessly repeat one procedure.'),
    ('testing_effect',
     'The Testing Effect / Retrieval Practice — hints not too eager; student first attempts genuine retrieval; scaffolding stripped during review.'),
    ('targeted_remediation',
     'Targeted Remediation — repeated failure triggers remedial practice on prereq skills, not recycled unsolvable problems or answer reveals.'),
]


# ─── BEA constants (also used by aggregator + reporters) ────────────────

BEA_DIMENSIONS = (
    'mistake_identification',
    'mistake_location',
    'providing_guidance',
    'actionability',
)
BEA_YES = 'Yes'
BEA_SOMEWHAT = 'To some extent'
BEA_NO = 'No'
_BEA_VALID_LABELS = frozenset({BEA_YES, BEA_SOMEWHAT, BEA_NO})


# ─── Prompt assembly ────────────────────────────────────────────────────

def build_prompt(transcript: str, lesson_label: str, persona: str) -> str:
    principles_block = '\n'.join(f"- **{key}**: {desc}" for key, desc in PRINCIPLES)
    return f"""You are an expert evaluator of AI tutoring quality. You will produce TWO
complementary evaluations of the same tutoring transcript in a single response.
The two evaluations live alongside each other in one JSON object; do not skip
either part.

## Context
- Lesson: {lesson_label}
- Student persona: {persona}
- Curriculum: Seychelles National Curriculum (S3 = Form 3, ~age 13-14)

---

# Part A — Per-session 10-principle scoring + prompt-edit recommendations

(Unit of analysis: the WHOLE session.)

## The 10 principles

{principles_block}

## A.1 Score

For each of the 10 principles, assign an integer score 0-5:
- 0 = principle clearly violated (e.g. answer leaked, no practice, no retrieval)
- 1 = mostly absent
- 2 = weak / inconsistent
- 3 = adequate
- 4 = strong
- 5 = exemplary

For each principle, give a one-sentence justification quoting or citing
evidence from specific turns. Also identify the 1-2 strongest and 1-2
weakest tutor behaviors.

## A.2 Recommendations

Produce three lists of recommendations, each item evidence-anchored to
the transcript:

- `prompt_recommendations`: changes to the tutoring SYSTEM PROMPT
  (wording, rules, examples, ordering, forbidden patterns). PRIMARY list.
- `flow_recommendations`: changes to engine logic / orchestration /
  scaffolding flow that the system prompt alone cannot fix (judge cycle
  caps, retry policy, prerequisite routing, exit-ticket gating).
- `experience_recommendations`: changes to what the student sees / feels
  (pacing, encouragement frequency, media placement, error-message tone).

Each recommendation requires: `title`, `rationale`, `evidence_quote`
(≤240 chars verbatim), `evidence_turn`, `suggested_prompt_edit`
(prompt_recommendations only; empty string for the other two lists),
`expected_effect`, `severity` (high/medium/low).

Aim for 3-8 prompt recommendations. Fewer is fine if the transcript
doesn't surface that many. Do not invent issues; do not under-report.

---

# Part B — Per-turn BEA-2025 evaluation

(Unit of analysis: ONE TUTOR TURN at a time, BEA-2025 Shared Task
taxonomy, Maurya et al. 2025.)

For each TUTOR turn in the transcript:

1. Look at the IMMEDIATELY PRECEDING STUDENT turn.

2. If that student turn contains a clear mistake OR expresses confusion
   that calls for remediation, the tutor turn is IN SCOPE: evaluate it
   on all four BEA dimensions below.

3. If the student turn is correct, off-topic, or non-substantive filler
   ("ok", "yes", "got it", a single character that doesn't engage), the
   tutor turn is OUT OF SCOPE — set `student_made_mistake: false`, fill
   `skip_reason`, leave the four scoring fields null.

4. The very first tutor turn (no preceding student turn) is also out of
   scope.

## B.1 The four BEA dimensions

Each dimension is scored as one of: `Yes` / `To some extent` / `No`.

**Mistake Identification** — did the tutor recognize the mistake?
- `Yes`: clearly identified / recognized.
- `To some extent`: suggests there may be a mistake but sounds uncertain.
- `No`: does not recognize (e.g., simply provides the answer or moves on).

**Mistake Location** — does the tutor accurately point to WHERE the
mistake is and WHAT it is?
- `Yes`: clearly points to the exact location of a genuine mistake.
- `To some extent`: some awareness but vague, unclear, or easy to
  misunderstand.
- `No`: no details about mistake location.

**Providing Guidance** — does the tutor offer correct and relevant
guidance (hint, explanation, supporting question)?
- `Yes`: guidance is correct AND relevant to the mistake.
- `To some extent`: guidance provided but partially incorrect,
  incomplete, or misleading.
- `No`: no guidance, or guidance is irrelevant or factually wrong.

**Actionability** — is it clear what the student should do next?
- `Yes`: clear suggestions on the next action.
- `To some extent`: indicates something needs to be done but unclear
  what exactly.
- `No`: no suggested action (e.g., simply reveals the final answer).

---

# Output format

Return ONLY a single JSON object, no preamble, no markdown fences,
no commentary around it. Schema:

{{
  "scores": {{
    "active_learning": {{"score": int, "evidence": str}},
    "direct_instruction_active_practice": {{"score": int, "evidence": str}},
    "deliberate_practice": {{"score": int, "evidence": str}},
    "mastery_learning": {{"score": int, "evidence": str}},
    "cognitive_load": {{"score": int, "evidence": str}},
    "layering": {{"score": int, "evidence": str}},
    "non_interference": {{"score": int, "evidence": str}},
    "interleaving": {{"score": int, "evidence": str}},
    "testing_effect": {{"score": int, "evidence": str}},
    "targeted_remediation": {{"score": int, "evidence": str}}
  }},
  "strongest_behaviors": [str, str],
  "weakest_behaviors":  [str, str],
  "prompt_recommendations":     [Recommendation, ...],
  "flow_recommendations":       [Recommendation, ...],
  "experience_recommendations": [Recommendation, ...],
  "overall_summary": str,

  "bea_evaluations": [
    {{
      "tutor_turn_id": str,
      "preceding_student_turn_id": str | null,
      "student_made_mistake": bool,
      "skip_reason": str | null,
      "mistake_description": str | null,
      "tutor_excerpt": str | null,
      "mistake_identification": "Yes" | "To some extent" | "No" | null,
      "mistake_location":       "Yes" | "To some extent" | "No" | null,
      "providing_guidance":     "Yes" | "To some extent" | "No" | null,
      "actionability":          "Yes" | "To some extent" | "No" | null,
      "rationale": str | null
    }},
    ...
  ]
}}

where Recommendation = {{
  "title": str, "rationale": str, "evidence_quote": str,
  "evidence_turn": str, "suggested_prompt_edit": str,
  "expected_effect": str, "severity": "high"|"medium"|"low"
}}

## Transcript

{transcript}
"""


# ─── Transcript metadata parsing ────────────────────────────────────────

_TURN_HEADER_RE = re.compile(r'^---\s+(TUTOR|STUDENT)\s+\(id=(\d+)', re.MULTILINE)
_TRANSCRIPT_HEADER_RE = re.compile(r'^# Transcript .*?lesson=(\d+)\s+persona=(\w+)', re.MULTILINE)


def _parse_transcript_meta(text: str) -> Tuple[str, str]:
    m = _TRANSCRIPT_HEADER_RE.search(text)
    if m:
        return m.group(1), m.group(2)
    return '?', '?'


def _count_tutor_turns(text: str) -> int:
    return sum(1 for m in _TURN_HEADER_RE.finditer(text) if m.group(1) == 'TUTOR')


# ─── JSON parsing (forgiving) ───────────────────────────────────────────

def parse_json_loose(raw: str) -> dict:
    raw = raw.strip()
    if raw.startswith('```'):
        raw = re.sub(r'^```(?:json)?\s*\n', '', raw)
        raw = re.sub(r'\n```\s*$', '', raw)
    start = raw.find('{')
    if start < 0:
        raise ValueError(f"No JSON object in response (first 200 chars): {raw[:200]!r}")
    depth = 0
    for i, ch in enumerate(raw[start:], start):
        if ch == '{':
            depth += 1
        elif ch == '}':
            depth -= 1
            if depth == 0:
                return json.loads(raw[start:i + 1])
    raise ValueError("Unbalanced JSON braces in response")


# ─── Aggregates ─────────────────────────────────────────────────────────

def _aggregate_bea(evaluations: List[dict]) -> dict:
    in_scope = [e for e in evaluations if e.get('student_made_mistake')]
    out: dict = {
        'n_total_tutor_turns': len(evaluations),
        'n_in_scope': len(in_scope),
        'coverage_rate': (len(in_scope) / len(evaluations)) if evaluations else 0.0,
    }
    if not in_scope:
        for dim in BEA_DIMENSIONS:
            out[f'{dim}_exact'] = None
            out[f'{dim}_lenient'] = None
        out['overall_exact_pass_rate'] = None
        out['overall_lenient_pass_rate'] = None
        out['strict_pass_rate'] = None
        out['lenient_pass_rate'] = None
        return out

    n = len(in_scope)
    for dim in BEA_DIMENSIONS:
        labels = [e.get(dim) for e in in_scope]
        out[f'{dim}_exact'] = sum(1 for l in labels if l == BEA_YES) / n
        out[f'{dim}_lenient'] = sum(1 for l in labels if l in (BEA_YES, BEA_SOMEWHAT)) / n

    out['overall_exact_pass_rate'] = sum(out[f'{d}_exact'] for d in BEA_DIMENSIONS) / 4
    out['overall_lenient_pass_rate'] = sum(out[f'{d}_lenient'] for d in BEA_DIMENSIONS) / 4
    out['strict_pass_rate'] = sum(
        1 for e in in_scope if all(e.get(d) == BEA_YES for d in BEA_DIMENSIONS)
    ) / n
    out['lenient_pass_rate'] = sum(
        1 for e in in_scope if all(e.get(d) in (BEA_YES, BEA_SOMEWHAT) for d in BEA_DIMENSIONS)
    ) / n
    return out


# ─── Per-cell judging ───────────────────────────────────────────────────

def score_transcript(client: Anthropic, transcript_path: Path) -> dict:
    text = transcript_path.read_text()
    lesson_label, persona = _parse_transcript_meta(text)

    t0 = time.time()
    response = client.messages.create(
        model=JUDGE_MODEL,
        max_tokens=JUDGE_MAX_TOKENS,
        messages=[{'role': 'user', 'content': build_prompt(text, lesson_label, persona)}],
    )
    wall = time.time() - t0
    raw = response.content[0].text if response.content else ''

    base = {
        'transcript': transcript_path.name,
        'lesson_label': lesson_label,
        'persona': persona,
        'judge_model': JUDGE_MODEL,
        'wall_seconds': wall,
        'tokens_in': response.usage.input_tokens,
        'tokens_out': response.usage.output_tokens,
        'n_tutor_turns_in_transcript': _count_tutor_turns(text),
    }

    try:
        parsed = parse_json_loose(raw)
    except Exception as exc:
        return {**base,
                'parse_error': f'{type(exc).__name__}: {exc}',
                'raw_response_head': raw[:500]}

    # Validate BEA labels; null out anything off-vocabulary.
    bea_evaluations = parsed.get('bea_evaluations') or []
    bad_labels: List[str] = []
    for e in bea_evaluations:
        for dim in BEA_DIMENSIONS:
            v = e.get(dim)
            if v is None or v in _BEA_VALID_LABELS:
                continue
            bad_labels.append(f"{e.get('tutor_turn_id')}/{dim}={v!r}")
            e[dim] = None

    out = {**base, **parsed}
    out['bea_aggregates'] = _aggregate_bea(bea_evaluations)
    if bad_labels:
        out['bea_bad_labels'] = bad_labels
    return out


# ─── Main ────────────────────────────────────────────────────────────────

def main():
    SCORES_DIR.mkdir(parents=True, exist_ok=True)
    transcripts = sorted(TRANSCRIPTS_DIR.glob('*.md'))
    if not transcripts:
        print(f"[judge] no transcripts in {TRANSCRIPTS_DIR}", file=sys.stderr)
        sys.exit(1)

    client = Anthropic()
    print(f"[judge] {len(transcripts)} transcripts in {TRANSCRIPTS_DIR}", file=sys.stderr)

    all_records: List[dict] = []
    for tp in transcripts:
        per_cell = SCORES_DIR / (tp.stem + '.json')
        if per_cell.exists():
            print(f"  [skip] {tp.name} — already scored", file=sys.stderr)
            all_records.append(json.loads(per_cell.read_text()))
            continue

        print(f"  [judge] {tp.name}", file=sys.stderr)
        result = score_transcript(client, tp)
        per_cell.write_text(json.dumps(result, indent=2))
        all_records.append(result)

        if 'parse_error' in result:
            print(f"    ↳ PARSE ERROR: {result['parse_error']}", file=sys.stderr)
            continue

        scores = result.get('scores') or {}
        # Some LLM outputs return a string in place of the per-principle
        # dict — guard with isinstance() so the loop doesn't crash mid-run.
        valid_scores = [
            s['score'] for s in scores.values()
            if isinstance(s, dict) and isinstance(s.get('score'), (int, float))
        ]
        mean_score = (sum(valid_scores) / len(valid_scores)) if valid_scores else None
        agg = result.get('bea_aggregates') or {}
        bea_n = agg.get('n_in_scope', 0)
        bits = []
        if mean_score is not None:
            bits.append(f"10p={mean_score:.2f}/5")
        if bea_n:
            bits.append(
                f"bea n={bea_n} strict={agg.get('strict_pass_rate'):.0%} "
                f"lenient={agg.get('lenient_pass_rate'):.0%}"
            )
        else:
            bits.append("bea n=0")
        bits.append(f"{result['wall_seconds']:.1f}s")
        bits.append(f"{result['tokens_in']}/{result['tokens_out']} tok")
        print(f"    ↳ {'  '.join(bits)}", file=sys.stderr)

    with ALL_SCORES.open('w') as f:
        for rec in all_records:
            f.write(json.dumps(rec) + '\n')

    print(f"\n[judge] wrote {len(all_records)} per-cell + {ALL_SCORES}", file=sys.stderr)


if __name__ == '__main__':
    main()
