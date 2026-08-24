"""What a cloud arm actually cost, and whether prompt caching is working.

    python offline_eval/cloud_cost.py offline_eval/multi_turn_results/geo_cloud

Reads the per-turn trace and prices it. The point is to catch a COLD cache
during a sweep rather than on the invoice: the tutor prompt is deliberately
layered for caching (static role/rules, then static step content, then the
uncached per-turn tail), and a silent invalidator turns a ~$20 Opus sweep into
a ~$57 one without any error appearing anywhere.

THE THREE BUCKETS ARE DISJOINT. Anthropic's `input_tokens` EXCLUDES cached
tokens; `cache_read_input_tokens` and `cache_creation_input_tokens` are
separate. The prompt's real size is the sum. Treating `input_tokens` as the
total makes reads look larger than the input and prices a run negative — which
is exactly what the first version of this did.
"""
import json
import pathlib
import sys

# $/1M tokens. Cache reads bill at ~0.1x input, writes at ~1.25x.
PRICES = {
    "claude-opus-4-7":  (5.00, 25.00),
    "gemini-3.5-flash": (0.30,  2.50),
    "gpt-5.4-mini":     (0.25,  2.00),
}


def price(name):
    for k, v in PRICES.items():
        if k in name:
            return v
    return None


def main():
    root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    traces = sorted(root.glob("**/trace/*.jsonl"))
    if not traces:
        print(f"no traces under {root}")
        return 1

    print(f"{'arm':<22}{'fresh':>11}{'cached':>11}{'written':>10}"
          f"{'hit%':>7}{'cost':>9}{'uncached':>10}")
    print("-" * 80)
    for t in traces:
        rows = [json.loads(l) for l in open(t)]
        fresh = sum(r.get("tok_in", 0) for r in rows)
        cached = sum(r.get("tok_cached", 0) for r in rows)
        written = sum(r.get("tok_write", 0) for r in rows)
        total = fresh + cached + written
        name = t.stem
        p = price(name)
        if not p or not total:
            print(f"{name:<22}{fresh:>11,}{cached:>11,}{written:>10,}"
                  f"{'':>7}{'  no price' if not p else '  no tokens':>9}")
            continue
        pin, _ = p
        cost = (fresh * pin + cached * pin * 0.1 + written * pin * 1.25) / 1e6
        plain = total * pin / 1e6
        print(f"{name:<22}{fresh:>11,}{cached:>11,}{written:>10,}"
              f"{100*cached/total:>6.0f}%{cost:>9.2f}{plain:>10.2f}")

    print("\n  'uncached' is what the same tokens would cost with caching off.")
    print("  A hit% near zero on a multi-turn arm means the cache is not working:")
    print("  every turn re-sends the same static prefix, so reads should dominate")
    print("  after the first turn of each session.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
