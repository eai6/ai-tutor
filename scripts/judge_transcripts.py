"""LLM-as-judge scorer + recommender for A/B test transcripts.

Reads each transcript from ab-test-reports/raw_transcripts/, sends it to
Claude Opus 4.7 (temp=0). The judge serves two roles in a single pass:

  1. Score the transcript 0-5 against the 10 science-of-learning
     principles (rubric from .claude/skills/evaluate-tutor/SKILL.md).
  2. Produce structured, evidence-anchored recommendations to improve
     the tutoring system prompt — plus secondary recommendations on
     engine flow and student experience.

The recommendations are the **primary** artefact. The scores are
inputs to the synthesis, not the headline.

Outputs:

  ab-test-reports/judge_scores/<cell_key>.json     (per-cell scores + recs)
  ab-test-reports/judge_scores/_all_scores.jsonl   (aggregated)

Run with:  venv/bin/python scripts/judge_transcripts.py
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

import django
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

# Load .env manually since Anthropic SDK reads ANTHROPIC_API_KEY from environ.
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
RUBRIC_PATH = _REPORT_DIR / 'judge_rubric.md'

JUDGE_MODEL = 'claude-opus-4-7'
JUDGE_TEMP = 0.0
JUDGE_MAX_TOKENS = 8192

PRINCIPLES = [
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


def build_prompt(transcript: str, lesson_label: str, persona: str) -> str:
    """Return the user-turn prompt for the judge."""
    principles_block = '\n'.join(f"- **{key}**: {desc}" for key, desc in PRINCIPLES)
    return f"""You are an expert evaluator of AI tutoring quality, grounded in the science of learning.

Your job has two parts. **Both are required.**

1. **Score** ONE tutoring session transcript against the 10 distilled principles below.
2. **Prescribe** concrete, evidence-anchored recommendations to improve the tutoring
   **system prompt** (primary), plus secondary recommendations on engine flow and
   student experience.

The recommendations are the primary deliverable — they feed directly into the next
revision of the tutoring system prompt. Scores are inputs to that synthesis, not the
end goal. Do not skip or thin out the recommendations.

## Context
- Lesson: {lesson_label}
- Student persona: {persona}
- Curriculum: Seychelles National Curriculum (S3 = Form 3, ~age 13-14)

## The 10 principles

{principles_block}

## Your task

### Part A — Score

For each of the 10 principles, assign an integer score 0-5:
- 0 = principle clearly violated (e.g. answer leaked, no practice, no retrieval)
- 1 = mostly absent
- 2 = weak / inconsistent
- 3 = adequate
- 4 = strong
- 5 = exemplary

For each principle, give a one-sentence justification quoting or citing evidence
from specific turns. Also identify the 1-2 strongest and 1-2 weakest tutor behaviors.

### Part B — Recommendations (primary deliverable)

Produce three lists of recommendations, each item evidence-anchored to the transcript:

- `prompt_recommendations`: changes to the tutoring **system prompt** itself
  (wording, rules, examples, ordering, forbidden patterns, etc.). This is the
  most important list.
- `flow_recommendations`: changes to engine logic / orchestration / scaffolding flow
  that the system prompt alone cannot fix (e.g. judge cycle caps, retry policy,
  prerequisite routing, exit-ticket gating).
- `experience_recommendations`: changes to what the student sees / feels (pacing,
  encouragement frequency, media placement, error-message tone).

Each recommendation must include:
- `title`: short imperative (e.g. "Forbid two consecutive teach blocks without a student turn")
- `rationale`: WHY — which principle it serves, which failure pattern it fixes
- `evidence_quote`: a verbatim excerpt from the transcript (≤ 240 chars)
- `evidence_turn`: which turn / section the quote came from (e.g. "TUTOR turn id=42")
- `suggested_prompt_edit` (prompt_recommendations only — use empty string elsewhere):
  the actual language to add, remove, or replace in the system prompt
- `expected_effect`: what specific, observable change in tutor behavior this should produce
- `severity`: "high" if this is fixing a frequent or load-bearing failure;
  "medium" if it's an improvement on top of acceptable behavior; "low" otherwise

Aim for 3–8 prompt recommendations per transcript. Fewer is fine if the transcript
genuinely doesn't surface that many. Do not invent issues to pad the list — but do
not under-report either; small qualitative wins matter when aggregated across cells.

## Output format

Return ONLY a single JSON object, no prose around it, no markdown fences. Schema:

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
  "weakest_behaviors": [str, str],
  "prompt_recommendations": [
    {{
      "title": str,
      "rationale": str,
      "evidence_quote": str,
      "evidence_turn": str,
      "suggested_prompt_edit": str,
      "expected_effect": str,
      "severity": "high"|"medium"|"low"
    }}
  ],
  "flow_recommendations": [
    {{
      "title": str,
      "rationale": str,
      "evidence_quote": str,
      "evidence_turn": str,
      "suggested_prompt_edit": "",
      "expected_effect": str,
      "severity": "high"|"medium"|"low"
    }}
  ],
  "experience_recommendations": [
    {{
      "title": str,
      "rationale": str,
      "evidence_quote": str,
      "evidence_turn": str,
      "suggested_prompt_edit": "",
      "expected_effect": str,
      "severity": "high"|"medium"|"low"
    }}
  ],
  "overall_summary": str
}}

## Transcript

{transcript}
"""


def parse_json_loose(raw: str) -> dict:
    """Strip code fences and parse JSON; raise if invalid.

    Also strips trailing commas before } or ] which Opus sometimes emits.
    """
    s = raw.strip()
    if s.startswith('```'):
        s = re.sub(r'^```[a-zA-Z]*\n?', '', s)
        s = re.sub(r'\n?```$', '', s)
    start = s.find('{')
    if start >= 0:
        s = s[start:]
    end = s.rfind('}')
    if end >= 0:
        s = s[:end + 1]
    # Strip trailing commas (json strict, Opus sometimes lenient)
    s = re.sub(r',(\s*[}\]])', r'\1', s)
    return json.loads(s)


def score_transcript(client: Anthropic, transcript_path: Path) -> dict:
    text = transcript_path.read_text()
    # Pull lesson + persona from header line
    header = text.splitlines()[0]
    m = re.search(r'lesson=(\S+)\s+persona=(\S+)', header)
    lesson_label = m.group(1) if m else '?'
    persona = m.group(2) if m else '?'
    prompt = build_prompt(text, lesson_label, persona)

    t0 = time.monotonic()
    resp = client.messages.create(
        model=JUDGE_MODEL,
        max_tokens=JUDGE_MAX_TOKENS,
        messages=[{'role': 'user', 'content': prompt}],
    )
    elapsed = time.monotonic() - t0
    raw = ''.join(b.text for b in resp.content if hasattr(b, 'text'))
    try:
        parsed = parse_json_loose(raw)
    except Exception as exc:
        return {
            'transcript': transcript_path.name,
            'error': f'parse: {exc}',
            'raw_output': raw[:2000],
            'wall_seconds': elapsed,
            'tokens_in': resp.usage.input_tokens,
            'tokens_out': resp.usage.output_tokens,
        }
    return {
        'transcript': transcript_path.name,
        'lesson_label': lesson_label,
        'persona': persona,
        'scores': parsed.get('scores', {}),
        'strongest_behaviors': parsed.get('strongest_behaviors', []),
        'weakest_behaviors': parsed.get('weakest_behaviors', []),
        'prompt_recommendations': parsed.get('prompt_recommendations', []),
        'flow_recommendations': parsed.get('flow_recommendations', []),
        'experience_recommendations': parsed.get('experience_recommendations', []),
        'overall_summary': parsed.get('overall_summary', ''),
        'wall_seconds': elapsed,
        'tokens_in': resp.usage.input_tokens,
        'tokens_out': resp.usage.output_tokens,
    }


def main():
    SCORES_DIR.mkdir(parents=True, exist_ok=True)
    # Save the rubric for reproducibility
    RUBRIC_PATH.write_text(
        '# LLM-as-judge rubric used for A/B runs\n\n'
        '> **Judge role**: score transcripts against the 10 science-of-learning\n'
        '> principles **and** produce evidence-anchored recommendations to improve\n'
        '> the tutoring system prompt, engine flow, and student experience. The\n'
        '> recommendations are the primary deliverable — scores are inputs to the\n'
        '> synthesis. See `design/AB_TESTING_PLAN.md`.\n\n'
        f'Judge model: `{JUDGE_MODEL}`, temperature={JUDGE_TEMP}, max_tokens={JUDGE_MAX_TOKENS}\n\n'
        '## 10 Science-of-Learning principles\n\n'
        + '\n'.join(f'- **{k}**: {d}' for k, d in PRINCIPLES) + '\n\n'
        '## Scoring scale\n\n0=violated · 1=mostly absent · 2=weak · 3=adequate · 4=strong · 5=exemplary\n\n'
        '## Recommendation buckets\n\n'
        '- `prompt_recommendations` — edits to the tutoring system prompt (primary).\n'
        '- `flow_recommendations` — engine / orchestration changes.\n'
        '- `experience_recommendations` — student-facing UX changes.\n\n'
        'Each recommendation must cite an `evidence_quote` and `evidence_turn`,\n'
        'state its `rationale`, `expected_effect`, and a `severity` of high/medium/low.\n'
        'Prompt recommendations additionally include a `suggested_prompt_edit`.\n\n'
        '## Judge prompt template\n\n```\n' + build_prompt('<TRANSCRIPT>', '<LESSON>', '<PERSONA>') + '\n```\n'
    )

    api_key = os.getenv('ANTHROPIC_API_KEY')
    if not api_key:
        sys.exit('ANTHROPIC_API_KEY missing')
    client = Anthropic(api_key=api_key)

    transcripts = sorted(TRANSCRIPTS_DIR.glob('*.md'))
    # If --retry-failed-only or default behavior: keep transcripts whose
    # existing score file has 'error' (or no score file).
    only_failed = '--retry-failed' in sys.argv
    if only_failed:
        keep = []
        for tp in transcripts:
            existing = SCORES_DIR / (tp.stem + '.json')
            if not existing.exists():
                keep.append(tp); continue
            try:
                obj = json.loads(existing.read_text())
                if 'error' in obj:
                    keep.append(tp)
            except Exception:
                keep.append(tp)
        transcripts = keep
        print(f'(retry-only) Re-scoring {len(transcripts)} transcript(s)')
    else:
        if ALL_SCORES.exists():
            ALL_SCORES.unlink()

    print(f'Scoring {len(transcripts)} transcript(s) with {JUDGE_MODEL}...')

    for i, tp in enumerate(transcripts, 1):
        print(f'[{i}/{len(transcripts)}] {tp.name}')
        score = score_transcript(client, tp)
        per_cell = SCORES_DIR / (tp.stem + '.json')
        per_cell.write_text(json.dumps(score, indent=2))
        if not only_failed:
            with ALL_SCORES.open('a') as f:
                f.write(json.dumps(score) + '\n')
        if 'error' in score:
            print(f'  ↳ ERROR: {score["error"]}')
        else:
            mean = sum(s['score'] for s in score['scores'].values()) / max(1, len(score['scores']))
            print(f'  ↳ mean={mean:.2f} tok_out={score["tokens_out"]} {score["wall_seconds"]:.1f}s')


if __name__ == '__main__':
    main()
