"""Build a single self-contained offline_eval/viewer_deploy/index.html.

Embeds every multi-turn session (transcript + judge rubric) inline and renders a
browse table, a session detail view, a cross-cycle compare (same seed-5 scenario
across baseline -> cycle1 -> cycle2), and a manual grading workflow: score a
session yourself against the eight pedagogical dimensions, then compare your
verdict with the judge's. No external resources — open the file directly in a
browser. Re-run any time (picks up new cycles).

    ./venv/bin/python offline_eval/build_viewer.py

The eight dimensions are NOT redefined here. They are imported from
``ai_tutor.apps.benchmark.pedagogy`` — the same module that drives the form,
the database and the scorer behind /dashboard/benchmark/sessions/ — so a
verdict recorded on this page answers the same questions, in the same order,
under the same all-or-nothing pass rule. ``pedagogy`` is pure standard library,
so this script still needs no venv. ``tests/test_viewer_grading.py`` fails the
build if the two ever drift.
"""
import glob
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS = os.path.join(ROOT, "offline_eval", "multi_turn_results")
SRC = os.path.join(RESULTS, "fixcheck")
OUT = os.path.join(ROOT, "offline_eval", "viewer_deploy", "index.html")

sys.path.insert(0, ROOT)
from ai_tutor.apps.benchmark import pedagogy as P  # noqa: E402

RUNS = [
    ("_prefix_colab", "00_prefix_colab"),
    ("_n5_sample5", "01_n5_sample5"),
    ("_stiff_rule", "01b_stiff_gemini"),
    ("_errored_overload", "02_errored_overload_INVALID"),
    ("_cycle0_baseline", "10_cycle0_baseline"),
    ("_cycle1", "11_cycle1_retry+budget"),
    ("_cycle2", "12_cycle2_persona_budgets"),
    ("_cycle2_overloaded", "12b_cycle2_overloaded"),
    ("_cycle3_3model", "13_cycle3_antidesync"),
    ("_cycle4", "14_cycle4_intentgate"),
    ("_cycle5", "15_cycle5_prompt_tuning"),
    # Cycle 6 was written to the top of the results tree rather than a
    # directory of its own, so it needs the empty subpath. Without this entry
    # its sixty sessions are on disk and off the page.
    ("", "16_cycle6"),
    ("cycle7_bottleneck_fixes", "17_cycle7_bottleneck_fixes"),
    ("cycle8_dispatch_grading", "18_cycle8_dispatch_grading"),
    ("cycle8b_salvage", "18b_cycle8b_salvage"),
    ("cycle9_letter_coherence", "19_cycle9_letter_coherence"),
    ("cycle10_qwen_prompt", "20_cycle10_qwen_prompt"),
    ("cycle11_qwen_content_budgets", "21_cycle11_qwen_content_budgets"),
]

# Result sets that sit OUTSIDE the fixcheck cycle arc, directly under
# multi_turn_results/. These are flat: the directory itself is one run and each
# JSON in it is one model arm, with no cycle subdirectories. The cycle walk
# above never reaches them, which is why they are declared separately rather
# than discovered.
FLAT_RUNS = [
    ("mt100", "30_mt100_18arm_board"),
    # Hand-annotated run: qwen3.6 vs qwen3.8 on the production engine, over the
    # 34 `hg1` scenarios. Generated with EVAL_SKIP_RUBRIC=1, so these sessions
    # carry NO rubric_result — the Grade tab is the instrument, and the
    # Agreement tab has no judge side to compare against. `passed` on these rows
    # means deterministic assertions only; check `rubric_skipped`.
    ("hg1_prod", "40_hg1_prod_qwen36_vs_qwen38"),
]


def dimensions_payload():
    """The eight dimensions, flattened for the page.

    ``values`` comes from ``pedagogy.choices_for``, which appends the
    "Not applicable" option exactly where the taxonomy allows it — so the page
    cannot offer n/a on a dimension the dashboard treats as always-applicable,
    or omit it where the dashboard offers it.
    """
    return [{
        "key": d.key,
        "label": d.label,
        "definition": d.definition,
        "guidance": d.session_guidance,
        "values": [list(v) for v in P.choices_for(d.key)],
        "desideratum": d.desideratum,
    } for d in P.DIMENSIONS]


# The pass rule, ported to JavaScript. A second implementation of a rule is a
# drift risk, and this one decides every verdict the page reports — so it is
# kept as one named constant, shipped verbatim, and pinned against
# pedagogy.session_passes over ~2000 value combinations by
# tests/test_viewer_grading.py rather than trusted.
PASS_RULE_JS = r"""
const NA = 'n/a';
// True / false / null, where null means "excluded from scoring". Null is
// returned for n/a AND for an unanswered dimension: neither is a failure.
function dimensionPasses(key, value){
  if(!value || value === NA) return null;
  const d = DIMS.find(x => x.key === key);
  if(!d) return null;
  return value === d.desideratum;
}
// A session passes iff EVERY applicable dimension sits at its desideratum.
// A session with nothing scorable returns false rather than a vacuous true:
// nothing was assessed, so nothing was demonstrated.
function sessionPasses(values){
  const verdicts = DIMS.map(d => dimensionPasses(d.key, values[d.key] || ''));
  const scorable = verdicts.filter(v => v !== null);
  return scorable.length > 0 && scorable.every(v => v === true);
}
// Every dimension answered. An unanswered dimension is not a "No", so an
// incomplete grading is not scored at all.
function gradeComplete(values){
  return DIMS.every(d => !!(values[d.key] || ''));
}
"""


# Summary statistics for the comparison tab. Kept beside the pass rule, and
# pinned by tests/test_viewer_grading.py for the same reason: each of these has
# an "empty" case where returning 0 would assert something that was never
# measured.
STATS_JS = r"""
// How much of this session sat at its desideratum, over what was SCORABLE.
// Dividing by eight instead would count dimensions that never arose against
// the tutor — the same mistake as treating n/a as a failure. Null, not 0,
// when nothing was scorable: 0% claims a judgement nobody made.
function sessionScore(values){
  let at = 0, applicable = 0;
  DIMS.forEach(d => {
    const o = dimensionPasses(d.key, values[d.key] || '');
    if(o === null) return;
    applicable++;
    if(o === true) at++;
  });
  return applicable
    ? {at: at, applicable: applicable, pct: 100 * at / applicable}
    : {at: 0, applicable: 0, pct: null};
}
// One row per persona in the dataset, INCLUDING personas with nothing graded —
// those are precisely the ones worth grading next, and a chart that omits them
// cannot be used to level the counts. Deficit is measured against the
// most-graded persona, so "every bar level" and "every deficit zero" are the
// same statement.
function personaBalance(rows, personas){
  const n = {};
  personas.forEach(p => {n[p] = 0;});
  rows.forEach(r => {if(n[r.p] !== undefined) n[r.p]++;});
  const target = personas.reduce((m, p) => Math.max(m, n[p]), 0);
  return personas.map(p => ({persona: p, n: n[p],
                            deficit: target - n[p], target: target}));
}
// How one rubric item should print. A judge item that did not apply is stored
// with score 0.0 and left OUT of the rubric mean, so showing that 0.00 would
// put apparent zeroes beside a high mean and read as a failure the tutor was
// never assessed on. A genuine 0.00 on an applicable item still prints 0.00.
function rubricDisplay(it){
  const na = !it.ap || it.sc == null;
  const v = na ? null : it.sc;
  return {na: na, value: v, text: na ? 'n/a' : v.toFixed(2),
          width: na ? 0 : Math.max(0, Math.min(1, v)) * 100};
}
// Share of sessions each side passed, over the same set. Null on an empty set.
function passRates(rows){
  const n = rows.length;
  if(!n) return {n: 0, mine: null, judge: null};
  return {n: n,
          mine: 100 * rows.filter(x => x.h).length / n,
          judge: 100 * rows.filter(x => x.j).length / n};
}
// Models you have graded something in, each with its count, so a dropdown
// built from this answers "how many have I done here" before it is toggled.
// A model with nothing graded is left out — the opposite of personaBalance
// one function up, and deliberately so. Personas are a worklist to level out,
// where a zero is the most useful row on the chart; models are scopes to
// compare, where an empty scope yields nothing but dashes.
function modelCounts(sessions){
  const n = {};
  sessions.forEach(d => {n[d.m] = (n[d.m] || 0) + 1;});
  return Object.keys(n).sort().map(m => ({model: m, n: n[m]}));
}
"""


def collect(src=None, runs=None, flat_runs=None):
    src = SRC if src is None else src
    runs = RUNS if runs is None else runs
    flat_runs = FLAT_RUNS if flat_runs is None else flat_runs
    out = []
    for sub, label in runs:
        out += _read_run(os.path.join(src, sub), label)
    for sub, label in flat_runs:
        out += _read_run(os.path.join(RESULTS, sub), label)
    return out


def _read_run(path, label):
    """Every session in one directory of result JSONs, tagged with one run label."""
    out = []
    for jf in sorted(glob.glob(os.path.join(path, "*.json"))):
        model = os.path.basename(jf)[:-5]
        try:
            data = json.load(open(jf))
        except Exception:
            continue
        for r in data.get("results", []):
            rr = r.get("rubric_result") or {}
            out.append({
                # Stable identity for a saved grade. Run label + model +
                # scenario is unique (scenario ids do not repeat within a
                # result file), and none of the three changes on a rebuild
                # — so grades survive re-running this script. Re-running a
                # scenario under a NEW cycle directory is a new key, which
                # is correct: it is a different session.
                "k": f"{label}|{model}|{r.get('scenario_id')}",
                "run": label, "m": model, "s": r.get("scenario_id"),
                "p": r.get("persona"), "sub": r.get("subject"),
                "l": r.get("lesson_id"), "o": r.get("sim_reason"),
                "pass": bool(r.get("passed")), "t": r.get("sim_turns"),
                "rm": rr.get("mean_score"), "th": rr.get("pass_threshold"),
                # The judge's OWN verdict on tutoring quality, kept apart
                # from `pass` above. `pass` also requires the session to
                # have reached the exit ticket, which is a mechanical
                # completion gate, not a judgement — comparing a human's
                # pedagogical verdict against it would conflate the two.
                # None where nothing was judged, so it drops out of the
                # agreement stats instead of counting as a fail.
                "rp": (None if rr.get("passed") is None
                       else bool(rr.get("passed"))),
                "e": bool(r.get("error")),
                "err": str(r.get("error") or "")[:400],
                "tr": [{"role": t.get("role"), "ph": t.get("phase"),
                        "c": str(t.get("content") or "")}
                       for t in (r.get("transcript") or [])],
                "ru": [{"it": it.get("item"), "sc": it.get("score"),
                        "ap": bool(it.get("applicable")),
                        "rz": str(it.get("reasoning") or "")}
                       for it in (rr.get("items") or [])],
            })
    return out


TEMPLATE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Multi-turn tutoring — experiment data</title>
<style>
:root{--bg:#fff;--fg:#1a1a1a;--mut:#666;--line:#e2e2e2;--card:#f7f7f8;--accent:#1f3a5f;
 --tutor:#e8f0fe;--stud:#eef7ee;--hi:#fff6e0;--pass:#1a7f37;--fail:#b42318;}
@media(prefers-color-scheme:dark){:root{--bg:#16181c;--fg:#e6e6e6;--mut:#9aa0a6;--line:#2c2f36;
 --card:#1e2126;--accent:#8fb8e6;--tutor:#1b2b45;--stud:#16301f;--hi:#3a3320;--pass:#4ac26b;--fail:#f0776c;}}
*{box-sizing:border-box}body{margin:0;font:14px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;
 background:var(--bg);color:var(--fg)}
header{padding:10px 16px;border-bottom:1px solid var(--line);display:flex;gap:16px;align-items:center;
 position:sticky;top:0;background:var(--bg);z-index:5;flex-wrap:wrap}
h1{font-size:16px;margin:0;color:var(--accent)}
.tab{padding:5px 12px;border:1px solid var(--line);border-radius:6px;cursor:pointer;background:var(--card)}
.tab.on{background:var(--accent);color:#fff;border-color:var(--accent)}
.wrap{padding:12px 16px;max-width:100%}
.filters{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:10px}
select,input{padding:5px 8px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg);font:inherit}
input[type=search]{min-width:240px}
.stat{color:var(--mut);font-size:13px}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{text-align:left;padding:6px 9px;border-bottom:1px solid var(--line);white-space:nowrap}
th{position:sticky;top:57px;background:var(--bg);cursor:pointer;user-select:none}
tbody tr{cursor:pointer}tbody tr:hover{background:var(--card)}
.badge{padding:1px 7px;border-radius:20px;font-size:12px;font-weight:600}
.b-pass{background:var(--stud);color:var(--pass)}.b-fail{background:var(--hi);color:var(--fail)}
.o-exit_ticket{color:var(--pass)}.o-max_turns{color:#b8860b}.o-deadlock,.o-error{color:var(--fail)}
.overlay{position:fixed;inset:0;background:rgba(0,0,0,.5);display:none;z-index:20}
.panel{position:fixed;top:0;right:0;height:100%;width:min(820px,96vw);background:var(--bg);
 border-left:1px solid var(--line);overflow:auto;padding:16px;z-index:21}
.close{float:right;cursor:pointer;font-size:20px;color:var(--mut)}
.meta{color:var(--mut);font-size:13px;margin:2px 0 12px}
.sec{font-weight:700;margin:16px 0 8px;color:var(--accent)}
.rub{width:100%;font-size:12.5px}
/* Rubric cells hold prose. The global `td{white-space:nowrap}` above is for the
   browse table; left to apply here it forces each item onto one line and pushes
   the judge's reasoning off the right edge of the page. */
.rub td{vertical-align:top;padding:5px 6px;white-space:normal}
.rub td:first-child{white-space:nowrap}
.bar{height:8px;border-radius:4px;background:var(--line);width:60px;display:inline-block;vertical-align:middle;overflow:hidden}
.bar>i{display:block;height:100%}
.msg{margin:8px 0;padding:8px 11px;border-radius:8px;max-width:88%}
.msg.tutor{background:var(--tutor)}.msg.student{background:var(--stud);margin-left:auto}
.who{font-size:11px;color:var(--mut);margin-bottom:2px}
.cmp{display:flex;gap:12px;overflow-x:auto}
.col{flex:1 0 380px;border:1px solid var(--line);border-radius:8px;padding:10px;background:var(--card)}
.small{font-size:12px;color:var(--mut)} pre{white-space:pre-wrap;margin:0;font:inherit}
.about{max-width:940px}.about p{margin:6px 0}.about li{margin:3px 0}
.about h2{color:var(--accent);font-size:15px;margin:22px 0 6px;border-bottom:1px solid var(--line);padding-bottom:4px}
.about td,.about th{white-space:normal;vertical-align:top}.about th{position:static}
.about td:first-child{white-space:nowrap;font-weight:600}
.about code{background:var(--card);padding:1px 5px;border-radius:4px;font-size:12.5px}

/* ── manual grading ─────────────────────────────────────────────── */
.hdr-r{margin-left:auto;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.btn{padding:6px 13px;border:1px solid var(--line);border-radius:7px;background:var(--card);
 color:var(--fg);font:inherit;font-size:13px;font-weight:600;cursor:pointer}
.btn.pri{background:var(--accent);color:#fff;border-color:var(--accent)}
.btn:disabled{opacity:.4;cursor:not-allowed}
.dirty{color:#b8860b;font-size:12.5px;font-weight:600}
.rule{background:var(--card);border:1px solid var(--line);border-left:3px solid var(--accent);
 border-radius:8px;padding:10px 13px;margin-bottom:12px;font-size:13px;line-height:1.55;max-width:1000px}
.gr{display:grid;grid-template-columns:minmax(0,1.05fr) minmax(0,1fr);gap:14px;align-items:start}
@media(max-width:1100px){.gr{grid-template-columns:1fr}}
.gr-l{position:sticky;top:64px}
@media(max-width:1100px){.gr-l{position:static}}
.tsc{max-height:calc(100vh - 240px);min-height:240px;overflow:auto;border:1px solid var(--line);
 border-radius:8px;padding:8px;background:var(--card)}
.dim{border:1px solid var(--line);border-radius:8px;padding:10px 12px;margin-bottom:9px}
.dim.done{border-color:var(--accent)}
.dim .nm{font-weight:600;margin-bottom:2px}
.dim .df{font-size:12.5px;margin-bottom:4px}
.dim .gd{font-size:12.5px;color:var(--mut);border-left:2px solid var(--line);padding-left:8px;
 margin-bottom:7px;line-height:1.5}
.dim label{display:flex;gap:7px;align-items:flex-start;font-size:13px;padding:2px 0;cursor:pointer}
.des{font-size:10.5px;font-weight:700;color:var(--pass);background:var(--stud);border-radius:20px;
 padding:0 6px;letter-spacing:.03em;white-space:nowrap}
.blind{border:1px dashed var(--line);border-radius:8px;padding:14px;text-align:center;
 color:var(--mut);font-size:13px;background:var(--card)}
textarea{width:100%;padding:7px 9px;border:1px solid var(--line);border-radius:6px;
 background:var(--bg);color:var(--fg);font:inherit;min-height:60px;resize:vertical}
.sxs{display:grid;grid-template-columns:1fr 1fr;gap:14px;align-items:start}
@media(max-width:900px){.sxs{grid-template-columns:1fr}}
/* Grid items default to min-width:auto, so a wide table refuses to shrink and
   overflows the page instead of wrapping inside its column. */
.sxs>.col{min-width:0;flex:none}
.mtx{border-collapse:collapse;width:auto}
.mtx td,.mtx th{border:1px solid var(--line);padding:7px 13px;text-align:center;white-space:nowrap}
.mtx th{position:static;background:var(--card)}
.mtx .hit{background:var(--stud);font-weight:700}.mtx .miss{background:var(--hi);font-weight:700}
.k-agree{color:var(--pass);font-weight:700}.k-dis{color:var(--fail);font-weight:700}
.pill{display:inline-block;padding:1px 8px;border-radius:20px;font-size:11.5px;font-weight:600;
 background:var(--card);border:1px solid var(--line)}
/* Sessions-graded-per-persona. One measure across six nominal categories, so
   every bar is the same hue — shading by value would burn the only free
   channel re-encoding the bar length. Identity comes from the row label. */
.bars{display:grid;grid-template-columns:auto minmax(120px,1fr) auto;
 gap:5px 10px;align-items:center;max-width:620px}
.bars .lb{font-size:12.5px;color:var(--mut);white-space:nowrap;text-align:right}
.bars .tr{position:relative;height:14px;background:var(--card);border-radius:0 4px 4px 0}
.bars .fl{height:100%;background:var(--accent);border-radius:0 4px 4px 0;min-width:0}
/* Hairline, solid, recessive — the level every bar is aiming at. */
.bars .tg{position:absolute;top:-3px;bottom:-3px;width:1px;background:var(--mut);opacity:.55}
.bars .vl{font-size:12.5px;font-variant-numeric:tabular-nums;white-space:nowrap}
.bars .df{color:var(--mut)}
.chart-h{font-weight:700;color:var(--accent);margin:16px 0 2px}
.chart-s{font-size:12.5px;color:var(--mut);margin-bottom:9px}
.cards{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:14px}
.kpi{border:1px solid var(--line);border-radius:8px;padding:9px 14px;background:var(--card);min-width:130px}
.kpi b{display:block;font-size:21px;line-height:1.25;color:var(--accent)}
.kpi span{font-size:12px;color:var(--mut)}
</style></head><body>
<header>
 <h1>Multi-turn tutoring — experiment data</h1>
 <span class="tab on" data-v="about" onclick="setView('about')">About &amp; how to use</span>
 <span class="tab" data-v="browse" onclick="setView('browse')">Browse</span>
 <span class="tab" data-v="compare" onclick="setView('compare')">Cross-cycle compare</span>
 <span class="tab" data-v="grade" onclick="setView('grade')">Grade</span>
 <span class="tab" data-v="sxs" onclick="setView('sxs')">Me vs judge</span>
 <span class="tab" data-v="agreement" onclick="setView('agreement')">Agreement</span>
 <span class="stat" id="count"></span>
 <span class="hdr-r">
  <span class="dirty" id="dirty"></span>
  <button class="btn" onclick="exportGrades()" id="btn-exp">Export grades</button>
  <button class="btn" onclick="document.getElementById('imp').click()">Import</button>
  <input type="file" id="imp" accept="application/json" style="display:none" onchange="importGrades(this)">
 </span>
</header>

<div class="wrap about" id="about">
 <h2>What this is</h2>
 <p>A browser for the raw data behind our <b>multi-turn tutoring evaluation</b>. Each row is one complete simulated tutoring <b>session</b>: a model under test (the <b>tutor</b>) teaches a simulated <b>student</b> through a whole lesson, and a separate <b>judge</b> scores the finished transcript against a rubric. This page lets you read every transcript and every judge score without opening the raw JSON.</p>
 <p class="small">Generated from the result JSONs by <code>offline_eval/build_viewer.py</code> — re-run it to refresh. It's a static file; nothing here calls out to the network.</p>

 <h2>How a session is scored</h2>
 <ul>
  <li><b>Tutor</b> — the model being evaluated. This is the only thing that changes between rows of the same scenario within a run. The cycle runs test five (glm-4.7, gemini-2.5-flash, deepseek-v3.1, kimi-k2-thinking, qwen3-next-80b-instruct); the mt100 board tests eighteen, including local Qwen arms on a Jetson.</li>
  <li><b>Student</b> — a simulated learner (Anthropic Haiku) role-playing one of six <b>personas</b>.</li>
  <li><b>Judge</b> — Anthropic Sonnet at temperature 0, scoring the finished transcript on ~11 rubric items, each 0.00–1.00.</li>
  <li>A session shows <span class="badge b-pass">PASS</span> only when it BOTH reaches the exit ticket AND the rubric <b>mean ≥ threshold</b> (0.6). Reaching the exit ticket alone is not enough.</li>
 </ul>

 <h2>The tabs</h2>
 <table><tbody>
  <tr><td>About</td><td>this page.</td></tr>
  <tr><td>Browse</td><td>the full session table. Filter with the dropdowns, sort by clicking a column header, and click any row to open its full transcript + judge scores in a panel on the right.</td></tr>
  <tr><td>Cross-cycle compare</td><td>pick one scenario + one model and see that SAME session side-by-side across every run that ran it — the fastest way to check whether a fix actually changed a specific session's behaviour.</td></tr>
  <tr><td>Grade</td><td>judge a session <b>yourself</b> against the eight pedagogical dimensions, one session at a time. See below.</td></tr>
  <tr><td>Me vs judge</td><td>your pass rate beside the judge's over the sessions you have graded, filterable to one <b>model</b>; the per-persona balance chart; and, for any single session, your eight dimensions beside the judge's rubric.</td></tr>
  <tr><td>Agreement</td><td>the same question over everything you have graded — confusion matrix, agreement rate, &kappa;, and which dimension drives your fails.</td></tr>
 </tbody></table>

 <h2>Grading a session yourself</h2>
 <p>The <b>Grade</b> tab asks the eight questions from Maurya et al. (NAACL 2025) — the same eight, in the same order, with the same wording and the same pass rule as the teacher dashboard at <code>/dashboard/benchmark/sessions/</code>. They are imported from that code at build time, not retyped here, and a test fails the build if the two ever drift.</p>
 <ul>
  <li><b>The pass rule is all-or-nothing.</b> A session passes only if every applicable dimension sits at its desideratum. One dimension at &ldquo;To some extent&rdquo; fails it. That is deliberately demanding — the interesting number is the per-dimension rate, which the Agreement tab reports.</li>
  <li><b>Not applicable</b> means the dimension genuinely never arose. It is excluded from scoring, not counted as a failure. Leaving a dimension blank is different: an incomplete grading is not scored at all.</li>
  <li><b>The judge's grade is hidden until you finish.</b> Read it first and the agreement figure measures how much it anchored you, not whether you independently agree. You can reveal it anyway; that session is then marked <i>peeked</i> and excluded from the headline figure.</li>
  <li><b>Your grades live in this browser</b> (localStorage), keyed by run + model + scenario, so they survive a rebuild of this page. They do <b>not</b> survive clearing your browser data, and they are not shared with anyone. The header counts sessions you have not yet exported.</li>
 </ul>

 <h2>Where to put an export</h2>
 <p><b>Export grades</b> downloads a JSON file — an ordinary browser download, to wherever your browser saves things (usually <code>~/Downloads</code>), named <code>manual_grades_&lt;count&gt;.json</code>. Nothing is uploaded; this page has no backend.</p>
 <p>That file is the only durable copy of your work. Move it into the repository, renaming it as you go — the exported name carries only a count, so successive exports collide and do not sort:</p>
 <p><code>mv ~/Downloads/manual_grades_37.json offline_eval/manual_grades/mt100_2026-08-17_daniel.json</code></p>
 <p><code>offline_eval/manual_grades/README.md</code> documents the file format, the value each dimension can hold, and the one trap in reading a file by hand: <code>verdicts</code> can contain part-finished gradings, which are excluded from scoring and from the <code>graded</code> count. Use <b>Import</b> to load a file back — your own or a colleague's. It merges per session, keeping whichever copy was edited more recently, so re-importing your own export changes nothing and importing someone else's adds only what you have not graded yourself.</p>

 <h2>What the comparison can and cannot tell you</h2>
 <p>Your verdict and the judge's are produced by <b>two different instruments</b>. You answer eight fixed pedagogical dimensions; the judge scores a handful of scenario-specific rubric items from 0.00 to 1.00 and passes the session on the mean. There is no dimension-by-dimension correspondence between them, so the comparison is made where the two are commensurable — at the <b>pass/fail verdict</b>. Per-dimension &kappa;, of the kind the dashboard computes between two annotators answering the same questions, is not available here and is not shown.</p>
 <p>By default the judge's side of the comparison is its <b>rubric verdict</b>, not the session's overall PASS. The overall result also requires the session to have reached the exit ticket, which is a mechanical completion gate rather than a judgement of teaching — comparing your pedagogical verdict against it would conflate the two. The Agreement tab lets you switch to the overall verdict if that is the question you want.</p>

 <h2>The filter dropdowns (Browse tab)</h2>
 <table><tbody>
  <tr><td>run</td><td>which round of the experiment (see the run list below).</td></tr>
  <tr><td>model</td><td>the tutor model under test.</td></tr>
  <tr><td>persona</td><td>the simulated student's behaviour (see personas below).</td></tr>
  <tr><td>subject</td><td>math or geography.</td></tr>
  <tr><td>outcome</td><td>how the session ended (see outcome codes below).</td></tr>
  <tr><td>pass</td><td>filter to PASS or FAIL only.</td></tr>
  <tr><td>search box</td><td>free text — matches the scenario id, the transcript content, AND the judge's reasoning. e.g. type <code>contradict</code> to find every session the judge flagged for self-contradiction.</td></tr>
 </tbody></table>
 <p class="small">The counter beside the tabs shows how many sessions match the current filter and their pass-rate. Click a column header to sort; click again to reverse.</p>

 <h2>Student personas</h2>
 <table><tbody>
  <tr><td>struggler</td><td>weak; many wrong answers, needs heavy scaffolding.</td></tr>
  <tr><td>average</td><td>middling; some slips.</td></tr>
  <tr><td>capable</td><td>strong; usually correct.</td></tr>
  <tr><td>probe_resistant</td><td>resists "explain your reasoning" prompts.</td></tr>
  <tr><td>non_responder</td><td>disengages — minimal, off-topic, or refusing answers.</td></tr>
  <tr><td>error_prone</td><td>careless arithmetic / procedural mistakes.</td></tr>
 </tbody></table>

 <h2>Outcome codes</h2>
 <table><tbody>
  <tr><td class="o-exit_ticket">exit_ticket</td><td>reached the graded quiz — the lesson completed. This is the success outcome.</td></tr>
  <tr><td class="o-max_turns">max_turns</td><td>ran out of the turn budget before finishing — a timeout.</td></tr>
  <tr><td class="o-deadlock">deadlock</td><td>tutor and student got stuck in a loop; the engine stopped it.</td></tr>
  <tr><td class="o-error">error</td><td>an infrastructure failure (an API error mid-session), NOT the model's fault — treat as invalid.</td></tr>
 </tbody></table>

 <h2>The runs (chronological)</h2>
 <p>The experiment iterates: implement a fix → run the same 20 scenarios → analyze → fix again. Each "run" is one of those rounds. The later runs (10 / 11 / 12) share the same 20 seed-5 scenarios, which is what makes the compare tab meaningful.</p>
 <table><tbody>
  <tr><td>00_prefix_colab</td><td>earliest baseline, before the anti-repetition &amp; judge fixes. n=5.</td></tr>
  <tr><td>01_n5_sample5</td><td>n=5 smoke test of the first fixes (anti-repetition, end-reason judge note, softened Gemini rule).</td></tr>
  <tr><td>01b_stiff_gemini</td><td>Gemini-only variant testing a stricter (later softened) prompt rule.</td></tr>
  <tr><td>02_errored_overload_INVALID</td><td>an n=20 attempt wiped by an Anthropic API outage. INVALID — kept only as evidence.</td></tr>
  <tr><td>10_cycle0_baseline</td><td>first proper n=20 baseline (seed-5 scenarios).</td></tr>
  <tr><td>11_cycle1_retry+budget</td><td>added transient-error retry (fixes 429/503 deadlocks) + a first turn-budget raise.</td></tr>
  <tr><td>12_cycle2_persona_budgets</td><td>added persona-aware turn budgets.</td></tr>
  <tr><td>12b_cycle2_overloaded</td><td>the first cycle-2 attempt, wiped by an API overload and re-run. Read with the same caution as 02.</td></tr>
  <tr><td>13_cycle3_antidesync</td><td>anti-desync guard: the tutor may not pose a new question over one the student has not answered. Gemini's orphaned questions fell 161 &rarr; 23.</td></tr>
  <tr><td>14_cycle4_intentgate</td><td>intent-gated that guard — block only when the student actually attempted an answer, so &ldquo;I don't know&rdquo; can still be pivoted away from. Cycle 3 had trapped the tutor re-posing.</td></tr>
  <tr><td>15_cycle5_prompt_tuning</td><td>a separate prompt variant per model family, aimed at the systematic rubric bleeds (over-probing, info-dumping, self-contradiction).</td></tr>
  <tr><td>16_cycle6</td><td>anti-repetition guard, the MCQ-fabrication fix, and telling the trajectory judge how the session ended. The transcript analysis of this cycle found prose/slot desync and opened the cycle-7 arc.</td></tr>
  <tr><td>17_cycle7_bottleneck_fixes</td><td>the slot becomes the single source of truth for the visible question, plus re-ask rejection, engine-vocabulary scrubbing and fixture repairs. Best single cycle: 26 &rarr; 41 of 60.</td></tr>
  <tr><td>18_cycle8_dispatch_grading</td><td>semantic dispatch order and MCQ value&rarr;letter grading. Gemini reached its best score; the stricter pose validation rejected malformed poses from kimi and qwen, which could not recover from them.</td></tr>
  <tr><td>18b_cycle8b_salvage</td><td>re-ran kimi and qwen with salvage instead of rejection for malformed poses. The lesson of cycle 8: validations need salvage for models that cannot self-correct.</td></tr>
  <tr><td>19_cycle9_letter_coherence</td><td>catalog letter coherence — the correct answer's TEXT is the authority and the reference letter is derived from where it actually sits in the options shown.</td></tr>
  <tr><td>20_cycle10_qwen_prompt</td><td>Qwen leg only: prompt iteration against its signature failures (hint micro-step loops, precision pedantry, re-asking answered questions).</td></tr>
  <tr><td>21_cycle11_qwen_content_budgets</td><td>Qwen leg only: content-budget iteration.</td></tr>
  <tr><td>30_mt100_18arm_board</td><td><b>The 18-arm board, and the newest result set here</b> — 18 models &times; 100 representative scenarios = 1,800 judged sessions, ~47 h of API time. A different question from the cycles above: those iterate one engine, this compares models on a fixed engine. See below.</td></tr>
 </tbody></table>
 <p class="small">Per-cycle detail, with results and lessons, is in <code>offline_eval/multi_turn_results/fixcheck/_cycle_log.md</code>.</p>

 <h2>The mt100 board (run <code>30_mt100_18arm_board</code>)</h2>
 <p>18 model arms over the same 100 representative multi-turn scenarios — 1,800 judged sessions. Unlike the cycle runs, nothing about the engine changes between arms; only the tutor model does, so the rows are comparable to each other in a way the cycles are not.</p>
 <p>Headline from that run: <b>qwen3.6-27b-instruct, running locally, ties claude-opus-4-7 at 94%</b> on a lower rubric mean (0.86 vs 0.91) — it clears the bar as often, less polished per turn. The 14 cloud arms span only 88&ndash;94%, so this scenario set separates tiers rather than frontier models.</p>
 <p><b>Caveats that limit cross-row comparison</b> — the three <code>gpt-5.6</code> arms ran with reasoning <b>disabled</b>; <code>gemini-3.1-pro</code> bought its 92% with 16.4 h of wall clock against <code>gpt-5.4-mini</code>'s 1.7 h for the same score; <code>gemini-2.5-pro</code> was stopped at 2/100 by choice and is absent. One session errored (an Anthropic-side HTTP 500). The full board and run configuration are in <code>offline_eval/multi_turn_results/mt100/README.md</code>.</p>
 <p class="small">This is usually the run you want on the Grade tab: it is the newest, the arms are directly comparable, and every session carries a judge verdict to disagree with. Filter <b>run</b> to <code>30_mt100_18arm_board</code> there.</p>

 <h2>Reading a session</h2>
 <ul>
  <li><b>Rubric scores are colour-coded:</b> <span style="color:var(--pass)">green ≥ 0.70</span>, <span style="color:#b8860b">amber 0.40–0.69</span>, <span style="color:var(--fail)">red &lt; 0.40</span>. An item marked <b>n/a</b> did not apply to that session and is excluded from the mean.</li>
  <li><b>Transcript:</b> blue bubbles = tutor, green = student. The <i>phase</i> label on tutor turns (engage / explore / explain / elaborate / evaluate) is the 5E teaching phase for that lesson step.</li>
 </ul>

 <h2>Caveats — read before drawing conclusions</h2>
 <ul>
  <li><b>Small samples are noisy.</b> The n=5 runs have a ±~22-point margin — treat them as directional only. The n=20 cycles (±~11pp) are the trustworthy comparison.</li>
  <li><b>Some scenarios are meant to be hard.</b> <code>speedrun_*</code> and <code>short_session_*</code> have deliberately tight turn budgets; timing out on those is by design, not a tutor failure.</li>
  <li><b>02_errored_overload is not real data</b> — an API outage failed every session in it. It's here so the failure is visible, not to be scored.</li>
 </ul>
</div>

<div class="wrap" id="browse" style="display:none">
 <div class="filters">
  <select id="f-run" onchange="draw()"></select>
  <select id="f-model" onchange="draw()"></select>
  <select id="f-persona" onchange="draw()"></select>
  <select id="f-subject" onchange="draw()"></select>
  <select id="f-outcome" onchange="draw()"></select>
  <select id="f-pass" onchange="draw()"><option value="">pass: any</option><option value="1">PASS</option><option value="0">FAIL</option></select>
  <input type="search" id="f-text" placeholder="search scenario / transcript / judge reasoning…" oninput="draw()">
 </div>
 <table><thead><tr>
  <th onclick="sortBy('run')">run</th><th onclick="sortBy('m')">model</th>
  <th onclick="sortBy('s')">scenario</th><th onclick="sortBy('p')">persona</th>
  <th onclick="sortBy('sub')">subj</th><th onclick="sortBy('o')">outcome</th>
  <th onclick="sortBy('pass')">result</th><th onclick="sortBy('t')">turns</th>
  <th onclick="sortBy('rm')">rubric</th>
 </tr></thead><tbody id="rows"></tbody></table>
</div>

<div class="wrap" id="compare" style="display:none">
 <div class="filters">
  <select id="c-scenario" onchange="drawCompare()"></select>
  <select id="c-model" onchange="drawCompare()"></select>
  <span class="small">Shows the SAME scenario for one model across every run that ran it — see whether a cycle's fix changed that session.</span>
 </div>
 <div class="cmp" id="cmpcols"></div>
</div>

<div class="wrap" id="grade" style="display:none">
 <div class="rule">
  <b>The pass rule is all-or-nothing.</b> A session passes only if every applicable
  dimension sits at its desideratum (marked <span class="des">desired</span>). One
  dimension at &ldquo;To some extent&rdquo; fails the session — that is deliberate, and the
  per-dimension rates are what the analysis reports. Where a dimension genuinely never
  arose, choose <i>Not applicable</i>; it is excluded from scoring rather than counted as
  a failure. Leaving a dimension blank is not the same thing — an incomplete grading is
  not scored at all.
  <br><b>The judge's grade is hidden until you save yours.</b> If you read it first, the
  agreement number measures how much it anchored you, not whether you independently agree.
 </div>
 <div class="filters">
  <select id="g-run" onchange="gradeQueue(0)"></select>
  <select id="g-model" onchange="gradeQueue(0)"></select>
  <select id="g-persona" onchange="gradeQueue(0)"></select>
  <select id="g-subject" onchange="gradeQueue(0)"></select>
  <select id="g-todo" onchange="gradeQueue(0)">
   <option value="todo">show: ungraded only</option>
   <option value="done">show: graded only</option>
   <option value="">show: all</option>
  </select>
  <button class="btn" onclick="gradeStep(-1)">&larr; prev</button>
  <button class="btn" onclick="gradeStep(1)">next &rarr;</button>
  <span class="stat" id="g-pos"></span>
 </div>
 <div class="gr" id="g-body"></div>
</div>

<div class="wrap" id="sxs" style="display:none">
 <div class="filters">
  <select id="x-run" onchange="fillSxsSel();drawSxs()"></select>
  <select id="x-model" onchange="fillSxsSel();drawSxs()"></select>
  <select id="x-sess" onchange="drawSxs()"></select>
  <span class="small">Every session you have graded, your verdict beside the judge's.</span>
 </div>
 <div id="x-summary"></div>
 <div id="x-body"></div>
</div>

<div class="wrap" id="agreement" style="display:none">
 <div class="filters">
  <select id="a-run" onchange="drawAgreement()"></select>
  <select id="a-model" onchange="drawAgreement()"></select>
  <select id="a-persona" onchange="drawAgreement()"></select>
  <select id="a-subject" onchange="drawAgreement()"></select>
  <select id="a-basis" onchange="drawAgreement()">
   <option value="rp">judge verdict: rubric only</option>
   <option value="pass">judge verdict: session overall (rubric + reached exit ticket)</option>
  </select>
 </div>
 <div id="a-body"></div>
</div>

<div class="overlay" id="ov" onclick="if(event.target.id=='ov')closeP()"></div>
<div class="panel" id="panel" style="display:none"></div>

<script>
const DATA = __DATA__;
const DIMS = __DIMS__;
__PASS_RULE__
__STATS__
let sortKey='run', sortDir=1;
const BY_KEY={}; DATA.forEach(d=>{BY_KEY[d.k]=d;});

function esc(s){return (s==null?'':String(s)).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));}
function uniq(k){return [...new Set(DATA.map(d=>d[k]).filter(x=>x!=null))].sort();}
function fillSel(id,label,vals){const s=document.getElementById(id);s.innerHTML='<option value="">'+label+': all</option>'+vals.map(v=>'<option>'+esc(v)+'</option>').join('');}
function scoreColor(v){if(v==null)return 'var(--mut)';return v>=0.7?'var(--pass)':v>=0.4?'#b8860b':'var(--fail)';}

function transcriptHTML(tr){return tr.map(t=>{
 const cls=t.role==='tutor'?'tutor':'student';
 const ph=t.ph&&t.role==='tutor'?' · '+esc(t.ph):'';
 return '<div class="msg '+cls+'"><div class="who">'+(t.role==='tutor'?'TUTOR':'student')+ph+'</div><pre>'+esc(t.c)+'</pre></div>';
}).join('');}

function rubricHTML(ru){if(!ru.length)return '<div class="small">no rubric scored (session errored or ended before judging).</div>';
 return '<table class="rub"><tbody>'+ru.map(it=>{
  const D=rubricDisplay(it);
  return '<tr><td><span class="bar"><i style="width:'+D.width+'%;background:'+scoreColor(D.value)+'"></i></span> '+
   '<b style="color:'+scoreColor(D.value)+'">'+D.text+'</b>'+
   (D.na?' <span class="small">did not apply</span>':'')+
   '</td><td><b>'+esc(it.it)+'</b><br><span class="small">'+esc(it.rz)+'</span></td></tr>';
 }).join('')+'</tbody></table>';}
function applicableCount(ru){return ru.filter(it=>!rubricDisplay(it).na).length;}

function sessionHTML(d){
 const rm=d.rm==null?'—':d.rm.toFixed(2);
 return '<div class="meta">'+esc(d.run)+' · <b>'+esc(d.m)+'</b> · persona '+esc(d.p)+' · '+esc(d.sub)+' · lesson '+esc(d.l)+
  '</div><div>outcome <b class="o-'+esc(d.o)+'">'+esc(d.o)+'</b> · '+
  '<span class="badge '+(d.pass?'b-pass':'b-fail')+'">'+(d.pass?'PASS':'FAIL')+'</span> · turns '+esc(d.t)+
  ' · rubric mean <b>'+rm+'</b> / '+esc(d.th)+'</div>'+
  (d.e?'<div class="small" style="color:var(--fail)">ERROR: '+esc(d.err)+'</div>':'')+
  '<div class="sec">Judge (rubric)</div>'+rubricHTML(d.ru)+
  '<div class="sec">Transcript</div>'+transcriptHTML(d.tr);
}

function openP(i){const d=SHOWN[i];document.getElementById('panel').innerHTML='<span class="close" onclick="closeP()">×</span><h2 style="margin:.2em 0">'+esc(d.s)+'</h2>'+sessionHTML(d);
 document.getElementById('ov').style.display='block';document.getElementById('panel').style.display='block';}
function closeP(){document.getElementById('ov').style.display='none';document.getElementById('panel').style.display='none';}

let SHOWN=[];
function draw(){
 const fr=v('f-run'),fm=v('f-model'),fp=v('f-persona'),fs=v('f-subject'),fo=v('f-outcome'),fpass=v('f-pass'),ft=v('f-text').toLowerCase();
 SHOWN=DATA.filter(d=>(!fr||d.run===fr)&&(!fm||d.m===fm)&&(!fp||d.p===fp)&&(!fs||d.sub===fs)&&(!fo||d.o===fo)&&
  (fpass===''||String(d.pass?1:0)===fpass)&&
  (!ft||d.s.toLowerCase().includes(ft)||d.tr.some(t=>t.c.toLowerCase().includes(ft))||d.ru.some(r=>(r.rz||'').toLowerCase().includes(ft))));
 SHOWN.sort((a,b)=>{let x=a[sortKey],y=b[sortKey];if(x==null)x='';if(y==null)y='';return (x>y?1:x<y?-1:0)*sortDir;});
 document.getElementById('rows').innerHTML=SHOWN.map((d,i)=>'<tr onclick="openP('+i+')">'+
  '<td class="small">'+esc(d.run)+'</td><td>'+esc(d.m)+'</td><td>'+esc(d.s)+'</td><td>'+esc(d.p)+'</td>'+
  '<td>'+esc(d.sub)+'</td><td class="o-'+esc(d.o)+'">'+esc(d.o)+'</td>'+
  '<td><span class="badge '+(d.pass?'b-pass':'b-fail')+'">'+(d.pass?'PASS':'FAIL')+'</span></td>'+
  '<td>'+esc(d.t)+'</td><td>'+(d.rm==null?'—':d.rm.toFixed(2))+'</td></tr>').join('');
 const np=SHOWN.filter(d=>d.pass).length;
 document.getElementById('count').textContent=SHOWN.length+' sessions · '+np+' pass ('+(SHOWN.length?Math.round(100*np/SHOWN.length):0)+'%)';
}
function v(id){return document.getElementById(id).value;}
function sortBy(k){if(sortKey===k)sortDir*=-1;else{sortKey=k;sortDir=1;}draw();}

const VIEWS=['about','browse','compare','grade','sxs','agreement'];
function setView(view){
 document.querySelectorAll('header .tab').forEach(t=>t.classList.toggle('on',t.dataset.v===view));
 VIEWS.forEach(x=>{document.getElementById(x).style.display=x===view?'':'none';});
 if(view==='compare')drawCompare();
 if(view==='grade')gradeQueue();
 if(view==='sxs'){fillSxsSel();drawSxs();}
 if(view==='agreement')drawAgreement();
}
function drawCompare(){
 const sc=v('c-scenario'),m=v('c-model');
 const set=DATA.filter(d=>d.s===sc&&d.m===m);
 // order columns by run label
 set.sort((a,b)=>a.run>b.run?1:-1);
 document.getElementById('cmpcols').innerHTML=set.length?set.map(d=>'<div class="col">'+
  '<div class="small">'+esc(d.run)+'</div><h3 style="margin:.2em 0">'+
  '<span class="badge '+(d.pass?'b-pass':'b-fail')+'">'+(d.pass?'PASS':'FAIL')+'</span> '+
  '<span class="o-'+esc(d.o)+'">'+esc(d.o)+'</span></h3>'+
  '<div class="small">rubric '+(d.rm==null?'—':d.rm.toFixed(2))+' · '+esc(d.t)+' turns</div>'+
  rubricHTML(d.ru)+'<div class="sec">Transcript</div>'+transcriptHTML(d.tr)+'</div>'
 ).join(''):'<div class="small">This model didn\'t run this scenario.</div>';
}

/* ── saved grades ──────────────────────────────────────────────────────
   One record per session: {d:{dimension:value}, notes, peeked, ts}. Held in
   localStorage, which is per-browser-profile and clearable — so the export is
   not a nicety, it is where a graded set actually lives. The header keeps a
   count of unexported changes rather than letting that go unnoticed. */
const LS='aitutor.manual.v1';
let STORE=loadStore();
// Sessions changed since the last export — not individual clicks. Eight radio
// presses on one session is one session at risk, and reporting it as "42
// unexported changes" tells the grader nothing they can act on.
let DIRTY=new Set();

function loadStore(){
 try{const raw=localStorage.getItem(LS);
  if(!raw)return {v:1,verdicts:{}};
  const o=JSON.parse(raw);
  if(!o||typeof o!=='object'||typeof o.verdicts!=='object'||!o.verdicts)return {v:1,verdicts:{}};
  return o;}
 catch(e){return {v:1,verdicts:{}};}
}
function saveStore(k){
 STORE.updated=new Date().toISOString();
 try{localStorage.setItem(LS,JSON.stringify(STORE));}
 catch(e){alert('Could not save to this browser ('+e.message+').\n\nExport now, or the grades on screen are all you have.');}
 if(k)DIRTY.add(k);
 paintDirty();
}
function paintDirty(){
 const n=DIRTY.size;
 document.getElementById('dirty').textContent=
  n?n+' session'+(n===1?'':'s')+' not exported':'';
}
function rec(k){return STORE.verdicts[k]||null;}
function graded(k){const r=rec(k);return !!(r&&gradeComplete(r.d||{}));}
function gradedKeys(){return Object.keys(STORE.verdicts).filter(graded);}
// Grades whose session is not in this build — a renamed run label or a result
// file that moved. Reported rather than silently dropped.
function orphanKeys(){return gradedKeys().filter(k=>!BY_KEY[k]);}

function exportGrades(){
 const keys=gradedKeys();
 const payload={version:1,exported:new Date().toISOString(),
  dimensions:DIMS.map(d=>d.key),graded:keys.length,verdicts:STORE.verdicts};
 const a=document.createElement('a');
 a.href=URL.createObjectURL(new Blob([JSON.stringify(payload,null,1)],{type:'application/json'}));
 a.download='manual_grades_'+keys.length+'.json';
 document.body.appendChild(a);a.click();a.remove();
 setTimeout(()=>URL.revokeObjectURL(a.href),2000);
 DIRTY.clear();paintDirty();
}
function importGrades(input){
 const f=input.files&&input.files[0];if(!f)return;
 const rd=new FileReader();
 rd.onload=()=>{
  let o;try{o=JSON.parse(rd.result);}catch(e){alert('Not valid JSON: '+e.message);return;}
  if(!o||typeof o.verdicts!=='object'||!o.verdicts){alert('No `verdicts` object in that file.');return;}
  let added=0,newer=0,kept=0;
  Object.keys(o.verdicts).forEach(k=>{
   const mine=STORE.verdicts[k],theirs=o.verdicts[k];
   if(!mine){STORE.verdicts[k]=theirs;added++;DIRTY.add(k);}
   else if((theirs.ts||'')>(mine.ts||'')){STORE.verdicts[k]=theirs;newer++;DIRTY.add(k);}
   else kept++;
  });
  saveStore();
  alert('Imported '+added+' new, replaced '+newer+' with a newer version, kept '+kept+' of mine.');
  refreshAll();
 };
 rd.readAsText(f);input.value='';
}

/* ── grading ─────────────────────────────────────────────────────────── */
let QUEUE=[],QI=0;

function gradeQueue(reset){
 const fr=v('g-run'),fm=v('g-model'),fp=v('g-persona'),fs=v('g-subject'),ft=v('g-todo');
 QUEUE=DATA.filter(d=>(!fr||d.run===fr)&&(!fm||d.m===fm)&&(!fp||d.p===fp)&&(!fs||d.sub===fs)&&
  (ft===''||(ft==='todo'?!graded(d.k):graded(d.k))));
 if(reset===0)QI=0;
 if(QI>=QUEUE.length)QI=Math.max(0,QUEUE.length-1);
 drawGrade();
}
function gradeStep(n){if(!QUEUE.length)return;QI=Math.min(QUEUE.length-1,Math.max(0,QI+n));drawGrade();}
function gradeGoto(k){
 const i=QUEUE.findIndex(d=>d.k===k);
 if(i<0){document.getElementById('g-todo').value='';gradeQueue(0);
  QI=Math.max(0,QUEUE.findIndex(d=>d.k===k));}
 else QI=i;
 setView('grade');drawGrade();
}

function drawGrade(){
 const done=gradedKeys().filter(k=>BY_KEY[k]).length;
 document.getElementById('g-pos').textContent=
  (QUEUE.length?(QI+1)+' of '+QUEUE.length+' in view':'nothing in view')+
  ' · '+done+' / '+DATA.length+' graded overall';
 const body=document.getElementById('g-body');
 if(!QUEUE.length){body.innerHTML='<div class="small">No sessions match these filters.</div>';return;}
 const d=QUEUE[QI],r=rec(d.k)||{d:{},notes:'',peeked:false};
 body.innerHTML=
  '<div class="gr-l">'+
   '<div class="meta">'+esc(d.run)+' · <b>'+esc(d.m)+'</b> · persona '+esc(d.p)+' · '+
    esc(d.sub)+' · lesson '+esc(d.l)+'</div>'+
   '<h3 style="margin:.1em 0 .3em">'+esc(d.s)+'</h3>'+
   '<div class="small">'+esc(d.t)+' turns · outcome <b class="o-'+esc(d.o)+'">'+esc(d.o)+'</b></div>'+
   (d.e?'<div class="small" style="color:var(--fail)">ERROR: '+esc(d.err)+'</div>':'')+
   '<div class="sec">Transcript</div><div class="tsc">'+transcriptHTML(d.tr)+'</div>'+
   '<div class="sec">Judge</div><div id="g-judge">'+judgeBox(d,r)+'</div>'+
  '</div>'+
  '<div>'+dimFormHTML(r)+
   '<div class="sec">Notes</div>'+
   '<textarea id="g-notes" onchange="setNotes(this.value)" placeholder="Anything the eight dimensions do not capture.">'+esc(r.notes||'')+'</textarea>'+
   '<div style="margin-top:10px" id="g-verdict"></div>'+
   '<div style="margin-top:10px"><button class="btn pri" onclick="gradeStep(1)">Next session &rarr;</button> '+
   '<button class="btn" onclick="clearGrade()">Clear this grade</button></div>'+
  '</div>';
 paintVerdict();
}

function dimFormHTML(r){
 const vals=r.d||{};
 return DIMS.map(dim=>{
  const cur=vals[dim.key]||'';
  const opts=dim.values.map(pair=>{
   const val=pair[0],label=pair[1];
   return '<label><input type="radio" name="'+esc(dim.key)+'" value="'+esc(val)+'"'+
    (cur===val?' checked':'')+' onchange="setDim(\''+esc(dim.key)+'\',this.value)">'+
    '<span>'+esc(label)+(val===dim.desideratum?' <span class="des">desired</span>':'')+'</span></label>';
  }).join('');
  return '<div class="dim'+(cur?' done':'')+'" data-k="'+esc(dim.key)+'">'+
   '<div class="nm">'+esc(dim.label)+'</div>'+
   '<div class="df">'+esc(dim.definition)+'</div>'+
   '<div class="gd">'+esc(dim.guidance)+'</div>'+opts+'</div>';
 }).join('');
}

function touch(k){
 const r=STORE.verdicts[k]||{d:{},notes:'',peeked:false,ts:''};
 r.d=r.d||{};STORE.verdicts[k]=r;return r;
}
function setDim(key,val){
 const d=QUEUE[QI];if(!d)return;
 const r=touch(d.k);r.d[key]=val;r.ts=new Date().toISOString();saveStore(d.k);
 // Repaint only what changed — re-rendering the form would throw away the
 // grader's scroll position halfway down eight dimensions.
 const card=document.querySelector('.dim[data-k="'+key+'"]');if(card)card.classList.add('done');
 paintVerdict();
 document.getElementById('g-judge').innerHTML=judgeBox(d,r);
}
function setNotes(t){const d=QUEUE[QI];if(!d)return;
 const r=touch(d.k);r.notes=t;r.ts=new Date().toISOString();saveStore(d.k);}
function clearGrade(){
 const d=QUEUE[QI];if(!d)return;
 if(!confirm('Discard your grade for '+d.s+'?'))return;
 delete STORE.verdicts[d.k];saveStore(d.k);drawGrade();
}
function peekJudge(){
 const d=QUEUE[QI];if(!d)return;
 const r=touch(d.k);r.peeked=true;r.ts=new Date().toISOString();saveStore(d.k);
 document.getElementById('g-judge').innerHTML=judgeBox(d,r);
}

function paintVerdict(){
 const d=QUEUE[QI];if(!d)return;
 const vals=(rec(d.k)||{}).d||{};
 const n=DIMS.filter(x=>vals[x.key]).length;
 const el=document.getElementById('g-verdict');if(!el)return;
 if(n<DIMS.length){
  el.innerHTML='<span class="small">'+n+' of '+DIMS.length+
   ' answered — an incomplete grading is not scored.</span>';return;}
 const p=sessionPasses(vals);
 el.innerHTML='Your verdict: <span class="badge '+(p?'b-pass':'b-fail')+'">'+(p?'PASS':'FAIL')+
  '</span> <span class="small">'+esc(failReason(vals))+'</span>';
}
function failReason(vals){
 const off=DIMS.filter(d=>dimensionPasses(d.key,vals[d.key]||'')===false).map(d=>d.label);
 if(off.length)return '— off desideratum: '+off.join(', ');
 const scorable=DIMS.filter(d=>dimensionPasses(d.key,vals[d.key]||'')!==null).length;
 if(!scorable)return '— nothing scorable: every dimension is n/a';
 return '— all '+scorable+' applicable dimensions at desideratum';
}

function judgeBox(d,r){
 if(graded(d.k)||r.peeked)return judgeHTML(d);
 return '<div class="blind"><b>Hidden until you finish grading.</b>'+
  '<div class="small" style="margin:6px 0 9px">Reading the judge first turns the agreement figure into a measure of how much it anchored you.</div>'+
  '<button class="btn" onclick="peekJudge()">Reveal anyway</button>'+
  '<div class="small" style="margin-top:6px">Revealing marks this session <b>peeked</b>: still visible everywhere, but excluded from the headline agreement figure.</div></div>';
}
function judgeHTML(d){
 return '<div>rubric mean <b>'+(d.rm==null?'—':d.rm.toFixed(2))+'</b> / '+esc(d.th)+' · '+
  'judge verdict '+badge(d.rp)+' · session overall '+badge(d.pass)+'</div>'+rubricHTML(d.ru);
}
function badge(p){
 if(p==null)return '<span class="small">—</span>';
 return '<span class="badge '+(p?'b-pass':'b-fail')+'">'+(p?'PASS':'FAIL')+'</span>';
}

/* ── me vs judge ─────────────────────────────────────────────────────── */
function fillSxsSel(){
 const run=v('x-run');
 // Counts ride in the option labels so the dropdown reports the workload per
 // model without being opened one entry at a time.
 const ms=document.getElementById('x-model'),mprev=ms.value;
 const counts=modelCounts(gradedSessions(run,''));
 const total=counts.reduce((a,c)=>a+c.n,0);
 ms.innerHTML='<option value="">model: all ('+total+')</option>'+
  counts.map(c=>'<option value="'+esc(c.model)+'">'+esc(c.model)+' ('+c.n+')</option>').join('');
 if(counts.some(c=>c.model===mprev))ms.value=mprev;

 const model=ms.value;
 const keys=gradedKeys().filter(k=>BY_KEY[k]&&(!run||BY_KEY[k].run===run)&&
                                   (!model||BY_KEY[k].m===model)).sort();
 const s=document.getElementById('x-sess');
 const prev=s.value;
 s.innerHTML=keys.length?keys.map(k=>{
  const d=BY_KEY[k];
  return '<option value="'+esc(k)+'">'+esc(d.s)+' — '+esc(d.m)+' — '+esc(d.run)+'</option>';
 }).join(''):'<option value="">nothing graded yet</option>';
 if(keys.indexOf(prev)>=0)s.value=prev;
}

// Every persona present in the data, so the balance chart can show a zero bar
// for one you have not started rather than omitting the row.
function personasIn(run,model){
 return [...new Set(DATA.filter(d=>(!run||d.run===run)&&(!model||d.m===model))
  .map(d=>d.p).filter(x=>x!=null))].sort();
}
// Sessions with a complete grading, for workload counting. Deliberately looser
// than gradedRows below: a peeked session or one the judge never scored still
// took your time, so it still counts toward balance.
function gradedSessions(run,model){
 return gradedKeys().map(k=>BY_KEY[k])
  .filter(d=>d&&(!run||d.run===run)&&(!model||d.m===model));
}

function drawSxs(){
 const run=v('x-run'),model=v('x-model');
 drawSxsSummary(run,model);
 const k=v('x-sess'),body=document.getElementById('x-body');
 const d=BY_KEY[k],r=rec(k);
 if(!d||!r){body.innerHTML='<div class="small">Grade a session on the Grade tab and it appears here.</div>';return;}
 body.innerHTML=sxsHTML(d,r);
}

function drawSxsSummary(run,model){
 const S=gradedRows({run:run,model:model,basis:'rp'});
 const R=passRates(S.rows);
 const pct=x=>x==null?'—':x.toFixed(1)+'%';
 const scope=(model||'all models')+(run?' in '+run:'');
 const el=document.getElementById('x-summary');

 const cards='<div class="cards">'+
  '<div class="kpi"><b>'+R.n+'</b><span>sessions compared</span></div>'+
  '<div class="kpi"><b>'+pct(R.mine)+'</b><span>my pass rate</span></div>'+
  '<div class="kpi"><b>'+pct(R.judge)+'</b><span>LLM judge pass rate</span></div>'+
  '</div>';

 const excl=[];
 if(S.peeked)excl.push(S.peeked+' peeked');
 if(S.noJudge)excl.push(S.noJudge+' with no judge verdict');

 // Naming the scope in words matters once a model can be selected: three bare
 // percentages look identical whichever model produced them.
 let note;
 if(!R.n){
  note='Nothing to compare in <b>'+esc(scope)+'</b>'+
   (excl.length?' — '+excl.join(' and ')+', which is all of it.':' yet.');
 }else{
  const gap=Math.abs(R.mine-R.judge)<0.05?
   'You and the judge pass the same share of them.':
   'You pass '+Math.abs(R.mine-R.judge).toFixed(1)+' points '+
   (R.mine>R.judge?'MORE':'FEWER')+' of them than the judge — you are the '+
   (R.mine>R.judge?'more lenient':'stricter')+' of the two.';
  note='<b>'+esc(scope)+'</b> · '+R.n+' session'+(R.n===1?'':'s')+' compared. '+gap+
   (excl.length?' '+excl.join(' and ')+' excluded from these percentages — both would '+
    'bias the comparison. They still count in the chart below.':'');
 }

 el.innerHTML=cards+'<div class="small" style="margin:-6px 0 12px">'+note+'</div>'+
  personaChart(run,model);
}

// A single-series horizontal bar chart: one measure (sessions graded) across
// six nominal personas. Value labels sit outside the bar ends because the bars
// are short, and a hairline marks the level every bar is aiming at.
function personaChart(run,model){
 const personas=personasIn(run,model);
 if(!personas.length)return '';
 const rows=personaBalance(gradedSessions(run,model),personas);
 const target=rows.length?rows[0].target:0;
 const scale=Math.max(target,1);
 const done=rows.filter(r=>r.deficit===0).length;

 const bars=rows.map(r=>{
  const w=100*r.n/scale;
  const tip=r.deficit?('+'+r.deficit+' to match the leader ('+target+')'):'level with the leader';
  return '<div class="lb" title="'+esc(tip)+'">'+esc(r.persona)+'</div>'+
   '<div class="tr" title="'+esc(r.persona+' — '+r.n+' graded, '+tip)+'">'+
    '<div class="fl" style="width:'+w.toFixed(2)+'%"></div>'+
    (target>0?'<div class="tg" style="left:100%"></div>':'')+
   '</div>'+
   '<div class="vl">'+r.n+(r.deficit?' <span class="df">+'+r.deficit+'</span>':'')+'</div>';
 }).join('');

 return '<div class="chart-h">Sessions graded per persona'+
   (model?' — '+esc(model):'')+'</div>'+
  '<div class="chart-s">'+
   (target===0?'Nothing graded in this scope yet.':
    done+' of '+rows.length+' persona'+(rows.length===1?'':'s')+' '+
    (done===1?'is':'are')+' level with the most-graded ('+target+
    '). The hairline is that level; <b>+n</b> is how many more it needs.')+
   ' Every completed grading counts here, including peeked ones.</div>'+
  '<div class="bars">'+bars+'</div>';
}
function sxsHTML(d,r){
 const vals=r.d||{},mine=sessionPasses(vals),theirs=d.rp,sc=sessionScore(vals);
 const rows=DIMS.map(dim=>{
  const val=vals[dim.key]||'';
  const found=dim.values.filter(p=>p[0]===val);
  const label=found.length?found[0][1]:'—';
  const ok=dimensionPasses(dim.key,val);
  const mark=ok===true?'<span style="color:var(--pass)">&#10003;</span>':
   ok===false?'<span style="color:var(--fail)">&#10007;</span>':'<span class="small">–</span>';
  return '<tr><td>'+esc(dim.label)+'</td><td>'+esc(label)+'</td><td>'+mark+'</td></tr>';
 }).join('');
 const verdict=theirs==null?'<span class="small">the judge scored no rubric for this session, so there is nothing to compare</span>':
  (mine===theirs?'<span class="k-agree">&#10003; AGREE</span>':
   '<span class="k-dis">&#10007; DISAGREE — judge '+(theirs?'more lenient':'stricter')+'</span>');
 return '<div class="meta">'+esc(d.run)+' · <b>'+esc(d.m)+'</b> · '+esc(d.s)+' · persona '+
   esc(d.p)+' · '+esc(d.sub)+'</div>'+
  '<div class="vd" style="font-size:15px;margin-bottom:12px">'+verdict+
   (r.peeked?' <span class="pill">peeked — excluded from the headline figure</span>':'')+'</div>'+
  '<div class="sxs">'+
   '<div class="col"><div class="sec" style="margin-top:0">My grade</div>'+
    '<table class="rub"><tbody>'+rows+'</tbody></table>'+
    '<div style="margin-top:10px">verdict '+badge(mine)+' <span class="small">'+esc(failReason(vals))+'</span></div>'+
    '<div style="margin-top:4px"><b>'+(sc.pct==null?'—':sc.pct.toFixed(0)+'%')+'</b> '+
     '<span class="small">'+(sc.pct==null?'nothing scorable':
      sc.at+' of '+sc.applicable+' applicable dimensions at desideratum')+'</span></div>'+
    (r.notes?'<div class="small" style="margin-top:8px"><b>Notes:</b> '+esc(r.notes)+'</div>':'')+
   '</div>'+
   '<div class="col"><div class="sec" style="margin-top:0">LLM judge (rubric)</div>'+
    rubricHTML(d.ru)+
    '<div style="margin-top:10px">verdict '+badge(d.rp)+
     ' <span class="small">mean '+(d.rm==null?'—':d.rm.toFixed(2))+' / threshold '+esc(d.th)+'</span></div>'+
    '<div style="margin-top:4px"><b>'+(d.rm==null?'—':(100*d.rm).toFixed(0)+'%')+'</b> '+
     '<span class="small">rubric mean across '+applicableCount(d.ru)+' applicable items'+
      (d.ru.length>applicableCount(d.ru)?' ('+(d.ru.length-applicableCount(d.ru))+
       ' did not apply and are excluded, exactly as your n/a answers are)':'')+
     '</span></div>'+
    '<div class="small" style="margin-top:6px">session overall '+badge(d.pass)+
     ' — rubric AND reaching the exit ticket. Outcome was <b class="o-'+esc(d.o)+'">'+esc(d.o)+'</b>.</div>'+
   '</div>'+
  '</div>'+
  '<div style="margin-top:12px"><button class="btn" onclick="gradeGoto(\''+esc(d.k)+'\')">Open in Grade</button></div>'+
  '<div class="sec">Transcript</div>'+transcriptHTML(d.tr);
}

/* ── agreement ───────────────────────────────────────────────────────── */
// Cohen's kappa, matching apps/benchmark/session_scoring.py::cohens_kappa.
// kappa is UNDEFINED when expected agreement is 1.0 — which happens whenever both
// raters used one category throughout. Returning 0.0 there would report perfect
// agreement as chance-level and invert the conclusion, so it returns null and
// says why; the raw agreement is reported alongside.
function cohensKappa(pairs){
 const n=pairs.length;
 if(!n)return {kappa:null,observed:null,expected:null,n:0,reason:'no overlapping grades'};
 const cats=[...new Set([].concat.apply([],pairs))];
 const observed=pairs.filter(p=>p[0]===p[1]).length/n;
 const ac={},bc={};
 pairs.forEach(p=>{ac[p[0]]=(ac[p[0]]||0)+1;bc[p[1]]=(bc[p[1]]||0)+1;});
 const expected=cats.reduce((s,c)=>s+((ac[c]||0)/n)*((bc[c]||0)/n),0);
 if(expected>=1)return {kappa:null,observed:observed,expected:expected,n:n,
  reason:'no category variance — both rated every session the same way'};
 return {kappa:(observed-expected)/(1-expected),observed:observed,expected:expected,n:n,reason:''};
}

// The one place a graded session becomes a comparable row. Both the comparison
// tab's percentages and the agreement tab's statistics go through it, so the
// two can never quietly apply different exclusion rules to the same sessions.
function gradedRows(f){
 const out={rows:[],peeked:0,noJudge:0,orphan:orphanKeys().length,basis:f.basis};
 gradedKeys().forEach(k=>{
  const d=BY_KEY[k];if(!d)return;
  if((f.run&&d.run!==f.run)||(f.model&&d.m!==f.model)||
     (f.persona&&d.p!==f.persona)||(f.subject&&d.sub!==f.subject))return;
  const r=rec(k),j=f.basis==='pass'?d.pass:d.rp;
  if(j==null){out.noJudge++;return;}
  if(r.peeked){out.peeked++;return;}
  out.rows.push({d:d,r:r,p:d.p,h:sessionPasses(r.d||{}),j:!!j});
 });
 return out;
}
function agreementSet(){
 return gradedRows({run:v('a-run'),model:v('a-model'),persona:v('a-persona'),
                    subject:v('a-subject'),basis:v('a-basis')});
}

function drawAgreement(){
 const S=agreementSet(),rows=S.rows,n=rows.length;
 const body=document.getElementById('a-body');
 const excluded=[];
 if(S.peeked)excluded.push(S.peeked+' peeked at the judge before grading');
 if(S.noJudge)excluded.push(S.noJudge+' with no judge verdict to compare');
 if(S.orphan)excluded.push(S.orphan+' graded against sessions not in this build');
 const note=excluded.length?'<div class="small" style="margin-bottom:10px">Excluded: '+
  excluded.join('; ')+'.</div>':'';

 if(!n){body.innerHTML=note+'<div class="small">Nothing to compare yet — grade some sessions on the Grade tab.</div>';return;}

 const tt=rows.filter(x=>x.h&&x.j).length, tf=rows.filter(x=>x.h&&!x.j).length,
       ft=rows.filter(x=>!x.h&&x.j).length, ff=rows.filter(x=>!x.h&&!x.j).length;
 const raw=(tt+ff)/n;
 const K=cohensKappa(rows.map(x=>[x.h?'pass':'fail',x.j?'pass':'fail']));
 const pct=x=>(100*x).toFixed(1)+'%';

 const kpis='<div class="cards">'+
  '<div class="kpi"><b>'+n+'</b><span>sessions compared</span></div>'+
  '<div class="kpi"><b>'+pct(raw)+'</b><span>raw agreement</span></div>'+
  '<div class="kpi"><b>'+(K.kappa==null?'—':K.kappa.toFixed(2))+'</b><span>Cohen&#39;s &kappa;</span></div>'+
  '<div class="kpi"><b>'+pct(rows.filter(x=>x.h).length/n)+'</b><span>my pass rate</span></div>'+
  '<div class="kpi"><b>'+pct(rows.filter(x=>x.j).length/n)+'</b><span>judge pass rate</span></div>'+
  '</div>'+
  (K.kappa==null?'<div class="small" style="margin:-6px 0 12px">&kappa; is undefined here: '+esc(K.reason)+'. Read the raw agreement instead.</div>':'');

 const matrix='<div class="sec">Confusion matrix</div>'+
  '<table class="mtx"><tbody>'+
  '<tr><th></th><th>judge PASS</th><th>judge FAIL</th></tr>'+
  '<tr><th>my PASS</th><td class="hit">'+tt+'</td><td class="miss">'+tf+'</td></tr>'+
  '<tr><th>my FAIL</th><td class="miss">'+ft+'</td><td class="hit">'+ff+'</td></tr>'+
  '</tbody></table>'+
  '<div class="small" style="margin-top:6px">'+tf+' the judge failed and I passed · '+
   ft+' the judge passed and I failed'+
   (ft>tf?' — the judge is the more lenient of us.':tf>ft?' — the judge is the stricter of us.':'.')+
  '</div>';

 const dimRows=DIMS.map(dim=>{
  let p=0,f=0,na=0;
  rows.forEach(x=>{const o=dimensionPasses(dim.key,(x.r.d||{})[dim.key]||'');
   if(o===true)p++;else if(o===false)f++;else na++;});
  const scored=p+f;
  return '<tr><td>'+esc(dim.label)+'</td><td>'+(scored?pct(p/scored):'—')+'</td>'+
   '<td>'+p+'</td><td>'+f+'</td><td>'+na+'</td></tr>';
 }).join('');
 const dimTable='<div class="sec">My per-dimension pass rate</div>'+
  '<div class="small" style="margin-bottom:6px">Which dimension drives my fails. n/a is excluded from the rate, not counted against.</div>'+
  '<table class="mtx"><tbody><tr><th>dimension</th><th>pass rate</th><th>at desideratum</th><th>off</th><th>n/a</th></tr>'+
  dimRows+'</tbody></table>';

 const dis=rows.filter(x=>x.h!==x.j);
 const disList=dis.length?'<div class="sec">Where we disagree ('+dis.length+')</div>'+
  '<table><thead><tr><th>scenario</th><th>model</th><th>run</th><th>me</th><th>judge</th><th>rubric</th><th></th></tr></thead><tbody>'+
  dis.map(x=>'<tr><td>'+esc(x.d.s)+'</td><td>'+esc(x.d.m)+'</td><td class="small">'+esc(x.d.run)+'</td>'+
   '<td>'+badge(x.h)+'</td><td>'+badge(x.j)+'</td><td>'+(x.d.rm==null?'—':x.d.rm.toFixed(2))+'</td>'+
   '<td><button class="btn" onclick="openSxs(\''+esc(x.d.k)+'\')">compare</button></td></tr>').join('')+
  '</tbody></table>':'<div class="sec">Where we disagree</div><div class="small">Nowhere — we agree on all '+n+'.</div>';

 body.innerHTML=note+kpis+matrix+dimTable+disList;
}
function openSxs(k){
 const d=BY_KEY[k];
 setView('sxs');
 // Move this tab's own filters onto the session first, or a run or model
 // selected here would leave the requested session out of the list entirely
 // and the tab would open on whatever happened to be selected before.
 if(d){
  document.getElementById('x-run').value=d.run;
  fillSxsSel();
  document.getElementById('x-model').value=d.m;
 }
 fillSxsSel();
 document.getElementById('x-sess').value=k;
 drawSxs();
}

function refreshAll(){
 paintDirty();gradeQueue();fillSxsSel();drawSxs();drawAgreement();
}

// init
fillSel('f-run','run',uniq('run'));fillSel('f-model','model',uniq('m'));
fillSel('f-persona','persona',uniq('p'));fillSel('f-subject','subject',uniq('sub'));
fillSel('f-outcome','outcome',uniq('o'));
document.getElementById('c-scenario').innerHTML=uniq('s').map(x=>'<option>'+esc(x)+'</option>').join('');
document.getElementById('c-model').innerHTML=uniq('m').map(x=>'<option>'+esc(x)+'</option>').join('');
fillSel('g-run','run',uniq('run'));fillSel('g-model','model',uniq('m'));
fillSel('g-persona','persona',uniq('p'));fillSel('g-subject','subject',uniq('sub'));
fillSel('x-run','run',uniq('run'));
fillSel('a-run','run',uniq('run'));fillSel('a-model','model',uniq('m'));
fillSel('a-persona','persona',uniq('p'));fillSel('a-subject','subject',uniq('sub'));
draw();
refreshAll();
</script></body></html>"""


def _embed(obj):
    """JSON for inlining inside a <script> — `</` would close the tag early."""
    return json.dumps(obj, ensure_ascii=False).replace("</", "<\\/")


def build(src=None, out=None, flat_runs=None):
    sessions = collect(src=src, flat_runs=flat_runs)
    out = OUT if out is None else out
    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    html = (TEMPLATE
            .replace("__DIMS__", _embed(dimensions_payload()))
            .replace("__PASS_RULE__", PASS_RULE_JS)
            .replace("__STATS__", STATS_JS)
            .replace("__DATA__", _embed(sessions)))
    with open(out, "w") as fh:
        fh.write(html)
    mb = os.path.getsize(out) / 1e6
    print(f"wrote {out}  ({len(sessions)} sessions, {mb:.1f} MB)")


if __name__ == "__main__":
    build()
