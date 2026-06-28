"""Phase 14.6 — unified visual source/model router smoke.

Zero live API calls — `fal_client` is stubbed at import time (it may not even
be installed locally), and `flux_generator._call_fal` is monkeypatched in the
end-to-end sections below so no network call is ever attempted. Everything
else exercised — `image_router.select_route()`, `image_router.build_fal_payload()`,
`image_router.build_cache_key_material()`, `flux_generator.generate_beat_image_with_routing()`,
`flux_generator.generate_all_beat_images()`, `flux_generator.generate_text_card_background_image()`
— is real, unmodified-by-this-script production code.

Run: python scripts/smoke_image_model_router.py
"""

from __future__ import annotations

import inspect
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Import-only stub: production code imports fal_client at module import time.
sys.modules.setdefault(
    "fal_client",
    types.SimpleNamespace(SyncClient=None, FalClientError=Exception),
)


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        suffix = f": {detail}" if detail else ""
        print(f"FAIL [{label}]{suffix}")
        raise SystemExit(1)
    print(f"PASS [{label}]")


from app.agents.agent4_visuals.services import image_router
from app.agents.agent4_visuals.services import flux_generator


def beat(order=1, **overrides) -> dict:
    base = {
        "beat_order": order, "section_order": order,
        "script_text": "Ordinary establishing narration.",
        "flux_prompt": "Empty office desk, morning light, photorealistic, sharp focus",
        "visual_intent": "office desk", "visual_type": "b-roll",
        "visual_category": "place", "environment": "indoor_office", "motif": "documents",
        "effect": "cut", "color_grade": "neutral", "transition_to_next": "cut",
        "overlay_text": "", "overlay_position": "none", "beat_intensity": "medium",
        "suggested_duration_sec": 3.0, "media_url": "", "media_type": "image",
        "media_strategy": "flux_generated", "text_card_style": "default",
    }
    base.update(overrides)
    return base


print("\n── 1: ordinary generated beat defaults to Schnell ──")
route = image_router.select_route(beat(), "content-1")
check("1a: routing disabled (default) -> schnell", route.model_key == "schnell")
check("1b: source=generated for an ordinary pending beat", route.source == "generated")

print("\n── 2: text-card background always routes to Schnell, even with Dev/Pro enabled ──")
tc_beat = beat(media_strategy="remotion_text_card", visual_type="text_card", beat_intensity="high")
route_tc = image_router.select_route(
    tc_beat, "content-1", purpose="text_card_background",
    routing_enabled=True, allow_dev=True, allow_pro=True, max_pro_per_content=99,
)
check(
    "2a: text-card background routes to schnell even with Dev/Pro fully enabled and a "
    "high-intensity cover-frame-like beat that would otherwise qualify for Pro",
    route_tc.model_key == "schnell" and route_tc.reason == "text_card_schnell_only",
)

print("\n── 3: Dev tier is not used when disabled ──")
qualifying_beat = beat(order=0, beat_intensity="high")  # cover frame + high intensity
route_dev_off = image_router.select_route(
    qualifying_beat, "content-1", routing_enabled=True, allow_dev=False, allow_pro=False,
)
check("3a: routing enabled but allow_dev=False -> still schnell", route_dev_off.model_key == "schnell")

print("\n── 4: Dev tier selected when enabled and heuristic qualifies ──")
route_dev_on = image_router.select_route(
    qualifying_beat, "content-1", routing_enabled=True, allow_dev=True, allow_pro=False,
)
check("4a: routing+allow_dev enabled, qualifying beat -> dev", route_dev_on.model_key == "dev")

non_qualifying_beat = beat(order=5, beat_intensity="low", visual_category="place")
route_dev_not_qualified = image_router.select_route(
    non_qualifying_beat, "content-1", routing_enabled=True, allow_dev=True, allow_pro=False,
)
check(
    "4b: routing+allow_dev enabled but beat does NOT qualify (not cover frame, not high "
    "intensity, no person, no reveal keyword) -> stays schnell",
    route_dev_not_qualified.model_key == "schnell",
)

print("\n── 5: Pro tier is not used when disabled ──")
route_pro_off = image_router.select_route(
    qualifying_beat, "content-1", routing_enabled=True, allow_dev=True, allow_pro=False,
    max_pro_per_content=99,
)
check("5a: allow_pro=False -> never pro, even with huge cap and a qualifying beat",
      route_pro_off.model_key != "pro_1_1")

print("\n── 6: Pro tier respects max_pro_images_per_content ──")
route_pro_within_cap = image_router.select_route(
    qualifying_beat, "content-1", routing_enabled=True, allow_dev=True, allow_pro=True,
    max_pro_per_content=1, pro_used_so_far=0,
)
check("6a: within cap (0 used, cap=1) -> pro_1_1", route_pro_within_cap.model_key == "pro_1_1")
route_pro_at_cap = image_router.select_route(
    qualifying_beat, "content-1", routing_enabled=True, allow_dev=True, allow_pro=True,
    max_pro_per_content=1, pro_used_so_far=1,
)
check("6b: at cap (1 used, cap=1) -> falls back to dev, not pro",
      route_pro_at_cap.model_key == "dev")
route_pro_zero_cap = image_router.select_route(
    qualifying_beat, "content-1", routing_enabled=True, allow_dev=True, allow_pro=True,
    max_pro_per_content=0, pro_used_so_far=0,
)
check("6c: default max_pro_per_content=0 (conservative default) -> never pro",
      route_pro_zero_cap.model_key != "pro_1_1")

print("\n── 7: Flux 2 Pro payload omits unsupported steps/guidance ──")
payload_flux2 = image_router.build_fal_payload("flux_2_pro", "a prompt")
check("7a: flux_2_pro payload has no num_inference_steps",
      "num_inference_steps" not in payload_flux2)
check("7b: flux_2_pro payload has no guidance_scale",
      "guidance_scale" not in payload_flux2)
check("7c: flux_2_pro capability table declares supports_steps=False/supports_guidance=False",
      image_router.MODEL_CAPABILITIES["flux_2_pro"]["supports_steps"] is False
      and image_router.MODEL_CAPABILITIES["flux_2_pro"]["supports_guidance"] is False)

print("\n── 8: Pro-family safety field uses safety_tolerance, not Schnell's enable_safety_checker-only ──")
payload_pro = image_router.build_fal_payload("pro_1_1", "a prompt")
check("8a: pro_1_1 payload includes safety_tolerance", "safety_tolerance" in payload_pro)
check("8b: pro_1_1 payload does NOT include enable_safety_checker (Schnell-only field)",
      "enable_safety_checker" not in payload_pro)
payload_schnell = image_router.build_fal_payload("schnell", "a prompt")
check("8c: schnell payload includes enable_safety_checker",
      "enable_safety_checker" in payload_schnell)
check("8d: schnell payload does NOT include safety_tolerance (Pro-family-only field)",
      "safety_tolerance" not in payload_schnell)
payload_flux2_safety = image_router.build_fal_payload("flux_2_pro", "a prompt")
check("8e: flux_2_pro (safety_mode='both') payload includes BOTH safety fields",
      "safety_tolerance" in payload_flux2_safety and "enable_safety_checker" in payload_flux2_safety)

print("\n── 9: Schnell/Dev payloads use correct size/steps/guidance fields ──")
check("9a: schnell payload uses image_size (not aspect_ratio)",
      "image_size" in payload_schnell and "aspect_ratio" not in payload_schnell)
check("9b: schnell payload num_inference_steps == capability default (8, preserved behavior)",
      payload_schnell["num_inference_steps"] == 8)
check("9c: schnell payload has no guidance_scale (supports_guidance=False)",
      "guidance_scale" not in payload_schnell)
payload_dev = image_router.build_fal_payload("dev", "a prompt")
check("9d: dev payload uses image_size", "image_size" in payload_dev)
check("9e: dev payload num_inference_steps == 28 (fal.ai documented Dev default)",
      payload_dev["num_inference_steps"] == 28)
check("9f: dev payload includes guidance_scale == 3.5", payload_dev.get("guidance_scale") == 3.5)
payload_ultra = image_router.build_fal_payload("pro_1_1_ultra", "a prompt", aspect_ratio="9:16")
check("9g: pro_1_1_ultra payload uses aspect_ratio, not image_size",
      "aspect_ratio" in payload_ultra and "image_size" not in payload_ultra
      and payload_ultra["aspect_ratio"] == "9:16")

print("\n── 10: cache keys differ between Schnell and Dev for the same prompt ──")
same_prompt = "A worn wooden door, brass knocker, photorealistic"
key_schnell = image_router.build_cache_key_material("schnell", same_prompt)
key_dev = image_router.build_cache_key_material("dev", same_prompt)
check("10a: schnell cache material == prompt verbatim (zero cache invalidation for existing beats)",
      key_schnell == same_prompt)
check("10b: dev cache material differs from schnell's for the identical prompt",
      key_dev != key_schnell)
check("10c: dev cache material is model-qualified", "model=dev" in key_dev)
key_pro = image_router.build_cache_key_material("pro_1_1", same_prompt)
check("10d: pro_1_1 cache material also differs from both schnell and dev",
      key_pro != key_schnell and key_pro != key_dev)
# Phase 14.4 text-card namespace backward-compat: schnell + cache_key_extra must
# reproduce the EXACT pre-Phase-14.6 material, or every existing text-card cache
# entry is invalidated for free.
key_textcard = image_router.build_cache_key_material(
    "schnell", same_prompt, cache_key_extra="text_card_background:3",
)
check(
    "10e: schnell + cache_key_extra reproduces the exact Phase 14.4 cache material format "
    "(f'{extra}\\n{prompt}') — no unintended cache invalidation for existing text-card images",
    key_textcard == f"text_card_background:3\n{same_prompt}",
)

print("\n── 11: returned media_url remains local cache/..., never a remote URL ──")
calls: list[dict] = []
orig_call_fal = flux_generator._call_fal


def fake_call_fal(prompt, cache_dir, media_path, cache_key_extra="", model_key="schnell"):
    calls.append({"prompt": prompt, "model_key": model_key, "cache_key_extra": cache_key_extra})
    return f"cache/content-1/{len(calls):02d}.jpg"


flux_generator._call_fal = fake_call_fal
orig_fal_key = flux_generator.settings.fal_key
flux_generator.settings.fal_key = "stub-key-never-used"
try:
    media_url = flux_generator.generate_beat_image_with_routing(beat(order=2), "content-1", {})
finally:
    flux_generator._call_fal = orig_call_fal
    flux_generator.settings.fal_key = orig_fal_key

check("11a: generate_beat_image_with_routing() returns a local cache/... path",
      isinstance(media_url, str) and media_url.startswith("cache/"))
check("11b: no http:// or https:// ever appears in the returned media_url",
      "http://" not in media_url and "https://" not in media_url)

print("\n── End-to-end: generate_all_beat_images() respects routing flags and the Pro cap ──")
flux_generator._call_fal = fake_call_fal
flux_generator.settings.fal_key = "stub-key-never-used"
orig_routing_enabled = flux_generator.settings.image_routing_enabled
orig_allow_dev = flux_generator.settings.image_routing_allow_dev
orig_allow_pro = flux_generator.settings.image_routing_allow_pro
orig_max_pro = flux_generator.settings.image_routing_max_pro_per_content
calls.clear()
try:
    flux_generator.settings.image_routing_enabled = True
    flux_generator.settings.image_routing_allow_dev = True
    flux_generator.settings.image_routing_allow_pro = True
    flux_generator.settings.image_routing_max_pro_per_content = 1

    beats = [
        beat(order=0, beat_intensity="high"),   # cover frame -> qualifies -> pro (cap=1, first)
        beat(order=1, beat_intensity="high"),   # also qualifies -> pro cap reached -> dev
        beat(order=2, beat_intensity="low", visual_category="place"),  # does not qualify -> schnell
        {**beat(order=3), "media_strategy": "remotion_text_card", "visual_type": "text_card",
         "overlay_text": "THE TRUTH", "flux_prompt": ""},  # text card -> always schnell
    ]
    result_beats = flux_generator.generate_all_beat_images(beats, "content-e2e")
finally:
    flux_generator._call_fal = orig_call_fal
    flux_generator.settings.fal_key = orig_fal_key
    flux_generator.settings.image_routing_enabled = orig_routing_enabled
    flux_generator.settings.image_routing_allow_dev = orig_allow_dev
    flux_generator.settings.image_routing_allow_pro = orig_allow_pro
    flux_generator.settings.image_routing_max_pro_per_content = orig_max_pro

model_keys_used = [c["model_key"] for c in calls]
check(
    "E2E-a: exactly one pro_1_1 call (the per-content cap=1 was respected across the whole batch)",
    model_keys_used.count("pro_1_1") == 1, f"model_keys_used={model_keys_used}",
)
check(
    "E2E-b: the second qualifying beat fell back to dev once the pro cap was reached",
    model_keys_used.count("dev") == 1, f"model_keys_used={model_keys_used}",
)
check(
    "E2E-c: the non-qualifying beat used schnell",
    model_keys_used.count("schnell") >= 1, f"model_keys_used={model_keys_used}",
)
check(
    "E2E-d: the text-card beat's background generation used schnell (last call), even though "
    "Dev/Pro routing was fully enabled for this whole batch",
    calls[-1]["model_key"] == "schnell", f"calls[-1]={calls[-1]}",
)
check(
    "E2E-e: every beat ended up with a local cache/... media_url",
    all((b.get("media_url") or "").startswith("cache/") for b in result_beats),
    [b.get("media_url") for b in result_beats],
)

print("\n── 12: existing text-card background smoke still passes ──")
import subprocess
proc = subprocess.run(
    [sys.executable, "scripts/smoke_text_card_generated_backgrounds.py"],
    cwd=str(ROOT), capture_output=True, text=True, timeout=120,
)
check("12a: smoke_text_card_generated_backgrounds.py exits 0 with SMOKE PASS",
      proc.returncode == 0 and "SMOKE PASS" in proc.stdout,
      proc.stdout[-400:] if proc.returncode != 0 else "")

print("\n── 13: existing Short visual hold-cap smoke still passes ──")
proc2 = subprocess.run(
    [sys.executable, "scripts/smoke_short_visual_hold_cap.py"],
    cwd=str(ROOT), capture_output=True, text=True, timeout=120,
)
check("13a: smoke_short_visual_hold_cap.py exits 0 with SMOKE PASS",
      proc2.returncode == 0 and "SMOKE PASS" in proc2.stdout,
      proc2.stdout[-400:] if proc2.returncode != 0 else "")

print("\n── 14: existing Agent 4 visual smokes still pass ──")
for smoke in (
    "scripts/smoke_agent4_visual_orchestrator.py",
    "scripts/smoke_child_remap_validator.py",
    "scripts/smoke_storyboard_validator_expansion.py",
    "scripts/smoke_media_validator.py",
    "scripts/smoke_flux_prompt_validator.py",
):
    proc_i = subprocess.run(
        [sys.executable, smoke], cwd=str(ROOT), capture_output=True, text=True, timeout=120,
    )
    check(f"14: {smoke} exits 0 with SMOKE PASS",
          proc_i.returncode == 0 and "SMOKE PASS" in proc_i.stdout,
          proc_i.stdout[-400:] if proc_i.returncode != 0 else "")

print("\n── Architecture/boundary checks ──")
src_router = inspect.getsource(image_router)
src_flux = inspect.getsource(flux_generator)
check("text-card background derivation path still does not import the router "
      "(generate_text_card_background_image/derive_text_card_background_prompt "
      "never reference image_router, preserving Phase 14.4's exemption)",
      "image_router" not in inspect.getsource(flux_generator.generate_text_card_background_image)
      and "image_router" not in inspect.getsource(flux_generator.derive_text_card_background_prompt))
import ast

_router_imports = []
for node in ast.walk(ast.parse(src_router)):
    if isinstance(node, ast.ImportFrom) and node.module:
        _router_imports.append(node.module)
    elif isinstance(node, ast.Import):
        _router_imports.extend(a.name for a in node.names)
check("image_router.py makes no fal_client import (pure decision/payload module, "
      "no direct provider call — flux_generator.py remains the only fal_client boundary; "
      "the module docstring mentions fal_client in prose only, which is fine)",
      not any("fal_client" in m for m in _router_imports))
check("flux_generator.py remains the only direct fal_client integration point "
      "(import fal_client only here, not in image_router.py)",
      "import fal_client" in src_flux)
check("capability table covers schnell, dev, pro_1_1, pro_1_1_ultra, flux_2_pro",
      set(image_router.MODEL_CAPABILITIES.keys())
      == {"schnell", "dev", "pro_1_1", "pro_1_1_ultra", "flux_2_pro"})

print("\n── Confirming no real/live external API calls were made ──────────────")
check("flux_generator._call_fal restored to the original after every stub use",
      flux_generator._call_fal is orig_call_fal)
check("settings.fal_key restored to its original value",
      flux_generator.settings.fal_key == orig_fal_key)
check("settings.image_routing_* flags restored to their original values",
      flux_generator.settings.image_routing_enabled == orig_routing_enabled
      and flux_generator.settings.image_routing_allow_dev == orig_allow_dev
      and flux_generator.settings.image_routing_allow_pro == orig_allow_pro
      and flux_generator.settings.image_routing_max_pro_per_content == orig_max_pro)

print()
print("SMOKE PASS — unified visual source/model router")
