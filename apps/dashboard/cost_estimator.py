"""Pre-dispatch cost estimate for material processing.

Returns a (cost_usd, duration_s) tuple based on:
  - PDF page count
  - Active ModelConfig for purpose='generation' (the vision model)
  - Per-page token avg (calibration baseline; refine after observing real runs)

Used by the upload + confirm flow to surface "272 pages → ~$4.50, ~25 min"
to the user BEFORE dispatching the Container Apps Job.

Pricing table is hard-coded here (not in DB). Update when a model is added
or providers change pricing — calibrate against the first 5-10 real runs.
"""

import logging
from decimal import Decimal
from typing import Dict, Optional, Tuple

from apps.dashboard.material_routing import count_pdf_pages

logger = logging.getLogger(__name__)


# Per-page token estimate for vision OCR. Anchored to:
#   - ~200-DPI page → ~1700×2200 px → ~5000 tokens per image (Anthropic
#     vision: width*height/750 ≈ tokens)
#   - ~250 tokens of extracted text per page (output)
# These are averages; calibrate after first 5-10 production runs.
TOKENS_PER_PAGE_INPUT = 5_000
TOKENS_PER_PAGE_OUTPUT = 250

# Pricing per 1M tokens (USD). Update when Anthropic / OpenAI / Google change
# pricing, or a new model is added. Source of truth: provider docs as of the
# date in the comment above each entry.
#
# Pattern matches model_name on ModelConfig (case-insensitive substring).
# First match wins, so list specific names before generic ones.
MODEL_PRICING_PER_M_TOKENS: Dict[str, Tuple[float, float]] = {
    # Anthropic — May 2026
    'claude-opus-4-7': (15.0, 75.0),
    'claude-opus-4-6': (15.0, 75.0),
    'claude-opus-4': (15.0, 75.0),
    'claude-sonnet-4-6': (3.0, 15.0),
    'claude-sonnet-4-5': (3.0, 15.0),
    'claude-sonnet-4': (3.0, 15.0),
    'claude-haiku-4-5': (0.80, 4.0),
    'claude-haiku-4': (0.80, 4.0),
    # Generic Anthropic fallback (in case of older model name)
    'claude': (3.0, 15.0),

    # OpenAI — May 2026
    'gpt-4o-mini': (0.15, 0.60),
    'gpt-4o': (2.50, 10.0),
    'gpt-image-2': (10.0, 40.0),
    'gpt-4': (10.0, 30.0),

    # Google — May 2026 (Gemini charges flat 258 tokens/image regardless
    # of size, so estimates here OVER-state input cost vs the real bill.
    # Acceptable for a baseline guard.)
    'gemini-3.1-pro': (1.25, 10.0),
    'gemini-3.1-flash': (0.075, 0.30),
    'gemini-3-pro': (1.25, 10.0),
    'gemini-3-flash': (0.075, 0.30),
    'gemini-2.5-pro': (1.25, 5.0),
    'gemini-2.5-flash': (0.075, 0.30),
    'gemini': (0.30, 1.20),
}

# Per-batch wall-clock estimate. 5-way concurrency over 10-page batches:
#   ~30 s vision call per batch × 10 pages / 5 workers = ~6 s per page wall.
# Calibrate after first runs.
SECONDS_PER_PAGE_WALL = 6.0


def _lookup_pricing(model_name: str) -> Tuple[float, float]:
    """Return (input_per_M_tokens, output_per_M_tokens). Falls back to a
    conservative default if the model isn't in the table."""
    name = (model_name or '').lower()
    for pattern, prices in MODEL_PRICING_PER_M_TOKENS.items():
        if pattern in name:
            return prices
    logger.warning(f"No pricing entry for model {model_name!r} — using conservative fallback")
    return (5.0, 25.0)   # Pessimistic default so estimates over-state, not under-state


def estimate_material_cost(
    file_path: str,
    mode: str = 'rich',
    page_count: Optional[int] = None,
) -> Dict:
    """Estimate LLM spend + wall-clock for processing a material file.

    Args:
        file_path: PDF path
        mode: 'rich' (vision) or 'fast' (text-only, near-zero LLM cost)
        page_count: Optional pre-computed count (avoids re-opening the PDF)

    Returns:
        {
            'pages': int,
            'mode': str,
            'estimated_cost_usd': Decimal (rounded to 2dp),
            'estimated_duration_seconds': int,
            'model_name': str,
            'price_input_per_m': float,
            'price_output_per_m': float,
            'note': str,   # Human-readable summary
        }
    """
    if page_count is None:
        page_count = count_pdf_pages(file_path)

    # Fast mode = text-only, no vision. Cost is negligible (just chunking +
    # local embedding). Show $0 + a small wall-clock for transparency.
    if mode == 'fast':
        return {
            'pages': page_count,
            'mode': 'fast',
            'estimated_cost_usd': Decimal('0.00'),
            'estimated_duration_seconds': max(30, page_count * 1),  # ~1s/page text+chunk
            'model_name': 'n/a (text-only)',
            'price_input_per_m': 0.0,
            'price_output_per_m': 0.0,
            'note': 'Fast mode — no vision LLM, near-zero spend.',
        }

    # Rich mode: full vision OCR. Look up pricing for the active generation model.
    from apps.llm.models import ModelConfig
    config = ModelConfig.get_for('generation')
    model_name = config.model_name if config else '(no active model)'
    in_per_m, out_per_m = _lookup_pricing(model_name)

    input_tokens = page_count * TOKENS_PER_PAGE_INPUT
    output_tokens = page_count * TOKENS_PER_PAGE_OUTPUT
    cost_usd = (input_tokens / 1_000_000) * in_per_m + (output_tokens / 1_000_000) * out_per_m

    duration_s = max(60, int(page_count * SECONDS_PER_PAGE_WALL))

    return {
        'pages': page_count,
        'mode': 'rich',
        'estimated_cost_usd': Decimal(f"{cost_usd:.2f}"),
        'estimated_duration_seconds': duration_s,
        'model_name': model_name,
        'price_input_per_m': in_per_m,
        'price_output_per_m': out_per_m,
        'note': (
            f"Estimate: {page_count} pages × ({TOKENS_PER_PAGE_INPUT} in + "
            f"{TOKENS_PER_PAGE_OUTPUT} out) tokens × ${in_per_m}/${out_per_m} per 1M. "
            "Calibrate against actual spend after first runs."
        ),
    }


# Cost guardrails — apply at confirm time. Super-admin can bypass the
# hard-block by passing ?force=1 on the confirm POST.
COST_WARN_THRESHOLD_USD = Decimal('10.00')
COST_HARD_BLOCK_USD = Decimal('50.00')


def cost_verdict(cost_usd: Decimal) -> str:
    """Returns 'green' (<=warn), 'yellow' (warn..hard), or 'red' (>hard)."""
    if cost_usd <= COST_WARN_THRESHOLD_USD:
        return 'green'
    if cost_usd <= COST_HARD_BLOCK_USD:
        return 'yellow'
    return 'red'
