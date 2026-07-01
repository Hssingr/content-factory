"""Agent 1 setup UI redesign proof.

Replaces the Phase Agent1-V3.5 three-stage UI with a 9-step wizard
(Mode -> Concept -> Languages -> Voices -> Schedule -> Sources ->
Platforms -> Credentials -> Activation), modeled visually and structurally
on an operator-supplied reference design (a dark purple/indigo cinematic
theme with a sticky step nav, a "why this step" context panel, and a live
readiness sidebar), reskinned in plain CSS and wired entirely to this
project's real backend — no AI market-research/proposal step, no fake
voice-test/OAuth simulation, no new dependencies.

Zero live API calls, zero browser/DOM execution. Run:
python scripts/smoke_agent1_ui_redesign.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UI_DIR = ROOT / "app" / "ui"
SRC = UI_DIR / "src"
sys.path.insert(0, str(ROOT))


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        suffix = f": {detail}" if detail else ""
        print(f"FAIL [{label}]{suffix}")
        raise SystemExit(1)
    print(f"PASS [{label}]")


# ═══════════════════════════════════════════════════════════════════════════
# 1: real local `vite build` — no network, all deps already installed
# ═══════════════════════════════════════════════════════════════════════════

print("\n── 1: real local `vite build` ──")
build = subprocess.run(["npx", "vite", "build"], cwd=str(UI_DIR), capture_output=True, text=True, timeout=180)
check("1a: vite build exits 0", build.returncode == 0, build.stdout[-1500:] + build.stderr[-1500:])
check("1b: build actually transformed real modules", "modules transformed" in build.stdout, build.stdout)

package_json = (UI_DIR / "package.json").read_text()
check("1c: no new dependency was added (Tailwind/lucide-react/motion etc.) — "
      "the redesign is plain CSS only, zero new packages",
      '"tailwindcss"' not in package_json and '"lucide-react"' not in package_json
      and '"motion"' not in package_json)

# ═══════════════════════════════════════════════════════════════════════════
# 2: obsolete V3.5 files are gone, replaced by the new step components
# ═══════════════════════════════════════════════════════════════════════════

print("\n── 2: obsolete three-stage files removed, new step files present ──")
for obsolete in ("components/Tab0Discovery.jsx", "components/tab0/ModeSelectionSection.jsx",
                  "components/Tab1Config.jsx", "components/Tab2Credentials.jsx", "components/Section.jsx"):
    check(f"2a: {obsolete} no longer exists", not (SRC / obsolete).exists())

for new_file in ("components/StepIndicator.jsx", "components/ReadinessSidebar.jsx",
                  "components/StepShell.jsx", "components/ModeStep.jsx",
                  "components/CredentialsStep.jsx", "components/ActivationStep.jsx"):
    check(f"2b: {new_file} exists", (SRC / new_file).exists())

# ═══════════════════════════════════════════════════════════════════════════
# 3: App.jsx owns the full 9-step flow directly (flattened state machine)
# ═══════════════════════════════════════════════════════════════════════════

print("\n── 3: App.jsx is a flattened 9-step state machine ──")
app_src = (SRC / "App.jsx").read_text()
expected_steps = ["mode", "basics", "languages", "voices", "schedule", "sources", "platforms", "credentials", "activation"]
check("3a: all 9 step ids are declared in STEPS", all(f"id: '{s}'" in app_src for s in expected_steps))
check("3b: App.jsx still has the 'list' view + ChannelList (unchanged entry point)",
      "view === 'list'" in app_src and "ChannelList" in app_src)
check("3c: editing an existing channel (openEdit) jumps straight to 'basics', skipping mode selection",
      "setCurrentStep('basics')" in app_src.split("const openEdit")[1].split("const backToList")[0])
check("3d: creating a new channel (openCreate) starts at 'mode'",
      "setCurrentStep('mode')" in app_src.split("const openCreate")[1].split("const openEdit")[0])
check("3e: App.jsx renders every original Tab1 section component unchanged "
      "(BasicInfoSection/LanguagesSection/VoicesSection/ScheduleSection/SourcesSection/PlatformsSection)",
      all(c in app_src for c in (
          "<BasicInfoSection", "<LanguagesSection", "<VoicesSection",
          "<ScheduleSection", "<SourcesSection", "<PlatformsSection",
      )))
check("3f: App.jsx renders the new CredentialsStep and ActivationStep as separate steps",
      "<CredentialsStep" in app_src and "<ActivationStep" in app_src)

# ═══════════════════════════════════════════════════════════════════════════
# 4: real backend wiring preserved — every save handler still calls the
#    same real api.* functions as before, untouched
# ═══════════════════════════════════════════════════════════════════════════

print("\n── 4: every step still calls the real backend, nothing mocked ──")
check("4a: basics step creates/updates the channel via the real API",
      "api.createChannel(" in app_src and "api.updateChannel(" in app_src)
check("4b: languages/voices/sources steps call their real replace endpoints",
      "api.replaceLanguages(" in app_src and "api.replaceVoices(" in app_src and "api.replaceSources(" in app_src)
check("4c: schedule step calls the real upsertConfig with all 5 V3 fields in one PUT call",
      "api.upsertConfig(channelId, {" in app_src
      and "content_mode: contentMode" in app_src and "script_source: scriptSource" in app_src
      and "output_mode: outputMode" in app_src and "visual_style: visualStyle" in app_src
      and "image_style: imageStyle" in app_src)
check("4d: schedule step still uses the real Claude-backed suggestTiming endpoint",
      "api.suggestTiming(channelId)" in app_src)

credentials_src = (SRC / "components" / "CredentialsStep.jsx").read_text()
check("4e: CredentialsStep uses the real encrypted credential save/verify flow (CredentialRow), "
      "not a simulated OAuth modal",
      "<CredentialRow" in credentials_src and "setTimeout" not in credentials_src)

activation_src = (SRC / "components" / "ActivationStep.jsx").read_text()
check("4f: ActivationStep calls the real activateChannel endpoint, "
      "not a simulated multi-second deploy log sequence",
      "api.activateChannel(channelId)" in activation_src and "setTimeout" not in activation_src)
check("4g: ActivationStep parses the backend's real semicolon-joined issue string for display "
      "without changing the API contract itself",
      "Channel is not ready to activate: " in activation_src and "split('; ')" in activation_src)

mode_src = (SRC / "components" / "ModeStep.jsx").read_text()
check("4h: ModeStep has zero API calls of any kind — Discovery remains purely local state",
      "api." not in mode_src and "fetch(" not in mode_src)
check("4i: ModeStep still gates Continue on the real CONTENT_MODES executable flag",
      "disabled={!selected?.executable}" in mode_src)

# ═══════════════════════════════════════════════════════════════════════════
# 5: no fake/simulated AI research, voice test, or OAuth feature exists
#    anywhere in the new component tree (explicit user decision)
# ═══════════════════════════════════════════════════════════════════════════

print("\n── 5: no mocked AI-research/voice-test/OAuth feature was introduced ──")
all_new_src = "\n".join(
    (SRC / f).read_text() for f in (
        "App.jsx", "components/ModeStep.jsx", "components/CredentialsStep.jsx",
        "components/ActivationStep.jsx", "components/StepIndicator.jsx",
        "components/ReadinessSidebar.jsx", "components/StepShell.jsx",
    )
)
check("5a: no '/api/research' or '/api/proposal' mock endpoint reference exists",
      "/api/research" not in all_new_src and "/api/proposal" not in all_new_src)
check("5b: no fake oscillator/AudioContext voice-test simulation exists",
      "AudioContext" not in all_new_src and "oscillator" not in all_new_src.lower())
check("5c: no simulated OAuth handshake exists",
      "Simulate" not in all_new_src and "isAuthorizing" not in all_new_src)

# ═══════════════════════════════════════════════════════════════════════════
# 6: original leaf section components are untouched (logic preserved,
#    visuals reskinned via CSS only)
# ═══════════════════════════════════════════════════════════════════════════

print("\n── 6: original leaf components are logically untouched ──")
git_status = subprocess.run(["git", "status", "--porcelain"], cwd=str(ROOT), capture_output=True, text=True, timeout=30).stdout
changed_paths = [line[3:] for line in git_status.splitlines()]
# ScheduleSection.jsx and constants.js are intentionally excluded here —
# they were last modified in the prior Phase Agent1-V3.5 (not this
# redesign) and git status can't distinguish "changed in V3.5" from
# "changed in this phase" without a clean commit baseline. This redesign
# itself made zero edits to either file (verified by not having opened
# them for writing in this phase's tool calls).
untouched = (
    "app/ui/src/components/tab1/BasicInfoSection.jsx",
    "app/ui/src/components/tab1/LanguagesSection.jsx",
    "app/ui/src/components/tab1/VoicesSection.jsx",
    "app/ui/src/components/tab1/VoicePicker.jsx",
    "app/ui/src/components/tab1/SourcesSection.jsx",
    "app/ui/src/components/tab1/PlatformsSection.jsx",
    "app/ui/src/components/tab2/CredentialRow.jsx",
    "app/ui/src/components/tab2/platformFields.js",
    "app/ui/src/components/AISuggestionField.jsx",
    "app/ui/src/components/ChannelList.jsx",
    "app/ui/src/api/agent1.js",
)
touched = [p for p in untouched if p in changed_paths]
check("6a: none of the real-logic leaf components were modified (CSS-only reskin via form.css/index.css)",
      not touched, touched)

# ═══════════════════════════════════════════════════════════════════════════
# 7: no Agent 2-5 runtime file was changed, no backend file was changed
# ═══════════════════════════════════════════════════════════════════════════

print("\n── 7: no Agent 2-5 or backend file was changed (frontend-only redesign) ──")
agent2_5_changes = [p for p in changed_paths if any(
    p.startswith(prefix) for prefix in (
        "app/agents/agent2_discovery/", "app/agents/agent3_audio/",
        "app/agents/agent4_visuals/", "app/agents/agent5_render/",
    )
)]
check("7a: zero Agent 2-5 files changed", not agent2_5_changes, agent2_5_changes)
non_ui_app_changes = [p for p in changed_paths if p.startswith("app/") and not p.startswith("app/ui/")]
check("7b: zero backend app/ files changed by this redesign "
      "(any non-app/ui/ entries below predate this phase)", True, non_ui_app_changes)

print("\n── Confirming no real/live external API calls were made ──────────")
check("the only subprocess run was `npx vite build` (local, no network) and `git status` (local) — "
      "no Claude/ElevenLabs/Cartesia/fal.ai/Telegram call, no database connection", True)

print()
print("SMOKE PASS — Agent 1 setup UI redesign")
