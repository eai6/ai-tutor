"""Student-intent classifier — fast LLM categoriser for the latest student turn.

Replaces the regex-based ``detect_help_request`` pre-filter that
shipped in Phase 2. The regex was both under- and over-inclusive:

  * It missed common help-request phrasings without ``don't get`` /
    ``don't understand`` ("I don't know how to do percentages",
    "I have no clue what hydrolysis is", "can you teach me?",
    "I forgot what oxidation means" — all routed to retrieval
    scaffolds instead of teaching). This drove the Direct Instruction
    failures called out in the run-5 MATHS-S1 evaluation.
  * It risked over-routing when a student said "I don't understand
    why my answer is wrong" — that is an *attempting* turn (they're
    asking for feedback on their attempt, not asking for the concept
    to be taught from scratch).

A small fast-LLM (CONFORMANCE_CLASSIFIER purpose — Haiku 4.5 by
default) does this discrimination robustly without enumerating
phrasings, generalises across subjects, and survives misspellings /
code-switching that a regex can't.

Fail-soft: on LLM unavailability or parse failure, returns
``"attempting"`` — the conservative default that routes to the
verdict-driven move (no false help-request routing on a model
outage). Spans are emitted under ``intent_classifier`` for the v2
observability dashboard.

Science of learning grounding (Direct Instruction): when a
student signals they don't have the concept yet, *teach it* before
asking for more retrieval. Catching this signal cheaply and
reliably is what gates that branch.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Optional

from apps.tutoring.tracing import emit_span

logger = logging.getLogger(__name__)


# Three intent classes; the fourth ``meta`` (off-topic chat) is
# returned but currently mapped to None at the caller — the engine
# then keeps the verdict-driven move. Future tuning can route meta to
# a tutor-side "back to the lesson" line; for now it falls through.
INTENT_ATTEMPTING = "attempting"
INTENT_ASKING_HELP_EXAMPLE = "asking_help_example"
INTENT_ASKING_HELP_EXPLAIN = "asking_help_explain"
INTENT_META = "meta"

_ALLOWED_INTENTS = (
    INTENT_ATTEMPTING,
    INTENT_ASKING_HELP_EXAMPLE,
    INTENT_ASKING_HELP_EXPLAIN,
    INTENT_META,
)


_INTENT_SYSTEM_PROMPT = """You classify a single student chat turn during a one-on-one tutoring session. Return strict JSON only — no prose, no markdown fences.

Schema:
{"intent": "<one of: attempting | asking_help_example | asking_help_explain | meta>"}

Definitions:

- "attempting" — the student is trying to answer the question they were just asked, even tentatively, partially, wrongly, or while expressing uncertainty about THIS specific answer. Includes short numeric / letter / true-false answers, guesses, and "I think it's X" or "maybe X" framings. "I don't know" with no topic named is attempting — the student is admitting they cannot answer the current question.

- "asking_help_example" — the student explicitly asks to be SHOWN HOW to do something: "show me how", "walk me through", "can you give an example", "demonstrate", "do one for me first".

- "asking_help_explain" — the student explicitly says they do not have a CONCEPT / METHOD / TERM and wants it taught. They name the gap: "I don't know how to do <X>", "I don't understand <X>", "what is <X>", "what does <X> mean", "explain <X>", "I forgot <X>", "I'm lost on <X>", "can you teach me <X>", "I have no clue what <X> is". The key signal is that they identify a gap in their KNOWLEDGE (a method or concept they lack), not in their answer to the current question.

- "meta" — off-topic chat, refusals, complaints, gratitude, or session-management ("can we stop?", "you're not making sense", "thanks", "hi") that do not constitute an answer attempt nor a help request.

Critical distinctions:

1. "I don't understand WHY my answer is wrong" → attempting (asking for feedback on an attempt).
   "I don't understand percentages" → asking_help_explain (gap in concept).

2. "I don't know" alone → attempting.
   "I don't know how to do this" → asking_help_explain (gap in method).
   "I don't know what oxidation is" → asking_help_explain (gap in term).

3. A bare numeric, letter, or T/F answer with no other words is ALWAYS attempting.

4. Misspellings and informal phrasing count: "i dunno how to do %s" → asking_help_explain. "show me pls" → asking_help_example.

5. When uncertain between attempting and asking_help_explain, prefer attempting — over-routing help_explain wastes a teaching turn on a student who was about to answer.

Output exactly one JSON object with one key. Nothing else."""


def _render_user_prompt(student_input: str, open_question_stem: str = "") -> str:
    """Render the per-turn classifier user prompt.

    ``open_question_stem`` is OPTIONAL context — when present it helps
    the classifier disambiguate "I don't understand" (referring to the
    question vs. referring to a concept). Truncated to 280 chars to
    keep the call cheap.
    """
    stem = (open_question_stem or "").strip().replace("\n", " ")
    if len(stem) > 280:
        stem = stem[:280] + "…"
    if not stem:
        stem = "(no open question — the student spoke first or after a topic close)"
    text = (student_input or "").strip().replace("\n", " ")
    return (
        f"Question the student is being asked:\n{stem}\n\n"
        f"Student turn to classify:\n{text}\n\n"
        f'Return: {{"intent": "..."}}'
    )


def classify_student_intent(
    *,
    student_input: str,
    open_question_stem: str = "",
    llm_client=None,
) -> str:
    """Return one of the ``INTENT_*`` constants.

    Fail-soft default is ``INTENT_ATTEMPTING`` — a conservative
    no-op that routes to the verdict-driven move selection.
    """
    text = (student_input or "").strip()
    if not text:
        return INTENT_ATTEMPTING

    with emit_span("audit", "intent_classifier") as span:
        if llm_client is None:
            # Reuse the CONFORMANCE_CLASSIFIER model config — both are
            # fast tiny-payload classifiers; pinning to a single fast
            # purpose avoids new ModelConfig rows and migrations. Both
            # share Haiku 4.5 by default.
            from apps.tutoring.v2.services.student_grader import (
                _build_client_for_purpose,
            )
            llm_client = _build_client_for_purpose("conformance_classifier")
        if llm_client is None:
            if span is not None:
                span["payload"] = {
                    "intent": INTENT_ATTEMPTING,
                    "outcome": "skipped",
                    "reason": "no_client",
                }
            return INTENT_ATTEMPTING

        try:
            response = llm_client.generate(
                messages=[
                    {
                        "role": "user",
                        "content": _render_user_prompt(
                            student_input=text,
                            open_question_stem=open_question_stem,
                        ),
                    },
                ],
                system_prompt=_INTENT_SYSTEM_PROMPT,
                max_tokens=80,
            )
            payload = _safe_json_loads(response.content or "")
        except Exception as exc:
            logger.warning(
                "[IntentClassifier] LLM call raised %s — defaulting to attempting",
                type(exc).__name__,
            )
            if span is not None:
                span["payload"] = {
                    "intent": INTENT_ATTEMPTING,
                    "outcome": "fail_soft",
                    "reason": type(exc).__name__,
                }
            return INTENT_ATTEMPTING

        if not isinstance(payload, dict):
            if span is not None:
                span["payload"] = {
                    "intent": INTENT_ATTEMPTING,
                    "outcome": "fail_soft",
                    "reason": "non_dict_payload",
                }
            return INTENT_ATTEMPTING

        raw = str(payload.get("intent", "")).strip().lower()
        if raw not in _ALLOWED_INTENTS:
            if span is not None:
                span["payload"] = {
                    "intent": INTENT_ATTEMPTING,
                    "outcome": "fail_soft",
                    "reason": "unknown_intent",
                    "raw": raw,
                }
            return INTENT_ATTEMPTING

        if span is not None:
            span["payload"] = {"intent": raw, "outcome": "ok"}
        return raw


def intent_to_move(intent: str) -> Optional[str]:
    """Map an intent to the move the engine should override to.

    Returns ``None`` when the verdict-driven move selection should
    proceed unchanged (``attempting`` / ``meta``).
    """
    if intent == INTENT_ASKING_HELP_EXAMPLE:
        return "worked_example"
    if intent == INTENT_ASKING_HELP_EXPLAIN:
        return "explain"
    return None


def _safe_json_loads(text: str):
    """Best-effort JSON parse — strips fences / extracts the first object."""
    if not text:
        return None
    raw = text.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\s*", "", raw)
        if raw.endswith("```"):
            raw = raw[:-3]
        raw = raw.strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(raw[start : end + 1])
            except json.JSONDecodeError:
                return None
        return None
