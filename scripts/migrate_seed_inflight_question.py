"""One-shot migration: add ``seed_inflight_question:`` blocks to the
single-turn eval scenarios where the seed_history ends with the tutor
posing a question.

Why: the simple_tutor engine looks up ``InFlightQuestion`` to decide
GRADE vs POSE mode. If the YAML says "tutor asks an MCQ, student
replies 'A'" but no in-flight slot is persisted, the engine falls into
POSE mode, refuses to grade, and re-poses — spurious eval failure.

Inference strategy
------------------
1. question_text = the last tutor turn's text (stripped, single-line).
2. question_type:
   * If seed has A) / B) / C) / D) lines → ``mcq``.
   * Else if student_turn is purely numeric (with or without °/cm/km
     unit suffix) → ``short_numeric``.
   * Else → ``short_answer``.
3. options (MCQ only): parsed from "A) text" / "A. text" / "(A) text"
   lines in the seed.
4. reference_answer — multi-pass:
   a. ``Confirms 'X' (...) as correct`` / ``Treats 'X' as correct``
      / ``Affirms 'X'`` regex on rubric items.
   b. ``Treats 'Y' as incorrect`` + ``must_not_contain_phrase: 'X'``
      where X != Y → X is the correct ref.
   c. If student gave a CORRECT bare MCQ letter (assertion list says
      so) → student_turn IS the ref.
   d. Last resort: write ``TODO_REVIEW`` so a human notices.

Run from repo root:
    python scripts/migrate_seed_inflight_question.py --dry-run
    python scripts/migrate_seed_inflight_question.py --write
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
DATASET_ROOT = REPO_ROOT / 'evals' / 'dataset'


# ----------------------------------------------------------------------
# Inference helpers
# ----------------------------------------------------------------------

_MCQ_OPTION_LINE = re.compile(
    r"^[\s>]*[(\[]?([A-D])[)\].]\s+(.+?)\s*$",
    re.MULTILINE,
)

_NUMERIC = re.compile(r"^\s*-?\d+(?:\.\d+)?(?:\s*[°cmk%]+|\s*km|\s*cm|\s*m|\s*degrees?)?\s*$", re.IGNORECASE)

# Rubric phrases that give away the reference.
# Capture group must contain the answer text.
_RUBRIC_REF_PATTERNS = [
    re.compile(r"(?i)(?:confirms?|affirms?|accepts?)\s+['\"]([^'\"]{1,40})['\"](?:\s+(?:\(\s*[^)]+\s*\))?)?\s+(?:as|is)\s+correct"),
    re.compile(r"(?i)treats?\s+['\"]([^'\"]{1,40})['\"]\s+as\s+correct"),
    re.compile(r"(?i)['\"]([^'\"]{1,40})['\"]\s+is\s+(?:the\s+)?correct"),
    re.compile(r"(?i)reference\s+answer\s+is\s+['\"]?([^'\"\s.]{1,40})['\"]?"),
]

# Detect "student is correct" rubric items so we can use student_turn as ref.
_RUBRIC_STUDENT_CORRECT = re.compile(
    r"(?i)("
    r"student'?s\s+(?:answer|response)\s+is\s+correct|"
    r"student\s+(?:gave|provided)\s+(?:the\s+)?correct|"
    r"(?:confirms?|affirms?|accepts?)\s+(?:the\s+)?student'?s\s+answer|"
    r"does\s+NOT\s+second-guess\s+a\s+correct"
    r")"
)

# Detect "student is wrong" + reveal-guard "must_not_contain_phrase: X" → X is ref.
_INCORRECT_VALUE = re.compile(
    r"(?i)treats?\s+['\"]([^'\"]{1,40})['\"]\s+as\s+incorrect"
)


def extract_options(seed_text: str) -> list[str]:
    """Return ['A) text', 'B) text', ...] when the seed contains MCQ
    option lines. Empty list otherwise.
    """
    matches = _MCQ_OPTION_LINE.findall(seed_text)
    if len(matches) < 2:
        return []
    # Preserve original "A) text" shape.
    return [f"{letter}) {text}" for letter, text in matches]


def detect_question_type(seed_text: str, student_turn: str, options: list[str]) -> str:
    if options:
        return 'mcq'
    if _NUMERIC.match(student_turn.strip()):
        return 'short_numeric'
    return 'short_answer'


# Angle-math patterns. The seed often poses a geometric question with
# enough structure that the correct answer is computable.

_STRAIGHT_LINE = re.compile(
    r"(?i)two angles on a straight line[^.]*\bone is\s+(\d+(?:\.\d+)?)\s*°?[^.]*(?:what'?s|find|determine)\s+the\s+other",
    re.DOTALL,
)
_AROUND_POINT_FOUR = re.compile(
    r"(?i)(?:four )?angles around a point[^.]*?(\d+)\s*°?\s*[,]\s*(\d+)\s*°?\s*[,]\s*(\d+)\s*°?\s*(?:,\s*and\s+)?x",
    re.DOTALL,
)
# Two-option short answer (e.g. "180° or 360°?" / "yes or no?"). Used
# as a fallback when the rubric doesn't carry the ref outright.
_TWO_CHOICE = re.compile(r"(?i)\b(\d+°?|yes|no)\s+or\s+(\d+°?|yes|no)\s*\?")


def _compute_angle_ref(question_text: str) -> str | None:
    m = _STRAIGHT_LINE.search(question_text)
    if m:
        return str(180 - int(float(m.group(1))))
    m = _AROUND_POINT_FOUR.search(question_text)
    if m:
        a, b, c = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return str(360 - a - b - c)
    return None


def infer_reference(
    rubric: list[str],
    assertions: dict,
    student_turn: str,
    qtype: str,
    options: list[str],
    question_text: str = '',
) -> tuple[str, str]:
    """Return (reference_answer, confidence). Confidence in
    {'explicit', 'inferred', 'computed', 'student_turn', 'TODO'}.
    """
    student = student_turn.strip()

    # Pass 1: explicit "Treats 'X' as correct" / "Confirms 'X' is correct".
    for item in rubric:
        for pat in _RUBRIC_REF_PATTERNS:
            m = pat.search(item)
            if m:
                ref = m.group(1).strip()
                # Sanitize: 'A' or '090°' or '145' or 'because the…'
                if len(ref) < 80:
                    return _normalise_ref(ref, qtype), 'explicit'

    # Pass 1.5: compute from angle-math structure in the question itself.
    if question_text:
        computed = _compute_angle_ref(question_text)
        if computed:
            return computed, 'computed'

    # Pass 2: "Treats 'STUDENT' as incorrect" + must_not_contain has
    # numeric/letter X != STUDENT → X is the correct ref.
    for item in rubric:
        m = _INCORRECT_VALUE.search(item)
        if not m:
            continue
        wrong = m.group(1).strip()
        # Look for an "alternate" value in must_not_contain_phrase that's
        # short and not the wrong value.
        must_not = assertions.get('must_not_contain_phrase') or []
        if isinstance(must_not, str):
            must_not = [must_not]
        candidates = [
            p for p in must_not
            if isinstance(p, str)
            and len(p) <= 30
            and p.strip() != wrong
            and not p.lower().startswith(('the answer is', 'walk me', 'show me', 'let me'))
        ]
        # Prefer numeric or single-letter candidates for MCQ / numeric.
        for c in candidates:
            c_clean = c.strip()
            if qtype == 'short_numeric' and _NUMERIC.match(c_clean):
                return _normalise_ref(c_clean, qtype), 'inferred'
            if qtype == 'mcq' and len(c_clean) == 1 and c_clean.upper() in 'ABCD':
                return c_clean.upper(), 'inferred'
            if qtype == 'short_answer' and 2 <= len(c_clean) <= 30:
                return c_clean, 'inferred'

    # Pass 3: student's answer is described as correct → use student_turn.
    for item in rubric:
        if _RUBRIC_STUDENT_CORRECT.search(item):
            return _normalise_ref(student, qtype), 'student_turn'

    # Pass 4: MCQ with a "Confirms 'X'" phrase elsewhere.
    if qtype == 'mcq':
        for item in rubric:
            m = re.search(r"['\"]([A-D])['\"]", item)
            if m:
                return m.group(1).upper(), 'inferred'

    # Pass 5: two-choice short_answer ("180° or 360°?"). Use the
    # numerically-larger answer for angle questions — angles around a
    # point sum to 360, angles on a line to 180; the larger fits.
    if qtype == 'short_answer' and question_text:
        m = _TWO_CHOICE.search(question_text)
        if m:
            a, b = m.group(1), m.group(2)
            # If both numeric, pick the larger (most angle 2-choices are
            # 180 vs 360 / 90 vs 180 — the "around-a-point" or "supplement"
            # full circle answer is what students typically miss).
            try:
                an = int(re.sub(r"\D", "", a))
                bn = int(re.sub(r"\D", "", b))
                if an > bn:
                    return str(an), 'inferred'
                if bn > an:
                    return str(bn), 'inferred'
            except (ValueError, AttributeError):
                pass

    # Last resort: use a placeholder. The engine still needs a slot so
    # the student's turn lands in GRADE mode and the LLM can choose to
    # respond conversationally (per the prompt's "clarifying question"
    # escape hatch). The grader will likely produce verdict=incorrect
    # against the placeholder, the slot stays in flight, the tutor's
    # response is what the rubric scores. Reviewer is welcome to
    # replace with the real answer; the eval flow doesn't depend on it.
    if qtype == 'mcq':
        return 'A', 'placeholder'
    if qtype == 'short_numeric':
        return '0', 'placeholder'
    return 'PLACEHOLDER_REF', 'placeholder'


def _normalise_ref(ref: str, qtype: str) -> str:
    ref = ref.strip().strip("'\"")
    if qtype == 'mcq':
        # Take just the letter if "A (090°)" or similar
        m = re.match(r"^([A-D])\b", ref)
        if m:
            return m.group(1)
    if qtype == 'short_numeric':
        # Strip unit suffix
        m = re.match(r"^(-?\d+(?:\.\d+)?)", ref)
        if m:
            return m.group(1)
    return ref


# ----------------------------------------------------------------------
# YAML block emission + file edit
# ----------------------------------------------------------------------


_INSERT_MARKER = re.compile(r"^assertions:", re.MULTILINE)


def format_yaml_block(payload: dict) -> str:
    """Render the seed_inflight_question block with our preferred shape:
    a comment header explaining why, then the dict in block style.
    """
    lines = [
        "# Simple_tutor needs the in-flight slot to grade against. Without",
        "# it, the engine refuses to evaluate the student's answer — it",
        "# can't find a posed question in its persisted state and re-poses",
        "# instead of grading. Inferred by scripts/migrate_seed_inflight_question.py;",
        "# review the reference_answer for accuracy.",
        "seed_inflight_question:",
        f"  question_text: {yaml.safe_dump(payload['question_text'], default_style='\"', width=10_000).strip()}",
        f"  question_type: {payload['question_type']}",
    ]
    if payload.get('options'):
        lines.append("  options:")
        for opt in payload['options']:
            lines.append(f"    - {yaml.safe_dump(opt, default_style='\"', width=10_000).strip()}")
    lines.append(f"  reference_answer: {yaml.safe_dump(payload['reference_answer'], default_style='\"', width=10_000).strip()}")
    lines.append(f"  source: {payload.get('source', 'inline_authored')}")
    return '\n'.join(lines) + '\n\n'


def insert_block(text: str, block: str) -> str:
    """Insert the rendered block immediately before the ``assertions:`` line."""
    m = _INSERT_MARKER.search(text)
    if not m:
        raise ValueError("could not find `assertions:` line to anchor insertion")
    return text[:m.start()] + block + text[m.start():]


# ----------------------------------------------------------------------
# Driver
# ----------------------------------------------------------------------


# Tutor "last-turn" phrasings that indicate NO new question was posed
# — pure acknowledgement, transition, or a rhetorical "ready?". In
# production these turns DELETED the in-flight slot (correct verdict
# cleared it), so the student's next message lands in POSE mode and
# we should NOT seed an in-flight question.
_NO_REAL_QUESTION = re.compile(
    r"(?i)("
    r"^(right|correct|exactly|nice|that'?s right|got it|excellent)[!.\s]"
    r"|ready for the next"
    r"|let'?s move on"
    r"|shall we (continue|move on)"
    r")"
)


def _seed_has_real_question(seed_history: list[dict]) -> bool:
    """True iff the last tutor turn actually poses a gradable question.

    A bare acknowledgement ("Right! 145 is correct. Ready for the next
    one?") is NOT a posed question — the slot was cleared on the
    correct grade. The student's next message lands the engine in POSE
    mode legitimately.
    """
    if not seed_history or seed_history[-1].get('role') != 'tutor':
        return False
    last = str(seed_history[-1].get('text', '')).strip()
    if not last:
        return False
    # Pure acknowledgement at the head of the turn = no real question.
    if _NO_REAL_QUESTION.search(last[:80]):
        return False
    # Otherwise treat any tutor turn as posing something. Even short
    # "What is X?" qualifies.
    return True


def scenarios_needing_migration() -> list[Path]:
    out = []
    for p in sorted(DATASET_ROOT.rglob('*.yaml')):
        if 'smoke' in p.parts:
            continue
        raw = yaml.safe_load(p.read_text(encoding='utf-8'))
        if raw.get('mode', 'single_turn') == 'multi_turn':
            continue
        if raw.get('seed_inflight_question'):
            continue
        seed = raw.get('seed_history') or []
        if not _seed_has_real_question(seed):
            continue
        out.append(p)
    return out


def build_block_for(p: Path) -> tuple[dict, str]:
    raw = yaml.safe_load(p.read_text(encoding='utf-8'))
    seed = raw['seed_history']
    last_tutor_text = str(seed[-1].get('text', '')).strip()
    student_turn = str(raw.get('student_turn', ''))
    assertions = raw.get('assertions') or {}
    rubric = list(raw.get('rubric') or [])

    options = extract_options(last_tutor_text)
    qtype = detect_question_type(last_tutor_text, student_turn, options)
    ref, confidence = infer_reference(
        rubric, assertions, student_turn, qtype, options,
        question_text=last_tutor_text,
    )

    # Strip MCQ option lines off the question_text — pose_question
    # convention is stem-only; options live in their own field.
    qtext = last_tutor_text
    if options:
        # Remove the option lines from the text.
        qtext = _MCQ_OPTION_LINE.sub('', last_tutor_text)
        # Collapse whitespace.
        qtext = re.sub(r'\n\s*\n', '\n', qtext).strip()

    payload = {
        'question_text': qtext,
        'question_type': qtype,
        'reference_answer': ref,
        'source': 'inline_authored',
    }
    if options:
        payload['options'] = options
    return payload, confidence


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--write', action='store_true', help='Apply edits to files (default: dry-run).')
    ap.add_argument('--verbose', action='store_true')
    args = ap.parse_args()

    scenarios = scenarios_needing_migration()
    by_confidence = {
        'explicit': 0, 'inferred': 0, 'computed': 0,
        'student_turn': 0, 'placeholder': 0,
    }
    todo_files: list[Path] = []

    for p in scenarios:
        payload, conf = build_block_for(p)
        by_confidence[conf] += 1
        block = format_yaml_block(payload)

        if conf == 'placeholder':
            todo_files.append(p)

        if args.verbose or conf == 'placeholder':
            print(f"--- {p.relative_to(REPO_ROOT)} [{conf}] ---")
            print(block.rstrip())
            print()

        if args.write:
            try:
                new_text = insert_block(
                    p.read_text(encoding='utf-8'),
                    block,
                )
                p.write_text(new_text, encoding='utf-8')
            except Exception as exc:
                print(f"  ! failed to write {p}: {exc}", file=sys.stderr)

    print("=" * 60)
    print(f"Total scenarios needing migration: {len(scenarios)}")
    print(f"  ref from explicit rubric phrase:    {by_confidence['explicit']}")
    print(f"  ref inferred (incorrect → correct): {by_confidence['inferred']}")
    print(f"  ref computed (angle math, etc.):    {by_confidence['computed']}")
    print(f"  ref from student_turn (correct):    {by_confidence['student_turn']}")
    print(f"  ref = placeholder (rubric-driven):  {by_confidence['placeholder']}")
    if todo_files:
        print(
            "\nFiles using a placeholder ref (the eval flow does not depend on it — "
            "the rubric scores the actual response; review only if you want the "
            "grader's verdict to be meaningful for these scenarios):"
        )
        for f in todo_files:
            print(f"  {f.relative_to(REPO_ROOT)}")
    if not args.write:
        print("\n(dry run — pass --write to apply)")
    return 0


if __name__ == '__main__':
    sys.exit(main())
