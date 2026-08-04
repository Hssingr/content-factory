"""Shared fal.ai Flux client — the single provider-integration point for image
generation, usable by any agent (CLAUDE.md §11.5: "Do not add a second fal.ai
integration point").

Extracted from `app.agents.agent4_visuals.services.flux_generator` /
`app.agents.agent4_visuals.services.image_router` (Agent 6 roadmap,
`code_report/agent6_metadata_roadmap.md`, Phase A) so a second agent (Agent 6's
thumbnail generation) can reuse the exact same cascade/retry/cache-key/payload
logic without importing Agent 4's package — the same shared-service precedent
already used for `app.services.video_sections`, `app.services.script_source`,
and `app.services.story_entities` (CLAUDE.md §7.5-§7.7). `flux_generator.py`
and `image_router.py` import and re-export the names they still use, so every
existing Agent 4 call site is unaffected.

Owns:
  - The model-capability table and fal.ai request-payload construction
    (`MODEL_CAPABILITIES`, `build_fal_payload()`, `build_cache_key_material()`)
    — moved here from `image_router.py` because they are provider-integration
    details `_call_fal()` itself depends on, not beat-routing decisions.
    `image_router.py`'s own routing *decision* logic (`select_route()`,
    the Dev/Pro eligibility heuristic) stays in Agent 4 — it is genuinely
    storyboard-beat-specific and has no meaning for a single thumbnail.
  - The one real `fal_client` call site (`_call_fal()`).
  - The generic, non-beat-specific 3-tier cascade wrapper (`generate_beat_image()`)
    — despite the name (kept for backward compatibility with every existing
    Agent 4 caller), it takes a prompt string and has no beat-object coupling;
    Agent 6 calls it directly for a single thumbnail generation.

Does not own: beat-routing tier selection, pixel-defect healing, neighbor-fill,
or any other storyboard-sequence-specific concern — those remain in
`app.agents.agent4_visuals.services.flux_generator`/`image_router`.
"""

import hashlib
import logging
import time
from pathlib import Path
from typing import Literal

import fal_client
import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# ── Model capability table (moved from image_router.py) ─────────────────────
# Endpoints verified against official fal.ai docs during Phase 14.5
# (code_report/phase_14_5_model_routing_feasibility.md). Defaults intentionally
# preserve the repo's existing Schnell behavior (8 steps) rather than fal.ai's
# own documented Schnell default (4) — constraint: preserve existing behavior.
ModelKey = Literal["schnell", "dev", "pro_1_1", "pro_1_1_ultra", "flux_2_pro"]
SizeMode = Literal["image_size", "aspect_ratio"]
SafetyMode = Literal["enable_safety_checker", "safety_tolerance", "both"]

MODEL_CAPABILITIES: dict[ModelKey, dict] = {
    "schnell": {
        # Intentionally NOT updated to fal.ai's current documented endpoint
        # name ("fal-ai/flux/schnell", per Phase 14.5's research) — this is
        # the exact literal string used in real production calls since before
        # Phase 14.6 introduced this table. Changing it would be a
        # live-behavior change this phase cannot verify (no live fal.ai calls
        # are permitted here; Phase 14.5 flagged this exact endpoint-name
        # discrepancy as needing an operator-run live canary before ever
        # changing it). Preserve-default-behavior wins.
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
        "max_side": 1440,
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
_FAL_SIZE_MULTIPLE = 16

# ── Defaults + cascade/retry constants (moved from flux_generator.py) ───────
# Public (no leading underscore): these are now part of this shared module's
# real API surface, not a private implementation detail of one agent.
# `flux_generator.py` imports them under their original underscore-prefixed
# names via `import ... as _DEFAULT_WIDTH` etc. so its own remaining call
# sites need zero further changes.
DEFAULT_MODEL_KEY: ModelKey = "schnell"
DEFAULT_WIDTH = 1920
DEFAULT_HEIGHT = 1080
RETRY_DELAY_SEC = 2.0
GENERATION_TIMEOUT_SEC = 60.0
DOWNLOAD_TIMEOUT_SEC = 20.0

# ── Safe fallback prompts by environment (moved from flux_generator.py) ─────
# Used as attempt 3 when the Claude-written flux_prompt and its shortened form both
# fail. These are guaranteed-safe (no content-policy edge cases, no mood words, pure
# physical description) so they should always succeed.
ENV_SAFE_PROMPTS: dict[str, str] = {
    "underwater":       (
        "Sunlit underwater view of empty swimming pool, turquoise water caustics on "
        "tiled bottom, overhead sun filtered through clear water, wide shot, "
        "photorealistic, sharp focus, no people"
    ),
    "indoor_office":    (
        "Empty office desk with scattered papers and a desk lamp, morning side light "
        "through window blinds casting parallel shadows, neutral tones, photorealistic, "
        "sharp focus, no people, no text visible"
    ),
    "indoor_domestic":  (
        "Living room with sofa and coffee table, warm afternoon window light casting "
        "long shadows across wooden floor, photorealistic, sharp focus, no people"
    ),
    "forest_nature":    (
        "Sunlit forest path between tall trees, dappled light through green leaf canopy, "
        "moss-covered ground, wide shot, photorealistic, sharp focus, no people"
    ),
    "urban_street":     (
        "Empty city street corner in early morning, parked cars along wet sidewalk, "
        "shop fronts closed, soft overcast daylight, wide shot, photorealistic, "
        "sharp focus, no people"
    ),
    "corridor_interior": (
        "Long empty corridor with polished tiled floor, fluorescent overhead panels, "
        "closed doors on both sides, long receding perspective, photorealistic, "
        "sharp focus, no people"
    ),
    "abstract_dark":    (
        "Close-up of weathered concrete wall surface, geometric grid pattern from "
        "expansion joints, soft raking side light revealing texture, grey neutral tones, "
        "macro shot, photorealistic, sharp focus"
    ),
    "open_landscape":   (
        "Wide open field under partly cloudy sky, green grass and wildflowers in "
        "foreground, horizon line in distance, soft diffuse natural daylight, "
        "wide establishing shot, photorealistic, sharp focus, no people"
    ),
    "laboratory":       (
        "Empty laboratory bench with glass beakers and equipment, clean white surface, "
        "overhead fluorescent light, clinical white and brushed steel, photorealistic, "
        "sharp focus, no people"
    ),
    "industrial":       (
        "Large empty industrial warehouse interior, concrete floor, steel support beams, "
        "diffuse overhead skylight panels, wide shot, photorealistic, sharp focus, "
        "no people"
    ),
    "vehicle":          (
        "Interior of empty car looking through windshield at straight road ahead, "
        "morning daylight, dashboard and steering wheel visible, photorealistic, "
        "sharp focus, no people"
    ),
    "other":            (
        "Empty wooden table in neutral room, simple object composition, soft window "
        "light from left, photorealistic, sharp focus, clean background, no people, "
        "no text"
    ),
}


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
        payload["image_size"] = _legal_image_size_for_model(model_key, width, height)
    else:  # "aspect_ratio"
        payload["aspect_ratio"] = _resolve_aspect_ratio(width, height, aspect_ratio)

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


def _round_to_multiple(value: float, multiple: int = _FAL_SIZE_MULTIPLE) -> int:
    return max(multiple, int(round(value / multiple)) * multiple)


def _legal_image_size_for_model(model_key: ModelKey, width: int, height: int) -> dict[str, int]:
    """Return fal-legal pixel dimensions for image_size endpoints.

    Flux Pro v1.1 silently clamps oversized custom dimensions to its own
    max-side envelope and can change aspect ratio while doing it. Keep the
    requested aspect and scale only that tier into its legal max-side window.
    Schnell/Dev/Flux 2 Pro preserve their existing requested dimensions.
    """
    caps = MODEL_CAPABILITIES[model_key]
    max_side = caps.get("max_side")
    if not max_side or max(width, height) <= max_side:
        return {"width": width, "height": height}

    if width >= height:
        legal_width = int(max_side)
        legal_height = _round_to_multiple(legal_width * height / width)
    else:
        legal_height = int(max_side)
        legal_width = _round_to_multiple(legal_height * width / height)
    return {"width": legal_width, "height": legal_height}


def _resolve_aspect_ratio(width: int, height: int, requested: str) -> str:
    """Resolve aspect_ratio-mode payloads from the actual requested frame."""
    if width == height:
        return "1:1"
    requested = (requested or "").strip()
    if width > height:
        return "16:9" if requested in {"", "16:9"} else requested
    return "9:16" if requested in {"", "16:9"} else requested


_DEFAULT_SCHNELL_SIZE = (1920, 1080)  # matches DEFAULT_WIDTH/DEFAULT_HEIGHT above


def build_cache_key_material(
    model_key: ModelKey,
    prompt: str,
    *,
    width: int = 1920,
    height: int = 1080,
    cache_key_extra: str = "",
) -> str:
    """Build the hash input for the local Flux cache filename.

    Backward-compatible by construction: for the Schnell tier at its
    pre-existing default landscape size (1920x1080 — the tier and size every
    pre-Phase-14.6 caller used, including Phase 14.4's text-card backgrounds),
    this reproduces the exact pre-Phase-14.6 material — ``prompt`` alone, or
    ``f"{cache_key_extra}\\n{prompt}"`` when a namespace is given — so every
    existing cached image (parent beats, child beats, and Phase 14.4
    text-card backgrounds) keeps being reused with zero cache invalidation.

    A ``size=`` prefix is added whenever the requested size differs from that
    default (Phase-roadmap-3.1: portrait 1080x1920 Short generation) or the
    model tier is not Schnell — both cases have no pre-existing cache to
    preserve and would otherwise collide with the landscape Schnell cache
    entry for the identical prompt text.
    """
    if model_key == "schnell" and (width, height) == _DEFAULT_SCHNELL_SIZE:
        return f"{cache_key_extra}\n{prompt}" if cache_key_extra else prompt

    parts = [f"model={model_key}", f"size={width}x{height}"]
    if cache_key_extra:
        parts.append(cache_key_extra)
    return "\n".join(parts) + "\n" + prompt


def _call_fal(
    prompt: str,
    cache_dir: Path,
    media_path: Path,
    cache_key_extra: str = "",
    model_key: ModelKey = DEFAULT_MODEL_KEY,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
) -> str | None:
    """Call one fal.ai Flux endpoint with a single prompt and save the result.

    The endpoint and request payload are selected from
    ``MODEL_CAPABILITIES[model_key]`` and built by ``build_fal_payload()`` —
    this is the only place in the whole codebase that talks to `fal_client`
    for image generation; the payload itself is model-aware so Pro-family/
    Flux-2-Pro requests never receive a Schnell-shaped field they do not
    support.

    ``width``/``height`` default to the repo's pre-existing landscape values
    so every caller that does not pass them gets identical behavior and an
    identical cache key to before (see ``build_cache_key_material()``).
    Passing a non-default size (e.g. 1080x1920 for a child Short beat, or
    1280x720 for an Agent 6 thumbnail) both requests those pixels from
    fal.ai and produces a distinct cache key so it can never collide with a
    landscape cache entry for the same prompt text.

    Returns local path relative to media_path on success, None on any API/network error.
    Does NOT retry — callers implement the cascade retry strategy.
    """
    caps = MODEL_CAPABILITIES[model_key]
    cache_material = build_cache_key_material(
        model_key, prompt,
        width=width, height=height, cache_key_extra=cache_key_extra,
    )
    prompt_hash = hashlib.sha256(cache_material.encode()).hexdigest()[:24]
    local_path  = cache_dir / f"{prompt_hash}.jpg"

    if local_path.exists():
        return str(local_path.relative_to(media_path))

    payload = build_fal_payload(
        model_key, prompt, width=width, height=height,
    )
    logger.debug(
        "FAL_IMAGE_REQUEST_PREPARED model=%s endpoint=%s payload_keys=%s",
        model_key, caps["endpoint"], sorted(payload.keys()),
    )

    client = fal_client.SyncClient(key=settings.fal_key)
    try:
        result = client.run(
            caps["endpoint"],
            arguments=payload,
            timeout=GENERATION_TIMEOUT_SEC,
        )
        images = result.get("images") or []
        if not images or not images[0].get("url"):
            return None
        img_resp = httpx.get(images[0]["url"], timeout=DOWNLOAD_TIMEOUT_SEC, follow_redirects=True)
        img_resp.raise_for_status()
        local_path.write_bytes(img_resp.content)
        local_path_str = str(local_path.relative_to(media_path))
        logger.info(
            "FAL_IMAGE_GENERATED model=%s endpoint=%s media_url=%s",
            model_key, caps["endpoint"], local_path_str,
        )
        return local_path_str
    except (fal_client.FalClientError, httpx.HTTPStatusError, Exception) as exc:
        logger.error(
            "FAL_IMAGE_FAILED model=%s endpoint=%s exception_type=%s exception_message=%s",
            model_key, caps["endpoint"], type(exc).__name__, str(exc),
        )
        return None


def generate_beat_image(
    flux_prompt: str,
    beat_index: int,
    content_id: str,
    environment: str = "other",
    cache_key_extra: str = "",
    model_key: ModelKey = DEFAULT_MODEL_KEY,
    width: int = DEFAULT_WIDTH,
    height: int = DEFAULT_HEIGHT,
) -> str | None:
    """Generate a Flux image for one storyboard beat (or any other caller —
    the name is kept for backward compatibility with every existing Agent 4
    call site, but nothing in this function is beat-specific: it takes a
    prompt string and has no beat-object coupling) via a 3-tier cascade.

    Cascade strategy (None is returned only on hard API/auth failure, never on
    prompt issues):
      Attempt 1 — full Claude-written flux_prompt.
      Attempt 2 — first 40 words of flux_prompt (simplified; avoids edge cases).
      Attempt 3 — hardcoded safe fallback from ENV_SAFE_PROMPTS[environment].
    ``None`` is returned only when FAL_KEY is missing or all three prompts fail
    with a hard network/auth error — callers then run their own fallback
    (e.g. Agent 4's neighbour-reuse, or Agent 6's non-fatal degrade), never a
    text card.

    Args:
        flux_prompt:  Rich image generation prompt (written by Claude).
        beat_index:   Index for logging only (0 for a non-beat caller like a
                       single thumbnail).
        content_id:   Content UUID string for logging only.
        environment:  Selects the safe fallback prompt.
        cache_key_extra: Optional cache namespace; used so text-card backgrounds
                       (or an Agent 6 thumbnail) do not reuse a prior beat image.
        model_key:    Which fal.ai Flux model/endpoint to use for every tier of this
                       cascade. Defaults to ``"schnell"`` — every pre-Phase-14.6 caller
                       (including text-card backgrounds, which never pass this argument)
                       gets exactly the prior behavior unchanged.
        width, height: Requested pixel dimensions for every cascade tier. Default to
                       the repo's landscape default (1920x1080) — every pre-existing
                       caller gets identical pixels and an identical cache key.
                       Child Short generation passes 1080x1920; Agent 6 thumbnail
                       generation passes 1280x720.

    Returns:
        Local path relative to ``media_path`` on success, ``None`` on hard failure.
    """
    if not settings.fal_key:
        logger.error("FAL_KEY not set — cannot generate Flux image for beat=%d", beat_index)
        return None

    cache_dir  = Path(settings.media_path) / "cache" / content_id
    cache_dir.mkdir(parents=True, exist_ok=True)
    media_path = Path(settings.media_path)

    # Build the 3-tier prompt cascade
    short_prompt = " ".join(flux_prompt.split()[:40]) if flux_prompt else ""
    safe_prompt  = ENV_SAFE_PROMPTS.get(environment, ENV_SAFE_PROMPTS["other"])

    cascade = [
        ("full",      flux_prompt   if flux_prompt else safe_prompt),
        ("shortened", short_prompt  if short_prompt else safe_prompt),
        ("safe",      safe_prompt),
    ]

    endpoint = MODEL_CAPABILITIES[model_key]["endpoint"]
    for tier, prompt in cascade:
        if not prompt:
            continue
        logger.debug(
            "Flux beat=%d content=%s endpoint=%s model=%s tier=%s prompt_words=%d "
            "size=%dx%d",
            beat_index, content_id, endpoint, model_key, tier, len(prompt.split()),
            width, height,
        )
        path = _call_fal(
            prompt, cache_dir, media_path, cache_key_extra=cache_key_extra,
            model_key=model_key, width=width, height=height,
        )
        if path:
            logger.debug("Flux beat=%d tier=%s: saved %s", beat_index, tier, path)
            return path
        logger.warning(
            "Flux beat=%d tier=%s failed — trying next tier", beat_index, tier,
        )
        time.sleep(RETRY_DELAY_SEC)

    logger.error(
        "Flux beat=%d content=%s: all 3 tiers failed (hard API/network error)",
        beat_index, content_id,
    )
    return None
