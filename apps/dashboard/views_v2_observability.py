"""v2 engine observability dashboard (Phase 3 §3.3).

Reads ``TurnSpan`` rows + ``SessionTurn.judge_outputs.v2_trace``
rollups produced by the v2 ``TutorEngine``. Surfaces the aggregate
signals required for the Phase 3 default-flip gate:

  - Safe-template trigger rate over a rolling window.
  - Verdict distribution (correct / partial / wrong / unverified).
  - Move-selection distribution.
  - P1 indicator counters — conformance-caught candidate rejections
    + pre-pose refusal counts.
  - Per-stage latency from spans (p50 / p95).

Alert thresholds are computed in-process; the dashboard surfaces an
``alerts`` block when any metric crosses its threshold. Mechanism
beyond surfacing is TBD per plan (currently log-level INFO + manual
review cadence for the pilot).

Phase 2 already emits the per-stage spans + per-turn rollup; this
module is purely the visualization layer on top of those persisted
rows. No new write paths.
"""

from __future__ import annotations

import logging
import statistics
from collections import Counter
from datetime import timedelta
from typing import Any

from django.shortcuts import render
from django.utils import timezone

from apps.tutoring.models import SessionTurn, TurnSpan, TutorSession

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Threshold constants — alerts fire when crossed
# ----------------------------------------------------------------------
# These are MVP starting points; tuned from pilot data per §3.3. The
# "baseline + 2σ" rule belongs once we have a baseline; pre-baseline
# we use a flat ceiling so the dashboard surfaces issues from day one.
SAFE_TEMPLATE_RATE_CEILING = 0.05   # 5% of turns
UNVERIFIED_RATE_CEILING = 0.20      # 20% of graded turns
PRE_POSE_REFUSAL_RATE_CEILING = 0.10  # 10% of pose attempts

DEFAULT_WINDOW_HOURS = 24


# ----------------------------------------------------------------------
# Aggregate-builder helpers (pure functions, easy to unit-test)
# ----------------------------------------------------------------------


def compute_v2_aggregates(
    *,
    window_hours: int = DEFAULT_WINDOW_HOURS,
    institution_id: int | None = None,
) -> dict[str, Any]:
    """Compute the aggregate signal bundle for the v2 dashboard.

    ``window_hours`` filters to recently-created tutor turns. The
    function makes a small number of queries (turns + spans for the
    same window) and reduces them in-process — adequate for the
    pilot's traffic volume. Optimize when needed.
    """
    cutoff = timezone.now() - timedelta(hours=window_hours)

    sessions_q = TutorSession.objects.filter(engine_version="v2")
    if institution_id is not None:
        sessions_q = sessions_q.filter(institution_id=institution_id)
    session_ids = list(sessions_q.values_list("id", flat=True))

    turns = (
        SessionTurn.objects
        .filter(
            session_id__in=session_ids,
            role=SessionTurn.Role.TUTOR,
            created_at__gte=cutoff,
        )
        .only("id", "judge_outputs", "metadata", "created_at")
    )

    move_counter: Counter[str] = Counter()
    verdict_counter: Counter[str] = Counter()
    fallback_count = 0
    retry_count = 0
    p1_correct_to_wrong_caught = 0
    p1_wrong_to_correct_caught = 0
    total_turns = 0

    turn_ids: list[int] = []
    for turn in turns:
        total_turns += 1
        turn_ids.append(turn.id)
        trace = (turn.judge_outputs or {}).get("v2_trace") or {}
        move = trace.get("selected_move") or ""
        if move:
            move_counter[move] += 1
        verdict = trace.get("verdict") or "no_verdict"
        verdict_counter[verdict] += 1
        if trace.get("fallback_used"):
            fallback_count += 1
        if trace.get("retry_used"):
            retry_count += 1
        for violation in trace.get("conformance_violations") or []:
            v = str(violation).lower()
            if "affirms_correctness" in v or "wrong_to_correct" in v:
                p1_wrong_to_correct_caught += 1
            if "refutes_correctness" in v or "correct_to_wrong" in v:
                p1_correct_to_wrong_caught += 1

    spans = (
        TurnSpan.objects
        .filter(turn_id__in=turn_ids)
        .only("kind", "name", "duration_ms", "payload")
    )

    stage_latencies: dict[str, list[int]] = {}
    pre_pose_refusals = 0
    pre_pose_attempts = 0
    for span in spans:
        key = f"{span.kind}.{span.name}"
        stage_latencies.setdefault(key, [])
        if span.duration_ms is not None:
            stage_latencies[key].append(int(span.duration_ms))
        # Pre-pose refusal counter — grader emits a "grader.pre_pose_check"
        # span with payload {"outcome": "refused"|"pass", ...}.
        if span.name == "pre_pose_check":
            pre_pose_attempts += 1
            outcome = (span.payload or {}).get("outcome")
            if outcome and outcome != "pass":
                pre_pose_refusals += 1

    latency_summary: dict[str, dict[str, int]] = {}
    for key, values in stage_latencies.items():
        if not values:
            continue
        latency_summary[key] = {
            "count": len(values),
            "p50_ms": int(statistics.median(values)),
            "p95_ms": int(_percentile(values, 0.95)),
        }

    safe_template_rate = fallback_count / total_turns if total_turns else 0.0
    graded_total = sum(
        verdict_counter[v] for v in ("correct", "wrong", "partial", "unverified")
    )
    unverified_rate = (
        verdict_counter.get("unverified", 0) / graded_total
        if graded_total
        else 0.0
    )
    pre_pose_refusal_rate = (
        pre_pose_refusals / pre_pose_attempts if pre_pose_attempts else 0.0
    )

    alerts: list[str] = []
    if safe_template_rate > SAFE_TEMPLATE_RATE_CEILING:
        alerts.append(
            f"Safe-template rate {safe_template_rate:.1%} exceeds "
            f"ceiling {SAFE_TEMPLATE_RATE_CEILING:.0%}"
        )
    if unverified_rate > UNVERIFIED_RATE_CEILING:
        alerts.append(
            f"Unverified-verdict rate {unverified_rate:.1%} exceeds "
            f"ceiling {UNVERIFIED_RATE_CEILING:.0%}"
        )
    if pre_pose_refusal_rate > PRE_POSE_REFUSAL_RATE_CEILING:
        alerts.append(
            f"Pre-pose refusal rate {pre_pose_refusal_rate:.1%} exceeds "
            f"ceiling {PRE_POSE_REFUSAL_RATE_CEILING:.0%}"
        )

    if alerts:
        logger.info("[v2_observability] alerts: %s", "; ".join(alerts))

    return {
        "window_hours": window_hours,
        "total_turns": total_turns,
        "verdict_distribution": dict(verdict_counter),
        "move_distribution": dict(move_counter),
        "fallback_count": fallback_count,
        "safe_template_rate": safe_template_rate,
        "retry_count": retry_count,
        "p1_correct_to_wrong_caught": p1_correct_to_wrong_caught,
        "p1_wrong_to_correct_caught": p1_wrong_to_correct_caught,
        "pre_pose_attempts": pre_pose_attempts,
        "pre_pose_refusals": pre_pose_refusals,
        "pre_pose_refusal_rate": pre_pose_refusal_rate,
        "unverified_rate": unverified_rate,
        "stage_latency_ms": latency_summary,
        "alerts": alerts,
    }


def _percentile(values: list[int], p: float) -> float:
    """Compute the p-th percentile of a list (no numpy dependency)."""
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * p
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return float(s[f])
    return s[f] + (s[c] - s[f]) * (k - f)


# ----------------------------------------------------------------------
# Dashboard view
# ----------------------------------------------------------------------


def v2_observability_dashboard(request):
    """Render the v2 engine observability dashboard.

    Staff-only via the existing ``staff_required`` decorator (applied
    at URL routing). Renders an aggregate summary for a recent
    rolling window. Query params: ``hours`` (default 24).
    """
    # Local import to avoid circular import at module load.
    from apps.dashboard.views import staff_required, get_staff_context

    @staff_required
    def _inner(request):
        try:
            hours = int(request.GET.get("hours", DEFAULT_WINDOW_HOURS))
        except (TypeError, ValueError):
            hours = DEFAULT_WINDOW_HOURS
        hours = max(1, min(hours, 24 * 14))

        ctx = get_staff_context(request)
        institution = ctx.get("institution")
        aggregates = compute_v2_aggregates(
            window_hours=hours,
            institution_id=getattr(institution, "id", None),
        )

        ctx.update({
            "aggregates": aggregates,
            "window_hours": hours,
        })
        return render(request, "dashboard/v2_observability.html", ctx)

    return _inner(request)
