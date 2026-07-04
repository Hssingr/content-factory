"""Agent 4 provider wrapper for fal.ai Flux image generation.

This is the only allowed direct `fal_client` integration point; other modules must
call this wrapper instead of instantiating fal.ai clients directly.

Flux Schnell image generator — one image per storyboard beat via fal.ai.

Each beat's ``flux_prompt`` (written by Claude in the storyboard pass) is sent to
``fal-ai/flux-1/schnell`` on fal.ai. The response contains a CDN image URL which is
downloaded and saved as a JPEG under ``{media_path}/cache/{content_id}/`` using a
SHA-256(prompt)[:24] filename so identical prompts within the same content item reuse
cached images without re-calling fal.ai.

On total failure (cascade + one safe retry with a fresh cache key), the beat's
``media_url`` is left empty and ``fill_failed_beats_from_neighbors()`` reuses the
nearest neighbouring beat's image — subtitles-only rendering (audit G-0): no text
card, no on-screen text of any kind; a repeated neighbouring image is strictly
better. A content item where NO beat generated at all keeps empty media_urls and
is stopped by Agent 5's missing-media blocker (fail loud, never render black).
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

_DEFAULT_MODEL_KEY: image_router.ModelKey = "schnell"
_DEFAULT_WIDTH = 1920
_DEFAULT_HEIGHT = 1080
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

# Concrete per-environment scene vocabulary — used by the text-prop prompt
# sanitizer below to anchor a sanitized prompt in a physical setting.
_ENVIRONMENT_SCENE_HINTS: dict[str, str] = {
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
_CONTEXT_WORD_LIMIT = 24
_CONTEXT_WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9'’-]*")


def _compact_context(raw: str) -> str:
    words = _CONTEXT_WORD_RE.findall(str(raw or "").replace('"', " "))
    return " ".join(words[:_CONTEXT_WORD_LIMIT]).strip()


# ── Text-prop detection and sanitization (Phase 14.7, prompt half) ──────────
# Ordinary generated beats (visual_type e.g. "document", "screenshot",
# "b-roll") whose subject is a real-world prop that would naturally carry
# readable text: a document, a missing-person poster, a calendar, a sign, a
# name tag. Image models render such text as illegible gibberish, so Python
# sanitizes the prompt to request a blank/non-legible prop. Under
# subtitles-only rendering (audit G-0) the overlay-derivation half of Phase
# 14.7 is removed — no readable text is ever drawn by Remotion except the
# subtitle track, so the sanitized image simply carries no legible text and
# the narration itself conveys what the prop said.
_TEXT_PROP_KEYWORDS: tuple[str, ...] = (
    "missing person poster", "missing poster", "wanted poster", "poster",
    "case file", "document", "report", "file folder", "calendar",
    "street sign", "sign", "label", "handwritten note", "note", "diary",
    "letter", "newspaper", "article", "phone screen", "text message",
    "phone message", "name tag", "identification card", "id card",
    "license", "headline",
)
_TEXT_PROP_FIELDS: tuple[str, ...] = ("flux_prompt", "visual_intent", "motif")

# Word-boundary patterns for the keywords above. Plain substring matching had
# a real false positive: "document" matched inside "documentary photograph" —
# a phrase the storyboard prompt itself recommends — so perfectly valid
# prompts were silently rewritten by the text-prop sanitizer (and kept beats
# were mutated when re-passed through beat building on a segment-level
# retry). \b-anchored search keeps "case file" and "missing person poster"
# matching while "documentary", "signature", "denoted", … no longer trigger.
_TEXT_PROP_KEYWORD_PATTERNS: tuple[tuple[str, "re.Pattern[str]"], ...] = tuple(
    (kw, re.compile(r"\b" + re.escape(kw) + r"\b")) for kw in _TEXT_PROP_KEYWORDS
)

_TEXT_PROP_NO_TEXT_CLAUSE = (
    "blank and unmarked, no readable text, no legible letters, no legible "
    "numbers, no legible words, no readable typography, no readable logos, "
    "no readable names, no readable dates, illegible or blank surface only"
)

# Positive framing techniques (audit G-3 / roadmap 2.9) — the same ANGLE /
# DISTANCE / LIGHTING / DETAIL taxonomy the storyboard prompt teaches Claude,
# enforced here deterministically as a belt-and-suspenders fix for any
# text-prop beat that reaches this sanitizer anyway (Claude's own framing
# choice was too direct, or the beat came from the child remap path, which
# has no equivalent storyboard-prompt guidance of its own). FLUX prompting
# guidance is explicit that positive description outperforms negative
# instruction — Flux has no true negative-prompt channel — so this clause is
# additive alongside (never a replacement for) _TEXT_PROP_NO_TEXT_CLAUSE.
_TEXT_PROP_FRAMING_CLAUSES: tuple[str, ...] = (
    "photographed from a low side-on angle, the text-bearing surface turned "
    "away from camera",
    "the text-bearing surface a small distant element within a wider shot, "
    "not the focal subject",
    "lit with raking side light across the surface, texture and glare "
    "obscuring the surface rather than clear reading light",
    "extreme close-up on physical texture and material detail only, the "
    "full surface not in frame",
)


def _select_text_prop_framing(beat: dict, prop_label: str) -> str:
    """Deterministically choose one framing technique for a text-prop beat.

    Never random: the same beat always selects the same clause (keyed off
    beat_order + prop_label + environment), so retries and segment-level
    regeneration reproduce byte-identical sanitized prompts.
    """
    key = f"{prop_label}:{beat.get('beat_order', 0)}:{beat.get('environment', '')}"
    index = int(hashlib.sha256(key.encode("utf-8")).hexdigest(), 16) % len(
        _TEXT_PROP_FRAMING_CLAUSES
    )
    return _TEXT_PROP_FRAMING_CLAUSES[index]

def is_text_prop_beat(beat: dict) -> bool:
    """True for a beat whose prop would naturally carry readable text —
    a document, poster, calendar, sign, name tag, etc. (word-boundary match;
    see _TEXT_PROP_KEYWORD_PATTERNS)."""
    haystack = " ".join(str(beat.get(f, "") or "") for f in _TEXT_PROP_FIELDS).lower()
    return any(pattern.search(haystack) for _, pattern in _TEXT_PROP_KEYWORD_PATTERNS)


def _detect_text_prop_label(beat: dict) -> str:
    """Return the first matching prop keyword — used only to keep the
    sanitized prompt's described object specific (a "calendar", not a vague
    "object"). Tuple order in `_TEXT_PROP_KEYWORDS` is most-specific-first
    so "missing person poster" is preferred over the generic "poster".
    """
    haystack = " ".join(str(beat.get(f, "") or "") for f in _TEXT_PROP_FIELDS).lower()
    for kw, pattern in _TEXT_PROP_KEYWORD_PATTERNS:
        if pattern.search(haystack):
            return kw
    return "document"


def derive_text_prop_prompt(beat: dict) -> str:
    """Build a sanitized Flux prompt for a text-prop beat.

    Describes the physical prop and scene only — never the literal text it
    would carry. Combines a positive ANGLE/DISTANCE/LIGHTING/DETAIL framing
    clause (audit G-3 / roadmap 2.9 — the same taxonomy the storyboard
    prompt teaches Claude, selected deterministically per beat) with the
    existing explicit "no readable text" clause — belt-and-suspenders, not
    either/or. Under subtitles-only rendering no overlay replaces that text:
    the narration itself already conveys what the prop said.
    """
    environment = str(beat.get("environment") or "other")
    env_scene = _ENVIRONMENT_SCENE_HINTS.get(environment, _ENVIRONMENT_SCENE_HINTS["other"])
    context = (
        _compact_context(beat.get("visual_intent", ""))
        or _compact_context(beat.get("flux_prompt", ""))
        or _compact_context(beat.get("script_text", ""))
    )
    prop_label = _detect_text_prop_label(beat)
    subject = f"{context}, a physical {prop_label} prop" if context else f"a physical {prop_label} prop"
    framing = _select_text_prop_framing(beat, prop_label)
    return (
        f"{subject} in the scene, {env_scene}, {framing}, photorealistic, "
        "natural practical lighting, textured surfaces, sharp focus, no people, "
        f"{_TEXT_PROP_NO_TEXT_CLAUSE}"
    )


def _call_fal(
    prompt: str,
    cache_dir: Path,
    media_path: Path,
    cache_key_extra: str = "",
    model_key: image_router.ModelKey = _DEFAULT_MODEL_KEY,
    width: int = _DEFAULT_WIDTH,
    height: int = _DEFAULT_HEIGHT,
) -> str | None:
    """Call one fal.ai Flux endpoint with a single prompt and save the result.

    The endpoint and request payload are selected from
    ``image_router.MODEL_CAPABILITIES[model_key]`` and built by
    ``image_router.build_fal_payload()`` — this is the only place that talks
    to `fal_client` for image generation; the payload itself is model-aware
    so Pro-family/Flux-2-Pro requests never receive a Schnell-shaped field
    they do not support.

    ``width``/``height`` default to the repo's pre-existing landscape values
    so every caller that does not pass them gets identical behavior and an
    identical cache key to before (see
    ``image_router.build_cache_key_material()``). Passing a non-default size
    (e.g. 1080x1920 for a child Short beat) both requests portrait pixels
    from fal.ai and produces a distinct cache key so it can never collide
    with a landscape cache entry for the same prompt text.

    Returns local path relative to media_path on success, None on any API/network error.
    Does NOT retry — callers implement the cascade retry strategy.
    """
    caps = image_router.MODEL_CAPABILITIES[model_key]
    cache_material = image_router.build_cache_key_material(
        model_key, prompt,
        width=width, height=height, cache_key_extra=cache_key_extra,
    )
    prompt_hash = hashlib.sha256(cache_material.encode()).hexdigest()[:24]
    local_path  = cache_dir / f"{prompt_hash}.jpg"

    if local_path.exists():
        return str(local_path.relative_to(media_path))

    payload = image_router.build_fal_payload(
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
    purpose: str = "beat_image",
    cache_key_extra: str = "",
    model_key: image_router.ModelKey = _DEFAULT_MODEL_KEY,
    width: int = _DEFAULT_WIDTH,
    height: int = _DEFAULT_HEIGHT,
) -> str | None:
    """Generate a Flux image for one storyboard beat via a 3-tier cascade.

    Cascade strategy (None is returned only on hard API/auth failure, never on
    prompt issues):
      Attempt 1 — full Claude-written flux_prompt.
      Attempt 2 — first 40 words of flux_prompt (simplified; avoids edge cases).
      Attempt 3 — hardcoded safe fallback from _ENV_SAFE_PROMPTS[environment].
    ``None`` is returned only when FAL_KEY is missing or all three prompts fail
    with a hard network/auth error — callers then run the neighbour-reuse
    fallback (``fill_failed_beats_from_neighbors``), never a text card.

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
        width, height: Requested pixel dimensions for every cascade tier. Default to
                       the repo's landscape default (1920x1080) — every pre-existing
                       caller gets identical pixels and an identical cache key.
                       Child Short generation passes 1080x1920 (roadmap 3.1).

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
            "Flux beat=%d content=%s purpose=%s endpoint=%s model=%s tier=%s prompt_words=%d "
            "size=%dx%d",
            beat_index, content_id, purpose, endpoint, model_key, tier, len(prompt.split()),
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
        time.sleep(_RETRY_DELAY_SEC)

    logger.error(
        "Flux beat=%d content=%s: all 3 tiers failed (hard API/network error)",
        beat_index, content_id,
    )
    return None


def generate_beat_image_with_routing(
    beat: dict, content_id: str, tier_counts: dict[str, int],
    width: int = _DEFAULT_WIDTH, height: int = _DEFAULT_HEIGHT,
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
    caller did before this phase. A beat whose ``media_url`` is already a
    local ``cache/`` path short-circuits (``route.source == "reuse"``):
    the existing path is returned untouched with no generation call and no
    tier-count increment.

    ``width``/``height`` default to the repo's landscape default so the
    parent path is unaffected; the child Short path (roadmap 3.1) passes
    1080x1920.
    """
    route = image_router.select_route(
        beat, content_id,
        purpose="beat_image",
        routing_enabled=settings.image_routing_enabled,
        allow_dev=settings.image_routing_allow_dev,
        allow_pro=settings.image_routing_allow_pro,
        max_pro_per_content=settings.image_routing_max_pro_per_content,
        # Pro budget counts every pro-family selection (robust to a future
        # tier being made selectable), not just the pro_1_1 key.
        pro_used_so_far=sum(
            count for key, count in tier_counts.items() if key.startswith("pro")
        ),
    )

    # Honor the router's reuse decision: a beat whose media_url is already a
    # local cache/ path is returned as-is — no generation call, no tier count.
    # This enforces the documented invariant that already-resolved media is
    # never touched, and keeps IMAGE_ROUTE_TIER_COUNTS an accurate count of
    # actual generations (cache-holding beats previously inflated "schnell").
    # Guarded by a disk check: if the referenced file is gone (operator wiped
    # cache/), fall through to generation so a re-run self-heals instead of
    # shipping a path the media validator would then block forever.
    if route.source == "reuse":
        existing_url = beat.get("media_url") or ""
        if existing_url and (Path(settings.media_path) / existing_url).is_file():
            return existing_url
        logger.warning(
            "IMAGE_ROUTE_REUSE_MISSING_ON_DISK content=%s beat=%s media_url=%s "
            "— cached file is gone, regenerating instead of reusing",
            content_id, beat.get("beat_order", beat.get("section_order", 0)), existing_url,
        )

    model_key = route.model_key or _DEFAULT_MODEL_KEY
    # Counted at selection time, deliberately: a hard-failed Pro generation
    # still consumes Pro budget for this content run, so a flaky endpoint can
    # never turn the per-content cap into an unbounded retry spend.
    tier_counts[model_key] = tier_counts.get(model_key, 0) + 1

    idx         = beat.get("beat_order", beat.get("section_order", 0))
    prompt      = beat.get("flux_prompt", "")
    environment = beat.get("environment", "other")
    return generate_beat_image(
        prompt, idx, content_id, environment=environment, model_key=model_key,
        width=width, height=height,
    )


def fill_failed_beats_from_neighbors(beats: list[dict], content_id: str) -> int:
    """Fill beats without a local image by reusing the nearest neighbour's image.

    Subtitles-only rendering (audit G-0/G-8): the ``__text_card__`` sentinel
    and the text-card fallback are removed — a beat whose generation hard-failed
    reuses the closest beat's already-generated image instead (earlier beat
    preferred on distance ties, matching how a held frame naturally extends).
    A legacy sentinel value is treated as "no image" and filled the same way.

    If NO beat in the list has a local image, nothing can be filled: media_urls
    stay empty and the content is stopped downstream by Agent 5's
    missing-media blocker (fail loud, never render black or text-only frames).

    Returns:
        Number of beats filled from a neighbour.
    """
    have = [
        i for i, b in enumerate(beats)
        if (b.get("media_url") or "").startswith("cache/")
    ]
    missing = [
        i for i, b in enumerate(beats)
        if not (b.get("media_url") or "").startswith("cache/")
    ]
    if not missing:
        return 0
    if not have:
        logger.error(
            "BEAT_IMAGE_NEIGHBOR_REUSE_IMPOSSIBLE content=%s missing=%d/%d "
            "— no beat has a local image to reuse; Agent 5's missing-media "
            "blocker owns this failure",
            content_id, len(missing), len(beats),
        )
        return 0

    filled = 0
    for i in missing:
        source = min(have, key=lambda j: (abs(j - i), j > i))
        beats[i]["media_url"] = beats[source]["media_url"]
        beats[i]["media_type"] = beats[source].get("media_type", "image")
        filled += 1
        logger.warning(
            "BEAT_IMAGE_NEIGHBOR_REUSED content=%s beat=%s source_beat=%s media_url=%s",
            content_id,
            beats[i].get("beat_order", i),
            beats[source].get("beat_order", source),
            beats[i]["media_url"],
        )
    return filled


def generate_all_beat_images(beats: list[dict], content_id: str) -> list[dict]:
    """Generate Flux images for all beats sequentially (1 worker, 0.5s inter-beat sleep).

    Mutates each beat in-place:
      - Success: sets ``beat["media_url"]`` to a local cache path, ``beat["media_type"] = "image"``
      - Hard failure: one extra environment-safe retry with a fresh cache key,
        then ``fill_failed_beats_from_neighbors()`` reuses the nearest
        neighbouring beat's image. Never a text card, never on-screen text
        (subtitles-only rendering, audit G-0/G-8).

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
        idx = beat.get("beat_order", beat.get("section_order", 0))
        path = generate_beat_image_with_routing(beat, content_id, tier_counts)
        if not path:
            # Safe-prompt retry with a fresh cache key: an empty flux_prompt
            # makes every cascade tier resolve to the environment-safe prompt,
            # and the unique cache_key_extra forces a new fal call instead of
            # re-reading a possibly-corrupt cached artifact.
            logger.warning(
                "BEAT_IMAGE_HARD_RETRY content=%s beat=%s — retrying with "
                "environment-safe prompt and fresh cache key",
                content_id, idx,
            )
            path = generate_beat_image(
                "", idx, content_id,
                environment=beat.get("environment", "other"),
                cache_key_extra=f"hard_retry:{idx}",
            )
        if path:
            beat["media_url"]  = path
            beat["media_type"] = "image"

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
                    "Flux beat=%s unexpected error: %s — neighbour reuse will fill this beat",
                    beat.get("beat_order", "?"), exc,
                )

    neighbor_reused = fill_failed_beats_from_neighbors(beats, content_id)

    succeeded = sum(
        1 for b in beats
        if (b.get("media_url") or "").startswith("cache/")
    )
    still_missing = len(beats) - succeeded
    logger.warning(
        "Flux generation complete: content=%s beats=%d succeeded=%d "
        "neighbor_reused=%d still_missing=%d",
        content_id, len(beats), succeeded, neighbor_reused, still_missing,
    )
    if tier_counts:
        logger.info(
            "IMAGE_ROUTE_TIER_COUNTS content=%s tier_counts=%s", content_id, tier_counts,
        )
    return beats
