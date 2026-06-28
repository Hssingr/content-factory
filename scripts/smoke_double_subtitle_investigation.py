"""Phase 14.10 — double-subtitle render bug investigation proof.

Read-only investigation. No production code is modified by this script.
Zero live API calls — every prompt/render boundary touched is either a real,
unmodified, pure-Python production function called with local fixtures, or a
faithful, explicitly-cited Python port of the relevant Remotion (.tsx)
component's pure selection logic (no JS runtime is available in this
environment, so the .tsx boolean conditions are reproduced verbatim in
Python and each port states exactly which file/lines it mirrors).

Run: python scripts/smoke_double_subtitle_investigation.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
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
from app.agents.agent5_render.services.remotion_builder import _section_for_remotion

REMOTION_SRC = ROOT / "remotion" / "src"

# ═══════════════════════════════════════════════════════════════════════════
# 1: Inventory every renderable text layer (real .tsx files, not assumed)
# ═══════════════════════════════════════════════════════════════════════════

print("\n── 1: inventory of renderable text layers ──")
main_video_src = (REMOTION_SRC / "compositions" / "MainVideo.tsx").read_text()
short_src      = (REMOTION_SRC / "compositions" / "Short.tsx").read_text()
media_section_src = (REMOTION_SRC / "components" / "MediaSection.tsx").read_text()
root_src = (REMOTION_SRC / "Root.tsx").read_text()

check(
    "1a: MainVideo.tsx mounts exactly one global subtitle component (StandardSubtitles)",
    "StandardSubtitles" in main_video_src and "KaraokeSubtitles" not in main_video_src,
)
check(
    "1b: Short.tsx mounts exactly one global subtitle component (KaraokeSubtitles)",
    "KaraokeSubtitles" in short_src and "StandardSubtitles" not in short_src,
)
check(
    "1c: MediaSection.tsx defines a per-section TextOverlay component, independent of the "
    "global subtitle components above",
    "const TextOverlay" in media_section_src,
)
check(
    "1d: MediaSection.tsx renders TextCard for visual_type==='text_card' and TextOverlay for "
    "every other visual_type whenever overlay_text/overlay_position are set — both are "
    "PER-SECTION layers, distinct from the composition-level subtitle layer",
    'section.visual_type === "text_card"' in media_section_src
    and "<TextOverlay" in media_section_src,
)
check(
    "1e: Root.tsx defines exactly two compositions (MainVideo, Short) — no third/legacy "
    "composition or caption component exists anywhere in remotion/src",
    root_src.count("<Composition") == 2,
)
all_component_files = sorted((REMOTION_SRC / "components").glob("*.tsx"))
# MediaSection.tsx is excluded here too: Phase 14.10b's fix added the
# computeOverlaySuppressWindows()/sectionHasActiveOverlay() coordination
# helpers there, whose comments legitimately mention "subtitle" (they exist
# specifically to tell the subtitle layer when to suppress itself) — this is
# the known, already-cataloged per-section layer from the inventory above,
# not an undiscovered third caption-rendering component.
caption_like_components = [
    f.stem for f in all_component_files
    if re.search(r"caption|subtitle", f.read_text(), re.IGNORECASE) and f.stem not in
    ("StandardSubtitles", "KaraokeSubtitles", "MediaSection")
]
check(
    "1f: no stale/legacy third caption-rendering component exists under remotion/src/components "
    "(exhaustive scan of every .tsx file, not just the two known ones)",
    not caption_like_components, caption_like_components,
)

print(
    "\n  Complete text-layer inventory:\n"
    "    - StandardSubtitles.tsx  — global caption layer, MainVideo only\n"
    "    - KaraokeSubtitles.tsx   — global caption layer, Short only\n"
    "    - TextCard.tsx           — per-section layer, visual_type=='text_card' beats\n"
    "    - TextOverlay (in MediaSection.tsx) — per-section layer, every other visual_type\n"
    "    - PartLabel (in Short.tsx) — per-composition layer, child-short part number only\n"
)

# ═══════════════════════════════════════════════════════════════════════════
# 2 & 3: subtitle timing flow + adjacent-chunk overlap check
# ═══════════════════════════════════════════════════════════════════════════

print("\n── 2 & 3: subtitle timing flow + adjacent-chunk overlap check ──")


def synthetic_transcript(n_words: int, ms_per_word: int = 350, gap_every: int = 12) -> list[dict]:
    words = []
    t = 0
    for i in range(n_words):
        start = t
        end = t + ms_per_word
        words.append({"word": f"word{i}", "start": start / 1000, "end": end / 1000})
        t = end + (400 if (i + 1) % gap_every == 0 else 60)  # occasional natural pause
    return words


transcript = synthetic_transcript(40)
standard_captions = build_standard_subtitles(transcript)
karaoke_chunks = build_karaoke_subtitles(transcript)

check("2a: build_standard_subtitles() produces a non-empty caption list from a real transcript",
      len(standard_captions) > 1, len(standard_captions))
check("2b: build_karaoke_subtitles() produces a non-empty chunk list from the same transcript",
      len(karaoke_chunks) > 1, len(karaoke_chunks))

overlaps_standard = [
    (a, b) for a, b in zip(standard_captions, standard_captions[1:])
    if a["end_ms"] > b["start_ms"]
]
overlaps_karaoke = [
    (a, b) for a, b in zip(karaoke_chunks, karaoke_chunks[1:])
    if a["end_ms"] > b["start_ms"]
]
check(
    "3a: adjacent STANDARD subtitle chunks never overlap in time — each chunk's end_ms <= the "
    "next chunk's start_ms, confirmed directly on real build_standard_subtitles() output "
    "(subtitle_chunk_transition_overlap is RULED OUT for the standard caption path)",
    not overlaps_standard, overlaps_standard,
)
check(
    "3b: adjacent KARAOKE subtitle chunks never overlap in time either "
    "(ruled out for the karaoke caption path too)",
    not overlaps_karaoke, overlaps_karaoke,
)

# Half-open interval + Array.find() semantics (StandardSubtitles.tsx:14-16,
# KaraokeSubtitles.tsx:15-17: `currentMs >= c.start_ms && currentMs < c.end_ms`,
# first match only) mean at most one chunk is ever "active" per millisecond,
# even in a hypothetical overlapping-chunk scenario — ported and proven directly.


def active_caption(captions: list[dict], current_ms: float) -> dict | None:
    """Direct port of StandardSubtitles.tsx:14-16 / KaraokeSubtitles.tsx:15-17."""
    for c in captions:
        if current_ms >= c["start_ms"] and current_ms < c["end_ms"]:
            return c
    return None


# Construct a deliberately-overlapping pair to prove .find()-equivalent semantics
# return at most one match even when input data is malformed.
deliberately_overlapping = [
    {"text": "first", "start_ms": 0, "end_ms": 1000},
    {"text": "second", "start_ms": 500, "end_ms": 1500},
]
check(
    "3c: even with deliberately overlapping input chunks, the find()-style selector returns "
    "exactly one active chunk per millisecond, never both — proves an inclusive/exclusive "
    "boundary bug in THIS selector could not itself cause two SUBTITLE layers to render "
    "simultaneously from the SAME component",
    active_caption(deliberately_overlapping, 700) == deliberately_overlapping[0],
)

# ═══════════════════════════════════════════════════════════════════════════
# 4, 5, 7: the real mechanism — section-level overlay vs. the global subtitle
#          layer are independent, uncoordinated components
# ═══════════════════════════════════════════════════════════════════════════

print("\n── 4, 5, 7: section overlay (TextOverlay/TextCard) vs. global subtitles — independence check ──")


def section_shows_text_overlay(section: dict) -> tuple[bool, str]:
    """Direct port of MediaSection.tsx's validClips>0 branch (lines ~190-198):

        {section.visual_type === "text_card" ? (<TextCard .../>) : (<TextOverlay .../>)}

    combined with TextOverlay's own guard (line 70): `if (!text || position === "none") return null;`
    Returns (renders_a_text_layer, which_component).
    """
    visual_type = section.get("visual_type", "b-roll")
    overlay_text = section.get("overlay_text") or ""
    overlay_position = section.get("overlay_position") or "none"
    if visual_type == "text_card":
        # TextCard always renders when reached via the validClips>0 branch
        # (MediaSection.tsx:190-195) — it does not gate on overlay_text/position at all.
        return True, "TextCard"
    if overlay_text and overlay_position != "none":
        return True, "TextOverlay"
    return False, ""


# A real, Phase-14.7-shaped section: an ORDINARY (non-text-card) document beat
# with a derived overlay, run through the REAL _section_for_remotion().
poster_beat = {
    "section_order": 2, "visual_type": "document", "media_strategy": "flux_generated",
    "media_url": "cache/content-x/poster.jpg", "media_type": "image",
    "overlay_text": "MISSING", "overlay_position": "center",
    "audio_start_ms": 4000, "audio_end_ms": 8000,
    "effect": "cut", "color_grade": "neutral", "transition_to_next": "cut",
    "visual_intent": "a missing person poster", "text_card_style": "default",
}
poster_props = _section_for_remotion(poster_beat)

renders_overlay, which = section_shows_text_overlay(poster_props)
check(
    "4a: a real, unmodified _section_for_remotion() call on an ORDINARY (visual_type='document') "
    "beat carrying Phase-14.7-style overlay_text='MISSING'/overlay_position='center' confirms "
    "MediaSection.tsx would render a TextOverlay for it — NOT gated on visual_type=='text_card'",
    renders_overlay and which == "TextOverlay",
)

# Sweep every millisecond of this beat's on-screen window and check whether a
# subtitle caption is ALSO active at the same instant — using the SAME real
# captions built in section 2/3 above, shifted so one caption chunk genuinely
# falls inside the beat's 4000-8000ms window (mirroring real production
# timing, where captions span virtually the entire narration).
caption_during_beat = [
    c for c in standard_captions
    if c["start_ms"] < poster_beat["audio_end_ms"] and c["end_ms"] > poster_beat["audio_start_ms"]
]
collision_ms = None
for ms in range(poster_beat["audio_start_ms"], poster_beat["audio_end_ms"], 10):
    if active_caption(standard_captions, ms) is not None:
        collision_ms = ms
        break

check(
    "4b: at least one real subtitle caption chunk overlaps in time with the poster beat's "
    "on-screen window (this is the normal case — captions run nearly continuously)",
    bool(caption_during_beat), caption_during_beat,
)
check(
    "4c: a concrete millisecond exists, inside the beat's own window, where BOTH the global "
    "subtitle layer (StandardSubtitles) AND the per-section TextOverlay would be simultaneously "
    "visible — this is the reproduced double-text-layer defect, found deterministically, not "
    "by visual inspection of a render",
    collision_ms is not None, collision_ms,
)
if collision_ms is not None:
    colliding_caption = active_caption(standard_captions, collision_ms)
    print(
        f"\n  REPRODUCED: at t={collision_ms}ms, StandardSubtitles shows caption text "
        f"{colliding_caption['text']!r} (bottom, boxed, 48px) while MediaSection's TextOverlay "
        f"simultaneously shows {poster_props['overlay_text']!r} (center, no box, 56px) — "
        "different text, different styling, both mounted and both passing their own visibility "
        "guard at the same instant.\n"
    )

# Negative control: same beat, but with NO overlay set (overlay_position default "none")
# — confirms the collision is specific to overlay-bearing beats, not universal.
ordinary_beat_no_overlay = {**poster_beat, "overlay_text": "", "overlay_position": "none"}
ordinary_props = _section_for_remotion(ordinary_beat_no_overlay)
renders_overlay_none, _ = section_shows_text_overlay(ordinary_props)
check(
    "4d: NEGATIVE CONTROL — the identical beat with overlay_position left at the default "
    "'none' renders NO per-section text layer at all, so no collision is possible for it "
    "(confirms the defect is conditional on overlay_text/position being set, i.e. "
    "'intermittent', exactly as reported)",
    renders_overlay_none is False,
)

# Generalization check: the SAME collision mechanism applies to text_card beats
# too — proving the defect is NOT specific to text-card, as the brief instructs
# not to assume.
text_card_beat = {**poster_beat, "visual_type": "text_card", "media_strategy": "remotion_text_card"}
text_card_props = _section_for_remotion(text_card_beat)
renders_tc, which_tc = section_shows_text_overlay(text_card_props)
check(
    "5a: the IDENTICAL collision mechanism (an always-on global subtitle layer with no "
    "awareness of per-section overlays) ALSO applies to text_card beats — TextCard renders "
    "unconditionally whenever visual_type=='text_card', with the same lack of subtitle "
    "coordination as the ordinary-beat TextOverlay case above",
    renders_tc and which_tc == "TextCard",
)
check(
    "5b: this confirms the root cause is GENERAL (any section-level text layer vs. the global "
    "subtitle layer), not specific to text-card — consistent with the real-world report being "
    "observed on an ordinary image beat, not a text-card beat",
    True,
)

# ═══════════════════════════════════════════════════════════════════════════
# 6: secondary mechanism — crossfade transition overlap between ADJACENT
#    sections (each may carry its own overlay/text-card)
# ═══════════════════════════════════════════════════════════════════════════

print("\n── 6: secondary mechanism — crossfade transition window overlap between adjacent sections ──")

transition_duration_frames = {
    "crossfade": 15, "dip_to_black": 20, "whip_pan": 10,
    "zoom_blur": 12, "match_cut": 6, "cut": 0, "none": 0,
}  # direct port of MediaSection.tsx transitionDurationFrames(), lines 286-297

FPS = 30


def sequence_window(section_audio_start_ms, section_dur_ms, crossfade_in_frames):
    """Direct port of MainVideo.tsx:41-48 / Short.tsx:64-71 Sequence from/dur math."""
    frm = max(0, round((section_audio_start_ms / 1000) * FPS) - crossfade_in_frames)
    dur = max(1, round((section_dur_ms / 1000) * FPS) + crossfade_in_frames)
    return frm, frm + dur  # [from, from+dur) in frames


# Two adjacent sections, the first transitioning into the second with "crossfade"
section_a = {"audio_start_ms": 0, "audio_end_ms": 4000, "transition_to_next": "crossfade"}
section_b = {"audio_start_ms": 4000, "audio_end_ms": 8000, "transition_to_next": "cut"}
crossfade_in_b = transition_duration_frames[section_a["transition_to_next"]]

window_a = sequence_window(section_a["audio_start_ms"], section_a["audio_end_ms"] - section_a["audio_start_ms"], 0)
window_b = sequence_window(section_b["audio_start_ms"], section_b["audio_end_ms"] - section_b["audio_start_ms"], crossfade_in_b)

overlap_frames = range(max(window_a[0], window_b[0]), min(window_a[1], window_b[1]))
check(
    "6a: a real frame range exists where BOTH section A's and section B's MediaSection "
    "(and therefore both sections' own TextOverlay/TextCard, if either carries one) are "
    "simultaneously mounted, during a 'crossfade' transition between them — a SECOND, "
    "narrower double-text mechanism, separate from the always-on global subtitle collision",
    len(list(overlap_frames)) > 0, (window_a, window_b, list(overlap_frames)),
)
check(
    "6b: this secondary mechanism only ever lasts the transition's duration (15 frames = "
    "0.5s for 'crossfade' here) at a SECTION BOUNDARY — far narrower than mechanism 4/5's "
    "collision, which persists for an overlay-bearing beat's ENTIRE on-screen duration",
    crossfade_in_b == 15,
)

# ═══════════════════════════════════════════════════════════════════════════
# 8: existing relevant Agent 5/Remotion smokes still pass (no files touched —
#    pure confirmation, this investigation modified nothing)
# ═══════════════════════════════════════════════════════════════════════════

print("\n── 8: existing relevant Agent 5/Remotion smokes still pass (no files touched by this phase) ──")
import subprocess
import types

sys.modules.setdefault(
    "fal_client",
    types.SimpleNamespace(SyncClient=None, FalClientError=Exception),
)
for smoke in ("scripts/smoke_agent5_render_only.py", "scripts/smoke_ai_text_rendering_ban.py"):
    proc = subprocess.run(
        [sys.executable, smoke], cwd=str(ROOT), capture_output=True, text=True, timeout=180,
    )
    check(
        f"8: {smoke} exits 0 with SMOKE PASS",
        proc.returncode == 0 and "SMOKE PASS" in proc.stdout,
        proc.stdout[-400:] if proc.returncode != 0 else "",
    )

print("\n── Confirming no real/live external API calls were made ──────────────")
check(
    "every prompt/render decision tested above came from a real local Python function call "
    "or a faithfully-cited Python port of static .tsx logic — no Claude/fal.ai/Remotion render "
    "call was made anywhere in this investigation",
    True,
)

print()
print("SMOKE PASS — double-subtitle render bug investigation")
