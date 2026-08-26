"""Pedagogical analysis of the geography boards, from 169 human grades.

    python offline_eval/pedagogy_report.py

The eight dimensions come from ai_tutor/apps/benchmark/pedagogy.py — the same
module behind the teacher dashboard — so a verdict recorded here answers the
same question, in the same order, under the same pass rule as one recorded in
production. They are imported rather than restated, which is what stops the
two drifting.

SCORING. A session passes only if EVERY applicable dimension sits at its
desideratum: all-or-nothing. `n/a` is excluded from scoring rather than counted
as a failure, and an incomplete grading is excluded entirely — an unanswered
dimension is not a "no", and scoring it as one would invent failures that the
grader never recorded.

The full value set matters, not just pass/fail. `to_some_extent` is a distinct
verdict from `no`, and a dimension that fails mostly at `to_some_extent` is a
different problem from one that fails outright — the first is a tutor being
vague, the second a tutor being wrong. Both are reported.
"""
import collections
import json
import sys

sys.path.insert(0, ".")
from ai_tutor.apps.benchmark import pedagogy as P            # noqa: E402

GRADES = "offline_eval/manual_grades/manual_grades_3runs_169.json"
LABEL = {
    "qwen3-4b-jetson": "qwen3-4B (on-device)",
    "qwen3.8-27b-instruct": "qwen3.8-27B (on-device)",
    "claude-opus-4-7": "Opus 4.7 (cloud)",
    "gemini-3.5-flash": "Gemini 3.5 Flash (cloud)",
    "gpt-5.4-mini": "GPT-5.4-mini (cloud)",
}
ORDER = ["qwen3-4b-jetson", "qwen3.8-27b-instruct",
         "gemini-3.5-flash", "gpt-5.4-mini", "claude-opus-4-7"]


def load():
    g = json.load(open(GRADES))
    dims = g["dimensions"]
    out = collections.defaultdict(list)
    incomplete = 0
    for k, v in g["verdicts"].items():
        if len(v.get("d", {})) != len(dims):
            incomplete += 1
            continue
        out[k.split("|")[1]].append(v)
    return out, dims, incomplete, g


def main():
    by_arm, dims, incomplete, g = load()
    DES = {d.key: d.desideratum for d in P.DIMENSIONS}
    arms = [a for a in ORDER if a in by_arm]

    print("=" * 76)
    print("SESSION PASS RATE — all eight dimensions at their desideratum")
    print("=" * 76)
    print(f"  {'arm':<28}{'graded':>8}{'passed':>8}{'rate':>8}{'peeked':>8}")
    for a in arms:
        v = by_arm[a]
        p = sum(1 for x in v if P.session_passes(x["d"]))
        pk = sum(1 for x in v if x.get("peeked"))
        print(f"  {LABEL[a]:<28}{len(v):>8}{p:>8}{100*p/len(v):>7.0f}%{pk:>8}")
    print(f"\n  {sum(len(v) for v in by_arm.values())} complete gradings, "
          f"{incomplete} incomplete (excluded), exported {g['exported'][:10]}")
    print("  'peeked' = the judge's grade was revealed before grading finished;")
    print("  such a session measures anchoring, not independent judgement.")

    print("\n" + "=" * 76)
    print("FAILURE RATE BY DIMENSION (% of scorable sessions off the desideratum)")
    print("=" * 76)
    hdr = f"  {'dimension':<24}" + "".join(f"{a.split('-')[0][:9]:>11}" for a in arms)
    print(hdr + "\n  " + "-" * (len(hdr) - 2))
    active = []
    for dim in dims:
        cells, any_fail = "", False
        for a in arms:
            vals = [x["d"][dim] for x in by_arm[a]]
            sc = [v for v in vals if v != "n/a"]
            bad = sum(1 for v in sc if v != DES[dim])
            any_fail |= bad > 0
            cells += f"{(100*bad/len(sc) if sc else 0):>10.0f}%"
        print(f"  {dim:<24}{cells}")
        if any_fail:
            active.append(dim)
    print(f"\n  desiderata: " + ", ".join(f"{k}={v}" for k, v in DES.items()))

    print("\n" + "=" * 76)
    print("HOW EACH DIMENSION FAILS — the verdict actually recorded")
    print("=" * 76)
    print("  'to_some_extent' is a tutor being vague; 'no' is a tutor being")
    print("  wrong. Collapsing them into one failure rate hides which.\n")
    for dim in active:
        print(f"  {dim}  (want: {DES[dim]})")
        for a in arms:
            c = collections.Counter(x["d"][dim] for x in by_arm[a])
            off = {k: n for k, n in c.items() if k != DES[dim] and k != "n/a"}
            if not off:
                continue
            detail = ", ".join(f"{k}={n}" for k, n in sorted(off.items()))
            print(f"      {LABEL[a]:<28}{detail}")
        print()

    print("=" * 76)
    print("WHAT SEPARATES THE ARMS")
    print("=" * 76)
    clean = [d for d in dims if d not in active]
    print(f"  Every arm scored 100% on {len(clean)} of 8 dimensions:")
    for d in clean:
        print(f"      {d}")
    print(f"\n  All separation happens on {len(active)}: {', '.join(active)}.")
    print("  The tiers do not differ in general competence — they differ in a")
    print("  small number of specific behaviours, which is what makes the gap")
    print("  addressable by prompt and policy rather than by model scale alone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
