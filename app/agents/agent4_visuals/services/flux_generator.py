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
CANONICAL_CHARACTER_DESCRIPTOR_KEY = "canonical_character_descriptor_lines"


def canonical_character_descriptor(entry: dict) -> str:
    """Serialize one blueprint descriptor in one immutable field order."""
    if not isinstance(entry, dict):
        return ""
    name = str(entry.get("name") or "").strip()
    # Read compatibility for persisted pre-5.8 blueprints.
    if "description" in entry:
        description = str(entry.get("description") or "").strip()
        age = str(entry.get("age") or "").strip()
        return f"{name}{f', {age}' if age else ''} — {description}" if name and description else ""
    fields = {
        key: str(entry.get(key) or "").strip()
        for key in (
            "apparent_age_band", "build", "hair_color", "hair_length", "hair_style",
            "facial_hair", "distinguishing_facial_features", "clothing_garment",
            "clothing_colors", "signature_accessory",
        )
    }
    if not name or not all(fields.values()):
        return ""
    return (
        f"{name} — apparent age: {fields['apparent_age_band']}; build: {fields['build']}; "
        f"hair: {fields['hair_color']}, {fields['hair_length']}, {fields['hair_style']}; "
        f"facial hair: {fields['facial_hair']}; distinguishing facial features: "
        f"{fields['distinguishing_facial_features']}; clothing: "
        f"{fields['clothing_garment']} in {fields['clothing_colors']}; signature accessory: "
        f"{fields['signature_accessory']}"
    )

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
    "notice board", "seal", "wax seal", "checklist", "list", "book cover",
    "cover", "signage", "storefront", "banknote", "bank note", "bill",
    "watermark", "artist credit", "credit line",
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

# Phase A3.1: Schnell tends to draw gibberish even when a text-bearing object
# is followed by several negative clauses. Reframe the *object class* into a
# physical detail instead. The table is deliberately small and generic; it
# never interprets story meaning. The original text-bearing subject is omitted
# because retaining it as "context" still causes Schnell to draw the object.
_TEXT_PROP_MATERIAL_REFRAMES: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("document", "report", "case file", "file folder", "letter", "diary", "manuscript"),
     ("paper texture, torn edges, folds, and a wax seal in close-up",
      "creased paper fibers, period fasteners, ink-stained fingertips, and raking light",
      "stacked paper edges, worn corners, binding thread, and hands sorting the pages")),
    (("checklist", "list"),
     ("a hand marking abstract rows on textured paper, with all marks out of focus",
      "creased paper, repeated non-legible strokes, a blunt pencil, and strong side light")),
    (("board", "price board", "notice board", "signage", "storefront"),
     ("chalk dust and a hand mid-stroke, with the board surface out of focus",
      "weathered painted surface, mounting brackets, peeling edges, and street context",
      "shopfront material details, worn trim, reflections, and an oblique unreadable sign face")),
    (("sign", "street sign", "name tag", "label", "identification card", "id card", "license"),
     ("weathered material, mounting hardware, edges, and a hand interacting with it",
      "an edge-on view of the object, scuffed surface, fasteners, and shallow focus")),
    (("screen", "phone screen", "text message", "phone message"),
     ("screen glow reflected on a face and hands, with the display itself out of focus",
      "a face lit by soft device glow, fingers gripping the dark edge, display turned away")),
    (("newspaper", "article", "headline"),
     ("newsprint texture, folded paper edges, ink grain, and hands holding the page",
      "a rolled newspaper edge, coarse fibers, smudged ink texture, and a reader's hands")),
    (("map", "map caption"),
     ("creased map paper, route lines as non-legible shapes, a pointing hand, and surrounding terrain tools",)),
    (("poster", "missing poster", "missing person poster", "wanted poster", "notice"),
     ("torn paper fibers, fastening pins, weathered edges, and the surrounding wall context",
      "a curling paper corner, paste residue, rough masonry, and an oblique unreadable surface")),
    (("calendar",),
     ("paper layers, a turning page, binding rings, and angled daylight",)),
    (("book", "book cover", "cover", "ledger", "parchment", "scroll"),
     ("aged material texture, binding or rolled edges, clasps, and hands interacting with it",
      "worn cover corners, embossed texture without symbols, page edges, and a hand opening it",
      "binding thread, deckled page edges, dust, and an edge-on close-up")),
    (("banknote", "bank note", "bill"),
     ("period paper currency seen edge-on, fibrous material, folds, and a hand exchanging it",
      "a worn stack of period notes, frayed edges and ink texture, with faces out of focus")),
    (("watermark", "artist credit", "credit line"),
     ("the underlying scene reframed in a tight material-detail close-up with clean image edges",
      "the main physical subject viewed from an oblique angle with uncluttered negative space")),
)

_META_RULE_FRAGMENT_RE = re.compile(
    r"\b(?:title[ -]?card(?: typography)?|no readable text|legible words?|"
    r"rule phrasing|prompt rule|do not render text)\b",
    re.IGNORECASE,
)
_STYLE_MARKER_RE = re.compile(
    r"\b(?:photorealistic|cinematic realism|cinematic[_ -]cartoon|dark realistic|"
    r"vintage film|digital art|oil painting|watercolor|anime|illustration)\b",
    re.IGNORECASE,
)
_STYLE_CONTINUATION_RE = re.compile(
    r"\b(?:outlines?|color fills?|stylized|palette|grain|render|lighting|sharp focus)\b",
    re.IGNORECASE,
)


def _style_anchor_chunks(prompt: str) -> list[str]:
    """Return the leading configured style clause without paraphrasing it."""
    chunks = [chunk.strip() for chunk in str(prompt or "").split(",") if chunk.strip()]
    style_chunks: list[str] = []
    collecting = False
    for chunk in chunks:
        if _META_RULE_FRAGMENT_RE.search(chunk):
            continue
        if _STYLE_MARKER_RE.search(chunk):
            collecting = True
        elif collecting and not _STYLE_CONTINUATION_RE.search(chunk):
            break
        if collecting:
            style_chunks.append(chunk)
    return style_chunks


def preserve_character_descriptor_lines(beat: dict, prompt: str) -> str:
    """Insert stored canonical descriptor lines verbatim after the style prefix."""
    lines = [
        str(line).strip() for line in beat.get(CANONICAL_CHARACTER_DESCRIPTOR_KEY, [])
        if str(line).strip()
    ]
    if not lines:
        return prompt
    remainder = str(prompt or "").strip(" ,")
    for line in lines:
        remainder = remainder.replace(line, "").strip(" ,")
    style_chunks = _style_anchor_chunks(remainder)
    if not style_chunks:
        style_chunks = _style_anchor_chunks(str(beat.get("flux_prompt") or ""))
    style_anchor = ", ".join(style_chunks)
    if style_anchor and remainder.startswith(style_anchor):
        remainder = remainder[len(style_anchor):].strip(" ,")
    parts = [part for part in (style_anchor, *lines, remainder) if part]
    return ", ".join(parts)

_MATERIAL_CONTEXT_LIGHTING_RE = re.compile(
    r"\b(?:under|in|with|lit by)\s+(?:a[n]?\s+)?"
    r"(?:bright|well-lit|daylight|sunlit|fluorescent|incandescent|golden|overcast)"
    r"[a-z -]{0,32}(?:light|lamp|lighting|sunlight|daylight)\b",
    re.IGNORECASE,
)

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

# Task 2a (code_report/TODO, 2026-08-05): a text-bearing keyword merely
# PRESENT somewhere in the scene ("...a market scene, newspapers stacked on
# a nearby cart...") used to trigger the same full material-texture reframe
# as a keyword that IS the prompt's own primary subject — discarding a
# perfectly fine background/context prompt for no reason. A keyword counts
# as the primary subject only when a structured field already says so
# (visual_type/motif == "document") or it falls within the first
# _PRIMARY_SUBJECT_WINDOW_WORDS of the prompt body — where every real
# storyboard prompt's own "of a/an/the <subject>" clause sits (e.g.
# "cinematic cartoon illustration of a rough hand-carved mine tunnel
# entrance"), right after the leading style-anchor clause.
_PRIMARY_SUBJECT_WINDOW_WORDS = 8

# Human-readable phrase per storyboard.py's _VALID_ENVIRONMENTS enum, used to
# keep the beat's own environment in the rewritten prompt instead of
# discarding it (Task 2a: "rewrite output preserves the original environment
# ... descriptors"). "" for values with no natural standalone phrasing.
_ENVIRONMENT_PHRASES: dict[str, str] = {
    "underwater": "underwater",
    "indoor_office": "in an office interior",
    "indoor_domestic": "in a domestic interior",
    "forest_nature": "outdoors in a natural forest setting",
    "urban_street": "on an urban street",
    "corridor_interior": "in an interior corridor",
    "abstract_dark": "against a dark abstract backdrop",
    "open_landscape": "in an open outdoor landscape",
    "laboratory": "in a laboratory interior",
    "industrial": "in an industrial interior",
    "vehicle": "inside a vehicle",
    "other": "",
}

# Best-effort, deterministic era/period phrase extraction from the ORIGINAL
# prompt, so a full reframe doesn't silently drop a period the storyboard
# prompt's own era/setting lock (CLAUDE.md §11.4 build_continuity_line_from_
# blueprint) put there. Heuristic, not exhaustive — same accepted tradeoff as
# every other deterministic keyword check in this codebase.
_ERA_DESCRIPTOR_RE = re.compile(
    r"\b(?:\d{1,2}(?:st|nd|rd|th)[- ]century|medieval|ancient|victorian|"
    r"edwardian|renaissance|colonial|prehistoric|byzantine|roman|greek|"
    r"[12]\d{3}s)\b",
    re.IGNORECASE,
)


def _matched_text_prop_keyword(beat: dict) -> str | None:
    """First text-prop keyword found anywhere across the detection fields, or
    None. Shared by is_text_prop_beat() (detection) and
    derive_text_prop_prompt() (so the de-emphasis fallback can name the same
    keyword the detector matched, instead of a generic placeholder)."""
    haystack = " ".join(str(beat.get(f, "") or "") for f in _TEXT_PROP_FIELDS).lower()
    for keyword, pattern in _TEXT_PROP_KEYWORD_PATTERNS:
        if pattern.search(haystack):
            return keyword
    return None


def is_text_prop_beat(beat: dict) -> bool:
    """True for a beat whose prop would naturally carry readable text —
    a document, poster, calendar, sign, name tag, etc. (word-boundary match;
    see _TEXT_PROP_KEYWORD_PATTERNS). This is the overall on/off gate for
    text-prop handling; see _is_primary_subject_text_prop() for the separate
    full-reframe-vs-de-emphasis-only decision inside derive_text_prop_prompt()."""
    return _matched_text_prop_keyword(beat) is not None


def _is_primary_subject_text_prop(beat: dict) -> bool:
    """True when the text-bearing keyword is the prompt's own primary
    subject (head noun phrase), not merely present somewhere in the scene —
    see _PRIMARY_SUBJECT_WINDOW_WORDS above for the deterministic rule.

    Uses _STYLE_MARKER_RE directly (not _style_anchor_chunks, which is
    comma-chunk-granular: a real prompt's leading style words and its own
    subject clause typically sit in the SAME first comma-chunk, e.g.
    "cinematic cartoon illustration of a rough hand-carved mine tunnel
    entrance" has no comma before "entrance" — chunk-level stripping would
    discard the subject clause along with the style words). Instead this
    slices right after the style-marker match itself, wherever it falls.
    """
    if str(beat.get("visual_type", "") or "").lower() == "document":
        return True
    if str(beat.get("motif", "") or "").lower() == "document":
        return True

    prompt = str(beat.get("flux_prompt", "") or "")
    style_match = _STYLE_MARKER_RE.search(prompt)
    body = prompt[style_match.end():].strip(" ,") if style_match else prompt
    window_text = " ".join(body.lower().split()[:_PRIMARY_SUBJECT_WINDOW_WORDS])
    return any(pattern.search(window_text) for _, pattern in _TEXT_PROP_KEYWORD_PATTERNS)


def _derive_deemphasis_prompt(beat: dict, matched_keyword: str) -> str:
    """Keep the original prompt, only de-emphasizing the text-bearing
    element — used for a peripheral mention (not the primary subject) or
    when the primary subject is text-bearing but no known material reframe
    exists for it. Never fabricates a generic "the object" placeholder;
    names the actual matched keyword instead."""
    original = str(beat.get("flux_prompt", "") or "").rstrip(". ")
    clause = f"{matched_keyword} out of focus and unreadable in the background"
    rewritten = f"{original}, {clause}" if original else clause
    return preserve_character_descriptor_lines(beat, rewritten)


def derive_text_prop_prompt(beat: dict) -> str:
    """Reframe a text-bearing subject as material detail, deterministically.

    Only a PRIMARY-subject text prop with a known material reframe gets the
    full texture-reframe rewrite. A peripheral mention, or a primary subject
    this module has no reframe table entry for, is de-emphasized in place
    instead — the original prompt is kept, never replaced with a generic
    "the object" placeholder (Task 2a, code_report/TODO, 2026-08-05).
    """
    matched_keyword = _matched_text_prop_keyword(beat)
    if not _is_primary_subject_text_prop(beat):
        return _derive_deemphasis_prompt(beat, matched_keyword or "text")

    descriptive_haystack = " ".join(
        str(beat.get(field, "") or "")
        for field in ("flux_prompt", "visual_intent")
    ).lower()
    fallback_haystack = str(beat.get("motif", "") or "").lower()
    beat_order = int(beat.get("beat_order", 0) or 0)
    reframe = None
    for keywords, candidates in _TEXT_PROP_MATERIAL_REFRAMES:
        if any(re.search(r"\b" + re.escape(keyword) + r"\b", descriptive_haystack) for keyword in keywords):
            reframe = candidates[beat_order % len(candidates)]
            break
    else:
        for keywords, candidates in _TEXT_PROP_MATERIAL_REFRAMES:
            if any(re.search(r"\b" + re.escape(keyword) + r"\b", fallback_haystack) for keyword in keywords):
                reframe = candidates[beat_order % len(candidates)]
                break

    if reframe is None:
        # Primary subject, but this module has no reframe entry for it —
        # de-emphasize the original rather than fabricate a generic line.
        return _derive_deemphasis_prompt(beat, matched_keyword or "text")

    original_prompt = str(beat.get("flux_prompt") or "")
    style_chunks = _style_anchor_chunks(original_prompt)
    style_anchor = ", ".join(style_chunks)
    # _style_anchor_chunks is comma-chunk-granular: when the style words and
    # the subject clause share one chunk with no comma between them
    # ("cinematic cartoon illustration of an open ledger on a wooden desk"),
    # it sweeps the subject INTO the anchor too — leaking the very
    # text-bearing noun this reframe exists to remove. Fall back to slicing
    # right after the style-marker match itself whenever the chunk-level
    # anchor still contains the matched keyword.
    if matched_keyword and re.search(r"\b" + re.escape(matched_keyword) + r"\b", style_anchor.lower()):
        style_match = _STYLE_MARKER_RE.search(original_prompt)
        style_anchor = original_prompt[:style_match.end()].strip(" ,") if style_match else ""
    lighting_match = _MATERIAL_CONTEXT_LIGHTING_RE.search(original_prompt)
    lighting = f", {lighting_match.group(0)}" if lighting_match else ""
    env_phrase = _ENVIRONMENT_PHRASES.get(str(beat.get("environment", "") or "").lower(), "")
    environment_clause = f", {env_phrase}" if env_phrase else ""
    era_matches = dict.fromkeys(m.lower() for m in _ERA_DESCRIPTOR_RE.findall(original_prompt))
    era_clause = f", {', '.join(era_matches)}" if era_matches else ""
    body = f"{reframe}{lighting}{environment_clause}{era_clause}"
    rewritten = f"{style_anchor}, {body}" if style_anchor else body
    return preserve_character_descriptor_lines(beat, rewritten)


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
    if is_text_prop_beat(beat):
        intent = derive_text_prop_prompt(beat).rstrip(". ")
    environment = str(beat.get("environment") or "other")
    base = intent if len(intent.split()) >= 4 else _ENV_SAFE_PROMPTS.get(
        environment, _ENV_SAFE_PROMPTS["other"],
    )
    rewritten = (
        f"{base}, {composition_slot_variation(idx)}, "
        f"{_WELL_LIT_REROLL_CLAUSE}, {_FULL_BLEED_REROLL_CLAUSE}."
    )
    return preserve_character_descriptor_lines(beat, rewritten)


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
