"""Factual-claim judge — verifies numeric / named claims against the
curriculum knowledge base.

Single-task focused prompt. Reuses the existing claim-extraction +
KB-evidence retrieval helpers in apps/tutoring/fact_verifier so the
factual axis stays consistent with the legacy verifier.

Skips when the response contains no extractable claims.
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
    extract_claims,
)
from apps.tutoring.tracing import traced_judge

logger = logging.getLogger(__name__)


@dataclass
class FactualResult:
    claims: List[ClaimVerdict] = field(default_factory=list)
    skipped: bool = False
    skip_reason: str = ""


_SYSTEM = (
    "You are a focused factual-claim reviewer for a tutoring response. "
    "For EACH claim listed in input.factual_claims, decide whether the "
    "retrieved curriculum evidence (input.evidence) supports / "
    "contradicts / leaves the claim unverified.\n"
    "\n"
    "Be CONSERVATIVE — when in doubt, return \"unverified\". NEVER "
    "fabricate support. Only mark \"supported\" when the evidence "
    "explicitly states the same value or matching language.\n"
    "\n"
    "Status definitions:\n"
    "  - supported: evidence clearly states the claim or a matching value.\n"
    "  - contradicted: evidence clearly states a different value.\n"
    "  - unverified: evidence does not address the claim either way.\n"
    "\n"
    "Output JSON ONLY (no prose, no code fence):\n"
    "{\"fact_claims\": [{\"claim\": \"<from input>\", "
    "\"status\": \"supported|contradicted|unverified\", "
    "\"evidence\": \"<<=80 char quote>\"}]}\n"
    "If input.factual_claims is empty, return {\"fact_claims\": []}.\n"
)

from apps.tutoring.judges._prompt_meta import prompt_fingerprint
PROMPT_HASH, PROMPT_CHARS = prompt_fingerprint(_SYSTEM)


@traced_judge('factual')
def run_factual_judge(
    response_text: str,
    *,
    lesson,
    llm_client=None,
    max_claims: int = 5,
    prior_exchanges: str = "",
) -> FactualResult:
    """Single-task factual-claim verification."""
    result = FactualResult()
    if not response_text or not response_text.strip():
        result.skipped = True
        result.skip_reason = "empty_response"
        return result
    if llm_client is None:
        result.skipped = True
        result.skip_reason = "no_llm_client"
        return result

    factual_claims = extract_claims(response_text, max_claims=max_claims)
    if not factual_claims:
        result.skipped = True
        result.skip_reason = "no_claims_detected"
        return result

    try:
        evidence = _retrieve_evidence(lesson, factual_claims) if lesson else ""
    except Exception as e:
        logger.warning("[FactualJudge] evidence retrieval failed: %s", e)
        evidence = ""

    payload = {
        "tutor_response": response_text[:2000],
        "prior_exchanges": (prior_exchanges or "")[:1600] or "(none)",
        "factual_claims": factual_claims,
        "evidence": (evidence or "(no evidence retrieved)")[:3000],
    }
    user_prompt = (
        f"INPUT:\n{json.dumps(payload, ensure_ascii=False)}\n\n"
        "Verify the factual claims listed in input.factual_claims. "
        "Consult input.prior_exchanges to resolve references like "
        "\"as we said\" or \"the value from before\" against earlier "
        "tutor statements — a claim that matches the prior tutor turn "
        "is internally consistent, even if input.evidence doesn't "
        "address it directly. Reply with ONLY the JSON object specified."
    )

    try:
        response = llm_client.generate(
            messages=[{"role": "user", "content": user_prompt}],
            system_prompt=_SYSTEM,
            max_tokens=400,
        )
        raw = (response.content or "").strip()
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.IGNORECASE)
        data = json.loads(raw)
    except Exception as e:
        logger.warning("[FactualJudge] call failed: %s", e)
        result.skipped = True
        result.skip_reason = f"llm_error: {type(e).__name__}"
        return result

    items = data.get("fact_claims") or []
    if isinstance(items, list):
        for item in items[:max_claims]:
            if not isinstance(item, dict):
                continue
            claim = str(item.get("claim") or "").strip()[:200]
            status = str(item.get("status") or "unverified").strip().lower()
            if status not in ("supported", "contradicted", "unverified"):
                status = "unverified"
            ev = str(item.get("evidence") or "")[:120]
            if claim:
                result.claims.append(ClaimVerdict(claim, status, ev))
    # Pad missing claims as "unverified" so downstream sees one entry per claim.
    for missing in factual_claims[len(result.claims):]:
        result.claims.append(ClaimVerdict(missing, "unverified"))
    return result
