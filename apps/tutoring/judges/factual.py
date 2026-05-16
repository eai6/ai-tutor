"""Factual-claim judge — verifies numeric / named claims in a tutor
response against the curriculum knowledge base.

Single LLM call that BOTH identifies every checkable factual claim
in the response AND verifies each against retrieved KB evidence. No
regex pre-extraction — see auto-memory/feedback_llm_claim_extraction.md
for why (regex-only smoke caught 1/4 of deliberate-bad claims; LLM
extraction caught 3/4 with the same single API call).

Mirrors apps/curriculum/content_judges/factual_step.py shape but
adapted for live tutor turns:
  - Includes prior_exchanges in the input so the model can resolve
    "as we said" / "the value from before" references against the
    earlier tutor history (consistency without requiring KB
    confirmation for every back-reference).
  - Output schema kept stable ({"fact_claims": [...]}) so the
    benchmark + downstream consumers see the same shape they did
    before the refactor — only the EXTRACTION pipeline changed.

Skips when: empty response / no llm_client / no KB evidence /
malformed LLM output. Each skip returns a clean FactualResult with
the skip_reason populated.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import List, Optional

from apps.tutoring.fact_verifier import (
    ClaimVerdict,
    _retrieve_evidence,
)
from apps.tutoring.tracing import traced_judge

logger = logging.getLogger(__name__)


@dataclass
class FactualResult:
    claims: List[ClaimVerdict] = field(default_factory=list)
    skipped: bool = False
    skip_reason: str = ""


_SYSTEM = (
    "You are a focused factual-claim reviewer for a live tutoring "
    "response. The text comes from a tutor speaking to a secondary-"
    "school student.\n"
    "\n"
    "Do this in two phases inside ONE response:\n"
    "\n"
    "Phase 1 — extract every checkable factual claim from "
    "input.tutor_response. Treat as a claim: any specific number, "
    "date, proper noun (place / person / institution name), unit "
    "measurement, statistic, or named relationship. Skip generic "
    "scaffolding (\"let's think about this\") and rhetorical questions. "
    "Aim for completeness — false negatives at this phase let "
    "fabrications through.\n"
    "\n"
    "Phase 2 — for each extracted claim, assign exactly one status:\n"
    "  - supported: evidence clearly states the claim or a matching value.\n"
    "  - contradicted: evidence clearly states a different value or "
    "the opposite.\n"
    "  - unverified: evidence does not address the claim either way. "
    "When the claim is a back-reference to something the tutor already "
    "said in input.prior_exchanges (e.g. \"as we said\", \"the 360° "
    "from before\"), checking prior_exchanges counts as evidence — "
    "internal consistency is a valid form of support even when the "
    "curriculum KB doesn't confirm.\n"
    "\n"
    "Be CONSERVATIVE — when in doubt, return \"unverified\". NEVER "
    "fabricate support. Approve as \"supported\" only when the "
    "evidence (or prior_exchanges) explicitly contains the claim or "
    "a matching number / name.\n"
    "\n"
    "Output JSON ONLY (no prose, no code fence):\n"
    "{\"fact_claims\": [{\"claim\": \"<text from the response>\", "
    "\"status\": \"supported|contradicted|unverified\", "
    "\"evidence\": \"<<=80 char quote or empty>\"}]}\n"
    "\n"
    "If the response contains no checkable claims, return "
    "{\"fact_claims\": []}.\n"
)

from apps.tutoring.judges._prompt_meta import prompt_fingerprint
PROMPT_HASH, PROMPT_CHARS = prompt_fingerprint(_SYSTEM)

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


@traced_judge('factual')
def run_factual_judge(
    response_text: str,
    *,
    lesson,
    llm_client=None,
    max_claims: int = 5,
    prior_exchanges: str = "",
) -> FactualResult:
    """Single-task factual-claim verification with LLM-driven extraction.

    Pipeline:
      1. Pre-retrieve KB evidence using the whole response_text as the
         query (not per-claim — the regex that produced per-claim
         queries never existed for the LLM-extraction path).
      2. Skip when no evidence is retrievable AND no prior_exchanges
         to lean on — can't verify against nothing.
      3. Single LLM call: model extracts claims + assigns status
         using evidence + prior_exchanges. No regex round-trip.
      4. Coerce + dedupe verdicts; cap at max_claims.

    Same FactualResult schema as before so downstream callers
    (validator.py + benchmark snapshot) see no shape change.
    """
    result = FactualResult()
    text = (response_text or "").strip()
    if not text:
        result.skipped = True
        result.skip_reason = "empty_response"
        return result
    if llm_client is None:
        result.skipped = True
        result.skip_reason = "no_llm_client"
        return result

    # Pre-retrieve evidence using the whole response text as the
    # query. _retrieve_evidence accepts a list (legacy: claim list);
    # passing a single-element list with the response text gives
    # broader, more semantic recall than per-claim queries did.
    try:
        evidence = _retrieve_evidence(lesson, [text[:500]]) if lesson else ""
    except Exception as e:
        logger.warning("[FactualJudge] evidence retrieval failed: %s", e)
        evidence = ""

    has_evidence = bool((evidence or "").strip())
    has_history = bool((prior_exchanges or "").strip())
    if not has_evidence and not has_history:
        # Nothing to verify against — neither curriculum nor prior
        # tutor turns. Skip cleanly rather than ask the LLM to
        # fact-check against nothing.
        result.skipped = True
        result.skip_reason = "no_kb_evidence"
        return result

    payload = {
        "tutor_response": text[:2000],
        "prior_exchanges": (prior_exchanges or "")[:1600] or "(none)",
        "evidence": (evidence or "(no curriculum evidence retrieved)")[:3000],
    }
    user_prompt = (
        f"INPUT:\n{json.dumps(payload, ensure_ascii=False)}\n\n"
        "Identify every checkable factual claim in input.tutor_response "
        "and assign each a status using input.evidence (curriculum) "
        "and input.prior_exchanges (the tutor's earlier statements "
        "this session). Reply with ONLY the JSON object specified."
    )

    try:
        response = llm_client.generate(
            messages=[{"role": "user", "content": user_prompt}],
            system_prompt=_SYSTEM,
            max_tokens=600,
        )
        raw = (response.content or "").strip()
        raw = _FENCE_RE.sub("", raw)
        data = json.loads(raw)
    except Exception as e:
        logger.warning("[FactualJudge] call failed: %s", e)
        result.skipped = True
        result.skip_reason = f"llm_error: {type(e).__name__}"
        return result

    items = data.get("fact_claims") or []
    seen: set = set()
    if isinstance(items, list):
        for item in items[:max_claims]:
            if not isinstance(item, dict):
                continue
            claim = str(item.get("claim") or "").strip()[:200]
            if not claim:
                continue
            key = claim.lower()
            if key in seen:
                continue
            seen.add(key)
            status = str(item.get("status") or "unverified").strip().lower()
            if status not in ("supported", "contradicted", "unverified"):
                status = "unverified"
            ev = str(item.get("evidence") or "")[:120]
            result.claims.append(ClaimVerdict(claim, status, ev))

    # Structured log line so the benchmark slice can compare regex-era
    # vs LLM-extraction-era distributions over time. See
    # auto-memory/feedback_llm_claim_extraction.md note about
    # benchmark contamination — this gives us the data to detect it.
    contradicted = sum(1 for c in result.claims if c.status == "contradicted")
    unverified = sum(1 for c in result.claims if c.status == "unverified")
    supported = sum(1 for c in result.claims if c.status == "supported")
    logger.info(
        "[FactualJudge] extraction=llm claims=%d supported=%d "
        "contradicted=%d unverified=%d",
        len(result.claims), supported, contradicted, unverified,
    )

    return result
