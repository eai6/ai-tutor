"""Centralized runtime-flag accessors for the v2 engine.

Per Phase 1 §6: both ``NEW_TUTOR`` and ``BANK_PREPOSE_RECHECK`` live
behind a single centralized accessor so flag reads aren't scattered
through call sites and so the test suite can patch them in one place.
"""

from apps.tutoring.v2.config.flags import (
    bank_prepose_recheck_enabled,
    is_new_tutor_enabled,
    select_engine_version,
)

__all__ = [
    "bank_prepose_recheck_enabled",
    "is_new_tutor_enabled",
    "select_engine_version",
]
