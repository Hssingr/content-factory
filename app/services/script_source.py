"""Shared ``ChannelConfig.script_source`` normalization (CLAUDE.md §7).

Extracted out of ``app.agents.agent1_setup.services.v3_config_rules`` so
Agent 2 can read the canonical spelling of a channel's configured
``script_source`` without importing Agent 1 business logic across the
CLAUDE.md §6A ownership boundary — matches the existing
``app.services.video_sections`` shared-utility precedent (§7.5). Pure,
generic, side-effect-free: no database access, no mutation of any row.
"""

from __future__ import annotations

# Aliases that mean the same thing but were spelled differently by an
# internal caller, a future schema revision, or an operator-provided value
# that did not go through the Pydantic schema (the schema itself only ever
# accepts "ai_generated" — "claude_generated" can only reach this function
# via a direct Python call, not the HTTP API).
_SCRIPT_SOURCE_ALIASES: dict[str, str] = {
    "claude_generated": "ai_generated",
}


def normalize_script_source(script_source: str) -> str:
    """Map known aliases to their canonical V3.2-schema spelling.

    Pure function: returns a new string, never writes anything. A caller
    that wants the normalized form persisted must do so explicitly and
    separately — this function never updates a ``ChannelConfig`` row.
    """
    return _SCRIPT_SOURCE_ALIASES.get(script_source, script_source)
