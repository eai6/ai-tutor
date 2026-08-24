"""Answer questions about a run from its turn trace.

    TUTOR_TRACE_DIR=/path/to/trace ./venv/bin/python offline_eval/trace_report.py
    ... trace_report.py --grep explanation     # turns whose tool results mention it
    ... trace_report.py --session 1191         # every turn of one session

Each question below is one that cost real time during the 2026-08-23 eval and
was answered by grepping a 45k-line console log, or could not be answered at
all. Pure standard library.
"""
import argparse
import collections
import glob
import json
import os
import statistics
import sys


def load(path):
    rows = []
    with open(path, encoding='utf-8') as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def summarise(rows, trace_dir=None):
    print(f'{len(rows)} turns traced\n')

    # "Which Ollama did this run actually talk to?" — the question a dead
    # tunnel made expensive, because the answer looked normal either way.
    hosts = collections.Counter(r.get('api_base') or '(default)' for r in rows)
    print('hosts contacted:')
    for host, n in hosts.most_common():
        print(f'   {n:5}  {host}')
    if len(hosts) > 1:
        print('   ^^ MORE THAN ONE HOST — a run should reach exactly one')

    models = collections.Counter(r.get('model') or '?' for r in rows)
    print('\nmodels:')
    for m, n in models.most_common():
        print(f'   {n:5}  {m}')

    surfaces = collections.Counter(r.get('answer_mode') or '?' for r in rows)
    print('\nanswer surface (measured per turn, not inferred):')
    for s, n in surfaces.most_common():
        print(f'   {n:5}  {s}  ({100*n/len(rows):.0f}%)')

    # "Did the turn fail?" — two placeholders in a row is what the simulator
    # scores as a deadlock, so this is the leading indicator for one.
    ph = [r for r in rows if r.get('placeholder')]
    print(f'\nfailure placeholders served: {len(ph)} '
          f'({100*len(ph)/max(len(rows),1):.1f}% of turns)')
    if ph:
        errs = collections.Counter(r.get('last_error') or '(none recorded)'
                                   for r in ph)
        for e, n in errs.most_common(5):
            print(f'   {n:5}  after {e}')
        calls = collections.Counter(r.get('failed_call') or 'end-of-turn'
                                    for r in ph)
        print('   gave up at:', dict(calls))

    # Cross-check the trace against the run JSONs sitting beside it. A trace
    # that under-counts is worse than none: on 2026-08-24 this report said
    # "0 placeholders" for a run whose transcripts were full of them, because
    # failed turns returned before the emit. Silence must not read as health.
    # The board JSONs sit one level up from the trace dir.
    root = os.path.dirname(os.path.abspath(trace_dir)) if trace_dir else None
    boards = ([f for f in glob.glob(os.path.join(root, '*.json'))
               if 'partial_' not in os.path.basename(f)] if root else [])
    seen = set()
    for b in boards:
        try:
            data = json.load(open(b))
        except Exception:
            continue
        for res in data.get('results', []):
            for t in res.get('transcript', []):
                if t.get('role') == 'tutor' and 'had trouble responding' in (
                        t.get('content') or ''):
                    seen.add((res.get('scenario_id'), t.get('turn_number')))
    if seen and len(seen) > len(ph):
        print(f'\n   !! TRACE UNDER-COUNTS: the transcripts contain '
              f'{len(seen)} placeholder turn(s) but the trace recorded '
              f'{len(ph)}. Turns are failing without being traced.')

    # "Was that slow turn slow, or was it retried?"
    retried = [r for r in rows if (r.get('retries') or 0) > 0]
    print(f'\nturns that retried: {len(retried)}')
    if retried:
        errs = collections.Counter(r.get('last_error') or '?' for r in retried)
        for e, n in errs.most_common(5):
            print(f'   {n:5}  {e}')

    verdicts = collections.Counter(r.get('verdict') or '(no grade)' for r in rows)
    print('\nverdicts:')
    for v, n in verdicts.most_common():
        print(f'   {n:5}  {v}')

    tools = collections.Counter(t for r in rows for t in (r.get('tools') or []))
    print('\ntools called:')
    for t, n in tools.most_common():
        print(f'   {n:5}  {t}')

    chars = [r.get('text_chars') or 0 for r in rows]
    if chars:
        print(f'\nreply length: median={statistics.median(chars):.0f} '
              f'min={min(chars)} max={max(chars)}')
        if min(chars) == 0:
            print('   ^^ a zero-length reply means the model produced no text')


def grep(rows, needle):
    """Did the model actually SEE this string? The question that could not be
    answered at all before, because tool-result bodies were never logged."""
    # Search what was SENT to the model, not the platform's result dict. The
    # bank's <explanation> is injected into the call-2 string and never appears
    # in the dict, so searching the dict answers a different question — the
    # mistake that made "0/7 turns" look like an answer when it was not.
    def _blob(r):
        return json.dumps({'sent': r.get('call2_sent') or [],
                           'results': r.get('tool_results') or []}, default=str)

    hits = 0
    for r in rows:
        if needle.lower() in _blob(r).lower():
            hits += 1
    print(f'"{needle}" appears in the tool results of {hits}/{len(rows)} turns')
    if hits:
        for r in rows:
            blob = _blob(r)
            if needle.lower() in blob.lower():
                i = blob.lower().index(needle.lower())
                print(f'   session {r.get("session_id")}: '
                      f'…{blob[max(0, i-60):i+110]}…')
                break


def session(rows, sid):
    mine = [r for r in rows if str(r.get('session_id')) == str(sid)]
    print(f'session {sid}: {len(mine)} turns\n')
    for i, r in enumerate(mine, 1):
        flag = ' [PLACEHOLDER]' if r.get('placeholder') else ''
        retry = f' retries={r["retries"]}' if r.get('retries') else ''
        print(f'  {i:3}. verdict={str(r.get("verdict")):9} '
              f'tools={r.get("tools")} chars={r.get("text_chars")}'
              f'{retry}{flag}')
        print(f'       {(r.get("reply") or "")[:110]}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dir', default=os.environ.get('TUTOR_TRACE_DIR'))
    ap.add_argument('--name', default=os.environ.get('TUTOR_TRACE_NAME', 'turns'))
    ap.add_argument('--grep', help='find a string in the tool results')
    ap.add_argument('--session', help='replay one session')
    args = ap.parse_args()

    if not args.dir:
        print('set TUTOR_TRACE_DIR or pass --dir')
        return 2
    path = os.path.join(args.dir, f'{args.name}.jsonl')
    if not os.path.exists(path):
        print(f'no trace at {path}')
        return 1

    rows = load(path)
    if not rows:
        print(f'{path} is empty')
        return 1
    if args.grep:
        grep(rows, args.grep)
    elif args.session:
        session(rows, args.session)
    else:
        summarise(rows, args.dir)
    return 0


if __name__ == '__main__':
    sys.exit(main())
