"""Prompt fingerprinting helper.

Each judge module computes ``PROMPT_HASH`` (first 10 chars of sha1)
+ ``PROMPT_CHARS`` at import time using ``prompt_fingerprint(_SYSTEM)``.
The fingerprints flow into ``CombinedJudgeResult.prompt_versions`` and
land on ``SessionTurn.metadata['prompt_pack']`` so benchmark
annotators can correlate behavior shifts with specific prompt revisions.

Cheap at import, deterministic per content. No external deps.
"""
from __future__ import annotations

import hashlib
from typing import Tuple


def prompt_fingerprint(text: str) -> Tuple[str, int]:
    """Return ``(hash, chars)`` for a prompt string.

    Hash is the first 10 hex chars of sha1 — enough collision resistance
    to distinguish prompt revisions while staying compact in snapshots.
    """
    text = text or ''
    digest = hashlib.sha1(text.encode('utf-8')).hexdigest()[:10]
    return digest, len(text)
