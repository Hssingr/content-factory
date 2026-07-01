"""Content Factory V3 channel-configuration rule helpers (Phase Agent1-V3.3).

Pure, local, side-effect-free functions only — no database access, no
network/API call, no mutation of any `ChannelConfig` row. These helpers
classify the V3.2 groundwork fields (`content_mode`, `script_source`,
`output_mode` — see CLAUDE.md §8.1) into two independent questions:

  "supported"  — is this value accepted by the V3.2 Pydantic schema at all
                 (app/schemas/channel.py's `ContentMode`/`ScriptSource`/
                 `OutputMode` Literal types)?
  "executable" — does ANY agent in this codebase today actually run
                 differently, or run at all, for this value? As of this
                 phase the answer is "no" for everything except the single
                 combination that already matches current real behavior
                 (`single_story` + `reddit` + `youtube_and_shorts`) — see
                 CLAUDE.md §8.1 and §9-§11A for what Agent 2-5 actually do.

This module is NOT wired into any route, activation check, or agent yet
(per Phase Agent1-V3.3's brief: "do not enforce it globally yet unless
safe"). It exists so a future phase has a single, already-tested place to
ask "is this config combination real yet?" before deciding whether to wire
enforcement into the activation route, the frontend, or an agent itself.
"""

from __future__ import annotations

from typing import TypedDict

# ── Supported value sets (must stay in sync with app/schemas/channel.py's
#    ContentMode / ScriptSource / OutputMode Literal types — "supported"
#    here means "the V3.2 schema accepts it", not "an agent runs it") ───────

SUPPORTED_CONTENT_MODES:  frozenset[str] = frozenset({"single_story", "limited_series", "ongoing_series"})
SUPPORTED_SCRIPT_SOURCES: frozenset[str] = frozenset({"reddit", "ai_generated", "user_provided", "hybrid"})
SUPPORTED_OUTPUT_MODES:   frozenset[str] = frozenset({"youtube_and_shorts", "youtube_long_only", "shorts_only"})

# Only this exact combination matches what Agent 2-5 actually run today.
_EXECUTABLE_CONTENT_MODE = "single_story"
_EXECUTABLE_SCRIPT_SOURCE = "reddit"
_EXECUTABLE_OUTPUT_MODE = "youtube_and_shorts"

# script_source aliases that mean the same thing but were spelled
# differently by an internal caller, a future schema revision, or an
# operator-provided value that did not go through the Pydantic schema (the
# schema itself only ever accepts "ai_generated" — "claude_generated" can
# only reach this module via a direct Python call, not the HTTP API).
# Pure mapping only — never mutates a DB row; see normalize_script_source().
_SCRIPT_SOURCE_ALIASES: dict[str, str] = {
    "claude_generated": "ai_generated",
}


class V3ConfigIssue(TypedDict):
    """One finding returned by validate_v3_channel_config()."""
    severity: str   # "BLOCKING" | "INFO" — BLOCKING means not executable yet
    field:    str   # "content_mode" | "script_source" | "output_mode"
    code:     str   # short machine-readable reason code
    message:  str   # human-readable, includes a coming-soon reason where relevant


# ── Normalization (pure — never touches the database) ──────────────────────

def normalize_script_source(script_source: str) -> str:
    """Map known aliases to their canonical V3.2-schema spelling.

    Pure function: returns a new string, never writes anything. Per the
    brief, this module must not silently change DB values — any caller
    that wants the normalized form persisted must do so explicitly and
    separately; this function only ever returns a value, it never updates
    a `ChannelConfig` row itself.
    """
    return _SCRIPT_SOURCE_ALIASES.get(script_source, script_source)


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
    """True only for script_source='reddit' AND content_mode='single_story'.

    Agent 2's discovery flow (run_discovery() -> fetch_batch()) always
    fetches a real candidate story from a configured ChannelSource today —
    there is no AI-improvised ('ai_generated'), operator-supplied
    ('user_provided'), or mixed ('hybrid') script-origin path anywhere in
    Agent 2, for any content_mode. The 'reddit' value is also only
    meaningful in combination with 'single_story' — there is no per-episode
    discovery loop for 'limited_series'/'ongoing_series' to plug a source
    into yet.
    """
    normalized = normalize_script_source(script_source)
    return content_mode == _EXECUTABLE_CONTENT_MODE and normalized == _EXECUTABLE_SCRIPT_SOURCE


# ── output_mode ───────────────────────────────────────────────────────────────

def is_supported_output_mode(output_mode: str) -> bool:
    """True if the V3.2 schema accepts this output_mode value at all."""
    return output_mode in SUPPORTED_OUTPUT_MODES


def is_executable_output_mode(output_mode: str) -> bool:
    """True only for 'youtube_and_shorts' — the existing parent + standalone
    Shorts architecture (CLAUDE.md §3/§28), which is what every channel
    actually produces today, unconditionally.

    'shorts_only' and 'youtube_long_only' are schema-supported but NOT
    executable: nothing in Agent 2/Agent 5 reads `output_mode` at all today
    — `run_shorts_planner()` always runs for every parent that reaches
    SCRIPTS_VALIDATED, and Agent 5 always renders the parent's main video.
    There is no config-driven switch anywhere to skip either half.
    """
    return output_mode == _EXECUTABLE_OUTPUT_MODE


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
        if normalized == "ai_generated":
            return (
                "ai_generated (also accepted as 'claude_generated') is reserved for a future "
                "phase where Agent 2 can write a script without a discovered source story — "
                "today every script is grounded in a real fetched story, for every content_mode."
            )
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
        if value == "shorts_only":
            return (
                "shorts_only is accepted by the V3 schema but the pipeline cannot yet run "
                "standalone-Shorts-only generation without a rendered parent video — "
                "run_shorts_planner() always requires a validated parent source script today."
            )
        if value == "youtube_long_only":
            return (
                "youtube_long_only is accepted by the V3 schema but nothing in Agent 2/Agent 5 "
                "reads output_mode yet to skip standalone Shorts planning/rendering — "
                "every channel produces both today, unconditionally."
            )
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
        if reason is None and normalized == _EXECUTABLE_SCRIPT_SOURCE:
            reason = (
                f"script_source='reddit' is only executable with content_mode='single_story' "
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
