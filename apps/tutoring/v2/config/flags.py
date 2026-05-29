"""Runtime feature flags for the v2 engine routing.

Per Phase 3 §3.4 — ``NEW_TUTOR`` default is now ``'on'``:
  - ``NEW_TUTOR`` (default ``'on'``) routes new sessions to v2 vs.
    legacy. Sticky per session via ``TutorSession.engine_version``,
    so a session that started on legacy stays on legacy across
    resumes regardless of later flag flips.
  - ``BANK_PREPOSE_RECHECK`` (default ``'on'``) toggles the
    derivability check on bank-path questions. Repeat guards run
    *independently* and are NOT affected by this flag (Phase 1 §4.3).

**Kill switch.** Setting ``NEW_TUTOR=off`` routes *new* sessions back
to the legacy engine while in-flight v2 sessions complete on v2
(sticky-per-session). This is the production safety lever for
student-facing safety incidents — NOT for "the benchmark dipped" or
"a metric looks off in the dashboard." Pull the kill switch when the
v2 engine is producing student-facing harm; use the observability
dashboard + roll-forward fixes for everything else.

Tests patch ``_truthy`` / env vars via ``unittest.mock.patch.dict``.
"""

from __future__ import annotations

import os

NEW_TUTOR_ENV = "NEW_TUTOR"
BANK_PREPOSE_RECHECK_ENV = "BANK_PREPOSE_RECHECK"

ENGINE_LEGACY = "legacy"
ENGINE_V2 = "v2"


def _truthy(raw: str | None) -> bool:
    if not raw:
        return False
    return raw.strip().lower() in {"1", "on", "true", "yes", "y"}


def _explicitly_off(raw: str | None) -> bool:
    """``True`` only when the env var is explicitly set to a falsey value.

    Distinct from ``not _truthy(raw)`` because empty / unset must read
    as the *default* (now 'on'), not as an explicit kill-switch flip.
    """
    if raw is None:
        return False
    return raw.strip().lower() in {"0", "off", "false", "no", "n"}


def is_new_tutor_enabled() -> bool:
    """Whether new sessions should be routed to the v2 engine.

    Phase 3 default: ``on``. Only an explicit ``NEW_TUTOR=off`` (the
    kill switch) routes new sessions back to legacy.
    """
    raw = os.environ.get(NEW_TUTOR_ENV)
    if _explicitly_off(raw):
        return False
    return True


def bank_prepose_recheck_enabled() -> bool:
    """Whether to run the derivability ``pre_pose_check`` on bank-path
    questions. Default ``on`` (Phase 1 §7 item 11)."""
    return _truthy(os.environ.get(BANK_PREPOSE_RECHECK_ENV, "on"))


def select_engine_version(existing: str | None) -> str:
    """Pick the engine for a session.

    - If ``existing`` is a known engine name (``legacy`` / ``v2``),
      it wins (sticky-per-session).
    - Otherwise, ``NEW_TUTOR=on`` picks ``v2``; everything else picks
      ``legacy``.
    """
    if existing in (ENGINE_LEGACY, ENGINE_V2):
        return existing
    return ENGINE_V2 if is_new_tutor_enabled() else ENGINE_LEGACY
