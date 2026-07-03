"""Cinematic Flux prompt construction from Agent 4 visual bible data."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

_DEFAULT_NEGATIVES = (
    "text",
    "watermark",
    "logo",
    "subtitles",
    "captions",
    "UI",
    "extra fingers",
    "distorted hands",
    "distorted face",
    "duplicate limbs",
    "deformed anatomy",
    "low quality",
    "blurry",
    "overexposed",
    "underexposed",
    "cartoonish unless configured",
    "random character changes",
)
_REQUIRED_METADATA_FIELDS = (
    "shot_type",
    "subject",
    "action",
    "emotion",
    "environment",
    "camera",
    "lighting",
    "composition",
)
_GENERIC_TERMS = ("scary", "creepy", "spooky", "horror scene", "dark room", "hallway at night")


@dataclass(frozen=True)
class PromptQualityIssue:
    severity: str
    code: str
    message: str


def build_cinematic_flux_prompt(
    *,
    beat: dict,
    narration_excerpt: str,
    visual_bible: dict | None,
    channel_config_context: dict | None,
    content_kind: str,
    beat_index: int,
    total_beats: int,
) -> dict:
    """Return cinematic prompt data for one visual beat without side effects."""
    bible = visual_bible or {}
    config = channel_config_context or _dict_or_empty(bible.get("config_context"))
    global_style = _dict_or_empty(bible.get("global_style"))
    characters = [c for c in bible.get("characters", []) if isinstance(c, dict)]
    locations = [loc for loc in bible.get("locations", []) if isinstance(loc, dict)]
    motifs = [motif for motif in bible.get("recurring_motifs", []) if isinstance(motif, dict)]

    text_context = " ".join([
        str(narration_excerpt or ""),
        str(beat.get("script_text") or ""),
        str(beat.get("visual_intent") or ""),
        str(beat.get("flux_prompt") or ""),
        str(beat.get("character") or ""),
        str(beat.get("location") or ""),
        str(beat.get("environment") or ""),
        str(beat.get("motif") or ""),
    ]).lower()

    character = _match_by_text(characters, text_context, ("name", "role", "continuity_tags"))
    location = _match_by_text(locations, text_context, ("name", "description", "continuity_tags"))
    motif = _match_by_text(motifs, text_context, ("name", "visual_description", "symbolic_role"))

    is_first_15 = int(beat.get("audio_start_ms") or 0) < 15000
    shot_type = _shot_type(beat, beat_index, is_first_15)
    subject = _subject(beat, character, narration_excerpt)
    action = _short_phrase(beat.get("visual_intent") or narration_excerpt or beat.get("script_text") or "tense story moment", 150)
    emotion = _emotion(beat, narration_excerpt)
    environment = _environment(beat, location)
    camera = _join_unique([
        shot_type,
        _first(global_style.get("lens_style"), "35mm natural perspective"),
        *_list(global_style.get("camera_language"))[:2],
    ])
    lighting = _join_unique([
        _first(location.get("lighting") if location else None, ""),
        *_list(global_style.get("lighting_rules"))[:2],
        _first(config.get("video_color_grade"), global_style.get("color_grade"), beat.get("color_grade"), "muted cinematic color grade"),
    ])
    composition = _join_unique([
        *_list(global_style.get("composition_rules"))[:2],
        "clear subject silhouette",
        "image-generation-ready realistic composition",
    ])
    continuity_tags = _continuity_tags(character, location)
    visual_bible_refs = _visual_bible_refs(character, location, motif)

    first_15_guidance = ""
    if is_first_15:
        first_15_guidance = " First 15 seconds: " + _join_unique(_list(bible.get("first_15_seconds_rules"))[:3])

    style_bits = _join_unique([
        _first(global_style.get("realism_level"), "photorealistic"),
        _first(config.get("visual_style"), global_style.get("visual_style")),
        _first(config.get("image_style"), global_style.get("image_style")),
    ])
    character_detail = _character_detail(character)
    location_detail = _location_detail(location)
    motif_detail = _motif_detail(motif)
    negative_prompt = build_negative_prompt(visual_bible=bible)

    prompt_parts = [
        f"Cinematic {shot_type} of {subject}",
        f"action: {action}",
        f"emotion: {emotion}",
        f"environment: {environment}",
        character_detail,
        location_detail,
        motif_detail,
        f"camera/lens/framing: {camera}",
        f"lighting/color grade: {lighting}",
        f"composition: {composition}",
        f"style: {style_bits}",
    ]
    if continuity_tags:
        prompt_parts.append("continuity tags: " + ", ".join(continuity_tags))
    if first_15_guidance:
        prompt_parts.append(first_15_guidance.strip())

    flux_prompt = ". ".join(part.strip(" .") for part in prompt_parts if part).strip() + "."
    flux_prompt += " Avoid: " + negative_prompt + "."

    metadata = {
        "shot_type": shot_type,
        "subject": subject,
        "action": action,
        "emotion": emotion,
        "environment": environment,
        "camera": camera,
        "lighting": lighting,
        "composition": composition,
        "continuity_tags": continuity_tags,
        "visual_bible_refs": visual_bible_refs,
        "is_first_15_seconds": is_first_15,
        "character": character.get("name", "") if character else str(beat.get("character") or ""),
        "location": location.get("name", "") if location else str(beat.get("location") or ""),
    }
    prompt_data = {
        "flux_prompt": flux_prompt,
        "negative_prompt": negative_prompt,
        "prompt_metadata": metadata,
    }
    prompt_data["prompt_quality_warnings"] = [issue.__dict__ for issue in inspect_flux_prompt_quality(prompt_data)]
    return prompt_data


def build_negative_prompt(*, visual_bible: dict | None) -> str:
    bible = visual_bible or {}
    rules = _list(bible.get("negative_prompt_rules"))
    forbidden = _list(bible.get("forbidden_generic_shots"))
    return ", ".join(_dedupe([*rules, *_DEFAULT_NEGATIVES, *forbidden]))


def inspect_flux_prompt_quality(prompt_data: dict) -> list[PromptQualityIssue]:
    prompt = str(prompt_data.get("flux_prompt") or "")
    metadata = _dict_or_empty(prompt_data.get("prompt_metadata"))
    negative_prompt = str(prompt_data.get("negative_prompt") or "")
    issues: list[PromptQualityIssue] = []
    if len(prompt.split()) < 35:
        issues.append(PromptQualityIssue("WARNING", "prompt_too_short", "Flux prompt has fewer than 35 words."))
    for field in _REQUIRED_METADATA_FIELDS:
        if not str(metadata.get(field) or "").strip():
            issues.append(PromptQualityIssue("WARNING", f"missing_{field}", f"Prompt metadata missing {field}."))
    if not metadata.get("continuity_tags"):
        issues.append(PromptQualityIssue("WARNING", "missing_continuity_tags", "No continuity tags included."))
    if not negative_prompt.strip():
        issues.append(PromptQualityIssue("WARNING", "missing_negative_prompt", "No negative prompt supplied."))
    lowered = prompt.lower()
    if any(term in lowered for term in _GENERIC_TERMS) and len(prompt.split()) < 60:
        issues.append(PromptQualityIssue("WARNING", "generic_horror_prompt", "Prompt may still be generic horror shorthand."))
    if metadata.get("is_first_15_seconds") is None:
        issues.append(PromptQualityIssue("WARNING", "first_15_seconds_not_marked", "First-15-seconds flag absent."))
    return issues


def apply_cinematic_prompts_to_beats(
    beats: list[dict],
    *,
    visual_bible: dict | None,
    content_kind: str,
) -> list[dict]:
    total = len(beats)
    config = _dict_or_empty((visual_bible or {}).get("config_context"))
    enriched: list[dict] = []
    for index, beat in enumerate(beats):
        new_beat = dict(beat)
        prompt_data = build_cinematic_flux_prompt(
            beat=new_beat,
            narration_excerpt=str(new_beat.get("script_text") or ""),
            visual_bible=visual_bible,
            channel_config_context=config,
            content_kind=content_kind,
            beat_index=index,
            total_beats=total,
        )
        new_beat["flux_prompt"] = prompt_data["flux_prompt"]
        new_beat["negative_prompt"] = prompt_data["negative_prompt"]
        metadata = prompt_data["prompt_metadata"]
        for key, value in metadata.items():
            new_beat[key] = value
        new_beat["prompt_quality_warnings"] = prompt_data["prompt_quality_warnings"]
        enriched.append(new_beat)
    return enriched


def _match_by_text(items: list[dict], text_context: str, keys: tuple[str, ...]) -> dict:
    for item in items:
        candidates: list[str] = []
        for key in keys:
            value = item.get(key)
            if isinstance(value, list):
                candidates.extend(str(v).lower() for v in value)
            elif value:
                candidates.append(str(value).lower())
        if any(candidate and candidate in text_context for candidate in candidates):
            return item
    return {}


def _shot_type(beat: dict, beat_index: int, is_first_15: bool) -> str:
    visual_type = str(beat.get("visual_type") or "").replace("_", " ")
    category = str(beat.get("visual_category") or "")
    if beat.get("shot_type"):
        return str(beat["shot_type"])
    if is_first_15 or beat_index == 0:
        return "high-tension establishing close-up"
    if category == "person":
        return "medium close-up"
    if visual_type and visual_type not in {"b-roll", "generated visual"}:
        return visual_type
    return "cinematic medium shot"


def _subject(beat: dict, character: dict, narration_excerpt: str) -> str:
    if character:
        clothing = _first(character.get("clothing"), "")
        appearance = _first(character.get("appearance"), "")
        return _join_unique(["the same " + str(character.get("name", "character")), appearance, clothing])
    if beat.get("subject"):
        return str(beat["subject"])
    return _short_phrase(beat.get("visual_intent") or narration_excerpt or "the story subject", 100)


def _emotion(beat: dict, narration_excerpt: str) -> str:
    text = " ".join([str(beat.get("visual_intent") or ""), narration_excerpt]).lower()
    for word in ("dread", "fear", "confusion", "grief", "anger", "shock", "relief", "suspicion"):
        if word in text:
            return word
    intensity = str(beat.get("beat_intensity") or "medium")
    return "controlled dread" if intensity == "high" else "tense uncertainty"


def _environment(beat: dict, location: dict) -> str:
    if location:
        return _join_unique([location.get("name", ""), location.get("description", ""), location.get("time_of_day", "")])
    return _first(beat.get("location"), beat.get("environment"), "story-specific environment")


def _character_detail(character: dict) -> str:
    if not character:
        return ""
    return _join_unique(["character continuity", character.get("appearance"), character.get("clothing"), character.get("body_language")])


def _location_detail(location: dict) -> str:
    if not location:
        return ""
    details = location.get("recurring_details") if isinstance(location.get("recurring_details"), list) else []
    return _join_unique(["location continuity", location.get("description"), location.get("color_palette"), *details[:3]])


def _motif_detail(motif: dict) -> str:
    if not motif:
        return ""
    return _join_unique(["recurring motif", motif.get("name"), motif.get("visual_description"), motif.get("usage_rule")])


def _visual_bible_refs(character: dict, location: dict, motif: dict) -> list[str]:
    refs: list[str] = []
    if character.get("name"):
        refs.append(f"character:{character['name']}")
    if location.get("name"):
        refs.append(f"location:{location['name']}")
    if motif.get("name"):
        refs.append(f"motif:{motif['name']}")
    return refs


def _continuity_tags(character: dict, location: dict) -> list[str]:
    return _dedupe([*_list(character.get("continuity_tags")), *_list(location.get("continuity_tags"))])


def _dict_or_empty(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value if v not in (None, "")]
    if value not in (None, ""):
        return [str(value)]
    return []


def _first(*values: Any) -> str:
    for value in values:
        if value not in (None, ""):
            return str(value)
    return ""


def _join_unique(values: list[Any]) -> str:
    return ", ".join(_dedupe([str(value).strip() for value in values if str(value or "").strip()]))


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        key = value.lower()
        if key and key not in seen:
            seen.add(key)
            result.append(value)
    return result


def _short_phrase(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"
