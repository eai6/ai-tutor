"""Parse the production chat transcripts in `memory/transcripts.md` and
replay every student turn through the NEW per-domain split judges,
using the same `judge_client` production resolves at runtime.

For each turn we recover:
  - the question being asked (from the tutor message before the student spoke)
  - the student's reply
  - the tutor's response to that reply
  - the OLD combined_judge verdict (✓ / ✗) from the metadata line

Then we run `run_combined_judge` (which delegates to the split judges)
and report:

  NEW vs OLD verdict, the eval source (deterministic vs LLM), any
  arithmetic/rule findings, and (when `--diff-only`) only the turns
  where NEW disagrees with OLD.

Run:
  python scripts/replay_transcripts_md.py
  python scripts/replay_transcripts_md.py --diff-only
  python scripts/replay_transcripts_md.py --max 20

Note: this calls a real Anthropic API for every turn that doesn't
deterministically short-circuit. ~30-60 turns × ~3 LLM calls each.
Set --max to limit while iterating.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
except ImportError:
    pass

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from apps.tutoring.combined_judge import run_combined_judge


TRANSCRIPT_PATH = (
    Path(__file__).resolve().parents[1] / "memory" / "transcripts.md"
)


# ----- Parsing -----
# The file is a sequence of chats. Each chat is a sequence of turns.
# A turn is one of:
#   - "Tutor" line followed by tutor message lines
#   - "<Student Name>" line followed by student message lines
# Each tutor turn may be followed (after the message) by a metadata
# block:
#   ✓ correct | ✗ incorrect          ← verdict on the PRIOR student input
#   via combined_judge | llm_evaluator
#   <flag tokens, one per line>
#   why?
# Then a "HH:MM" timestamp line, then the next role.
#
# We treat the metadata block as belonging to the tutor turn that
# precedes it — but the verdict is judging the STUDENT input that
# preceded the tutor turn.

VERDICT_LINE = re.compile(r"^[✓✗]\s+(correct|incorrect)\s*$")
TIME_LINE = re.compile(r"^\d{1,2}:\d{2}\s*$")
KNOWN_FLAGS = {
    "via combined_judge", "via llm_evaluator", "via keyword_fallback",
    "via deterministic_numeric", "via deterministic_mcq",
    "no working", "with working", "bare answer",
    "rule1_violation", "authoring_violation", "arithmetic_violation",
    "info_dump_warning", "no_question", "figure_ref_without_signal",
    "numeric_claim_unverified", "flagged",
    "complete ✓", "partial ➜",
    "why?",
}


@dataclass
class Turn:
    role: str  # "tutor" | "student"
    speaker: str
    text: str
    verdict: Optional[bool] = None  # the OLD verdict on the PRIOR student input
    eval_layer: Optional[str] = None
    flags: List[str] = field(default_factory=list)


@dataclass
class Chat:
    chat_index: int
    turns: List[Turn] = field(default_factory=list)


def _is_role_line(line: str) -> Optional[str]:
    """Return 'tutor' if line == 'Tutor', else 'student' if it looks
    like a student name (single line, capitalized words, length < 60),
    else None.

    The transcripts use plain student names like "Samanthi
    Mahatantilage" or "OMARI TENGEZA" — heuristic: 1-4 words, alpha,
    not a known control token.
    """
    s = line.strip()
    if s == "Tutor":
        return "tutor"
    if not s or s.startswith("✓") or s.startswith("✗"):
        return None
    if VERDICT_LINE.match(s) or TIME_LINE.match(s):
        return None
    if s in KNOWN_FLAGS or s.startswith(("via ", "1 ", "Unverified", "(")):
        return None
    # Likely student name: 1-4 alpha-ish words, total length < 60
    parts = s.split()
    if 1 <= len(parts) <= 4 and len(s) < 60:
        if all(re.match(r"^[A-Za-z][A-Za-z\.\-']*$", p) for p in parts):
            return "student"
    return None


def parse_transcripts(path: Path) -> List[Chat]:
    raw = path.read_text(encoding="utf-8").splitlines()
    chats: List[Chat] = []
    current_chat: Optional[Chat] = None
    current_turn: Optional[Turn] = None
    chat_index = 0

    def _flush_turn():
        nonlocal current_turn
        if current_turn is not None and current_chat is not None:
            current_turn.text = current_turn.text.strip()
            if current_turn.text:
                current_chat.turns.append(current_turn)
        current_turn = None

    i = 0
    while i < len(raw):
        line = raw[i]
        s = line.strip()

        role = _is_role_line(line)

        if role is not None:
            # Start a new turn. If this is "Tutor" and the previous
            # role was also "tutor" (after a metadata block), still
            # start a fresh turn — multiple tutor turns can stack.
            #
            # If we see "Tutor" at the start of a fresh paragraph and
            # the last chat ended (no current_turn), start a new chat.
            if role == "tutor" and (
                current_chat is None
                or (
                    not current_chat.turns
                    or (
                        current_chat.turns
                        and current_chat.turns[-1].role == "tutor"
                        and i > 0 and raw[i - 1].strip() == ""
                        and i > 1 and raw[i - 2].strip() == ""
                    )
                )
            ):
                if current_chat is None:
                    chat_index += 1
                    current_chat = Chat(chat_index=chat_index)
                    chats.append(current_chat)
                # If the prior chat is "stale" (lots of empty lines +
                # Tutor restart), open a new chat.
                elif (
                    i >= 5
                    and all(raw[k].strip() == "" for k in range(i - 5, i))
                ):
                    _flush_turn()
                    chat_index += 1
                    current_chat = Chat(chat_index=chat_index)
                    chats.append(current_chat)

            _flush_turn()
            current_turn = Turn(role=role, speaker=s, text="")
            i += 1
            continue

        # Metadata line — verdict / eval / flags / time / why?
        if VERDICT_LINE.match(s):
            verdict = (s.split()[-1] == "correct")
            if current_turn is not None:
                current_turn.verdict = verdict
            i += 1
            continue
        if s.startswith("via "):
            if current_turn is not None:
                current_turn.eval_layer = s[len("via "):].strip()
            i += 1
            continue
        if s in KNOWN_FLAGS or s.startswith("Unverified") or (
            s.endswith("fact-checked") or s.endswith("step")
            or s.endswith("steps")
        ):
            if current_turn is not None:
                current_turn.flags.append(s)
            i += 1
            continue
        if TIME_LINE.match(s):
            i += 1
            continue
        if s == "":
            i += 1
            continue

        # Default: append to current turn body
        if current_turn is not None:
            current_turn.text += line + "\n"
        i += 1

    _flush_turn()
    return chats


# ----- Replay -----

@dataclass
class ReplayCase:
    chat_idx: int
    student_name: str
    student_input: str
    tutor_response: str
    prior_question: str
    expected_answer_guess: str
    old_verdict: Optional[bool]
    old_eval_layer: Optional[str]
    old_flags: List[str]


def _looks_like_question_line(line: str) -> bool:
    s = line.strip()
    return s.endswith("?") or s.endswith("___°.") or s.endswith("___°")


def _extract_question_from_tutor(text: str) -> str:
    """Pick the most likely question line from a tutor message — the
    last sentence ending in '?' or '___°.'. Falls back to the last
    non-empty line."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    for line in reversed(lines):
        if _looks_like_question_line(line):
            return line[:200]
    if lines:
        return lines[-1][:200]
    return "(no question)"


def _guess_expected_answer(prior_question: str) -> str:
    """Heuristically extract the expected_answer for common question
    patterns in the angles-on-a-straight-line transcripts.

    Patterns:
      - "One angle ... is X°. ... other angle is ___°."  → 180-X
      - "180° - X°" / "subtract X from 180" → 180-X
      - "30° + 62° + w = 180°" → 88
      - "100° + 120° + x = 360°" → 140
      - MCQ "A) 88°" — return "A" when the right value is 88
      - "what is X + Y?" → X+Y
    Returns "" when no confident guess.
    """
    q = prior_question.lower().replace("°", "")

    # MCQ for w with options
    if (
        "30" in q and "62" in q and "w" in q
        and ("a) 88" in q.lower() or "a) 88" in prior_question.lower())
    ):
        return "A"
    # "Find x" with three angles around a point summing to 360
    m = re.search(r"three angles around a point are\s+(\d+).*?(\d+).*?x", q)
    if m:
        try:
            return str(360 - int(m.group(1)) - int(m.group(2)))
        except ValueError:
            pass
    # "One angle on a straight line is X°. ... other angle is ___°."
    m = re.search(r"one angle (?:on a straight line )?is\s+(\d+)", q)
    if m and ("other angle" in q or "missing angle" in q):
        try:
            return str(180 - int(m.group(1)))
        except ValueError:
            pass
    # "Two angles on a straight line are X and Y. What is their sum?"
    m = re.search(r"two angles on a straight line are\s+(\d+).+?(\d+).+?sum", q)
    if m:
        try:
            return str(int(m.group(1)) + int(m.group(2)))
        except ValueError:
            pass
    # "angles are X and Y. What is the sum"
    m = re.search(r"angles are\s+(\d+).+?(\d+).+?sum of the two angles", q)
    if m:
        try:
            return str(int(m.group(1)) + int(m.group(2)))
        except ValueError:
            pass
    # "If one angle is X°, what is the angle adjacent" → 180-X
    m = re.search(r"if one angle is\s+(\d+).+adjacent", q)
    if m:
        try:
            return str(180 - int(m.group(1)))
        except ValueError:
            pass
    # "vertically opposite angle" — equal to the original
    m = re.search(r"one angle.*?is\s+(\d+).+vertically opposite", q)
    if m:
        return m.group(1)
    return ""


def build_cases(chats: List[Chat]) -> List[ReplayCase]:
    cases: List[ReplayCase] = []
    for chat in chats:
        prior_tutor_text = ""
        for idx, turn in enumerate(chat.turns):
            if turn.role != "tutor":
                # Track student turns — but we only build replay cases
                # when the FOLLOWING tutor turn carries a verdict.
                continue
            # The verdict on this tutor turn judges the student turn
            # that came RIGHT BEFORE this one.
            if idx == 0:
                prior_tutor_text = turn.text
                continue
            prev = chat.turns[idx - 1]
            if prev.role != "student":
                prior_tutor_text = turn.text
                continue
            if turn.verdict is None:
                # No metadata — likely the very last tutor turn after
                # the student finished. Skip.
                prior_tutor_text = turn.text
                continue
            question = _extract_question_from_tutor(prior_tutor_text)
            expected = _guess_expected_answer(question)
            cases.append(ReplayCase(
                chat_idx=chat.chat_index,
                student_name=prev.speaker,
                student_input=prev.text.strip(),
                tutor_response=turn.text,
                prior_question=question,
                expected_answer_guess=expected,
                old_verdict=turn.verdict,
                old_eval_layer=turn.eval_layer,
                old_flags=list(turn.flags),
            ))
            prior_tutor_text = turn.text
    return cases


def _build_step_context(case: ReplayCase) -> dict:
    deterministic_verdict = None
    deterministic_source = ""
    expected = case.expected_answer_guess
    if expected:
        try:
            from apps.tutoring.grader import check_math_answer
            # Strip degree symbols + whitespace — `check_math_answer`'s
            # numeric extractor doesn't tolerate °, so "90°" vs "90"
            # would otherwise fall through to None.
            stripped = (case.student_input or "").replace("°", "").strip()
            r = check_math_answer(stripped, expected)
            if r is not None and r.is_correct is not None:
                deterministic_verdict = bool(r.is_correct)
                deterministic_source = "numeric"
        except Exception:
            pass
        if (
            deterministic_verdict is None
            and expected.upper() in ("A", "B", "C", "D")
        ):
            m = re.match(
                r"^[\(\[]?\s*([A-D])\s*[\)\]\.]*\s*$",
                case.student_input.strip(), re.IGNORECASE,
            )
            if m:
                deterministic_verdict = (
                    m.group(1).upper() == expected.upper()
                )
                deterministic_source = "mcq_letter"
    return {
        "step_type": "practice",
        "step_index": 0,
        "exchanges_on_this_step": 1,
        "completion_criteria": (
            "Complete when the student gives the FINAL correct answer "
            "to the posed question."
        ),
        "expected_answer": expected,
        "teacher_script_excerpt": "",
        "format_hint": (
            "Answer format: short_numeric." if expected
            and expected.upper() not in ("A", "B", "C", "D")
            else "Answer format: multiple_choice."
            if expected.upper() in ("A", "B", "C", "D")
            else "Answer format: free_text."
        ),
        "recent_conversation": (
            f"TUTOR: {case.prior_question}\n"
            f"STUDENT: {case.student_input}"
        ),
        "deterministic_verdict": deterministic_verdict,
        "deterministic_source": deterministic_source,
    }


def _resolve_judge_client():
    from apps.llm.models import ModelConfig
    from apps.tutoring.conversational_tutor import ConversationalTutor
    from apps.tutoring.models import TutorSession

    judge_cfg = ModelConfig.objects.filter(
        purpose=ModelConfig.Purpose.JUDGE, is_active=True,
    ).first()
    if judge_cfg is not None:
        print(
            f"[judge] PROD JUDGE config: institution={judge_cfg.institution.name} "
            f"provider={judge_cfg.provider} model={judge_cfg.model_name}"
        )
    session = (
        TutorSession.objects.select_related("lesson").order_by("-started_at").first()
    )
    if session is None:
        raise SystemExit("No TutorSession in local DB.")
    tutor = ConversationalTutor(session)
    return tutor.judge_client


def _v(v):
    return "✓" if v is True else ("✗" if v is False else "∅")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--max", type=int, default=None,
                        help="Limit to N turns (for fast iteration)")
    parser.add_argument("--diff-only", action="store_true",
                        help="Only print turns where NEW disagrees with OLD")
    parser.add_argument("--chat", type=int, default=None,
                        help="Only run a single chat (1-based index)")
    parser.add_argument("--md", type=str, default=None,
                        help="Write a markdown audit file with the (NEW vs OLD) "
                             "disagreements at the given path")
    args = parser.parse_args()

    chats = parse_transcripts(TRANSCRIPT_PATH)
    print(f"Parsed {len(chats)} chat(s):")
    for c in chats:
        n_student = sum(1 for t in c.turns if t.role == "student")
        n_with_verdict = sum(
            1 for t in c.turns if t.role == "tutor" and t.verdict is not None
        )
        print(f"  chat {c.chat_index}: turns={len(c.turns)} "
              f"student={n_student} with_verdict={n_with_verdict}")

    if args.chat is not None:
        chats = [c for c in chats if c.chat_index == args.chat]

    cases = build_cases(chats)
    print(f"\nBuilt {len(cases)} replay cases.")
    if args.max:
        cases = cases[: args.max]
        print(f"  → limiting to first {len(cases)} (per --max)")

    judge_client = _resolve_judge_client()

    n_disagree = 0
    n_old_correct = 0
    n_new_correct = 0
    n_total = 0
    n_skipped = 0
    md_records = []  # (case, ctx, result) for disagreements

    for i, case in enumerate(cases, 1):
        ctx = _build_step_context(case)
        try:
            result = run_combined_judge(
                case.tutor_response,
                lesson=None,
                llm_client=judge_client,
                bank_stems=[case.prior_question[:200]] if case.prior_question else [],
                student_input=case.student_input,
                answer_was_bare=True,
                answer_was_wrong=False,
                step_context=ctx,
                subject_is_math=True,
                bank_offered=True,
            )
        except Exception as e:
            print(f"[case {i}] judge error: {e}")
            n_skipped += 1
            continue

        n_total += 1
        new_v = result.answer_correct
        agree = (new_v == case.old_verdict)
        if not agree:
            n_disagree += 1
            md_records.append((i, case, ctx, result))

        if args.diff_only and agree:
            continue

        # Compact one-block report
        print(f"\n--- chat {case.chat_idx} | turn {i} | {case.student_name} ---")
        print(f"  Q: {case.prior_question[:140]}")
        print(f"  Student: {case.student_input!r}")
        print(f"  Tutor:   {case.tutor_response.strip()[:160].replace(chr(10), ' / ')}…")
        print(f"  expected_guess: {case.expected_answer_guess or '(none)'}  "
              f"det={ctx['deterministic_verdict']} ({ctx['deterministic_source'] or 'none'})")
        print(f"  OLD verdict: {_v(case.old_verdict)} via {case.old_eval_layer or '?'} "
              f"flags={case.old_flags}")
        print(f"  NEW verdict: {_v(new_v)} src={result.step_eval_source!r}  "
              f"arith_corrections={len(result.arithmetic_corrections)}  "
              f"rule_violations={[v.rule for v in result.rule_violations]}")
        if result.arithmetic_corrections:
            for c in result.arithmetic_corrections[:2]:
                print(f"    arith: {c.get('expression')!r} claimed={c.get('claimed')!r} correct={c.get('correct')!r}")
        if result.step_eval_reasoning:
            print(f"    reasoning: {result.step_eval_reasoning[:200]}")
        print(f"  → AGREE with OLD: {'YES' if agree else 'NO'}")

    print(f"\n{'=' * 70}")
    print(f"SUMMARY: {n_total} turns judged, {n_skipped} skipped")
    print(f"  agree with OLD verdict: {n_total - n_disagree}/{n_total}")
    print(f"  disagree with OLD verdict: {n_disagree}/{n_total}")
    print('=' * 70)

    if args.md and md_records:
        out = Path(args.md)
        with out.open("w") as f:
            f.write(_md_audit(md_records, n_total, n_disagree))
        print(f"\nWrote {len(md_records)} disagreement records → {out}")


def _md_audit(records, n_total, n_disagree):
    """Render the disagreement records as a Markdown audit document."""
    lines = []
    lines.append("# Judge audit — NEW (split judges) vs OLD (monolithic combined_judge)")
    lines.append("")
    lines.append(
        f"Replayed every student turn in `memory/transcripts.md` "
        f"({n_total} turns total) through the new per-domain split judges "
        f"using the production JUDGE ModelConfig (Sonnet 4 via "
        f"`tutor.judge_client`). This file lists the **{n_disagree} turns "
        f"where the NEW verdict disagrees with the OLD verdict** so you can "
        f"audit each one and decide whether NEW or OLD is right."
    )
    lines.append("")
    lines.append(
        "Verdict legend: `✓` correct, `✗` incorrect, `∅` null (no signal "
        "either way — student wasn't answering a gradable question)."
    )
    lines.append("")
    lines.append(
        "For each turn we show the question, the student's reply, the "
        "tutor's response, and what each judge concluded. Use the "
        "audit-checkbox column to record your call (NEW better / OLD better / "
        "tie / parser bug)."
    )
    lines.append("")
    lines.append("---")
    lines.append("")

    for idx, (i, case, ctx, result) in enumerate(records, 1):
        lines.append(f"## {idx}. Chat {case.chat_idx} · turn {i} · {case.student_name}")
        lines.append("")
        lines.append("**Question (from prior tutor turn):**")
        lines.append("")
        lines.append("> " + case.prior_question.replace("\n", "\n> "))
        lines.append("")
        lines.append("**Student input:**")
        lines.append("")
        lines.append("```")
        lines.append(case.student_input)
        lines.append("```")
        lines.append("")
        lines.append("**Tutor response:**")
        lines.append("")
        lines.append("```")
        body = case.tutor_response.strip()
        if len(body) > 600:
            body = body[:600] + "…"
        lines.append(body)
        lines.append("```")
        lines.append("")
        lines.append(
            f"**Expected-answer guess (heuristic):** `{case.expected_answer_guess or '(none)'}` · "
            f"deterministic verdict: `{ctx['deterministic_verdict']}` "
            f"({ctx['deterministic_source'] or 'none'})"
        )
        lines.append("")
        lines.append(
            f"**OLD verdict:** {_v(case.old_verdict)}  ·  "
            f"via `{case.old_eval_layer or '?'}`  ·  "
            f"flags: {', '.join(f'`{x}`' for x in case.old_flags) or '(none)'}"
        )
        lines.append("")
        lines.append(
            f"**NEW verdict:** {_v(result.answer_correct)}  ·  "
            f"source: `{result.step_eval_source or 'n/a'}`  ·  "
            f"arith: {len(result.arithmetic_corrections)}  ·  "
            f"rule violations: {[v.rule for v in result.rule_violations] or '(none)'}"
        )
        if result.arithmetic_corrections:
            lines.append("")
            lines.append("Arithmetic findings:")
            for c in result.arithmetic_corrections[:3]:
                lines.append(
                    f"  - `{c.get('expression')}` "
                    f"claimed=`{c.get('claimed')}` "
                    f"correct=`{c.get('correct')}`"
                )
        if result.step_eval_reasoning:
            lines.append("")
            lines.append(f"step_eval reasoning: _{result.step_eval_reasoning}_")
        lines.append("")
        lines.append("**Audit:** `[ ] NEW better   [ ] OLD better   [ ] tie / borderline   [ ] parser bug`")
        lines.append("")
        lines.append("Notes:")
        lines.append("")
        lines.append("---")
        lines.append("")

    return "\n".join(lines)


if __name__ == "__main__":
    main()
