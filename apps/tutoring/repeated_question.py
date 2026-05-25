"""Repeated-question structural guard for tutor responses.

Bank-question repetition is already guarded deterministically:
`shown_question_ids` filters the pool so the same bank Q can't be
re-rendered via pose_question. But the tutor ALSO authors questions
in chat (probes, follow-ups, scaffolding), and there's no guard
against:

  1. Cross-turn authored repetition — tutor asks "What is a compass
     rose?" on turn 5, then "What's the function of a compass rose?"
     on turn 9 after topic drift.
  2. Authored ≈ already-shown bank — tutor authors a probe whose
     substance overlaps with a bank Q the student already answered.
  3. Paraphrased re-ask of CURRENT active bank Q — tutor paraphrases
     the bank question the student is actively trying to answer
     instead of giving a hint.

The prompt rule "DO NOT re-author the question stem" exists but Sonnet
sometimes ignores it. This module is the structural belt to that prompt
brace, per memory/hint_vs_reveal_guards_plan.md W14.

Design:
  ┌─ deterministic (Jaccard signature match) ─→ repeat? YES/NO ─┐
  ┤                                                              ├─ trust LLM on disagreement
  └─ LLM judge on borderline 0.45–0.7 (skipped otherwise) ──────┘

Returns RepeatVerdict on detection, None when clean.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import List, Optional, Set

logger = logging.getLogger(__name__)


_STOPWORDS: Set[str] = {
    'a', 'an', 'the', 'of', 'to', 'in', 'on', 'for', 'is', 'are',
    'was', 'were', 'be', 'been', 'being', 'that', 'this', 'these',
    'those', 'it', 'its', 'as', 'at', 'by', 'with', 'from', 'or',
    'and', 'but', 'if', 'then', 'so', 'do', 'does', 'did', 'has',
    'have', 'had', 'will', 'would', 'should', 'could', 'can', 'may',
    'might', 'must', 'i', 'you', 'we', 'they', 'he', 'she', 'them',
    'us', 'his', 'her', 'their', 'our', 'your', 'my', 'me', 'about',
    'into', 'than', 'over', 'also', 'just', 'now', 'only', 'one',
    'two', 'three', 'all', 'any', 'each', 'when', 'where', 'while',
    'such', 'no', 'not', 'more', 'less', 'very', 'really', 'what',
    'which', 'who', 'how', 'why',  # interrogatives — same Q across
                                   # different interrogatives = repeat
}

# Thresholds (tunable from production traces).
# Calibrated to catch verb-swap paraphrases ("what does X show?" ≈ "what
# is X's function?") which only score ~0.5 Jaccard despite being the
# same question semantically. Strong threshold deliberately low; the
# LLM judge filters false-positives in the borderline zone.
# PR2 (2026-05-25): STRONG dropped from 0.55 → 0.45 so the deterministic
# guard catches more paraphrase repeats without depending on the LLM
# borderline check. False positives just cost an extra regen cycle;
# false negatives ship the repeat to the student.
JACCARD_EXACT_REPEAT     = 0.75   # near-identical signature → definite
JACCARD_STRONG_REPEAT    = 0.45   # strong overlap → repeat
JACCARD_BORDERLINE_LOW   = 0.20   # below this → LLM not called (clean)
                                  # Low because short questions (3-5 sig
                                  # tokens) score low Jaccard despite
                                  # being semantically identical. LLM
                                  # filters false-positives.
ACTIVE_PARAPHRASE_THRESH = 0.45   # paraphrase of CURRENT active bank Q

# Max number of borderline pairs to send to the LLM judge per turn.
# Each pair = one LLM call; cap prevents runaway cost when an authored
# response touches many prior questions on borderline overlap.
MAX_LLM_BORDERLINE_PAIRS = 3


@dataclass
class RepeatVerdict:
    repeated: bool
    reason: str
    repeat_kind: str  # 'cross_turn' | 'bank_repeat' | 'active_paraphrase'
    matched_question: str = ""    # the prior question we matched against
    new_question: str = ""        # the new question that repeated
    jaccard: float = 0.0
    sources: List[str] = field(default_factory=list)  # ['deterministic', 'llm']
    elapsed_ms: int = 0


# ---------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------

_NON_WORD_RE = re.compile(r"[^a-z0-9\s]+")
_MULTI_WS_RE = re.compile(r"\s+")
_QUESTION_SPLIT_RE = re.compile(r'(?<=[.!?])\s+')


def _tokenize(text: str) -> List[str]:
    if not text:
        return []
    s = text.lower()
    s = _NON_WORD_RE.sub(' ', s)
    s = _MULTI_WS_RE.sub(' ', s).strip()
    return [t for t in s.split() if t and t not in _STOPWORDS]


def normalise_question_signature(text: str) -> str:
    """Return a stable, comparable signature for a question. Sorted
    bag-of-significant-tokens — different phrasings of the same Q
    collapse to similar signatures."""
    return ' '.join(sorted(_tokenize(text)))


def _jaccard_tokens(a: str, b: str) -> float:
    sa = set(a.split()) if a else set()
    sb = set(b.split()) if b else set()
    if not sa or not sb:
        return 0.0
    inter = sa & sb
    union = sa | sb
    return len(inter) / len(union) if union else 0.0


def extract_questions(response: str) -> List[str]:
    """Pull every `?`-terminated sentence from the response.
    Returns the raw sentence text (not normalised) so callers can
    surface it back to the user."""
    if not response:
        return []
    sentences = _QUESTION_SPLIT_RE.split(response.strip())
    return [s.strip() for s in sentences if s.strip().endswith('?')]


# ---------------------------------------------------------------------
# Deterministic check
# ---------------------------------------------------------------------

def _deterministic_check(
    new_questions: List[str],
    recent_question_signatures: List[str],
    shown_bank_stems: List[str],
    active_bank_stem: Optional[str],
) -> Optional[RepeatVerdict]:
    """Match each new question against recent / bank / active. Returns
    the first repeat found, or None.

    Returns dict with keys: repeated, kind, jaccard, matched, new_q.
    """
    if not new_questions:
        return None

    # Pre-compute signatures for bank stems + active.
    bank_sigs = [(s, normalise_question_signature(s)) for s in (shown_bank_stems or [])]
    active_sig = (
        normalise_question_signature(active_bank_stem)
        if active_bank_stem else ""
    )

    for new_q in new_questions:
        new_sig = normalise_question_signature(new_q)
        if not new_sig:
            continue

        # (1) Cross-turn authored repeat — against recent tutor questions
        for prev_sig in (recent_question_signatures or []):
            if not prev_sig:
                continue
            j = _jaccard_tokens(new_sig, prev_sig)
            if j >= JACCARD_EXACT_REPEAT:
                return RepeatVerdict(
                    repeated=True,
                    reason=f"near-identical to recent question (jaccard={j:.2f})",
                    repeat_kind='cross_turn',
                    matched_question=prev_sig,
                    new_question=new_q,
                    jaccard=j,
                    sources=['deterministic'],
                )
            if j >= JACCARD_STRONG_REPEAT:
                return RepeatVerdict(
                    repeated=True,
                    reason=f"strong overlap with recent question (jaccard={j:.2f})",
                    repeat_kind='cross_turn',
                    matched_question=prev_sig,
                    new_question=new_q,
                    jaccard=j,
                    sources=['deterministic'],
                )

        # (2) Bank repeat — against questions already shown via pose_question
        for stem, stem_sig in bank_sigs:
            if not stem_sig:
                continue
            j = _jaccard_tokens(new_sig, stem_sig)
            if j >= JACCARD_STRONG_REPEAT:
                return RepeatVerdict(
                    repeated=True,
                    reason=f"overlaps with already-shown bank question (jaccard={j:.2f})",
                    repeat_kind='bank_repeat',
                    matched_question=stem[:200],
                    new_question=new_q,
                    jaccard=j,
                    sources=['deterministic'],
                )

        # (3) Active paraphrase — paraphrasing the CURRENT pending Q
        if active_sig:
            j = _jaccard_tokens(new_sig, active_sig)
            if j >= ACTIVE_PARAPHRASE_THRESH:
                return RepeatVerdict(
                    repeated=True,
                    reason=f"paraphrasing the active pending bank question (jaccard={j:.2f}) — give a HINT instead",
                    repeat_kind='active_paraphrase',
                    matched_question=active_bank_stem[:200],
                    new_question=new_q,
                    jaccard=j,
                    sources=['deterministic'],
                )

    return None


def _borderline_candidates(
    new_questions: List[str],
    recent_question_signatures: List[str],
    shown_bank_stems: List[str],
    active_bank_stem: Optional[str],
):
    """Find (new_q, prev_q_text) pairs whose Jaccard is between the
    BORDERLINE_LOW and STRONG_REPEAT thresholds — the zone where the
    LLM judge actually adds value."""
    out = []
    bank_sigs = [(s, normalise_question_signature(s)) for s in (shown_bank_stems or [])]
    active_sig = (
        normalise_question_signature(active_bank_stem)
        if active_bank_stem else ""
    )
    for new_q in new_questions:
        new_sig = normalise_question_signature(new_q)
        if not new_sig:
            continue
        for prev_sig in (recent_question_signatures or []):
            j = _jaccard_tokens(new_sig, prev_sig)
            if JACCARD_BORDERLINE_LOW <= j < JACCARD_STRONG_REPEAT:
                out.append((new_q, prev_sig))
        for stem, stem_sig in bank_sigs:
            j = _jaccard_tokens(new_sig, stem_sig)
            if JACCARD_BORDERLINE_LOW <= j < JACCARD_STRONG_REPEAT:
                out.append((new_q, stem))
        if active_sig:
            j = _jaccard_tokens(new_sig, active_sig)
            if JACCARD_BORDERLINE_LOW <= j < ACTIVE_PARAPHRASE_THRESH:
                out.append((new_q, active_bank_stem))
    return out


# ---------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------

def detect_repeated_question(
    response: str,
    recent_question_signatures: List[str],
    shown_bank_stems: List[str],
    active_bank_stem: Optional[str],
    llm_client=None,
) -> Optional[RepeatVerdict]:
    """Detect whether the tutor response repeated a question already
    asked. Returns None when clean, RepeatVerdict when repeat detected.

    Deterministic pass catches strong cases (Jaccard >= 0.7 on bank /
    cross-turn, >= 0.6 on active-paraphrase). LLM judge runs on
    borderline cases (0.45 <= Jaccard < threshold) where deterministic
    is ambiguous.

    Disagreement: trust the LLM (false positives hurt UX more than
    missing one repetition — student can always ask 'didn't you ask
    that already?').
    """
    import time
    t0 = time.monotonic()

    if not response or not response.strip():
        return None
    new_questions = extract_questions(response)
    if not new_questions:
        return None

    # ---- deterministic ----
    det_verdict = _deterministic_check(
        new_questions=new_questions,
        recent_question_signatures=recent_question_signatures or [],
        shown_bank_stems=shown_bank_stems or [],
        active_bank_stem=active_bank_stem,
    )
    if det_verdict is not None:
        det_verdict.elapsed_ms = int((time.monotonic() - t0) * 1000)
        logger.info(
            "[RepeatDetect] DETERMINISTIC %s — jaccard=%.2f — %d ms",
            det_verdict.repeat_kind, det_verdict.jaccard, det_verdict.elapsed_ms,
        )
        return det_verdict

    # ---- LLM judge (borderline only) ----
    borderline = _borderline_candidates(
        new_questions=new_questions,
        recent_question_signatures=recent_question_signatures or [],
        shown_bank_stems=shown_bank_stems or [],
        active_bank_stem=active_bank_stem,
    )
    if not borderline or llm_client is None:
        return None

    # Call the unified grader's JUDGE_REPEAT path on up to
    # MAX_LLM_BORDERLINE_PAIRS pairs. The previous version only checked
    # borderline[0] — repeats in any later pair slipped through silently.
    from apps.tutoring.exit_ticket_grader import (
        BatchRepeatItem, JudgmentType, run_grading_batch,
    )
    pairs = borderline[:MAX_LLM_BORDERLINE_PAIRS]
    items = [
        BatchRepeatItem(
            index=idx, new_question=new_q,
            previous_questions=[matched_text],
        )
        for idx, (new_q, matched_text) in enumerate(pairs)
    ]
    try:
        results = run_grading_batch(
            items, judgment_type=JudgmentType.JUDGE_REPEAT, llm_client=llm_client,
        )
    except Exception as exc:
        logger.warning("[RepeatDetect] LLM judge crash: %s", exc)
        return None

    if not results:
        return None
    # Return the first positive verdict (results match input order).
    for r in results:
        if not getattr(r, 'repeated', False):
            continue
        new_q, matched_text = pairs[r.index]
        elapsed = int((time.monotonic() - t0) * 1000)
        logger.info(
            "[RepeatDetect] LLM flagged borderline pair %d/%d — reason=%r — %d ms",
            r.index + 1, len(pairs), (r.reason or '')[:120], elapsed,
        )
        return RepeatVerdict(
            repeated=True,
            reason=r.reason or 'LLM judge flagged borderline overlap',
            repeat_kind='cross_turn',
            matched_question=matched_text[:200],
            new_question=new_q,
            jaccard=0.0,  # borderline — exact value irrelevant for LLM-driven
            sources=['llm'],
            elapsed_ms=elapsed,
        )
    return None


# ---------------------------------------------------------------------
# Template-repeat detection (ab-test-reports-v5 engine rec #9).
#
# detect_repeated_question covers cases where the tutor literally re-
# asks the same question (cross-turn paraphrase, bank repeat, active-
# paraphrase). detect_template_repeat is its sibling: catches cases
# where the tutor poses a STRUCTURALLY identical question with
# different surface details (numbers, names, places, units) — a
# different surface form but the same template the student just solved.
# That's an interleaving failure: the student practices the same
# procedure twice when the lesson intent was to mix structures.
#
# Approach mirrors detect_repeated_question:
#   - Cheap precondition: token-Jaccard between the new question and
#     each recent tutor question. Below 0.15 means surface words don't
#     even overlap → no template repeat possible, skip LLM.
#   - Above 0.15 → ask the LLM judge (JUDGE_TEMPLATE_REPEAT) whether
#     the procedure is identical.
#   - LLM verdict is authoritative; deterministic Jaccard only gates
#     the call (cost control).
#
# Returns RepeatVerdict with repeat_kind='template_repeat' on hit,
# None otherwise. Fail-soft on LLM error → None.
# ---------------------------------------------------------------------

TEMPLATE_JACCARD_FLOOR = 0.15  # below this, prose is too different to matter
TEMPLATE_LLM_TOKEN_BUDGET = 1024


def detect_template_repeat(
    response: str,
    recent_tutor_questions: List[str],
    llm_client=None,
) -> Optional[RepeatVerdict]:
    """Detect whether the tutor's NEW question reuses the same
    structural template as a recent tutor question.

    Args:
      response: the tutor turn being validated.
      recent_tutor_questions: raw question texts from the last few
        tutor turns (oldest → newest, or unordered — order doesn't
        matter for this check). Pass the question PORTION (sentence
        ending in '?') if possible; whole-turn text works too but
        is noisier.
      llm_client: BaseLLMClient used for the JUDGE_TEMPLATE_REPEAT
        call. Without it, this function returns None (no LLM, no
        judgment — safer than guessing).

    Returns:
      RepeatVerdict with repeat_kind='template_repeat' when the LLM
      judge flags a match. None when clean or LLM unavailable.
    """
    import time
    if llm_client is None:
        return None
    if not response or not response.strip():
        return None
    if not recent_tutor_questions:
        return None

    new_questions = extract_questions(response)
    if not new_questions:
        return None

    t0 = time.monotonic()

    # Cheap precondition: token Jaccard. We don't need a high
    # threshold — we just want to skip the LLM call when the prose is
    # obviously unrelated. The LLM is the authoritative judge.
    candidates = []
    for new_q in new_questions:
        new_sig = normalise_question_signature(new_q)
        if not new_sig:
            continue
        for prev_q in recent_tutor_questions:
            if not prev_q or not prev_q.strip():
                continue
            prev_sig = normalise_question_signature(prev_q)
            if not prev_sig:
                continue
            j = _jaccard_tokens(new_sig, prev_sig)
            if j >= TEMPLATE_JACCARD_FLOOR:
                candidates.append((new_q, prev_q, j))

    if not candidates:
        return None

    # Pick the highest-Jaccard candidate for the LLM call (most likely
    # template match; saves tokens vs sending the full list).
    candidates.sort(key=lambda x: x[2], reverse=True)
    new_q, prev_q, top_j = candidates[0]

    from apps.tutoring.exit_ticket_grader import (
        BatchRepeatItem, JudgmentType, run_grading_batch,
    )
    item = BatchRepeatItem(
        index=0,
        new_question=new_q,
        previous_questions=[prev_q],
    )
    try:
        results = run_grading_batch(
            [item],
            judgment_type=JudgmentType.JUDGE_TEMPLATE_REPEAT,
            llm_client=llm_client,
            max_tokens=TEMPLATE_LLM_TOKEN_BUDGET,
        )
    except Exception as exc:
        logger.warning("[TemplateRepeat] LLM judge crash: %s", exc)
        return None

    elapsed = int((time.monotonic() - t0) * 1000)
    if not results or not results[0].repeated:
        logger.info(
            "[TemplateRepeat] clean — top jaccard=%.2f — %d ms",
            top_j, elapsed,
        )
        return None

    r = results[0]
    logger.info(
        "[TemplateRepeat] FLAGGED — jaccard=%.2f reason=%r — %d ms",
        top_j, r.reason[:120], elapsed,
    )
    return RepeatVerdict(
        repeated=True,
        reason=r.reason or 'LLM flagged same template, different surface details',
        repeat_kind='template_repeat',
        matched_question=(prev_q or '')[:200],
        new_question=new_q,
        jaccard=top_j,
        sources=['llm'],
        elapsed_ms=elapsed,
    )
