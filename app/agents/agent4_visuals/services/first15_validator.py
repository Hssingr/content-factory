"""Deterministic first-15-seconds visual hook validation for Agent 4."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

_PASS = "PASS"
_PASS_WARNINGS = "PASS_WITH_WARNINGS"
_FAIL = "FAIL_BLOCKING"
_FIRST15_MS = 15000

_GENERIC_OPENINGS = (
    "generic dark street",
    "dark street",
    "generic forest",
    "empty forest",
    "generic house",
    "house exterior",
    "generic empty room",
    "empty room",
    "empty hallway",
    "hallway with no subject",
    "scary place at night",
    "dark room",
    "low-detail landscape",
    "boring establishing shot",
)
_ABSTRACT_TERMS = (
    "abstract",
    "symbol",
    "symbolic",
    "floating skull",
    "floating icon",
    "shadow shape",
    "ominous shape",
)
_HUMAN_TERMS = (
    "face",
    "eyes",
    "hand",
    "person",
    "girl",
    "boy",
    "man",
    "woman",
    "child",
    "mother",
    "father",
    "teen",
    "character",
    "silhouette",
)
_OBJECT_HOOK_TERMS = (
    "clue",
    "object",
    "knife",
    "key",
    "photo",
    "photograph",
    "portrait",
    "tape",
    "letter",
    "door",
    "closet",
    "footprint",
    "muddy footprints",
    "plate",
    "chair",
    "blood",
    "scratch",
    "scratched",
    "mirror",
    "toy",
)
_ACTION_TERMS = (
    "reaches",
    "opens",
    "moves",
    "turns",
    "stares",
    "whispers",
    "reveals",
    "discovers",
    "pulls",
    "scratches",
    "bleeds",
    "shakes",
    "rattles",
    "stands",
    "freezes",
    "leading",
)
_EMOTION_TERMS = (
    "fear",
    "dread",
    "suspicion",
    "panic",
    "shock",
    "terror",
    "unease",
    "unsettling",
    "afraid",
    "frightened",
    "horror",
    "curiosity",
    "mystery",
    "mysterious",
)
_SPECIFIC_DETAIL_TERMS = (
    "muddy",
    "scratched",
    "untouched",
    "pulled back",
    "half-open",
    "family portrait",
    "closet",
    "three",
    "one chair",
    "peeling",
    "broken",
    "wet",
    "numbered",
    "same face",
)


@dataclass(frozen=True)
class First15Issue:
    severity: str
    code: str
    beat_index: int | None
    section_order: int | None
    message: str
    evidence: dict | None = None


@dataclass(frozen=True)
class First15ValidationResult:
    status: str
    issues: list[First15Issue]
    checked_count: int
    strong_hook_count: int
    weak_generic_count: int
    first_15_duration_ms: int


def validate_first_15_seconds(
    beats_or_sections: list[dict],
    *,
    visual_bible: dict | None = None,
    content_kind: str = "parent",
) -> First15ValidationResult:
    """Validate only beats that start before 15 seconds using deterministic checks."""
    normalized = [_normalize_beat(beat, index) for index, beat in enumerate(beats_or_sections)]
    first15 = [beat for beat in normalized if _is_first15(beat)]
    if not beats_or_sections:
        return First15ValidationResult(
            _FAIL,
            [First15Issue("BLOCKING", "first_15_missing", None, None, "No beats were available for first-15-seconds validation.")],
            0,
            0,
            0,
            0,
        )
    if not first15:
        return First15ValidationResult(
            _FAIL,
            [First15Issue("BLOCKING", "first_15_no_visual_sections", None, None, "No visual beat starts inside the first 15 seconds.")],
            0,
            0,
            0,
            0,
        )

    forbidden = _lower_join(_list((visual_bible or {}).get("forbidden_generic_shots")))
    issues: list[First15Issue] = []
    strong_count = 0
    generic_count = 0
    abstract_count = 0
    no_subject_action_count = 0
    first15_duration = 0

    for beat in first15:
        text = _beat_text(beat)
        section_order = _section_order(beat)
        beat_index = int(beat.get("_beat_index", 0))
        first15_duration = max(first15_duration, min(_int(beat.get("audio_end_ms")), _FIRST15_MS))

        generic = _is_generic(text, forbidden)
        abstract = _is_abstract(text)
        has_focus = _has_human_focus(text) or _has_object_hook(text) or bool(str(beat.get("subject") or "").strip())
        has_action = _has_action(text) or bool(str(beat.get("action") or "").strip())
        has_emotion = _has_emotion(text) or bool(str(beat.get("emotion") or "").strip())
        specific_location = _has_specific_location_detail(text)
        strong = _is_strong_hook(text, beat)

        if strong:
            strong_count += 1
        if generic:
            generic_count += 1
            issues.append(First15Issue("WARNING", "generic_opening_shot", beat_index, section_order, "Opening beat may be a generic low-curiosity horror shot.", {"text_excerpt": text[:180]}))
        if abstract:
            abstract_count += 1
            issues.append(First15Issue("WARNING", "weak_horror_curiosity", beat_index, section_order, "Opening beat relies on abstract/symbolic imagery without clear story tension.", {"text_excerpt": text[:180]}))
        if not has_focus:
            issues.append(First15Issue("WARNING", "missing_human_or_object_focus", beat_index, section_order, "Opening beat lacks a clear human face/body or mysterious object focus."))
        if not has_emotion:
            issues.append(First15Issue("WARNING", "missing_emotion", beat_index, section_order, "Opening beat does not make fear, suspicion, dread, or curiosity visually readable."))
        if not specific_location:
            issues.append(First15Issue("WARNING", "missing_specific_location_detail", beat_index, section_order, "Opening location lacks specific narrative detail."))
        if _is_empty_room(text) and not _empty_room_is_specific(text):
            issues.append(First15Issue("WARNING", "empty_room_without_specific_detail", beat_index, section_order, "Empty-room opening lacks a concrete clue or narrative detail."))
        if not bool(beat.get("is_first_15_seconds")):
            issues.append(First15Issue("WARNING", "missing_first15_metadata_flag", beat_index, section_order, "Opening beat is not marked with is_first_15_seconds metadata."))
        if len(str(beat.get("flux_prompt") or "").split()) < 35:
            issues.append(First15Issue("WARNING", "opening_prompt_too_short", beat_index, section_order, "Opening prompt has fewer than 35 words."))
        if not has_focus and not has_action:
            no_subject_action_count += 1

    blocking: list[First15Issue] = []
    if generic_count == len(first15) and strong_count == 0:
        blocking.append(First15Issue("BLOCKING", "first_15_all_generic", None, None, "All first-15-second beats are generic opening shots."))
    if no_subject_action_count == len(first15) and strong_count == 0:
        blocking.append(First15Issue("BLOCKING", "first_15_no_subject_or_action", None, None, "First-15-second beats lack a usable subject/object and action."))
    if abstract_count == len(first15) and strong_count == 0:
        blocking.append(First15Issue("BLOCKING", "first_15_only_abstract", None, None, "First-15-second beats are only abstract/symbolic imagery."))

    if content_kind == "child_short" and strong_count == 0:
        issues.append(First15Issue("WARNING", "weak_horror_curiosity", None, None, "Child short opening has no strong hook; shorts should open with sharper curiosity."))

    all_issues = [*blocking, *issues]
    if blocking:
        status = _FAIL
    elif issues:
        status = _PASS_WARNINGS
    else:
        status = _PASS
    return First15ValidationResult(status, all_issues, len(first15), strong_count, generic_count, first15_duration)


def enhance_first15_prompt_data(
    prompt_data: dict,
    *,
    visual_bible: dict | None,
    beat_index: int,
) -> dict:
    """Return strengthened first-15 prompt/metadata without external calls."""
    data = dict(prompt_data or {})
    if not _is_first15(data):
        return data

    bible = visual_bible or {}
    metadata = dict(data.get("prompt_metadata") if isinstance(data.get("prompt_metadata"), dict) else {})
    for key in ("subject", "action", "emotion", "environment", "camera", "lighting", "composition"):
        if data.get(key) not in (None, "") and metadata.get(key) in (None, ""):
            metadata[key] = data[key]

    before = json.dumps(data, sort_keys=True, default=str)
    metadata["is_first_15_seconds"] = True
    data["is_first_15_seconds"] = True
    tags = _strength_tags(data, visual_bible=bible)
    if tags:
        data["first15_strength_tags"] = tags
        metadata["first15_strength_tags"] = tags

    if not str(metadata.get("subject") or data.get("subject") or "").strip():
        metadata["subject"] = "a specific unsettling clue tied to the narration"
        data["subject"] = metadata["subject"]
    if not str(metadata.get("action") or data.get("action") or "").strip():
        metadata["action"] = "reveals an immediate visual question"
        data["action"] = metadata["action"]
    if not str(metadata.get("emotion") or data.get("emotion") or "").strip():
        metadata["emotion"] = "readable dread and suspicion"
        data["emotion"] = metadata["emotion"]

    first15_rules = _list(bible.get("first_15_seconds_rules"))
    forbidden = _list(bible.get("forbidden_generic_shots"))
    global_style = bible.get("global_style") if isinstance(bible.get("global_style"), dict) else {}
    config = bible.get("config_context") if isinstance(bible.get("config_context"), dict) else {}
    guidance_parts = [
        "First-15 visual hook: create immediate curiosity, danger, or mystery",
        *_list(first15_rules)[:3],
        _first(config.get("visual_style"), global_style.get("visual_style")),
        _first(config.get("image_style"), global_style.get("image_style")),
        _first(config.get("video_color_grade"), global_style.get("color_grade")),
    ]
    guidance = _join_unique(guidance_parts)
    prompt = str(data.get("flux_prompt") or data.get("prompt") or "").strip()
    if guidance and "First-15 visual hook:" not in prompt:
        prompt = _insert_before_avoid(prompt, guidance + ".")
        data["flux_prompt"] = prompt

    negative_prompt = str(data.get("negative_prompt") or "")
    for rule in [*_list(bible.get("negative_prompt_rules")), *forbidden]:
        if rule and rule.lower() not in negative_prompt.lower():
            negative_prompt = (negative_prompt + ", " + rule).strip(" ,")
    if negative_prompt:
        data["negative_prompt"] = negative_prompt

    data["prompt_metadata"] = metadata
    data["first15_enhanced"] = json.dumps(data, sort_keys=True, default=str) != before
    if data["first15_enhanced"] and "flux_prompt" in data:
        data["prompt_quality_warnings"] = _append_warning(
            data.get("prompt_quality_warnings"),
            "first15_prompt_enhanced",
            "First-15-seconds opening prompt was deterministically strengthened.",
        )
    return data


def apply_first15_enhancement_and_validation(
    beats: list[dict],
    *,
    visual_bible: dict | None,
    content_kind: str,
) -> tuple[list[dict], First15ValidationResult]:
    """Enhance first-15 beats, validate them, and attach persistable metadata."""
    enhanced = [
        enhance_first15_prompt_data(dict(beat), visual_bible=visual_bible, beat_index=index)
        for index, beat in enumerate(beats)
    ]
    result = validate_first_15_seconds(enhanced, visual_bible=visual_bible, content_kind=content_kind)
    by_index: dict[int, list[dict[str, Any]]] = {}
    for issue in result.issues:
        if issue.beat_index is not None:
            by_index.setdefault(issue.beat_index, []).append(_issue_dict(issue))
    result_issues = [_issue_dict(issue) for issue in result.issues]
    for index, beat in enumerate(enhanced):
        if _is_first15(beat):
            beat["is_first_15_seconds"] = True
            beat["first15_validation_status"] = result.status
            beat["first15_issues"] = by_index.get(index, [])
            beat.setdefault("first15_strength_tags", _strength_tags(beat, visual_bible=visual_bible))
            beat.setdefault("first15_enhanced", False)
        elif result.status == _FAIL:
            beat["first15_validation_status"] = result.status
        beat["first15_validation_summary"] = {
            "status": result.status,
            "checked_count": result.checked_count,
            "strong_hook_count": result.strong_hook_count,
            "weak_generic_count": result.weak_generic_count,
            "first_15_duration_ms": result.first_15_duration_ms,
            "issues": result_issues,
        }
    return enhanced, result


def _normalize_beat(beat: dict, index: int) -> dict:
    data = dict(beat or {})
    raw_generation = data.get("generation_prompt")
    if isinstance(raw_generation, str):
        try:
            parsed = json.loads(raw_generation)
        except (TypeError, json.JSONDecodeError):
            parsed = {}
        if isinstance(parsed, dict):
            for key, value in parsed.items():
                data.setdefault(key, value)
    metadata = data.get("prompt_metadata") if isinstance(data.get("prompt_metadata"), dict) else {}
    for key, value in metadata.items():
        data.setdefault(key, value)
    data["_beat_index"] = index
    return data


def _is_first15(beat: dict) -> bool:
    return _int(beat.get("audio_start_ms")) < _FIRST15_MS


def _beat_text(beat: dict) -> str:
    fields = (
        "script_text",
        "flux_prompt",
        "visual_intent",
        "generation_prompt",
        "visual_category",
        "shot_type",
        "subject",
        "action",
        "emotion",
        "environment",
        "camera",
        "lighting",
        "composition",
        "location",
        "character",
    )
    parts = [str(beat.get(field) or "") for field in fields]
    tags = beat.get("continuity_tags")
    if isinstance(tags, list):
        parts.extend(str(tag) for tag in tags)
    return " ".join(" ".join(parts).lower().split())


def _is_strong_hook(text: str, beat: dict) -> bool:
    focus = _has_human_focus(text) or _has_object_hook(text) or bool(str(beat.get("subject") or "").strip())
    tension = _has_emotion(text) or _has_action(text) or any(word in text for word in ("danger", "clue", "mystery", "question"))
    specific = _has_specific_location_detail(text) or len(text.split()) >= 55
    if _is_empty_room(text) and _empty_room_is_specific(text):
        return True
    return focus and tension and specific and not (_is_generic(text, "") and not _has_specific_location_detail(text))


def _strength_tags(beat: dict, *, visual_bible: dict | None) -> list[str]:
    text = _beat_text(_normalize_beat(beat, 0))
    tags: list[str] = []
    if _has_human_focus(text):
        tags.append("human_focus")
    if _has_object_hook(text):
        tags.append("mysterious_object")
    if _has_action(text):
        tags.append("specific_action")
    if _has_emotion(text):
        tags.append("readable_emotion")
    if _has_specific_location_detail(text):
        tags.append("specific_location_detail")
    if any(str(rule).lower() in text for rule in _list((visual_bible or {}).get("first_15_seconds_rules"))):
        tags.append("visual_bible_first15_rule")
    return _dedupe(tags)


def _issue_dict(issue: First15Issue) -> dict[str, Any]:
    return {
        "severity": issue.severity,
        "code": issue.code,
        "beat_index": issue.beat_index,
        "section_order": issue.section_order,
        "message": issue.message,
        "evidence": issue.evidence or {},
    }


def _append_warning(existing: Any, code: str, message: str) -> list:
    warnings = list(existing) if isinstance(existing, list) else []
    if not any(isinstance(item, dict) and item.get("code") == code for item in warnings):
        warnings.append({"severity": "WARNING", "code": code, "message": message})
    return warnings


def _is_generic(text: str, forbidden: str) -> bool:
    return any(term in text for term in _GENERIC_OPENINGS) or any(term and term in text for term in forbidden.split(" | "))


def _is_abstract(text: str) -> bool:
    return any(term in text for term in _ABSTRACT_TERMS) and not (_has_human_focus(text) or _has_object_hook(text))


def _has_human_focus(text: str) -> bool:
    return any(term in text for term in _HUMAN_TERMS)


def _has_object_hook(text: str) -> bool:
    return any(term in text for term in _OBJECT_HOOK_TERMS)


def _has_action(text: str) -> bool:
    return any(term in text for term in _ACTION_TERMS)


def _has_emotion(text: str) -> bool:
    return any(term in text for term in _EMOTION_TERMS)


def _has_specific_location_detail(text: str) -> bool:
    return any(term in text for term in _SPECIFIC_DETAIL_TERMS) or len(set(text.split())) >= 40


def _is_empty_room(text: str) -> bool:
    return "empty room" in text or "empty bedroom" in text or "empty kitchen" in text or "empty hallway" in text


def _empty_room_is_specific(text: str) -> bool:
    if "generic empty room" in text or "generic empty hallway" in text:
        return False
    return _is_empty_room(text) and (_has_object_hook(text) or _has_specific_location_detail(text) or _has_action(text))


def _insert_before_avoid(prompt: str, addition: str) -> str:
    if not prompt:
        return addition
    marker = " Avoid:"
    if marker in prompt:
        before, after = prompt.split(marker, 1)
        return before.rstrip(" .") + ". " + addition.strip() + marker + after
    return prompt.rstrip(" .") + ". " + addition.strip()


def _section_order(beat: dict) -> int | None:
    value = beat.get("section_order", beat.get("beat_order"))
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


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


def _lower_join(values: list[str]) -> str:
    return " | ".join(str(value).lower() for value in values if value)
