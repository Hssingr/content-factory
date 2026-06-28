"""Music library metadata schema (Phase 15.2) — design/audit only.

Defines the REQUIRED metadata shape for a future local background-music
loop library and provides two deterministic, local-only validators:

  validate_entry_metadata()    — pure metadata-shape/licensing-field check,
                                  no file I/O, no network.
  validate_entry_asset_on_disk() — optional local filesystem check (does the
                                  referenced file exist, is it a supported
                                  format, is its duration readable). Local
                                  reads only — never a network call.

This module does not select, mix, duck, loop, or play any audio, and does
not call any music provider or generation API. Asset *selection* and
Remotion *wiring* are explicitly Phase 15.3+ work and are not implemented
here — see code_report/phase_15_2_music_asset_strategy.md for the full
design rationale and the licensing policy this schema enforces.

Storage convention (Phase 15.2 decision, not yet wired into any render
path): real, licensed audio files belong under
``{settings.media_path}/music/{filename}`` — the same runtime,
git-ignored, operator-populated convention already used for
``audio/``, ``video/``, and ``cache/`` (see CLAUDE.md §13). This metadata
schema/example file is repository-controlled (it contains no audio), but
the audio files themselves must never be committed.
"""

from __future__ import annotations

import wave
from pathlib import Path
from typing import TypedDict

# ── Schema ───────────────────────────────────────────────────────────────────

VALID_INTENSITY_TIERS: frozenset[str] = frozenset({"low", "medium", "high"})

# Open, not exhaustive — Phase 15.3 may extend this list. Validation only
# requires at least one non-empty tag per entry, not membership in this set;
# this set exists for documentation/consistency, not as a hard enum gate.
SUGGESTED_MOOD_TAGS: frozenset[str] = frozenset({
    "suspense", "dread", "aftermath", "neutral", "hopeful", "tense", "calm",
    "uneasy", "resolution",
})

VALID_AUDIO_EXTENSIONS: frozenset[str] = frozenset({".mp3", ".wav", ".m4a", ".ogg"})

# Every field a music-library entry must carry before it may ever be
# selected by a future Phase 15.3 routing step. None of these are optional —
# an entry missing any of them is not eligible for use, per the licensing
# policy (no unverified/undocumented asset may be used).
REQUIRED_FIELDS: tuple[str, ...] = (
    "id", "filename", "intensity_tier", "mood_tags", "bpm", "loopable",
    "duration_sec", "license", "source", "attribution_required",
    "safe_for_commercial_use",
)


class MusicLibraryEntry(TypedDict, total=False):
    id: str
    filename: str
    intensity_tier: str           # "low" | "medium" | "high"
    mood_tags: list[str]          # at least one tag, e.g. ["suspense", "tense"]
    bpm: float | None             # None when unknown — never invented
    loopable: bool
    duration_sec: float
    license: str                  # human-readable license name/terms, never empty
    source: str                   # provider/site/person + URL or contact, never empty
    attribution_required: bool
    safe_for_commercial_use: bool  # must be True for the entry to ever be used


# ── Metadata-shape + licensing-field validation (no I/O) ───────────────────

def validate_entry_metadata(entry: dict) -> list[str]:
    """Validate one music-library entry's metadata shape and licensing
    fields. Pure function — no file or network access.

    Returns a list of human-readable issue strings; an empty list means the
    entry is structurally complete and its licensing fields are present and
    non-empty. This does NOT verify that a license is legally valid or that
    the source is trustworthy — that remains a human/legal judgment per the
    licensing policy (code_report/phase_15_2_music_asset_strategy.md) — it
    only catches missing/malformed/unsafe-by-default metadata.
    """
    issues: list[str] = []

    for field in REQUIRED_FIELDS:
        if field not in entry:
            issues.append(f"missing required field: {field}")

    if "intensity_tier" in entry and entry["intensity_tier"] not in VALID_INTENSITY_TIERS:
        issues.append(
            f"intensity_tier must be one of {sorted(VALID_INTENSITY_TIERS)}, "
            f"got {entry.get('intensity_tier')!r}"
        )

    if "mood_tags" in entry:
        tags = entry["mood_tags"]
        if not isinstance(tags, list) or not tags or not all(isinstance(t, str) and t.strip() for t in tags):
            issues.append("mood_tags must be a non-empty list of non-empty strings")

    if "license" in entry and not str(entry.get("license") or "").strip():
        issues.append(
            "license must be a non-empty, documented string — no unverified/"
            "undocumented license is allowed (licensing policy)"
        )

    if "source" in entry and not str(entry.get("source") or "").strip():
        issues.append(
            "source must be a non-empty, traceable string — no untraceable "
            "asset is allowed (licensing policy)"
        )

    if "safe_for_commercial_use" in entry and entry.get("safe_for_commercial_use") is not True:
        issues.append(
            "safe_for_commercial_use must be explicitly true — an entry "
            "that is false, missing, or any non-boolean value must never "
            "be eligible for use (licensing policy: default-deny, not "
            "default-allow)"
        )

    if "loopable" in entry and not isinstance(entry["loopable"], bool):
        issues.append("loopable must be a boolean")

    if "attribution_required" in entry and not isinstance(entry["attribution_required"], bool):
        issues.append("attribution_required must be a boolean")

    if "duration_sec" in entry:
        duration = entry["duration_sec"]
        if not isinstance(duration, (int, float)) or isinstance(duration, bool) or duration <= 0:
            issues.append("duration_sec must be a positive number")

    if "bpm" in entry and entry["bpm"] is not None:
        bpm = entry["bpm"]
        if not isinstance(bpm, (int, float)) or isinstance(bpm, bool) or bpm <= 0:
            issues.append("bpm must be null or a positive number — never invented if unknown")

    if "filename" in entry:
        filename = str(entry.get("filename") or "")
        if not filename or Path(filename).suffix.lower() not in VALID_AUDIO_EXTENSIONS:
            issues.append(
                f"filename must be non-empty and use a supported extension "
                f"{sorted(VALID_AUDIO_EXTENSIONS)}, got {filename!r}"
            )

    return issues


# ── Optional local-filesystem validation (no network) ──────────────────────

def validate_entry_asset_on_disk(entry: dict, media_path: Path) -> list[str]:
    """Check whether the entry's referenced audio file actually exists under
    ``{media_path}/music/`` and, for WAV files, that its duration is
    readable via the stdlib ``wave`` module.

    Local filesystem reads only — never a network call, never raises on a
    missing file (returns an issue string instead), safe to call against a
    media_path that has no real music assets at all (every Phase 15.2
    example entry is expected to fail this check, since no real audio file
    exists yet — that is the correct, intentional result, not a bug).

    MP3/M4A/OGG duration reading is intentionally not implemented here — it
    would require a new audio-metadata dependency (e.g. ``mutagen``), which
    is a Phase 15.3+ decision, not this phase's. WAV is checked with the
    Python standard library only, to keep this validator dependency-free.
    """
    issues: list[str] = []
    filename = entry.get("filename")
    if not filename:
        return ["no filename to check"]

    path = (media_path / "music" / str(filename))

    if path.suffix.lower() not in VALID_AUDIO_EXTENSIONS:
        issues.append(f"unsupported audio format: {path.suffix!r}")

    if not path.exists():
        issues.append(f"file does not exist on disk: {path}")
        return issues

    if path.suffix.lower() == ".wav":
        try:
            with wave.open(str(path), "rb") as w:
                frames = w.getnframes()
                rate = w.getframerate()
                if rate <= 0 or frames <= 0:
                    issues.append("could not determine a valid duration from WAV header")
        except wave.Error as exc:
            issues.append(f"could not read WAV duration: {exc}")
    # else: mp3/m4a/ogg — existence/format checked above; duration trusted
    # from metadata until a Phase 15.3+ decision adds a real audio-metadata
    # dependency.

    return issues
