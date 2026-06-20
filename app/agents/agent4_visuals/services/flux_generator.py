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
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import fal_client
import httpx

from app.config import settings

logger = logging.getLogger(__name__)

_FAL_ENDPOINT = "fal-ai/flux-1/schnell"
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


def _call_fal(prompt: str, cache_dir: Path, media_path: Path) -> str | None:
    """Call fal.ai Flux Schnell with a single prompt and save the result.

    Returns local path relative to media_path on success, None on any API/network error.
    Does NOT retry — callers implement the cascade retry strategy.
    """
    prompt_hash = hashlib.sha256(prompt.encode()).hexdigest()[:24]
    local_path  = cache_dir / f"{prompt_hash}.jpg"

    if local_path.exists():
        return str(local_path.relative_to(media_path))

    client = fal_client.SyncClient(key=settings.fal_key)
    try:
        result = client.run(
            _FAL_ENDPOINT,
            arguments={
                "prompt":              prompt,
                "image_size":          {"width": 1920, "height": 1080},
                "num_inference_steps": 8,
                "num_images":          1,
                "output_format":       "jpeg",
            },
            timeout=_GENERATION_TIMEOUT_SEC,
        )
        images = result.get("images") or []
        if not images or not images[0].get("url"):
            return None
        img_resp = httpx.get(images[0]["url"], timeout=_DOWNLOAD_TIMEOUT_SEC, follow_redirects=True)
        img_resp.raise_for_status()
        local_path.write_bytes(img_resp.content)
        return str(local_path.relative_to(media_path))
    except (fal_client.FalClientError, httpx.HTTPStatusError, Exception):
        return None


def generate_beat_image(
    flux_prompt: str,
    beat_index: int,
    content_id: str,
    environment: str = "other",
) -> str | None:
    """Generate a Flux Schnell image for one storyboard beat via a 3-tier cascade.

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

    for tier, prompt in cascade:
        if not prompt:
            continue
        logger.debug(
            "Flux beat=%d content=%s tier=%s prompt_words=%d",
            beat_index, content_id, tier, len(prompt.split()),
        )
        path = _call_fal(prompt, cache_dir, media_path)
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

    def _generate_one(beat: dict) -> dict:
        idx         = beat.get("beat_order", beat.get("section_order", 0))
        prompt      = beat.get("flux_prompt", "")
        environment = beat.get("environment", "other")

        path = generate_beat_image(prompt, idx, content_id, environment=environment)
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
    return beats
