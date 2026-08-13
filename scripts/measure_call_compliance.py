"""Measure Call-1 tool compliance and turn cost on the offline model.

Built as the end-to-end A/B owed by `memory/tool_compliance_root_cause.md`,
and it earned its keep immediately: it refuted the fix it was written to
confirm. Keep it as the standing harness for any future claim about local
tool compliance or turn latency — the lesson it taught is that isolated
Ollama probes do not predict real-turn behaviour, so nothing gets believed
until it has been run here.

ONE CONFIG PER PROCESS, deliberately. Two earlier attempts at a single
multi-config run were killed by memory pressure on the Jetson — a 3.89 GB
model plus three sessions' worth of Django objects does not fit. Run:

    .venv/bin/python scripts/measure_call_compliance.py --config two
    .venv/bin/python scripts/measure_call_compliance.py --config one

then summarise with --summarise. Each run writes its own JSON, so a killed
run costs one config rather than the whole matrix.

The synthetic student is cloud Haiku (`ModelConfig` purpose=STUDENT_SIM), so
it does not compete with the tutor for the Jetson's unified memory. Only the
tutor's own `respond_for_view` wall time is recorded — student latency is
excluded from every number here.

Instrumentation is by wrapping engine functions from the outside rather than
by editing the engine, so the code under measurement is byte-identical to
what ships.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'ai_tutor.config.settings')

# The kiosk's real tutoring model. Set before django.setup() because
# model_profiles and ModelConfig.get_for both read it at call time.
os.environ.setdefault('TUTOR_MODEL_OVERRIDE', 'local_ollama/qwen3-4b-jetson')

import django  # noqa: E402

django.setup()

OUT_DIR = Path(__file__).resolve().parents[1] / 'eval-reports' / 'call_compliance'


# ---------------------------------------------------------------------------
# Configs
# ---------------------------------------------------------------------------
# `call_mode` — TUTOR_CALL_MODE. 'two' is the original design; 'one' is what
#               _call_mode() resolves to for local families.
#
# The `directive` dimension is GONE. It measured `prompts.render_turn_directive`,
# which this script refuted (5-10x slower, no compliance gain) and which was
# then deleted from the engine — so there is no longer a knob to A/B. The
# refuting numbers are in eval-reports/call_compliance/{baseline,directive-two,
# directive-one}.json and memory/tool_compliance_root_cause.md; use
# scripts/probe_tool_loop.py, which needs no engine support, to try further
# variants.
#
# `block_0` — QWEN_BLOCK_0, the deduped Qwen Block-0 prompt
#             (family_prompts.MARKDOWN_BLOCK_0_COMPACT, 13.5k vs 20.5k chars).
#             Added 2026-07-30 to run the A/B that probe_tool_loop.py cannot:
#             the probe replays ONE captured Call 1, so it can measure whether
#             the shortened prompt still emits the tool, but not whether a whole
#             session still teaches. On the probe it was compliance-neutral
#             (8/20 either way) with median Call 1 12.7s -> 7.3s and prompt
#             tokens 7,780 -> 6,087. What it cannot see: the probe showed the
#             failures MOVING rather than going away (it loses the opening POSE
#             turn and gains the turn where the full prompt calls the wrong
#             tool), and a real session chains turns, so a lost pose early can
#             cost every turn after it.
CONFIGS = {
    'one': {'call_mode': 'one'},
    'two': {'call_mode': 'two'},
    # The A/B pair. Same call mode, same everything else — only Block 0 differs.
    'full-two': {'call_mode': 'two', 'block_0': 'full'},
    'compact-two': {'call_mode': 'two', 'block_0': 'compact'},
    # The combination the A/B argues for. `one` was recorded as "mostly inert on
    # the local model" because Call 1 skipped the tool on ~4/5 turns, so the turn
    # fell through to the Call-2 repair anyway. The compact prompt breaks that
    # premise — it emitted the expected tool on 7/7 bare-answer turns — and
    # higher Call-1 compliance is exactly what makes one-call mode pay: it also
    # makes Call 2 EXPENSIVE, because a compliant Call 1 leads to a Call 2 that
    # writes the whole reply (~30 s) instead of a repair that emits no text
    # (~4 s). Test the pair, not just the prompt.
    'compact-one': {'call_mode': 'one', 'block_0': 'compact'},
}


# ---------------------------------------------------------------------------
# Per-turn metric collection
# ---------------------------------------------------------------------------
class Recorder:
    """Collects one dict per tutor turn by wrapping engine internals.

    Turn boundaries come from respond_for_view; everything else keys off
    the turn currently open. `dispatch_seq` is what distinguishes Call 1
    from Call 2 — _dispatch_tools runs once after each.
    """

    def __init__(self) -> None:
        self.turns: list[dict] = []
        self.cur: dict | None = None

    def open_turn(self) -> dict:
        self.cur = {
            'llm_calls': 0,
            'call_secs': [],
            'dispatch_seq': 0,
            'call1_tools': [],
            'call2_tools': [],
            'missing_tool': None,
            'polarity_rewrote': False,
            'verdict': None,
            'ttft': None,
            'streamed': False,
            'wall': None,
            'error': None,
        }
        return self.cur


def install(rec: Recorder):
    """Wrap the engine seams. Returns the patched respond_for_view."""
    from ai_tutor.apps.tutoring.simple_tutor import engine

    real_call_llm = engine._call_llm
    real_dispatch = engine._dispatch_tools
    real_missing = engine._missing_forced_tool
    real_polarity = engine._align_reply_polarity
    real_verdict = engine._turn_verdict
    real_respond = engine.respond_for_view

    def call_llm(**kw):
        c = rec.cur
        t0 = time.time()
        try:
            return real_call_llm(**kw)
        finally:
            if c is not None:
                c['llm_calls'] += 1
                c['call_secs'].append(round(time.time() - t0, 2))

    def dispatch(**kw):
        out = real_dispatch(**kw)
        c = rec.cur
        if c is not None:
            c['dispatch_seq'] += 1
            names = [t.get('tool') for t in (out[1] or [])]
            key = 'call1_tools' if c['dispatch_seq'] == 1 else 'call2_tools'
            c[key] = names
        return out

    def missing(force_pose, force_grade, tool_results):
        out = real_missing(force_pose, force_grade, tool_results)
        if rec.cur is not None:
            rec.cur['missing_tool'] = out
        return out

    def polarity(session, text_reply, tool_results, **kw):
        out = real_polarity(session, text_reply, tool_results, **kw)
        if rec.cur is not None and out != text_reply:
            # Only the batch pass counts. The stream gate calls this per
            # snapshot; those rewrites are the same decision re-applied.
            rec.cur['polarity_rewrote'] = True
        return out

    def verdict(tool_results):
        out = real_verdict(tool_results)
        if rec.cur is not None and out is not None:
            rec.cur['verdict'] = out
        return out

    def respond_for_view(session, user_input, **kw):
        c = rec.open_turn()
        t0 = time.time()

        def on_delta(text: str):
            if c['ttft'] is None:
                c['ttft'] = round(time.time() - t0, 2)
            c['streamed'] = True

        try:
            return real_respond(session, user_input, on_delta=on_delta)
        except Exception as exc:                       # noqa: BLE001
            c['error'] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            c['wall'] = round(time.time() - t0, 2)
            rec.turns.append(c)
            rec.cur = None

    engine._call_llm = call_llm
    engine._dispatch_tools = dispatch
    engine._missing_forced_tool = missing
    engine._align_reply_polarity = polarity
    engine._turn_verdict = verdict
    engine.respond_for_view = respond_for_view
    return real_respond


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
def _mem_available_mb() -> int | None:
    """MemAvailable from /proc/meminfo, or None off Linux."""
    try:
        with open('/proc/meminfo') as fh:
            for line in fh:
                if line.startswith('MemAvailable:'):
                    return int(line.split()[1]) // 1024
    except OSError:
        pass
    return None


def _block_0_chars() -> int:
    """Record which prompt the run ACTUALLY used, not which it asked for.

    A config that sets QWEN_BLOCK_0 proves nothing if the env var stops being
    read; recording the resolved length means the JSON itself shows whether the
    two arms differed.
    """
    from ai_tutor.apps.tutoring.simple_tutor.family_prompts import build_family_block_0
    return len(build_family_block_0('qwen', 'BASE'))


def run_config(name: str, *, lesson: int, turns: int, persona: str) -> dict:
    cfg = CONFIGS[name]
    os.environ['TUTOR_CALL_MODE'] = cfg['call_mode']
    # Set explicitly rather than only when 'compact', so a stale value inherited
    # from the shell cannot silently make the control arm run the variant.
    #
    # The fallback is 'compact' as of 2026-08-05, because that is now the
    # shipped default (family_prompts.build_family_block_0). It used to be
    # 'full', which meant a config with no explicit block_0 — like `two` —
    # silently measured the OLD template and reported blk0=20475 even after the
    # promotion. A harness whose implicit default drifts from production
    # measures history rather than the product.
    os.environ['QWEN_BLOCK_0'] = cfg.get('block_0', 'compact')
    # Streaming on for every arm so it is a constant, not a variable. It is
    # also what the kiosk will run, and it is the only way to get TTFT.
    os.environ['TUTOR_STREAMING'] = '1'

    rec = Recorder()
    install(rec)

    from ai_tutor.apps.tutoring.student_sim.driver import simulate_session

    t0 = time.time()
    sim = simulate_session(lesson_id=lesson, persona=persona, max_turns=turns)
    wall = time.time() - t0

    # The opening start_for_view turn is not a graded exchange and does not
    # go through respond_for_view, so it is absent from rec.turns by
    # construction — no filtering needed.
    return {
        'config': name,
        'call_mode': cfg['call_mode'],
        'block_0': cfg.get('block_0', 'full'),
        'block_0_chars': _block_0_chars(),
        # Recorded because it invalidated a whole arm once (2026-07-30): the
        # second of two back-to-back runs started with 52 MB available and every
        # turn came in 4-5x slower, improving monotonically as the run went on —
        # the signature of memory pressure, not of the prompt under test. Any
        # latency comparison between two runs with very different values here is
        # meaningless. Unload the model between arms and alternate their order.
        'mem_available_mb_at_start': _mem_available_mb(),
        'model': os.environ.get('TUTOR_MODEL_OVERRIDE'),
        'lesson': lesson,
        'persona': persona,
        'sim_reason': sim.reason,
        'sim_error': sim.error,
        'session_id': sim.session_id,
        'total_wall': round(wall, 1),
        'turns': rec.turns,
    }


def summarise(run: dict) -> dict:
    ts = [t for t in run['turns'] if t['error'] is None]
    if not ts:
        return {'config': run['config'], 'n': 0}

    def rate(pred):
        hits = [t for t in ts if pred(t)]
        return f"{len(hits)}/{len(ts)}"

    # A turn is Call-1 compliant when the expected tool was NOT missing.
    # _missing_forced_tool is the engine's own definition, so this measures
    # the thing the repair path keys off rather than a re-derived proxy.
    graded = [t for t in ts if t['verdict'] is not None]

    # Duplicate tool spam. The smoke run caught Call 1 emitting record_answer
    # 31 times in ONE response — _dispatch_tools drops the duplicates, but
    # they are already paid for in decode, and each one becomes a tool_result
    # message that Call 2 then has to prefill. It dominated turn cost, so it
    # is measured rather than assumed constant across arms.
    def dup_max(t):
        names = t['call1_tools'] or []
        return max((names.count(n) for n in set(names)), default=0)

    return {
        'config': run['config'],
        'blk0': run.get('block_0_chars', '-'),
        'memMB': run.get('mem_available_mb_at_start', '-'),
        'n': len(ts),
        'call1_compliant': rate(lambda t: t['missing_tool'] is None),
        'call1_had_tool': rate(lambda t: bool(t['call1_tools'])),
        'median_call1_tools': round(statistics.median(
            len(t['call1_tools'] or []) for t in ts), 1),
        'max_dup_tool': max((dup_max(t) for t in ts), default=0),
        'two_call_turns': rate(lambda t: t['llm_calls'] >= 2),
        'mean_calls': round(statistics.mean(t['llm_calls'] for t in ts), 2),
        'median_wall': round(statistics.median(t['wall'] for t in ts), 1),
        'median_ttft': round(statistics.median(
            [t['ttft'] for t in ts if t['ttft'] is not None] or [0]), 1),
        'streamed': rate(lambda t: t['streamed']),
        'graded_turns': len(graded),
        'polarity_rewrote': (
            f"{sum(1 for t in graded if t['polarity_rewrote'])}/{len(graded)}"
            if graded else '0/0'
        ),
        'sim_reason': run['sim_reason'],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--config', choices=sorted(CONFIGS))
    ap.add_argument('--lesson', type=int, default=1137)
    ap.add_argument('--turns', type=int, default=8)
    ap.add_argument('--persona', default='error_prone')
    ap.add_argument('--summarise', action='store_true',
                    help='Print the table across every JSON already written.')
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    if args.summarise:
        # Only this script's run files. probe_tool_loop.py writes
        # call1_payload.json and tool_loop_probe.json into the same directory
        # and neither has a 'turns' key, so an unfiltered glob raises KeyError
        # on whichever sorts first.
        rows = []
        for p in sorted(OUT_DIR.glob('*.json')):
            try:
                run = json.loads(p.read_text())
            except json.JSONDecodeError:
                continue
            if isinstance(run, dict) and 'turns' in run and 'config' in run:
                rows.append(summarise(run))
        if not rows:
            print('no runs yet')
            return 1
        cols = list(rows[0])
        width = {c: max(len(c), *(len(str(r.get(c, ''))) for r in rows)) for c in cols}
        print('  '.join(c.ljust(width[c]) for c in cols))
        for r in rows:
            print('  '.join(str(r.get(c, '')).ljust(width[c]) for c in cols))
        return 0

    if not args.config:
        ap.error('--config is required unless --summarise')

    print(f"[{args.config}] lesson={args.lesson} persona={args.persona} "
          f"turns={args.turns} model={os.environ.get('TUTOR_MODEL_OVERRIDE')}",
          flush=True)
    run = run_config(args.config, lesson=args.lesson, turns=args.turns,
                     persona=args.persona)
    out = OUT_DIR / f"{args.config}.json"
    out.write_text(json.dumps(run, indent=2))
    print(json.dumps(summarise(run), indent=2), flush=True)
    print(f"wrote {out}", flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
