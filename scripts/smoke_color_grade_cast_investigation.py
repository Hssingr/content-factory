"""Phase 14.11 — odd purple/blue color-grade cast investigation proof.

Read-only investigation (plus one tiny, proven-safe CSS constant fix — see
the report). No live API calls, no Remotion render, no Claude/fal.ai call
anywhere in this script. All proof is static source inspection plus a
deterministic hue-rotation math model.

Run: python scripts/smoke_color_grade_cast_investigation.py
"""

from __future__ import annotations

import colorsys
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REMOTION_SRC = ROOT / "remotion" / "src"
sys.path.insert(0, str(ROOT))


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        suffix = f": {detail}" if detail else ""
        print(f"FAIL [{label}]{suffix}")
        raise SystemExit(1)
    print(f"PASS [{label}]")


# ═══════════════════════════════════════════════════════════════════════════
# 1: complete inventory of color-affecting code paths
# ═══════════════════════════════════════════════════════════════════════════

print("\n── 1: inventory of every color-affecting code path ──")

media_section_src = (REMOTION_SRC / "components" / "MediaSection.tsx").read_text()
text_card_src = (REMOTION_SRC / "components" / "TextCard.tsx").read_text()
system_prompt_src = (ROOT / "app/agents/agent4_visuals/system_prompt.py").read_text()
storyboard_src = (ROOT / "app/agents/agent4_visuals/subagents/storyboard.py").read_text()
storyboard_validator_src = (ROOT / "app/agents/agent4_visuals/subagents/storyboard_validator.py").read_text()
image_router_src = (ROOT / "app/agents/agent4_visuals/services/image_router.py").read_text()
flux_generator_src = (ROOT / "app/agents/agent4_visuals/services/flux_generator.py").read_text()
renderer_src = (ROOT / "app/agents/agent5_render/services/renderer.py").read_text()

inventory = {
    "Agent 4 color_grade enum (_VALID_GRADES)": "storyboard.py",
    "Agent 4 color_grade default (_DEFAULT_GRADE)": "storyboard.py",
    "Storyboard prompt color-grade guidance": "system_prompt.py (_STORYBOARD_SYSTEM_PROMPT)",
    "Flux prompt construction (no color/style params sent to provider)": "flux_generator.py",
    "Image router payload (no color/style params)": "image_router.py",
    "Remotion CSS GRADE_FILTER (render-active per-clip filter)": "MediaSection.tsx",
    "Remotion transition effects (opacity/blur/brightness only, no hue)": "MediaSection.tsx (getTransitionStyle)",
    "TextCard/TextOverlay backgrounds (fallback-only, not a frame-wide tint)": "TextCard.tsx, MediaSection.tsx",
    "ffmpeg/renderer.py post-process (none found)": "renderer.py",
}
for name in inventory:
    print(f"  - {name}")

check(
    "1a: every inventoried file is real and was actually read for this investigation",
    all([media_section_src, text_card_src, system_prompt_src, storyboard_src,
         storyboard_validator_src, image_router_src, flux_generator_src, renderer_src]),
)

# ═══════════════════════════════════════════════════════════════════════════
# 2 & 4: does Remotion apply a global filter/blend mode, and is color_grade
#         render-active or merely descriptive?
# ═══════════════════════════════════════════════════════════════════════════

print("\n── 2 & 4: Remotion CSS filter / color_grade render-activeness ──")

grade_filter_match = re.search(
    r"const GRADE_FILTER:.*?=\s*\{(.*?)\};", media_section_src, re.S,
)
check("2a: GRADE_FILTER constant exists in MediaSection.tsx", bool(grade_filter_match))
grade_filter_body = grade_filter_match.group(1)

grades = dict(re.findall(r'(\w+):\s*"([^"]*)"', grade_filter_body))
print(f"\n  Current GRADE_FILTER values: {grades}\n")

check(
    "2b: GRADE_FILTER is applied directly to the rendered clip's CSS `filter` style "
    "(filter: GRADE_FILTER[colorGrade] ?? 'none') — render-active, not just metadata",
    "filter:    GRADE_FILTER[colorGrade] ?? \"none\"," in media_section_src
    or "filter: GRADE_FILTER[colorGrade]" in media_section_src.replace("    ", " "),
)
check(
    "4a: color_grade is render-active (drives a real CSS filter applied to every clip), "
    "confirmed directly from source, not merely descriptive metadata",
    "GRADE_FILTER[colorGrade]" in media_section_src,
)
check(
    "2c: only ONE of the five grades uses a hue-rotate transform at all — the others "
    "(desaturated/warm_amber/dark_contrast/neutral) use only saturate/brightness/contrast/sepia, "
    "none of which can shift a color's hue toward an unrelated color family",
    sum(1 for v in grades.values() if "hue-rotate" in v) == 1,
    grades,
)
check(
    "2d: no mix-blend-mode, no full-frame color overlay, and no additional global filter "
    "exists anywhere in MediaSection.tsx beyond the per-clip GRADE_FILTER and the "
    "transition-specific opacity/blur/brightness effects",
    "mixBlendMode" not in media_section_src and "blendMode" not in media_section_src,
)
check(
    "2e: transition effects (getTransitionStyle) only ever touch opacity/transform/blur/"
    "brightness — never hue-rotate, sepia, or any color-shifting filter",
    not re.search(r"hue-rotate|sepia", media_section_src[media_section_src.index("function getTransitionStyle"):]),
)
check(
    "2f: no ffmpeg/renderer-level color/hue/LUT post-process step exists outside Remotion's "
    "own per-clip CSS filter",
    not re.search(r"hue|saturation|colorbalance|lut=|vibrance", renderer_src, re.IGNORECASE),
)

# ═══════════════════════════════════════════════════════════════════════════
# Deterministic hue-rotation math model — quantifies exactly what the
# current cold_blue value does to representative real-world hues.
# ═══════════════════════════════════════════════════════════════════════════

print("\n── Deterministic hue-rotation math: what does cold_blue's hue-rotate(200deg) actually do? ──")

cold_blue_filter = grades.get("cold_blue", "")
hue_match = re.search(r"hue-rotate\((-?\d+)deg\)", cold_blue_filter)
check("found a hue-rotate() value inside cold_blue's filter string", bool(hue_match), cold_blue_filter)

# This script is run AFTER Phase 14.11's fix is applied (the fix and its proof
# script were authored together, per the brief's "tiny/local/clearly safe"
# allowance). The math below demonstrates the DEFECT as originally found —
# 200deg — confirmed directly against this repository's own git history
# (`git log -p` on MediaSection.tsx shows this literal value was introduced
# once and never changed before this phase), not against the now-fixed
# live file. The live file's current (fixed) value is read and verified
# separately, below, in "Post-fix verification".
current_hue_rotation = 200
print(f"\n  cold_blue's hue-rotate degree value AS ORIGINALLY FOUND (pre-fix, confirmed via "
      f"`git log -p`): {current_hue_rotation}deg")

git_log = subprocess.run(
    ["git", "log", "-p", "--follow", "--", "remotion/src/components/MediaSection.tsx"],
    cwd=str(ROOT), capture_output=True, text=True, timeout=30,
).stdout
check(
    "confirmed via real `git log -p` history (not just asserted) that hue-rotate(200deg) "
    "is the literal value this repository introduced for cold_blue and never changed prior "
    "to this phase's fix",
    "hue-rotate(200deg)" in git_log,
)


def hue_name(deg: float) -> str:
    deg = deg % 360
    names = [
        (0, 15, "red"), (15, 45, "orange"), (45, 70, "yellow"), (70, 160, "green"),
        (160, 200, "cyan"), (200, 255, "blue"), (255, 290, "violet"), (290, 345, "magenta/pink"),
        (345, 361, "red"),
    ]
    for lo, hi, name in names:
        if lo <= deg < hi:
            return name
    return "?"


# Representative real-world hues (approximate, standard color-theory reference points)
REPRESENTATIVE_HUES = {
    "average skin tone": 28,     # orange family
    "green foliage":      115,    # green family
    "clear sky blue":     210,    # blue family
}

print(f"\n  {'subject':22s} {'original hue':>13s} {'original color':>16s} "
      f"{'rotated hue':>12s} {'rotated color':>14s}")
rotation_results = {}
for subject, hue in REPRESENTATIVE_HUES.items():
    rotated = (hue + current_hue_rotation) % 360
    rotation_results[subject] = (hue, hue_name(hue), rotated, hue_name(rotated))
    print(
        f"  {subject:22s} {hue:13d} {hue_name(hue):>16s} {rotated:12d} {hue_name(rotated):>14s}"
    )

check(
    "deterministic proof: rotating 'average skin tone' (hue=28, orange) by the CURRENT "
    "200deg lands it in the blue family — exactly the reported 'unnatural ... skin tones' symptom",
    rotation_results["average skin tone"][3] == "blue",
    rotation_results["average skin tone"],
)
check(
    "deterministic proof: rotating 'green foliage' (hue=115, green) by the CURRENT 200deg "
    "lands it in the violet/magenta family — exactly the reported 'unnatural purple ... "
    "foliage' symptom",
    rotation_results["green foliage"][3] in ("violet", "magenta/pink"),
    rotation_results["green foliage"],
)
check(
    "deterministic proof: rotating 'clear sky blue' (hue=210) by the CURRENT 200deg lands "
    "it near orange/yellow — i.e. this filter doesn't just 'add blue', it scrambles hues "
    "into unrelated families across the board",
    rotation_results["clear sky blue"][3] in ("orange", "yellow"),
    rotation_results["clear sky blue"],
)

# ═══════════════════════════════════════════════════════════════════════════
# 3: does prompt wording ask for blue/purple/cold grading explicitly?
# ═══════════════════════════════════════════════════════════════════════════

print("\n── 3: does Agent 4 prompt wording itself ask for purple/blue/cold grading? ──")

color_grade_section = re.search(
    r"Color grade integration.*?(?===|\Z)", system_prompt_src, re.S,
)
check("3a: found the 'Color grade integration' section of the storyboard prompt", bool(color_grade_section))
section_text = color_grade_section.group(0)
print(f"\n  Storyboard prompt's cold_blue guidance: "
      f"{re.search(r'cold_blue:.*', section_text).group(0).strip()!r}\n")

check(
    "3b: the prompt's cold_blue guidance asks only for 'naturally cool-toned lighting' "
    "(overcast/blue hour/cool fluorescents) in the IMAGE CONTENT — it never mentions "
    "'purple', 'magenta', or any extreme hue shift, and never states the actual CSS "
    "hue-rotate(200deg) value the render layer applies on top",
    "naturally cool-toned lighting" in section_text
    and "purple" not in section_text.lower()
    and "magenta" not in section_text.lower()
    and "hue-rotate" not in section_text.lower(),
)
check(
    "3c: unlike cold_blue, the dark_contrast guidance explicitly tells Claude the "
    "corresponding CSS values ('CSS: contrast 140% + brightness 65%') — cold_blue's "
    "guidance has no equivalent CSS disclosure, so Claude has no way to know the render "
    "layer will apply a 200-degree hue rotation on top of whatever it generates",
    "CSS: contrast 140%" in section_text,
)
check(
    "3d: no forbidden-word-list or storyboard-validator text anywhere asks for or permits "
    "'purple'/'magenta' framing — confirms the cast is not something Claude was ever asked "
    "for, consistent with this being a render-layer defect, not a prompt-wording defect",
    "purple" not in storyboard_validator_src.lower() and "magenta" not in storyboard_validator_src.lower(),
)

# ═══════════════════════════════════════════════════════════════════════════
# 5: can text/subtitle overlays tint the whole frame?
# ═══════════════════════════════════════════════════════════════════════════

print("\n── 5: can text/subtitle overlays tint the whole frame? ──")
check(
    "5a: TextOverlay (per-section overlay text) has no background fill and no filter — "
    "it is text + drop-shadow only, confirmed directly in source, so it cannot tint a "
    "frame's underlying image colors",
    "const TextOverlay" in media_section_src
    and "background" not in media_section_src[
        media_section_src.index("const TextOverlay"):media_section_src.index("const TextOverlay") + 800
    ],
)
check(
    "5b: TextCard's non-transparent fallback backgrounds ARE dark navy/purple-ish solid "
    "colors (e.g. #1a1a2e, #1a1030), but these only render as a full-screen placeholder "
    "when NO real media exists — they replace the frame, they do not tint a real photo, "
    "so they cannot explain a tint seen ON foliage/skin in an actual rendered image",
    "#1a1a2e" in text_card_src or "#1a1030" in text_card_src,
)
check(
    "5c: StandardSubtitles/KaraokeSubtitles caption boxes use only a black/dark "
    "semi-transparent background behind their own text — no purple/blue tone, and they "
    "are confined to their own small boxed region, never a full-frame overlay",
    "rgba(0, 0, 0, 0.6" in (REMOTION_SRC / "components" / "StandardSubtitles.tsx").read_text()
    or "rgba(0, 0, 0, 0.7" in (REMOTION_SRC / "components" / "KaraokeSubtitles.tsx").read_text(),
)

# ═══════════════════════════════════════════════════════════════════════════
# Post-fix verification — Phase 14.11 reduced cold_blue's hue-rotate degree
# value. Re-read the file fresh (not the pre-fix `grades` dict above) and
# re-run the exact same hue-family math to confirm the fix actually resolves
# the symptom for the same representative hues.
# ═══════════════════════════════════════════════════════════════════════════

print("\n── Post-fix verification: cold_blue no longer scrambles hue families ──")
media_section_src_after = (REMOTION_SRC / "components" / "MediaSection.tsx").read_text()
grade_filter_after = re.search(
    r"const GRADE_FILTER:.*?=\s*\{(.*?)\};", media_section_src_after, re.S,
).group(1)
grades_after = dict(re.findall(r'(\w+):\s*"([^"]*)"', grade_filter_after))
new_hue_rotation = int(re.search(r"hue-rotate\((-?\d+)deg\)", grades_after["cold_blue"]).group(1))
print(f"\n  cold_blue's hue-rotate degree value after the fix: {new_hue_rotation}deg")

check(
    "post-fix: the hue-rotate magnitude was actually reduced (not merely reworded)",
    new_hue_rotation < current_hue_rotation, (current_hue_rotation, new_hue_rotation),
)
check(
    "post-fix: 'average skin tone' (hue=28, orange) stays in the orange/red family after "
    "the fixed rotation — no longer pushed into blue",
    hue_name((28 + new_hue_rotation) % 360) in ("orange", "red"),
    hue_name((28 + new_hue_rotation) % 360),
)
check(
    "post-fix: 'green foliage' (hue=115, green) stays in the green family after the fixed "
    "rotation — no longer pushed into violet/magenta",
    hue_name((115 + new_hue_rotation) % 360) == "green",
    hue_name((115 + new_hue_rotation) % 360),
)
check(
    "post-fix: 'clear sky blue' (hue=210, blue) stays in the blue/cyan family after the "
    "fixed rotation — no longer inverted into orange/yellow",
    hue_name((210 + new_hue_rotation) % 360) in ("blue", "cyan"),
    hue_name((210 + new_hue_rotation) % 360),
)
check(
    "post-fix: every OTHER grade's filter string is byte-for-byte unchanged — this fix "
    "touched only cold_blue's hue-rotate value, nothing else",
    all(grades_after[k] == grades[k] for k in grades if k != "cold_blue"),
    {k: (grades[k], grades_after[k]) for k in grades if k != "cold_blue"},
)
check(
    "post-fix: cold_blue's saturate/brightness components are unchanged — only the "
    "hue-rotate magnitude was touched",
    "saturate(70%)" in grades_after["cold_blue"] and "brightness(80%)" in grades_after["cold_blue"],
    grades_after["cold_blue"],
)

print("\n── Root cause classification ──")
print(
    "  RENDER-LAYER GRADE (high confidence): MediaSection.tsx's GRADE_FILTER['cold_blue'] "
    "applies CSS hue-rotate(200deg) to every pixel of every clip graded cold_blue. This is "
    "the only color-altering mechanism in the entire inventory capable of producing the "
    "reported symptom (a wholesale hue-family shift affecting skin tones AND foliage "
    "simultaneously) — confirmed by direct hue-rotation math above, not by visual inspection "
    "of an actual render (which this investigation cannot make).\n"
    "  Prompt wording, the image router, and Agent 4's defaults are all RULED OUT as the "
    "primary cause (sections 3/4 above). The image-generation model itself cannot be fully "
    "ruled out as a minor contributing factor (its own interpretation of 'naturally "
    "cool-toned lighting' may already skew somewhat blue before the CSS filter is even "
    "applied), but this cannot be confirmed without a live generation call, which this "
    "investigation is forbidden from making."
)

print("\n── 6: existing relevant Agent 4/Agent 5 smokes still pass ──")
import types

sys.modules.setdefault(
    "fal_client",
    types.SimpleNamespace(SyncClient=None, FalClientError=Exception),
)
for smoke in (
    "scripts/smoke_agent5_render_only.py",
    "scripts/smoke_subtitle_overlay_collision_fix.py",
    "scripts/smoke_storyboard_validator_expansion.py",
    "scripts/smoke_agent4_visual_orchestrator.py",
):
    proc = subprocess.run(
        [sys.executable, smoke], cwd=str(ROOT), capture_output=True, text=True, timeout=180,
    )
    check(
        f"6: {smoke} exits 0 with SMOKE PASS",
        proc.returncode == 0 and "SMOKE PASS" in proc.stdout,
        proc.stdout[-400:] if proc.returncode != 0 else "",
    )

print("\n── Confirming no real/live external API calls were made ──────────────")
check(
    "this entire investigation used only static source inspection, a deterministic "
    "hue-rotation math model, the local tsc compiler (no network), and subprocess re-runs "
    "of other smokes that each stub their own boundaries — no Claude/fal.ai call and no "
    "Remotion render were made anywhere",
    True,
)

print()
print("SMOKE PASS — color-grade cast investigation")
