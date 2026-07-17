"""Export multi-turn eval result JSONs into a human-reviewable structure.

For each session it writes a markdown file with the FULL chat transcript and the
judge's rubric scoring (every item, its 0-1 score, applicable flag, and the
judge's one-line reasoning), plus a top-level INDEX.csv listing every session so
you can sort/filter, and a README describing each run.

    ./venv/bin/python offline_eval/export_review_data.py

Reads offline_eval/multi_turn_results/fixcheck/{_prefix_colab,_n5_sample5,
_cycle0_baseline,_cycle1,...}/*.json ; writes offline_eval/experiment_review/.
Re-run any time (e.g. after a new cycle completes) — it rebuilds from scratch.
"""
import csv
import glob
import json
import os
import re
import shutil

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "offline_eval", "multi_turn_results", "fixcheck")
OUT = os.path.join(ROOT, "offline_eval", "experiment_review")

# Source subfolder -> (sort-key label, one-line description) for the README + tree.
RUNS = {
    "_prefix_colab":    ("00_prefix_colab",
                         "Pre anti-repetition / end-reason fixes (Colab). n=5."),
    "_n5_sample5":      ("01_n5_sample5",
                         "n=5 smoke: anti-repetition + end-reason + softened Gemini rule."),
    "_stiff_rule":      ("01b_stiff_gemini",
                         "Gemini only, n=5, the STIFF anti-repetition rule (later softened)."),
    "_errored_overload": ("02_errored_overload_INVALID",
                          "n=20 attempt WIPED by an Anthropic 529 overload — INVALID, kept as evidence."),
    "_cycle0_baseline": ("10_cycle0_baseline",
                         "n=20 seed-5 baseline (anti-repetition + end-reason + softened Gemini)."),
    "_cycle1":          ("11_cycle1_retry+budget",
                         "n=20 seed-5: + transient-error retry + step+8 turn budgets."),
    "_cycle2":          ("12_cycle2_persona_budgets",
                         "n=20 seed-5: + persona-aware turn budgets (struggling personas step*3)."),
}


def _slug(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(s or "unknown"))[:80]


def _fmt_session(run_label, model, r) -> str:
    sid = r.get("scenario_id", "?")
    passed = r.get("passed")
    reason = r.get("sim_reason", "?")
    err = r.get("error")
    rr = r.get("rubric_result") or {}
    mean = rr.get("mean_score")
    thr = rr.get("pass_threshold")
    judge = rr.get("model")

    lines = [f"# {sid}", ""]
    lines.append(f"- **run**: {run_label}   **model (tutor)**: {model}")
    lines.append(f"- **persona**: {r.get('persona')}   **subject**: {r.get('subject')}"
                 f"   **lesson**: {r.get('lesson_id')}")
    lines.append(f"- **outcome**: `{reason}`   **result**: "
                 f"{'✅ PASS' if passed else '❌ FAIL'}   **turns**: {r.get('sim_turns')}")
    if err:
        lines.append(f"- **ERROR**: `{str(err)[:300]}`")
    lines.append("")

    # --- Judge / rubric ---------------------------------------------------
    lines.append("## Judge (rubric scoring)")
    if rr.get("error"):
        lines.append(f"_judge error: {rr['error']}_")
    elif not rr.get("items"):
        lines.append("_no rubric scored (session errored or ended before scoring)._")
    else:
        verdict = "PASS" if rr.get("passed") else "FAIL"
        lines.append(f"judge: `{judge}`  ·  mean **{mean:.2f}** / threshold {thr} "
                     f"→ **{verdict}**")
        lines.append("")
        lines.append("| score | applicable | rubric item | judge reasoning |")
        lines.append("|---|---|---|---|")
        for it in rr["items"]:
            sc = it.get("score")
            scs = f"{sc:.2f}" if isinstance(sc, (int, float)) else str(sc)
            appl = "yes" if it.get("applicable") else "n/a"
            item = str(it.get("item", "")).replace("|", "\\|").replace("\n", " ")
            reason = str(it.get("reasoning", "")).replace("|", "\\|").replace("\n", " ")
            lines.append(f"| {scs} | {appl} | {item} | {reason} |")
    lines.append("")

    # --- Transcript -------------------------------------------------------
    lines.append("## Chat transcript")
    lines.append("")
    for t in (r.get("transcript") or []):
        role = t.get("role", "?")
        phase = t.get("phase")
        tag = f" _(phase: {phase})_" if phase and role == "tutor" else ""
        who = "🧑‍🏫 **TUTOR**" if role == "tutor" else "🧑‍🎓 **student**"
        content = str(t.get("content", "")).strip()
        lines.append(f"{who}{tag}:")
        lines.append("")
        for ln in content.splitlines() or [""]:
            lines.append(f"> {ln}")
        lines.append("")
    return "\n".join(lines)


def build():
    if os.path.isdir(OUT):
        shutil.rmtree(OUT)
    os.makedirs(OUT)
    index_rows = []

    for sub, (label, _desc) in RUNS.items():
        srcdir = os.path.join(SRC, sub)
        for jf in sorted(glob.glob(os.path.join(srcdir, "*.json"))):
            model = os.path.basename(jf)[:-5]
            try:
                data = json.load(open(jf))
            except Exception:
                continue
            outdir = os.path.join(OUT, label, _slug(model))
            os.makedirs(outdir, exist_ok=True)
            for r in data.get("results", []):
                sid = r.get("scenario_id", "unknown")
                passed = bool(r.get("passed"))
                rr = r.get("rubric_result") or {}
                fname = f"{_slug(sid)}__{'PASS' if passed else 'FAIL'}.md"
                with open(os.path.join(outdir, fname), "w") as fh:
                    fh.write(_fmt_session(label, model, r))
                index_rows.append({
                    "run": label, "model": model, "scenario_id": sid,
                    "persona": r.get("persona"), "subject": r.get("subject"),
                    "lesson_id": r.get("lesson_id"), "outcome": r.get("sim_reason"),
                    "passed": passed, "turns": r.get("sim_turns"),
                    "rubric_mean": rr.get("mean_score"),
                    "threshold": rr.get("pass_threshold"),
                    "errored": bool(r.get("error")),
                    "path": os.path.relpath(os.path.join(outdir, fname), OUT),
                })

    # INDEX.csv
    cols = ["run", "model", "scenario_id", "persona", "subject", "lesson_id",
            "outcome", "passed", "turns", "rubric_mean", "threshold", "errored", "path"]
    with open(os.path.join(OUT, "INDEX.csv"), "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for row in sorted(index_rows, key=lambda r: (r["run"], r["model"], r["scenario_id"])):
            w.writerow(row)

    # README
    with open(os.path.join(OUT, "README.md"), "w") as fh:
        fh.write("# Multi-turn tutoring experiment — raw data\n\n")
        fh.write("Each session below has its own markdown file with the **full chat "
                 "transcript** and the **judge's rubric scoring** (every item, its "
                 "0–1 score, and the judge's reasoning). `INDEX.csv` lists every "
                 "session for sorting/filtering.\n\n")
        fh.write("Tutor = the model under test; student + rubric judge = Anthropic "
                 "(Haiku sim, Sonnet trajectory judge). A session passes only when it "
                 "reaches the exit ticket AND the rubric mean clears the threshold.\n\n")
        fh.write("## Runs (chronological)\n\n")
        for sub, (label, desc) in RUNS.items():
            n = len(glob.glob(os.path.join(SRC, sub, "*.json")))
            if n:
                fh.write(f"- **{label}/** — {desc}\n")
        fh.write("\n`outcome`: `exit_ticket` = reached the graded quiz (success) · "
                 "`max_turns` = ran out of turns · `deadlock` = tutor/student stuck · "
                 "`error` = infrastructure failure.\n")

    print(f"wrote {len(index_rows)} sessions to {OUT}")
    by_run = {}
    for r in index_rows:
        by_run.setdefault(r["run"], 0)
        by_run[r["run"]] += 1
    for run, n in sorted(by_run.items()):
        print(f"  {run}: {n} sessions")


if __name__ == "__main__":
    build()
