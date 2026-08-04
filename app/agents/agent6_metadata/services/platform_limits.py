"""Agent 6 — deterministic per-platform metadata limits.

Roadmap: code_report/agent6_metadata_roadmap.md, Phase D. Python constants +
enforcement functions — "Claude reasons, Python decides": every platform
character/hashtag-count cap is enforced here regardless of what Claude
returns (CLAUDE.md §21.2: "Do not rely on 'please follow the rules' when
Python can enforce the rule").

Two call sites, once Phase E's PATCH endpoint exists: metadata generation
itself (this phase), and an operator edit (Check 6) — same function, never
forked, so an operator-edited field is held to the identical deterministic
limit as a freshly generated one.

Values sourced from each platform's public documentation (external facts,
not something a code-read can verify — flagged honestly rather than
presented as grep-confirmed).
"""

import logging

logger = logging.getLogger(__name__)

PLATFORM_LIMITS: dict[str, dict[str, int | None]] = {
    "youtube":   {"title_max": 100,  "description_max": 5000,  "hashtag_max": None},
    "tiktok":    {"title_max": 150,  "description_max": 2200,  "hashtag_max": 5},
    "instagram": {"title_max": 125,  "description_max": 2200,  "hashtag_max": 30},
    "facebook":  {"title_max": 255,  "description_max": 63206, "hashtag_max": None},
}

# Check 1 v2 decision: the burned-in overlay phrase, youtube-only. Not a platform upload
# limit like the table above (no platform API rejects an image for having "too much" text)
# — this is a legibility ceiling for a 1280x720 image at feed thumbnail size, chosen to
# match the "2-5 punchy words" real-thumbnail-practice guidance in the operator's brief.
THUMBNAIL_TEXT_MAX_CHARS = 30
THUMBNAIL_TEXT_MAX_WORDS = 5


def _truncate_at_word_boundary(text: str, max_len: int) -> str:
    """Truncate to at most ``max_len`` characters without cutting a word in
    half — matches the existing text-hygiene convention elsewhere in this
    codebase (e.g. ``_apply_hook_by_construction()``'s sentence-boundary
    care)."""
    if len(text) <= max_len:
        return text
    truncated = text[:max_len]
    last_space = truncated.rfind(" ")
    if last_space > 0:
        truncated = truncated[:last_space]
    return truncated.rstrip()


def _truncate_and_log(text: str, max_len: int, platform: str, field: str) -> str:
    if len(text) <= max_len:
        return text
    truncated = _truncate_at_word_boundary(text, max_len)
    logger.warning(
        "METADATA_LIMIT_ENFORCED platform=%s field=%s original_len=%d final_len=%d",
        platform, field, len(text), len(truncated),
    )
    return truncated


def enforce_platform_limits(
    platform: str, title: str, description: str, hashtags: list[str],
) -> tuple[str, str, list[str]]:
    """Truncate/cap ``title``, ``description``, and ``hashtags`` to
    ``platform``'s deterministic limits.

    Pure, deterministic, and **never raises on a too-long value** — it is
    fixed, not rejected (there is no "ship it anyway" option for a hard
    platform upload limit the way there is for narration length, since an
    actual upload would be rejected by the platform). Every truncation is
    logged (CLAUDE.md Golden Rule 3 — no silent fallbacks).

    Raises:
        ValueError: if ``platform`` is not a known key in PLATFORM_LIMITS —
            an unrecognized platform is a real bug upstream, not a value to
            silently tolerate (mirrors ``resolve_model()``'s
            fail-loud-on-unknown-key convention).
    """
    if platform not in PLATFORM_LIMITS:
        raise ValueError(
            f"Unknown platform '{platform}' — not in PLATFORM_LIMITS "
            f"({sorted(PLATFORM_LIMITS)})"
        )
    limits = PLATFORM_LIMITS[platform]

    final_title = _truncate_and_log(title, limits["title_max"], platform, "title")
    final_description = _truncate_and_log(
        description, limits["description_max"], platform, "description",
    )

    hashtag_max = limits["hashtag_max"]
    if hashtag_max is not None and len(hashtags) > hashtag_max:
        final_hashtags = list(hashtags[:hashtag_max])
        logger.warning(
            "METADATA_LIMIT_ENFORCED platform=%s field=hashtags original_len=%d final_len=%d",
            platform, len(hashtags), len(final_hashtags),
        )
    else:
        final_hashtags = list(hashtags)

    return final_title, final_description, final_hashtags


def enforce_thumbnail_text_limit(text: str) -> str:
    """Cap ``thumbnail_text`` (the youtube-row overlay phrase, Check 1 v2
    decision) to both THUMBNAIL_TEXT_MAX_WORDS and THUMBNAIL_TEXT_MAX_CHARS —
    whichever is stricter for the given string — never raises.

    Same call-site convention as ``enforce_platform_limits()``: used at
    generation time (this phase) and, once Phase E's PATCH endpoint exists,
    on any operator edit — one implementation, never forked.
    """
    original = text
    words = text.split()
    if len(words) > THUMBNAIL_TEXT_MAX_WORDS:
        text = " ".join(words[:THUMBNAIL_TEXT_MAX_WORDS])
    if len(text) > THUMBNAIL_TEXT_MAX_CHARS:
        text = _truncate_at_word_boundary(text, THUMBNAIL_TEXT_MAX_CHARS)

    if text != original:
        logger.warning(
            "METADATA_LIMIT_ENFORCED platform=youtube field=thumbnail_text "
            "original_len=%d final_len=%d",
            len(original), len(text),
        )
    return text
