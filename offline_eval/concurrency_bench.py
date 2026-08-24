"""How many students can one GPU tutor at once?

    python offline_eval/concurrency_bench.py --model qwen3-4b-jetson
    python offline_eval/concurrency_bench.py --model qwen3.8-27b-instruct --levels 1,2,4

Run it ON the box. Pure standard library.

WHAT IT MEASURES, and why that is not "concurrent students". The benchmark
fires N simultaneous generations and reports how per-request latency degrades
with N. That is a SERVER capacity number. A classroom number is different,
because a student is not generating continuously: they read the question,
think, and type. One busy slot therefore serves several students.

    students ≈ slots × (turn_latency + think_time) / turn_latency

so the think time you assume drives the answer as much as the hardware does.
The script reports the server number and applies a stated think-time range —
it does not hide the assumption inside one figure.

THE PROMPT IS SIZED FROM THE REAL BOARDS. Measured on the geography 27b arm:
584 calls, median 4,916 input tokens, ~90 output. A toy 20-token prompt would
overstate capacity badly, because prefill dominates this workload — the tutor
re-sends its system prompt, question pool and recent turns every turn.

TWO CEILINGS, and they bind differently per model:
  * VRAM — each parallel slot needs its own KV cache. At num_ctx 32768 the 27b
    needs ~3 GB per slot on top of 17 GB of weights, so a 24 GB card holds very
    few. The 4b (2.5 GB, num_ctx 16384) has far more room.
  * Compute — once slots are saturated, added concurrency buys throughput at
    the cost of per-request latency. The point where median latency crosses
    what a student will sit through is the real limit.
"""
import argparse
import json
import statistics
import sys
import threading
import time
import urllib.error
import urllib.request

# ~5k tokens of lesson-shaped filler. Sized to the measured median prompt
# (4,916 input tokens); the exact words do not matter, the token count does.
_PARA = (
    "Weathering is the breakdown of rock in place, without transport. "
    "Physical weathering fractures rock mechanically; chemical weathering "
    "alters its minerals. In the Seychelles the granite bedrock shows "
    "exfoliation, where curved sheets peel from the surface as confining "
    "pressure is released. Mass movement is the downslope motion of material "
    "under gravity, ranging from imperceptible creep to sudden rockfall. "
)


def build_prompt(target_tokens: int = 4900) -> str:
    # English runs ~1.3 tokens per WORD, so ~0.75 words per token. Getting this
    # backwards (dividing by 0.75) builds a prompt 2.4x too large and makes the
    # capacity look far worse than it is. The script prints the model's own
    # prompt_eval_count so the assumption is checked against reality, not
    # trusted.
    words_needed = int(target_tokens * 0.75)
    out, n = [], 0
    while n < words_needed:
        out.append(_PARA)
        n += len(_PARA.split())
    return " ".join(out)


# A turn is call 1 (pick a tool - a SHORT structured emission) then call 2
# (write the reply, ~90 tokens). Modelling a turn as two 90-token calls
# over-counts call 1 and inflates the turn, badly for a slow model where call
# 1's short output is a large share of a long call. Checked against the boards,
# doubling over-stated the 27B turn by 1.39x and under-stated the 4B by 0.55x —
# wrong in both directions, so TURN MODE issues the real pair instead.
CALL1_TOKENS = 24


def _chat(host, model, prompt, num_predict, question):
    body = {
        "model": model,
        "stream": False,
        "messages": [{"role": "user", "content": prompt + question}],
        "options": {"num_predict": num_predict},
    }
    req = urllib.request.Request(
        f"{host}/api/chat", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=900) as r:
        return json.loads(r.read())


def one_call(host: str, model: str, prompt: str, num_predict: int, out: list,
             idx: int, turn_mode: bool = False):
    t0 = time.perf_counter()
    try:
        if turn_mode:
            a = _chat(host, model, prompt, CALL1_TOKENS,
                      "\n\nWhich tool should be called next? Reply with the tool name only.")
            b = _chat(host, model, prompt,
                      num_predict, "\n\nIn one sentence, what is exfoliation?")
            tin = a.get("prompt_eval_count", 0) + b.get("prompt_eval_count", 0)
            tout = a.get("eval_count", 0) + b.get("eval_count", 0)
        else:
            b = _chat(host, model, prompt, num_predict,
                      "\n\nIn one sentence, what is exfoliation?")
            tin, tout = b.get("prompt_eval_count", 0), b.get("eval_count", 0)
        out[idx] = {"ok": True, "secs": time.perf_counter() - t0,
                    "in": tin, "out": tout}
    except Exception as exc:                                  # noqa: BLE001
        out[idx] = {"ok": False, "secs": time.perf_counter() - t0,
                    "err": f"{type(exc).__name__}: {str(exc)[:80]}"}


def _one_round(host: str, model: str, prompt: str, n: int, num_predict: int,
               turn_mode: bool = False) -> list:
    results = [None] * n
    threads = [threading.Thread(target=one_call,
                                args=(host, model, prompt, num_predict, results, i,
                                      turn_mode))
               for i in range(n)]
    t0 = time.perf_counter()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for r in results:
        if r:
            r["wall"] = time.perf_counter() - t0
    return [r for r in results if r]


def run_level(host: str, model: str, prompt: str, n: int, num_predict: int,
              repeat: int = 3, turn_mode: bool = False) -> dict:
    """Run the level `repeat` times and pool the rounds, dropping the first.

    Single-shot levels were not reproducible. One sweep timed N=8 at 13.3s and
    N=12 at 4.9s on the same server — latency cannot fall as load rises, so
    that pair was noise, not capacity. The cause is per-slot warm-up: a slot
    that has never served a request pays allocation and cache costs the next
    request does not. The first round absorbs that and is discarded; the rest
    are pooled, so p50 comes from `repeat-1` x N observations rather than one.
    """
    rounds = [_one_round(host, model, prompt, n, num_predict, turn_mode)
              for _ in range(max(repeat, 2))]
    warm = [r for rnd in rounds[1:] for r in rnd]

    ok = [r for r in warm if r.get("ok")]
    bad = [r for r in warm if not r.get("ok")]
    if not ok:
        return {"n": n, "ok": 0, "failed": len(bad),
                "err": bad[0].get("err") if bad else "all failed"}
    secs = sorted(r["secs"] for r in ok)
    # Throughput is per round, so average the rounds rather than dividing the
    # pooled token count by one round's wall clock.
    tps = [sum(r["out"] for r in rnd if r.get("ok")) / max(
        (r["wall"] for r in rnd if r.get("ok")), default=1)
        for rnd in rounds[1:]]
    return {
        "n": n, "ok": len(ok), "failed": len(bad),
        "p50": statistics.median(secs),
        "p95": secs[min(int(0.95 * (len(secs) - 1)), len(secs) - 1)],
        "max": max(secs),
        "wall": statistics.mean([r["wall"] for r in ok]),
        "tok_s": statistics.mean(tps) if tps else 0,
        "in_tokens": statistics.median([r["in"] for r in ok]),
        "samples": len(ok),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--host", default="http://127.0.0.1:11434")
    ap.add_argument("--levels", default="1,2,4,8,16")
    ap.add_argument("--num-predict", type=int, default=90,
                    help="measured median tutor reply is ~90 tokens")
    ap.add_argument("--turn-mode", action="store_true",
                    help="time a whole TUTOR TURN (short call 1 + full call 2), "
                         "not a single call")
    ap.add_argument("--repeat", type=int, default=3,
                    help="rounds per level; the first is discarded as warm-up")
    ap.add_argument("--acceptable", type=float, default=20.0,
                    help="seconds a student will wait for one tutor turn")
    args = ap.parse_args()

    prompt = build_prompt()
    levels = [int(x) for x in args.levels.split(",") if x.strip()]

    print(f"model      {args.model}")
    print(f"prompt     ~{len(prompt.split())} words")
    print(f"reply cap  {args.num_predict} tokens")
    print(f"timing     {'WHOLE TURN (call1+call2)' if args.turn_mode else 'one call'}\n")
    print(f"{'N':>3}{'ok':>5}{'p50':>9}{'p95':>9}{'max':>9}{'tok/s':>9}{'obs':>6}")
    print("-" * 56)

    rows = []
    for n in levels:
        r = run_level(args.host, args.model, prompt, n, args.num_predict, args.repeat,
                      args.turn_mode)
        rows.append(r)
        if not r.get("p50"):
            print(f"{n:>3}{r['ok']:>5}   FAILED: {r.get('err')}")
            break
        print(f"{n:>3}{r['ok']:>5}{r['p50']:>8.1f}s{r['p95']:>8.1f}s"
              f"{r['max']:>8.1f}s{r['tok_s']:>9.1f}{r.get('samples',0):>6}")

    good = [r for r in rows if r.get("p50")]
    if not good:
        return 1

    print(f"\nmeasured prompt: {good[0]['in_tokens']:.0f} input tokens "
          f"(real boards: 4,916 median)")

    # The server number, then the classroom number — with the assumption named.
    usable = [r for r in good if r["p50"] <= args.acceptable]
    slots = max((r["n"] for r in usable), default=0)
    print(f"\nSERVER: largest N holding p50 <= {args.acceptable:.0f}s is {slots or '<1'}")
    if slots:
        base = good[0]["p50"]
        print(f"\nCLASSROOM (students = slots x (latency + think) / latency):")
        print(f"  {'think time':>12}{'students':>12}")
        for think in (15, 30, 60):
            lat = next(r["p50"] for r in good if r["n"] == slots)
            print(f"  {think:>10}s{slots * (lat + think) / lat:>12.0f}")
        print(f"\n  Think time is an ASSUMPTION, not a measurement. A 40-minute "
              f"lab\n  session and a homework setting differ enough to change "
              f"the answer\n  several-fold — pick the one that matches the "
              f"deployment.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
