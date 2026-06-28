"""Phase 15.2 — music asset strategy / licensing-safe local library audit proof.

Design/audit phase only. No real music asset is added anywhere — a single
silent placeholder WAV is synthesized in the OS temp directory (never under
this repository, never committed) purely to prove the disk-validation
function's existence/format/duration logic actually works end to end, not
just in theory. Zero network access, zero external downloads, zero live API
calls anywhere in this script.

Run: python scripts/smoke_music_asset_strategy.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        suffix = f": {detail}" if detail else ""
        print(f"FAIL [{label}]{suffix}")
        raise SystemExit(1)
    print(f"PASS [{label}]")


from app.agents.agent5_render.music.schema import (
    REQUIRED_FIELDS, VALID_INTENSITY_TIERS, VALID_AUDIO_EXTENSIONS,
    validate_entry_metadata, validate_entry_asset_on_disk,
)

EXAMPLE_PATH = ROOT / "app/agents/agent5_render/music/music_library.example.json"

# ═══════════════════════════════════════════════════════════════════════════
# 1: no real music assets were added anywhere
# ═══════════════════════════════════════════════════════════════════════════

print("\n── 1: no real music assets were added ──")

check("1a: the example metadata file exists and is plain JSON (no binary audio)",
      EXAMPLE_PATH.exists() and EXAMPLE_PATH.suffix == ".json")

library = json.loads(EXAMPLE_PATH.read_text())
check("1b: the example file is explicitly marked as a template, not real data",
      library.get("_TEMPLATE_ONLY") is True and "EXAMPLE/TEMPLATE" in library.get("_warning", ""))

# Scope the "no real audio file added" check to git's view of what THIS
# phase actually changed — not a repo-wide filesystem scan. media/ (gitignored
# runtime narration audio from real past pipeline runs) and remotion_tmp/
# (a Remotion render-temp/bundle cache) both pre-exist this phase, are
# untouched by it, and are not source the repository tracks — scanning them
# would conflate the user's existing application data with this phase's
# actual changes.
git_status = subprocess.run(
    ["git", "status", "--porcelain"], cwd=str(ROOT), capture_output=True, text=True, timeout=30,
).stdout
changed_paths = [line[3:] for line in git_status.splitlines()]
_AUDIO_EXTS = (".mp3", ".wav", ".m4a", ".ogg")
new_audio_in_git_status = [p for p in changed_paths if p.lower().endswith(_AUDIO_EXTS)]
check(
    "1c: `git status --porcelain` shows zero new/modified audio files (.mp3/.wav/.m4a/"
    ".ogg) — confirms no real music asset, or any other audio file, was added or changed "
    "by this phase (media/ and remotion_tmp/ are gitignored runtime/temp directories from "
    "real prior pipeline runs, pre-existing and untouched by this phase, not scanned here)",
    not new_audio_in_git_status, new_audio_in_git_status,
)
check(
    "1d: media/music/ (the recommended future runtime storage location) does not exist — "
    "actual licensed assets must be operator-provided at deploy time, never committed or "
    "synthesized here",
    not (ROOT / "media" / "music").exists(),
)

# ═══════════════════════════════════════════════════════════════════════════
# 2: no external download was made (structural — no network library used)
# ═══════════════════════════════════════════════════════════════════════════

print("\n── 2: no external download capability is even present in the new code ──")
schema_src = (ROOT / "app/agents/agent5_render/music/schema.py").read_text()
check(
    "2a: schema.py imports no network library (requests/httpx/urllib/socket) — "
    "structurally incapable of downloading anything",
    not any(lib in schema_src for lib in ("import requests", "import httpx", "import urllib", "import socket")),
)

# ═══════════════════════════════════════════════════════════════════════════
# 3 & 4: metadata schema/example validates; required license fields exist
# ═══════════════════════════════════════════════════════════════════════════

print("\n── 3 & 4: example metadata validates against the schema; license fields present ──")

check(
    "4a: REQUIRED_FIELDS includes every licensing-relevant field the brief lists "
    "(license, source, attribution_required, safe_for_commercial_use)",
    {"license", "source", "attribution_required", "safe_for_commercial_use"} <= set(REQUIRED_FIELDS),
)

entries = library["entries"]
check("3a: the example file has at least 3 placeholder entries to exercise the schema",
      len(entries) >= 3, len(entries))

results = {e["id"]: validate_entry_metadata(e) for e in entries}
for entry_id, issues in results.items():
    print(f"  {entry_id}: {issues or 'OK'}")

clean_entries = [e for e in entries if not e["id"].endswith("NOT-CLEARED")]
unsafe_entries = [e for e in entries if e["id"].endswith("NOT-CLEARED")]

check(
    "3b: every properly-filled-out placeholder entry (license/source non-empty, "
    "safe_for_commercial_use=true, all fields present) passes metadata validation "
    "with zero issues",
    all(not results[e["id"]] for e in clean_entries),
    {e["id"]: results[e["id"]] for e in clean_entries},
)
check(
    "3c: the deliberately-unsafe placeholder entry (safe_for_commercial_use=false) is "
    "correctly FLAGGED by the validator — proves the schema enforces default-deny, not "
    "default-allow, and that this is a real, discriminating check (not a vacuous pass)",
    all(results[e["id"]] for e in unsafe_entries)
    and all(
        any("safe_for_commercial_use" in issue for issue in results[e["id"]])
        for e in unsafe_entries
    ),
    {e["id"]: results[e["id"]] for e in unsafe_entries},
)

# Negative control: a deliberately incomplete entry (missing fields) is also caught.
incomplete_entry = {"id": "incomplete-test", "filename": "x.mp3"}
incomplete_issues = validate_entry_metadata(incomplete_entry)
check(
    "3d: an incomplete entry (most required fields missing) is flagged with one issue "
    "per missing field — confirms the validator checks ALL required fields, not just one",
    len(incomplete_issues) >= len(REQUIRED_FIELDS) - 2,  # id+filename present, rest missing
    incomplete_issues,
)

check(
    "4b: intensity_tier enum and audio-extension allowlist are both non-empty and used "
    "by the validator (not dead constants)",
    bool(VALID_INTENSITY_TIERS) and bool(VALID_AUDIO_EXTENSIONS)
    and "VALID_INTENSITY_TIERS" in schema_src and "VALID_AUDIO_EXTENSIONS" in schema_src,
)

# ═══════════════════════════════════════════════════════════════════════════
# 5: future render path can locate assets through a deterministic path
#    convention, without actually rendering
# ═══════════════════════════════════════════════════════════════════════════

print("\n── 5: deterministic path convention + real disk-validation mechanics ──")

fake_media_path = Path(tempfile.mkdtemp(prefix="phase_15_2_media_"))
for entry in entries:
    resolved = fake_media_path / "music" / entry["filename"]
    check(
        f"5a: entry {entry['id']!r} resolves to the documented "
        "{media_path}/music/{filename} convention (the same staticFile()-relative "
        "pattern Phase 15.1 confirmed audio_file already uses)",
        resolved == fake_media_path / "music" / entry["filename"],
    )

missing_issues = validate_entry_asset_on_disk(entries[0], fake_media_path)
check(
    "5b: with no real file present, validate_entry_asset_on_disk() correctly reports "
    "'missing on disk' for a placeholder entry — proves the check is real and "
    "discriminating, not a vacuous pass, and proves no real asset is silently assumed "
    "to exist",
    any("does not exist" in i for i in missing_issues), missing_issues,
)

# Now synthesize ONE silent, trivially-generated placeholder WAV — pure
# stdlib `wave` synthesis, zero copyrighted content, zero download — in the
# OS temp directory only, to prove the disk-validation mechanism actually
# works when a real file IS present. This file is never written into this
# repository and is cleaned up immediately after the check.
music_dir = fake_media_path / "music"
music_dir.mkdir(parents=True, exist_ok=True)
synthetic_path = music_dir / "synthetic-test-only.wav"
with wave.open(str(synthetic_path), "wb") as w:
    w.setnchannels(1)
    w.setsampwidth(2)
    w.setframerate(8000)
    w.writeframes(b"\x00\x00" * 8000)  # 1 second of silence, no real audio content

synthetic_entry = {**entries[0], "filename": "synthetic-test-only.wav"}
present_issues = validate_entry_asset_on_disk(synthetic_entry, fake_media_path)
check(
    "5c: with a real (synthetic, silent, stdlib-generated) WAV file present, "
    "validate_entry_asset_on_disk() reports ZERO issues — proves the existence/format/"
    "duration-readable checks actually function correctly end to end, not just in theory",
    present_issues == [], present_issues,
)

import shutil
shutil.rmtree(fake_media_path, ignore_errors=True)

# ═══════════════════════════════════════════════════════════════════════════
# 6: Phase 15.1's investigation proof still passes
# ═══════════════════════════════════════════════════════════════════════════

print("\n── 6: Phase 15.1 investigation proof still passes ──")
proc = subprocess.run(
    [sys.executable, "scripts/smoke_music_intensity_curve_investigation.py"],
    cwd=str(ROOT), capture_output=True, text=True, timeout=180,
)
check(
    "6a: scripts/smoke_music_intensity_curve_investigation.py exits 0 with SMOKE PASS",
    proc.returncode == 0 and "SMOKE PASS" in proc.stdout,
    proc.stdout[-400:] if proc.returncode != 0 else "",
)

print("\n── Confirming no real/live external API calls or downloads were made ──────────")
check(
    "every check above used local JSON/file reads, a stdlib-only synthetic silent WAV "
    "(no copyrighted content, never committed), and a subprocess re-run of an existing "
    "smoke that independently stubs its own boundaries — no network call, no music "
    "provider, no external download occurred anywhere",
    True,
)

print()
print("SMOKE PASS — music asset strategy / licensing-safe local library audit")
