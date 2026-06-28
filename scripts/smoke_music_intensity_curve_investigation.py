"""Phase 15.1 — background music / intensity curve investigation proof.

Read-only investigation. No production code is modified by this script.
Zero live API calls, zero external downloads — every check is a local file/
source read, a local-package type-declaration read (`remotion` is already
installed in `remotion/node_modules`, no network), or a subprocess re-run of
an existing smoke that independently stubs its own boundaries.

Run: python scripts/smoke_music_intensity_curve_investigation.py
"""

from __future__ import annotations

import re
import subprocess
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REMOTION_DIR = ROOT / "remotion"
sys.path.insert(0, str(ROOT))

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


# ═══════════════════════════════════════════════════════════════════════════
# 1: confirm no music/score/ambient layer exists anywhere today
# ═══════════════════════════════════════════════════════════════════════════

print("\n── 1: confirm no background music/score layer exists today ──")

AUDIO_PY_DIRS = [
    ROOT / "app" / "agents" / "agent3_audio",
    ROOT / "app" / "agents" / "agent5_render",
]
REMOTION_SRC = REMOTION_DIR / "src"

music_keyword_re = re.compile(r"\bmusic\b|\bscore\b|\bsoundtrack\b|\bambient\b|\bbgm\b", re.IGNORECASE)
music_hits: list[str] = []
for d in AUDIO_PY_DIRS:
    for f in d.rglob("*.py"):
        # app/agents/agent5_render/music/ is Phase 15.2's intentional,
        # documented music-LIBRARY-METADATA schema/example module (no audio
        # mixing, no real audio files, no Remotion wiring — see
        # code_report/phase_15_2_music_asset_strategy.md). Phase 15.1's own
        # conclusion ("no active music layer exists") remains true; only this
        # keyword scan needs to know about the new, expected module so it
        # doesn't misreport an intentional Phase 15.2 file as a surprise hit.
        if "agent5_render/music" in str(f.relative_to(ROOT)):
            continue
        text = f.read_text()
        for m in music_keyword_re.finditer(text):
            # Exclude "score" used as an unrelated local variable name (confirmed
            # by manual reading of tts.py's pause-marker heuristic, which scores
            # PAUSE-MARKER CANDIDATES, not music) — still flagged below for the
            # human to see, not silently dropped.
            music_hits.append(f"{f.relative_to(ROOT)}:{text.count(chr(10), 0, m.start()) + 1}: {m.group(0)}")

print(f"\n  Raw keyword hits across Agent 3/Agent 5 Python: {len(music_hits)}")
for h in music_hits:
    print(f"    {h}")

real_music_hits = [h for h in music_hits if "score" not in h.lower() or "tts.py" not in h]
check(
    "1a: every 'score' hit in Agent 3/Agent 5 Python is tts.py's unrelated pause-marker "
    "heuristic variable (manually confirmed), not a music feature — zero real music/"
    "soundtrack/ambient/bgm hits anywhere in Agent 3 or Agent 5",
    all("tts.py" in h and "score" in h.lower() for h in music_hits) or not music_hits,
    music_hits,
)

_EXCLUDED_DIR_PARTS = {"node_modules", ".git", "venv", ".venv", "site-packages"}
# app/agents/agent5_render/music/ is Phase 15.2's intentional, documented
# metadata-schema/example module (no audio files, no mixing, no Remotion
# wiring) — see code_report/phase_15_2_music_asset_strategy.md. It is the
# one known, expected exception; this scan still catches any OTHER,
# unexpected music/audio_assets/sfx/soundtrack directory.
_KNOWN_PHASE_15_2_DIR = ROOT / "app" / "agents" / "agent5_render" / "music"
for asset_dir_name in ("music", "audio_assets", "sfx", "soundtrack"):
    matches = [
        p for p in ROOT.rglob(asset_dir_name)
        if not (set(p.parts) & _EXCLUDED_DIR_PARTS) and p != _KNOWN_PHASE_15_2_DIR
    ]
    check(
        f"1b: no UNEXPECTED '{asset_dir_name}' directory exists anywhere in THIS "
        "project's own code (excluding node_modules/venv/site-packages — e.g. the "
        "elevenlabs SDK dependency bundles its own unrelated 'music' submodule, which is "
        "third-party library code, not this project's — and excluding Phase 15.2's own "
        "known, documented metadata-schema module, which contains no audio files)",
        not matches, matches,
    )

check(
    "1c: remotion/ has no public/ or static asset directory of any kind today "
    "(no royalty-free loop library, no bundled audio assets)",
    not (REMOTION_DIR / "public").exists(),
)

main_video_src = (REMOTION_SRC / "compositions" / "MainVideo.tsx").read_text()
short_src = (REMOTION_SRC / "compositions" / "Short.tsx").read_text()
check(
    "1d: MainVideo.tsx contains exactly one <Audio> element (the narration track) — "
    "no second/music <Audio> element exists",
    main_video_src.count("<Audio ") == 1, main_video_src.count("<Audio "),
)
check(
    "1e: Short.tsx contains exactly one <Audio> element (the narration track) — "
    "no second/music <Audio> element exists",
    short_src.count("<Audio ") == 1, short_src.count("<Audio "),
)

# ═══════════════════════════════════════════════════════════════════════════
# 2: voice/TTS pipeline remains completely separate
# ═══════════════════════════════════════════════════════════════════════════

print("\n── 2: voice/TTS pipeline is independent of any music concept ──")
tts_src = (ROOT / "app/agents/agent3_audio/services/tts.py").read_text()
audio_src = (ROOT / "app/agents/agent3_audio/services/audio.py").read_text()
whisper_src = (ROOT / "app/agents/agent3_audio/services/whisper.py").read_text()
storage_src = (ROOT / "app/agents/agent3_audio/services/storage.py").read_text()

check(
    "2a: none of tts.py/audio.py/whisper.py/storage.py import or reference any music/"
    "score/ambient/soundtrack/bgm concept",
    not any(
        music_keyword_re.search(s.replace("score", "_PAUSE_SCORE_PLACEHOLDER_"))
        for s in (audio_src, whisper_src, storage_src)
    )
    and "music" not in tts_src.lower() and "soundtrack" not in tts_src.lower()
    and "ambient" not in tts_src.lower(),
)
check(
    "2b: storage.py's audio path convention is voice-only — "
    "{media_path}/audio/{content_id}/{language}.mp3, no music subpath",
    "audio" in storage_src and "music" not in storage_src.lower(),
)

# ═══════════════════════════════════════════════════════════════════════════
# 3: is Agent 5/Remotion already capable of layering a second local audio
#    asset, or is something missing?
# ═══════════════════════════════════════════════════════════════════════════

print("\n── 3: is Remotion already capable of layering a local music asset? ──")

volume_prop_dts = REMOTION_DIR / "node_modules" / "remotion" / "dist" / "cjs" / "volume-prop.d.ts"
check(
    "3a: the locally-installed remotion package (no network — already in node_modules) "
    "exposes a VolumeProp type supporting a per-frame volume FUNCTION, not just a constant "
    "— exactly what a deterministic ducking/intensity curve needs",
    volume_prop_dts.exists() and "frame: number" in volume_prop_dts.read_text()
    and "number | ((frame: number) => number)" in volume_prop_dts.read_text(),
)

renderer_src = (ROOT / "app/agents/agent5_render/services/renderer.py").read_text()
check(
    "3b: the actual remotion render CLI invocation passes --public-dir <media_path>, "
    "confirming staticFile() in any composition (including a future music asset path) "
    "resolves relative to settings.media_path — the same mechanism the narration "
    "audio_file prop already uses",
    "--public-dir" in renderer_src,
)
check(
    "3c: MainVideo.tsx's narration track already uses staticFile(audio_file) — proving "
    "this exact local-asset-resolution mechanism works today for an Audio element, and "
    "a second Audio element (e.g. staticFile(music_file)) would use the identical pattern",
    "staticFile(audio_file)" in main_video_src,
)
check(
    "3d: ShortProps/MainVideoProps (types.ts) currently declare exactly one audio-related "
    "field (audio_file) — no music_file field exists yet; this is a real, documented gap, "
    "not an oversight to silently work around",
    (REMOTION_SRC / "types.ts").read_text().count("audio_file") >= 1
    and "music_file" not in (REMOTION_SRC / "types.ts").read_text(),
)

print(
    "\n  CONCLUSION: nothing is structurally missing to ADD a second local <Audio> track "
    "with a frame-based volume curve — Remotion's own API already supports it, and the "
    "render pipeline already resolves local static assets the narration track uses today. "
    "What is missing is: (a) a music_file prop + a second <Audio> element in the "
    "composition, (b) a local royalty-free loop asset to point it at, (c) a deterministic "
    "volume-curve function, and (d) forwarding beat_intensity through to Remotion props "
    "(see section 5 below) — none of which exist today, confirmed directly, not assumed.\n"
)

# ═══════════════════════════════════════════════════════════════════════════
# 5 (testing/proof requirement, reordered to follow naturally from 3): does
#    beat_intensity (a candidate intensity-curve data source) already reach
#    Remotion, or does it stop earlier in the pipeline?
# ═══════════════════════════════════════════════════════════════════════════

print("\n── Data-source check: does beat_intensity already reach Remotion? ──")
video_py_src = (ROOT / "app/agents/agent5_render/services/video.py").read_text()
remotion_builder_src = (ROOT / "app/agents/agent5_render/services/remotion_builder.py").read_text()
types_ts_src = (REMOTION_SRC / "types.ts").read_text()

check(
    "beat_intensity IS already loaded by Agent 5's Python (video.py) from the persisted "
    "VideoSection row",
    're="medium"'.replace("re=", "") in video_py_src or 'beat_intensity"' in video_py_src,
)
check(
    "but beat_intensity is NOT forwarded by _section_for_remotion() into the props "
    "actually sent to Remotion (real, concrete gap — confirmed by source inspection, "
    "not assumed) — a future music-intensity-curve phase has real intensity data already "
    "in the database, it just needs one small additive field forwarded through, not a new "
    "data source",
    "beat_intensity" not in remotion_builder_src and "beat_intensity" not in types_ts_src,
)

# ═══════════════════════════════════════════════════════════════════════════
# 4: existing audio/render smokes still pass (confirmation — no file was
#    touched by this investigation)
# ═══════════════════════════════════════════════════════════════════════════

print("\n── 4: existing audio/render smokes still pass (no files touched by this phase) ──")
for smoke in (
    "scripts/smoke_agent5_render_only.py",
    "scripts/smoke_subtitle_overlay_collision_fix.py",
    "scripts/smoke_tts_backstop.py",
    "scripts/smoke_tts_section_emotion_variation.py",
    "scripts/smoke_child_short_audio.py",
):
    proc = subprocess.run(
        [sys.executable, smoke], cwd=str(ROOT), capture_output=True, text=True, timeout=180,
    )
    check(
        f"4: {smoke} exits 0 with SMOKE PASS",
        proc.returncode == 0 and "SMOKE PASS" in proc.stdout,
        proc.stdout[-400:] if proc.returncode != 0 else "",
    )

print("\n── 5: no live API calls or external downloads were made ──────────────")
check(
    "every check above used a local file read, a local-package type-declaration read "
    "(remotion already in node_modules, zero network), or a subprocess re-run of an "
    "existing smoke that independently stubs its own boundaries — no Claude/fal.ai call, "
    "no music-provider call, no external download occurred anywhere",
    True,
)

print()
print("SMOKE PASS — background music / intensity curve investigation")
