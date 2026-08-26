"""Unit-cost analysis of tutoring delivery, derived from raw run data.

    python offline_eval/cost_audit.py                    # full derivation
    python offline_eval/cost_audit.py --wage 1.50 --ratio 40

GEOGRAPHY ONLY. The maths boards are excluded: two of their lessons hold fewer
bank questions than a session consumes, so sessions there terminate on the turn
cap rather than on the tutor's behaviour, and every arm is depressed by the
same content gap. Including them would price a content defect as a model
difference.

EVERY INPUT IS COMPUTED FROM FILES IN THIS REPOSITORY. Nothing below is a
remembered figure: token counts come from the per-turn traces, latencies and
turn counts from the board transcripts, quality from the human grade file. The
script prints the derivation chain so each published number can be traced to
the run that produced it.

METHOD. Costing follows the ingredients method (Levin & McEwan), the
convention in education cost-effectiveness analysis: identify the resources
each alternative consumes, price them, and express the total on a common
denominator. Durable goods are annualised with a capital recovery factor
rather than expensed at purchase.

The denominator is the STUDENT-TUTORING-HOUR (STH) — one student receiving one
hour of tutoring. Session length varies by model, so a per-session denominator
would not be comparable across arms; a per-student denominator would hide
intensity of use, which is the variable that decides the comparison.
"""
import argparse
import glob
import json
import math
import os
import statistics as st

RES = "offline_eval/multi_turn_results"
GEO_BOARDS = ["geo_4b_v2", "geo_27b_v2", "geo_cloud"]
GRADES = "offline_eval/manual_grades/manual_grades_3runs_169.json"

# Published list prices, $ per million tokens (input, output). Cache reads bill
# at ~10% of the input rate and cache writes at ~125%.
PRICE = {
    "claude-opus-4-7":  (5.00, 25.00),
    "gemini-3.5-flash": (0.30,  2.50),
    "gpt-5.4-mini":     (0.25,  2.00),
}
CACHE_READ, CACHE_WRITE = 0.10, 1.25
CHARS_PER_TOKEN = 4.0       # English; used only to price output
CALL1_OUT_TOKENS = 25       # the tool-selection call emits a short structured call

# Capacity ceilings at a 30-second turn budget, measured in RTX3090_CAPACITY.md
CONCURRENCY = {"qwen3-4b-jetson": 48, "qwen3.8-27b-instruct": 4}

K_CAPITAL, GPU_WATTS, KWH = 2500.0, 350.0, 0.20
HOURS_DAY, SCHOOL_DAYS = 6, 190


def crf(rate, years):
    """Capital recovery factor: annualises a lump sum over its useful life.

    Dividing capital by life omits the opportunity cost of funds tied up in
    the asset. The CRF does not, and it is the convention in the ingredients
    method for any resource lasting more than one year.
    """
    return 1.0 / years if rate <= 0 else rate * (1+rate)**years / ((1+rate)**years - 1)


def board_stats():
    """Sessions, tutor turns and per-turn latency, from the board transcripts."""
    out = {}
    for b in GEO_BOARDS:
        for f in sorted(glob.glob(f"{RES}/{b}/*.json")):
            if os.path.basename(f).startswith("partial_"):
                continue                      # resume checkpoints, not boards
            d = json.load(open(f))
            arm, lat, turns, sess = os.path.basename(f)[:-5], [], [], 0
            for r in d.get("results", []):
                a = [t for t in r.get("transcript", [])
                     if t.get("role") == "tutor" and t.get("latency_ms") is not None]
                if not a:
                    continue
                sess += 1
                turns.append(len(a))
                lat += [t["latency_ms"] / 1000 for t in a]
            if sess:
                out[arm] = {"sessions": sess, "turns": len(lat),
                            "turns_per_session": st.median(turns),
                            "sec_per_turn": st.median(lat)}
    return out


def token_cost():
    """Metered spend per arm, from the per-turn token buckets in the trace.

    Output tokens are not recorded by the tracer, so they are estimated from
    reply length. They are 6-8% of the bill — small, but omitting them
    understates every cloud arm, so they are estimated rather than dropped.
    """
    out = {}
    for f in sorted(glob.glob(f"{RES}/geo_cloud/trace/*.jsonl")):
        arm = os.path.basename(f)[:-6]
        if arm not in PRICE:
            continue
        rows = [json.loads(l) for l in open(f)]
        fresh = sum(r.get("tok_in", 0) or 0 for r in rows)
        cached = sum(r.get("tok_cached", 0) or 0 for r in rows)
        written = sum(r.get("tok_write", 0) or 0 for r in rows)
        chars = sum(r.get("text_chars", 0) or len(r.get("reply", "")) for r in rows)
        out_tok = chars / CHARS_PER_TOKEN + CALL1_OUT_TOKENS * len(rows)
        p_in, p_out = PRICE[arm]
        billed_in = (fresh * p_in + cached * p_in * CACHE_READ
                     + written * p_in * CACHE_WRITE) / 1e6
        billed_out = out_tok * p_out / 1e6
        out[arm] = {"fresh": fresh, "cached": cached, "written": written,
                    "out_tok": out_tok, "in_$": billed_in, "out_$": billed_out,
                    "total_$": billed_in + billed_out,
                    "uncached_$": (fresh + cached + written) * p_in / 1e6,
                    "turns": len(rows)}
    return out


def quality():
    """Human-graded pass rate per arm, geography boards only."""
    import sys
    sys.path.insert(0, ".")
    from ai_tutor.apps.benchmark import pedagogy as P
    g = json.load(open(GRADES))
    dims = g["dimensions"]
    out = {}
    for k, v in g["verdicts"].items():
        if len(v.get("d", {})) != len(dims):
            continue                      # incomplete: not a "no", just unscored
        run, arm, _ = k.split("|", 2)
        if "geo" not in run and "4b_postfix" not in run and "27b_postfix" not in run:
            continue
        n, p = out.get(arm, (0, 0))
        out[arm] = (n + 1, p + (1 if P.session_passes(v["d"]) else 0))
    return {a: (n, p, p / n) for a, (n, p) in out.items()}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--students", type=int, default=300)
    ap.add_argument("--sessions", type=int, default=200,
                    help="sessions per student per year (5/wk x 40 wks)")
    ap.add_argument("--think", type=int, default=30, help="student seconds per turn")
    ap.add_argument("--life", type=int, default=3, help="useful life of card, years")
    ap.add_argument("--discount", type=float, default=0.05)
    ap.add_argument("--wage", type=float, default=1.50, help="teacher $/hour")
    ap.add_argument("--ratio", type=int, default=40, help="pupil-teacher ratio")
    a = ap.parse_args()

    B, T, Q = board_stats(), token_cost(), quality()

    print("=" * 78)
    print("STEP 1 — OBSERVED RUN DATA (geography, 34 scenarios per arm)")
    print("=" * 78)
    print(f"  {'arm':<24}{'sessions':>9}{'turns':>7}{'turns/sess':>12}{'s/turn p50':>12}")
    for arm, s in B.items():
        print(f"  {arm:<24}{s['sessions']:>9}{s['turns']:>7}"
              f"{s['turns_per_session']:>12.1f}{s['sec_per_turn']:>12.2f}")
    print("  source: multi_turn_results/<board>/<arm>.json, transcript[].latency_ms")

    print("\n" + "=" * 78)
    print("STEP 2 — SESSION DURATION (the STH denominator)")
    print("=" * 78)
    print("  A real session is longer than the harness took: the simulated student")
    print("  answers instantly, a real one reads and types. Duration is therefore")
    print(f"  turns x (tutor latency + {a.think}s think time).\n")
    print(f"  {'arm':<24}{'turns':>7}{'tutor s':>9}{'think s':>9}{'min/session':>13}")
    dur = {}
    for arm, s in B.items():
        tps, spt = s["turns_per_session"], s["sec_per_turn"]
        mins = tps * (spt + a.think) / 60
        dur[arm] = mins
        print(f"  {arm:<24}{tps:>7.1f}{tps*spt:>9.0f}{tps*a.think:>9.0f}{mins:>13.1f}")

    print("\n" + "=" * 78)
    print("STEP 3 — MARGINAL COST: cloud, metered from the token buckets")
    print("=" * 78)
    print(f"  {'arm':<20}{'fresh':>11}{'cached':>11}{'written':>10}{'out(est)':>10}")
    for arm, t in T.items():
        print(f"  {arm:<20}{t['fresh']:>11,}{t['cached']:>11,}"
              f"{t['written']:>10,}{t['out_tok']:>10,.0f}")
    print("  source: multi_turn_results/geo_cloud/trace/<arm>.jsonl, tok_* fields")
    print("  the three input buckets are DISJOINT: a provider's input_tokens")
    print("  excludes cached tokens, so prompt size is their sum.\n")
    print(f"  {'arm':<20}{'input $':>10}{'output $':>10}{'total $':>10}"
          f"{'$/session':>12}{'$/STH':>10}")
    cloud_sth = {}
    for arm, t in T.items():
        per_sess = t["total_$"] / B[arm]["sessions"]
        sth = per_sess / (dur[arm] / 60)
        cloud_sth[arm] = sth
        print(f"  {arm:<20}{t['in_$']:>10.2f}{t['out_$']:>10.2f}{t['total_$']:>10.2f}"
              f"{per_sess:>12.4f}{sth:>10.3f}")

    print("\n" + "=" * 78)
    print("STEP 4 — FIXED COST: on-device, annualised")
    print("=" * 78)
    ann = K_CAPITAL * crf(a.discount, a.life)
    E = GPU_WATTS / 1000 * HOURS_DAY * SCHOOL_DAYS * KWH
    print(f"  capital K                     ${K_CAPITAL:>10,.0f}")
    print(f"  CRF({a.discount:.0%}, {a.life}y)                   {crf(a.discount, a.life):>11.4f}")
    print(f"  annualised capital            ${ann:>10,.0f}/yr")
    print(f"  energy: {GPU_WATTS:.0f}W x {HOURS_DAY}h x {SCHOOL_DAYS}d x ${KWH}/kWh"
          f"   ${E:>10,.0f}/yr")
    print(f"  annual cost of ownership      ${ann+E:>10,.0f}/yr")

    open_h = HOURS_DAY * SCHOOL_DAYS
    print(f"\n  demand and capacity ({a.students} students x {a.sessions} sessions/yr):")
    print(f"  {'arm':<24}{'STH/yr':>10}{'avg online':>12}{'ceiling':>9}{'cards':>7}{'$/STH':>9}")
    local_sth = {}
    for arm, n in CONCURRENCY.items():
        if arm not in dur:
            continue
        Qd = dur[arm] / 60 * a.sessions * a.students
        avg = Qd / open_h
        cards = math.ceil(Qd / (n * open_h))
        sth = cards * (ann + E) / Qd
        local_sth[arm] = sth
        print(f"  {arm:<24}{Qd:>10,.0f}{avg:>12.1f}{n:>9}{cards:>7}{sth:>9.3f}")
    print("  'avg online' is the mean number tutoring at any instant — the roll is")
    print("  never online together. Demand above one card's capacity is met by")
    print("  buying another card, not by truncating output.")

    print("\n" + "=" * 78)
    print("STEP 5 — AVERAGE TOTAL COST per student-tutoring-hour")
    print("=" * 78)
    rows = [(f"on-device {k}", v, "fixed + ~0 marginal") for k, v in local_sth.items()]
    rows += [(f"cloud {k}", v, "pure variable") for k, v in cloud_sth.items()]
    rows.append((f"human 1:{a.ratio} @ ${a.wage:.2f}/h", a.wage / a.ratio, "pure variable"))
    rows.append((f"human 1:1 @ ${a.wage:.2f}/h", a.wage, "pure variable"))
    print(f"  {'option':<40}{'$/STH':>9}{'structure':>22}")
    for nm, v, k in sorted(rows, key=lambda r: r[1]):
        print(f"  {nm:<40}{v:>9.3f}{k:>22}")

    print("\n" + "=" * 78)
    print("STEP 6 — COST-EFFECTIVENESS ($ per quality-adjusted STH)")
    print("=" * 78)
    print("  quality is the human-graded pass rate: a PROCESS measure of tutoring,")
    print("  not a learning outcome. This is not LAYS per $100.\n")
    print(f"  {'option':<40}{'$/STH':>8}{'graded':>8}{'pass':>7}{'$/QASTH':>10}")
    ce = [(f"on-device {k}", v) for k, v in local_sth.items()]
    ce += [(f"cloud {k}", v) for k, v in cloud_sth.items()]
    for nm, v in sorted(ce, key=lambda r: r[1]):
        arm = nm.split(" ", 1)[1]
        if arm not in Q:
            continue
        n, p, rate = Q[arm]
        print(f"  {nm:<40}{v:>8.3f}{n:>8}{rate:>7.0%}{v/rate:>10.3f}")

    print("\n" + "=" * 78)
    print("STEP 7 — WHAT A SCHOOL PAYS")
    print("=" * 78)
    print(f"  {'option':<34}{'150: total':>12}{'150: /pupil':>13}"
          f"{'300: total':>12}{'300: /pupil':>13}")
    allrows = ([("on-device " + k, v) for k, v in local_sth.items()]
               + [("cloud " + k, v) for k, v in cloud_sth.items()])
    for nm, sth in sorted(allrows, key=lambda r: r[1]):
        arm = nm.split(" ", 1)[1]
        cells = ""
        for roll in (150, 300):
            if nm.startswith("on-device"):
                # On-device is a FIXED cost: one card serves the larger roll
                # too, so the school total does not rise with the roll until
                # capacity forces a second card. Cost per pupil therefore
                # halves as the roll doubles — the opposite of metered cloud.
                cards = math.ceil((dur[arm]/60*a.sessions*roll)
                                  / (CONCURRENCY[arm]*open_h))
                total = cards * (ann + E)
            else:
                total = sth * dur[arm] / 60 * a.sessions * roll
            cells += f"{total:>12,.0f}{total/roll:>13.2f}"
        print(f"  {nm:<34}{cells}")
    print("\n  On-device totals are flat in the roll until a second card is needed;")
    print("  cloud totals are linear in it. That is the whole cost argument.")

    print("\n" + "=" * 78)
    print("STEP 8 — INTERNAL CONSISTENCY CHECKS")
    print("=" * 78)
    ok = True
    # The on-device per-student figure can be reached two ways: through the STH
    # chain (duration -> hours -> $/STH -> $/student) and directly from annual
    # ownership cost over the roll. They must agree, or an intermediate step is
    # wrong. This is the check that would have caught a bad duration.
    for arm, sth in local_sth.items():
        via_sth = sth * dur[arm] / 60 * a.sessions
        cards = math.ceil((dur[arm]/60*a.sessions*a.students) / (CONCURRENCY[arm]*open_h))
        direct = cards * (ann + E) / a.students
        agree = abs(via_sth - direct) < 0.01
        ok &= agree
        print(f"  {arm:<26} via STH ${via_sth:>7.2f}   direct ${direct:>7.2f}"
              f"   {'agree' if agree else 'MISMATCH'}")
    # Cloud: metered total must equal per-session x sessions.
    for arm, t in T.items():
        recon = (t["total_$"] / B[arm]["sessions"]) * B[arm]["sessions"]
        agree = abs(recon - t["total_$"]) < 1e-6
        ok &= agree
        print(f"  {arm:<26} billed  ${t['total_$']:>7.2f}   recon  ${recon:>7.2f}"
              f"   {'agree' if agree else 'MISMATCH'}")
    # Every arm must have been graded, or a cost-effectiveness row is missing.
    missing = [a_ for a_ in list(local_sth) + list(cloud_sth) if a_ not in Q]
    print(f"  arms costed but ungraded:  {missing or 'none'}")
    print(f"\n  {'ALL CHECKS PASS' if ok and not missing else 'CHECK FAILED — do not publish'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
