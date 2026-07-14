"""Shared field contract for Agent 4's local beat-review artifact."""

# Historical review-only fields are retained for compatibility with artifacts
# from before their enrichment producers were removed by the Elimination
# Mandate. Current structured storyboard output cannot produce these keys.
LEGACY_REVIEW_METADATA_FIELDS: tuple[str, ...] = (
    "negative_prompt", "shot_type", "subject", "action", "emotion", "camera",
    "lighting", "composition", "continuity_tags", "visual_bible_refs",
    "location", "character", "is_first_15_seconds", "prompt_quality_warnings",
    "first15_validation_status", "first15_issues", "first15_strength_tags",
    "first15_enhanced", "first15_validation_summary",
)

# Live fields that the current storyboard/timestamp pipeline really produces.
# Snapshotting them makes beat_review_metadata.json a useful, self-contained
# observability artifact instead of a list of section_order-only objects.
LIVE_REVIEW_SNAPSHOT_FIELDS: tuple[str, ...] = (
    "visual_intent", "visual_type", "visual_category", "environment",
    "flux_prompt", "effect", "color_grade", "transition_to_next", "motif",
    "beat_intensity", "suggested_duration_sec", "start_hint", "end_hint",
    "media_url", "media_type",
)

REVIEW_METADATA_FIELDS = (
    *LEGACY_REVIEW_METADATA_FIELDS,
    *LIVE_REVIEW_SNAPSHOT_FIELDS,
)
