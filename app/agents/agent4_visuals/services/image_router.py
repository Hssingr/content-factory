"""Agent 4 — capability-based visual source/model router (Phase 14.6 foundation).

This module owns the *decision* of which image source/model a beat should use
(reuse / stock / Schnell / Dev / Pro-family) and the *payload normalization*
for whichever fal.ai Flux endpoint is selected. It does not call fal.ai itself
— `flux_generator.py` remains the only direct `fal_client` integration point
(CLAUDE.md §27.1/§7.1-style boundary: provider calls stay behind one wrapper).

Conservative-by-default contract (do not relax without an explicit config
change from the operator):
  - Text-card backgrounds (`purpose="text_card_background"`) ALWAYS route to
    Schnell, unconditionally, regardless of any routing config. This is the
    Phase 14.4 invariant and is enforced here as a hard short-circuit, not a
    heuristic.
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
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Literal

logger = logging.getLogger(__name__)

ModelKey = Literal["schnell", "dev", "pro_1_1", "pro_1_1_ultra", "flux_2_pro"]
SizeMode = Literal["image_size", "aspect_ratio"]
SafetyMode = Literal["enable_safety_checker", "safety_tolerance", "both"]
RouteSource = Literal["reuse", "stock", "generated", "fallback"]
Purpose = Literal["beat_image", "text_card_background"]

_TEXT_CARD_PURPOSE: Purpose = "text_card_background"

# ── Model capability table ──────────────────────────────────────────────────
# Endpoints verified against official fal.ai docs during Phase 14.5
# (code_report/phase_14_5_model_routing_feasibility.md). Defaults intentionally
# preserve the repo's existing Schnell behavior (8 steps) rather than fal.ai's
# own documented Schnell default (4) — constraint: preserve existing behavior.
MODEL_CAPABILITIES: dict[ModelKey, dict] = {
    "schnell": {
        # Intentionally NOT updated to fal.ai's current documented endpoint
        # name ("fal-ai/flux/schnell", per Phase 14.5's research) — this is
        # the exact literal string the repo's `_FAL_ENDPOINT` constant has
        # used in real production calls since before this phase. Changing it
        # would be a live-behavior change this phase cannot verify (no live
        # fal.ai calls are permitted here; Phase 14.5 flagged this exact
        # endpoint-name discrepancy as needing an operator-run live canary
        # before ever changing it). Preserve-default-behavior wins.
        "endpoint": "fal-ai/flux-1/schnell",
        "size_mode": "image_size",
        "supports_steps": True,
        "default_steps": 8,
        "supports_guidance": False,
        "default_guidance": None,
        "supports_seed": True,
        "safety_mode": "enable_safety_checker",
        "output_formats": ("jpeg", "png"),
    },
    "dev": {
        "endpoint": "fal-ai/flux/dev",
        "size_mode": "image_size",
        "supports_steps": True,
        "default_steps": 28,
        "supports_guidance": True,
        "default_guidance": 3.5,
        "supports_seed": True,
        "safety_mode": "enable_safety_checker",
        "output_formats": ("jpeg", "png"),
    },
    "pro_1_1": {
        "endpoint": "fal-ai/flux-pro/v1.1",
        "size_mode": "image_size",
        "supports_steps": True,
        "default_steps": 28,
        "supports_guidance": True,
        "default_guidance": 3.5,
        "supports_seed": True,
        "safety_mode": "safety_tolerance",
        "output_formats": ("jpeg", "png"),
    },
    "pro_1_1_ultra": {
        "endpoint": "fal-ai/flux-pro/v1.1-ultra",
        "size_mode": "aspect_ratio",
        "supports_steps": False,
        "default_steps": None,
        "supports_guidance": False,
        "default_guidance": None,
        "supports_seed": True,
        "safety_mode": "safety_tolerance",
        "output_formats": ("jpeg", "png"),
    },
    "flux_2_pro": {
        "endpoint": "fal-ai/flux-2-pro",
        "size_mode": "image_size",
        "supports_steps": False,
        "default_steps": None,
        "supports_guidance": False,
        "default_guidance": None,
        "supports_seed": True,
        "safety_mode": "both",
        "output_formats": ("jpeg", "png"),
    },
}

_DEFAULT_SAFETY_TOLERANCE = "2"  # fal.ai Pro-family enum (1=strictest .. 6=most permissive)

# Beats that already qualify for Dev/Pro under the heuristic (kept narrow and
# additive — Phase 14.5's recommended first pass, not a quality/cost policy).
_IMPORTANT_VISUAL_CATEGORIES = {"person"}
_REVEAL_KEYWORDS = (
    "revealed", "discovered", "shocking", "turns out", "truth was",
    "finally", "secret", "confession", "exposed",
)


@dataclass(frozen=True)
class ImageRequest:
    """Pipeline-level description of one image to produce — provider-agnostic."""

    prompt: str
    content_id: str
    beat_order: int
    purpose: Purpose = "beat_image"
    target_width: int = 1920
    target_height: int = 1080
    aspect_ratio: str = "16:9"
    seed: int | None = None
    output_format: Literal["jpeg", "png"] = "jpeg"
    cache_namespace: str = ""


@dataclass(frozen=True)
class ImageRoute:
    """The router's decision for one `ImageRequest` — model/source only, no payload."""

    model_key: ModelKey | None     # None when source="reuse" or "stock" (no generation needed)
    source: RouteSource
    reason: str


@dataclass(frozen=True)
class ImageResult:
    """Normalized outcome of fulfilling an `ImageRequest` — local-only media reference."""

    media_url: str | None          # local cache/... path only; never a remote URL
    provider: str
    model_key: ModelKey | None
    source: RouteSource
    seed: int | None = None
    safety: dict[str, object] = field(default_factory=dict)


def _heuristic_qualifies_for_higher_tier(beat: dict) -> bool:
    """First-pass Dev/Pro eligibility heuristic (Phase 14.5 recommendation).

    Disabled by default at the call site (routing/tier flags default False) —
    this function only matters once an operator explicitly opts in.
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
    purpose: Purpose = "beat_image",
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

    if purpose == _TEXT_CARD_PURPOSE:
        route = ImageRoute("schnell", "generated", "text_card_schnell_only")
        logger.info(
            "IMAGE_ROUTE_SELECTED content=%s beat=%s purpose=%s model=%s source=%s reason=%s",
            content_id, beat_order, purpose, route.model_key, route.source, route.reason,
        )
        return route

    media_url = str(beat.get("media_url") or "")
    if media_url.startswith("cache/"):
        route = ImageRoute(None, "reuse", "beat_already_has_local_media")
        logger.info(
            "IMAGE_ROUTE_SELECTED content=%s beat=%s purpose=%s model=%s source=%s reason=%s",
            content_id, beat_order, purpose, route.model_key, route.source, route.reason,
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
            "IMAGE_ROUTE_SELECTED content=%s beat=%s purpose=%s model=%s source=%s reason=%s",
            content_id, beat_order, purpose, route.model_key, route.source, route.reason,
        )
        return route

    if not routing_enabled or not (allow_dev or allow_pro):
        route = ImageRoute("schnell", "generated", "routing_disabled_default_schnell")
        logger.info(
            "IMAGE_ROUTE_SELECTED content=%s beat=%s purpose=%s model=%s source=%s reason=%s",
            content_id, beat_order, purpose, route.model_key, route.source, route.reason,
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
        "IMAGE_ROUTE_SELECTED content=%s beat=%s purpose=%s model=%s source=%s reason=%s",
        content_id, beat_order, purpose, route.model_key, route.source, route.reason,
    )
    return route


def build_fal_payload(
    model_key: ModelKey,
    prompt: str,
    *,
    width: int = 1920,
    height: int = 1080,
    aspect_ratio: str = "16:9",
    seed: int | None = None,
    output_format: Literal["jpeg", "png"] = "jpeg",
) -> dict:
    """Build a fal.ai request payload containing only the fields the chosen
    model's capability entry supports — never a fixed Schnell-shaped dict.

    Pure function: no network call, no fal_client import.
    """
    caps = MODEL_CAPABILITIES[model_key]

    payload: dict = {
        "prompt": prompt,
        "num_images": 1,
        "output_format": output_format,
    }

    if caps["size_mode"] == "image_size":
        payload["image_size"] = {"width": width, "height": height}
    else:  # "aspect_ratio"
        payload["aspect_ratio"] = aspect_ratio

    if caps["supports_steps"]:
        payload["num_inference_steps"] = caps["default_steps"]

    if caps["supports_guidance"]:
        payload["guidance_scale"] = caps["default_guidance"]

    if caps["supports_seed"] and seed is not None:
        payload["seed"] = seed

    safety_mode = caps["safety_mode"]
    if safety_mode == "enable_safety_checker":
        payload["enable_safety_checker"] = True
    elif safety_mode == "safety_tolerance":
        payload["safety_tolerance"] = _DEFAULT_SAFETY_TOLERANCE
    elif safety_mode == "both":
        payload["enable_safety_checker"] = True
        payload["safety_tolerance"] = _DEFAULT_SAFETY_TOLERANCE

    logger.debug(
        "FAL_IMAGE_REQUEST_PREPARED model=%s endpoint=%s payload_keys=%s",
        model_key, caps["endpoint"], sorted(payload.keys()),
    )
    return payload


def build_cache_key_material(
    model_key: ModelKey,
    prompt: str,
    *,
    width: int = 1920,
    height: int = 1080,
    cache_key_extra: str = "",
) -> str:
    """Build the hash input for the local Flux cache filename.

    Backward-compatible by construction: for the Schnell tier (the default,
    and the tier every pre-Phase-14.6 caller used, including Phase 14.4's
    text-card backgrounds), this reproduces the exact pre-Phase-14.6 material
    — ``prompt`` alone, or ``f"{cache_key_extra}\\n{prompt}"`` when a
    namespace is given — so every existing cached image (parent beats, child
    beats, and Phase 14.4 text-card backgrounds) keeps being reused with zero
    cache invalidation. A model-qualified prefix is added only for a
    non-Schnell tier, which has no pre-existing cache to preserve and would
    otherwise collide with Schnell's cache entry for the same prompt.
    """
    if model_key == "schnell":
        return f"{cache_key_extra}\n{prompt}" if cache_key_extra else prompt

    parts = [f"model={model_key}", f"size={width}x{height}"]
    if cache_key_extra:
        parts.append(cache_key_extra)
    return "\n".join(parts) + "\n" + prompt
