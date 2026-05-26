"""Runtime feature flags for the v2 engine routing.

Per Phase 1 §6:
  - ``NEW_TUTOR`` (default ``'off'``) routes new sessions to v2 vs.
    legacy. Sticky per session via ``TutorSession.engine_version``.
  - ``BANK_PREPOSE_RECHECK`` (default ``'on'``) toggles the
    derivability check on bank-path questions. Repeat guards run
    *independently* and are NOT affected by this flag (Phase 1 §4.3).

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


def is_new_tutor_enabled() -> bool:
    """Whether new sessions should be routed to the v2 engine.

    Default ``off`` in Phase 1 — Phase 3 flips the default to ``on``.
    """
    return _truthy(os.environ.get(NEW_TUTOR_ENV, "off"))


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
