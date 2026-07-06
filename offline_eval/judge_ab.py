#!/usr/bin/env python3
"""Judge A/B — score the SAME multi-turn transcripts with two judges, report agreement.

Holds the transcript fixed and varies only the rubric judge (Haiku 4.5 vs
Sonnet 4.6), so any score difference is the judge, not the tutor. Answers:
"is Haiku a good enough judge for multi-turn, or does it diverge from a stronger
model near the pass/fail line?"

Transcripts are generated once (real tutor+student sessions) and cached, so
re-judging (adding a third judge, re-running with a tweaked rubric) is cheap and
doesn't re-run the tutor.

Run:  venv/bin/python offline_eval/judge_ab.py
Env:  SIMPLE_TUTOR_ENGINE=1 (default). Regenerate transcripts: rm the cache file.
"""
from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path

import django

# Standalone-script launch (`python offline_eval/judge_ab.py`) puts offline_eval/
# on sys.path, not the repo root, so `config.settings` isn't importable. Add it.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
os.environ.setdefault("SIMPLE_TUTOR_ENGINE", "1")
django.setup()

from evals.runner import Scenario, _eval_institution_and_user  # noqa: E402
from evals.scorers import llm_rubric  # noqa: E402
from apps.tutoring.student_sim import simulate_session  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATASET = ROOT / "evals" / "dataset" / "multi_turn"
CACHE = ROOT / "offline_eval" / "multi_turn_results" / "_judge_ab_transcripts.json"
OUT = ROOT / "offline_eval" / "multi_turn_results" / "_judge_ab_report.json"

HAIKU = {"provider": "anthropic", "model": "claude-haiku-4-5-20251001",
         "temperature": 0.0, "max_tokens": 4096}
SONNET = {"provider": "anthropic", "model": "claude-sonnet-4-6",
          "temperature": 0.0, "max_tokens": 4096}

# 8 sessions on a single fixed tutor, spanning all 6 personas + both subjects so
# the judges see a range of session quality (capable/average high; error_prone /
# probe_resistant / non_responder lower).
TUTOR = "google/gemini-2.5-flash"
SAMPLE = [
    "capable_geo_direction_001",
    "capable_math_session_001",
    "average_geo_scale_001",
    "struggler_math_session_001",
    "error_prone_session_001",
    "error_prone_geo_direction_001",
    "probe_resistant_math_session_001",
    "non_responder_math_session_001",
]


def generate_transcripts() -> list[dict]:
    if CACHE.exists():
        print(f"[cache] reusing {CACHE.name} (rm it to regenerate)")
        return json.loads(CACHE.read_text())
    inst, _user = _eval_institution_and_user()
    os.environ["TUTOR_MODEL_OVERRIDE"] = TUTOR
    out = []
    for sid in SAMPLE:
        scn = Scenario.from_yaml(DATASET / f"{sid}.yaml")
        sim = simulate_session(
            lesson_id=scn.lesson_id, persona=scn.persona,
            max_turns=scn.max_turns, institution_id=inst.id,
        )
        transcript = [{"role": t.role, "content": t.content} for t in sim.transcript]
        out.append({
            "scenario": sid, "model": TUTOR, "persona": scn.persona,
            "subject": scn.subject, "sim_reason": sim.reason,
            "rubric": scn.rubric, "pass_threshold": scn.pass_threshold,
            "transcript": transcript,
        })
        print(f"  ran {sid:38s} {sim.reason:12s} {len(transcript)} turns", flush=True)
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(out, indent=1))
    print(f"[cache] wrote {CACHE.name}")
    return out


def judge(item: dict, cfg: dict):
    return llm_rubric.score_trajectory(
        item["rubric"], transcript=item["transcript"],
        pass_threshold=item["pass_threshold"], judge_config=cfg,
    )


def pearson(xs, ys) -> float:
    n = len(xs)
    if n < 2:
        return float("nan")
    mx, my = sum(xs) / n, sum(ys) / n
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    return num / (dx * dy) if dx and dy else float("nan")


def cohen_kappa(a: list[bool], b: list[bool]) -> float:
    n = len(a)
    if not n:
        return float("nan")
    po = sum(1 for x, y in zip(a, b) if x == y) / n
    pa1 = sum(a) / n
    pb1 = sum(b) / n
    pe = pa1 * pb1 + (1 - pa1) * (1 - pb1)
    return (po - pe) / (1 - pe) if pe != 1 else 1.0


def main():
    items = generate_transcripts()
    rows = []
    print("\n=== judging (Haiku vs Sonnet) ===")
    for it in items:
        h = judge(it, HAIKU)
        s = judge(it, SONNET)
        rows.append({
            "scenario": it["scenario"], "persona": it["persona"],
            "subject": it["subject"], "sim_reason": it["sim_reason"],
            "haiku_mean": round(h.mean_score, 3), "haiku_pass": h.passed,
            "haiku_err": h.error,
            "sonnet_mean": round(s.mean_score, 3), "sonnet_pass": s.passed,
            "sonnet_err": s.error,
            "delta": round(h.mean_score - s.mean_score, 3),
            "threshold": it["pass_threshold"],
            "haiku_items": [(i.score, i.applicable) for i in h.items],
            "sonnet_items": [(i.score, i.applicable) for i in s.items],
        })
        flag = "" if h.passed == s.passed else "   <-- PASS/FAIL DISAGREE"
        print(f"  {it['scenario']:38s} H={h.mean_score:.2f}/{'P' if h.passed else 'F'}"
              f"  S={s.mean_score:.2f}/{'P' if s.passed else 'F'}  Δ={h.mean_score - s.mean_score:+.2f}{flag}",
              flush=True)

    ok = [r for r in rows if not r["haiku_err"] and not r["sonnet_err"]]
    hmeans = [r["haiku_mean"] for r in ok]
    smeans = [r["sonnet_mean"] for r in ok]
    hpass = [r["haiku_pass"] for r in ok]
    spass = [r["sonnet_pass"] for r in ok]
    abs_deltas = [abs(r["delta"]) for r in ok]
    disagree = [r for r in ok if r["haiku_pass"] != r["sonnet_pass"]]

    print("\n=== AGREEMENT REPORT ===")
    print(f"sessions judged (no error): {len(ok)}/{len(rows)}")
    if ok:
        print(f"Haiku  mean-of-means: {sum(hmeans)/len(hmeans):.3f}  |  pass rate: {sum(hpass)}/{len(ok)}")
        print(f"Sonnet mean-of-means: {sum(smeans)/len(smeans):.3f}  |  pass rate: {sum(spass)}/{len(ok)}")
        print(f"mean |Δ score|: {sum(abs_deltas)/len(abs_deltas):.3f}   max |Δ|: {max(abs_deltas):.3f}")
        print(f"score correlation (Pearson r): {pearson(hmeans, smeans):.3f}")
        conc = sum(1 for x, y in zip(hpass, spass) if x == y)
        print(f"pass/fail concordance: {conc}/{len(ok)} ({100*conc/len(ok):.0f}%)   "
              f"Cohen's kappa: {cohen_kappa(hpass, spass):.3f}")
        if disagree:
            print(f"\nPASS/FAIL DISAGREEMENTS ({len(disagree)}):")
            for r in disagree:
                print(f"  {r['scenario']:38s} thr={r['threshold']}  "
                      f"Haiku {r['haiku_mean']:.2f}/{'P' if r['haiku_pass'] else 'F'}  "
                      f"Sonnet {r['sonnet_mean']:.2f}/{'P' if r['sonnet_pass'] else 'F'}")
        else:
            print("\nno pass/fail disagreements.")
    errs = [r for r in rows if r["haiku_err"] or r["sonnet_err"]]
    if errs:
        print(f"\njudge errors on {len(errs)} session(s):")
        for r in errs:
            print(f"  {r['scenario']}: haiku={r['haiku_err']!r} sonnet={r['sonnet_err']!r}")

    OUT.write_text(json.dumps({"tutor": TUTOR, "rows": rows}, indent=1))
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
