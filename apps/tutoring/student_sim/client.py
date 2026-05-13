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

from apps.llm.client import get_llm_client, LLMResponse
from apps.llm.models import ModelConfig
from apps.tutoring.student_sim.personas import Persona, get_persona


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

    def next_reply(self, tutor_msg: str) -> str:
        """Get the persona's reply to one tutor message.

        Updates the in-memory history. Returns the bare reply text
        (no role prefix, no quotes).
        """
        self._history.append(StudentTurn(role='tutor', content=tutor_msg))
        messages = self._build_messages()
        response = self.client.generate(
            messages=messages,
            system_prompt=self.persona.system_prompt,
            max_tokens=self.model_config.max_tokens,
            temperature=self.persona.temperature,
        )
        reply = (response.content or '').strip()
        # Strip a leading "Student:" prefix if the model adds one despite
        # the system prompt telling it not to. Also strip surrounding
        # quotes for the same reason.
        if reply.lower().startswith('student:'):
            reply = reply.split(':', 1)[1].strip()
        if len(reply) >= 2 and reply[0] == reply[-1] and reply[0] in ('"', "'"):
            reply = reply[1:-1].strip()
        self._history.append(StudentTurn(role='student', content=reply))
        self.last_response = response
        return reply

    def _build_messages(self) -> list[dict]:
        """Build the role-flipped message list for the LLM call.

        From the student-LLM's perspective:
        - Tutor utterances = `user` messages (incoming).
        - Student utterances = `assistant` messages (own prior outputs).
        """
        out: list[dict] = []
        for turn in self._history:
            role = 'user' if turn.role == 'tutor' else 'assistant'
            out.append({'role': role, 'content': turn.content})
        return out

    def reset(self) -> None:
        """Clear conversation history. Useful between sessions."""
        self._history.clear()
        self.last_response = None
