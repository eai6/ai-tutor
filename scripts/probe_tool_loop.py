"""Replay a REAL captured Call-1 payload at Ollama under prompt/decode variants.

Written to isolate the tool-call repetition loop that the per-turn directive
caused, and it found it: with the directive on, every generation ran to the
num_predict ceiling and ended either as a duplicate tool-call storm (31 x
record_answer in one response, silently de-duplicated by `_dispatch_tools` so
it was invisible in behaviour and only showed up in decode time) or cut off
mid-`<tool_call>`, which Ollama reports as `HTTP 500 {"error":"... invalid tool
call arguments ...: unexpected end of JSON input"}`. The directive has since
been deleted from the engine; see memory/tool_compliance_root_cause.md.

**This script outlives it, and is the reason to keep it: replaying the captured
payload is the only cheap way to test a local-model hypothesis honestly.** The
directive scored 6/8 against a hand-built request and 3/8 against this one —
the captured payload is longer and carries the full tool-schema set, and that
difference is where the loop lived. Anything believed from a toy prompt should
be re-run here before it reaches the engine.

Call 1 is never streamed (only `_run_second_call` receives the gate), so what
this measures is genuine model output, not a stream-accumulation artefact.

Two modes, because the engine is slow and Ollama is not:

    .venv/bin/python scripts/probe_tool_loop.py --capture
        Runs real turns and dumps each Call 1's exact payload — system blocks,
        messages, tools, sampling — to eval-reports/call_compliance/.

    .venv/bin/python scripts/probe_tool_loop.py --replay [-n 5]
        Replays those payloads straight at Ollama under the variants below,
        counting tool calls per response. No Django, no session, no student
        sim, so a variant costs one generation instead of a whole turn.

The untried leads are `single_clause`, `once`, and a `presence_penalty` sweep
(`repeat_penalty` is 1 on this tag — repetition control is off entirely, and
Qwen3-Instruct-2507's own guidance prescribes presence_penalty for this).
"""
from __future__ import annotations

import argparse
import copy
import itertools
import json
import logging
import os
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'ai_tutor.config.settings')
os.environ.setdefault('TUTOR_MODEL_OVERRIDE', 'local_ollama/qwen3-4b-jetson')

OUT_DIR = Path(__file__).resolve().parents[1] / 'eval-reports' / 'call_compliance'
CAPTURE = OUT_DIR / 'call1_payload.json'

# The directive text, split so a variant can drop the trailing clause. These
# are this script's own copies and the only surviving ones — the engine's
# `prompts.render_turn_directive` was deleted, so there is nothing to keep them
# in sync with. They stay because the `shipped` arm is the control every future
# variant is scored against.
GRADE_HEAD = (
    "Record what the student just answered by calling record_answer "
    "in this same reply, then write your response to them.\n"
    "Pass their answer exactly as they wrote it — the platform grades "
    "it against the question already in flight."
)
POSE_HEAD = (
    "Ask exactly one question, and register it by calling "
    "pose_question in this same reply.\n"
    "A question written only as prose creates no gradable slot, so the "
    "student's next message has nothing to be checked against."
)


def _wrap(body: str) -> str:
    return f"\n\n<this_turn>\n{body}\n</this_turn>"


def variants(mode: str) -> dict[str, str]:
    """Directive texts to compare. Keys are the arm names in the report."""
    head = GRADE_HEAD if 'GRADE' in mode else POSE_HEAD
    tool = 'record_answer' if 'GRADE' in mode else 'pose_question'

    # 'no_directive' is what the engine now sends and so is the reference
    # arm; 'shipped' is the deleted directive, kept as the known-bad control.
    # The rest each change ONE thing against 'shipped':
    #   no_directive  — the engine's actual behaviour. Measured 0/8 looped,
    #                   0/8 HTTP 500, 3-9 s. This is the bar to beat.
    #   single_clause — drops the "then write your response" continuation, which
    #                   is the clause that tells the model to keep generating
    #                   after the call and so gives the loop somewhere to go
    #   once          — states the call count positively and explicitly
    #   presence      — the shipped directive plus presence_penalty. This is a
    #                   DECODING fix rather than a prompt fix, and it is the one
    #                   upstream actually prescribes: `ollama show` reports
    #                   repeat_penalty 1 for qwen3:4b-instruct (inherited from
    #                   the base tag, not set by our Modelfile), i.e. repetition
    #                   control is fully off, and Qwen3-Instruct-2507's own
    #                   guidance is to use presence_penalty for exactly this.
    #                   `model_profiles.SamplingProfile.presence_penalty` and
    #                   `OllamaClient` (client.py:1739) already plumb it, so a
    #                   win here is a one-line profile change.
    first_line = head.split('\n')[0]
    once = _wrap(f"{head}\nOne {tool} call is all this turn needs.")
    return {
        'no_directive': '',
        'shipped': _wrap(head),
        'single_clause': _wrap(first_line.replace(
            ', then write your response to them.', '.')),
        'once': once,
        'presence': _wrap(head),
    }


# Arms that change decoding rather than the prompt.
ARM_OPTIONS = {
    'presence': {'presence_penalty': 1.5},
}


# ---------------------------------------------------------------------------
# System-prompt arms — the LENGTH axis
# ---------------------------------------------------------------------------
#
# The directive arms above change the USER message. These change Block 0 of the
# SYSTEM prompt, which is where Finding 2 in memory/tool_compliance_root_cause.md
# located the actual suppressor: prefixes of the real captured prompt scored 5/5
# tool calls at 8k and 0/5 at the full 24k. That finding was measured on
# truncated prefixes, which cut mid-instruction and so confound "shorter" with
# "mutilated". These arms swap in COMPLETE prompts of decreasing length instead,
# so length varies while the prompt stays well-formed:
#
#   full            as captured, 20.5k block 0 -> 23.8k system   (control)
#   compact         duplication removed, 13.5k -> 16.9k system
#   compact_noslot  compact minus the added slot rule            (isolation arm)
#   terse           rationale removed too,      7.3k -> 10.7k system
#
# `compact_noslot` exists because `compact` changes two things at once: it is
# shorter AND it adds the "a question written only as prose creates no slot"
# rule that the Gemini block has always carried and the Qwen block never did.
# Without this arm a win could not be attributed, and the last time this problem
# was worked a two-things-at-once result cost a full diagnosis cycle.
SLOT_RULE_PARAGRAPH = (
    "Every question you ask goes through `pose_question`, and every answer the "
    "student gives goes through `record_answer`. A question written only as "
    "prose creates no slot, so the student's next message has nothing to be "
    "graded against and the lesson stalls.\n\n"
)


def _extract_fills(template: str, rendered: str) -> dict[str, str]:
    """Recover the {PLACEHOLDER} values from an already-rendered Block 0.

    The captured payload holds Block 0 *after* prompts.py filled
    {ROLE_AUDIENCE}/{FIGURE_RULE}/{LOCALE_RULE}, and a variant has to be
    rendered with the SAME values or the arm is not a length comparison.

    Rather than re-deriving them (which needs Django, a locale profile, and the
    lesson's figures_enabled flag, each an opportunity to fill in a value the
    captured turn did not use), recover them by diff: split the template on its
    placeholders, locate each literal segment in the rendered text in order, and
    whatever sits between two segments IS the fill. Zero drift by construction.
    """
    import re as _re
    parts = _re.split(r'(\{[A-Z_]+\})', template)
    fills: dict[str, str] = {}
    pos = 0
    pending: str | None = None
    for part in parts:
        if not part:
            continue
        if part.startswith('{') and part.endswith('}'):
            pending = part
            continue
        idx = rendered.find(part, pos)
        if idx < 0:
            raise SystemExit(
                'cannot align template with the captured Block 0 — the '
                'template has been edited since capture. Re-run --capture.')
        if pending is not None:
            fills[pending] = rendered[pos:idx]
            pending = None
        pos = idx + len(part)
    if pending is not None:
        fills[pending] = rendered[pos:]
    return fills


# ---------------------------------------------------------------------------
# Intent-guidance arms — the PER-TURN block, and the one the data points at
# ---------------------------------------------------------------------------
#
# The length sweep found that tool compliance tracks the payload, not Block-0
# size: on the `answer` payloads nearly every arm scored 4/4, and on the
# `answer_or_other` payloads nearly every arm scored 0/4, across prompts from
# 24k down to 7.9k chars. What separates those payloads is the
# `<message_intent>` guidance in the per-turn block — 190-264 chars, rendered
# LAST, closest to the student message.
#
# `prompts._INTENT_GUIDANCE['answer_or_other']` currently reads:
#
#   "Intent could not be classified deterministically. Use judgement: if it
#    reads as an answer attempt, call record_answer; if it reads as a
#    clarification or pushback, respond conversationally."
#
# Three defects, and they compound on a 4B model:
#   1. It CONTRADICTS Block 0, which says to call record_answer on every turn
#      with a question in flight, passing an empty extracted_answer when the
#      message was not an answer. This block is last, so it wins the conflict.
#   2. "Use judgement" is the vague-qualifier anti-pattern, and it opens with
#      the platform declaring its own uncertainty, which weakens the directive
#      it then gives.
#   3. It never mentions the empty-extracted_answer mechanism, so the model has
#      no way to both call the tool and respond conversationally — the two
#      branches are presented as exclusive when the protocol makes them
#      compatible.
#
# `always_record` removes the conflict rather than adding emphasis: the CALL
# becomes unconditional and the judgement moves into the ARGUMENT, which is the
# distinction the engine already implements. `_dispatch_tools` handles an empty
# extracted_answer by recording nothing and leaving the slot open.
INTENT_ARMS: dict[str, str | None] = {
    'current': None,
    'always_record': (
        "This message may be an answer to the in-flight question. Call "
        "record_answer either way: pass the value the student gave if their "
        "message contains one, or an empty extracted_answer if it does not. "
        "The platform grades a value and records nothing for an empty one, so "
        "the question stays open when it was not an answer. Then write your "
        "reply to what the student actually said."
    ),
    # Isolation arm: same unconditional call, none of the explanation. Separates
    # "the conflict was the problem" from "the extra words were the problem".
    'terse_record': (
        "Call record_answer with the value the student gave, or with an empty "
        "extracted_answer if their message contains no value. Then reply to "
        "what they said."
    ),
    # The arm that tests a CLASSIFIER fix instead of a prompt fix, by rendering
    # the guidance the turn would have received if intent.py had classified it
    # correctly. Both failing payloads are answers with a conversational
    # preamble — "ohh wait, so its 450" and "ohh yeah i get it now, its 360" —
    # and they reach `answer_or_other` through the bare fallback at
    # intent.py:277: `_NUMERIC_ONLY` wants nothing but a number and
    # `_NUMERIC_WITH_WORK` wants visible working, so a trailing number after
    # prose matches neither.
    #
    # This matters because the `answer` classification is the one that WORKS:
    # payloads 1 and 4 carry it and scored 8/8 on expected-tool across the
    # compact arms, while the two `answer_or_other` payloads scored 0/40 across
    # every prompt variant. If this arm lifts them, the fix belongs in the
    # classifier (a deterministic trailing-number rule) rather than in any
    # prompt, which is both cheaper and testable without a model.
    'as_answer': None,   # filled below from the real guidance table
}

try:                                      # keep in sync with the engine's text
    from ai_tutor.apps.tutoring.simple_tutor.prompts import _INTENT_GUIDANCE as _IG
    INTENT_ARMS['as_answer'] = _IG['answer']
except Exception:                         # pragma: no cover - probe convenience
    INTENT_ARMS['as_answer'] = (
        "Treat this turn as a graded answer attempt to the in-flight "
        "question. Call record_answer with the literal extracted answer."
    )


# ---------------------------------------------------------------------------
# Payload mutation arms — isolating WHICH turns get a tool call
# ---------------------------------------------------------------------------
#
# Both the Block-0 length sweep and the intent-guidance sweep came back flat:
# payloads 1 and 4 get their expected tool under nearly every prompt, payloads
# 2 and 3 get it under none (0/40 across five prompts, then 0/32 across four
# intent variants). So the determinant is in the TURN, not the instructions.
#
# Two differences between those groups, and they are confounded in the captured
# data:
#   (a) the student message — "270" / "150" are bare values, while
#       "ohh wait, so its 450" / "ohh yeah i get it now, its 360" wrap the value
#       in conversational prose.
#   (b) attempt_count — 0 on the working payloads, 1 and 2 on the failing ones,
#       which also means the failing turns carry hint history in <recent_turns>.
#
# These arms mutate one captured payload at a time so (a) and (b) separate. That
# is worth doing before any more prompt work: if (a) is the cause the fix is in
# answer EXTRACTION, and if (b) is the cause the fix is in what the engine puts
# in context on a retry — and neither is a prompt-length problem.
_TRAILING_VALUE_RE = None


def _mutate(msgs: list, blocks: list, arm: str) -> tuple[list, list]:
    """Apply a payload mutation arm. Returns (msgs, blocks), both fresh."""
    import re as _re
    global _TRAILING_VALUE_RE
    if _TRAILING_VALUE_RE is None:
        # last number (int/decimal/negative) or bare option letter in the text
        _TRAILING_VALUE_RE = _re.compile(
            r'(-?\d+(?:\.\d+)?)(?!.*-?\d)|(?:\b([A-D])\b)(?![\s\S]*\b[A-D]\b)')

    msgs = [dict(m) for m in msgs]
    blocks = list(blocks)
    if arm == 'none':
        return msgs, blocks

    if arm in ('bare_answer', 'bare_and_attempt0'):
        m = _TRAILING_VALUE_RE.search(msgs[-1]['content'] or '')
        if not m:
            raise SystemExit(f'no trailing value in {msgs[-1]["content"]!r}')
        msgs[-1]['content'] = m.group(1) or m.group(2)

    if arm == 'prosify':
        # The converse test: wrap a working payload's bare value in the same
        # shape of preamble the failing payloads use. If this breaks a 4/4
        # payload, the prose is the cause.
        msgs[-1]['content'] = f"ohh yeah i get it now, its {msgs[-1]['content']}"

    if arm in ('attempt0', 'bare_and_attempt0'):
        for i, b in enumerate(blocks):
            if '<attempt_count>' in b:
                blocks[i] = _re.sub(r'<attempt_count>\d+</attempt_count>',
                                    '<attempt_count>0</attempt_count>', b)
                break
        else:
            raise SystemExit('payload carries no <attempt_count>')

    return msgs, blocks


MUTATE_ARMS = ('none', 'bare_answer', 'attempt0', 'bare_and_attempt0',
               'prosify')


_GUIDANCE_RE = None


def _guidance_re():
    global _GUIDANCE_RE
    if _GUIDANCE_RE is None:
        import re as _re
        _GUIDANCE_RE = _re.compile(
            r'(<message_intent>\s*<classification>[^<]*</classification>\s*'
            r'<guidance>)(.*?)(</guidance>)', _re.S)
    return _GUIDANCE_RE


def _swap_intent_guidance(block_text: str, new_text: str) -> str:
    """Replace the <guidance> body inside the per-turn <message_intent> block.

    Matched on the full rendered ELEMENT — opening tag, <classification>, then
    <guidance> — not on the substring `<message_intent>`. Block 0 mentions that
    tag repeatedly in prose while explaining the data sections, so a substring
    search finds the instruction block instead of the rendered one.
    """
    out, n = _guidance_re().subn(
        lambda m: m.group(1) + new_text + m.group(3), block_text, count=1)
    if not n:
        raise SystemExit('no rendered <message_intent> element found to swap')
    return out


def _pose_demo(captured_block_0: str) -> str:
    """Lift the POSE-only worked turn out of the FULL prompt, verbatim.

    The full template's `## Examples` section opens with a pose-only turn that
    ends in a literal ``Tool call: `pose_question(...)` `` line. Both shortened
    variants dropped it (they keep only the combined grade-and-pose turn), and
    the first sweep showed `full` calling the tool 4/4 on the POSE payload where
    `compact` scored 0/4 — so the missing pose-shaped few-shot is a candidate
    mechanism with the right sign.

    Taken by slicing the captured text rather than retyping it, so the arm tests
    the exact string that works in the arm that works. Returns '' if the section
    is not found, which makes the arm collapse into its base rather than
    silently testing something else.
    """
    start = captured_block_0.find('**Good turn** —')
    if start < 0:
        return ''
    end = captured_block_0.find('**Good turn — grading', start)
    return captured_block_0[start:end].rstrip() if end > start else ''


def system_arms(captured_block_0: str) -> dict[str, str]:
    """Return {arm: block-0 text}, each rendered with the captured fills."""
    from ai_tutor.apps.tutoring.simple_tutor import family_prompts as fp

    fills = _extract_fills(fp.MARKDOWN_BLOCK_0_TEMPLATE, captured_block_0)

    def render(template: str) -> str:
        out = template
        for ph, val in fills.items():
            out = out.replace(ph, val)
        return out

    compact = render(fp.MARKDOWN_BLOCK_0_COMPACT)

    # `terse` minus its whole `## Each reply` block — the per-reply pedagogy
    # rules (name the slip, verdict consistency, MCQ letter balance, 5E phases,
    # brevity of clarifications...). Cut on the TEMPLATE, bounded by the
    # {FIGURE_RULE} placeholder, so the boundary is a literal that cannot drift
    # into rendered lesson text.
    #
    # This arm answers "how much of the length cost IS the pedagogy?" and it is
    # the floor of the sweep: everything load-bearing for the tool protocol
    # (modes, the slot rule, the hint ladder, the two-call worked turn, safety)
    # survives, and only the reply-quality rules go. Read the result narrowly —
    # tool compliance here is not evidence about tutoring quality, which these
    # rules exist to protect and which this probe does not measure at all.
    terse_tpl = fp.MARKDOWN_BLOCK_0_TERSE
    head, sep, tail = terse_tpl.partition('## Each reply')
    if not sep:
        raise SystemExit("terse template lost its '## Each reply' section")
    terse_min = render(head + tail[tail.index('{FIGURE_RULE}'):])

    terse = render(terse_tpl)

    # Restore the pose-shaped few-shot the shortened variants dropped. Inserted
    # before `## Safety` so the safety block keeps the last word, which is where
    # the base template deliberately puts it.
    demo = _pose_demo(captured_block_0)

    def with_demo(text: str) -> str:
        if not demo or '## Safety' not in text:
            return text
        return text.replace(
            '## Safety', f'**Good turn — posing.** {demo[len("**Good turn** —"):].lstrip()}'
            f'\n\n## Safety', 1)

    return {
        'full': captured_block_0,
        'compact': compact,
        'compact_noslot': compact.replace(SLOT_RULE_PARAGRAPH, ''),
        'terse': terse,
        'terse_no_reply_rules': terse_min,
        'terse_pose_demo': with_demo(terse),
        'compact_pose_demo': with_demo(compact),
    }


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------
def capture(lesson: int, persona: str, turns: int) -> int:
    """Save Call 1 of every turn, not just the first.

    The loop is stochastic and appears to be mode-dependent — the one caught
    in the directive-two run was a GRADE turn, while turn 1 of a session is
    always a POSE. Replaying a single captured payload would therefore be
    sampling the wrong distribution. Keeping one payload per turn lets the
    replay report per-payload as well as per-arm, so "arm X loops" can be
    separated from "payload Y loops".
    """
    import django
    django.setup()

    from ai_tutor.apps.tutoring.simple_tutor import engine

    real_call = engine._call_llm
    grabbed: list[dict] = []
    cur_mode: dict = {'mode': None}

    # Mode comes from the engine's OWN log record, read as structured args
    # rather than formatted text. respond() emits
    # "[simple_tutor] mode=%s intent=%s ..." right after deciding, so args[0]
    # is exactly the value the turn used.
    #
    # This used to piggyback on a `render_turn_directive` monkeypatch, which
    # died with that function. Do not go back to regexing the system prompt for
    # the mode — an earlier version did, matched nothing, and silently labelled
    # every payload with the fallback.
    class _ModeSpy(logging.Handler):
        def emit(self, record):
            if str(record.msg).startswith('[simple_tutor] mode=%s') and record.args:
                cur_mode['mode'] = record.args[0]

    mode_spy = _ModeSpy()
    logging.getLogger(engine.__name__).addHandler(mode_spy)

    def spy(**kw):
        # Call 1 is identified STRUCTURALLY: it is the only _call_llm whose
        # messages list is a single user turn. Call 2 carries the assistant
        # reply plus tool_result blocks, and the repair path adds more still.
        # A parity counter ("odd calls are Call 1") does not survive the
        # opening start_for_view turn — it captured Call 2 of the greeting.
        #
        # DEEP COPY, not a reference. `messages` is the SAME list object the
        # two-call loop appends to: _run_second_call pushes the assistant
        # reply and the tool_result user block onto it in place. Storing the
        # reference meant the filter correctly matched a 1-element list that
        # had grown to 3 by the time it was serialised — a captured "Call 1"
        # payload that silently contained Call 2's history.
        msgs = kw.get('messages') or []
        if len(msgs) == 1 and (msgs[0].get('role') == 'user'):
            grabbed.append(copy.deepcopy({
                'system_blocks': kw.get('system_blocks'),
                'tools': kw.get('tools'),
                'messages': msgs,
                'mode': cur_mode['mode'],
            }))
        return real_call(**kw)

    engine._call_llm = spy

    os.environ['TUTOR_CALL_MODE'] = 'two'
    os.environ.pop('TUTOR_STREAMING', None)

    from ai_tutor.apps.tutoring.student_sim.driver import simulate_session
    try:
        simulate_session(lesson_id=lesson, persona=persona, max_turns=turns)
    finally:
        logging.getLogger(engine.__name__).removeHandler(mode_spy)

    if not grabbed:
        print('no call captured', file=sys.stderr)
        return 1

    from ai_tutor.apps.llm.models import ModelConfig
    cfg = ModelConfig.get_for('tutoring')

    # The greeting turn is not a graded exchange, so no mode was logged for it
    # and it is not a payload the replay should sample.
    grabbed = [g for g in grabbed if g['mode']]

    out = {'model_name': cfg.model_name, 'payloads': grabbed}
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    CAPTURE.write_text(json.dumps(out, indent=2, default=str))
    for i, g in enumerate(grabbed):
        sys_chars = sum(len(_text_of(b)) for b in g['system_blocks'])
        print(f"  [{i}] mode={g['mode']} system_chars={sys_chars} "
              f"tools={len(g['tools'])} messages={len(g['messages'])}")
    print(f"wrote {len(grabbed)} payloads to {CAPTURE}")
    return 0


def _text_of(block) -> str:
    if isinstance(block, str):
        return block
    if isinstance(block, dict):
        return block.get('text') or ''
    return str(block)


# ---------------------------------------------------------------------------
# Replay
# ---------------------------------------------------------------------------
def _flatten(payload: dict, block_0: str | None = None,
             intent_guidance: str | None = None,
             mutate: str = 'none') -> tuple[str, list, list]:
    """Engine-shaped payload -> the (system, messages, tools) Ollama wants.

    `block_0` replaces the captured static instruction block and
    `intent_guidance` rewrites only the <guidance> body of the per-turn
    <message_intent> block. Everything else — the step block, the in-flight
    slot, recent turns, the length budget — stays exactly as the engine built
    it, so an arm varies one thing and nothing else about the turn.
    """
    blocks = [_text_of(b) for b in payload['system_blocks']]
    if block_0 is not None:
        blocks[0] = block_0
    if intent_guidance is not None:
        # Locate the block by matching the rendered element, not the tag name:
        # Block 0's instructions refer to `<message_intent>` in prose, so a
        # substring search picks the instruction block and the swap silently
        # rewrites nothing (or the wrong thing).
        idx = next((i for i, b in enumerate(blocks) if _guidance_re().search(b)),
                   None)
        if idx is None:
            raise SystemExit('payload carries no rendered <message_intent>')
        blocks[idx] = _swap_intent_guidance(blocks[idx], intent_guidance)
    msgs = []
    for m in payload['messages']:
        content = m.get('content')
        if isinstance(content, list):
            parts = [c.get('text', '') for c in content
                     if isinstance(c, dict) and c.get('type') == 'text']
            content = '\n'.join(p for p in parts if p)
        msgs.append({'role': m.get('role', 'user'), 'content': content or ''})
    if mutate != 'none':
        msgs, blocks = _mutate(msgs, blocks, mutate)
    system = '\n\n'.join(blocks)
    tools = [{'type': 'function',
              'function': {'name': t['name'],
                           'description': t.get('description', ''),
                           'parameters': t.get('input_schema', {})}}
             for t in payload['tools']]
    return system, msgs, tools


def replay(n: int, arms: list[str] | None, payload_idx: list[int] | None,
           sysarms: list[str] | None = None,
           intentarms: list[str] | None = None,
           mutarms: list[str] | None = None) -> int:
    import requests

    if not CAPTURE.exists():
        print(f'no capture at {CAPTURE} — run --capture first', file=sys.stderr)
        return 1
    cap = json.loads(CAPTURE.read_text())
    payloads = cap['payloads']
    chosen = payload_idx if payload_idx else list(range(len(payloads)))

    # The two axes are orthogonal and are NOT crossed by default: asking for
    # system arms holds the user message at the engine's real behaviour
    # (`no_directive`), because the directive is refuted and crossing it in
    # would spend most of the run re-measuring a known-bad arm.
    if (sysarms or intentarms or mutarms) and not arms:
        arms = ['no_directive']
    picked_intent = intentarms or ['current']
    for ia in picked_intent:
        if ia not in INTENT_ARMS:
            raise SystemExit(f'unknown intent arm {ia!r}; '
                             f'have {list(INTENT_ARMS)}')
    picked_mut = mutarms or ['none']
    for ma in picked_mut:
        if ma not in MUTATE_ARMS:
            raise SystemExit(f'unknown mutate arm {ma!r}; have {MUTATE_ARMS}')

    url = f"{os.getenv('OLLAMA_API_BASE', 'http://localhost:11434')}/api/chat"
    samples: list[dict] = []

    for pi in chosen:
        payload = payloads[pi]
        all_sys = system_arms(_text_of(payload['system_blocks'][0])) \
            if sysarms else {'full': None}
        picked_sys = sysarms or ['full']
        all_arms = variants(payload['mode'])
        picked = arms or list(all_arms)

        for sysarm, intentarm, mutarm in itertools.product(
                picked_sys, picked_intent, picked_mut):
            system, msgs, tools = _flatten(
                payload, all_sys[sysarm], INTENT_ARMS[intentarm], mutarm)
            for arm in picked:
                directive = all_arms[arm]
                for i in range(n):
                    body = list(msgs)
                    if directive:
                        body = body[:-1] + [{
                            **body[-1],
                            'content': body[-1]['content'] + directive}]
                    options = {'temperature': 0.7, 'top_p': 0.8, 'top_k': 20,
                               'num_ctx': 16384, 'num_gpu': 99,
                               'num_predict': 1024}
                    options.update(ARM_OPTIONS.get(arm, {}))
                    t0 = time.time()
                    r = requests.post(url, json={
                        'model': cap['model_name'], 'messages':
                            [{'role': 'system', 'content': system}] + body,
                        'tools': tools, 'stream': False, 'options': options,
                    }, timeout=900)

                    # A 500 is an OUTCOME here, not an error to abort on. When
                    # the loop runs into num_predict it is cut off
                    # mid-<tool_call>, Ollama fails to parse the partial call
                    # and returns 500 rather than a truncated message. That is
                    # the single most expensive thing this probe measures:
                    # engine._is_transient_error treats 500 as retryable
                    # (engine.py:1785), so the engine re-runs the whole looping
                    # generation up to 5 times. Crashing here would discard
                    # exactly the samples that matter.
                    label = '/'.join(
                        x for x in (sysarm,
                                    intentarm if intentarm != 'current' else '',
                                    mutarm if mutarm != 'none' else '',
                                    arm) if x)
                    tag = f"p{pi} {label} {i+1}/{n}"
                    if r.status_code >= 500:
                        samples.append({
                            'payload': pi, 'mode': payload['mode'],
                            'sysarm': sysarm, 'intentarm': intentarm,
                            'mutarm': mutarm, 'arm': arm,
                            'system_chars': len(system),
                            'tools': 0, 'max_dup': 0, 'out_tokens': None,
                            'capped': True, 'http_500': True,
                            'secs': round(time.time() - t0, 1), 'text': '',
                        })
                        print(f"  [{tag}] HTTP {r.status_code} after "
                              f"{samples[-1]['secs']}s — loop hit the cap "
                              f"mid-tool-call", flush=True)
                        continue
                    r.raise_for_status()
                    data = r.json()
                    msg = data.get('message') or {}
                    tc = msg.get('tool_calls') or []
                    names = [c.get('function', {}).get('name') for c in tc]
                    out_tok = data.get('eval_count') or 0
                    samples.append({
                        'payload': pi,
                        'mode': payload['mode'],
                        'sysarm': sysarm,
                        'intentarm': intentarm,
                        'mutarm': mutarm,
                        'arm': arm,
                        'system_chars': len(system),
                        # What the arm actually COSTS in context, straight from
                        # Ollama rather than divided out of a char count. Also
                        # the KV-cache sizing input: on a 7.4 GB shared-memory
                        # Jetson, num_ctx is reserved memory, so an arm that
                        # halves prompt tokens is a memory result as well as a
                        # compliance one.
                        'in_tokens': data.get('prompt_eval_count') or 0,
                        'tools': len(tc),
                        # WHICH tools, not just how many. A GRADE turn that
                        # calls pose_question has registered a new question and
                        # silently dropped the answer the student just gave —
                        # a protocol failure that a tool COUNT scores as a
                        # success. Measured on payload 4: the full prompt did
                        # exactly this on 3 of 4 trials.
                        'names': names,
                        'max_dup': max((names.count(x) for x in set(names)),
                                       default=0),
                        'out_tokens': out_tok,
                        # The loop's signature is hitting num_predict exactly.
                        # That is a cleaner discriminator than a tool count,
                        # because a turn can legitimately want 2-3 calls.
                        'capped': out_tok >= 1024,
                        'http_500': False,
                        'secs': round(time.time() - t0, 1),
                        # Kept so reply QUALITY can be eyeballed, not just loop
                        # counts: a shorter prompt that wins on tool calls while
                        # producing worse prose is not a win, and this probe
                        # cannot see prose quality any other way.
                        'text': (msg.get('content') or '')[:400],
                    })
                    s = samples[-1]
                    print(f"  [{tag}] tools={s['tools']} dup={s['max_dup']} "
                          f"out={out_tok}{'!CAP' if s['capped'] else ''} "
                          f"{s['secs']}s {names[:3]}", flush=True)

    # -- aggregate per (system arm, directive arm), pooled across payloads ---
    def _key(s):
        return (s['sysarm'], s.get('intentarm', 'current'),
                s.get('mutarm', 'none'), s['arm'])

    rows = []
    seen_keys = list(dict.fromkeys(_key(s) for s in samples))
    for key in seen_keys:
        sysarm, intentarm, mutarm, arm = key
        got = [s for s in samples if _key(s) == key]
        ok = [s for s in got if not s['http_500']]
        rows.append({
            'sysarm': sysarm,
            'intent': intentarm,
            'mutate': mutarm,
            'arm': arm,
            'sys_chars': int(statistics.median(
                s['system_chars'] for s in got)),
            'n': len(got),
            'had_tool': f"{sum(1 for s in got if s['tools'])}/{len(got)}",
            'looped': f"{sum(1 for s in got if s['capped'])}/{len(got)}",
            'http_500': f"{sum(1 for s in got if s['http_500'])}/{len(got)}",
            'median_tools': statistics.median(s['tools'] for s in ok) if ok else '-',
            'max_dup': max((s['max_dup'] for s in ok), default=0),
            'median_in': int(statistics.median(
                s.get('in_tokens') or 0 for s in ok)) if ok else '-',
            'median_out': statistics.median(
                s['out_tokens'] for s in ok) if ok else '-',
            'median_secs': statistics.median(s['secs'] for s in got),
        })

    def _table(rs: list[dict]) -> None:
        cols = list(rs[0])
        width = {c: max(len(c), *(len(str(r[c])) for r in rs)) for c in cols}
        print('  '.join(c.ljust(width[c]) for c in cols))
        for r in rs:
            print('  '.join(str(r[c]).ljust(width[c]) for c in cols))

    print()
    _table(rows)

    # POSE and GRADE are different asks — a POSE turn wants pose_question, a
    # GRADE turn wants record_answer, and the captured set is 1 POSE to 4 GRADE.
    # Pooling them hides an arm that fixes one and breaks the other.
    modes = list(dict.fromkeys(s['mode'] for s in samples))
    if len(modes) > 1:
        by_mode = []
        for key in seen_keys:
            row = {'sysarm': key[0], 'intent': key[1], 'mutate': key[2],
                   'arm': key[3]}
            for mode in modes:
                got = [s for s in samples
                       if _key(s) == key and s['mode'] == mode]
                row[mode] = (f"{sum(1 for s in got if s['tools'])}/{len(got)}"
                             if got else '-')
            by_mode.append(row)
        print('\ntool calls by mode:')
        _table(by_mode)

    (OUT_DIR / 'tool_loop_probe.json').write_text(
        json.dumps({'summary': rows, 'samples': samples}, indent=2))
    print(f"\nwrote {OUT_DIR / 'tool_loop_probe.json'}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--capture', action='store_true')
    ap.add_argument('--replay', action='store_true')
    ap.add_argument('--arm', action='append',
                    help='replay only these user-message directive arms')
    ap.add_argument('--sysarm', action='append',
                    help='system Block-0 arms to sweep: full | compact | '
                         'compact_noslot | terse | terse_no_reply_rules | '
                         'terse_pose_demo | compact_pose_demo. '
                         'Implies --arm no_directive unless --arm is given.')
    ap.add_argument('--intentarm', action='append',
                    help='per-turn <message_intent> guidance arms: '
                         'current | always_record | terse_record. '
                         'Implies --arm no_directive unless --arm is given.')
    ap.add_argument('--mutate', action='append',
                    help='payload mutation arms: none | bare_answer | '
                         'attempt0 | bare_and_attempt0 | prosify. Isolates '
                         'WHICH turns get a tool call.')
    ap.add_argument('--payload', action='append', type=int,
                    help='replay only these captured payload indices')
    ap.add_argument('-n', type=int, default=5)
    ap.add_argument('--lesson', type=int, default=1137)
    ap.add_argument('--persona', default='error_prone')
    ap.add_argument('--turns', type=int, default=4,
                    help='capture: how many turns to harvest Call 1 from')
    args = ap.parse_args()

    if args.capture:
        return capture(args.lesson, args.persona, args.turns)
    if args.replay:
        return replay(args.n, args.arm, args.payload, args.sysarm,
                      args.intentarm, args.mutate)
    ap.error('pass --capture or --replay')
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
