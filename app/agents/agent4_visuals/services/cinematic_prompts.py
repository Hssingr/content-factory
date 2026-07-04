"""Additive visual-bible continuity enrichment for Flux prompts.

The storyboard model owns the actual image prompt. This module only supplies a
compact visual-bible context for storyboard generation and appends matched
character/location continuity clauses after validation. It must never replace a
Claude-authored ``flux_prompt`` with a template or add negative-prompt text to
the positive prompt sent to Flux.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

_REQUIRED_METADATA_FIELDS = (
    "base_prompt_preserved",
    "continuity_tags",
    "visual_bible_refs",
)
_GENERIC_TERMS = ("scary", "creepy", "spooky", "horror scene", "dark room", "hallway at night")
_MAX_COMPACT_ITEMS = 6
_MAX_COMPACT_LIST_ITEMS = 4


@dataclass(frozen=True)
class PromptQualityIssue:
    severity: str
    code: str
    message: str


def compact_visual_bible_for_storyboard(visual_bible: dict | None) -> dict:
    """Return a compact JSON-safe bible subset for ``generate_storyboard_batch``.

    The subset gives Claude stable continuity inputs before it writes each
    ``flux_prompt``. It intentionally omits global ``camera_language`` and
    ``lighting_rules`` arrays: Phase 2.1 moves style guidance upstream, but does
    not permit verbatim camera/lighting-rule injection into final prompts.
    """
    bible = visual_bible or {}
    if not isinstance(bible, dict) or not bible:
        return {}

    global_style = _dict_or_empty(bible.get("global_style"))
    config = _dict_or_empty(bible.get("config_context"))
    compact: dict[str, Any] = {}

    summary = str(bible.get("story_visual_summary") or "").strip()
    if summary:
        compact["story_visual_summary"] = summary

    style = {
        key: value
        for key, value in {
            "visual_style": _first(config.get("visual_style"), global_style.get("visual_style")),
            "image_style": _first(config.get("image_style"), global_style.get("image_style")),
            "realism_level": global_style.get("realism_level"),
            "color_grade": _first(config.get("video_color_grade"), global_style.get("color_grade")),
            "composition_rules": _compact_list(global_style.get("composition_rules")),
        }.items()
        if value
    }
    if style:
        compact["style"] = style

    characters = [_compact_character(item) for item in _dict_items(bible.get("characters"))]
    characters = [item for item in characters if item]
    if characters:
        compact["characters"] = characters[:_MAX_COMPACT_ITEMS]

    locations = [_compact_location(item) for item in _dict_items(bible.get("locations"))]
    locations = [item for item in locations if item]
    if locations:
        compact["locations"] = locations[:_MAX_COMPACT_ITEMS]

    motifs = [_compact_motif(item) for item in _dict_items(bible.get("recurring_motifs"))]
    motifs = [item for item in motifs if item]
    if motifs:
        compact["recurring_motifs"] = motifs[:_MAX_COMPACT_ITEMS]

    first15 = _compact_list(bible.get("first_15_seconds_rules"))
    if first15:
        compact["first_15_seconds_rules"] = first15

    forbidden = _compact_list(bible.get("forbidden_generic_shots"))
    if forbidden:
        compact["forbidden_generic_shots"] = forbidden

    return compact


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
    """Return additive continuity data for one beat without side effects."""
    del channel_config_context, content_kind, total_beats

    bible = visual_bible or {}
    characters = [c for c in bible.get("characters", []) if isinstance(c, dict)]
    locations = [loc for loc in bible.get("locations", []) if isinstance(loc, dict)]

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

    base_prompt = " ".join(str(beat.get("flux_prompt") or "").split()).strip()
    continuity_clauses = _continuity_clauses(character, location)
    flux_prompt = _append_continuity_clauses(base_prompt, continuity_clauses)

    metadata = {
        "base_prompt_preserved": True,
        "continuity_tags": _continuity_tags(character, location),
        "visual_bible_refs": _visual_bible_refs(character, location),
        "is_first_15_seconds": int(beat.get("audio_start_ms") or 0) < 15000,
        "character": character.get("name", "") if character else str(beat.get("character") or ""),
        "location": location.get("name", "") if location else str(beat.get("location") or ""),
        "continuity_clause_count": len(continuity_clauses),
        "beat_index": beat_index,
    }
    prompt_data = {
        "flux_prompt": flux_prompt,
        "prompt_metadata": metadata,
        "continuity_clauses": continuity_clauses,
    }
    prompt_data["prompt_quality_warnings"] = [issue.__dict__ for issue in inspect_flux_prompt_quality(prompt_data)]
    return prompt_data


def inspect_flux_prompt_quality(prompt_data: dict) -> list[PromptQualityIssue]:
    prompt = str(prompt_data.get("flux_prompt") or "")
    metadata = _dict_or_empty(prompt_data.get("prompt_metadata"))
    issues: list[PromptQualityIssue] = []
    if len(prompt.split()) < 20:
        issues.append(PromptQualityIssue("WARNING", "prompt_too_short", "Flux prompt has fewer than 20 words."))
    for field in _REQUIRED_METADATA_FIELDS:
        if field not in metadata:
            issues.append(PromptQualityIssue("WARNING", f"missing_{field}", f"Prompt metadata missing {field}."))
    lowered = prompt.lower()
    if "avoid:" in lowered:
        issues.append(PromptQualityIssue("WARNING", "positive_prompt_contains_avoid", "Flux prompt contains an Avoid clause."))
    if prompt.lstrip().lower().startswith("cinematic"):
        issues.append(PromptQualityIssue("WARNING", "prompt_starts_with_cinematic", "Flux prompt starts with forbidden mood shorthand."))
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
    enriched: list[dict] = []
    for index, beat in enumerate(beats):
        new_beat = dict(beat)
        prompt_data = build_cinematic_flux_prompt(
            beat=new_beat,
            narration_excerpt=str(new_beat.get("script_text") or ""),
            visual_bible=visual_bible,
            channel_config_context=None,
            content_kind=content_kind,
            beat_index=index,
            total_beats=total,
        )
        new_beat["flux_prompt"] = prompt_data["flux_prompt"]
        metadata = prompt_data["prompt_metadata"]
        for key in (
            "continuity_tags",
            "visual_bible_refs",
            "is_first_15_seconds",
            "character",
            "location",
            "continuity_clause_count",
            "base_prompt_preserved",
        ):
            new_beat[key] = metadata.get(key)
        new_beat["prompt_quality_warnings"] = prompt_data["prompt_quality_warnings"]
        enriched.append(new_beat)
    return enriched


def _continuity_clauses(character: dict, location: dict) -> list[str]:
    clauses: list[str] = []
    character_detail = _character_detail(character)
    if character_detail:
        clauses.append("Character continuity: " + character_detail)
    location_detail = _location_detail(location)
    if location_detail:
        clauses.append("Location continuity: " + location_detail)
    return clauses


def _append_continuity_clauses(base_prompt: str, clauses: list[str]) -> str:
    prompt = base_prompt
    lowered = prompt.lower()
    for clause in clauses:
        if clause.lower() not in lowered:
            separator = " " if prompt.endswith(".") or not prompt else ". "
            prompt = f"{prompt}{separator}{clause}." if prompt else f"{clause}."
            lowered = prompt.lower()
    return prompt


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


def _character_detail(character: dict) -> str:
    if not character:
        return ""
    return _join_unique([
        _first(character.get("name"), "same character"),
        character.get("appearance"),
        character.get("clothing"),
        character.get("body_language"),
    ])


def _location_detail(location: dict) -> str:
    if not location:
        return ""
    details = location.get("recurring_details") if isinstance(location.get("recurring_details"), list) else []
    return _join_unique([
        _first(location.get("name"), "same location"),
        location.get("description"),
        location.get("time_of_day"),
        location.get("color_palette"),
        *details[:3],
    ])


def _visual_bible_refs(character: dict, location: dict) -> list[str]:
    refs: list[str] = []
    if character.get("name"):
        refs.append(f"character:{character['name']}")
    if location.get("name"):
        refs.append(f"location:{location['name']}")
    return refs


def _continuity_tags(character: dict, location: dict) -> list[str]:
    return _dedupe([*_list(character.get("continuity_tags")), *_list(location.get("continuity_tags"))])


def _compact_character(item: dict) -> dict:
    return _drop_empty({
        "name": item.get("name"),
        "role": item.get("role"),
        "appearance": item.get("appearance"),
        "clothing": item.get("clothing"),
        "body_language": item.get("body_language"),
        "continuity_tags": _compact_list(item.get("continuity_tags")),
    })


def _compact_location(item: dict) -> dict:
    return _drop_empty({
        "name": item.get("name"),
        "description": item.get("description"),
        "time_of_day": item.get("time_of_day"),
        "color_palette": item.get("color_palette"),
        "recurring_details": _compact_list(item.get("recurring_details")),
        "continuity_tags": _compact_list(item.get("continuity_tags")),
    })


def _compact_motif(item: dict) -> dict:
    return _drop_empty({
        "name": item.get("name"),
        "visual_description": item.get("visual_description"),
        "symbolic_role": item.get("symbolic_role"),
        "usage_rule": item.get("usage_rule"),
    })


def _dict_items(value: Any) -> list[dict]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _dict_or_empty(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value if v not in (None, "")]
    if value not in (None, ""):
        return [str(value)]
    return []


def _compact_list(value: Any) -> list[str]:
    return _list(value)[:_MAX_COMPACT_LIST_ITEMS]


def _first(*values: Any) -> str:
    for value in values:
        if value not in (None, "", []):
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


def _drop_empty(data: dict) -> dict:
    return {key: value for key, value in data.items() if value not in (None, "", [])}
