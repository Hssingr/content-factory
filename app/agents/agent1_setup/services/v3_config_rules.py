"""Content Factory V3 channel-configuration rule helpers (Phase Agent1-V3.3).

Pure, local, side-effect-free functions only — no database access, no
network/API call, no mutation of any `ChannelConfig` row. These helpers
classify the V3.2 groundwork fields (`content_mode`, `script_source`,
`output_mode` — see CLAUDE.md §8.1) into two independent questions:

  "supported"  — is this value accepted by the V3.2 Pydantic schema at all
                 (app/schemas/channel.py's `ContentMode`/`ScriptSource`/
                 `OutputMode` Literal types)?
  "executable" — does ANY agent in this codebase today actually run
                 differently, or run at all, for this value? `content_mode`
                 is executable only for `single_story`; `script_source` is
                 executable for `reddit`/`ai_generated`, both only in
                 combination with `single_story`; `output_mode` is
                 executable for all three of `youtube_and_shorts`,
                 `youtube_long_only`, and `shorts_only` — see CLAUDE.md
                 §8.1 and §9-§11A for what Agent 2-5 actually do with each.

This module's own helpers perform no database access or enforcement
themselves — `validate_v3_channel_config()` is used by
`activation_readiness.check_activation_readiness()` (§8.3) to gate channel
activation, but this module still has no callers anywhere in Agent 2/3/4/5.
"""

from __future__ import annotations

from typing import TypedDict

from app.services.script_source import _SCRIPT_SOURCE_ALIASES, normalize_script_source

# ── Supported value sets (must stay in sync with app/schemas/channel.py's
#    ContentMode / ScriptSource / OutputMode Literal types — "supported"
#    here means "the V3.2 schema accepts it", not "an agent runs it") ───────

SUPPORTED_CONTENT_MODES:  frozenset[str] = frozenset({"single_story", "limited_series", "ongoing_series"})
SUPPORTED_SCRIPT_SOURCES: frozenset[str] = frozenset({"reddit", "ai_generated", "user_provided", "hybrid"})
SUPPORTED_OUTPUT_MODES:   frozenset[str] = frozenset({"youtube_and_shorts", "youtube_long_only", "shorts_only"})

# Only these combinations match what Agent 2-5 actually run today.
# ai_generated is executable (AI-Generated Story Discovery, see
# code_report/ai_generated_story_discovery_design.md and CLAUDE.md §9.5)
# alongside reddit — both are real discovery paths for single_story content.
_EXECUTABLE_CONTENT_MODE = "single_story"
_EXECUTABLE_SCRIPT_SOURCES = frozenset({"reddit", "ai_generated"})
# youtube_and_shorts: the default parent + standalone Shorts architecture.
# youtube_long_only: same parent pipeline with the Shorts planner skipped —
# run_script_workflow() branches on ChannelConfig.output_mode (post-roadmap
# deep audit; the first real runtime consumer of output_mode).
# shorts_only: a Solo Short — a standalone short episode with no parent at
# all, written directly from source material and rendered through the
# existing Short path (code_report/output_mode_shorts_only_and_youtube_long_
# only_roadmap.md). run_discovery()/_create_manual_fallback() create the
# content row as is_short_episode=True; run_script_workflow() and
# run_visual_generation() both branch on that flag before any output_mode
# check is needed downstream.
_EXECUTABLE_OUTPUT_MODES = frozenset({"youtube_and_shorts", "youtube_long_only", "shorts_only"})


class V3ConfigIssue(TypedDict):
    """One finding returned by validate_v3_channel_config()."""
    severity: str   # "BLOCKING" | "INFO" — BLOCKING means not executable yet
    field:    str   # "content_mode" | "script_source" | "output_mode"
    code:     str   # short machine-readable reason code
    message:  str   # human-readable, includes a coming-soon reason where relevant


# normalize_script_source() is re-exported here (imported above) so existing
# callers that reach it as `v3_config_rules.normalize_script_source(...)`
# keep working — the canonical implementation now lives in
# app.services.script_source (CLAUDE.md §7), shared with Agent 2.


# ── content_mode ─────────────────────────────────────────────────────────────

def is_supported_content_mode(content_mode: str) -> bool:
    """True if the V3.2 schema accepts this content_mode value at all."""
    return content_mode in SUPPORTED_CONTENT_MODES


def is_executable_content_mode(content_mode: str) -> bool:
    """True only for 'single_story' — the one mode Agent 2 actually runs today.

    'limited_series' and 'ongoing_series' are schema-supported (an operator
    can already select and save them) but no agent has execution logic for
    either yet.
    """
    return content_mode == _EXECUTABLE_CONTENT_MODE


# ── script_source ─────────────────────────────────────────────────────────────

def is_supported_script_source(script_source: str) -> bool:
    """True if the V3.2 schema accepts this script_source value (after
    normalizing known aliases like 'claude_generated' -> 'ai_generated')."""
    return normalize_script_source(script_source) in SUPPORTED_SCRIPT_SOURCES


def is_executable_script_source(content_mode: str, script_source: str) -> bool:
    """True for script_source in {'reddit', 'ai_generated'} AND content_mode='single_story'.

    Agent 2's discovery flow (run_discovery() -> fetch_batch() or
    -> generate_story_premise()) fetches a real candidate story from a
    configured ChannelSource for 'reddit', or synthesizes an original premise
    grounded in channel niche/tone/description for 'ai_generated' — see
    code_report/ai_generated_story_discovery_design.md. There is still no
    operator-supplied ('user_provided') or mixed ('hybrid') script-origin
    path anywhere in Agent 2, for any content_mode. Both executable sources
    are also only meaningful in combination with 'single_story' — there is
    no per-episode discovery loop for 'limited_series'/'ongoing_series' to
    plug a source into yet.
    """
    normalized = normalize_script_source(script_source)
    return content_mode == _EXECUTABLE_CONTENT_MODE and normalized in _EXECUTABLE_SCRIPT_SOURCES


# ── output_mode ───────────────────────────────────────────────────────────────

def is_supported_output_mode(output_mode: str) -> bool:
    """True if the V3.2 schema accepts this output_mode value at all."""
    return output_mode in SUPPORTED_OUTPUT_MODES


def is_executable_output_mode(output_mode: str) -> bool:
    """True for 'youtube_and_shorts' (the default parent + standalone Shorts
    architecture, CLAUDE.md §3/§28), 'youtube_long_only' (same parent
    pipeline; run_script_workflow() skips run_shorts_planner() when
    ChannelConfig.output_mode is 'youtube_long_only' — logged as
    SHORTS_PLANNER_SKIPPED), and 'shorts_only' (a Solo Short — one
    standalone short episode with no parent per discovery cycle, written
    directly from source material; see code_report/output_mode_shorts_only_
    and_youtube_long_only_roadmap.md).
    """
    return output_mode in _EXECUTABLE_OUTPUT_MODES


# ── Coming-soon reason messages ─────────────────────────────────────────────

def coming_soon_reason(field: str, value: str) -> str | None:
    """Human-readable reason a supported-but-not-executable value isn't live
    yet. Returns None if the value is already executable or not a
    recognized supported value (callers should check is_supported_*() first
    if they want to distinguish "unsupported" from "no reason needed").
    """
    if field == "content_mode":
        if value == "limited_series":
            return (
                "limited_series is accepted by the V3 schema but Agent 2 has no "
                "multi-episode planning/execution logic yet — coming in a future phase."
            )
        if value == "ongoing_series":
            return (
                "ongoing_series is accepted by the V3 schema but Agent 2 has no "
                "open-ended series scheduling/execution logic yet — coming in a future phase."
            )
        return None
    if field == "script_source":
        normalized = normalize_script_source(value)
        if normalized == "user_provided":
            return (
                "user_provided is reserved for a future phase where an operator can submit "
                "their own narration text instead of Agent 2 discovering one."
            )
        if normalized == "hybrid":
            return (
                "hybrid is reserved for a future phase that combines a discovered source with "
                "AI-improvised material — not yet implemented."
            )
        if normalized == "reddit":
            return None
        return None
    if field == "output_mode":
        return None
    return None


# ── Combined validation ──────────────────────────────────────────────────────

def validate_v3_channel_config(config: dict) -> dict:
    """Classify one channel's V3 config fields as supported/executable and
    return structured issues for anything not yet executable.

    Args:
        config: A dict (or any object with the same keys via `.get()`-style
            access is NOT required — pass a plain dict, e.g.
            `{"content_mode": ..., "script_source": ..., "output_mode": ...}`,
            such as `ChannelConfigUpsert.model_dump()` or a subset of it).
            Missing keys are treated as the V3.2 schema defaults
            ("single_story" / "reddit" / "youtube_and_shorts") so calling
            this with a partial dict never raises.

    Returns:
        {
          "executable": bool,   # True only if every field is individually executable
          "supported":  bool,   # True if every field is at least schema-supported
          "issues":     list[V3ConfigIssue],
        }

    This function never touches the database and never raises for an
    unsupported value — an unsupported value is reported as a BLOCKING
    issue, not an exception, so a caller can decide what to do with it
    (this module enforces nothing on its own — see the module docstring).
    """
    content_mode  = config.get("content_mode", "single_story")
    script_source = config.get("script_source", "reddit")
    output_mode   = config.get("output_mode", "youtube_and_shorts")

    issues: list[V3ConfigIssue] = []

    if not is_supported_content_mode(content_mode):
        issues.append(V3ConfigIssue(
            severity="BLOCKING", field="content_mode", code="unsupported_value",
            message=f"content_mode={content_mode!r} is not a recognized V3 value "
                    f"(expected one of {sorted(SUPPORTED_CONTENT_MODES)}).",
        ))
    elif not is_executable_content_mode(content_mode):
        issues.append(V3ConfigIssue(
            severity="BLOCKING", field="content_mode", code="not_yet_executable",
            message=coming_soon_reason("content_mode", content_mode) or
                    f"content_mode={content_mode!r} is supported but not yet executable.",
        ))

    if not is_supported_script_source(script_source):
        issues.append(V3ConfigIssue(
            severity="BLOCKING", field="script_source", code="unsupported_value",
            message=f"script_source={script_source!r} is not a recognized V3 value "
                    f"(expected one of {sorted(SUPPORTED_SCRIPT_SOURCES)}, or the alias "
                    f"{sorted(_SCRIPT_SOURCE_ALIASES)}).",
        ))
    elif not is_executable_script_source(content_mode, script_source):
        normalized = normalize_script_source(script_source)
        reason = coming_soon_reason("script_source", script_source)
        if reason is None and normalized in _EXECUTABLE_SCRIPT_SOURCES:
            reason = (
                f"script_source={normalized!r} is only executable with content_mode='single_story' "
                f"(got content_mode={content_mode!r})."
            )
        issues.append(V3ConfigIssue(
            severity="BLOCKING", field="script_source", code="not_yet_executable",
            message=reason or f"script_source={script_source!r} is supported but not yet executable.",
        ))

    if not is_supported_output_mode(output_mode):
        issues.append(V3ConfigIssue(
            severity="BLOCKING", field="output_mode", code="unsupported_value",
            message=f"output_mode={output_mode!r} is not a recognized V3 value "
                    f"(expected one of {sorted(SUPPORTED_OUTPUT_MODES)}).",
        ))
    elif not is_executable_output_mode(output_mode):
        issues.append(V3ConfigIssue(
            severity="BLOCKING", field="output_mode", code="not_yet_executable",
            message=coming_soon_reason("output_mode", output_mode) or
                    f"output_mode={output_mode!r} is supported but not yet executable.",
        ))

    supported = (
        is_supported_content_mode(content_mode)
        and is_supported_script_source(script_source)
        and is_supported_output_mode(output_mode)
    )
    executable = (
        is_executable_content_mode(content_mode)
        and is_executable_script_source(content_mode, script_source)
        and is_executable_output_mode(output_mode)
    )

    return {"executable": executable, "supported": supported, "issues": issues}
