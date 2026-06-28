"""Agent 4 provider wrapper for fal.ai Flux image generation.

This is the only allowed direct `fal_client` integration point; other modules must
call this wrapper instead of instantiating fal.ai clients directly.

Flux Schnell image generator — one image per storyboard beat via fal.ai.

Each beat's ``flux_prompt`` (written by Claude in the storyboard pass) is sent to
``fal-ai/flux-1/schnell`` on fal.ai. The response contains a CDN image URL which is
downloaded and saved as a JPEG under ``{media_path}/cache/{content_id}/`` using a
SHA-256(prompt)[:24] filename so identical prompts within the same content item reuse
cached images without re-calling fal.ai.

On total failure (3 retries exhausted), the beat is marked with
``visual_type = "text_card"`` and ``media_url = "__text_card__"`` so Remotion
falls back to the TextCard.tsx composition instead of a broken black frame.
"""

import hashlib
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import fal_client
import httpx

from app.config import settings
from app.agents.agent4_visuals.services import image_router

logger = logging.getLogger(__name__)

# Kept as a module constant for logging/back-compat call sites — always equal
# to image_router.MODEL_CAPABILITIES["schnell"]["endpoint"] (see that table's
# comment for why this literal string is preserved rather than updated to
# fal.ai's current documented name).
_FAL_ENDPOINT = image_router.MODEL_CAPABILITIES["schnell"]["endpoint"]
_DEFAULT_MODEL_KEY: image_router.ModelKey = "schnell"
_TEXT_CARD_BACKGROUND_PURPOSE = "text_card_background"
_DEFAULT_WIDTH = 1920
_DEFAULT_HEIGHT = 1080
_MAX_RETRIES = 3
_RETRY_DELAY_SEC = 2.0
_INTER_BEAT_SLEEP_SEC = 0.5  # conservative until rate limits confirmed
_GENERATION_TIMEOUT_SEC = 60.0
_DOWNLOAD_TIMEOUT_SEC = 20.0

# ── Safe fallback prompts by environment ───────────────────────────────────────
# Used as attempt 3 when the Claude-written flux_prompt and its shortened form both
# fail. These are guaranteed-safe (no content-policy edge cases, no mood words, pure
# physical description) so they should always succeed.
_ENV_SAFE_PROMPTS: dict[str, str] = {
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

_TEXT_CARD_ENVIRONMENT_SCENES: dict[str, str] = {
    "underwater": "clear underwater pool tiles and rippling light patterns",
    "indoor_office": "office desk surface with lamp glow, folders, and practical workspace details",
    "indoor_domestic": "quiet home interior with furniture, shelves, and warm window light",
    "forest_nature": "forest path with leaves, tree trunks, and natural daylight",
    "urban_street": "city sidewalk, parked cars, storefront shapes, and overcast daylight",
    "corridor_interior": "long interior corridor with doors, floor reflections, and overhead lights",
    "abstract_dark": "close textured wall surface with geometric shadows and visible material grain",
    "open_landscape": "open field horizon with grass, sky, and distant landscape detail",
    "laboratory": "laboratory bench with glassware, instruments, and clean overhead light",
    "industrial": "warehouse interior with concrete floor, metal beams, and skylight panels",
    "vehicle": "vehicle interior with dashboard, windshield, and road shapes outside",
    "other": "real-world tabletop scene with story-relevant objects and natural side light",
}
_TEXT_CARD_NO_TEXT_CLAUSE = (
    "no readable text, no letters, no numbers, no signs, no logos, "
    "no captions, no typography"
)
_TEXT_CARD_CONTEXT_WORD_LIMIT = 24
_TEXT_CARD_WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9'’-]*")


def is_text_card_beat(beat: dict) -> bool:
    """Return True for deliberate Remotion text-card beats."""
    return (
        beat.get("media_strategy") == "remotion_text_card"
        or beat.get("visual_type") == "text_card"
    )


def _compact_text_card_context(raw: str) -> str:
    words = _TEXT_CARD_WORD_RE.findall(str(raw or "").replace('"', " "))
    return " ".join(words[:_TEXT_CARD_CONTEXT_WORD_LIMIT]).strip()


def derive_text_card_background_prompt(beat: dict) -> str:
    """Build a concrete Flux background prompt for Remotion-rendered text cards.

    The prompt describes only the physical background scene. Readable text stays
    in Remotion, so overlay_text is intentionally not copied into the image
    prompt unless no narration/visual-intent context exists at all.
    """
    environment = str(beat.get("environment") or "other")
    env_scene = _TEXT_CARD_ENVIRONMENT_SCENES.get(environment, _TEXT_CARD_ENVIRONMENT_SCENES["other"])
    context = (
        _compact_text_card_context(beat.get("visual_intent", ""))
        or _compact_text_card_context(beat.get("script_text", ""))
        or _compact_text_card_context(beat.get("overlay_text", ""))
        or env_scene
    )
    return (
        f"{context}, represented as a real physical background scene, {env_scene}, "
        "medium wide documentary photograph, natural practical lighting, textured surfaces, "
        "story-relevant objects in frame, photorealistic, sharp focus, no people, "
        f"{_TEXT_CARD_NO_TEXT_CLAUSE}"
    )


def generate_text_card_background_image(beat: dict, content_id: str) -> dict:
    """Generate a Flux Schnell background for a Remotion-rendered text card."""
    idx = beat.get("beat_order", beat.get("section_order", 0))
    environment = beat.get("environment", "other")
    prompt = derive_text_card_background_prompt(beat)
    beat["flux_prompt"] = prompt
    beat["visual_type"] = "text_card"
    beat["media_strategy"] = "remotion_text_card"
    beat["media_type"] = "image"
    logger.info(
        "TEXT_CARD_BACKGROUND_PROMPT_DERIVED content=%s beat=%s prompt_words=%d prompt=%r",
        content_id, idx, len(prompt.split()), prompt[:240],
    )

    path = generate_beat_image(
        prompt,
        idx,
        content_id,
        environment=environment,
        purpose=_TEXT_CARD_BACKGROUND_PURPOSE,
        cache_key_extra=f"{_TEXT_CARD_BACKGROUND_PURPOSE}:{idx}",
    )
    if path:
        beat["media_url"] = path
        logger.info(
            "TEXT_CARD_BACKGROUND_IMAGE_GENERATED content=%s beat=%s endpoint=%s media_url=%s",
            content_id, idx, _FAL_ENDPOINT, path,
        )
    else:
        beat["media_url"] = "__text_card__"
        logger.warning(
            "TEXT_CARD_BACKGROUND_FALLBACK content=%s beat=%s reason=flux_generation_failed",
            content_id, idx,
        )
    return beat


# ── Text-prop detection and sanitization (Phase 14.7) ───────────────────────
# Distinct from text-card beats above (Phase 14.4, `media_strategy ==
# "remotion_text_card"`) — these are ORDINARY generated beats (visual_type
# stays e.g. "document", "screenshot", "b-roll") whose subject is a
# real-world prop that would naturally carry readable text: a document, a
# missing-person poster, a calendar, a sign, a name tag. Image models render
# such text as illegible gibberish (the original real-world defect this
# phase fixes: a missing-person poster with corrupted name/body text), so
# Python sanitizes the prompt to request a blank/non-legible prop and
# Remotion renders any needed readable text as an overlay instead — the same
# generated-background-plus-overlay pattern Phase 14.4 established for text
# cards, generalized to this broader prop category. The two mechanisms never
# overlap: `is_text_prop_beat()` always returns False for a beat
# `is_text_card_beat()` already claims.
_TEXT_PROP_KEYWORDS: tuple[str, ...] = (
    "missing person poster", "missing poster", "wanted poster", "poster",
    "case file", "document", "report", "file folder", "calendar",
    "street sign", "sign", "label", "handwritten note", "note", "diary",
    "letter", "newspaper", "article", "phone screen", "text message",
    "phone message", "name tag", "identification card", "id card",
    "license", "headline",
)
_TEXT_PROP_FIELDS: tuple[str, ...] = ("flux_prompt", "visual_intent", "motif")

_TEXT_PROP_NO_TEXT_CLAUSE = (
    "blank and unmarked, no readable text, no legible letters, no legible "
    "numbers, no legible words, no readable typography, no readable logos, "
    "no readable names, no readable dates, illegible or blank surface only"
)

_MISSING_POSTER_KEYWORDS: tuple[str, ...] = (
    "missing person poster", "missing poster", "wanted poster",
)
_DATE_OVERLAY_RE = re.compile(
    r"\b(?:January|February|March|April|May|June|July|August|September|"
    r"October|November|December)\s+\d{1,2}(?:st|nd|rd|th)?\b"
    r"|\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b",
    re.IGNORECASE,
)


def is_text_prop_beat(beat: dict) -> bool:
    """True for an ordinary (non-text-card) beat whose prop would naturally
    carry readable text — a document, poster, calendar, sign, name tag, etc.

    Always False for a beat `is_text_card_beat()` already claims — the two
    mechanisms are mutually exclusive, never layered.
    """
    if is_text_card_beat(beat):
        return False
    haystack = " ".join(str(beat.get(f, "") or "") for f in _TEXT_PROP_FIELDS).lower()
    return any(kw in haystack for kw in _TEXT_PROP_KEYWORDS)


def _detect_text_prop_label(beat: dict) -> str:
    """Return the first matching prop keyword — used only to keep the
    sanitized prompt's described object specific (a "calendar", not a vague
    "object"). Tuple order in `_TEXT_PROP_KEYWORDS` is most-specific-first
    so "missing person poster" is preferred over the generic "poster".
    """
    haystack = " ".join(str(beat.get(f, "") or "") for f in _TEXT_PROP_FIELDS).lower()
    for kw in _TEXT_PROP_KEYWORDS:
        if kw in haystack:
            return kw
    return "document"


def derive_text_prop_prompt(beat: dict) -> str:
    """Build a sanitized Flux prompt for a text-prop beat.

    Describes the physical prop and scene only — never the literal text it
    would carry — and explicitly forbids any readable text, letters,
    numbers, names, or dates in the generated image. Readable content for
    this prop, if any, is rendered by Remotion instead
    (`derive_text_prop_overlay()`), not by the image model.
    """
    environment = str(beat.get("environment") or "other")
    env_scene = _TEXT_CARD_ENVIRONMENT_SCENES.get(environment, _TEXT_CARD_ENVIRONMENT_SCENES["other"])
    context = (
        _compact_text_card_context(beat.get("visual_intent", ""))
        or _compact_text_card_context(beat.get("flux_prompt", ""))
        or _compact_text_card_context(beat.get("script_text", ""))
    )
    prop_label = _detect_text_prop_label(beat)
    subject = f"{context}, a physical {prop_label} prop" if context else f"a physical {prop_label} prop"
    return (
        f"{subject} in the scene, {env_scene}, medium shot, photorealistic, "
        "natural practical lighting, textured surfaces, sharp focus, no people, "
        f"{_TEXT_PROP_NO_TEXT_CLAUSE}"
    )


def derive_text_prop_overlay(beat: dict) -> str:
    """Derive minimal, non-invented readable text for a text-prop beat's
    Remotion overlay.

    Returns "" when no safe, derivable text exists — never fabricates
    detailed body copy (names, addresses, full headlines). Only two
    generation rules exist, deliberately narrow:
      1. A missing-person/wanted poster beat gets the generic label
         "MISSING" — never an invented name.
      2. A calendar/date beat gets an exact date substring lifted verbatim
         from the beat's own script_text, if one is actually present — never
         an invented date.
    Every other text-prop beat (document, sign, name tag, ...) gets no
    overlay at all unless the beat already carries explicit `overlay_text`.
    """
    existing = str(beat.get("overlay_text", "") or "").strip()
    if existing:
        return existing

    haystack = " ".join(
        str(beat.get(f, "") or "") for f in ("visual_intent", "flux_prompt", "script_text")
    ).lower()

    if any(kw in haystack for kw in _MISSING_POSTER_KEYWORDS):
        return "MISSING"

    if "calendar" in haystack or "date" in haystack:
        match = _DATE_OVERLAY_RE.search(str(beat.get("script_text", "") or ""))
        if match:
            return match.group(0).upper()

    return ""


def _call_fal(
    prompt: str,
    cache_dir: Path,
    media_path: Path,
    cache_key_extra: str = "",
    model_key: image_router.ModelKey = _DEFAULT_MODEL_KEY,
) -> str | None:
    """Call one fal.ai Flux endpoint with a single prompt and save the result.

    The endpoint and request payload are selected from
    ``image_router.MODEL_CAPABILITIES[model_key]`` and built by
    ``image_router.build_fal_payload()`` — this is the only place that talks
    to `fal_client` for image generation; the payload itself is model-aware
    so Pro-family/Flux-2-Pro requests never receive a Schnell-shaped field
    they do not support.

    Returns local path relative to media_path on success, None on any API/network error.
    Does NOT retry — callers implement the cascade retry strategy.
    """
    caps = image_router.MODEL_CAPABILITIES[model_key]
    cache_material = image_router.build_cache_key_material(
        model_key, prompt,
        width=_DEFAULT_WIDTH, height=_DEFAULT_HEIGHT, cache_key_extra=cache_key_extra,
    )
    prompt_hash = hashlib.sha256(cache_material.encode()).hexdigest()[:24]
    local_path  = cache_dir / f"{prompt_hash}.jpg"

    if local_path.exists():
        return str(local_path.relative_to(media_path))

    payload = image_router.build_fal_payload(
        model_key, prompt, width=_DEFAULT_WIDTH, height=_DEFAULT_HEIGHT,
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
            timeout=_GENERATION_TIMEOUT_SEC,
        )
        images = result.get("images") or []
        if not images or not images[0].get("url"):
            return None
        img_resp = httpx.get(images[0]["url"], timeout=_DOWNLOAD_TIMEOUT_SEC, follow_redirects=True)
        img_resp.raise_for_status()
        local_path.write_bytes(img_resp.content)
        local_path_str = str(local_path.relative_to(media_path))
        logger.info(
            "FAL_IMAGE_GENERATED model=%s endpoint=%s media_url=%s",
            model_key, caps["endpoint"], local_path_str,
        )
        return local_path_str
    except (fal_client.FalClientError, httpx.HTTPStatusError, Exception):
        return None


def generate_beat_image(
    flux_prompt: str,
    beat_index: int,
    content_id: str,
    environment: str = "other",
    purpose: str = "beat_image",
    cache_key_extra: str = "",
    model_key: image_router.ModelKey = _DEFAULT_MODEL_KEY,
) -> str | None:
    """Generate a Flux image for one storyboard beat via a 3-tier cascade.

    Cascade strategy (text_card fires only on hard API/auth failure, never on prompt issues):
      Attempt 1 — full Claude-written flux_prompt.
      Attempt 2 — first 40 words of flux_prompt (simplified; avoids edge cases).
      Attempt 3 — hardcoded safe fallback from _ENV_SAFE_PROMPTS[environment].
    text_card is returned only when FAL_KEY is missing or all three prompts fail
    with a hard network/auth error (not a content or prompt issue).

    Args:
        flux_prompt:  Rich cinematic image generation prompt (written by Claude).
        beat_index:   Beat index for logging only.
        content_id:   Content UUID string for logging only.
        environment:  Beat's environment field — selects the safe fallback prompt.
        purpose:      Log label for the generation purpose; does not itself change
                       which model is used — the caller (image_router.select_route())
                       decides ``model_key`` before calling this function.
        cache_key_extra: Optional cache namespace; used so text-card backgrounds do not reuse a prior beat image.
        model_key:    Which fal.ai Flux model/endpoint to use for every tier of this
                       cascade. Defaults to ``"schnell"`` — every pre-Phase-14.6 caller
                       (including text-card backgrounds, which never pass this argument)
                       gets exactly the prior behavior unchanged.

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
    safe_prompt  = _ENV_SAFE_PROMPTS.get(environment, _ENV_SAFE_PROMPTS["other"])

    cascade = [
        ("full",      flux_prompt   if flux_prompt else safe_prompt),
        ("shortened", short_prompt  if short_prompt else safe_prompt),
        ("safe",      safe_prompt),
    ]

    endpoint = image_router.MODEL_CAPABILITIES[model_key]["endpoint"]
    for tier, prompt in cascade:
        if not prompt:
            continue
        logger.debug(
            "Flux beat=%d content=%s purpose=%s endpoint=%s model=%s tier=%s prompt_words=%d",
            beat_index, content_id, purpose, endpoint, model_key, tier, len(prompt.split()),
        )
        path = _call_fal(prompt, cache_dir, media_path, cache_key_extra=cache_key_extra, model_key=model_key)
        if path:
            logger.debug("Flux beat=%d tier=%s: saved %s", beat_index, tier, path)
            return path
        logger.warning(
            "Flux beat=%d tier=%s failed — trying next tier", beat_index, tier,
        )
        time.sleep(_RETRY_DELAY_SEC)

    logger.error(
        "Flux beat=%d content=%s: all 3 tiers failed (hard API/network error) — text_card",
        beat_index, content_id,
    )
    return None


def generate_beat_image_with_routing(
    beat: dict, content_id: str, tier_counts: dict[str, int],
) -> str | None:
    """Select a model tier for one ordinary (non-text-card) beat, then generate.

    Centralizes the Phase 14.6 routing decision so the parent path
    (`generate_all_beat_images()` below) and the child path
    (`generate_pending_beat_images()` in storyboard.py) make the exact same
    decision with the exact same per-content Pro-tier bookkeeping
    (``tier_counts``, shared across one content item's generation run —
    caller-owned, not persisted).

    With routing disabled (the default), this always resolves to ``"schnell"``
    — identical to calling ``generate_beat_image()`` directly, as every
    caller did before this phase.
    """
    route = image_router.select_route(
        beat, content_id,
        purpose="beat_image",
        routing_enabled=settings.image_routing_enabled,
        allow_dev=settings.image_routing_allow_dev,
        allow_pro=settings.image_routing_allow_pro,
        max_pro_per_content=settings.image_routing_max_pro_per_content,
        pro_used_so_far=tier_counts.get("pro_1_1", 0),
    )
    model_key = route.model_key or _DEFAULT_MODEL_KEY
    tier_counts[model_key] = tier_counts.get(model_key, 0) + 1

    idx         = beat.get("beat_order", beat.get("section_order", 0))
    prompt      = beat.get("flux_prompt", "")
    environment = beat.get("environment", "other")
    return generate_beat_image(prompt, idx, content_id, environment=environment, model_key=model_key)


def generate_all_beat_images(beats: list[dict], content_id: str) -> list[dict]:
    """Generate Flux images for all beats sequentially (1 worker, 0.5s inter-beat sleep).

    Mutates each beat in-place:
      - Success: sets ``beat["media_url"]`` to a local cache path, ``beat["media_type"] = "image"``
      - Failure: sets ``beat["visual_type"] = "text_card"``, ``beat["media_url"] = "__text_card__"``

    Args:
        beats:      Storyboard beat dicts with a ``flux_prompt`` field.
        content_id: Content UUID string for logging.

    Returns:
        The same list with each beat's ``media_url`` set.
    """
    if not beats:
        return beats

    logger.info(
        "Flux generation start: content=%s beats=%d workers=1",
        content_id, len(beats),
    )

    # Shared across all beats in this call only (caller-scoped, never
    # persisted) — bounds Pro-tier usage per content per CLAUDE.md's
    # routing-conservatism contract. Safe to share across the thread pool
    # below because max_workers=1 (beats are generated one at a time).
    tier_counts: dict[str, int] = {}

    def _generate_one(beat: dict) -> dict:
        if is_text_card_beat(beat):
            generate_text_card_background_image(beat, content_id)
            time.sleep(_INTER_BEAT_SLEEP_SEC)
            return beat

        path = generate_beat_image_with_routing(beat, content_id, tier_counts)
        if path:
            beat["media_url"]  = path
            beat["media_type"] = "image"
        else:
            beat["visual_type"] = "text_card"
            beat["media_url"]   = "__text_card__"

        time.sleep(_INTER_BEAT_SLEEP_SEC)
        return beat

    with ThreadPoolExecutor(max_workers=1) as pool:
        futures = {pool.submit(_generate_one, beat): beat for beat in beats}
        for future in as_completed(futures):
            try:
                future.result()
            except Exception as exc:
                beat = futures[future]
                logger.error(
                    "Flux beat=%d unexpected error: %s — text_card fallback",
                    beat.get("beat_order", "?"), exc,
                )
                beat["visual_type"] = "text_card"
                beat["media_url"]   = "__text_card__"

    succeeded = sum(
        1 for b in beats
        if (b.get("media_url") or "").startswith("cache/")
    )
    failed = len(beats) - succeeded
    logger.warning(
        "Flux generation complete: content=%s beats=%d succeeded=%d text_card_fallback=%d",
        content_id, len(beats), succeeded, failed,
    )
    if tier_counts:
        logger.info(
            "IMAGE_ROUTE_TIER_COUNTS content=%s tier_counts=%s", content_id, tier_counts,
        )
    return beats
