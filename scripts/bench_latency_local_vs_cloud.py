"""Latency benchmark: local Qwen3-4B on the Jetson vs Anthropic Sonnet 5.

Plan and interpretation notes: memory/latency_bench_local_vs_cloud.md


Measures, per call, on a streaming request:
  - TTFT     time to first content token (prefill + queue + network)
  - total    wall time until the stream closes
  - tok/s    output tokens / (total - ttft)   i.e. pure decode rate

Three prompt sizes exercise the prefill axis, which is where an edge GPU and a
datacentre diverge most. Every model sees the identical prompt text.

Usage:
    .venv/bin/python scripts/bench_latency_local_vs_cloud.py --trials 5
    .venv/bin/python scripts/bench_latency_local_vs_cloud.py --local-only
"""

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OLLAMA = os.getenv("OLLAMA_BASE", "http://localhost:11434")

# Production tutoring invariants (CLAUDE.md): TUTORING temperature clamped to
# [0.1, 0.3]; cap output so length variance doesn't dominate the total column.
TEMPERATURE = 0.3
MAX_TOKENS = 400

# The Jetson tag by default. Overridable so the same harness can run on a box
# where that tag was never built — the tag is a thin wrapper over
# qwen3:4b-instruct whose PARAMETERs (num_ctx, temperature, top_p, top_k) are
# either sent explicitly below or identical to the base tag's own defaults, so
# BENCH_LOCAL_MODEL=qwen3:4b-instruct measures the same configuration.
LOCAL_MODEL = os.getenv("BENCH_LOCAL_MODEL", "qwen3-4b-jetson")
LOCAL_NUM_CTX = 16384  # must match the Modelfile, else Ollama evicts + reloads

CLOUD_MODELS = [
    ("claude-sonnet-5", "Sonnet 5"),
]

# ── Prompts ──────────────────────────────────────────────────────────────────
# Shaped like a real 5E tutoring turn but synthetic, so the file is
# self-contained and every model provably sees identical bytes.

BASE_SYSTEM = """You are an AI tutor for secondary school students in Seychelles.
You teach one lesson step at a time using the 5E model (Engage, Explore, Explain,
Elaborate, Evaluate). Keep every reply under 120 words. Ask exactly one question
at the end of each reply. Never give the final answer outright when the student
has not yet attempted the step.

Current lesson: Forces and Motion — Unit 3, Step 4 (Explain).
Learning objective: the student can state Newton's second law and apply F = ma
to a one-dimensional problem with consistent SI units.
"""

LESSON_CONTEXT = """
Prior steps the student has completed:
- Step 1 (Engage): predicted which of two trolleys accelerates faster.
- Step 2 (Explore): measured acceleration for masses 1 kg, 2 kg, 4 kg at fixed force.
- Step 3 (Explore): plotted acceleration against 1/mass and found a straight line.

Known misconceptions for this step:
- Confusing mass with weight; using newtons as a unit of mass.
- Believing a constant force produces a constant velocity rather than a constant
  acceleration.
- Dropping units mid-calculation and reporting a bare number.

Teaching moves available:
- Name the subskill before drilling it ("this is unit conversion, not the physics").
- Use a rung-based complexity ladder: same structure, one new variable per rung.
- A correct bare answer is confirmed with a one-line "because ..." and we advance.
- A wrong bare answer triggers a single ask-for-working as diagnosis, once.
"""

KB_EXCERPT = """
Knowledge base excerpt (retrieved, institution-scoped):

Newton's second law states that the acceleration of an object is directly
proportional to the net force acting on it and inversely proportional to its
mass. Written as an equation, F = ma, where F is the net force in newtons (N),
m is the mass in kilograms (kg), and a is the acceleration in metres per second
squared (m/s^2). One newton is defined as the force required to accelerate a
mass of one kilogram at one metre per second squared, so 1 N = 1 kg m/s^2.

The law applies to the net, or resultant, force. When several forces act on a
body, they must first be combined as vectors. In one dimension this reduces to
choosing a positive direction and adding forces with signs. A trolley pulled
forward by 12 N while friction opposes it with 4 N experiences a net force of
8 N, not 12 N, and it is the 8 N that appears in F = ma.

Mass and weight are distinct quantities and are a persistent source of student
error. Mass is a scalar measure of the quantity of matter in a body, measured in
kilograms, and does not change with location. Weight is the gravitational force
acting on that mass, measured in newtons, and is given by W = mg, where g is the
gravitational field strength, approximately 9.8 N/kg at the Earth's surface. A
student who writes "mass = 60 N" has conflated the two. On the Moon, where g is
about 1.6 N/kg, the same 60 kg student has an unchanged mass of 60 kg but a
weight of roughly 96 N rather than 588 N.

A constant net force produces a constant acceleration, not a constant velocity.
This is counter to everyday experience, where a car travelling at a steady speed
clearly has its engine running. The resolution is that at steady speed the
driving force and the resistive forces are balanced, so the net force is zero
and the acceleration is zero. Velocity is constant precisely because there is no
net force, not because a force is being continuously supplied.

Worked example. A box of mass 5.0 kg rests on a smooth horizontal floor. A
horizontal force of 20 N is applied. The net force is 20 N because the floor is
smooth and the vertical forces balance. Rearranging F = ma gives a = F/m, so
a = 20 N / 5.0 kg = 4.0 m/s^2. Checking the units, N/kg = (kg m/s^2)/kg = m/s^2,
which confirms the result is an acceleration.

Second worked example, with friction. A crate of mass 25 kg is pushed with a
horizontal force of 150 N against a frictional force of 50 N. The net force is
150 - 50 = 100 N. Then a = 100 / 25 = 4.0 m/s^2. Note that the two examples give
the same acceleration from different numbers, which is a useful contrast to
offer a student who is pattern-matching on the applied force alone.

Common assessment items at this level ask the student to rearrange F = ma for
any of the three variables, to identify the net force from a simple free-body
sketch, and to carry units through a calculation. Marks are typically awarded
separately for the rearrangement, the substitution, and the unit, which is why
a bare numerical answer, even a correct one, is worth less than the working.
"""

CONVERSATION = """
Recent conversation:
Tutor: You found that acceleration doubles when mass halves. What does that tell
you about how force, mass and acceleration are related?
Student: that when mass goes down accel goes up
Tutor: Right — they trade off. Newton wrote that as F = ma. If a 2 kg trolley is
pushed with 6 N, what acceleration would you expect?
Student: 3
Tutor: Correct, because 6 divided by 2 is 3 — and the unit is m/s squared. Now a
harder one: a 4 kg box is pushed with 20 N but friction pushes back with 4 N.
What is the acceleration?
"""

STUDENT_TURN = "Student: is it 5"

PROMPTS = {
    "short": (BASE_SYSTEM, STUDENT_TURN),
    "medium": (BASE_SYSTEM + LESSON_CONTEXT, CONVERSATION + STUDENT_TURN),
    "long": (BASE_SYSTEM + LESSON_CONTEXT + KB_EXCERPT, CONVERSATION + STUDENT_TURN),
}


# ── Runners ──────────────────────────────────────────────────────────────────

def run_local(system: str, user: str) -> dict:
    """Streaming /api/chat call. Mirrors the options OllamaClient sends, plus
    num_ctx pinned so the resident runner is reused rather than evicted."""
    t0 = time.perf_counter()
    ttft = None
    out_chars = 0
    final = {}

    resp = requests.post(
        f"{OLLAMA}/api/chat",
        json={
            "model": LOCAL_MODEL,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": True,
            "think": False,  # qwen3 is a thinking model; prod tutoring path is not
            "options": {
                "temperature": TEMPERATURE,
                "num_predict": MAX_TOKENS,
                "num_ctx": LOCAL_NUM_CTX,
            },
        },
        stream=True,
        timeout=300,
    )
    resp.raise_for_status()
    for line in resp.iter_lines():
        if not line:
            continue
        chunk = json.loads(line)
        piece = (chunk.get("message") or {}).get("content") or ""
        if piece and ttft is None:
            ttft = time.perf_counter() - t0
        out_chars += len(piece)
        if chunk.get("done"):
            final = chunk
    total = time.perf_counter() - t0

    return {
        "ttft": ttft if ttft is not None else total,
        "total": total,
        "tokens_in": final.get("prompt_eval_count", 0),
        "tokens_out": final.get("eval_count", 0),
        "chars": out_chars,
        # Ollama's own nanosecond counters, for attributing the TTFT
        "load_s": final.get("load_duration", 0) / 1e9,
        "prefill_s": final.get("prompt_eval_duration", 0) / 1e9,
        "decode_s": final.get("eval_duration", 0) / 1e9,
    }


def run_cloud(client, model: str, system: str, user: str) -> dict:
    t0 = time.perf_counter()
    ttft = None
    out_chars = 0
    tokens_in = tokens_out = 0

    # Sonnet 5 rejects non-default sampling params (400), so no temperature here —
    # the local arm keeps its 0.3. And thinking is ON by default when the field is
    # omitted: left alone, every cloud turn would reason first, which is not the
    # same workload as the local model running think=False.
    with client.messages.stream(
        model=model,
        max_tokens=MAX_TOKENS,
        thinking={"type": "disabled"},
        system=system,
        messages=[{"role": "user", "content": user}],
    ) as stream:
        for text in stream.text_stream:
            if ttft is None:
                ttft = time.perf_counter() - t0
            out_chars += len(text)
        msg = stream.get_final_message()
        tokens_in = msg.usage.input_tokens
        tokens_out = msg.usage.output_tokens
    total = time.perf_counter() - t0

    return {
        "ttft": ttft if ttft is not None else total,
        "total": total,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "chars": out_chars,
        "load_s": 0.0,
        "prefill_s": 0.0,
        "decode_s": 0.0,
    }


# ── Harness ──────────────────────────────────────────────────────────────────

def summarize(runs: list[dict]) -> dict:
    def col(k):
        return [r[k] for r in runs]

    decode_rates = [
        r["tokens_out"] / (r["total"] - r["ttft"])
        for r in runs
        if r["tokens_out"] and (r["total"] - r["ttft"]) > 0.01
    ]
    return {
        "n": len(runs),
        "ttft_med": statistics.median(col("ttft")),
        "ttft_min": min(col("ttft")),
        "ttft_max": max(col("ttft")),
        "total_med": statistics.median(col("total")),
        "total_min": min(col("total")),
        "total_max": max(col("total")),
        "tokens_in": statistics.median(col("tokens_in")),
        "tokens_out": statistics.median(col("tokens_out")),
        "tok_s": statistics.median(decode_rates) if decode_rates else 0.0,
        "prefill_med": statistics.median(col("prefill_s")),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--trials", type=int, default=5)
    ap.add_argument("--local-only", action="store_true")
    ap.add_argument("--cloud-only", action="store_true")
    ap.add_argument("--sizes", default="short,medium,long")
    ap.add_argument("--out", default="offline_eval/latency_local_vs_cloud.json")
    args = ap.parse_args()

    sizes = [s.strip() for s in args.sizes.split(",") if s.strip()]

    targets = []
    if not args.cloud_only:
        targets.append(("local", LOCAL_MODEL, f"Qwen3-4B local ({LOCAL_MODEL}, Q4_K_M)"))
    if not args.local_only:
        import anthropic

        # config/settings.py loads .env; do the same without booting Django
        if not os.getenv("ANTHROPIC_API_KEY"):
            for line in (ROOT / ".env").read_text().splitlines():
                if line.startswith("ANTHROPIC_API_KEY="):
                    os.environ["ANTHROPIC_API_KEY"] = line.split("=", 1)[1].strip()
        client = anthropic.Anthropic()
        for mid, label in CLOUD_MODELS:
            targets.append(("cloud", mid, label))
    else:
        client = None

    results = {}

    for kind, model, label in targets:
        for size in sizes:
            system, user = PROMPTS[size]
            key = f"{label} | {size}"

            # Warmup: pays the model load, the TLS handshake, and any cold
            # routing. Discarded.
            try:
                if kind == "local":
                    run_local(system, user)
                else:
                    run_cloud(client, model, system, user)
            except Exception as exc:
                print(f"  {key}: warmup FAILED — {type(exc).__name__}: {exc}")
                continue

            runs = []
            for i in range(args.trials):
                try:
                    r = run_local(system, user) if kind == "local" else run_cloud(
                        client, model, system, user)
                    runs.append(r)
                    print(f"  {key} [{i+1}/{args.trials}] "
                          f"ttft={r['ttft']:.2f}s total={r['total']:.2f}s "
                          f"out={r['tokens_out']}tok", flush=True)
                except Exception as exc:
                    print(f"  {key} [{i+1}] FAILED — {type(exc).__name__}: {exc}",
                          flush=True)
            if runs:
                results[key] = {"model": model, "label": label, "size": size,
                                "kind": kind, **summarize(runs),
                                "raw": runs}

    # ── Report ───────────────────────────────────────────────────────────
    print("\n" + "=" * 104)
    print(f"{'model':<28} {'size':<7} {'in':>6} {'out':>5} "
          f"{'TTFT med':>9} {'TTFT rng':>14} {'total med':>10} {'tok/s':>7}")
    print("-" * 104)
    for size in sizes:
        for key, s in results.items():
            if s["size"] != size:
                continue
            print(f"{s['label']:<28} {size:<7} {s['tokens_in']:>6.0f} "
                  f"{s['tokens_out']:>5.0f} {s['ttft_med']:>8.2f}s "
                  f"{s['ttft_min']:>6.2f}-{s['ttft_max']:<6.2f} "
                  f"{s['total_med']:>9.2f}s {s['tok_s']:>7.1f}")
        print()

    out_path = ROOT / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(results, indent=2))
    print(f"raw → {out_path}")


if __name__ == "__main__":
    main()
