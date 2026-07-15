"""Canonical narration-POV normalization for Agent 2 config reads."""

from __future__ import annotations

import difflib
import logging

logger = logging.getLogger(__name__)

VALID_NARRATION_POVS = ("third_person", "first_person_storytime")

_ALIASES = {
    "first_person": "first_person_storytime",
    "firstperson": "first_person_storytime",
    "storytime": "first_person_storytime",
    "thirdperson": "third_person",
}


def normalize_narration_pov(value: object, *, channel_id: object = "unknown") -> str:
    """Return the closest supported POV and warn when persisted input is invalid.

    ``None``/blank means the normal missing-config default and returns
    ``third_person`` silently. Non-empty legacy or unknown values are normalized
    to a canonical token, first through explicit aliases and then by deterministic
    string similarity against the two supported values.
    """
    raw = str(value or "").strip()
    if not raw:
        return "third_person"

    token = raw.casefold().replace("-", "_").replace(" ", "_")
    if token in VALID_NARRATION_POVS:
        return token

    normalized = _ALIASES.get(token)
    if normalized is None:
        normalized = difflib.get_close_matches(
            token, VALID_NARRATION_POVS, n=1, cutoff=0.0,
        )[0]

    logger.warning(
        "NARRATION_POV_NORMALIZED channel_id=%s raw=%r normalized=%s "
        "reason=unsupported_persisted_value action=continue_with_normalized_value",
        channel_id, raw, normalized,
    )
    return normalized
