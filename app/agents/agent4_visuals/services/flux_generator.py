"""Agent 4 provider wrapper for fal.ai Flux image generation.

The raw fal.ai HTTP call site (`_call_fal()`) and the generic 3-tier cascade
wrapper (`generate_beat_image()`) moved to the shared `app.services.flux_client`
module (Agent 6 roadmap, Phase A, `code_report/agent6_metadata_roadmap.md`) so
a second agent can reuse them without importing this package — re-imported
below (aliased to their original names) so every function in this file that
still calls them needs zero changes. `app.services.flux_client` remains the
only allowed direct `fal_client` integration point in the whole codebase;
nothing in this file talks to `fal_client` directly anymore.

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

Every successfully generated image also passes the unified image health gate
(``_ensure_beat_image_healthy()``): near-black, baked-in letterbox, and
off-aspect dimensions are detected via ``media_validation``'s own detectors,
then healed with a bounded two-attempt ladder — a corrective-clause reroll,
then a deterministic prompt rewrite (visual_intent + composition variation +
both corrective clauses) — before neighbor-fill becomes the rare terminal
fallback (explicit operator preference: regenerate with a changed prompt
rather than duplicate a neighbor's image).
"""

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from PIL import Image, ImageStat, UnidentifiedImageError

from app.config import settings
from app.agents.agent4_visuals.services import image_router
from app.agents.agent4_visuals.subagents.storyboard_validator import composition_slot_variation
from app.services.flux_client import (
    DEFAULT_MODEL_KEY as _DEFAULT_MODEL_KEY,
    DEFAULT_WIDTH as _DEFAULT_WIDTH,
    DEFAULT_HEIGHT as _DEFAULT_HEIGHT,
    ENV_SAFE_PROMPTS as _ENV_SAFE_PROMPTS,
    generate_beat_image,
)

logger = logging.getLogger(__name__)

_INTER_BEAT_SLEEP_SEC = 0.5  # conservative until rate limits confirmed
_PIXEL_HASH_SIZE = 8
_PIXEL_HASH_COLLISION_MAX_DISTANCE = 3

# Luminance gate (operator-confirmed live-canary fix): a real production run
# shipped a beat whose Flux generation was a 100% black JPEG (mean luminance
# 0.0/255) — it passed every existing check, since none of them inspect
# actual pixel content, only file existence/decodability/aspect ratio. This
# floor is the same threshold used to audit that run's cached images (ffprobe
# signalstats YAVG < 24 correlated with visibly black/near-black rendered
# frames once CSS color-grade filters were applied on top).
_LUMINANCE_MEAN_FLOOR = 24.0
_WELL_LIT_REROLL_CLAUSE = (
    "bright well-lit scene, strong visible ambient light source, "
    "no underexposure, no near-black frame"
)
# Corrective clause for the letterbox reroll gate (fix-and-retry, never
# block): a real run shipped Flux generations with black cinematic bars
# baked into the pixels, which the render then displays inside the real
# video frame — double letterboxing that reads as a broken export.
_FULL_BLEED_REROLL_CLAUSE = (
    "full-bleed composition filling the entire frame edge to edge, "
    "no black bars, no letterboxing, no cinematic bars, no borders or "
    "frame around the image"
)

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
    # Added after an independent review of real output found these exact
    # words in beats that slipped past sanitization (7 of 15 book/ledger/
    # parchment-mentioning beats in one run were uncaught — code_report/
    # 8abd7fea_independent_video_output_review.md, finding 2).
    "book", "ledger", "parchment", "scroll", "manuscript", "notice",
    "notice board", "seal", "wax seal",
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

# Elimination Mandate (code_report/forensic_output_audit_borrasca_run.md,
# D2.2/D2.3): the previous sanitizer rewrote the subject entirely (environment
# scene hint + detected prop label + a selected ANGLE/DISTANCE/LIGHTING/DETAIL
# framing clause + a 10-clause negative wall), which produced broken English
# ("corkboard-less wide shot"), self-contradictions ("Sam's hands... his
# expression" alongside "no people"), and identical sanitized prompts for
# distinct beats (since the template ignored most of the original prompt) —
# directly causing duplicate images. Claude's own flux_prompt is never
# rewritten now; exactly one short clause is appended.
_TEXT_PROP_NO_TEXT_CLAUSE = "no readable text or legible words in the frame"

# Applied to EVERY generated beat, not just text-prop beats (independent
# review of real output found a stray "2024." watermark-style mark baked
# into an ordinary portrait/window beat with no document/sign/poster subject
# — code_report/8abd7fea_independent_video_output_review.md, finding 3).
# Flux models occasionally bake in stock-photo-style watermarks, signatures,
# or date stamps regardless of subject; this is the same one-clause-append
# pattern already proven safe for text props, just applied unconditionally.
UNIVERSAL_NO_ARTIFACT_CLAUSE = (
    "no watermark, no signature, no visible date or timestamp, no logos"
)


def is_text_prop_beat(beat: dict) -> bool:
    """True for a beat whose prop would naturally carry readable text —
    a document, poster, calendar, sign, name tag, etc. (word-boundary match;
    see _TEXT_PROP_KEYWORD_PATTERNS)."""
    haystack = " ".join(str(beat.get(f, "") or "") for f in _TEXT_PROP_FIELDS).lower()
    return any(pattern.search(haystack) for _, pattern in _TEXT_PROP_KEYWORD_PATTERNS)


def derive_text_prop_prompt(beat: dict) -> str:
    """Return Claude's own flux_prompt verbatim, plus one no-readable-text clause.

    Never rewrites the subject — see the Elimination Mandate note above this
    function. Falls back to visual_intent only if flux_prompt is genuinely
    empty (never fabricates new subject text).
    """
    original = str(beat.get("flux_prompt") or beat.get("visual_intent") or "").strip()
    if not original:
        return _TEXT_PROP_NO_TEXT_CLAUSE
    return f"{original}, {_TEXT_PROP_NO_TEXT_CLAUSE}"


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

    With routing explicitly disabled, this resolves to ``"schnell"``. The
    operator-enabled default routes qualifying beats to Dev while ordinary
    beats stay on Schnell. A beat whose ``media_url`` is already a
    local ``cache/`` path short-circuits (``route.source == "reuse"``):
    the existing path is returned untouched with no generation call and no
    tier-count increment.

    ``width``/``height`` default to the repo's landscape default so the
    parent path is unaffected; the child Short path (roadmap 3.1) passes
    1080x1920.
    """
    route = image_router.select_route(
        beat, content_id,
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


def _append_composition_variation(prompt: str, occurrence: int) -> tuple[str, str]:
    variation = composition_slot_variation(occurrence)
    base = str(prompt or "").rstrip(". ")
    return (f"{base}, {variation}." if base else f"{variation}.", variation)


def _average_pixel_hash(media_url: str, media_path: Path) -> int | None:
    path = Path(media_url)
    image_path = path if path.is_absolute() else media_path / path
    try:
        with Image.open(image_path) as image:
            gray = image.convert("L").resize((_PIXEL_HASH_SIZE, _PIXEL_HASH_SIZE))
            pixels = list(gray.getdata())
    except (FileNotFoundError, UnidentifiedImageError, OSError, ValueError) as exc:
        logger.warning(
            "PIXEL_HASH_UNAVAILABLE media_url=%s exception_type=%s exception_message=%s",
            media_url, type(exc).__name__, str(exc),
        )
        return None

    avg = sum(pixels) / len(pixels)
    value = 0
    for pixel in pixels:
        value = (value << 1) | int(pixel >= avg)
    return value


def _hamming_distance(left: int, right: int) -> int:
    return (left ^ right).bit_count()


def _find_pixel_collision(
    media_url: str,
    pixel_ledger: list[dict],
    *,
    media_path: Path,
) -> tuple[int, dict, int] | None:
    image_hash = _average_pixel_hash(media_url, media_path)
    if image_hash is None:
        return None
    for entry in pixel_ledger:
        distance = _hamming_distance(image_hash, entry["hash"])
        if distance <= _PIXEL_HASH_COLLISION_MAX_DISTANCE:
            return image_hash, entry, distance
    return None


def _record_pixel_hash(
    media_url: str,
    pixel_ledger: list[dict],
    *,
    media_path: Path,
    beat_order: int,
) -> None:
    image_hash = _average_pixel_hash(media_url, media_path)
    if image_hash is not None:
        pixel_ledger.append({"hash": image_hash, "media_url": media_url, "beat_order": beat_order})


def _dedupe_generated_image_once(
    beat: dict,
    path: str,
    content_id: str,
    tier_counts: dict[str, int],
    pixel_ledger: list[dict],
    *,
    width: int,
    height: int,
) -> str:
    """Reroll one generated image if its perceptual hash collides in this run.

    This is an anti-duplicate guard, not a quality loop: it compares only local
    pixels against earlier accepted images in the same generation call and makes
    at most one deterministic prompt variation.
    """
    media_path = Path(settings.media_path)
    idx = beat.get("beat_order", beat.get("section_order", 0))
    collision = _find_pixel_collision(path, pixel_ledger, media_path=media_path)
    if collision is None:
        _record_pixel_hash(path, pixel_ledger, media_path=media_path, beat_order=idx)
        return path

    _current_hash, prior, distance = collision
    reroll_prompt, variation = _append_composition_variation(
        str(beat.get("flux_prompt", "") or ""),
        len(pixel_ledger),
    )
    reroll_beat = dict(beat)
    reroll_beat["media_url"] = ""
    reroll_beat["flux_prompt"] = reroll_prompt
    logger.warning(
        "PIXEL_DUPLICATE_REROLL content=%s beat=%s prior_beat=%s distance=%d "
        "variation=%r",
        content_id, idx, prior.get("beat_order"), distance, variation,
    )

    reroll_path = generate_beat_image_with_routing(
        reroll_beat, content_id, tier_counts, width=width, height=height,
    )
    if reroll_path:
        beat["flux_prompt"] = reroll_prompt
        post_collision = _find_pixel_collision(reroll_path, pixel_ledger, media_path=media_path)
        if post_collision is not None:
            _hash, post_prior, post_distance = post_collision
            logger.error(
                "PIXEL_DUPLICATE_REROLL_STILL_COLLIDES content=%s beat=%s "
                "prior_beat=%s distance=%d media_url=%s",
                content_id, idx, post_prior.get("beat_order"), post_distance, reroll_path,
            )
        _record_pixel_hash(reroll_path, pixel_ledger, media_path=media_path, beat_order=idx)
        return reroll_path

    logger.error(
        "PIXEL_DUPLICATE_REROLL_FAILED content=%s beat=%s original_media_url=%s",
        content_id, idx, path,
    )
    _record_pixel_hash(path, pixel_ledger, media_path=media_path, beat_order=idx)
    return path


def _mean_luminance(media_url: str, media_path: Path) -> float | None:
    """Mean grayscale luminance (0-255) of a generated image.

    The same signal ffprobe's ``signalstats`` ``YAVG`` measures — used here
    to catch a Flux generation that silently produced a pure-black or
    near-black frame, which nothing else in this module inspects (existence/
    decodability checks pass a black JPEG just as readily as a normal one).
    """
    path = Path(media_url)
    image_path = path if path.is_absolute() else media_path / path
    try:
        with Image.open(image_path) as image:
            mean = ImageStat.Stat(image.convert("L")).mean[0]
    except (FileNotFoundError, UnidentifiedImageError, OSError, ValueError) as exc:
        logger.warning(
            "LUMINANCE_CHECK_UNAVAILABLE media_url=%s exception_type=%s exception_message=%s",
            media_url, type(exc).__name__, str(exc),
        )
        return None
    return mean


def _append_well_lit_clause(prompt: str) -> str:
    base = str(prompt or "").rstrip(". ")
    return f"{base}, {_WELL_LIT_REROLL_CLAUSE}." if base else f"{_WELL_LIT_REROLL_CLAUSE}."


def _detect_image_defect(
    resolved: Path, *, width: int, height: int,
) -> tuple[str, str] | None:
    """Every repairable pixel-defect class behind one call: near-black,
    baked-in letterbox, and off-aspect dimensions — reusing
    ``media_validation``'s own detectors (single implementation, never
    forked). Fail-open on unreadable files (the structural checks own those).
    """
    from app.agents.agent4_visuals.services.media_validation import (
        detect_image_pixel_defect,
        detect_image_wrong_aspect,
    )

    return (
        detect_image_pixel_defect(resolved)
        or detect_image_wrong_aspect(resolved, width / height)
    )


def _build_defect_rewrite_prompt(beat: dict, idx: int) -> str:
    """Deterministic prompt REWRITE for the second healing attempt.

    Per explicit operator preference (2026-07-16), a defective image gets a
    genuinely DIFFERENT image request before any neighbor duplication: built
    from the beat's ``visual_intent`` (or the environment-safe base when the
    intent is too thin), a composition-slot variation drawn from the shared
    24-slot rotation, and BOTH corrective clauses. Pure string construction —
    no AI call.
    """
    intent = str(beat.get("visual_intent") or "").strip().rstrip(". ")
    environment = str(beat.get("environment") or "other")
    base = intent if len(intent.split()) >= 4 else _ENV_SAFE_PROMPTS.get(
        environment, _ENV_SAFE_PROMPTS["other"],
    )
    return (
        f"{base}, {composition_slot_variation(idx)}, "
        f"{_WELL_LIT_REROLL_CLAUSE}, {_FULL_BLEED_REROLL_CLAUSE}."
    )


def _ensure_beat_image_healthy(
    beat: dict,
    path: str,
    content_id: str,
    *,
    width: int,
    height: int,
) -> str:
    """Unified image health gate — detect → fix → retry, neighbor-fill last.

    Replaces the former separate dark/letterbox/size gates (which each made
    one attempt and fell straight to neighbor duplication — a fallback the
    operator explicitly dislikes). Escalation ladder, bounded at TWO extra
    generations per defective beat:

      1. corrective-clause reroll — the beat's own prompt plus the clause
         matched to the detected defect (well-lit / full-bleed; a size
         defect keeps the prompt unchanged), with a fresh cache key — the
         identical prompt+size would hash straight back to the cached bad
         artifact;
      2. deterministic prompt REWRITE (``_build_defect_rewrite_prompt``) —
         a different image request, not the same prompt re-rolled.

    Every regenerated image is re-checked against ALL defect classes, not
    just the one that triggered healing (closes the former cross-check gap
    where e.g. a dark-reroll's replacement was never letterbox-checked).
    Only when both attempts fail does the beat hand off to neighbor-fill
    (``""`` return) — the rare terminal fallback; something must still fill
    the beat, or the render defers on missing media.

    Returns:
        A healthy local path, or ``""`` to signal neighbor-fill hand-off.
    """
    media_path = Path(settings.media_path)
    idx = beat.get("beat_order", beat.get("section_order", 0))

    def _resolved(p: str) -> Path:
        pp = Path(p)
        return pp if pp.is_absolute() else media_path / pp

    defect = _detect_image_defect(_resolved(path), width=width, height=height)
    if defect is None:
        return path
    code = defect[0]
    logger.warning(
        "BEAT_IMAGE_DEFECT content=%s beat=%s code=%s media_url=%s — healing",
        content_id, idx, code, path,
    )

    base_prompt = str(beat.get("flux_prompt", "") or "")
    if code == "near_black_image":
        attempt1_prompt = _append_well_lit_clause(base_prompt)
    elif code == "letterboxed_image":
        attempt1_prompt = _append_full_bleed_clause(base_prompt)
    else:
        attempt1_prompt = base_prompt

    environment = beat.get("environment", "other")
    for attempt, prompt in (
        (1, attempt1_prompt),
        (2, _build_defect_rewrite_prompt(beat, idx)),
    ):
        new_path = generate_beat_image(
            prompt, idx, content_id,
            environment=environment,
            cache_key_extra=f"heal{attempt}:{code}:{idx}",
            width=width, height=height,
        )
        if not new_path:
            logger.warning(
                "BEAT_IMAGE_HEAL_ATTEMPT_FAILED content=%s beat=%s attempt=%d "
                "code=%s — generation returned nothing",
                content_id, idx, attempt, code,
            )
            continue
        remaining = _detect_image_defect(
            _resolved(new_path), width=width, height=height,
        )
        if remaining is None:
            beat["flux_prompt"] = prompt
            logger.warning(
                "BEAT_IMAGE_HEALED content=%s beat=%s attempt=%d code=%s new=%s",
                content_id, idx, attempt, code, new_path,
            )
            return new_path
        code = remaining[0]
        logger.warning(
            "BEAT_IMAGE_HEAL_ATTEMPT_STILL_DEFECTIVE content=%s beat=%s "
            "attempt=%d remaining_code=%s",
            content_id, idx, attempt, code,
        )

    logger.error(
        "BEAT_IMAGE_HEAL_EXHAUSTED content=%s beat=%s code=%s "
        "— handing off to neighbor-fill (terminal fallback)",
        content_id, idx, code,
    )
    return ""


def _append_full_bleed_clause(prompt: str) -> str:
    base = str(prompt or "").rstrip(". ")
    return f"{base}, {_FULL_BLEED_REROLL_CLAUSE}." if base else f"{_FULL_BLEED_REROLL_CLAUSE}."


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


def generate_all_beat_images(
    beats: list[dict], content_id: str,
    width: int = _DEFAULT_WIDTH, height: int = _DEFAULT_HEIGHT,
) -> list[dict]:
    """Generate Flux images for all beats sequentially (1 worker, 0.5s inter-beat sleep).

    Mutates each beat in-place:
      - Success: sets ``beat["media_url"]`` to a local cache path, ``beat["media_type"] = "image"``
      - Hard failure: one extra environment-safe retry with a fresh cache key,
        then ``fill_failed_beats_from_neighbors()`` reuses the nearest
        neighbouring beat's image. Never a text card, never on-screen text
        (subtitles-only rendering, audit G-0/G-8).
      - Pixel defect (near-black / letterbox / off-aspect):
        ``_ensure_beat_image_healthy()`` heals with up to two regeneration
        attempts (corrective clause, then a deterministic prompt rewrite);
        only after both fail does the beat fall through to neighbor-fill.

    Args:
        beats:      Storyboard beat dicts with a ``flux_prompt`` field.
        content_id: Content UUID string for logging.
        width:      Image width — landscape 1920 (default) for the parent
                    path; the Solo Short visual pass (output_mode
                    "shorts_only", see code_report/output_mode_shorts_only_
                    and_youtube_long_only_roadmap.md) passes 1080 for
                    portrait, mirroring generate_pending_beat_images()'s
                    existing child-remap portrait generation.
        height:     Image height — landscape 1080 (default) or 1920 (Solo
                    Short portrait).

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
    pixel_ledger: list[dict] = []

    def _generate_one(beat: dict) -> dict:
        idx = beat.get("beat_order", beat.get("section_order", 0))
        path = generate_beat_image_with_routing(
            beat, content_id, tier_counts, width=width, height=height,
        )
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
                width=width, height=height,
            )
        if path:
            path = _dedupe_generated_image_once(
                beat, path, content_id, tier_counts, pixel_ledger,
                width=width, height=height,
            )
            path = _ensure_beat_image_healthy(
                beat, path, content_id,
                width=width, height=height,
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
