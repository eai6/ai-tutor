"""Cost of the cloud arms: list prices, and what the geography run actually cost.

    python offline_eval/cost_table.py

Deliberately narrow. Cloud tutoring is metered per token, so its cost is
reported as published unit prices plus the metered spend of a run of known
size. On-device tutoring has no per-token price at all — the marginal cost of
a session is the electricity to produce it — so it is reported as the capital
required, and not forced onto a per-token basis it does not have.

Token counts come from the per-response traces in
multi_turn_results/geo_cloud/trace/. Output tokens are not recorded by the
tracer and are estimated from reply length; they are 6-8% of each bill.
"""
import glob
import json
import os

# Published list prices, USD per million tokens.
PRICES = {
    "claude-opus-4-7":  {"in": 5.00, "out": 25.00},
    "gemini-3.5-flash": {"in": 0.30, "out":  2.50},
    "gpt-5.4-mini":     {"in": 0.25, "out":  2.00},
}
CACHE_READ, CACHE_WRITE = 0.10, 1.25     # multiples of the input rate
CHARS_PER_TOKEN, CALL1_OUT = 4.0, 25
SESSIONS = 34                            # geography board size

GPU_CAPITAL = 2500.0                     # RTX 3090, one-off purchase


def main():
    print("=" * 72)
    print("TABLE 1 — Published unit prices, USD per million tokens")
    print("=" * 72)
    print(f"  {'model':<20}{'input':>10}{'output':>10}{'cache read':>13}{'cache write':>13}")
    for m, p in PRICES.items():
        print(f"  {m:<20}{p['in']:>10.2f}{p['out']:>10.2f}"
              f"{p['in']*CACHE_READ:>13.3f}{p['in']*CACHE_WRITE:>13.3f}")
    print("\n  Cache reads bill at ~10% of the input rate and cache writes at ~125%.")
    print("  The tutor prompt is layered so its static prefix is cached, which is")
    print("  why the read rate matters as much as the headline input rate.")

    print("\n" + "=" * 72)
    print(f"TABLE 2 — Tokens consumed, geography board ({SESSIONS} sessions per arm)")
    print("=" * 72)
    print(f"  {'model':<20}{'fresh in':>12}{'cached in':>12}{'cache wr':>11}{'output':>10}")
    usage = {}
    for f in sorted(glob.glob("offline_eval/multi_turn_results/geo_cloud/trace/*.jsonl")):
        arm = os.path.basename(f)[:-6]
        if arm not in PRICES:
            continue
        rows = [json.loads(l) for l in open(f)]
        u = {
            "fresh":   sum(r.get("tok_in", 0) or 0 for r in rows),
            "cached":  sum(r.get("tok_cached", 0) or 0 for r in rows),
            "written": sum(r.get("tok_write", 0) or 0 for r in rows),
            "out": sum(r.get("text_chars", 0) or len(r.get("reply", ""))
                       for r in rows) / CHARS_PER_TOKEN + CALL1_OUT * len(rows),
            "turns": len(rows),
        }
        usage[arm] = u
        print(f"  {arm:<20}{u['fresh']:>12,}{u['cached']:>12,}"
              f"{u['written']:>11,}{u['out']:>10,.0f}")
    print("\n  The three input buckets are DISJOINT: a provider's input_tokens")
    print("  excludes cached tokens, so the prompt's true size is their sum.")

    print("\n" + "=" * 72)
    print("TABLE 3 — What the run cost")
    print("=" * 72)
    print(f"  {'model':<20}{'billed':>10}{'per session':>13}{'if uncached':>13}{'saved':>9}")
    for arm, u in usage.items():
        p = PRICES[arm]
        billed = (u["fresh"] * p["in"]
                  + u["cached"] * p["in"] * CACHE_READ
                  + u["written"] * p["in"] * CACHE_WRITE
                  + u["out"] * p["out"]) / 1e6
        unc = ((u["fresh"] + u["cached"] + u["written"]) * p["in"]
               + u["out"] * p["out"]) / 1e6
        print(f"  {arm:<20}{billed:>10.2f}{billed/SESSIONS:>13.4f}"
              f"{unc:>13.2f}{100*(1-billed/unc):>8.0f}%")
    print("\n  'per session' is the number that scales: a school pays it again for")
    print("  every session, every year. It does not fall with volume.")

    print("\n" + "=" * 72)
    print("ON-DEVICE — capital, not a per-token price")
    print("=" * 72)
    print(f"  NVIDIA RTX 3090, 24 GB                ${GPU_CAPITAL:,.0f}  one-off")
    print("\n  Both on-device models run on this single card, so the hardware cost")
    print("  is the same for either. There is no per-token charge: once the card")
    print("  is bought, the marginal cost of a session is the electricity to")
    print("  produce it. That is the difference that matters — a metered service")
    print("  is paid for again with every session, a bought card is not.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
