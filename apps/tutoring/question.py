"""Unified Question abstraction (task #190, 2026-05-17).

The tutor used to dispatch on a string `kind` field
(`lesson_step` / `exit_ticket_question` / `inline_authored` /
`inline_mcq` chat-bootstrap) across many functions:
`_build_active_bank_question_block`, `_grade_against_last_bank_question`,
`_build_bank_grade_signal_block`, `_build_hint_calibration_block`,
`_build_regen_bank_context`. Each variant had its own metadata shape,
lookup path, and field availability. Result: 4× the code, 4× the bugs
(tasks #176, #181, #189 all root from this).

This module defines ONE `Question` dataclass with all the common fields
every variant might have. Adapters in each branch produce a `Question`;
all downstream rendering / grading consumes the same shape — no
kind-dispatch needed downstream.

Usage from the engine:

    from apps.tutoring.question import Question
    q = Question.from_engine_state(self._awaiting_answer)
    if q is not None:
        # render hint block, score regen, etc. — all uniform
        block = q.render_active_block(status='answered_wrong', difficulty_level=0)

User directive 2026-05-17: "I think we should abstract a question as
an object. Whether it is from the bank, warmup, or conceptual, [it]
should have all the fields or attributes that the bank questions have
so the tutor can just deal with a question instead of dealing with
different kinds of questions."
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# Question source — analytic tag, not a dispatch axis. The engine
# treats all sources uniformly; this is just for telemetry + audit
# trails so we can see WHERE a question came from in metrics.
SOURCE_LESSON_STEP = "lesson_step"
SOURCE_EXIT_TICKET = "exit_ticket_question"
SOURCE_INLINE_AUTHORED = "inline_authored"  # tutor used pose_inline_question tool
SOURCE_CHAT_AUTHORED = "chat_authored"      # tutor typed a question in prose
SOURCE_FIGURE = "figure_question"           # tutor asked about an attached figure
                                            #   — behaves like chat-authored (no
                                            #   built-in canonical, grounded grading)


@dataclass
class Question:
    """A single question the student is being asked.

    All fields are optional or default to empty so any source can
    construct one with whatever data it has. Downstream code reads via
    attribute access without needing to check the source.
    """

    # Identity / provenance ----------------------------------------------
    source: str = ""              # one of SOURCE_* constants
    source_id: Optional[int] = None  # DB id when applicable

    # Core fields ---------------------------------------------------------
    stem: str = ""                # the question text shown to the student
    question_type: str = "short_answer"  # mcq | short_answer | fill_in_blank | true_false | numeric | matching

    # MCQ-specific (empty for non-MCQ) -----------------------------------
    options: Dict[str, str] = field(default_factory=dict)  # {'A': '...', 'B': '...', ...}
    correct_answer: str = ""      # canonical letter for MCQ ("B"), text for short_answer

    # Free-form answer fields --------------------------------------------
    expected_answer: str = ""     # alt phrasing of the canonical answer text
    explanation: str = ""         # canonical why-it's-correct walkthrough
    answer_key: str = ""          # same as correct_answer for inline-authored
    working: str = ""             # canonical step-by-step (math)
    alternatives: List[str] = field(default_factory=list)  # acceptable variants

    # Engine state -------------------------------------------------------
    wrong_attempts: int = 0       # running count of wrong tries on THIS Q
    turn_index: Optional[int] = None  # conversation index where posed
    posed_at: str = ""            # ISO timestamp

    # ---------------------------------------------------------------
    # Construction adapters — one per source. Engine code calls these
    # instead of building Question dicts inline. Adding a new question
    # source means adding ONE new classmethod here.
    # ---------------------------------------------------------------

    @classmethod
    def from_lesson_step(cls, step) -> "Question":
        """Adapter for `apps.curriculum.models.LessonStep`."""
        if step is None:
            return cls()
        # LessonStep has expected_answer for short types; doesn't usually
        # have option_a/b/c/d (that's exit-ticket territory). Some lesson
        # steps with answer_type='mcq' do have choices on the JSON field.
        opts: Dict[str, str] = {}
        choices = getattr(step, 'choices', None) or {}
        if isinstance(choices, dict):
            for letter in ('A', 'B', 'C', 'D'):
                val = choices.get(letter) or choices.get(letter.lower())
                if val:
                    opts[letter] = str(val)[:200]
        qtype = (getattr(step, 'answer_type', '') or '').lower() or (
            getattr(step, 'step_type', '') or ''
        ).lower() or "short_answer"
        return cls(
            source=SOURCE_LESSON_STEP,
            source_id=getattr(step, 'id', None),
            stem=(step.question or step.teacher_script or "")[:600],
            question_type=qtype if qtype in (
                'mcq', 'short_answer', 'fill_in_blank',
                'true_false', 'numeric', 'matching',
            ) else 'short_answer',
            options=opts,
            expected_answer=(step.expected_answer or "")[:300],
        )

    @classmethod
    def from_exit_ticket(cls, q) -> "Question":
        """Adapter for `apps.tutoring.models.ExitTicketQuestion`."""
        if q is None:
            return cls()
        qtype = (q.question_type or 'mcq').lower()
        opts: Dict[str, str] = {}
        if qtype == 'mcq':
            for letter, attr in (
                ('A', 'option_a'), ('B', 'option_b'),
                ('C', 'option_c'), ('D', 'option_d'),
            ):
                val = getattr(q, attr, None)
                if val:
                    opts[letter] = str(val)[:200]
        return cls(
            source=SOURCE_EXIT_TICKET,
            source_id=q.id,
            stem=(q.question_text or "")[:600],
            question_type=qtype,
            options=opts,
            correct_answer=(q.correct_answer or "").strip(),
            explanation=(q.explanation or "")[:600],
        )

    @classmethod
    def from_inline_authored(cls, ia: Dict[str, Any]) -> "Question":
        """Adapter for the `inline_authored_question` metadata block
        emitted by the pose_inline_question tool (carries an answer_key)."""
        if not ia:
            return cls()
        qtype = (ia.get('question_type') or 'short_answer').lower()
        return cls(
            source=SOURCE_INLINE_AUTHORED,
            stem=(ia.get('question') or "")[:600],
            question_type=qtype,
            answer_key=(ia.get('answer_key') or "")[:200],
            correct_answer=(ia.get('answer_key') or "").strip(),
            working=(ia.get('working') or "")[:600],
            alternatives=list(ia.get('alternatives') or [])[:8],
        )

    @classmethod
    def from_figure_question(
        cls,
        stem: str,
        *,
        figure_url: str = "",
        figure_caption: str = "",
        derived_answer: str = "",
    ) -> "Question":
        """Adapter for "look at this figure and answer..." questions.
        Treated like chat-authored — no built-in canonical, grounded
        LLM grading (the grader sees the figure caption + stem +
        student response). When a derived_answer was computed by the
        figure_vision judge, pass it in so the leak detector + W2
        forbidden list have something concrete to match against.
        """
        q = cls(
            source=SOURCE_FIGURE,
            stem=(stem or "")[:600],
            question_type="short_answer",
            expected_answer=str(derived_answer or "").strip()[:300],
        )
        # Stash the figure context on explanation so the rendering
        # block includes it for the LLM (Question doesn't have a
        # dedicated figure field — explanation is the natural slot).
        ctx_parts: List[str] = []
        if figure_caption:
            ctx_parts.append(f"Figure caption: {figure_caption[:200]}")
        if figure_url:
            ctx_parts.append(f"Figure URL: {figure_url[:200]}")
        if ctx_parts:
            q.explanation = " | ".join(ctx_parts)
        return q

    @classmethod
    def from_chat_authored(
        cls,
        stem: str,
        *,
        question_type: str = "short_answer",
        derived_answer: str = "",
        options: Optional[Dict[str, str]] = None,
    ) -> "Question":
        """Adapter for the chat-grader bootstrap path — the LLM typed
        a question in prose (no tool call), so we have no metadata but
        the question text + (optionally) a derived expected answer from
        the chat-authored grader (`grade_chat_authored_question`'s
        BankGradeResult.expected when the grounded path produced one).
        """
        return cls(
            source=SOURCE_CHAT_AUTHORED,
            stem=(stem or "")[:600],
            question_type=question_type.lower(),
            options=dict(options or {}),
            expected_answer=str(derived_answer or "").strip()[:300],
        )

    @classmethod
    def from_engine_state(cls, rec: Optional[Dict[str, Any]]) -> Optional["Question"]:
        """Reconstruct a Question from the engine's `_awaiting_answer`
        dict. Returns None when there's no live question.

        Backward-compat with the old kind-dispatch shape: handles
        records that only have `{kind, question_id, ...}` by re-fetching
        from the DB / metadata as needed.
        """
        if not rec or not isinstance(rec, dict):
            return None

        # Preferred path: serialised Question (forward compat once the
        # engine starts writing this shape).
        if rec.get('_q_serialized'):
            return cls(**(rec.get('_q_serialized') or {}))

        kind = rec.get('kind') or ''
        wrong = int(rec.get('wrong_attempts', 0) or 0)
        turn_idx = rec.get('turn_index')
        posed_at = rec.get('posed_at') or ""

        if kind == SOURCE_LESSON_STEP and rec.get('question_id'):
            try:
                from apps.curriculum.models import LessonStep
                step = LessonStep.objects.filter(id=rec['question_id']).first()
                if step is not None:
                    q = cls.from_lesson_step(step)
                    q.wrong_attempts = wrong
                    q.turn_index = turn_idx
                    q.posed_at = posed_at
                    return q
            except Exception:
                pass

        if kind == SOURCE_EXIT_TICKET and rec.get('question_id'):
            try:
                from apps.tutoring.models import ExitTicketQuestion
                etq = ExitTicketQuestion.objects.filter(id=rec['question_id']).first()
                if etq is not None:
                    q = cls.from_exit_ticket(etq)
                    q.wrong_attempts = wrong
                    q.turn_index = turn_idx
                    q.posed_at = posed_at
                    return q
            except Exception:
                pass

        if kind == SOURCE_INLINE_AUTHORED:
            # Two sub-cases:
            # (a) full pose_inline_question metadata on the posing
            #     tutor turn — recover via DB lookup
            # (b) chat-grader bootstrap — stem is on rec.authored_question_text
            stem = (rec.get('authored_question_text') or '').strip()
            if stem:
                q = cls.from_chat_authored(
                    stem,
                    question_type=rec.get('question_type') or 'short_answer',
                )
                q.wrong_attempts = wrong
                q.turn_index = turn_idx
                q.posed_at = posed_at
                return q
            # Otherwise the caller must fall back to turn-metadata
            # lookup with the original turn_index. Return a thin
            # Question that the caller can hydrate further.
            q = cls(
                source=SOURCE_INLINE_AUTHORED,
                question_type=rec.get('question_type') or 'short_answer',
                wrong_attempts=wrong,
                turn_index=turn_idx,
                posed_at=posed_at,
            )
            return q

        if kind == 'inline_mcq':
            # Legacy bootstrap kind for inline MCQ (task #173). Treat
            # as chat-authored — the inline_mcq label was a transient
            # bookkeeping kind from before this abstraction landed.
            stem = (rec.get('authored_question_text') or '').strip()
            if stem:
                q = cls.from_chat_authored(stem, question_type='mcq')
                q.wrong_attempts = wrong
                q.turn_index = turn_idx
                q.posed_at = posed_at
                return q

        return None

    # ---------------------------------------------------------------
    # Convenience predicates / accessors used by rendering code so the
    # callers don't have to know about question_type strings.
    # ---------------------------------------------------------------

    @property
    def is_mcq(self) -> bool:
        return self.question_type == 'mcq' or bool(self.options)

    @property
    def has_canonical(self) -> bool:
        """Do we know the canonical answer programmatically? True for
        bank Qs (DB), inline_authored (has answer_key), and
        chat-authored where the grounded grader derived one."""
        return bool(
            (self.correct_answer or "").strip()
            or (self.answer_key or "").strip()
            or (self.expected_answer or "").strip()
        )

    @property
    def canonical_letter(self) -> str:
        """The correct option letter for MCQ, or "" if not applicable."""
        if not self.is_mcq:
            return ""
        letter = (self.correct_answer or "").strip().upper()
        return letter if letter in ("A", "B", "C", "D") else ""

    @property
    def canonical_text(self) -> str:
        """Canonical answer text — option text for MCQ, expected_answer
        for short, answer_key for inline-authored. Empty when none known."""
        if self.is_mcq and self.canonical_letter:
            return self.options.get(self.canonical_letter, "")
        return (
            self.answer_key.strip()
            or self.correct_answer.strip()
            or self.expected_answer.strip()
        )

    # ---------------------------------------------------------------
    # Rendering — the [ACTIVE QUESTION] system-prompt block. One
    # implementation that handles every source uniformly. The engine
    # appends scaffolding rules + hint calibration on top.
    # ---------------------------------------------------------------

    # ---------------------------------------------------------------
    # Grading — single entry point that picks the right grader based
    # on whether we have a canonical answer. Hides the existing dispatch
    # (grade_bank_response vs grade_chat_authored_question) so the
    # engine just calls q.grade(...) regardless of source.
    # ---------------------------------------------------------------

    def grade(
        self,
        student_input,
        *,
        llm_client=None,
        kb_context: str = "",
        is_math: bool = False,
    ):
        """Grade a student's input against this question.

        Returns a `BankGradeResult` (see apps.tutoring.bank_grader).
        The dispatch:
          - Has canonical answer (bank Qs, inline_authored with key) →
            grade_bank_response (deterministic + LLM batch fallback)
          - No canonical (chat-authored prose Qs) →
            grade_chat_authored_question (grounded LLM with KB + search)
        Both paths return the same BankGradeResult shape.
        """
        from apps.tutoring.bank_grader import (
            BankGradeResult,
            grade_bank_response,
            grade_chat_authored_question,
        )

        if not self.stem.strip():
            return BankGradeResult(is_correct=None, skip_reason="no_question_stem")
        if student_input is None or (isinstance(student_input, str) and not student_input.strip()):
            return BankGradeResult(is_correct=None, skip_reason="no_student_input")

        # Has-key path: build a duck-typed question with the fields
        # grade_bank_response expects (matches ExitTicketQuestion shape).
        if self.has_canonical:
            duck = _DuckQuestion(self)
            return grade_bank_response(
                duck,
                student_input,
                llm_client=llm_client,
                is_math=is_math,
            )

        # No-key path: grounded LLM grader. Falls back to non-grounded
        # batch when llm_client is None / grounding unavailable.
        if llm_client is None:
            return BankGradeResult(
                is_correct=None,
                skip_reason="no_canonical_and_no_llm_client",
            )
        return grade_chat_authored_question(
            self.stem,
            student_input if isinstance(student_input, str) else str(student_input),
            llm_client=llm_client,
            is_math=is_math,
            kb_context=kb_context,
            use_grounding=True,
        )

    # ---------------------------------------------------------------
    # Full active-block rendering (header + status + scaffolding +
    # hint calibration). Replaces the kind-dispatch chain in the
    # engine's _build_active_bank_question_block.
    # ---------------------------------------------------------------

    def render_active_block(
        self,
        *,
        status: str = "awaiting_answer",
        difficulty_level: int = 0,
        reveal_threshold: int = 3,
        hint_calibration: Optional[str] = None,
    ) -> str:
        """Render the full [ACTIVE QUESTION] system-prompt block.

        Args:
          status: 'awaiting_answer' | 'answered_correct' | 'answered_wrong'
          difficulty_level: -2 (easiest) … +2 (hardest). Used by the
            caller's hint-calibration helper (passed in as the
            `hint_calibration` string to avoid coupling Question to
            the engine's helper module).
          reveal_threshold: number of wrong attempts at which move-on
            is allowed (no reveal). Caller passes from
            `self._reveal_threshold()`.
          hint_calibration: pre-rendered hint-calibration block from
            the engine. Appended verbatim when present.

        Returns the full block as a single string (with leading newline
        for cleanly appending to the system prompt) — empty when the
        question is empty.
        """
        if not self.stem.strip():
            return ""

        lines = self.render_active_block_header()
        lines.append(f"  student_status: {status}")

        # Status-driven scaffolding rules. Unchanged from the original
        # engine code; just consumed via Question now.
        reveal_allowed = (status == 'answered_wrong' and self.wrong_attempts >= reveal_threshold)
        rules = {
            'awaiting_answer': (
                "Scaffolding: HINT ONLY — never reveal the correct "
                "option letter or the answer text in this turn. "
                "Don't ask the student to explain their reasoning "
                "yet — let them answer first. If they're stuck, "
                "give ONE hint that narrows the choices or names "
                "the concept being tested. Reference options "
                "indirectly without re-stating the stem."
            ),
            'answered_correct': (
                "Scaffolding: the student got it RIGHT. Confirm "
                "briefly + explain WHY using the explanation field "
                "above. NEVER ask 'let's check that', NEVER ask the "
                "student to show working, NEVER probe their reasoning. "
                "The verified answer key tells you it's correct."
            ),
            'answered_wrong': (
                "Scaffolding: the student answered INCORRECTLY "
                f"(wrong_attempts: {self.wrong_attempts}). DO NOT "
                "REVEAL the correct option letter or paraphrase the "
                "correct answer text. Acknowledge gently ('not quite' / "
                "'close, but think about…'), point at the concept "
                "they missed (use the explanation field to inform "
                "the hint, but rephrase as a clue not a giveaway), "
                "and invite them to try again. ONE short probe is OK "
                "('what made you pick that?'). Let them attempt the "
                "question again."
                + (
                    f"\n  ↳ MOVE-ON TO SAME-CONCEPT VARIANT this turn: "
                    f"student has missed this question {reveal_threshold}+ "
                    "times — do NOT reveal the answer. REQUIRED STEPS:\n"
                    "    1. EXPLAIN the underlying concept clearly in "
                    "2-4 sentences with a concrete EXAMPLE. The student "
                    "missed this question multiple times — they need the "
                    "IDEA taught, not just a hint pointer. Use the "
                    "explanation field above to ground your teach-back, "
                    "but rephrase in your own words. Do NOT mention the "
                    "answer text or option letter.\n"
                    "    2. THEN re-quiz the SAME concept from a "
                    "DIFFERENT ANGLE — different format (short-answer "
                    "instead of MCQ), different scenario/example, or "
                    "different wording. The new question MUST test the "
                    "SAME underlying idea — do NOT jump to a different "
                    "concept or unrelated topic. If no bank slot fits, "
                    "author the variant in prose (the chat-grader will "
                    "handle it).\n"
                    "    3. Keep BOTH the explanation and the new "
                    "question in ONE response (or use the move-on bubble "
                    "split: ack+explanation in bubble A, new question in "
                    "bubble B)."
                    if reveal_allowed else ""
                )
            ),
        }
        lines.append("")
        lines.append(rules.get(status, rules['awaiting_answer']))

        if hint_calibration:
            lines.append(hint_calibration)

        return "\n".join(lines)

    def render_active_block_header(self) -> List[str]:
        """Return the header + identity lines for the [ACTIVE QUESTION]
        block. Caller appends status + scaffolding + hint calibration.
        """
        # Source-aware header so logs / dashboards can see provenance,
        # but the BODY shape is uniform.
        header_label = {
            SOURCE_LESSON_STEP: "[ACTIVE BANK QUESTION — lesson step]",
            SOURCE_EXIT_TICKET: "[ACTIVE BANK QUESTION — exit ticket pool]",
            SOURCE_INLINE_AUTHORED: "[ACTIVE INLINE-AUTHORED QUESTION]",
            SOURCE_CHAT_AUTHORED: "[ACTIVE CHAT-AUTHORED QUESTION]",
            SOURCE_FIGURE: "[ACTIVE FIGURE QUESTION]",
        }.get(self.source, "[ACTIVE QUESTION]")

        lines: List[str] = [
            header_label,
            "A question is awaiting an answer. The student sees the",
            "question rendered in the chat / artifact panel. Do NOT",
            "re-author the question stem in your reply.",
            "",
            f"  source: {self.source or '(unknown)'}",
        ]
        if self.source_id is not None:
            lines.append(f"  source_id: {self.source_id}")
        lines.append(f"  question_type: {self.question_type}")
        lines.append(f"  stem: {self.stem[:600]}")
        if self.options:
            lines.append("  options:")
            for letter in sorted(self.options.keys()):
                lines.append(f"    {letter}: {self.options[letter][:200]}")
        if self.canonical_letter:
            lines.append(f"  correct_answer: {self.canonical_letter}")
        elif self.has_canonical:
            lines.append(f"  canonical_answer: {self.canonical_text[:200]}")
        if self.explanation:
            lines.append(f"  explanation: {self.explanation[:400]}")
        if self.working:
            lines.append(f"  reference_working: {self.working[:300]}")
        return lines


class _DuckQuestion:
    """Minimal adapter that makes a `Question` quack like the
    `ExitTicketQuestion` model that `grade_bank_response` expects.

    grade_bank_response reads: question_type, correct_answer,
    option_a/b/c/d, answer_data, question_text. We mirror exactly
    those fields from the Question's data.
    """

    def __init__(self, q: "Question"):
        self.question_type = q.question_type
        self.correct_answer = q.canonical_letter or (
            q.answer_key or q.correct_answer or q.expected_answer
        )
        # MCQ options: map letter → text. None for non-MCQ.
        self.option_a = q.options.get('A', '') if q.is_mcq else ''
        self.option_b = q.options.get('B', '') if q.is_mcq else ''
        self.option_c = q.options.get('C', '') if q.is_mcq else ''
        self.option_d = q.options.get('D', '') if q.is_mcq else ''
        # answer_data: bag for short_answer / fib / matching keywords +
        # alternatives + canonical working.
        ad: Dict[str, Any] = {}
        if q.alternatives:
            ad['keywords'] = list(q.alternatives)
        if q.expected_answer and not q.is_mcq:
            ad['model_answer'] = q.expected_answer
        if q.working:
            ad['canonical_working'] = q.working
        self.answer_data = ad
        self.question_text = q.stem
        self.expected_answer = q.expected_answer
        self.explanation = q.explanation
