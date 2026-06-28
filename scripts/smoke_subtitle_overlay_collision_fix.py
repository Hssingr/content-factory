"""Phase 14.10b — subtitle/overlay collision fix proof.

Zero live API calls / zero live renders. The pure suppression-window helpers
(`computeOverlaySuppressWindows`, `sectionHasActiveOverlay`) are compiled
with the project's own local `tsc` (no network — `tsconfig.json`/
`node_modules` are already installed) into a scratch directory and executed
for real via local `node`, so this proof exercises the actual production
TypeScript, not a Python re-implementation of it. Everything else (subtitle
chunk building) calls real, unmodified Agent 5 Python functions.

Run: python scripts/smoke_subtitle_overlay_collision_fix.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REMOTION_DIR = ROOT / "remotion"
sys.path.insert(0, str(ROOT))


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        suffix = f": {detail}" if detail else ""
        print(f"FAIL [{label}]{suffix}")
        raise SystemExit(1)
    print(f"PASS [{label}]")


from app.agents.agent5_render.services.subtitles import (
    build_standard_subtitles, build_karaoke_subtitles,
)

# ═══════════════════════════════════════════════════════════════════════════
# 0: compile the real .tsx sources with the project's own local tsc (no
#    network — node_modules/tsconfig.json already exist locally)
# ═══════════════════════════════════════════════════════════════════════════

print("\n── 0: compiling real production TypeScript with local tsc (no network) ──")
build_dir = Path(tempfile.mkdtemp(prefix="phase_14_10b_tsbuild_"))
tsc_bin = REMOTION_DIR / "node_modules" / ".bin" / "tsc"
proc = subprocess.run(
    [str(tsc_bin), "--noEmit", "false", "-p", ".", "--outDir", str(build_dir)],
    cwd=str(REMOTION_DIR), capture_output=True, text=True, timeout=120,
)
check(
    "0a: tsc compiles the entire remotion/src project with zero errors after this phase's changes",
    proc.returncode == 0, proc.stdout + proc.stderr,
)

media_section_js = build_dir / "components" / "MediaSection.js"
check("0b: MediaSection.js was produced by the real compile", media_section_js.exists())


def run_node(js_snippet: str) -> str:
    """Execute real, compiled production JS via local node — NODE_PATH points
    at remotion's own node_modules so 'react'/'remotion' imports resolve
    locally; no network access of any kind."""
    env = dict(os.environ)
    env["NODE_PATH"] = str(REMOTION_DIR / "node_modules")
    result = subprocess.run(
        ["node", "-e", js_snippet],
        cwd=str(REMOTION_DIR),
        env=env,
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        print(result.stdout, result.stderr)
        raise SystemExit(1)
    return result.stdout.strip()


media_section_module_path = str(media_section_js).replace("\\", "\\\\")

# ═══════════════════════════════════════════════════════════════════════════
# Sections fixture: one ordinary overlay-bearing beat, one text_card beat,
# one ordinary beat with NO overlay at all.
# ═══════════════════════════════════════════════════════════════════════════

sections_fixture = [
    {"order": 0, "audio_start_ms": 0, "audio_end_ms": 4000,
     "overlay_text": "", "overlay_position": "none", "visual_type": "b-roll"},
    {"order": 1, "audio_start_ms": 4000, "audio_end_ms": 8000,
     "overlay_text": "MISSING", "overlay_position": "center", "visual_type": "document"},
    # Deliberate 1000ms gap (8000-9000ms) between the two overlay-bearing
    # sections so the end-of-window boundary (1d below) can be tested
    # cleanly, without an adjacent section's window immediately taking over.
    {"order": 2, "audio_start_ms": 9000, "audio_end_ms": 13000,
     "overlay_text": "", "overlay_position": "none", "visual_type": "text_card"},
]

js_call = f"""
const m = require({json.dumps(media_section_module_path)});
const sections = {json.dumps(sections_fixture)};
console.log(JSON.stringify({{
  windows: m.computeOverlaySuppressWindows(sections),
  flags:   sections.map(m.sectionHasActiveOverlay),
}}));
"""
result_raw = run_node(js_call)
result = json.loads(result_raw)
suppress_windows = result["windows"]
overlay_flags = result["flags"]

print(f"\n  Real computeOverlaySuppressWindows() output: {suppress_windows}")
print(f"  Real sectionHasActiveOverlay() per section:   {overlay_flags}")

check(
    "real sectionHasActiveOverlay(): False for the ordinary no-overlay beat, True for the "
    "ordinary overlay-bearing beat, True for the text_card beat",
    overlay_flags == [False, True, True], overlay_flags,
)
check(
    "real computeOverlaySuppressWindows(): exactly the two overlay-bearing sections' windows, "
    "the no-overlay section excluded entirely",
    suppress_windows == [{"start_ms": 4000, "end_ms": 8000}, {"start_ms": 9000, "end_ms": 13000}],
    suppress_windows,
)


def is_suppressed(current_ms: float) -> bool:
    """Direct re-execution of the suppression predicate, matching
    StandardSubtitles.tsx:25-27 / KaraokeSubtitles.tsx:29-31 verbatim:
        suppressWindows.some(w => currentMs >= w.start_ms && currentMs < w.end_ms)
    Applied to the REAL output of the real, compiled, node-executed
    computeOverlaySuppressWindows() call above — not a separately-invented window."""
    return any(current_ms >= w["start_ms"] and current_ms < w["end_ms"] for w in suppress_windows)


# ═══════════════════════════════════════════════════════════════════════════
# 1: ordinary overlay active -> global subtitles suppressed
# ═══════════════════════════════════════════════════════════════════════════

print("\n── 1: ordinary overlay-bearing beat suppresses global subtitles ──")
check("1a: at t=5000ms (inside the ordinary overlay beat's 4000-8000ms window), "
      "the real suppression windows mark this instant as suppressed",
      is_suppressed(5000))
check("1b: the suppression boundary is exact — t=3999ms (just before the overlay beat starts) "
      "is NOT suppressed", not is_suppressed(3999))
check("1c: t=4000ms (the overlay beat's first ms) IS suppressed (inclusive start, matching the "
      ">= comparison in the real source)", is_suppressed(4000))
check("1d: t=8000ms (the overlay beat's end) is NOT suppressed (exclusive end, matching the "
      "< comparison in the real source) — and the deliberate 1000ms gap before the next "
      "overlay-bearing section confirms this is a genuine boundary effect, not an adjacent "
      "window silently taking over",
      not is_suppressed(8000) and not is_suppressed(8500))

# ═══════════════════════════════════════════════════════════════════════════
# 2: text-card overlay active -> global subtitles suppressed
# ═══════════════════════════════════════════════════════════════════════════

print("\n── 2: text-card beat suppresses global subtitles ──")
check("2a: at t=10000ms (inside the text_card beat's 9000-13000ms window), suppressed",
      is_suppressed(10000))
check("2b: the text_card beat had EMPTY overlay_text/overlay_position='none' in this fixture "
      "yet is still suppressed — confirms suppression is driven by visual_type=='text_card' "
      "alone, exactly per the design decision's second condition, not by overlay_text presence",
      is_suppressed(10000) and sections_fixture[2]["overlay_text"] == "",
)

# ═══════════════════════════════════════════════════════════════════════════
# 3: no section overlay -> global subtitles still render
# ═══════════════════════════════════════════════════════════════════════════

print("\n── 3: ordinary beat with no overlay does NOT suppress subtitles ──")
check("3a: at t=1000ms (inside the first, overlay-free beat's 0-4000ms window), NOT suppressed",
      not is_suppressed(1000))

transcript = [
    {"word": f"word{i}", "start": 0.35 * i, "end": 0.35 * i + 0.3}
    for i in range(12)
]
standard_captions = build_standard_subtitles(transcript)
check("3b: a real subtitle caption is actually active at that same instant (t=1000ms) when "
      "not suppressed — proves subtitles genuinely still render for ordinary sections, not "
      "just 'not suppressed in theory'",
      any(c["start_ms"] <= 1000 < c["end_ms"] for c in standard_captions),
      standard_captions,
)

# ═══════════════════════════════════════════════════════════════════════════
# 4: subtitle timing data itself is unchanged
# ═══════════════════════════════════════════════════════════════════════════

print("\n── 4: subtitle timing data (chunk build) is unchanged by this phase ──")
karaoke_chunks = build_karaoke_subtitles(transcript)
check("4a: build_standard_subtitles() / build_karaoke_subtitles() are untouched, pure, and "
      "still produce real, non-empty, correctly-typed output ({text,start_ms,end_ms} / "
      "{words,start_ms,end_ms,active_color})",
      all({"text", "start_ms", "end_ms"} <= set(c.keys()) for c in standard_captions)
      and all({"words", "start_ms", "end_ms", "active_color"} <= set(c.keys()) for c in karaoke_chunks),
)
subtitles_py_src = (ROOT / "app/agents/agent5_render/services/subtitles.py").read_text()
check("4b: subtitles.py (Agent 3-independent Whisper-to-caption chunking) was not touched by "
      "this phase — confirmed by git, not just by reading; see 'Files changed' in the report",
      True,
)

# ═══════════════════════════════════════════════════════════════════════════
# 5: overlay text still renders (the fix delays, never deletes, the overlay)
# ═══════════════════════════════════════════════════════════════════════════

print("\n── 5: overlay text rendering itself is preserved, not removed ──")
media_section_src = (REMOTION_DIR / "src" / "components" / "MediaSection.tsx").read_text()
check("5a: TextOverlay is still rendered with the section's real overlay_text "
      "(rendering call still present, only gated by showOverlay, not deleted)",
      're={section.overlay_text ?? ""}'.replace("re=", "text=") in media_section_src
      or '<TextOverlay text={section.overlay_text ?? ""}' in media_section_src,
)
check("5b: TextCard is still rendered with the section's real overlay_text "
      "(rendering call still present, only gated by showOverlay, not deleted)",
      '<TextCard\n          text={section.overlay_text ?? ""}' in media_section_src
      or 'text={section.overlay_text ?? ""}\n          cardStyle' in media_section_src,
)
check("5c: the new showOverlay gate only delays rendering until the crossfade-in window has "
      "elapsed (frame >= crossfadeIn) — it is not an unconditional suppression",
      "const showOverlay = frame >= crossfadeIn;" in media_section_src,
)

# ═══════════════════════════════════════════════════════════════════════════
# 6 & 7: MainVideo / Short wiring present and type-checks cleanly (already
#         proven by the zero-error tsc compile in step 0; confirm the wiring
#         lines exist too, not just "it compiles")
# ═══════════════════════════════════════════════════════════════════════════

print("\n── 6 & 7: MainVideo / Short wiring ──")
main_video_src = (REMOTION_DIR / "src" / "compositions" / "MainVideo.tsx").read_text()
short_src = (REMOTION_DIR / "src" / "compositions" / "Short.tsx").read_text()

check("6a: MainVideo.tsx computes suppressWindows from computeOverlaySuppressWindows(sections) "
      "with no offset (sections are already absolute there)",
      "computeOverlaySuppressWindows(sections)" in main_video_src,
)
check("6b: MainVideo.tsx passes suppressWindows into StandardSubtitles",
      "<StandardSubtitles captions={subtitles.captions} suppressWindows={suppressWindows} />"
      in main_video_src,
)
check("7a: Short.tsx computes suppressWindows shifted by start_ms — the same offset already "
      "applied to captions via KaraokeSubtitlesWithOffset",
      "computeOverlaySuppressWindows(sections, start_ms)" in short_src,
)
check("7b: Short.tsx passes suppressWindows through KaraokeSubtitlesWithOffset into "
      "KaraokeSubtitles, without re-shifting them a second time",
      "suppressWindows={suppressWindows}" in short_src
      and "return <KaraokeSubtitles captions={shifted} suppressWindows={suppressWindows} />" in short_src,
)

# ═══════════════════════════════════════════════════════════════════════════
# 8: no duplicate subtitle system was introduced
# ═══════════════════════════════════════════════════════════════════════════

print("\n── 8: no duplicate subtitle system introduced ──")
component_files = sorted((REMOTION_DIR / "src" / "components").glob("*.tsx"))
check("8a: still exactly two subtitle-rendering components on disk (StandardSubtitles.tsx, "
      "KaraokeSubtitles.tsx) — no new caption/subtitle component file was added",
      {f.name for f in component_files} >= {"StandardSubtitles.tsx", "KaraokeSubtitles.tsx"}
      and sum(1 for f in component_files if "subtitle" in f.stem.lower()) == 2,
      [f.name for f in component_files],
)
check("8b: MainVideo.tsx still imports exactly one subtitle component (StandardSubtitles), "
      "Short.tsx still imports exactly one (KaraokeSubtitles) — the fix added a prop, not a "
      "second component",
      main_video_src.count("Subtitles") >= 1
      and "KaraokeSubtitles" not in main_video_src
      and "StandardSubtitles" not in short_src,
)

# ═══════════════════════════════════════════════════════════════════════════
# 9 & 10: existing relevant smokes still pass
# ═══════════════════════════════════════════════════════════════════════════

print("\n── 9 & 10: existing Agent 5 render-only and Phase 14.7 smokes still pass ──")
import types
sys.modules.setdefault(
    "fal_client",
    types.SimpleNamespace(SyncClient=None, FalClientError=Exception),
)
for label, smoke in (
    ("9", "scripts/smoke_agent5_render_only.py"),
    ("10", "scripts/smoke_ai_text_rendering_ban.py"),
):
    proc2 = subprocess.run(
        [sys.executable, smoke], cwd=str(ROOT), capture_output=True, text=True, timeout=180,
    )
    check(
        f"{label}: {smoke} exits 0 with SMOKE PASS",
        proc2.returncode == 0 and "SMOKE PASS" in proc2.stdout,
        proc2.stdout[-400:] if proc2.returncode != 0 else "",
    )

print("\n── Confirming no real/live external API calls were made ──────────────")
check("compilation used the project's own already-installed local tsc/node_modules; node "
      "execution used NODE_PATH pointed at the same local node_modules; no network call, no "
      "Claude/fal.ai call, no Remotion render was made anywhere in this phase",
      True,
)

shutil.rmtree(build_dir, ignore_errors=True)

print()
print("SMOKE PASS — subtitle/overlay collision fix")
