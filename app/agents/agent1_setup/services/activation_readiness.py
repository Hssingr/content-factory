"""Channel activation readiness (Phase Agent1-V3.4).

The single backend source of truth for "is this channel actually ready to
activate?" — replaces the previous, looser `any(p.verified for p in
channel.platforms)` check that let a channel activate with only one of
several selected platforms verified (the mismatch the V3.1 audit found:
the frontend already required ALL selected platform×language rows
verified; the backend only required ANY).

`check_activation_readiness()` is a pure function over an already-loaded
`Channel` ORM object (its `config`/`languages`/`voices`/`sources`/
`platforms`/`publish_timings` relationships must already be eager-loaded —
exactly what `channels_service.get_by_id()`/`get_all_for_user()` already
return). It issues no queries of its own and makes no network/API call.

"Selected platforms" on the backend means every `ChannelPlatform` row that
exists for this channel — a row is only created once an operator has saved
credentials for that platform×language pair (`save_credentials()` /
`POST .../credentials`), so row-existence already IS the backend's
equivalent of the frontend's "selected" checkbox state.
"""

from __future__ import annotations

from typing import TypedDict

from app.models import Channel
from app.agents.agent1_setup.services.v3_config_rules import validate_v3_channel_config


class ReadinessIssue(TypedDict):
    """One reason a channel is not (yet) ready to activate."""
    severity: str   # "BLOCKING" — every issue this module returns is blocking;
                     # no WARNING-level issue currently exists, but the field
                     # is kept for parity with V3ConfigIssue and future use.
    code:     str    # short, machine-readable reason code
    message:  str    # human-readable detail


def _config_dict(channel: Channel) -> dict:
    """Extract the V3 fields from channel.config as a plain dict for
    validate_v3_channel_config() — falls back to schema defaults if the
    channel has no ChannelConfig row at all yet (handled separately as its
    own BLOCKING issue below, not silently treated as ready)."""
    if channel.config is None:
        return {}
    return {
        "content_mode":  getattr(channel.config, "content_mode", "single_story"),
        "script_source": getattr(channel.config, "script_source", "reddit"),
        "output_mode":   getattr(channel.config, "output_mode", "youtube_and_shorts"),
    }


def check_activation_readiness(channel: Channel) -> dict:
    """Classify whether `channel` may be activated right now.

    Returns:
        {
          "ready":    bool,
          "issues":   list[ReadinessIssue],   # every reason activation is blocked
          "warnings": list[ReadinessIssue],   # non-blocking observations (currently always [])
        }

    Checks performed (each independent — all issues are collected, not
    just the first one found, so a caller/log line can show the operator
    everything that needs fixing at once):

      1. A ChannelConfig row exists.
      2. The channel's V3 config (content_mode/script_source/output_mode)
         is fully EXECUTABLE per v3_config_rules.validate_v3_channel_config()
         — this is how limited_series/ongoing_series and any other
         not-yet-executable V3 value block activation.
      3. At least one ChannelLanguage row exists.
      4. Every configured language has at least one ChannelVoice row.
      5. script_source="reddit" (the only executable script source today)
         requires at least one ChannelSource row.
      6. At least one ChannelPublishTiming row exists.
      7. At least one ChannelPlatform row exists (something was selected
         and credentials were saved for it).
      8. EVERY existing ChannelPlatform row is verified=True — not just
         one of them. This is the fix for the V3.1-audited frontend/backend
         mismatch.
      9. output_mode="youtube_and_shorts" (the only executable output mode
         today) requires a ChannelPlatform row with platform="youtube"
         among the selected platforms.
    """
    issues: list[ReadinessIssue] = []

    # 1 & 2 — config existence + V3 executability
    if channel.config is None:
        issues.append(ReadinessIssue(
            severity="BLOCKING", code="missing_config",
            message="Channel has no ChannelConfig row yet — save Section 4 (Schedule) first.",
        ))
    else:
        v3_result = validate_v3_channel_config(_config_dict(channel))
        for v3_issue in v3_result["issues"]:
            issues.append(ReadinessIssue(
                severity="BLOCKING",
                code=f"v3_config:{v3_issue['field']}:{v3_issue['code']}",
                message=v3_issue["message"],
            ))

    # 3 — languages
    if not channel.languages:
        issues.append(ReadinessIssue(
            severity="BLOCKING", code="no_languages",
            message="No language is configured — save Section 2 (Languages) with at least one language.",
        ))

    # 4 — a voice per configured language
    languages_with_voice = {v.language for v in channel.voices}
    for lang_row in channel.languages:
        if lang_row.language not in languages_with_voice:
            issues.append(ReadinessIssue(
                severity="BLOCKING", code=f"missing_voice:{lang_row.language}",
                message=f"Language '{lang_row.language}' has no configured voice — "
                        f"save Section 3 (Voices) for this language.",
            ))

    # 5 — sources required for script_source="reddit" (the only executable source today)
    script_source = getattr(channel.config, "script_source", "reddit") if channel.config else "reddit"
    if script_source == "reddit" and not channel.sources:
        issues.append(ReadinessIssue(
            severity="BLOCKING", code="no_sources_for_reddit_mode",
            message="script_source='reddit' requires at least one content source — "
                    "save Section 5 (Content sources) with at least one source.",
        ))

    # 6 — publish timing
    if not channel.publish_timings:
        issues.append(ReadinessIssue(
            severity="BLOCKING", code="no_publish_timing",
            message="No publish timing is configured — save Section 4 (Schedule) timing.",
        ))

    # 7 & 8 — platforms selected, and ALL of them verified (not just one)
    if not channel.platforms:
        issues.append(ReadinessIssue(
            severity="BLOCKING", code="no_platforms_selected",
            message="No platform credentials have been saved — complete Tab 2 (Credentials) "
                    "for at least one platform.",
        ))
    else:
        unverified = [p for p in channel.platforms if not p.verified]
        for p in unverified:
            issues.append(ReadinessIssue(
                severity="BLOCKING", code=f"unverified_platform:{p.platform}:{p.language}",
                message=f"Platform '{p.platform}' ({p.language}) credentials are not verified yet.",
            ))

    # 9 — output_mode="youtube_and_shorts" requires a youtube platform row
    output_mode = getattr(channel.config, "output_mode", "youtube_and_shorts") if channel.config else "youtube_and_shorts"
    if output_mode == "youtube_and_shorts" and not any(p.platform == "youtube" for p in channel.platforms):
        issues.append(ReadinessIssue(
            severity="BLOCKING", code="youtube_required_for_output_mode",
            message="output_mode='youtube_and_shorts' requires a YouTube platform credential — "
                    "save credentials for YouTube in Tab 2.",
        ))

    return {"ready": not issues, "issues": issues, "warnings": []}
