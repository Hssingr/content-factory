"""Agent 4 local visual bible generation and validation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from app.models import Channel, ChannelConfig, ChannelLanguage, ChannelPlatform, Content, Script
from app.services.claude_client import call_claude_structured
from app.services.local_run_paths import ensure_run_dirs, get_run_root, get_visuals_dir

VISUAL_BIBLE_VERSION = "1.0"
VISUAL_BIBLE_TASK = "visual_bible_generation"

_REQUIRED_TOP_LEVEL = (
    "version",
    "content_id",
    "generated_at",
    "config_context",
    "story_visual_summary",
    "global_style",
    "characters",
    "locations",
    "recurring_motifs",
    "continuity_rules",
    "negative_prompt_rules",
    "first_15_seconds_rules",
    "forbidden_generic_shots",
)

_VISUAL_BIBLE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": True,
    "properties": {
        "story_visual_summary": {"type": "string"},
        "global_style": {
            "type": "object",
            "additionalProperties": True,
            "properties": {
                "realism_level": {"type": "string"},
                "visual_style": {"type": "string"},
                "image_style": {"type": "string"},
                "color_grade": {"type": "string"},
                "lighting_rules": {"type": "array", "items": {"type": "string"}},
                "camera_language": {"type": "array", "items": {"type": "string"}},
                "lens_style": {"type": "string"},
                "composition_rules": {"type": "array", "items": {"type": "string"}},
            },
            "required": [
                "realism_level",
                "visual_style",
                "image_style",
                "color_grade",
                "lighting_rules",
                "camera_language",
                "lens_style",
                "composition_rules",
            ],
        },
        "characters": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
        "locations": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
        "recurring_motifs": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
        "continuity_rules": {"type": "array", "items": {"type": "string"}},
        "negative_prompt_rules": {"type": "array", "items": {"type": "string"}},
        "first_15_seconds_rules": {"type": "array", "items": {"type": "string"}},
        "forbidden_generic_shots": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "story_visual_summary",
        "global_style",
        "characters",
        "locations",
        "recurring_motifs",
        "continuity_rules",
        "negative_prompt_rules",
        "first_15_seconds_rules",
        "forbidden_generic_shots",
    ],
}


@dataclass(frozen=True)
class VisualBibleIssue:
    severity: str
    code: str
    message: str


def get_visual_bible_path(content_id: int | str | UUID) -> Path:
    return get_visuals_dir(content_id) / "visual_bible.json"


def load_visual_bible_for_content(content_id: int | str | UUID) -> dict | None:
    path = get_visual_bible_path(content_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def generate_visual_bible_for_content(
    content_id: int | str | UUID,
    db: Session,
    *,
    force: bool = False,
) -> dict:
    """Generate, validate, and persist a local visual bible for Agent 4."""
    ensure_run_dirs(content_id)
    content: Content | None = db.get(Content, content_id)
    if content is None:
        raise ValueError(f"Content {content_id} not found for visual bible generation")

    existing = load_visual_bible_for_content(content_id)
    if existing and not force:
        issues = validate_visual_bible(existing)
        if not _blocking(issues):
            return existing

    parent_bible = _load_parent_visual_bible(content, force=force)
    if parent_bible and not force:
        child_bible = _child_copy_from_parent(content, parent_bible)
        _write_visual_bible(content.id, child_bible)
        return child_bible

    channel: Channel | None = db.get(Channel, content.channel_id)
    if channel is None:
        raise ValueError(f"Channel {content.channel_id} not found for content {content_id}")

    channel_config: ChannelConfig | None = db.get(ChannelConfig, channel.id)
    scripts = _load_validated_scripts(content_id, db)
    config_context = _build_config_context(content, channel, channel_config, db)
    prompt_input = _build_prompt_input(
        content=content,
        channel=channel,
        channel_config=channel_config,
        scripts=scripts,
        config_context=config_context,
    )

    bible = _call_and_prepare_bible(content, prompt_input, config_context)
    issues = validate_visual_bible(bible, expected_config_context=config_context)
    if _blocking(issues):
        bible = _call_and_prepare_bible(
            content,
            prompt_input,
            config_context,
            corrective_issues=issues,
        )
        issues = validate_visual_bible(bible, expected_config_context=config_context)
    blocking = _blocking(issues)
    if blocking:
        codes = ", ".join(issue.code for issue in blocking)
        raise ValueError(f"visual_bible invalid after retry: {codes}")

    _write_visual_bible(content.id, bible)
    return bible


def validate_visual_bible(
    bible: dict,
    *,
    expected_config_context: dict[str, Any] | None = None,
) -> list[VisualBibleIssue]:
    issues: list[VisualBibleIssue] = []
    if not isinstance(bible, dict):
        return [VisualBibleIssue("BLOCKING", "bible_not_object", "Visual bible is not a JSON object.")]

    for field in _REQUIRED_TOP_LEVEL:
        if field not in bible:
            issues.append(VisualBibleIssue("BLOCKING", f"missing_{field}", f"Missing required top-level field: {field}."))

    config_context = bible.get("config_context")
    if not isinstance(config_context, dict):
        issues.append(VisualBibleIssue("BLOCKING", "config_context_invalid", "config_context is missing or not an object."))
        config_context = {}

    global_style = bible.get("global_style")
    if not isinstance(global_style, dict):
        issues.append(VisualBibleIssue("BLOCKING", "global_style_invalid", "global_style is missing or not an object."))
        global_style = {}

    _require_list(bible, "characters", issues)
    _require_list(bible, "locations", issues)
    _require_list(bible, "recurring_motifs", issues)
    _require_list(bible, "continuity_rules", issues)
    _require_list(bible, "negative_prompt_rules", issues)
    _require_list(bible, "first_15_seconds_rules", issues)
    _require_list(bible, "forbidden_generic_shots", issues)

    if not str(bible.get("story_visual_summary") or "").strip():
        issues.append(VisualBibleIssue("BLOCKING", "story_visual_summary_empty", "story_visual_summary is empty."))

    style_guidance = [
        global_style.get("visual_style"),
        global_style.get("image_style"),
        global_style.get("color_grade"),
        global_style.get("lens_style"),
        *(global_style.get("lighting_rules") if isinstance(global_style.get("lighting_rules"), list) else []),
        *(global_style.get("camera_language") if isinstance(global_style.get("camera_language"), list) else []),
    ]
    if not any(str(value or "").strip() for value in style_guidance):
        issues.append(VisualBibleIssue("BLOCKING", "style_guidance_missing", "No usable style/camera/lighting guidance found."))

    if isinstance(bible.get("continuity_rules"), list) and not bible["continuity_rules"]:
        issues.append(VisualBibleIssue("WARNING", "continuity_rules_empty", "continuity_rules is empty; downstream prompt builders will rely on available style, character, location, and motif metadata."))

    if expected_config_context:
        for key in ("visual_style", "image_style"):
            expected = str(expected_config_context.get(key) or "").strip()
            actual = str(config_context.get(key) or "").strip()
            if expected and actual != expected:
                issues.append(VisualBibleIssue(
                    "BLOCKING",
                    f"config_context_{key}_mismatch",
                    f"config_context.{key}={actual!r} does not match expected {expected!r}.",
                ))

    if isinstance(bible.get("characters"), list) and not bible["characters"]:
        issues.append(VisualBibleIssue("WARNING", "characters_empty", "No named characters found."))
    if isinstance(bible.get("recurring_motifs"), list) and not bible["recurring_motifs"]:
        issues.append(VisualBibleIssue("WARNING", "recurring_motifs_empty", "No recurring motifs found."))
    if isinstance(bible.get("locations"), list) and len(bible["locations"]) < 1:
        issues.append(VisualBibleIssue("WARNING", "few_locations", "Few or no locations found."))
    if isinstance(bible.get("first_15_seconds_rules"), list) and len(bible["first_15_seconds_rules"]) < 2:
        issues.append(VisualBibleIssue("WARNING", "first_15_seconds_rules_weak", "first_15_seconds_rules is missing or weak."))
    if isinstance(bible.get("forbidden_generic_shots"), list) and len(bible["forbidden_generic_shots"]) < 3:
        issues.append(VisualBibleIssue("WARNING", "forbidden_generic_shots_generic", "forbidden_generic_shots list is likely too thin."))
    if not str(config_context.get("visual_style") or "").strip():
        issues.append(VisualBibleIssue("WARNING", "channel_visual_style_unavailable", "Channel visual style unavailable."))
    if not str(config_context.get("image_style") or "").strip():
        issues.append(VisualBibleIssue("WARNING", "channel_image_style_unavailable", "Channel image style unavailable."))

    return issues


def _call_and_prepare_bible(
    content: Content,
    prompt_input: dict[str, Any],
    config_context: dict[str, Any],
    corrective_issues: list[VisualBibleIssue] | None = None,
) -> dict:
    user_message = _build_user_message(prompt_input, corrective_issues=corrective_issues)
    result = call_claude_structured(
        task=VISUAL_BIBLE_TASK,
        system_prompt=_VISUAL_BIBLE_SYSTEM_PROMPT,
        user_message=user_message,
        schema_name="visual_bible",
        input_schema=_VISUAL_BIBLE_SCHEMA,
        max_tokens=2600,
    )
    return _prepare_bible(content, result, config_context)


def _prepare_bible(content: Content, raw_bible: dict, config_context: dict[str, Any]) -> dict:
    bible = dict(raw_bible or {})
    bible["version"] = VISUAL_BIBLE_VERSION
    bible["content_id"] = str(content.id)
    bible["generated_at"] = datetime.now(timezone.utc).isoformat()
    bible["config_context"] = config_context
    bible.setdefault("story_visual_summary", "")
    bible.setdefault("global_style", {})
    bible.setdefault("characters", [])
    bible.setdefault("locations", [])
    bible.setdefault("recurring_motifs", [])
    bible.setdefault("continuity_rules", [])
    bible.setdefault("negative_prompt_rules", [])
    bible.setdefault("first_15_seconds_rules", [])
    bible.setdefault("forbidden_generic_shots", [])
    return bible


def _load_parent_visual_bible(content: Content, *, force: bool) -> dict | None:
    if not bool(getattr(content, "is_short_episode", False)) or not getattr(content, "parent_content_id", None):
        return None
    parent_bible = load_visual_bible_for_content(content.parent_content_id)
    if not parent_bible:
        return None
    if _blocking(validate_visual_bible(parent_bible)):
        return None
    return parent_bible


def _child_copy_from_parent(content: Content, parent_bible: dict) -> dict:
    child_bible = dict(parent_bible)
    child_bible["content_id"] = str(content.id)
    child_bible["generated_at"] = datetime.now(timezone.utc).isoformat()
    child_bible["parent_content_id"] = str(content.parent_content_id)
    child_bible["selected_parent_visual_bible_path"] = str(get_visual_bible_path(content.parent_content_id))
    child_bible["child_short_focus"] = {
        "short_part_number": getattr(content, "short_part_number", None),
        "short_total_parts": getattr(content, "short_total_parts", None),
        "rule": "Reuse the parent visual identity; do not create a separate visual world for this short.",
    }
    config_context = dict(child_bible.get("config_context") or {})
    config_context["source"] = "parent_visual_bible"
    child_bible["config_context"] = config_context
    return child_bible


def _write_visual_bible(content_id: int | str | UUID, bible: dict) -> None:
    ensure_run_dirs(content_id)
    path = get_visual_bible_path(content_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(bible, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _build_config_context(
    content: Content,
    channel: Channel,
    config: ChannelConfig | None,
    db: Session,
) -> dict[str, Any]:
    snapshot = getattr(content, "channel_config_snapshot", None)
    if isinstance(snapshot, dict) and snapshot:
        return _config_context_from_snapshot(snapshot)

    languages = [
        row.language
        for row in db.query(ChannelLanguage).filter(ChannelLanguage.channel_id == channel.id).all()
    ]
    platforms = sorted({
        row.platform
        for row in db.query(ChannelPlatform).filter(ChannelPlatform.channel_id == channel.id).all()
        if getattr(row, "active", False)
    })
    return {
        "source": "live_channel_config",
        "channel_name": getattr(channel, "name", "") or "",
        "channel_description": getattr(channel, "description", "") or "",
        "channel_niche": getattr(channel, "niche", "") or "",
        "channel_tone": getattr(channel, "tone", "") or "",
        "content_mode": getattr(config, "content_mode", "") if config else "",
        "script_source": getattr(config, "script_source", "") if config else "",
        "output_mode": getattr(config, "output_mode", "") if config else "",
        "visual_style": getattr(config, "visual_style", "") if config else "",
        "image_style": getattr(config, "image_style", "") if config else "",
        "video_style_type": getattr(config, "video_style_type", "") if config else "",
        "video_color_grade": getattr(config, "video_color_grade", "") if config else "",
        "target_languages": sorted({lang for lang in languages if lang}),
        "target_platforms": platforms,
    }


def _config_context_from_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    flat = dict(snapshot)
    config = snapshot.get("config") if isinstance(snapshot.get("config"), dict) else {}
    channel = snapshot.get("channel") if isinstance(snapshot.get("channel"), dict) else {}
    return {
        "source": "channel_config_snapshot",
        "channel_name": _first_text(flat.get("channel_name"), channel.get("name")),
        "channel_description": _first_text(flat.get("channel_description"), channel.get("description")),
        "channel_niche": _first_text(flat.get("channel_niche"), channel.get("niche"), flat.get("niche")),
        "channel_tone": _first_text(flat.get("channel_tone"), channel.get("tone"), flat.get("tone")),
        "content_mode": _first_text(flat.get("content_mode"), config.get("content_mode")),
        "script_source": _first_text(flat.get("script_source"), config.get("script_source")),
        "output_mode": _first_text(flat.get("output_mode"), config.get("output_mode")),
        "visual_style": _first_text(flat.get("visual_style"), config.get("visual_style")),
        "image_style": _first_text(flat.get("image_style"), config.get("image_style")),
        "video_style_type": _first_text(flat.get("video_style_type"), config.get("video_style_type")),
        "video_color_grade": _first_text(flat.get("video_color_grade"), config.get("video_color_grade")),
        "target_languages": _list_text(flat.get("target_languages") or config.get("target_languages") or []),
        "target_platforms": _list_text(flat.get("target_platforms") or config.get("target_platforms") or []),
    }


def _load_validated_scripts(content_id: int | str | UUID, db: Session) -> list[Script]:
    return list(
        db.query(Script)
        .filter(Script.content_id == content_id, Script.validated.is_(True))
        .order_by(Script.language, Script.version.desc())
        .all()
    )


def _build_prompt_input(
    *,
    content: Content,
    channel: Channel,
    channel_config: ChannelConfig | None,
    scripts: list[Script],
    config_context: dict[str, Any],
) -> dict[str, Any]:
    source_script = next((script for script in scripts if script.language == content.source_language), None)
    if source_script is None and scripts:
        source_script = scripts[0]
    script_excerpt = _excerpt(getattr(source_script, "voice_script", "") if source_script else "", 6000)
    return {
        "content": {
            "id": str(content.id),
            "title": getattr(content, "title", "") or "",
            "source_language": getattr(content, "source_language", "") or "",
            "kind": "child_short" if bool(getattr(content, "is_short_episode", False)) else "parent_long_form",
            "parent_content_id": str(getattr(content, "parent_content_id", "") or ""),
            "story_blueprint": getattr(content, "story_blueprint", None) or {},
        },
        "channel": {
            "name": getattr(channel, "name", "") or "",
            "description": getattr(channel, "description", "") or "",
            "niche": getattr(channel, "niche", "") or "",
            "tone": getattr(channel, "tone", "") or "",
        },
        "config_context": config_context,
        "script_excerpt": script_excerpt,
    }


def _build_user_message(
    prompt_input: dict[str, Any],
    *,
    corrective_issues: list[VisualBibleIssue] | None = None,
) -> str:
    corrective = ""
    if corrective_issues:
        corrective = (
            "\nPrevious visual bible failed deterministic validation. Correct these issues exactly:\n"
            + json.dumps([issue.__dict__ for issue in corrective_issues if issue.severity == "BLOCKING"], ensure_ascii=False, indent=2)
            + "\n"
        )
    return (
        "Create a cinematic visual continuity bible for image generation. This is not a script summary.\n"
        "Use the configured channel visual_style/image_style/color grade. Define stable characters, clothing, locations, lighting, camera/lens/framing language, recurring motifs, continuity tags, negative prompt rules, first-15-seconds visual rules, and forbidden generic shots.\n"
        "continuity_rules should contain at least 3 practical rules when possible, such as stable character identity, stable clothing/props, consistent location lighting, and recurring motif usage. If the story has limited detail, infer conservative rules from the script and channel style rather than returning an empty list.\n"
        "Forbid random character appearance changes, random location changes, generic horror filler, text/watermark/logo suggestions, exact platform analytics, claims of live research, invented production metadata, and symbolic-only abstractions that cannot be filmed.\n"
        "Do not include secrets, credentials, API keys, platform tokens, raw environment values, or encrypted credential blobs.\n"
        f"{corrective}\nContext JSON:\n"
        + json.dumps(prompt_input, ensure_ascii=False, indent=2)
    )


_VISUAL_BIBLE_SYSTEM_PROMPT = """You are Agent 4's visual continuity director.
Return only the structured visual_bible tool payload. Build practical, camera-pointable continuity guidance for Flux/image generation. Preserve channel configuration. Never include credentials or secrets. Avoid generic horror filler, text/logo/watermark suggestions, and random identity drift."""


def _require_list(bible: dict, field: str, issues: list[VisualBibleIssue]) -> None:
    if not isinstance(bible.get(field), list):
        issues.append(VisualBibleIssue("BLOCKING", f"{field}_invalid", f"{field} is missing or not a list."))


def _blocking(issues: list[VisualBibleIssue]) -> list[VisualBibleIssue]:
    return [issue for issue in issues if issue.severity == "BLOCKING"]


def _excerpt(text: str, limit: int) -> str:
    compact = " ".join(str(text or "").split())
    return compact[:limit]


def _first_text(*values: Any) -> str:
    for value in values:
        if value not in (None, ""):
            return str(value)
    return ""


def _list_text(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    return [str(value) for value in values if value not in (None, "")]
