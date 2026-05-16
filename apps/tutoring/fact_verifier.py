"""Factual claim verification for tutor responses.

V2 of the Socratic validator (memory/socratic_validator_plan.md).

Pipeline:
  1. Extract candidate claims from the response (regex + heuristics).
  2. Retrieve supporting chunks from the curriculum KnowledgeBase and
     the SeychellesContext library, scoped to the lesson.
  3. Ask an LLM judge whether each claim is supported by the retrieved
     evidence. The judge returns a tri-state per claim:
       supported  — clear textual support in the retrieved chunks
       contradicted — chunks contradict the claim
       unverified — neither support nor contradict the claim
  4. Return a structured result the validator can merge into
     SessionTurn.metadata + use to mark / strip unverified spans.

Designed to fail-closed when the LLM client or KB is unavailable: the
verifier returns "skipped" rather than blocking the response.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Iterable, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Claim extraction
# ---------------------------------------------------------------------------

# Patterns for numeric / specific claims worth verifying. Each is wrapped
# in a single capture group so we can extract the literal claim span.
_CLAIM_PATTERNS = [
    # Numbers with units / suffixes: "$1.59 billion", "85%", "98 km",
    # "67th" -> 'th' suffix is captured by the rank pattern below.
    r"(\$?\s*\d{1,3}(?:,\d{3})*(?:\.\d+)?\s*(?:%|billion|million|thousand|trillion|km|kg|m\b|cm|°c|°f|degrees|years?))",
    # Comma-separated thousands like "200,000" or "1,234,567" — common
    # for populations + counts.
    r"(\b\d{1,3}(?:,\d{3}){1,3}\b)",
    # Ranking phrases: "ranks 67th out of 189"
    r"(rank(?:ed|ing|s)?\s+\d+(?:st|nd|rd|th)?(?:\s+out\s+of\s+\d+)?)",
    # Named indicators followed by a number: "HDI 0.796", "GDP 1.59"
    r"((?:HDI|GDP|GNP|GNI|PPP|CO2|GINI|MEDC|LEDC)\s*[:=]?\s*\$?\d[\d,.]*)",
    # Year ranges: "1990–2020"
    r"(\d{4}\s*[–\-]\s*\d{4})",
    # "in 2023", "by 2030"
    r"(?:in|by|since|until)\s+(\d{4})",
]
_CLAIM_RE = re.compile("|".join(_CLAIM_PATTERNS), re.IGNORECASE)


def extract_claims(text: str, max_claims: int = 5) -> List[str]:
    """Return up to `max_claims` distinct claim spans from `text`.

    The list is order-preserving and de-duplicated case-insensitively.
    """
    if not text:
        return []
    seen: set = set()
    out: List[str] = []
    for match in _CLAIM_RE.finditer(text):
        # Pull the first non-empty capture group (the actual claim).
        claim = next((g for g in match.groups() if g), None)
        if not claim:
            continue
        key = claim.lower().strip()
        if key in seen:
            continue
        seen.add(key)
        out.append(claim.strip())
        if len(out) >= max_claims:
            break
    return out


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class ClaimVerdict:
    claim: str
    status: str  # "supported" | "contradicted" | "unverified"
    evidence: str = ""

    @property
    def is_problem(self) -> bool:
        return self.status in {"contradicted", "unverified"}


@dataclass
class FactCheckResult:
    claims: List[ClaimVerdict] = field(default_factory=list)
    skipped: bool = False
    skip_reason: str = ""
    layers_run: List[str] = field(default_factory=list)

    @property
    def has_problems(self) -> bool:
        return any(c.is_problem for c in self.claims)

    @property
    def problem_claims(self) -> List[str]:
        return [c.claim for c in self.claims if c.is_problem]

    @property
    def contradicted_claims(self) -> List[str]:
        return [c.claim for c in self.claims if c.status == "contradicted"]

    def to_metadata(self) -> dict:
        return {
            "factual_claims_checked": len(self.claims),
            "factual_claims_unverified": [
                c.claim for c in self.claims if c.status == "unverified"
            ],
            "factual_claims_contradicted": self.contradicted_claims,
            "fact_check_skipped": self.skipped,
            "fact_check_skip_reason": self.skip_reason,
        }


# ---------------------------------------------------------------------------
# RAG retrieval
# ---------------------------------------------------------------------------


def _retrieve_evidence(lesson, claims: List[str], n_results: int = 5) -> str:
    """Combine claim text into a single query and fetch top-K chunks
    from the curriculum KB + SeychellesContext.

    Returns a single newline-joined evidence string (cap at ~3500 chars
    to keep the LLM prompt small)."""
    chunks: List[str] = []
    query = " | ".join(claims)[:500]

    # Curriculum KB — query BOTH the lesson's institution-scoped KB
    # AND the global (institution_id=0) KB, then merge. Mirrors the
    # `Q(institution=inst) | Q(institution__isnull=True)` pattern from
    # CLAUDE.md: institution-specific content plus platform-wide
    # curriculum (e.g. global Geography textbooks indexed under
    # institution_0) both contribute evidence. Without this merge, a
    # lesson at institution=12 would never see globally-shared
    # curriculum and the judge would skip with no_kb_evidence on
    # otherwise-checkable claims.
    try:
        from apps.curriculum.knowledge_base import get_knowledge_base
        institution_id = (
            getattr(lesson.unit.course.institution, "id", None)
            if lesson and lesson.unit and lesson.unit.course
            else None
        )
        kb_buckets = []
        if institution_id and institution_id != 0:
            kb_buckets.append(institution_id)
        kb_buckets.append(0)
        per_bucket = max(2, n_results // len(kb_buckets))
        seen_content = set()
        for iid in kb_buckets:
            try:
                kb = get_knowledge_base(iid)
                results = kb.search(query, n_results=per_bucket) or []
            except Exception as inner_e:
                logger.debug(f"[FactCheck] KB inst={iid} unavailable: {inner_e}")
                continue
            for r in results:
                content = (r.get("content") or "").strip()
                if not content or content[:120] in seen_content:
                    continue
                seen_content.add(content[:120])
                tag = "curriculum" if iid == 0 else f"curriculum:inst{iid}"
                chunks.append(f"[{tag}] {content[:600]}")
    except Exception as e:
        logger.warning(f"[FactCheck] KB retrieval failed: {e}")

    # SeychellesContext entries — small table so we just grab the
    # top relevant ones via simple keyword overlap.
    try:
        from apps.curriculum.models import SeychellesContext
        keywords = {w.lower() for c in claims for w in re.findall(r"\w{4,}", c)}
        # Cheap scan; the table is small (< 100 rows expected).
        candidates = SeychellesContext.objects.filter(is_active=True)
        scored = []
        for ctx in candidates:
            haystack = f"{ctx.title} {ctx.content}".lower()
            score = sum(1 for kw in keywords if kw in haystack)
            if score > 0:
                scored.append((score, ctx))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        for _, ctx in scored[:3]:
            chunks.append(
                f"[seychelles_context:{ctx.category}] {ctx.title} — {ctx.content[:400]}"
            )
    except Exception as e:
        logger.warning(f"[FactCheck] SeychellesContext retrieval failed: {e}")

    evidence = "\n".join(chunks)
    return evidence[:3500]


# ---------------------------------------------------------------------------
# LLM judge
# ---------------------------------------------------------------------------


_JUDGE_SYSTEM = (
    "You are a strict fact-checker for a tutoring system. You will be "
    "given a list of factual claims and a body of retrieved evidence. "
    "For EACH claim, decide: supported / contradicted / unverified.\n"
    "- supported: the evidence clearly states the claim or a numeric "
    "value matches.\n"
    "- contradicted: the evidence clearly states a different value or "
    "the opposite.\n"
    "- unverified: the evidence does not address the claim either way.\n"
    "Be conservative — when in doubt, return unverified, NEVER fabricate "
    "support."
)


def _ask_llm_judge(
    llm_client,
    response_text: str,
    claims: List[str],
    evidence: str,
) -> List[ClaimVerdict]:
    """Send claims + evidence to the LLM, parse a JSON verdict per claim.
    Falls back to "unverified" for everything if anything goes wrong."""
    if not llm_client or not claims:
        return [ClaimVerdict(c, "unverified") for c in claims]

    payload = {
        "tutor_response": response_text[:1500],
        "claims": claims,
        "evidence": evidence or "(no evidence retrieved)",
    }
    user_prompt = (
        "Fact-check these claims against the retrieved evidence. Reply "
        "with ONLY a JSON array, one object per claim, in the same order "
        "as the input. Each object must be {\"claim\": str, \"status\": "
        '"supported"|"contradicted"|"unverified", "evidence": str}. '
        "evidence should quote the supporting / contradicting snippet "
        "(<= 80 chars), or empty string if unverified.\n\n"
        f"INPUT:\n{json.dumps(payload, ensure_ascii=False)}"
    )

    try:
        response = llm_client.generate(
            messages=[{"role": "user", "content": user_prompt}],
            system_prompt=_JUDGE_SYSTEM,
            max_tokens=600,
        )
        raw = (response.content or "").strip()
        # Strip markdown code fences if the model added them.
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.IGNORECASE)
        data = json.loads(raw)
        if not isinstance(data, list):
            raise ValueError("expected JSON array")
        verdicts: List[ClaimVerdict] = []
        for item, claim in zip(data, claims):
            status = (item or {}).get("status", "unverified")
            if status not in {"supported", "contradicted", "unverified"}:
                status = "unverified"
            evidence_str = (item or {}).get("evidence", "") or ""
            verdicts.append(
                ClaimVerdict(claim=claim, status=status, evidence=str(evidence_str)[:200])
            )
        # If LLM returned fewer items than claims, pad with unverified.
        for missing in claims[len(verdicts):]:
            verdicts.append(ClaimVerdict(missing, "unverified"))
        return verdicts
    except Exception as e:
        logger.warning(f"[FactCheck] judge failed: {e}")
        return [ClaimVerdict(c, "unverified") for c in claims]


# ---------------------------------------------------------------------------
# Public entry
# ---------------------------------------------------------------------------


def verify_response(
    response_text: str,
    lesson,
    llm_client=None,
    max_claims: int = 5,
) -> FactCheckResult:
    """Run the full fact-check pipeline over a tutor response.

    Returns a FactCheckResult. Caller decides what to do with the
    verdicts (validator marks unverified spans, dashboard surfaces them).
    """
    result = FactCheckResult()
    if not response_text:
        result.skipped = True
        result.skip_reason = "empty_response"
        return result

    claims = extract_claims(response_text, max_claims=max_claims)
    if not claims:
        result.skipped = True
        result.skip_reason = "no_claims_detected"
        result.layers_run.append("extract")
        return result
    result.layers_run.append("extract")

    if llm_client is None:
        # Without an LLM, we can still mark claims as unverified so the
        # teacher sees them but no judgment.
        result.claims = [ClaimVerdict(c, "unverified") for c in claims]
        result.skipped = True
        result.skip_reason = "no_llm_client"
        return result

    evidence = _retrieve_evidence(lesson, claims)
    result.layers_run.append("retrieve")
    result.claims = _ask_llm_judge(llm_client, response_text, claims, evidence)
    result.layers_run.append("judge")
    return result
