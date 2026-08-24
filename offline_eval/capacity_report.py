"""Turn the concurrency sweep into a students-supported answer.

    python offline_eval/capacity_report.py offline_eval/sweep_rtx3090.log

WHY THE BENCHMARK DOES NOT REPORT THIS ITSELF. The benchmark times ONE model
call. A student's turn costs TWO, because the engine runs TUTOR_CALL_MODE=two:
call 1 picks the tool, the platform grades, call 2 writes the reply. Reading a
single-call latency as a turn halves the apparent cost and doubles the apparent
capacity — the mistake this file exists to not make.

    turn(N) = 2 x call(N)

That doubling is CHECKED, not assumed, against the eval boards' own measured
per-turn medians (latency_report.py, 641 real turns for math-27b). The check
prints, and it prints whether it passed.

An earlier version scaled a board latency by the ratio call(N)/call(1). That
needs an N=1 level in the sweep, and silently used the smallest N present as
the baseline when there wasn't one — inflating every ratio. Doubling needs no
baseline and cannot fail that way.

THE THREE NUMBERS ARE DIFFERENT AND ARE REPORTED SEPARATELY:
  * slots    — how many requests the GPU batches at once. A server setting,
               bounded by VRAM, since num_ctx is allocated PER SLOT.
  * N        — how many students are waiting at the same instant. Requests
               beyond the slot count QUEUE rather than fail, so N can exceed
               slots. This is the measured, assumption-free capacity number.
  * students — how many can be in a lesson at once. Larger than N, because a
               student spends most of a turn reading and typing, using no GPU:
                   students = N x (turn + think) / turn
               It inherits the think-time ASSUMPTION, so it is a range.
"""
import re
import sys

# Measured per-TURN medians from the eval boards (latency_report.py). Whole
# turns: both calls, plus the platform's grading in between.
BOARD_TURN = {
    "qwen3-4b-jetson": {"geography": 5.09, "maths": 7.46},
    "qwen3.8-27b-instruct": {"geography": 19.62, "maths": 14.86},
}
# Same weights, smaller num_ctx — the boards' latency still applies.
ALIASES = {"qwen3-4b-ctx8k": "qwen3-4b-jetson"}

# The turn budget is a POLICY choice, not a measurement, so it is an argument
# rather than a constant. Tightening it does not just relabel rows: it can move
# the crossing below every level a sweep tested, in which case the honest answer
# is "not measured here" and the sweep needs extending downward.
DEFAULT_TURN_BUDGET_S = 60.0
THINK_TIMES = (15, 30, 60)

_HDR = re.compile(r"^#+ (\S+) slots=(\d+)")
_ROW = re.compile(r"^\s*(\d+)\s+\d+\s+([\d.]+)s\s+([\d.]+)s")


def parse_sweep(path):
    out, key = {}, None
    for line in open(path):
        h = _HDR.match(line)
        if h:
            # setdefault, NOT assignment: the same (model, slots) arm is often
            # swept twice across separate logs to extend the N range, and
            # resetting here silently discarded every level from the earlier
            # run — leaving a table that looked complete and had lost its
            # low-N rows, which are the ones under budget.
            key = (h.group(1), int(h.group(2)))
            out.setdefault(key, {})
            continue
        r = _ROW.match(line)
        if r and key:
            out[key][int(r.group(1))] = (float(r.group(2)), float(r.group(3)))
    return {k: v for k, v in out.items() if v}


def main():
    args = sys.argv[1:]
    budget = DEFAULT_TURN_BUDGET_S
    # --turn-mode: the log already holds whole TURNS (call1+call2 timed
    # together), so the x2 that converts a single call into a turn must NOT be
    # applied on top. Doubling a turn-mode log would report every figure at
    # twice its true cost.
    turn_log = False
    while args and args[0].startswith("--"):
        a = args.pop(0)
        if a.startswith("--budget="):
            budget = float(a.split("=", 1)[1])
        elif a == "--turn-mode":
            turn_log = True
    paths = args or ["offline_eval/sweep_rtx3090.log"]
    sweep = {}
    for p in paths:
        sweep.update(parse_sweep(p))
    if not sweep:
        print(f"no sweep levels parsed from {paths}")
        return 1
    mult = 1 if turn_log else 2
    print(f"sweep: {', '.join(paths)}\nturn budget: {budget:.0f}s")
    print(f"source: {'whole turns, measured' if turn_log else 'single calls, x2'}\n")

    print("CHECK — turn = 2 x call, against the boards' measured turns.")
    for (model, slots), pts in sorted(sweep.items()):
        base = BOARD_TURN[ALIASES.get(model, model)]
        if 1 not in pts:
            print(f"  {model:<24} no N=1 level in this sweep — check skipped")
            continue
        est, real = mult * pts[1][0], min(base.values())
        print(f"  {model:<24} {mult}x{pts[1][0]:.1f}s = {est:.1f}s vs board {real:.1f}s"
              f"   {'ok' if 0.6 <= est / real <= 1.6 else 'MISMATCH'}")

    for (model, slots), pts in sorted(sweep.items()):
        print(f"\n{'='*74}\n{model}   {slots} parallel slots\n{'='*74}")
        print(f"  {'N':>5}{'turn p50':>11}{'turn p95':>11}   "
              + "".join(f"{'think ' + str(t) + 's':>11}" for t in THINK_TIMES))
        for n in sorted(pts):
            p50, p95 = pts[n]
            t50, t95 = mult * p50, mult * p95
            live = t50 <= budget
            cells = "".join(f"{n * (t50 + t) / t50:>11.0f}" if live else f"{'-':>11}"
                            for t in THINK_TIMES)
            flag = "" if t95 <= budget else "   tail over budget"
            print(f"  {n:>5}{t50:>10.1f}s{t95:>10.1f}s   {cells}{flag}")

        ok50 = [n for n, (a, _) in pts.items() if mult * a <= budget]
        ok95 = [n for n, (_, b) in pts.items() if mult * b <= budget]
        print(f"\n  max concurrent students, median turn under "
              f"{budget:.0f}s: {max(ok50, default=0)}")
        print(f"  max concurrent students, 95th pct turn under "
              f"{budget:.0f}s: {max(ok95, default=0)}")

    print(f"\n  '-' = median turn over {budget:.0f}s; adding students there "
          f"buys throughput\n      by making every one of them wait longer.")
    print("\n  Think-time columns are an ASSUMPTION, not a measurement: a timed lab\n"
          "  sits near 15s, ordinary classwork nearer 30s, homework 60s+.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
