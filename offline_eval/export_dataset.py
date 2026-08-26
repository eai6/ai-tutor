"""Flatten the geography run into one workbook and a folder of CSVs.

    python offline_eval/export_dataset.py

The run's raw form is scattered — one JSON per arm for boards, one JSONL per
arm for traces, one JSON for the human grades — and each is nested. This
flattens all of it into tidy tables, one row per observation, so it can be
opened in a spreadsheet, read into R or Stata, or attached to a paper.

Writes offline_eval/export/:
    ai_tutor_geography_dataset.xlsx   every table as a sheet, plus a codebook
    *.csv                             the same tables, one file each
    transcripts.jsonl                 full conversation text, one session/line

GEOGRAPHY ONLY, matching the analysis. Two maths lessons hold fewer bank
questions than a session consumes, so sessions there end on the turn cap rather
than on the tutor's behaviour.

TIDY MEANS ONE ROW PER OBSERVATION. Grades are exported LONG (one row per
session x dimension) rather than wide, because that is the shape a regression
or a cross-tab wants, and the wide form is one pivot away.
"""
import collections
import glob
import json
import os
import pathlib
import sys

import pandas as pd

sys.path.insert(0, ".")
from ai_tutor.apps.benchmark import pedagogy as P            # noqa: E402

RES = "offline_eval/multi_turn_results"
BOARDS = {"geo_4b_v2": "on-device", "geo_27b_v2": "on-device", "geo_cloud": "cloud"}
GRADES = "offline_eval/manual_grades/manual_grades_3runs_169.json"
OUT = pathlib.Path("offline_eval/export")

PRICE_IN = {"claude-opus-4-7": 5.00, "gemini-3.5-flash": 0.30, "gpt-5.4-mini": 0.25}
PRICE_OUT = {"claude-opus-4-7": 25.00, "gemini-3.5-flash": 2.50, "gpt-5.4-mini": 2.00}
CACHE_READ, CACHE_WRITE, CHARS_PER_TOKEN, CALL1_OUT = 0.10, 1.25, 4.0, 25


def sessions_and_messages():
    """One row per session; one row per message."""
    srows, mrows = [], []
    for board, tier in BOARDS.items():
        for f in sorted(glob.glob(f"{RES}/{board}/*.json")):
            if os.path.basename(f).startswith("partial_"):
                continue                       # resume checkpoints, not boards
            arm = os.path.basename(f)[:-5]
            for r in json.load(open(f)).get("results", []):
                tr = r.get("transcript", [])
                tutor = [t for t in tr if t.get("role") == "tutor"]
                lat = [t["latency_ms"] / 1000 for t in tutor
                       if t.get("latency_ms") is not None]
                srows.append({
                    "arm": arm, "tier": tier, "board": board,
                    "scenario_id": r.get("scenario_id"),
                    "assertions_passed": r.get("passed"),
                    "tutor_responses": len(tutor),
                    "student_messages": sum(1 for t in tr if t.get("role") == "student"),
                    "tutor_seconds_total": round(sum(lat), 2) if lat else None,
                    "tutor_seconds_median": round(pd.Series(lat).median(), 2) if lat else None,
                })
                for i, t in enumerate(tr):
                    mrows.append({
                        "arm": arm, "tier": tier, "scenario_id": r.get("scenario_id"),
                        "message_index": i, "exchange_number": t.get("turn_number"),
                        "role": t.get("role"), "phase": t.get("phase"),
                        "latency_seconds": (round(t["latency_ms"] / 1000, 3)
                                            if t.get("latency_ms") is not None else None),
                        "characters": len(t.get("content") or ""),
                    })
    return pd.DataFrame(srows), pd.DataFrame(mrows)


def responses():
    """One row per tutor response, from the engine trace: tokens, tools, verdict."""
    rows = []
    for board in BOARDS:
        for f in sorted(glob.glob(f"{RES}/{board}/trace/*.jsonl")):
            arm = os.path.basename(f)[:-6]
            for line in open(f):
                r = json.loads(line)
                rows.append({
                    "arm": arm, "tier": BOARDS[board],
                    "session_id": r.get("session_id"), "lesson_id": r.get("lesson_id"),
                    "step_index": r.get("step_index"),
                    "model": r.get("model"), "answer_mode": r.get("answer_mode"),
                    "two_call": r.get("two_call"),
                    "tools_called": "|".join(r.get("tools") or []),
                    "grading_verdict": r.get("verdict"),
                    "tokens_input_fresh": r.get("tok_in", 0) or 0,
                    "tokens_input_cached": r.get("tok_cached", 0) or 0,
                    "tokens_cache_write": r.get("tok_write", 0) or 0,
                    "reply_characters": r.get("text_chars", 0) or len(r.get("reply", "")),
                    "retries": r.get("retries", 0) or 0,
                    "placeholder": bool(r.get("placeholder")),
                })
    return pd.DataFrame(rows)


def grades_long():
    """One row per graded session x dimension. Long, not wide."""
    g = json.load(open(GRADES))
    dims = g["dimensions"]
    des = {d.key: d.desideratum for d in P.DIMENSIONS}
    rows = []
    for k, v in g["verdicts"].items():
        run, arm, scenario = k.split("|", 2)
        if "geo" not in run and "postfix" not in run:
            continue
        complete = len(v.get("d", {})) == len(dims)
        for dim, val in v.get("d", {}).items():
            rows.append({
                "arm": arm, "board": run, "scenario_id": scenario,
                "dimension": dim, "verdict": val,
                "desideratum": des.get(dim),
                "at_desideratum": None if val == "n/a" else (val == des.get(dim)),
                "grading_complete": complete,
                "peeked": bool(v.get("peeked")),
                "session_passes": (P.session_passes(v["d"]) if complete else None),
                "graded_at": v.get("ts"),
            })
    return pd.DataFrame(rows)


def transcripts_jsonl(path):
    """Full conversation text — too big for a spreadsheet cell, so its own file."""
    n = 0
    with open(path, "w") as out:
        for board in BOARDS:
            for f in sorted(glob.glob(f"{RES}/{board}/*.json")):
                if os.path.basename(f).startswith("partial_"):
                    continue
                arm = os.path.basename(f)[:-5]
                for r in json.load(open(f)).get("results", []):
                    out.write(json.dumps({
                        "arm": arm, "board": board,
                        "scenario_id": r.get("scenario_id"),
                        "assertions_passed": r.get("passed"),
                        "messages": [{"role": t.get("role"),
                                      "content": t.get("content"),
                                      "latency_seconds": (t["latency_ms"] / 1000
                                                          if t.get("latency_ms") is not None
                                                          else None)}
                                     for t in r.get("transcript", [])],
                    }) + "\n")
                    n += 1
    return n


def cost_summary(resp):
    rows = []
    for arm, g in resp[resp.arm.isin(PRICE_IN)].groupby("arm"):
        out_tok = g.reply_characters.sum() / CHARS_PER_TOKEN + CALL1_OUT * len(g)
        pi, po = PRICE_IN[arm], PRICE_OUT[arm]
        billed = (g.tokens_input_fresh.sum() * pi
                  + g.tokens_input_cached.sum() * pi * CACHE_READ
                  + g.tokens_cache_write.sum() * pi * CACHE_WRITE
                  + out_tok * po) / 1e6
        rows.append({
            "arm": arm, "price_input_per_1M": pi, "price_output_per_1M": po,
            "tokens_input_fresh": int(g.tokens_input_fresh.sum()),
            "tokens_input_cached": int(g.tokens_input_cached.sum()),
            "tokens_cache_write": int(g.tokens_cache_write.sum()),
            "tokens_output_estimated": int(out_tok),
            "sessions": g.session_id.nunique(),
            "billed_usd": round(billed, 4),
            "usd_per_session": round(billed / g.session_id.nunique(), 5),
        })
    return pd.DataFrame(rows)


CODEBOOK = [
    ("sessions", "one row per tutoring session (34 per arm)"),
    ("messages", "one row per message, tutor and student, in order"),
    ("tutor_responses", "one row per tutor response from the engine trace: tokens, tools, verdict"),
    ("grades_long", "one row per graded session x dimension; LONG format"),
    ("cost_summary", "metered spend per cloud arm, derived from tutor_responses"),
    ("codebook", "this sheet"),
    ("", ""),
    ("KEY TERMS", ""),
    ("tutor response", "ONE message from the tutor. Not an exchange: a session with 7 "
                       "tutor responses also holds 6-7 student messages."),
    ("exchange_number", "the transcript's own turn_number: a tutor message and the "
                        "student reply share one value."),
    ("tier", "on-device (RTX 3090) or cloud API"),
    ("at_desideratum", "TRUE if the verdict matches the dimension's target. NULL for "
                       "n/a, which the taxonomy treats as unscorable, NOT as a failure."),
    ("session_passes", "TRUE only if EVERY applicable dimension is at its desideratum "
                       "(all-or-nothing). NULL if the grading is incomplete."),
    ("grading_complete", "FALSE if fewer than 8 dimensions were answered. Exclude these: "
                         "an unanswered dimension is not a 'no'."),
    ("peeked", "the judge's grade was revealed before grading finished; such a session "
               "measures anchoring, not independent judgement."),
    ("assertions_passed", "deterministic checks only (session ran, tools fired, grading "
                          "resolved). NOT a measure of teaching quality."),
    ("tokens_input_*", "the three input buckets are DISJOINT: a provider's input_tokens "
                       "excludes cached tokens, so prompt size is their sum."),
    ("tokens_output_estimated", "not recorded by the tracer; estimated from reply length "
                                "at ~4 chars/token plus ~25 tokens for the tool call."),
    ("", ""),
    ("SCOPE", "Geography only, 5 arms x 34 scenarios. Grades cover geography boards."),
]


def main():
    OUT.mkdir(exist_ok=True)
    sess, msgs = sessions_and_messages()
    resp = responses()
    grades = grades_long()
    cost = cost_summary(resp)
    code = pd.DataFrame(CODEBOOK, columns=["name", "meaning"])

    tables = {"sessions": sess, "messages": msgs, "tutor_responses": resp,
              "grades_long": grades, "cost_summary": cost, "codebook": code}
    for name, df in tables.items():
        df.to_csv(OUT / f"{name}.csv", index=False)

    xlsx = OUT / "ai_tutor_geography_dataset.xlsx"
    with pd.ExcelWriter(xlsx, engine="openpyxl") as w:
        code.to_excel(w, sheet_name="codebook", index=False)
        for name, df in tables.items():
            if name != "codebook":
                df.to_excel(w, sheet_name=name[:31], index=False)

    n = transcripts_jsonl(OUT / "transcripts.jsonl")

    print(f"  wrote {OUT}/\n")
    print(f"  {'table':<20}{'rows':>8}{'cols':>7}")
    for name, df in tables.items():
        print(f"  {name:<20}{len(df):>8}{len(df.columns):>7}")
    print(f"  {'transcripts.jsonl':<20}{n:>8}{'':>7}  full conversation text")
    print(f"\n  workbook: {xlsx.name} ({xlsx.stat().st_size//1024} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
