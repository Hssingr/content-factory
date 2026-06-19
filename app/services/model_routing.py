"""Model routing table — maps pipeline tasks to the appropriate Claude model.

Resolution order (highest → lowest precedence):
  1. Explicit ``model_override`` argument on a call (rare, discouraged).
  2. Dev override: when ``CLAUDE_TIER=dev`` in the environment, all tasks route to Haiku
     EXCEPT ``story_research`` (web_search tool) and ``auto_correction`` (correction quality).
  3. ``MODEL_ROUTING[task]`` — the production table.

Unknown tasks raise ``ValueError`` immediately (fail-loud, no default fallback).
"""

SONNET = "claude-sonnet-4-6"
HAIKU  = "claude-haiku-4-5-20251001"

# Keys are canonical task identifiers passed as ``task=`` to call_claude*.
# Haiku: cheap/fast tasks — quality checks, scoring, short generations where
#        output variation is low-stakes or immediately Python-validated.
# Sonnet: every task where generation quality or tool capability matters.
#         Includes story_research (web_search tool — Haiku does not support it).
#         Includes channel_suggestion (Agent 1 UX — bad suggestions degrade onboarding).
MODEL_ROUTING: dict[str, str] = {
    # ── Core generation (Sonnet) ──────────────────────────────────────────
    "script_generation":       SONNET,
    "native_adaptation":       SONNET,
    "quality_rewrite":         SONNET,
    "auto_correction":         SONNET,  # dev-mode exception — correction quality matters
    "storyboard":              SONNET,
    "story_gate_scoring":      SONNET,  # single-story gate: 18-dimension structured call
    "revision":                SONNET,
    "story_research":          SONNET,  # uses web_search tool — Haiku does not support it
    "channel_suggestion":      SONNET,  # Agent 1 UX — poor suggestions harm onboarding quality
    "story_blueprint":         SONNET,  # Narrative skeleton — foundation for all sections
    "section_generation":      SONNET,  # Creative writing per section
    "short_script":            SONNET,  # Standalone short planning: TikTok-optimised episode script
    # ── Fast / cheap (Haiku) ─────────────────────────────────────────────
    "script_quality_check":    HAIKU,
    "script_validation":       HAIKU,
    "media_scoring":           HAIKU,
    "content_reformat":        HAIKU,   # reformatting prose discovery output to JSON
    "section_validation":      HAIKU,   # Agent 4 section quality check
    "section_splitting":       HAIKU,   # Agent 4 legacy section splitter
    "visual_reinterpretation": HAIKU,   # Agent 4 alternative visual query generation
    "global_validation":       HAIKU,   # Narrative coherence check after assembly
    "shorts_planner":          HAIKU,   # Standalone short planning: structural planning for Short episodes
    "short_storyboard_remap":  HAIKU,   # Child short visual remap: remap parent beats to Short narration timing
}


def resolve_model(task: str, model_override: str | None = None) -> str:
    """Return the Claude model ID for a given pipeline task.

    Resolution order:
      1. ``model_override`` when set — bypasses the table entirely (discouraged).
      2. Dev override: ``CLAUDE_TIER=dev`` → Haiku for all tasks except ``story_research``
         (web_search tool requires Sonnet regardless of tier).
      3. ``MODEL_ROUTING[task]`` — production routing table.

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
    # Dev tier: cheap iterations on Haiku.
    # Exceptions that always use Sonnet regardless of tier:
    #   story_research  — web_search tool is not available on Haiku.
    #   auto_correction — correction quality is critical; Haiku produces regressions.
    # Import settings here (not at module level) so pydantic-settings has already
    # loaded .env before this call.
    from app.config import settings
    _DEV_SONNET_EXCEPTIONS = {"story_research", "auto_correction"}
    if settings.claude_tier.lower() == "dev" and task not in _DEV_SONNET_EXCEPTIONS:
        return HAIKU
    return MODEL_ROUTING[task]
