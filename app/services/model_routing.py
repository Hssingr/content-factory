"""Model routing table — maps pipeline tasks to the appropriate Claude model.

Resolution order (highest → lowest precedence):
  1. Explicit ``model_override`` argument on a call (rare, discouraged).
  2. Dev override: when ``CLAUDE_TIER=dev`` in the environment, all tasks route to the configured secondary model
     EXCEPT ``story_research`` (web_search tool).
  3. ``MODEL_ROUTING[task]`` — the production slot table.

Unknown tasks raise ``ValueError`` immediately (fail-loud, no default fallback).
"""

DEFAULT_PRIMARY_MODEL = "claude-sonnet-5"
DEFAULT_SECONDARY_MODEL = "claude-sonnet-4-6"
PRIMARY_MODEL = "primary"
SECONDARY_MODEL = "secondary"


def _configured_model(slot: str) -> str:
    """Resolve a routing-table slot to the operator-configured model ID."""
    from app.config import settings
    if slot == PRIMARY_MODEL:
        return settings.primary_model
    if slot == SECONDARY_MODEL:
        return settings.secondary_model
    return slot

# Keys are canonical task identifiers passed as ``task=`` to call_claude*.
# Secondary model: cheap/fast tasks — quality checks, scoring, short generations where
#        output variation is low-stakes or immediately Python-validated.
# Primary model: every task where generation quality or tool capability matters.
#         Includes story_research (web_search tool — secondary model does not support it by default).
#         Includes channel_suggestion (Agent 1 UX — bad suggestions degrade onboarding).
MODEL_ROUTING: dict[str, str] = {
    # ── Core generation (primary) ──────────────────────────────────────────
    # Retired (post-roadmap deep audit): "script_generation" (zero call sites —
    # section generation uses "section_generation") and "auto_correction" (the
    # auto_correct_script prompt-repair layer was deleted with it).
    # Retired (fresh full-system audit §1.3): "revision" — the script-revision
    # chain was unreachable dead code (no script exists while a validation is
    # PENDING); Telegram CHANGE is now story-level feedback with no Claude call.
    "native_adaptation":       PRIMARY_MODEL,
    "storyboard":              PRIMARY_MODEL,
    "story_gate_scoring":      PRIMARY_MODEL,  # single-story gate structured call
    "story_research":          PRIMARY_MODEL,  # uses web_search tool — secondary model does not support it by default
    "channel_suggestion":      PRIMARY_MODEL,  # Agent 1 UX — poor suggestions harm onboarding quality
    "channel_research":        PRIMARY_MODEL,  # Agent 1 market-research estimate; nuanced strategy output
    "story_blueprint":         PRIMARY_MODEL,  # Narrative skeleton — foundation for all sections
    "section_generation":      PRIMARY_MODEL,  # Creative writing per section
    "short_script":            PRIMARY_MODEL,  # Standalone short planning: TikTok-optimised episode script
    # ── Fast / cheap (secondary) ─────────────────────────────────────────────
    "content_reformat":        SECONDARY_MODEL,   # reformatting prose discovery output to JSON
    "shorts_planner":          SECONDARY_MODEL,   # Standalone short planning: structural planning for Short episodes
    "short_storyboard_remap":  SECONDARY_MODEL,   # Child short visual remap: remap parent beats to Short narration timing
}


def resolve_model(task: str, model_override: str | None = None) -> str:
    """Return the Claude model ID for a given pipeline task.

    Resolution order:
      1. ``model_override`` when set — bypasses the table entirely (discouraged).
      2. Dev override: ``CLAUDE_TIER=dev`` → configured secondary model for all tasks except ``story_research``
         (web_search tool requires the configured primary model regardless of tier).
      3. ``MODEL_ROUTING[task]`` — production slot table.

    Args:
        task:           Canonical task key (must be present in MODEL_ROUTING).
        model_override: Explicit model ID; bypasses routing entirely when set.
                        Use sparingly — prefer the routing table.

    Returns:
        A Claude model ID string.

    Raises:
        ValueError: If ``task`` is not in MODEL_ROUTING and no override is given.
    """
    if model_override:
        return model_override
    if task not in MODEL_ROUTING:
        raise ValueError(
            f"Unknown Claude task '{task}'. "
            f"Add it to MODEL_ROUTING in app/services/model_routing.py. "
            f"Known tasks: {sorted(MODEL_ROUTING)}"
        )
    # Dev tier: cheap iterations on the configured secondary model.
    # Exception that always uses the configured primary model regardless of tier:
    #   story_research — web_search tool is not available on the secondary model by default.
    # Import settings here (not at module level) so pydantic-settings has already
    # loaded .env before this call.
    from app.config import settings
    _DEV_PRIMARY_MODEL_EXCEPTIONS = {"story_research"}
    if settings.claude_tier.lower() == "dev" and task not in _DEV_PRIMARY_MODEL_EXCEPTIONS:
        return settings.secondary_model
    return _configured_model(MODEL_ROUTING[task])
