"""Channel configuration snapshot foundation (Phase Agent1-V3.6).

Content generation today reads channel configuration live, via whatever
`channel.config`/`channel.languages`/`channel.voices`/`channel.sources`/
`channel.platforms`/`channel.publish_timings` happen to be at the moment an
agent runs — there is no stable, point-in-time record of what configuration
actually applied to a given content run. If an operator edits the channel
mid-pipeline, a later agent has no way to know whether it is seeing the
same configuration an earlier agent saw.

This module is the **foundation only**: a pure, local, side-effect-free
builder/validator pair that produces an immutable snapshot dict from a
`Channel` ORM object. It does not run anywhere yet — no Celery task, no
Agent 2/3/4/5 code, and no Agent 1 route calls `build_channel_config_snapshot()`
or `attach_snapshot_to_content()` today. A future phase decides the actual
generation-start hook and wires consumption; this phase only proves the
data shape and the immutability contract are correct.

No network call, no Claude call, no database write of its own (`attach_snapshot_to_content()`
sets an in-memory ORM attribute only — committing is the caller's
responsibility, exactly like every other Agent 1 service function in this
package).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import TypedDict

from app.models import Channel


class ConfigSnapshotIssue(TypedDict):
    """One reason a snapshot failed validation."""
    severity: str   # "BLOCKING" — every issue this module returns is blocking
    field: str
    code: str
    message: str


# Fields validate_channel_config_snapshot() treats as critical — their
# absence (key missing entirely) makes a snapshot invalid. A present-but-empty
# list (e.g. no languages configured yet) is a separate, non-structural
# concern this function does not judge; it only checks that the snapshot
# was built with the expected shape.
_REQUIRED_TOP_LEVEL_FIELDS = (
    "channel_id",
    "channel_config_id",
    "content_mode",
    "script_source",
    "output_mode",
    "visual_style",
    "image_style",
    "languages",
    "platforms",
    "videos_per_week",
    "publish_timing_summary",
    "voices",
    "source_summary",
    "captured_at",
)


def _voice_summary(channel: Channel) -> list[dict]:
    """Provider/model/id per language — never the encrypted credential blob
    (that lives on ChannelPlatform, not ChannelVoice, and is out of scope
    here regardless — see CLAUDE.md §30, credentials must never be
    duplicated into a second, unencrypted location)."""
    return [
        {
            "language": v.language,
            "provider": v.provider,
            "tts_model": v.tts_model,
            "voice_id": v.voice_id,
        }
        for v in channel.voices
    ]


def _source_summary(channel: Channel) -> list[dict]:
    """Source type/language/trust_score only — source_value (the actual
    feed URL/handle) is included since it is not a credential, but no
    platform credential field is ever read here."""
    return [
        {
            "source_type": s.source_type,
            "source_value": s.source_value,
            "language": s.language,
            "trust_score": s.trust_score,
        }
        for s in channel.sources
    ]


def _publish_timing_summary(channel: Channel) -> list[dict]:
    return [
        {
            "platform": t.platform,
            "language": t.language,
            "timezone": t.timezone,
            "optimal_days": t.optimal_days,
            "optimal_hour_start": t.optimal_hour_start,
            "optimal_hour_end": t.optimal_hour_end,
            "shorts_spread_hours": t.shorts_spread_hours,
        }
        for t in channel.publish_timings
    ]


def _platform_summary(channel: Channel) -> list[dict]:
    """Platform/language/verified/active only — `credentials_encrypted` is
    deliberately never read or copied into a snapshot (CLAUDE.md §30)."""
    return [
        {
            "platform": p.platform,
            "language": p.language,
            "verified": p.verified,
            "active": p.active,
        }
        for p in channel.platforms
    ]


def build_channel_config_snapshot(channel: Channel) -> dict:
    """Build an immutable, point-in-time snapshot of `channel`'s effective
    configuration.

    `channel` must already be eager-loaded with `config`/`languages`/
    `voices`/`sources`/`platforms`/`publish_timings` — exactly what
    `channels_service.get_by_id()`/`get_all_for_user()` already return
    (same precondition `check_activation_readiness()` already documents).
    Issues no queries of its own and makes no network/API call.

    Every value here is a snapshot of the *current* row state at the
    moment this function runs — the caller (a future phase) decides when
    to call it and is responsible for never calling it again for the same
    content run afterward. This module enforces immutability only in the
    sense that the function is pure and produces a plain dict with no
    further mutation hooks; it cannot prevent a caller from calling it
    twice and producing two different dicts. That ordering contract
    belongs to whichever future phase wires this into a generation-start
    hook.

    ChannelConfig is a one-to-one row keyed directly by `channel_id`
    (see app/models/channel_config.py) — there is no separate version
    column today, so `channel_config_id` is `channel.config.channel_id`
    (identical to `channel_id`) when a config row exists, or `None` when
    it does not. This is documented explicitly rather than invented as a
    fake version number.
    """
    config = channel.config
    return {
        "channel_id": str(channel.id),
        "channel_config_id": str(config.channel_id) if config is not None else None,
        "content_mode": getattr(config, "content_mode", "single_story") if config else "single_story",
        "script_source": getattr(config, "script_source", "reddit") if config else "reddit",
        "output_mode": getattr(config, "output_mode", "youtube_and_shorts") if config else "youtube_and_shorts",
        "visual_style": getattr(config, "visual_style", "documentary") if config else "documentary",
        "image_style": getattr(config, "image_style", "photorealistic") if config else "photorealistic",
        "languages": [
            {"language": l.language, "channel_name": l.channel_name} for l in channel.languages
        ],
        "platforms": _platform_summary(channel),
        "videos_per_week": getattr(config, "videos_per_week", 3) if config else 3,
        "publish_timing_summary": _publish_timing_summary(channel),
        "voices": _voice_summary(channel),
        "source_summary": _source_summary(channel),
        "captured_at": datetime.now(timezone.utc).isoformat(),
    }


def validate_channel_config_snapshot(snapshot: dict) -> list[ConfigSnapshotIssue]:
    """Deterministic structural validation of a snapshot dict.

    Returns a list of issues (empty list means valid) rather than raising,
    matching this package's existing `check_activation_readiness()`/
    `validate_v3_channel_config()` convention of "collect every issue, let
    the caller decide" (CLAUDE.md §15 rule 2 — business rules live in
    Python, not in a prompt or in an exception that hides the other
    problems).

    Checks performed:
      1. `snapshot` is a dict at all.
      2. Every field in `_REQUIRED_TOP_LEVEL_FIELDS` is present as a key
         (missing key only — an empty list/None value for a non-critical
         field is not itself a structural failure; see module docstring).
      3. `channel_id` is present and non-empty (the one field with no
         legitimate "unconfigured" empty state).
    """
    issues: list[ConfigSnapshotIssue] = []

    if not isinstance(snapshot, dict):
        return [ConfigSnapshotIssue(
            severity="BLOCKING", field="<root>", code="not_a_dict",
            message=f"Snapshot must be a dict, got {type(snapshot).__name__}.",
        )]

    for field in _REQUIRED_TOP_LEVEL_FIELDS:
        if field not in snapshot:
            issues.append(ConfigSnapshotIssue(
                severity="BLOCKING", field=field, code="missing_field",
                message=f"Snapshot is missing required field '{field}'.",
            ))

    if not snapshot.get("channel_id"):
        issues.append(ConfigSnapshotIssue(
            severity="BLOCKING", field="channel_id", code="missing_channel_id",
            message="Snapshot has no channel_id — every snapshot must be traceable to a channel.",
        ))

    return issues


def attach_snapshot_to_content(content, snapshot: dict) -> None:
    """Set `content.channel_config_snapshot` to `snapshot`, in-memory only.

    Purely local — no query, no commit, no network/API call. The caller
    owns the database session and decides when (or whether) to commit.
    This function refuses to overwrite an already-set snapshot, enforcing
    the immutability rule at the one point where a future caller could
    otherwise violate it by calling this twice for the same content row.

    Not called from any route, task, or agent today — see module
    docstring. A future phase decides the actual generation-start hook.
    """
    existing = getattr(content, "channel_config_snapshot", None)
    if existing is not None:
        raise ValueError(
            "content.channel_config_snapshot is already set — snapshots are immutable "
            "once attached and must never be overwritten."
        )
    content.channel_config_snapshot = snapshot
