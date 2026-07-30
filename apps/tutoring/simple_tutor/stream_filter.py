"""Safe-prefix computation for streamed tutor replies.

The engine post-processes a reply through eight whole-text transforms
(engine.respond, "Deterministic backstop" onward) before any of it is safe
to show a student. Streaming raw model tokens bypasses every one of them —
including `_filter_reveals`, which is what stops a wrong-answer hint from
printing the reference answer.

So streaming here is an **advisory preview, never the source of truth**:

  * `respond()` still runs the full batch pipeline on the complete raw text.
    That result is what persists and what the final `done` payload carries.
    Persisted output is byte-identical to the non-streaming path by
    construction.
  * The stream carries a conservatively-cut *safe prefix* of the same text,
    computed here.

Byte-exact incremental filtering is NOT achievable, which is why the design
reconciles instead of trying. The batch filters have whole-text operations
that no prefix can reproduce:

  * `_PAREN_RE` / `_ANSWER_PAREN_RE` match parentheticals that legitimately
    span sentence boundaries, so flushing at ". " emits the first half
    before the paren pass can fire.
  * `_is_tool_json_line` is *line*-local, not sentence-local.
  * `_scrub_engine_vocab` ends with `if not re.search(r'[A-Za-z0-9]', result):
    result = ''` — a whole-text collapse. You cannot un-emit text that later
    turns out to be nothing.

`safe_cut_index` therefore only ever cuts where those operations cannot be
mid-flight, and the transports render cumulative snapshots (not append-only
deltas) so a final payload that differs from the streamed prefix is a free
correction rather than a retraction.
"""
from __future__ import annotations

import os
import re

# A cut is only safe at the end of a completed sentence or a line. Emitting
# a partial sentence would defeat _align_reply_polarity, whose whole job is
# replacing the FIRST sentence when it contradicts the grader's verdict —
# the student would read "That's right!" and watch it flip to "Not quite."
_BOUNDARY_RE = re.compile(r'[.!?](?=\s)|\n')

# Openers that must never be shown before the whole sentence is in hand.
# (Kept as a doc anchor: the boundary rule above already implies it.)

_OPEN_TOOL_TAG_RE = re.compile(r'<tool_(?:call|response)>', re.I)
_CLOSE_TOOL_TAG_RE = re.compile(r'</tool_(?:call|response)>', re.I)


def streaming_enabled() -> bool:
    """True when the transport should stream.

    Default OFF. `CLAUDE.md` forbids SSE in production because Azure
    Container Apps cannot do chunked streaming; the offline Jetson kiosk is
    gunicorn on a LAN and is not on ACA. Gating on an env var rather than a
    settings flag keeps the ACA request path byte-identical — nothing there
    ever sets it.
    """
    return os.getenv('TUTOR_STREAMING', '').strip() in ('1', 'true', 'True')


def _fence_count(s: str) -> int:
    return s.count('```')


def safe_cut_index(raw: str) -> int:
    """Length of the longest prefix of ``raw`` that is safe to filter+emit.

    Returns 0 when nothing is safe yet. A candidate boundary is rejected
    unless, at that point, the text is not sitting inside any construct the
    batch filters treat as a unit:

      * unbalanced ``(`` — `_PAREN_RE` and `_ANSWER_PAREN_RE` span sentences
      * unbalanced ``{`` / ``[`` — a tool call leaking as JSON text is
        exactly what `_is_tool_json_line` drops, and Ollama models do leak
        them (see `_maybe_parse_text_tool_call`). Half of one must never
        reach a student.
      * an unclosed ``<tool_call>`` tag — `_XML_TOOL_TAG_RE` needs both ends
      * an unclosed ``` fence
    """
    if not raw:
        return 0
    best = 0
    for m in _BOUNDARY_RE.finditer(raw):
        end = m.end()
        head = raw[:end]
        if head.count('(') != head.count(')'):
            continue
        if head.count('{') != head.count('}'):
            continue
        if head.count('[') != head.count(']'):
            continue
        if len(_OPEN_TOOL_TAG_RE.findall(head)) != len(
                _CLOSE_TOOL_TAG_RE.findall(head)):
            continue
        if _fence_count(head) % 2 != 0:
            continue
        best = end
    return best


class StreamGate:
    """Turns raw model deltas into safe, monotonically-growing snapshots.

    One per streamed call. `feed()` is handed every raw delta; it invokes
    ``emit`` with the cumulative safe prefix, and only when that prefix has
    actually grown — so a burst of tokens inside an unclosed parenthesis
    produces no traffic at all.

    The gate owns the filter inputs that must NOT be recomputed per chunk:
    `reference` (else an InFlightQuestion query per chunk) and the polarity
    rotation index (else a persisted counter increment, a session save, AND
    a different acknowledgement in every snapshot).
    """

    def __init__(self, *, session, tool_results, family, emit):
        self.session = session
        self.tool_results = tool_results
        self.family = family
        self.emit = emit
        self.raw = ''
        self._last_emitted = ''
        self._reference = None          # resolved lazily, then cached
        self._rotation_index = None     # resolved lazily, then cached
        self._resolved_reference = False

    # -- filter inputs, resolved at most once ---------------------------

    def reference_answer(self) -> str:
        """The open question's reference answer, or '' when there is none."""
        if not self._resolved_reference:
            self._resolved_reference = True
            try:
                from apps.tutoring.models import InFlightQuestion
                slot = InFlightQuestion.objects.filter(
                    session=self.session).first()
                self._reference = (
                    (slot.reference_answer or '') if slot is not None else ''
                )
            except Exception:
                # Fail CLOSED for the reveal filter: an empty reference
                # disables it, so on a lookup error we would rather emit
                # nothing than emit an unfiltered hint. See feed().
                self._reference = None
        return self._reference or ''

    def rotation_index(self) -> int:
        """Polarity ack index for this turn — resolved once, reused by the
        final batch pass so the streamed opener and the persisted opener are
        the same string."""
        if self._rotation_index is None:
            from apps.tutoring.simple_tutor.engine import _rotation_index
            self._rotation_index = _rotation_index(self.session, 'polarity')
        return self._rotation_index

    # -- the streaming path --------------------------------------------

    def safe_prefix(self) -> str:
        from apps.tutoring.simple_tutor import engine as _eng

        cut = safe_cut_index(self.raw)
        if cut <= 0:
            return ''
        prefix = self.raw[:cut]

        out = _eng._scrub_engine_vocab(prefix)
        if not out:
            return ''

        # Mirror respond()'s gating exactly: these two run for eval/local
        # families only, and the local Qwen the kiosk runs IS one of them.
        if self.family and self.family not in _eng._FORCE_POSE_EXEMPT_FAMILIES:
            verdict = _eng._turn_verdict(self.tool_results)
            if verdict in ('correct', 'incorrect'):
                out = _eng._align_reply_polarity(
                    self.session, out, self.tool_results,
                    rotation_index=self.rotation_index(),
                )
            out = _eng._filter_reveals(
                self.session, out, self.tool_results,
                reference=self.reference_answer(),
            )
        return out

    def feed(self, delta: str) -> None:
        if not delta:
            return
        self.raw += delta

        # Safety gate. When the verdict is `incorrect` and we cannot
        # establish whether a reference answer exists, `_filter_reveals`
        # would silently no-op and a hint stating the answer would go
        # straight to the student. Withhold the stream instead; the batch
        # pipeline still produces a correct final reply.
        #
        # The resolution MUST happen here rather than lazily inside
        # safe_prefix(): resolving it there let the very first snapshot
        # through before _resolved_reference was ever set, so a failed
        # lookup leaked one sentence and only blocked afterwards.
        from apps.tutoring.simple_tutor import engine as _eng
        if (self.family
                and self.family not in _eng._FORCE_POSE_EXEMPT_FAMILIES
                and _eng._turn_verdict(self.tool_results) == 'incorrect'):
            self.reference_answer()          # resolve before deciding
            if self._reference is None:
                return

        prefix = self.safe_prefix()
        if prefix and prefix != self._last_emitted:
            self._last_emitted = prefix
            self.emit(prefix)

    # The gate IS the on_delta callable, so `_call_llm` can discover
    # `begin_attempt` on it without a second parameter.
    __call__ = feed

    def reset_buffer(self) -> None:
        """Drop accumulated raw text, keeping what was already emitted.

        Call this at any boundary where the NEXT text is a replacement for
        the current text rather than a continuation of it — a retry, or the
        handover from Call 1's flushed prose to Call 2's stream.

        Concatenating across such a boundary is not merely cosmetic: it
        moves the reply's opening away from position 0, so
        `_align_reply_polarity` takes its mid-reply branch instead of its
        opener branch and produces different text. That is exactly how the
        parity test caught this.

        `_last_emitted` is deliberately NOT cleared. Transports render
        cumulative snapshots, so the next emit replaces the bubble.
        """
        self.raw = ''

    # `_call_llm` looks for this name on the on_delta object to reset the
    # gate before each generation attempt.
    begin_attempt = reset_buffer

    @property
    def emitted(self) -> str:
        return self._last_emitted

    @property
    def used_rotation_index(self) -> int | None:
        """The index actually consumed, for the final batch pass. None when
        polarity alignment never came up while streaming."""
        return self._rotation_index
