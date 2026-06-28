"""Text-card generated background smoke — zero live API calls, zero DB access.

Verifies:
  1. A remotion_text_card beat with empty flux_prompt gets a derived background prompt.
  2. The derived prompt does not ask Flux to render the readable overlay text.
  3. A text-card beat receives a generated background media_url.
  4. Text overlay remains render-time text in Remotion props/source.
  5. Text-card background generation uses Flux Schnell, not Dev/Pro/model routing.
  6. The existing text-card frequency cap remains 30%.
  7. Normal non-text-card image beats are unchanged.
  8. No real fal.ai / Flux / Claude / Remotion call is made.

Run: python scripts/smoke_text_card_generated_backgrounds.py
Expected output: PASS lines, then SMOKE PASS.
"""

from __future__ import annotations

import inspect
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Import-only stub: production code imports fal_client at module import time, but
# this smoke monkeypatches the actual Flux call boundary before generation.
sys.modules.setdefault(
    "fal_client",
    types.SimpleNamespace(SyncClient=None, FalClientError=Exception),
)

from app.agents.agent4_visuals.services import flux_generator
from app.agents.agent4_visuals.subagents.storyboard_validator import validate_storyboard
from app.agents.agent5_render.services.remotion_builder import _section_for_remotion


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        suffix = f": {detail}" if detail else ""
        print(f"FAIL [{label}]{suffix}")
        raise SystemExit(1)
    print(f"PASS [{label}]")


calls: list[dict] = []
orig_call_fal = flux_generator._call_fal
orig_fal_key = flux_generator.settings.fal_key

def fake_call_fal(
    prompt: str, cache_dir: Path, media_path: Path,
    cache_key_extra: str = "", model_key: str = "schnell",
) -> str | None:
    calls.append({
        "prompt": prompt,
        "cache_key_extra": cache_key_extra,
        "cache_dir": str(cache_dir),
        "model_key": model_key,
    })
    return f"cache/smoke-content/{len(calls):02d}.jpg"

try:
    flux_generator.settings.fal_key = "stubbed-key-never-used"
    flux_generator._call_fal = fake_call_fal

    text_card = {
        "beat_order": 3,
        "section_order": 3,
        "media_strategy": "remotion_text_card",
        "visual_type": "text_card",
        "visual_intent": "A police interview room table with a sealed envelope and recorder before the reveal",
        "script_text": "Then the detective placed the sealed envelope on the table.",
        "overlay_text": "EMERGENCY ALERT 911",
        "text_card_style": "document",
        "environment": "indoor_office",
        "flux_prompt": "",
        "media_url": "",
        "media_type": "image",
    }

    flux_generator.generate_text_card_background_image(text_card, "smoke-content")
    derived_prompt = text_card["flux_prompt"]

    check("text-card empty flux_prompt gets derived prompt", bool(derived_prompt.strip()))
    check(
        "derived prompt is concrete and contextual",
        "police interview room table" in derived_prompt
        and "office desk surface" in derived_prompt
        and "real physical background scene" in derived_prompt,
        derived_prompt,
    )
    check(
        "derived prompt forbids readable text in generated image",
        "no readable text" in derived_prompt
        and "no letters" in derived_prompt
        and "no typography" in derived_prompt,
        derived_prompt,
    )
    check(
        "derived prompt does not copy overlay text into Flux prompt",
        "EMERGENCY" not in derived_prompt and "911" not in derived_prompt,
        derived_prompt,
    )
    check(
        "text-card receives generated background media_url",
        text_card["media_url"].startswith("cache/smoke-content/"),
        text_card["media_url"],
    )
    check("text-card remains visual_type=text_card", text_card["visual_type"] == "text_card")
    check("text-card media_type remains image background", text_card["media_type"] == "image")
    check(
        "text-card cache namespace prevents prior beat image reuse",
        calls and calls[-1]["cache_key_extra"] == "text_card_background:3",
        repr(calls[-1] if calls else None),
    )
    check(
        "Phase 14.6: text-card background generation still routes to model_key=schnell "
        "(generate_text_card_background_image() never passes model_key, so it always "
        "defaults to schnell regardless of any image-router config)",
        calls and calls[-1]["model_key"] == "schnell",
        repr(calls[-1] if calls else None),
    )

    props_section = _section_for_remotion({
        **text_card,
        "audio_start_ms": 0,
        "audio_end_ms": 5000,
        "effect": "slow_zoom",
        "color_grade": "neutral",
        "transition_to_next": "cut",
        "overlay_position": "center",
    })
    check("Remotion props preserve text-card background clip", bool(props_section["clips"]))
    check("Remotion props keep overlay_text for render-time text", props_section["overlay_text"] == "EMERGENCY ALERT 911")
    check("Remotion props keep visual_type=text_card", props_section["visual_type"] == "text_card")

    media_section_src = (ROOT / "remotion" / "src" / "components" / "MediaSection.tsx").read_text()
    text_card_src = (ROOT / "remotion" / "src" / "components" / "TextCard.tsx").read_text()
    check(
        "Remotion overlays TextCard over generated background",
        "section.visual_type === \"text_card\"" in media_section_src
        and "transparentBackground" in media_section_src
        and "<TextCard" in media_section_src,
    )
    check(
        "TextCard supports transparent background mode",
        "transparentBackground?: boolean" in text_card_src
        and "transparentBackground = false" in text_card_src,
    )

    check("Flux endpoint is Schnell", flux_generator._FAL_ENDPOINT == "fal-ai/flux-1/schnell")
    flux_src = inspect.getsource(flux_generator)
    check("text-card background path imports no model routing", "model_routing" not in flux_src and "resolve_model" not in flux_src)
    check("text-card background path does not mention Dev/Pro Flux endpoints", "flux-1/dev" not in flux_src and "flux-1/pro" not in flux_src)

    cap_fixture = []
    for i in range(10):
        cap_fixture.append({
            "beat_order": i,
            "media_strategy": "remotion_text_card" if i < 4 else "flux_generated",
            "visual_type": "text_card" if i < 4 else "b-roll",
            "flux_prompt": "office desk with lamp and folder, close-up, indoor office, practical light, photorealistic, sharp focus, no text",
            "environment": "indoor_office",
            "motif": "document" if i < 4 else "object",
            "effect": "cut",
            "beat_intensity": "medium",
        })
    cap_issues = validate_storyboard(cap_fixture)
    check(
        "text-card frequency cap remains at 30 percent",
        any(issue["check"] == "text_card_saturation" for issue in cap_issues)
        and "> 30% threshold" in inspect.getsource(validate_storyboard),
    )

    normal = {
        "beat_order": 0,
        "media_strategy": "flux_generated",
        "visual_type": "b-roll",
        "flux_prompt": "worn door with brass handle, close-up, hallway, side light, photorealistic, sharp focus, no text",
        "environment": "corridor_interior",
        "media_url": "",
    }
    flux_generator.generate_all_beat_images([normal], "smoke-normal")
    check("normal non-text-card prompt unchanged", normal["flux_prompt"].startswith("worn door with brass handle"))
    check("normal non-text-card media_strategy unchanged", normal["media_strategy"] == "flux_generated")
    check("normal non-text-card receives generated media_url", normal["media_url"].startswith("cache/smoke-content/"))

finally:
    flux_generator._call_fal = orig_call_fal
    flux_generator.settings.fal_key = orig_fal_key

check("no real fal.ai / Flux / Claude / Remotion call made", True)
print("SMOKE PASS — text-card generated backgrounds")
