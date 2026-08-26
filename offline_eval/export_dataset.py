"""Flatten the geography run into one workbook and a folder of CSVs.

    python offline_eval/export_dataset.py

The run's raw form is scattered — one JSON per arm for boards, one JSONL per
arm for traces, one JSON for the human grades — and each is nested. This
flattens all of it into tidy tables, one row per observation, so it can be
opened in a spreadsheet, read into R or Stata, or attached to a paper.

Writes offline_eval/export/:
    ai_tutor_geography_dataset.xlsx   SELF-CONTAINED: every table as a sheet,
                                      including the transcripts and a codebook

No cost sheet: the measured quantity is tokens, and it is in tutor_responses.
Turning tokens into money needs list prices that move, cache multipliers, and
an estimate of the output tokens the tracer never recorded — assumptions softer
than the rest of the data, and better made by whoever needs the figure.
    *.csv                             the same tables, one file each
    transcripts_nested.jsonl          the same text nested by session

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



def sessions_and_messages():
    """One row per session; one row per message."""
    srows, mrows = [], []
    for board, tier in BOARDS.items():
        for f in sorted(glob.glob(f"{RES}/{board}/*.json")):
            if os.path.basename(f).startswith("partial_"):
                continue                       # resume checkpoints, not boards
            arm = os.path.basename(f)[:-5]
            d = json.load(open(f))
            # The boards timestamp the RUN, not each session — there is no
            # per-session clock in the data. Recorded as run_* so nobody reads
            # a session-level time into it, with the date split out for
            # filtering and git_sha for the code version that produced it.
            started, finished = d.get("started_at"), d.get("finished_at")
            for r in d.get("results", []):
                tr = r.get("transcript", [])
                tutor = [t for t in tr if t.get("role") == "tutor"]
                lat = [t["latency_ms"] / 1000 for t in tutor
                       if t.get("latency_ms") is not None]
                srows.append({
                    "arm": arm, "tier": tier, "board": board,
                    "session_id": r.get("session_id"),
                    "scenario_id": r.get("scenario_id"),
                    "lesson_id": r.get("lesson_id"),
                    "persona": r.get("persona"),
                    "run_date": (started or "")[:10] or None,
                    "run_started_at": started,
                    "run_finished_at": finished,
                    "git_sha": d.get("git_sha"),
                    "tutor_model_spec": d.get("tutor_model"),
                    "engine": d.get("engine"),
                    "assertions_passed": r.get("passed"),
                    "tutor_responses": len(tutor),
                    "student_messages": sum(1 for t in tr if t.get("role") == "student"),
                    "tutor_seconds_total": round(sum(lat), 2) if lat else None,
                    "tutor_seconds_median": round(pd.Series(lat).median(), 2) if lat else None,
                })
                for i, t in enumerate(tr):
                    mrows.append({
                        "arm": arm, "tier": tier,
                        "session_id": r.get("session_id"),
                        "scenario_id": r.get("scenario_id"),
                        "message_index": i, "exchange_number": t.get("turn_number"),
                        "role": t.get("role"), "phase": t.get("phase"),
                        # The message text itself. Longest in this corpus is
                        # 1,073 characters against Excel's 32,767 cell limit,
                        # so the transcript lives IN the workbook rather than
                        # in a side file the reader has to reunite with it.
                        "message": t.get("content") or "",
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


def scenario_to_session():
    """(arm, scenario_id) -> session_id.

    One scenario produces exactly one session per arm in this run — verified,
    not assumed: 170 sessions, 170 distinct (arm, scenario_id) pairs, no
    scenario run twice. The grade file keys on scenario, so session_id is
    joined in rather than left for the reader to reconstruct.
    """
    m = {}
    for board in BOARDS:
        for f in sorted(glob.glob(f"{RES}/{board}/*.json")):
            if os.path.basename(f).startswith("partial_"):
                continue
            arm = os.path.basename(f)[:-5]
            for r in json.load(open(f)).get("results", []):
                m[(arm, r.get("scenario_id"))] = r.get("session_id")
    return m


def grades_long():
    """One row per graded session x dimension. Long, not wide."""
    s2s = scenario_to_session()
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
                "arm": arm, "board": run,
                "session_id": s2s.get((arm, scenario)),
                "scenario_id": scenario,
                "dimension": dim, "verdict": val,
                "desideratum": des.get(dim),
                "at_desideratum": None if val == "n/a" else (val == des.get(dim)),
                "grading_complete": complete,
                "peeked": bool(v.get("peeked")),
                "session_passes": (P.session_passes(v["d"]) if complete else None),
                # Unlike the run clock, the grading clock IS per session.
                "graded_date": (v.get("ts") or "")[:10] or None,
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


def concurrency():
    """The GPU concurrency sweep: N simultaneous whole tutor responses vs latency.

    Parsed from the sweep log so the workbook is self-contained — otherwise the
    capacity section of the analysis is the one part a reader cannot reproduce
    from the export alone.
    """
    import re
    hdr = re.compile(r"^#+ (\S+) slots=(\d+)")
    row = re.compile(r"^\s*(\d+)\s+\d+\s+([\d.]+)s\s+([\d.]+)s")
    out, key = [], None
    path = pathlib.Path("offline_eval/sweep_rtx3090_turnmode.log")
    if not path.exists():
        return pd.DataFrame()
    for line in open(path):
        h = hdr.match(line)
        if h:
            key = (h.group(1), int(h.group(2)))
            continue
        m = row.match(line)
        if m and key:
            out.append({"model": key[0], "parallel_slots": key[1],
                        "concurrent_requests": int(m.group(1)),
                        "response_seconds_p50": float(m.group(2)),
                        "response_seconds_p95": float(m.group(3))})
    return pd.DataFrame(out)


CODEBOOK = [
    ("transcripts", "one row per message, IN ORDER, with the message text itself"),
    ("sessions", "one row per tutoring session (34 per arm)"),
    ("tutor_responses", "one row per tutor response from the engine trace: tokens, tools, verdict"),
    ("grades_long", "one row per graded session x dimension; LONG format"),
    ("(no cost sheet)", "deliberate. The measured quantity is TOKENS, in "
                        "tutor_responses. Converting them to dollars needs list "
                        "prices that change, cache-rate multipliers, and an ESTIMATE "
                        "of output tokens the tracer never recorded — so the money "
                        "figure is softer than the rest of this dataset and is left "
                        "to whoever needs it, with their own prices."),
    ("concurrency", "GPU capacity sweep: N simultaneous tutor responses vs latency, "
                    "measured on one RTX 3090. The ONLY sheet not drawn from the "
                    "tutoring sessions — it is a synthetic load test, so it has no "
                    "session_id and does not join to the other sheets."),
    ("qwen3-4b-ctx8k", "the same 4B weights as qwen3-4b-jetson with an 8,192-token "
                       "context instead of 16,384. Ollama allocates context PER "
                       "PARALLEL SLOT, so halving it fits 24 slots in the same 24 GB "
                       "rather than 12 — that is what produces the 48-concurrent "
                       "figure. NOTE: quality was measured at 16k context and "
                       "capacity at 8k, so those two results come from different "
                       "configurations of the same model."),
    ("codebook", "this sheet"),
    ("", ""),
    ("KEY TERMS", ""),
    ("tutor response", "ONE message from the tutor. Not an exchange: a session with 7 "
                       "tutor responses also holds 6-7 student messages."),
    ("exchange_number", "the transcript's own turn_number: a tutor message and the "
                        "student reply share one value."),
    ("tier", "on-device (RTX 3090) or cloud API"),
    ("session_id", "the tutoring session. One scenario produces exactly ONE session "
                   "per arm in this run (170 sessions, 170 distinct arm x scenario "
                   "pairs, none repeated), so session_id and scenario_id identify the "
                   "same thing here. Use session_id to join to tutor_responses."),
    ("scenario_id", "the test case that was run. Names the situation being tested; "
                    "the same scenario is run once per arm."),
    ("persona", "the simulated student's behaviour profile for that scenario."),
    ("run_date / run_started_at / run_finished_at",
     "when the ARM was run, not the individual session — the boards carry no "
     "per-session clock, so every session in one arm shares these values. All "
     "five arms ran on 2026-08-24."),
    ("git_sha", "the commit that produced the run. The on-device arms ran on "
                "earlier commits than the cloud arms; the engine was unchanged "
                "between them, but the sha is recorded so that is checkable "
                "rather than asserted."),
    ("tutor_model_spec", "the provider/model string the harness was given."),
    ("graded_date / graded_at", "when the human grading was recorded. Unlike the run "
                                "clock this IS per session. Grading ran 2026-08-25, the "
                                "day after the sessions."),
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
    code = pd.DataFrame(CODEBOOK, columns=["name", "meaning"])

    tables = {"transcripts": msgs, "sessions": sess, "tutor_responses": resp,
              "grades_long": grades, "concurrency": concurrency(),
              "codebook": code}
    for name, df in tables.items():
        df.to_csv(OUT / f"{name}.csv", index=False)

    xlsx = OUT / "ai_tutor_geography_dataset.xlsx"
    with pd.ExcelWriter(xlsx, engine="openpyxl") as w:
        code.to_excel(w, sheet_name="codebook", index=False)
        for name, df in tables.items():
            if name != "codebook":
                df.to_excel(w, sheet_name=name[:31], index=False)

    # JSONL kept for anyone who wants nested records; the workbook is
    # self-contained without it.
    n = transcripts_jsonl(OUT / "transcripts_nested.jsonl")

    print(f"  wrote {OUT}/\n")
    print(f"  {'table':<20}{'rows':>8}{'cols':>7}")
    for name, df in tables.items():
        print(f"  {name:<20}{len(df):>8}{len(df.columns):>7}")
    print(f"  {'transcripts_nested':<20}{n:>8}{'':>7}  same text, nested JSONL")
    print(f"\n  workbook: {xlsx.name} ({xlsx.stat().st_size//1024} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
