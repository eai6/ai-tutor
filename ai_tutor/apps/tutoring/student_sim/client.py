"""StudentClient — wraps a BaseLLMClient with a persona system prompt.

Thin wrapper around `apps.llm.client.get_llm_client()`. Pulls the active
ModelConfig with `Purpose.STUDENT_SIM`, applies the persona's system
prompt and temperature override, exposes `next_reply(tutor_msg, history)`.

The session driver (Phase 2) holds an instance per session and calls
`next_reply` once per tutor turn.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import logging
import re

from ai_tutor.apps.llm.client import get_llm_client, LLMResponse, call_with_transient_retry
from ai_tutor.apps.llm.models import ModelConfig
from ai_tutor.apps.tutoring.student_sim.personas import Persona, get_persona

logger = logging.getLogger(__name__)


# Placeholder substituted for empty message content before the history
# is sent to the LLM. The Anthropic API rejects messages with
# empty/whitespace-only content with:
#   400 invalid_request_error: messages.N: user messages must have
#   non-empty content
# Observed across all 5 A/B cycles (v3-v7) when the tutor (especially
# Gemini Flash/Pro) emits a turn that's only a tool call with no prose
# (chars=0 tool_use_count=0 bank_rendered=False stop_reason=end_turn),
# which lands in history as content="". A later turn's full-history
# call then 400s on that historical empty message. Same applies to
# empty student replies on the assistant side.
#
# Substituting a short placeholder keeps the conversation well-formed
# (alternating user/assistant ordering preserved) AND gives the LLM a
# semantically reasonable read of the empty turn.
_EMPTY_USER_PLACEHOLDER = "(no message)"
_EMPTY_ASSISTANT_PLACEHOLDER = "..."


@dataclass
class StudentTurn:
    """One turn in the synthetic conversation, from the student's POV.

    Used as the in-memory history the StudentClient sends back to the
    LLM. Distinct from `SessionTurn` (the DB-side tutor record) because
    the LLM needs role-flipped messages: tutor's text is the
    student's "user" (incoming), student's text is the "assistant"
    (outgoing).
    """
    role: str  # 'tutor' or 'student'
    content: str


_PICKER_LETTER_RX = re.compile(r'\b([A-D])\b')


def _picker_letters(answer_choices: dict | None) -> list[str]:
    """The letters the buttons actually offer, or [] when there is no picker.

    Reads the engine's payload shape: {'letters': [{'letter': 'A', 'text': ...}]}.
    Returns [] for a malformed or empty payload so the caller falls back to
    free text rather than constraining the student to nothing.
    """
    if not isinstance(answer_choices, dict):
        return []
    out = []
    for item in (answer_choices.get('letters') or []):
        if isinstance(item, dict):
            L = str(item.get('letter') or '').strip().upper()
            if L in {'A', 'B', 'C', 'D'}:
                out.append(L)
    return out


def _picker_instruction(letters: list[str]) -> str:
    """The appended constraint. Deliberately says WHY, not just what: personas
    are told elsewhere to explain their reasoning, and a bare "reply with one
    letter" fights that instruction instead of scoping it."""
    opts = ', '.join(letters)
    return (
        "\n\nThis question is multiple choice and you are answering on a device "
        f"that shows you only four buttons: {opts}. There is no text box. "
        "Reply with exactly one letter and nothing else — no explanation, no "
        "punctuation, no restating the option. Choose the way this persona "
        "would choose: if you would have got it wrong in writing, pick the "
        "wrong letter here too."
    )


def _coerce_to_letter(reply: str, letters: list[str]) -> str:
    """Force the reply down to one offered letter.

    A sim that ignores the instruction and types prose would otherwise reach
    the tutor as free text, silently turning a picker session into a
    free-text one. Falls back to the first offered letter rather than to
    free text: a real picker UI cannot send anything else, and a wrong letter
    is a truthful thing for a student to send while a sentence is not.
    """
    up = (reply or '').strip().upper()
    if up in letters:
        return up
    m = _PICKER_LETTER_RX.search(up)
    if m and m.group(1) in letters:
        return m.group(1)
    logger.warning(
        "[StudentSim] picker reply %r had no offered letter; coerced to %s",
        reply[:60], letters[0],
    )
    return letters[0]


class StudentClient:
    """LLM-backed synthetic student.

    Initialize once per session with a persona. Call `next_reply` for each
    tutor turn. The client maintains its own in-memory history and
    role-flips messages so the LLM sees itself as the assistant
    (the student) responding to user messages (the tutor).
    """

    def __init__(self, persona_key: str, *,
                 institution_id: Optional[int] = None,
                 model_config: Optional[ModelConfig] = None):
        self.persona: Persona = get_persona(persona_key)
        self._history: list[StudentTurn] = []
        self.last_response: Optional[LLMResponse] = None

        if model_config is None:
            model_config = self._resolve_model_config(institution_id)
        self.model_config = model_config
        self.client = get_llm_client(model_config)

    @staticmethod
    def _resolve_model_config(institution_id: Optional[int]) -> ModelConfig:
        """Pick the active STUDENT_SIM ModelConfig for the institution.

        Per CLAUDE.md multi-tenancy: prefer the institution-scoped config;
        fall back to a platform-wide one. If nothing is configured at all,
        raises so the failure is loud (the simulator can't run blind).
        """
        qs = ModelConfig.objects.filter(
            purpose=ModelConfig.Purpose.STUDENT_SIM,
            is_active=True,
        )
        if institution_id is not None:
            scoped = qs.filter(institution_id=institution_id).first()
            if scoped is not None:
                return scoped
        # Fall back to any active STUDENT_SIM config.
        any_config = qs.first()
        if any_config is None:
            raise RuntimeError(
                "No active ModelConfig with purpose='student_sim' found. "
                "Run `python manage.py migrate llm` to seed defaults, "
                "or create one manually in admin."
            )
        return any_config

    def next_reply(self, tutor_msg: str, answer_choices: dict | None = None) -> str:
        """Get the persona's reply to one tutor message.

        Updates the in-memory history. Returns the bare reply text
        (no role prefix, no quotes).

        ``answer_choices`` is the engine's ``answer_choices`` payload for this
        turn — present only when the A-D buttons are the student's ONLY input
        (see engine._uses_answer_picker). When it is present the reply is
        constrained to a single letter, because that is all the real UI can
        send. Letting the sim type a sentence there would measure an interface
        the deployment does not have, and would hand the grader free-text
        signal a button-tapping student could never produce.
        """
        # Empty-content guard at the append site. If the tutor emits
        # an empty turn (Gemini Flash/Pro does this on tool-only
        # responses with no prose), substitute a placeholder so a
        # later call's full-history payload doesn't 400 on Anthropic.
        # See module-level note on _EMPTY_USER_PLACEHOLDER.
        if not (tutor_msg or '').strip():
            logger.warning(
                "[StudentSim] empty tutor_msg substituted with placeholder"
            )
            tutor_msg = _EMPTY_USER_PLACEHOLDER
        self._history.append(StudentTurn(role='tutor', content=tutor_msg))
        messages = self._build_messages()
        letters = _picker_letters(answer_choices)
        system_prompt = self.persona.system_prompt
        if letters:
            # Appended, not substituted: the persona still decides WHICH letter
            # (a struggler still picks badly), it just cannot answer in prose.
            system_prompt = system_prompt + _picker_instruction(letters)
        response = call_with_transient_retry(lambda: self.client.generate(
            messages=messages,
            system_prompt=system_prompt,
            max_tokens=self.model_config.max_tokens,
            temperature=self.persona.temperature,
        ), label='student_sim')
        reply = (response.content or '').strip()
        if letters:
            reply = _coerce_to_letter(reply, letters)
        # Strip a leading "Student:" prefix if the model adds one despite
        # the system prompt telling it not to. Also strip surrounding
        # quotes for the same reason.
        if reply.lower().startswith('student:'):
            reply = reply.split(':', 1)[1].strip()
        if len(reply) >= 2 and reply[0] == reply[-1] and reply[0] in ('"', "'"):
            reply = reply[1:-1].strip()
        # Empty-reply guard. Same reason as the tutor_msg guard above
        # but on the assistant side -- some student-sim turns return
        # zero-char content (rare but observed in Gemini-tutor cells)
        # and that lands in history as content="" which 400s on the
        # next call.
        if not reply:
            logger.warning(
                "[StudentSim] empty student reply substituted with placeholder"
            )
            reply = _EMPTY_ASSISTANT_PLACEHOLDER
        self._history.append(StudentTurn(role='student', content=reply))
        self.last_response = response
        return reply

    def _build_messages(self) -> list[dict]:
        """Build the role-flipped message list for the LLM call.

        From the student-LLM's perspective:
        - Tutor utterances = `user` messages (incoming).
        - Student utterances = `assistant` messages (own prior outputs).

        Defense in depth: substitute placeholders for any empty content
        that slipped past the append-site guards. The Anthropic API
        rejects empty content with a 400 (see module-level note).
        """
        out: list[dict] = []
        for turn in self._history:
            role = 'user' if turn.role == 'tutor' else 'assistant'
            content = turn.content if (turn.content or '').strip() else (
                _EMPTY_USER_PLACEHOLDER if role == 'user'
                else _EMPTY_ASSISTANT_PLACEHOLDER
            )
            out.append({'role': role, 'content': content})
        return out

    def reset(self) -> None:
        """Clear conversation history. Useful between sessions."""
        self._history.clear()
        self.last_response = None
