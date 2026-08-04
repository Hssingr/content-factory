"""Agent 4 — capability-based visual source/model router (Phase 14.6 foundation).

This module owns the *decision* of which image source/model a beat should use
(reuse / stock / Schnell / Dev / Pro-family). It does not call fal.ai itself
— `flux_generator.py` remains the only direct `fal_client` integration point
(CLAUDE.md §27.1/§7.1-style boundary: provider calls stay behind one wrapper).

Routing contract (Tier 5 R16 was explicitly enabled by the operator):
  - Ordinary generated beats route to Schnell unless
    `settings.image_routing_enabled` is True AND the relevant tier flag
    (`image_routing_allow_dev` / `image_routing_allow_pro`) is True AND the
    first-pass eligibility heuristic says the beat qualifies.
  - Pro-family usage is hard-capped per content by
    `settings.image_routing_max_pro_per_content` (default conservative/0).
  - Stock is reserved, never selected — no stock provider exists yet.

See `code_report/phase_14_5_model_routing_feasibility.md` for the research
this design is based on, and `code_report/phase_14_6_unified_visual_router.md`
for this phase's own report.

The model-capability table and fal.ai payload/cache-key construction
(`MODEL_CAPABILITIES`, `ModelKey`, `SizeMode`, `SafetyMode`,
`build_fal_payload()`, `build_cache_key_material()`) moved to the shared
`app.services.flux_client` module (Agent 6 roadmap, Phase A,
`code_report/agent6_metadata_roadmap.md`) — they are provider-integration
details `_call_fal()` itself depends on, not beat-routing decisions, and a
second agent (Agent 6) needed them without importing this package. Re-exported
below so every existing caller of `image_router.MODEL_CAPABILITIES` /
`image_router.build_fal_payload` / `image_router.build_cache_key_material` /
`image_router.ModelKey` is unaffected.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

from app.services.flux_client import (
    MODEL_CAPABILITIES,
    ModelKey,
    SafetyMode,
    SizeMode,
    build_cache_key_material,
    build_fal_payload,
)

logger = logging.getLogger(__name__)

RouteSource = Literal["reuse", "stock", "generated", "fallback"]

__all__ = [
    "MODEL_CAPABILITIES",
    "ModelKey",
    "SafetyMode",
    "SizeMode",
    "build_cache_key_material",
    "build_fal_payload",
    "RouteSource",
    "ImageRoute",
    "select_route",
]

# Beats that already qualify for Dev/Pro under the heuristic (kept narrow and
# additive — Phase 14.5's recommended first pass, not a quality/cost policy).
# Known limits, accepted deliberately: _REVEAL_KEYWORDS is English-only (a
# non-English source script gets no reveal-based upgrades — same
# source-language-only convention as the stylistic script checks, CLAUDE.md
# §5.3/S-6), and `thumbnail_candidate` is a future field nothing sets yet.
_IMPORTANT_VISUAL_CATEGORIES = {"person"}
_REVEAL_KEYWORDS = (
    "revealed", "discovered", "shocking", "turns out", "truth was",
    "finally", "secret", "confession", "exposed",
)


@dataclass(frozen=True)
class ImageRoute:
    """The router's decision for one `ImageRequest` — model/source only, no payload."""

    model_key: ModelKey | None     # None when source="reuse" or "stock" (no generation needed)
    source: RouteSource
    reason: str


def _heuristic_qualifies_for_higher_tier(beat: dict) -> bool:
    """First-pass Dev/Pro eligibility heuristic (Phase 14.5 recommendation).

    The pure function keeps conservative argument defaults for isolated callers;
    production passes the operator-enabled settings from app.config.
    """
    if int(beat.get("beat_order", 0)) == 0:
        return True  # cover frame
    if beat.get("beat_intensity") == "high":
        return True
    if beat.get("visual_category") in _IMPORTANT_VISUAL_CATEGORIES:
        return True
    script_text = str(beat.get("script_text", "")).lower()
    if any(kw in script_text for kw in _REVEAL_KEYWORDS):
        return True
    if beat.get("thumbnail_candidate"):
        return True
    return False


def select_route(
    beat: dict,
    content_id: str,
    *,
    routing_enabled: bool = False,
    allow_dev: bool = False,
    allow_pro: bool = False,
    max_pro_per_content: int = 0,
    pro_used_so_far: int = 0,
) -> ImageRoute:
    """Decide which model/source a beat's image generation should use.

    Conservative by construction: every parameter defaults to "routing off,
    everything goes to Schnell" so callers that do not pass routing flags get
    today's exact behavior.
    """
    beat_order = beat.get("beat_order", beat.get("section_order", 0))

    media_url = str(beat.get("media_url") or "")
    if media_url.startswith("cache/"):
        route = ImageRoute(None, "reuse", "beat_already_has_local_media")
        logger.info(
            "IMAGE_ROUTE_SELECTED content=%s beat=%s model=%s source=%s reason=%s",
            content_id, beat_order, route.model_key, route.source, route.reason,
        )
        return route

    media_strategy = beat.get("media_strategy", "flux_generated")
    if media_strategy not in ("flux_generated", "remotion_text_card"):
        # Stock or any other reserved strategy: no real provider exists yet.
        # _build_beat_section() already overrides stock to flux_generated
        # before beats reach this point, so this branch is defensive/reserved.
        logger.info(
            "IMAGE_ROUTE_FALLBACK content=%s beat=%s reason=stock_disabled_use_schnell",
            content_id, beat_order,
        )
        route = ImageRoute("schnell", "generated", "stock_disabled_fallback_schnell")
        logger.info(
            "IMAGE_ROUTE_SELECTED content=%s beat=%s model=%s source=%s reason=%s",
            content_id, beat_order, route.model_key, route.source, route.reason,
        )
        return route

    if not routing_enabled or not (allow_dev or allow_pro):
        route = ImageRoute("schnell", "generated", "routing_disabled_default_schnell")
        logger.info(
            "IMAGE_ROUTE_SELECTED content=%s beat=%s model=%s source=%s reason=%s",
            content_id, beat_order, route.model_key, route.source, route.reason,
        )
        return route

    qualifies = _heuristic_qualifies_for_higher_tier(beat)
    if qualifies and allow_pro and pro_used_so_far < max_pro_per_content:
        route = ImageRoute("pro_1_1", "generated", "heuristic_qualified_pro_within_cap")
    elif qualifies and allow_dev:
        route = ImageRoute("dev", "generated", "heuristic_qualified_dev")
    else:
        reason = (
            "heuristic_not_qualified"
            if not qualifies
            else "pro_cap_reached_and_dev_not_allowed"
        )
        route = ImageRoute("schnell", "generated", reason)

    logger.info(
        "IMAGE_ROUTE_SELECTED content=%s beat=%s model=%s source=%s reason=%s",
        content_id, beat_order, route.model_key, route.source, route.reason,
    )
    return route
