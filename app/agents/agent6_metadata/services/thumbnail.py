"""Agent 6 — thumbnail generation (base image + per-language Pillow overlay).

Roadmap: code_report/agent6_metadata_roadmap.md, Phases C (base image, stages
1-3) and D (per-language overlay, stages 4-5 — implemented here, gated on
Phase D's own platform_metadata_generation output, thumbnail_text, which is
produced by metadata_orchestrator.py, not by anything in this module). Only
runs for parent long-form content (``is_short_episode == False``) with at
least one verified ``platform="youtube"`` ``ChannelPlatform`` row for the
channel (Check 4).

Base-image generation reuses the shared
``app.services.flux_client.generate_beat_image()`` (Agent 6 roadmap Phase A)
at 1280x720 — never a second fal.ai integration point (CLAUDE.md §11.5). The
per-language overlay is Pillow-only — zero paid calls, zero network. Agent 6
does not reuse Agent 4's beat-oriented pixel-defect/health-check machinery
(Check 3 decision) — validation here is its own small, self-contained check
appropriate for a single standalone image, not a storyboard sequence.
"""

import logging
import shutil
import uuid
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, UnidentifiedImageError
from sqlalchemy.orm import Session

from app.agents.agent6_metadata import system_prompt
from app.config import settings
from app.models import ChannelConfig, ChannelPlatform, Content
from app.services import flux_client

logger = logging.getLogger(__name__)

_THUMBNAIL_WIDTH = 1280
_THUMBNAIL_HEIGHT = 720
# Catches a truncated/corrupt download without false-positiving on a
# legitimately simple image (roadmap's own stated floor).
_MIN_FILE_SIZE_BYTES = 5_000

# ── Per-language overlay (stages 4-5) ───────────────────────────────────────
_FONT_PATH = Path(__file__).resolve().parents[1] / "assets" / "fonts" / "ArchivoBlack-Regular.ttf"
_FONT_SIZE = 96
_STROKE_WIDTH = 4
# Keep the overlay text clear of the bottom-right duration-badge corner and
# top-left channel-icon area every platform UI overlays (roadmap's own
# stated concern) — a fraction of the frame, not a pixel constant, so it
# scales if the geometry ever changes.
_SAFE_MARGIN_RATIO = 0.08
# YouTube's real documented thumbnail upload ceiling.
_MAX_FILE_SIZE_BYTES = 2 * 1024 * 1024
# A codepoint from the Unicode Private Use Area — guaranteed absent from any
# ordinary TEXT font's cmap, so its rendered glyph is always the font's
# .notdef fallback. Used as a reference bounding box to detect "this font
# has no real glyph for this character" without a fontTools dependency: a
# character whose bbox matches the guaranteed-.notdef bbox is (with high
# confidence, CLAUDE.md's accepted heuristic tradeoff, §11.4) itself
# unrendered.
#
# Known limitation (recorded honestly, not silently fixed by adding a
# dependency): this is a bbox-equality heuristic, not a true cmap membership
# check, and rests on two assumptions that hold for the bundled Archivo Black
# (an ordinary Latin text face) but are not universal:
#   1. The font's .notdef glyph renders as a bbox distinguishable from every
#      real glyph's bbox. A font whose .notdef happens to coincide with some
#      real character's bbox would misclassify that character as missing —
#      implausible for a normal text face, not structurally impossible.
#   2. U+E000 itself is undefined in the font. This is NOT guaranteed for
#      icon/symbol fonts that deliberately assign real glyphs into the
#      Private Use Area (e.g. Font Awesome, Material Icons) — the still-open
#      "font as future per-channel config" decision (roadmap Open decisions)
#      must re-verify this probe against whichever font a future phase picks,
#      not assume it transfers unchanged.
_PROBE_MISSING_CODEPOINT = ""


def _resolve_model_key() -> flux_client.ModelKey:
    """Check 3 decision: "dev" tier when the operator's existing global
    kill switches allow it, else "schnell" — reuses
    IMAGE_ROUTING_ENABLED/IMAGE_ROUTING_ALLOW_DEV exactly as Agent 4's beat
    router does, rather than a third, thumbnail-specific env var."""
    if settings.image_routing_enabled and settings.image_routing_allow_dev:
        return "dev"
    return "schnell"


def _validate_raw_thumbnail(relative_path: str) -> tuple[bool, str]:
    """Deterministic validation for one generated image — dimensions,
    file-size floor, PIL-decodability. Hard equality on dimensions (not a
    tolerance band): a thumbnail has exactly one correct size with no
    downstream crop/letterbox step to absorb drift."""
    full_path = Path(settings.media_path) / relative_path
    if not full_path.is_file():
        return False, "file_missing"

    size = full_path.stat().st_size
    if size <= _MIN_FILE_SIZE_BYTES:
        return False, f"file_too_small_{size}_bytes"

    try:
        with Image.open(full_path) as image:
            image.verify()
    except (UnidentifiedImageError, OSError) as exc:
        return False, f"unreadable_image_{type(exc).__name__}"

    # verify() invalidates the file object for further use — reopen to read size.
    try:
        with Image.open(full_path) as image:
            width, height = image.size
    except (UnidentifiedImageError, OSError) as exc:
        return False, f"unreadable_image_{type(exc).__name__}"

    if (width, height) != (_THUMBNAIL_WIDTH, _THUMBNAIL_HEIGHT):
        return False, f"wrong_dimensions_{width}x{height}"

    return True, "ok"


def _place_base_image(raw_relative_path: str, content_id: uuid.UUID) -> str:
    """Copy the validated Flux-cache file to the stable, canonical
    ``{media_path}/thumbnails/{content_id}/base.jpg`` location Phase D's
    per-language overlay step will read from — a predictable path, not the
    Flux prompt-hash cache filename."""
    media_path = Path(settings.media_path)
    source = media_path / raw_relative_path
    dest_dir = media_path / "thumbnails" / str(content_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / "base.jpg"
    shutil.copyfile(source, dest)
    relative_dest = str(dest.relative_to(media_path))
    logger.info("THUMBNAIL_BASE_IMAGE_READY content=%s path=%s", content_id, relative_dest)
    return relative_dest


def _validate_and_place(
    raw_relative_path: str | None, content_id: uuid.UUID, attempt_label: str,
) -> str | None:
    if not raw_relative_path:
        logger.warning(
            "THUMBNAIL_FLUX_CALL_FAILED content=%s attempt=%s", content_id, attempt_label,
        )
        return None

    ok, reason = _validate_raw_thumbnail(raw_relative_path)
    if not ok:
        logger.warning(
            "THUMBNAIL_VALIDATION_FAILED content=%s attempt=%s reason=%s path=%s",
            content_id, attempt_label, reason, raw_relative_path,
        )
        return None

    return _place_base_image(raw_relative_path, content_id)


def generate_thumbnail_base_image(content_id: uuid.UUID, db: Session) -> str | None:
    """Generate the single, language-agnostic Flux base image for a
    content's YouTube thumbnail (stages 1-3 of the thumbnail pipeline — no
    per-language overlay, no VideoMetadata write; both are Phase D).

    Gated to parent long-form content (``is_short_episode == False``) with
    at least one verified ``platform="youtube"`` ``ChannelPlatform`` row for
    the channel — every child-of-parent Short, every Solo Short, and every
    channel with no YouTube target skips this entirely, with zero Flux calls
    (Check 4).

    On any unrecoverable failure this returns ``None`` rather than raising —
    a missing custom thumbnail is a degraded-but-shippable outcome (the
    codebase's established never-block policy for media generation,
    CLAUDE.md §11.4), never a reason to fail the whole content item.

    Args:
        content_id: UUID of content with status ``RENDERED``.
        db:         SQLAlchemy session managed by the caller.

    Returns:
        Local path relative to ``media_path``
        (``thumbnails/{content_id}/base.jpg``) on success, ``None`` if
        gated out, skipped, or unrecoverably failed.
    """
    content: Content | None = db.get(Content, content_id)
    if not content:
        logger.warning("THUMBNAIL_SKIPPED content=%s reason=content_not_found", content_id)
        return None

    if bool(getattr(content, "is_short_episode", False)):
        logger.info("THUMBNAIL_SKIPPED content=%s reason=is_short_episode", content_id)
        return None

    has_verified_youtube = (
        db.query(ChannelPlatform)
        .filter(
            ChannelPlatform.channel_id == content.channel_id,
            ChannelPlatform.platform == "youtube",
            ChannelPlatform.verified.is_(True),
        )
        .first()
    ) is not None
    if not has_verified_youtube:
        logger.info(
            "THUMBNAIL_SKIPPED content=%s reason=no_verified_youtube_platform", content_id,
        )
        return None

    config: ChannelConfig | None = db.get(ChannelConfig, content.channel_id)
    prompt = system_prompt.generate_thumbnail_prompt(
        content.story_blueprint,
        visual_style=config.visual_style if config else "",
        image_style=config.image_style if config else "",
    )

    model_key = _resolve_model_key()
    cid_str = str(content_id)

    raw_path = flux_client.generate_beat_image(
        prompt, 0, cid_str, environment="other",
        model_key=model_key, width=_THUMBNAIL_WIDTH, height=_THUMBNAIL_HEIGHT,
    )
    result = _validate_and_place(raw_path, content_id, attempt_label="initial")
    if result:
        return result

    # Exactly one retry, fresh cache key (mirrors flux_generator's
    # BEAT_IMAGE_HARD_RETRY one-retry convention) — a second failure is
    # non-fatal, logged, and returns None.
    retry_path = flux_client.generate_beat_image(
        prompt, 0, cid_str, environment="other",
        cache_key_extra="thumbnail_retry:1", model_key=model_key,
        width=_THUMBNAIL_WIDTH, height=_THUMBNAIL_HEIGHT,
    )
    result = _validate_and_place(retry_path, content_id, attempt_label="retry")
    if result:
        return result

    logger.error(
        "THUMBNAIL_GENERATION_FAILED content=%s — non-fatal, metadata generation "
        "proceeds without a custom thumbnail (YouTube falls back to its own "
        "platform-default thumbnail selection)",
        content_id,
    )
    return None


def _font_covers_text(font: "ImageFont.FreeTypeFont", text: str) -> tuple[bool, str | None]:
    """True if every non-whitespace character in ``text`` has a real glyph
    in ``font`` — see _PROBE_MISSING_CODEPOINT's comment for the technique.
    Cheap insurance today (every currently-supported language is Latin-script,
    Finding 3.5) — becomes load-bearing the moment a non-Latin-script
    language is added."""
    notdef_bbox = font.getbbox(_PROBE_MISSING_CODEPOINT)
    for char in text:
        if char.isspace():
            continue
        if font.getbbox(char) == notdef_bbox:
            return False, char
    return True, None


def _save_within_size_limit(image: "Image.Image", dest: Path) -> None:
    """Save as JPEG, reducing quality if needed to stay under YouTube's real
    2MB thumbnail upload ceiling (stage 5) — best-effort down to a quality
    floor rather than looping forever."""
    quality = 95
    while True:
        image.save(dest, format="JPEG", quality=quality)
        if dest.stat().st_size <= _MAX_FILE_SIZE_BYTES or quality <= 40:
            return
        quality -= 15


def composite_thumbnail_overlay(
    base_relative_path: str,
    thumbnail_text: str,
    language: str,
    content_id: uuid.UUID,
) -> str | None:
    """Stage 4 + 5: burn ``thumbnail_text`` onto a copy of the shared base
    image for one language, validate the result, and return its path.

    Never regenerates the base image — Pillow-only, zero paid calls, zero
    network. Non-fatal on any failure (missing base file, unreadable base
    image, an uncovered font glyph, or a final-validation failure): returns
    ``None`` and logs, leaving that language's ``thumbnail_file_path`` NULL
    rather than blocking the rest of metadata generation.

    Args:
        base_relative_path: Path to the shared base image, relative to
            media_path (``thumbnails/{content_id}/base.jpg`` — Phase C's
            canonical output).
        thumbnail_text:     Already platform-limit-enforced overlay phrase
            for this language (Check 1 v2 decision — never the title).
        language:            BCP-47 language code — determines the output
            filename.
        content_id:          Content UUID, for logging and path construction.

    Returns:
        Local path relative to ``media_path``
        (``thumbnails/{content_id}/{language}.jpg``) on success, ``None``
        otherwise.
    """
    media_path = Path(settings.media_path)
    base_path = media_path / base_relative_path
    if not base_path.is_file():
        logger.warning(
            "THUMBNAIL_OVERLAY_BASE_MISSING content=%s language=%s path=%s",
            content_id, language, base_relative_path,
        )
        return None

    font = ImageFont.truetype(str(_FONT_PATH), _FONT_SIZE)
    covers, bad_char = _font_covers_text(font, thumbnail_text)
    if not covers:
        logger.warning(
            "THUMBNAIL_FONT_GLYPH_MISSING content=%s language=%s char=%r",
            content_id, language, bad_char,
        )
        return None

    try:
        with Image.open(base_path) as base_image:
            image = base_image.convert("RGB").copy()
    except (UnidentifiedImageError, OSError) as exc:
        logger.warning(
            "THUMBNAIL_OVERLAY_BASE_UNREADABLE content=%s language=%s "
            "exception_type=%s",
            content_id, language, type(exc).__name__,
        )
        return None

    draw = ImageDraw.Draw(image)
    width, height = image.size
    margin_x = int(width * _SAFE_MARGIN_RATIO)
    margin_y = int(height * _SAFE_MARGIN_RATIO)
    left, top, right, bottom = draw.textbbox(
        (0, 0), thumbnail_text, font=font, stroke_width=_STROKE_WIDTH,
    )
    text_w, text_h = right - left, bottom - top
    x = max(margin_x, (width - text_w) // 2)
    # Anchored toward the bottom, but pulled up clear of the bottom-right
    # duration-badge corner every platform UI overlays.
    y = max(margin_y, height - margin_y - text_h - int(height * 0.10))
    draw.text(
        (x - left, y - top), thumbnail_text, font=font, fill=(255, 255, 255),
        stroke_width=_STROKE_WIDTH, stroke_fill=(0, 0, 0),
    )

    dest_dir = media_path / "thumbnails" / str(content_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{language}.jpg"
    _save_within_size_limit(image, dest)

    relative_dest = str(dest.relative_to(media_path))
    ok, reason = _validate_raw_thumbnail(relative_dest)
    if not ok:
        logger.warning(
            "THUMBNAIL_OVERLAY_VALIDATION_FAILED content=%s language=%s reason=%s",
            content_id, language, reason,
        )
        return None

    logger.info(
        "THUMBNAIL_OVERLAY_READY content=%s language=%s path=%s",
        content_id, language, relative_dest,
    )
    return relative_dest
