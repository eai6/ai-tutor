"""Turn the concurrency sweep into a students-supported answer.

    python offline_eval/capacity_report.py

WHY THIS EXISTS RATHER THAN THE BENCHMARK REPORTING IT DIRECTLY. The benchmark
times ONE model call. A student's turn costs TWO, because the engine runs
TUTOR_CALL_MODE=two: call 1 picks the tool, the platform grades, call 2 writes
the reply. Reading a single-call latency as a turn halves the apparent cost and
doubles the apparent capacity — the mistake this file exists to not make.

So the two numbers come from the two places that measure them honestly:

  * per-turn latency at N=1 is MEASURED, from the eval boards themselves
    (latency_report.py over 641 real turns for math-27b, etc). No modelling of
    what a turn "should" cost.
  * the slowdown from concurrency comes from the sweep, as a RATIO
    p50(N)/p50(1). A ratio is what the benchmark measures well, and it carries
    over to the two-call turn without assuming how the turn splits.

Then, for N concurrent slots and a student who spends `think` seconds reading
the question and typing an answer:

    turn(N)     = board_turn_p50 x p50(N)/p50(1)
    students(N) = N x (turn(N) + think) / turn(N)

THINK TIME IS AN ASSUMPTION AND IT DOMINATES. It is the difference between a
lab where students race and homework where they wander off. It is reported as
a range, never folded into one number.
"""

# p50 seconds per single call, from the sweep. Keyed by (model, slots) -> {N: p50}.
# Only configurations that passed the sanity gate below are listed: 4b at 16
# slots reserved ~20 GB of KV cache (num_ctx is allocated PER SLOT), pushed the
# 3090 to 22.9/24.5 GB, and made even N=1 slower than production — a measurement
# of a misconfigured server, not of the GPU.
SWEEP = {
    ("qwen3-4b-jetson", 4):        {1: 2.4, 2: 3.3, 4: 3.7},
    ("qwen3-4b-jetson", 8):        {1: 2.5, 4: 5.4, 8: 5.7},
    ("qwen3.8-27b-instruct", 4):   {1: 9.9, 2: 6.9, 4: 10.8},
    ("qwen3.8-27b-instruct", 8):   {1: 10.4, 4: 8.7, 8: 20.5},
}

# Measured per-TURN medians from the eval boards (latency_report.py). These are
# whole turns: both calls, plus the platform's grading in between.
BOARD_TURN = {
    "qwen3-4b-jetson": {"geography": 5.09, "maths": 7.46},
    "qwen3.8-27b-instruct": {"geography": 19.62, "maths": 14.86},
}

# What a student will sit through for one tutor reply before the session stops
# feeling live. Not measured here; stated so it can be argued with.
TOLERABLE_TURN_S = 25.0
THINK_TIMES = (15, 30, 60)


def main() -> int:
    print("SANITY GATE — a single call must be about half a measured turn,")
    print("because a turn is two calls. If it is not, the sweep config is wrong.\n")
    print(f"  {'model':<24}{'call p50':>10}{'x2':>8}{'board turn':>12}{'':>4}")
    for model, subj in BOARD_TURN.items():
        base = min(v[1] for (m, _), v in SWEEP.items() if m == model)
        turn = min(subj.values())
        ok = 0.6 <= (2 * base) / turn <= 1.6
        print(f"  {model:<24}{base:>9.1f}s{2*base:>7.1f}s{turn:>11.1f}s"
              f"    {'ok' if ok else 'MISMATCH'}")

    for (model, slots), pts in sorted(SWEEP.items()):
        print(f"\n{'='*66}\n{model}   {slots} parallel slots\n{'='*66}")
        base = pts[1]
        for subject, board in sorted(BOARD_TURN[model].items()):
            print(f"\n  {subject} (measured turn at N=1: {board:.1f}s)")
            print(f"    {'N':>4}{'slowdown':>11}{'turn':>9}{'':>3}"
                  + "".join(f"{'think ' + str(t) + 's':>12}" for t in THINK_TIMES))
            for n in sorted(pts):
                turn = board * (pts[n] / base)
                live = turn <= TOLERABLE_TURN_S
                cells = "".join(
                    f"{n * (turn + t) / turn:>12.0f}" if live else f"{'-':>12}"
                    for t in THINK_TIMES)
                print(f"    {n:>4}{pts[n]/base:>10.2f}x{turn:>8.1f}s"
                      f"{'' if live else ' !'}{'' if live else ''}{cells}"
                      + ("   over budget" if not live else ""))

    print(f"\n  '-' means the turn exceeds {TOLERABLE_TURN_S:.0f}s and the session "
          f"stops feeling live;\n  adding students there buys throughput by making "
          f"every one of them wait.")
    print("\n  Think time is an ASSUMPTION. Pick the column matching the setting:\n"
          "  a timed lab sits near 15s, ordinary classwork nearer 30s, homework 60s+.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
