"""Post-hoc meta-judge for the DeepMind experiment.

Re-rates every tutor turn from the saved Opus + Haiku experiment sessions
on three axes (1-5 scale) using two cross-vendor judges (Gemini 2.5 Pro
+ GPT-4o). Aggregates per (model x lesson x persona) and writes a markdown
report to memory/deepmind_meta_judge_results.md.

Run via:
    python manage.py shell <scripts/run_meta_judge.py
"""

import json
import os
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import django
django.setup() if not django.apps.apps.ready else None  # safe re-entry

from apps.tutoring.models import TutorSession, SessionTurn
from apps.llm.models import ModelConfig
from apps.llm.client import get_llm_client


# ---- inputs ----------------------------------------------------------------

EXPERIMENT_JSONL = Path('memory/.deepmind_model_experiment_results.jsonl')
# Set via env var when running the full sweep so we don't clobber the
# Opus + Haiku focused output:
#   META_JUDGE_TAG=all36 python manage.py shell <scripts/run_meta_judge.py
_TAG = os.environ.get('META_JUDGE_TAG', '').strip()
_SUFFIX = f"_{_TAG}" if _TAG else ""
OUTPUT_MD = Path(f'memory/deepmind_meta_judge_results{_SUFFIX}.md')
OUTPUT_JSONL = Path(f'memory/.deepmind_meta_judge_scores{_SUFFIX}.jsonl')

# Filter: opus+haiku only by default; set META_JUDGE_FILTER="" to disable.
_FILTER = os.environ.get('META_JUDGE_FILTER', 'opus,haiku').strip()
_FILTER_LABELS = [s.strip().lower() for s in _FILTER.split(',') if s.strip()] or None

META_JUDGES = [
    ('google', 'gemini-2.5-flash'),  # cross-vendor; 2.5-pro was 503'ing
    ('openai', 'gpt-4o'),
]

# ---- prompt ----------------------------------------------------------------

RUBRIC_PROMPT = """You are evaluating a single tutor turn from an AI-driven tutoring session. Use the BEA 2025 Shared Task on AI Tutor Evaluation rubric: 4 dimensions, each rated "Yes" / "To some extent" / "No".

Take into account the whole conversation context and whether the tutor is successfully addressing a genuine error or confusion from the student. Assess whether the response is "something you would expect from a good tutor".

LESSON CONTEXT:
{lesson_context}

PRIOR TUTOR TURN (or "[NONE - session start]"):
{prior_tutor}

PRIOR STUDENT INPUT (the message the tutor is responding to, or "[NONE]"):
{student_input_before}

THIS TUTOR TURN (what we are evaluating):
{tutor_turn}

STUDENT'S NEXT INPUT (what the student said after, success signal, or "[NONE - session ended]"):
{student_input_after}

First decide whether the prior student input contains a mistake or confusion that the tutor turn is responding to. Set `is_mistake_response` to true only if the prior student input shows an error, misconception, partial answer, or confusion the tutor is addressing. Set it to false for warm-ups, introductions, question-asking turns, transitions, or responses to fully-correct student answers.

If `is_mistake_response` is false, set all 4 dimension scores to "N/A".

If `is_mistake_response` is true, rate each dimension:

1. **Mistake_Identification** — Does the tutor recognize that the student made a mistake?
   - "Yes": clearly identifies / recognizes the error
   - "To some extent": suggests there may be a mistake but uncertain
   - "No": does not recognize the mistake

2. **Mistake_Location** — Does the tutor point to where the mistake is?
   - "Yes": clearly points to the exact location of the error
   - "To some extent": demonstrates awareness of where the error is but is vague
   - "No": does not provide any details related to where the mistake is

3. **Providing_Guidance** — Does the tutor offer correct and useful guidance?
   - "Yes": correct and relevant guidance (hint, explanation, example, question)
   - "To some extent": guidance is fully or partially incorrect, incomplete, or somewhat misleading
   - "No": does not include any guidance, or guidance is irrelevant

4. **Actionability** — Is it clear what the student should do next?
   - "Yes": clear suggestions on what the student should do next
   - "To some extent": indicates that something needs to be done but unclear what
   - "No": does not suggest any action

Respond with ONLY a JSON object, no preamble:
{{"is_mistake_response": true|false, "Mistake_Identification": "Yes|To some extent|No|N/A", "Mistake_Location": "Yes|To some extent|No|N/A", "Providing_Guidance": "Yes|To some extent|No|N/A", "Actionability": "Yes|To some extent|No|N/A", "reason": "one short sentence"}}
"""


# ---- helpers ---------------------------------------------------------------

def load_experiment_cells(filter_labels=None):
    """Return all cells from the experiment JSONL. If filter_labels is
    given (list of substrings, lowercased), keep only matching cells."""
    cells = []
    for line in EXPERIMENT_JSONL.read_text().splitlines():
        if not line.strip():
            continue
        d = json.loads(line)
        label = d.get('model_label', '')
        if filter_labels and not any(s in label.lower() for s in filter_labels):
            continue
        if d.get('session_id') is None:
            continue
        cells.append({
            'label': label,
            'provider': d.get('provider', 'anthropic'),
            'model_name': d.get('model_name', ''),
            'lesson_id': d['lesson_id'],
            'persona': d['persona'],
            'session_id': d['session_id'],
        })
    return cells


def get_lesson_context(lesson):
    parts = [f"Lesson: {lesson.title}"]
    if getattr(lesson, 'objective', None):
        parts.append(f"Lesson objective: {lesson.objective[:500]}")
    unit = getattr(lesson, 'unit', None)
    if unit:
        parts.append(f"Unit: {unit.title}")
        course = getattr(unit, 'course', None)
        if course:
            subj = getattr(course, 'subject_type', '') or getattr(course, 'subject_code', '')
            parts.append(f"Course: {course.title} (subject: {subj})")
            if getattr(course, 'grade_level', None):
                parts.append(f"Grade level: {course.grade_level}")
    return "\n".join(parts)


def build_turn_rows(session):
    """Yield (prior_tutor, student_input_before, tutor_turn, student_input_after)
    tuples for each tutor turn in chronological order."""
    turns = list(session.turns.order_by('created_at'))
    prior_tutor = None
    student_input_before = None
    for i, t in enumerate(turns):
        if t.role != 'tutor':
            continue
        # find next student turn
        next_student = None
        for nxt in turns[i+1:]:
            if nxt.role == 'student':
                next_student = nxt.content
                break
        yield {
            'turn_id': t.id,
            'prior_tutor': prior_tutor,
            'student_input_before': student_input_before,
            'tutor_turn': t.content,
            'student_input_after': next_student,
        }
        prior_tutor = t.content
    # update student_input_before for *next* tutor turn
    # actually: redo loop tracking properly
    # (above version is fine for context but let's enrich)


def build_turn_rows_v2(session):
    """Correct version: tracks the immediately-prior student input."""
    turns = list(session.turns.order_by('created_at'))
    rows = []
    last_student = None
    last_tutor = None
    for i, t in enumerate(turns):
        if t.role == 'student':
            last_student = t.content
            continue
        if t.role == 'tutor':
            next_student = None
            for nxt in turns[i+1:]:
                if nxt.role == 'student':
                    next_student = nxt.content
                    break
            rows.append({
                'turn_id': t.id,
                'prior_tutor': last_tutor,
                'student_input_before': last_student,
                'tutor_turn': t.content,
                'student_input_after': next_student,
            })
            last_tutor = t.content
            last_student = None  # consumed
    return rows


def call_meta_judge(provider, model_name, prompt, retries=2):
    """Returns dict with scores or {'error': ...}. Retries on transient errors."""
    cfg = ModelConfig.resolve_runtime(provider, model_name)
    if cfg is None:
        return {'error': f'no config for {provider}/{model_name}'}
    cfg.purpose = ModelConfig.Purpose.JUDGE  # forces temp=0
    client = get_llm_client(cfg)
    last_err = None
    for attempt in range(retries + 1):
        try:
            resp = client.generate(
                messages=[{'role': 'user', 'content': prompt}],
                system_prompt="You are an expert education evaluator. Score precisely. Return only JSON. Keep the 'reason' field to one short sentence (<25 words).",
                max_tokens=1500,
                temperature=0,
            )
            break
        except Exception as e:
            last_err = e
            msg = str(e)
            if '503' in msg or '429' in msg or 'UNAVAILABLE' in msg:
                time.sleep(2 ** attempt)  # 1, 2, 4
                continue
            return {'error': f'{type(e).__name__}: {e}'}
    else:
        return {'error': f'{type(last_err).__name__}: {last_err}'}

    text = (resp.content or '').strip()
    # strip code fences
    if text.startswith('```'):
        text = text.split('```', 2)[1].lstrip('json').strip()
        if text.endswith('```'):
            text = text[:-3].strip()
    # find first { and last }
    s = text.find('{')
    e = text.rfind('}')
    if s == -1 or e == -1:
        return {'error': 'no_json', 'raw': text[:200]}
    try:
        return json.loads(text[s:e+1])
    except Exception as ex:
        return {'error': f'json_parse: {ex}', 'raw': text[:200]}


def score_one_turn(cell, row):
    lesson = TutorSession.objects.get(id=cell['session_id']).lesson
    prompt = RUBRIC_PROMPT.format(
        lesson_context=get_lesson_context(lesson),
        prior_tutor=(row['prior_tutor'] or '[NONE - session start]')[:1500],
        student_input_before=(row['student_input_before'] or '[NONE]')[:800],
        tutor_turn=row['tutor_turn'][:2000],
        student_input_after=(row['student_input_after'] or '[NONE - session ended]')[:600],
    )

    judges = {}
    for provider, model_name in META_JUDGES:
        result = call_meta_judge(provider, model_name, prompt)
        judges[f"{provider}/{model_name}"] = result

    return {
        'label': cell['label'],
        'lesson_id': cell['lesson_id'],
        'persona': cell['persona'],
        'session_id': cell['session_id'],
        'turn_id': row['turn_id'],
        'judges': judges,
    }


# ---- main ------------------------------------------------------------------

def main():
    cells = load_experiment_cells(filter_labels=_FILTER_LABELS)
    print(f"[Meta-judge] {len(cells)} cells (filter={_FILTER_LABELS or 'ALL'})")
    print(f"[Meta-judge] tag={_TAG or '(none)'} → {OUTPUT_MD}")

    all_rows = []
    for c in cells:
        try:
            s = TutorSession.objects.get(id=c['session_id'])
        except TutorSession.DoesNotExist:
            print(f"  ✗ session {c['session_id']} missing — skip")
            continue
        rows = build_turn_rows_v2(s)
        print(f"  · {c['label']:25} L{c['lesson_id']} {c['persona']:9} → {len(rows)} tutor turns")
        for r in rows:
            all_rows.append((c, r))

    total = len(all_rows)
    print(f"[Meta-judge] {total} tutor turns × {len(META_JUDGES)} judges = {total * len(META_JUDGES)} calls")

    # parallel — 6 workers
    scored = []
    OUTPUT_JSONL.write_text('')  # truncate
    fp = OUTPUT_JSONL.open('a')
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(score_one_turn, c, r): (c, r) for c, r in all_rows}
        for i, fut in enumerate(as_completed(futures), 1):
            try:
                result = fut.result()
            except Exception as e:
                c, r = futures[fut]
                result = {'label': c['label'], 'session_id': c['session_id'],
                          'turn_id': r['turn_id'], 'error': str(e)}
            scored.append(result)
            fp.write(json.dumps(result) + '\n')
            fp.flush()
            if i % 10 == 0 or i == total:
                elapsed = time.time() - t0
                print(f"  ... {i}/{total} ({elapsed:.0f}s, {elapsed/i:.1f}s/turn)")
    fp.close()
    print(f"[Meta-judge] done in {time.time()-t0:.0f}s → {OUTPUT_JSONL}")

    # ---- aggregate ---------------------------------------------------------
    write_report(scored)


BEA_DIMS = ['Mistake_Identification', 'Mistake_Location',
            'Providing_Guidance', 'Actionability']

ORDINAL_MAP = {'Yes': 1.0, 'To some extent': 0.5, 'No': 0.0}


def ordinal_to_num(val):
    """Map 'Yes'/'To some extent'/'No' to 1.0 / 0.5 / 0.0. Return None for N/A."""
    if not isinstance(val, str):
        return None
    v = val.strip()
    return ORDINAL_MAP.get(v)  # None for "N/A" or unknown


def write_report(scored):
    """Aggregate per (model × lesson × persona) and per model overall."""
    from collections import defaultdict, Counter

    # Per-cell: per dimension (combined) numeric list + per-judge numeric lists + raw label counts
    def fresh_cell():
        d = {f"{dim}_combined": [] for dim in BEA_DIMS}
        for dim in BEA_DIMS:
            d[f"{dim}_g"] = []
            d[f"{dim}_o"] = []
            d[f"{dim}_labels"] = Counter()  # raw "Yes"/"To some extent"/"No" counts (combined)
        d['mistake_turns'] = 0
        d['non_mistake_turns'] = 0
        return d

    agg_cell = defaultdict(fresh_cell)

    for row in scored:
        if 'error' in row:
            continue
        key = (row['label'], row['lesson_id'], row['persona'])
        gem = row['judges'].get('google/gemini-2.5-flash', {}) or {}
        oai = row['judges'].get('openai/gpt-4o', {}) or {}

        # Decide if turn is a mistake-response (vote = either judge says true)
        gem_mr = gem.get('is_mistake_response') is True
        oai_mr = oai.get('is_mistake_response') is True
        is_mr = gem_mr or oai_mr  # liberal: count as mistake if either flags it

        if not is_mr:
            agg_cell[key]['non_mistake_turns'] += 1
            continue
        agg_cell[key]['mistake_turns'] += 1

        for dim in BEA_DIMS:
            g_val = ordinal_to_num(gem.get(dim))
            o_val = ordinal_to_num(oai.get(dim))
            if g_val is not None:
                agg_cell[key][f"{dim}_g"].append(g_val)
            if o_val is not None:
                agg_cell[key][f"{dim}_o"].append(o_val)
            both = [x for x in (g_val, o_val) if x is not None]
            if both:
                agg_cell[key][f"{dim}_combined"].append(sum(both) / len(both))
            # raw labels for distribution
            for src in (gem.get(dim), oai.get(dim)):
                if isinstance(src, str) and src.strip() in ORDINAL_MAP:
                    agg_cell[key][f"{dim}_labels"][src.strip()] += 1

    def avg(xs):
        return sum(xs) / len(xs) if xs else float('nan')

    # Per-model aggregate
    agg_model = defaultdict(fresh_cell)
    for (label, _, _), v in agg_cell.items():
        agg_model[label]['mistake_turns'] += v['mistake_turns']
        agg_model[label]['non_mistake_turns'] += v['non_mistake_turns']
        for dim in BEA_DIMS:
            agg_model[label][f"{dim}_combined"].extend(v[f"{dim}_combined"])
            agg_model[label][f"{dim}_g"].extend(v[f"{dim}_g"])
            agg_model[label][f"{dim}_o"].extend(v[f"{dim}_o"])
            for lab, cnt in v[f"{dim}_labels"].items():
                agg_model[label][f"{dim}_labels"][lab] += cnt

    # Inter-judge agreement (exact label match across BEA dims, mistake-response turns)
    matches = 0
    compares = 0
    for row in scored:
        if 'error' in row:
            continue
        gem = row['judges'].get('google/gemini-2.5-flash', {}) or {}
        oai = row['judges'].get('openai/gpt-4o', {}) or {}
        for dim in BEA_DIMS:
            g_v = gem.get(dim)
            o_v = oai.get(dim)
            if isinstance(g_v, str) and isinstance(o_v, str) and g_v in ORDINAL_MAP and o_v in ORDINAL_MAP:
                compares += 1
                if g_v == o_v:
                    matches += 1
    agreement = matches / compares if compares else float('nan')

    lines = []
    scope = (f"the {len(set((r['session_id'] for r in scored if 'session_id' in r)))} "
             f"experiment sessions covered ({len(set((r['label'] for r in scored if 'label' in r)))} models)")
    lines.append(f"# Post-hoc meta-judge results — BEA 2025 rubric ({_TAG or 'opus+haiku'})")
    lines.append("")
    lines.append(f"Cross-vendor evaluation of every saved tutor turn across {scope}, "
                 "using the BEA 2025 Shared Task on AI Tutor Evaluation rubric "
                 "(https://sig-edu.org/sharedtask/2025).")
    lines.append("")
    lines.append("Two non-Anthropic meta-judges rate each tutor turn on 4 "
                 "dimensions, 3-point ordinal scale (\"Yes\" / \"To some extent\" / \"No\"):")
    lines.append("")
    lines.append("- **Mistake_Identification** — does the tutor recognize the student made a mistake?")
    lines.append("- **Mistake_Location** — does the tutor point to where the mistake is?")
    lines.append("- **Providing_Guidance** — does the tutor offer correct, useful guidance?")
    lines.append("- **Actionability** — is it clear what the student should do next?")
    lines.append("")
    lines.append("Meta-judges: **Gemini 2.5 Flash** (Google) and **GPT-4o** (OpenAI). "
                 "Chosen non-Anthropic to avoid same-vendor bias on Anthropic-tutored "
                 "sessions. Both temperature=0.")
    lines.append("")
    lines.append("Scoring convention: \"Yes\"=1.0, \"To some extent\"=0.5, \"No\"=0.0. "
                 "Combined per-turn score is mean of the two judges; per-cell score "
                 "is mean across mistake-response turns only (non-mistake turns "
                 "excluded — e.g. warm-ups, introductions, transitions).")
    lines.append("")
    lines.append(f"**Inter-judge agreement** (exact-label match, mistake-response turns): "
                 f"{agreement:.1%} across {compares} comparisons.")
    lines.append("")
    lines.append("## Per-model averages (across all sessions)")
    lines.append("")
    lines.append("| model | mistake turns | non-mistake | "
                 "Mistake_ID | Mistake_Loc | Guidance | Actionability | BEA mean |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|")
    for label in sorted(agg_model.keys()):
        v = agg_model[label]
        mt, nm = v['mistake_turns'], v['non_mistake_turns']
        dim_avgs = [avg(v[f"{dim}_combined"]) for dim in BEA_DIMS]
        bea_mean = sum(d for d in dim_avgs if d == d) / len([d for d in dim_avgs if d == d]) if any(d == d for d in dim_avgs) else float('nan')
        lines.append(f"| {label} | {mt} | {nm} | "
                     + " | ".join(f"{d:.2f}" for d in dim_avgs)
                     + f" | **{bea_mean:.2f}** |")
    lines.append("")
    lines.append("## Per (model × lesson × persona) cell")
    lines.append("")
    lines.append("| model | lesson | persona | mistake turns | "
                 "Mistake_ID | Mistake_Loc | Guidance | Actionability |")
    lines.append("|---|---:|---|---:|---:|---:|---:|---:|")
    for key in sorted(agg_cell.keys()):
        label, lesson, persona = key
        v = agg_cell[key]
        dim_avgs = [avg(v[f"{dim}_combined"]) for dim in BEA_DIMS]
        lines.append(f"| {label} | {lesson} | {persona} | {v['mistake_turns']} | "
                     + " | ".join(f"{d:.2f}" for d in dim_avgs)
                     + " |")
    lines.append("")
    lines.append("## Label distribution per model (raw judge votes)")
    lines.append("")
    lines.append("Combined across both meta-judges. Each mistake-response turn "
                 "contributes 2 votes per dimension (one per judge).")
    lines.append("")
    lines.append("| model | dim | Yes | To some extent | No |")
    lines.append("|---|---|---:|---:|---:|")
    for label in sorted(agg_model.keys()):
        v = agg_model[label]
        for dim in BEA_DIMS:
            c = v[f"{dim}_labels"]
            tot = sum(c.values()) or 1
            lines.append(f"| {label} | {dim} | "
                         f"{c.get('Yes',0)} ({c.get('Yes',0)/tot:.0%}) | "
                         f"{c.get('To some extent',0)} ({c.get('To some extent',0)/tot:.0%}) | "
                         f"{c.get('No',0)} ({c.get('No',0)/tot:.0%}) |")
    lines.append("")
    lines.append("## Per-judge breakdown (sanity check)")
    lines.append("")
    lines.append("| model | judge | Mistake_ID | Mistake_Loc | Guidance | Actionability |")
    lines.append("|---|---|---:|---:|---:|---:|")
    for label in sorted(agg_model.keys()):
        v = agg_model[label]
        gem = [avg(v[f"{dim}_g"]) for dim in BEA_DIMS]
        oai = [avg(v[f"{dim}_o"]) for dim in BEA_DIMS]
        lines.append(f"| {label} | Gemini 2.5 Flash | "
                     + " | ".join(f"{d:.2f}" for d in gem) + " |")
        lines.append(f"| {label} | GPT-4o | "
                     + " | ".join(f"{d:.2f}" for d in oai) + " |")
    lines.append("")
    lines.append("## Methodology notes")
    lines.append("")
    lines.append("- Rubric source: BEA 2025 Shared Task on AI Tutor Evaluation, "
                 "https://sig-edu.org/sharedtask/2025.")
    lines.append("- Judges first decide `is_mistake_response`: true iff the prior "
                 "student input contains a mistake / confusion the tutor is "
                 "addressing. Warm-ups, introductions, transitions, and responses "
                 "to fully-correct student answers are excluded from BEA scoring.")
    lines.append("- A turn is counted as mistake-response if EITHER judge flags it "
                 "as such (liberal inclusion to maximize signal).")
    lines.append("- Per-turn combined score = mean of the two judges; per-cell "
                 "score = mean across mistake-response turns; BEA mean = mean of "
                 "the 4 per-cell dimension scores.")
    lines.append("- Errors / parse failures are silently dropped from per-axis "
                 "averages. The 'mistake turns' count reflects judged turns; "
                 "errored turns are excluded entirely.")
    lines.append("- Same lesson context provided to both judges: lesson title, "
                 "objective, unit, course, grade level; prior tutor turn; "
                 "preceding student input; turn under evaluation; next student input.")
    lines.append("")
    lines.append(f"Raw per-turn data: `{OUTPUT_JSONL}` "
                 f"({sum(1 for _ in OUTPUT_JSONL.open())} rows).")

    OUTPUT_MD.write_text("\n".join(lines))
    print(f"[Meta-judge] report → {OUTPUT_MD}")


if __name__ == '__main__' or True:
    main()
