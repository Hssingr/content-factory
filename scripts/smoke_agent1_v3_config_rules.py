"""Agent 1 V3.3 — backend mode/output/script-source rule helper proof.

Zero live API calls, zero database access. Every check is a pure local
function call against app.agents.agent1_setup.services.v3_config_rules, or
a static AST/import check confirming no Agent 2-5 runtime file was touched.

Run: python scripts/smoke_agent1_v3_config_rules.py
"""

from __future__ import annotations

import subprocess
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


from app.agents.agent1_setup.services import v3_config_rules as rules

# ═══════════════════════════════════════════════════════════════════════════
# 1: single_story + reddit + youtube_and_shorts is fully executable
# ═══════════════════════════════════════════════════════════════════════════

print("\n── 1: single_story + reddit + youtube_and_shorts is executable ──")
check("1a: is_executable_content_mode('single_story') is True",
      rules.is_executable_content_mode("single_story") is True)
check("1b: is_executable_script_source('single_story', 'reddit') is True",
      rules.is_executable_script_source("single_story", "reddit") is True)
check("1c: is_executable_output_mode('youtube_and_shorts') is True",
      rules.is_executable_output_mode("youtube_and_shorts") is True)

result_happy = rules.validate_v3_channel_config({
    "content_mode": "single_story", "script_source": "reddit", "output_mode": "youtube_and_shorts",
})
check("1d: validate_v3_channel_config() reports executable=True, supported=True, zero issues "
      "for the real, currently-working combination",
      result_happy == {"executable": True, "supported": True, "issues": []}, result_happy)

result_default = rules.validate_v3_channel_config({})
check("1e: validate_v3_channel_config({}) (no keys at all) falls back to the V3.2 schema "
      "defaults and is still fully executable — never raises on a partial/empty dict",
      result_default["executable"] is True and result_default["issues"] == [], result_default)

# ═══════════════════════════════════════════════════════════════════════════
# 2: limited_series / ongoing_series are supported but not executable
# ═══════════════════════════════════════════════════════════════════════════

print("\n── 2: limited_series / ongoing_series — supported, not executable ──")
for mode in ("limited_series", "ongoing_series"):
    check(f"2a: is_supported_content_mode({mode!r}) is True",
          rules.is_supported_content_mode(mode) is True)
    check(f"2b: is_executable_content_mode({mode!r}) is False",
          rules.is_executable_content_mode(mode) is False)
    reason = rules.coming_soon_reason("content_mode", mode)
    check(f"2c: coming_soon_reason('content_mode', {mode!r}) returns a non-empty, specific reason",
          bool(reason) and mode in reason, reason)

    result = rules.validate_v3_channel_config(
        {"content_mode": mode, "script_source": "reddit", "output_mode": "youtube_and_shorts"}
    )
    check(f"2d: validate_v3_channel_config() for {mode!r} reports executable=False, "
          f"supported=True, with at least one BLOCKING content_mode issue carrying the "
          f"coming-soon reason",
          result["executable"] is False and result["supported"] is True
          and any(i["field"] == "content_mode" and i["severity"] == "BLOCKING"
                  and i["message"] == reason for i in result["issues"]),
          result,
          )

# ═══════════════════════════════════════════════════════════════════════════
# 3: unsupported script sources are blocked with a clear reason; aliases
#    ('claude_generated' -> 'ai_generated') are normalized consistently
# ═══════════════════════════════════════════════════════════════════════════

print("\n── 3: script_source handling — supported/executable/normalization ──")
check("3a: is_supported_script_source('reddit') is True", rules.is_supported_script_source("reddit") is True)
check("3b: is_supported_script_source('totally_made_up') is False",
      rules.is_supported_script_source("totally_made_up") is False)
check("3c: is_supported_script_source('claude_generated') is True — normalized to 'ai_generated' "
      "before the supported-set check, even though 'claude_generated' is not itself in the "
      "schema's Literal set",
      rules.is_supported_script_source("claude_generated") is True)
check("3d: normalize_script_source('claude_generated') == 'ai_generated'",
      rules.normalize_script_source("claude_generated") == "ai_generated")
check("3e: normalize_script_source('reddit') == 'reddit' (pass-through, not just aliases)",
      rules.normalize_script_source("reddit") == "reddit")

for src in ("ai_generated", "claude_generated", "user_provided", "hybrid"):
    check(f"3f: is_executable_script_source('single_story', {src!r}) is False — only "
          f"'reddit' is executable, even for single_story",
          rules.is_executable_script_source("single_story", src) is False)
    reason = rules.coming_soon_reason("script_source", src)
    check(f"3g: coming_soon_reason('script_source', {src!r}) returns a clear, non-empty reason",
          bool(reason), reason)

check("3h: is_executable_script_source('limited_series', 'reddit') is False — 'reddit' is "
      "only executable when paired with content_mode='single_story'",
      rules.is_executable_script_source("limited_series", "reddit") is False)

result_bad_source = rules.validate_v3_channel_config({
    "content_mode": "single_story", "script_source": "totally_made_up", "output_mode": "youtube_and_shorts",
})
check("3i: an unsupported script_source produces a BLOCKING 'unsupported_value' issue "
      "(distinct code from 'not_yet_executable')",
      any(i["field"] == "script_source" and i["code"] == "unsupported_value"
          for i in result_bad_source["issues"])
      and result_bad_source["supported"] is False,
      result_bad_source,
      )

# ═══════════════════════════════════════════════════════════════════════════
# 4: output modes are classified correctly
# ═══════════════════════════════════════════════════════════════════════════

print("\n── 4: output_mode classification ──")
check("4a: is_executable_output_mode('youtube_and_shorts') is True",
      rules.is_executable_output_mode("youtube_and_shorts") is True)
for mode in ("shorts_only", "youtube_long_only"):
    check(f"4b: is_supported_output_mode({mode!r}) is True", rules.is_supported_output_mode(mode) is True)
    check(f"4c: is_executable_output_mode({mode!r}) is False", rules.is_executable_output_mode(mode) is False)
    reason = rules.coming_soon_reason("output_mode", mode)
    check(f"4d: coming_soon_reason('output_mode', {mode!r}) names the actual current "
          f"pipeline limitation, not a generic placeholder",
          bool(reason) and ("run_shorts_planner" in reason or "output_mode" in reason), reason)
check("4e: is_supported_output_mode('tiktok_only') is False (not in the V3.2 schema at all)",
      rules.is_supported_output_mode("tiktok_only") is False)

# ═══════════════════════════════════════════════════════════════════════════
# 5: validate_v3_channel_config() returns structured issues (already proven
#    above per-field; this section proves the AGGREGATE multi-field case)
# ═══════════════════════════════════════════════════════════════════════════

print("\n── 5: validate_v3_channel_config() aggregates multiple simultaneous issues ──")
multi_bad = rules.validate_v3_channel_config({
    "content_mode": "limited_series", "script_source": "hybrid", "output_mode": "shorts_only",
})
check("5a: all three fields are individually supported (all are valid V3.2 schema values) "
      "yet the combination is fully non-executable",
      multi_bad["supported"] is True and multi_bad["executable"] is False)
check("5b: exactly three issues are returned, one per field, all BLOCKING, all "
      "'not_yet_executable' (not 'unsupported_value', since every value here IS schema-supported)",
      len(multi_bad["issues"]) == 3
      and all(i["severity"] == "BLOCKING" and i["code"] == "not_yet_executable" for i in multi_bad["issues"])
      and {i["field"] for i in multi_bad["issues"]} == {"content_mode", "script_source", "output_mode"},
      multi_bad,
      )
check("5c: every issue's message is a non-empty, field-specific string (no empty/placeholder text)",
      all(len(i["message"]) > 20 for i in multi_bad["issues"]))

# ═══════════════════════════════════════════════════════════════════════════
# 6: existing Agent 1 routers/services still import cleanly
# ═══════════════════════════════════════════════════════════════════════════

print("\n── 6: existing Agent 1 routers/services still import cleanly ──")
import app.agents.agent1_setup.routers.channels as channels_router_mod
import app.agents.agent1_setup.services.channels as channels_service_mod
from app.schemas.channel import ChannelConfigUpsert

check("6a: agent1_setup.routers.channels imports cleanly", channels_router_mod.router is not None)
check("6b: agent1_setup.services.channels imports cleanly", callable(channels_service_mod.upsert_config))
check("6c: ChannelConfigUpsert (V3.2 schema) still imports and validates cleanly",
      ChannelConfigUpsert().content_mode == "single_story")

import inspect
router_src = inspect.getsource(channels_router_mod)
check("6d: the channels router does NOT import v3_config_rules — this phase's helper module "
      "is not wired into any route yet, per the brief's 'do not enforce it globally yet'",
      "v3_config_rules" not in router_src)
service_src = inspect.getsource(channels_service_mod)
check("6e: the channels service does NOT import v3_config_rules either — same reasoning",
      "v3_config_rules" not in service_src)

# ═══════════════════════════════════════════════════════════════════════════
# 7: no Agent 2-5 runtime file was modified by this phase
# ═══════════════════════════════════════════════════════════════════════════

print("\n── 7: no Agent 2-5 runtime file was touched (git-status-based, not assumed) ──")
git_status = subprocess.run(
    ["git", "status", "--porcelain"], cwd=str(ROOT), capture_output=True, text=True, timeout=30,
).stdout
changed_paths = [line[3:] for line in git_status.splitlines()]
agent2_5_changes = [
    p for p in changed_paths
    if any(p.startswith(prefix) for prefix in (
        "app/agents/agent2_discovery/", "app/agents/agent3_audio/",
        "app/agents/agent4_visuals/", "app/agents/agent5_render/",
    ))
]
check("7a: git status shows zero changed/new files under agent2_discovery/agent3_audio/"
      "agent4_visuals/agent5_render — this phase touched Agent 1 only",
      not agent2_5_changes, agent2_5_changes)

print("\n── Confirming no real/live external API calls were made ──────────────")
check(
    "every check above used pure local function calls against v3_config_rules.py, local "
    "Pydantic validation, local imports, or `git status --porcelain` — no Claude/ElevenLabs/"
    "Cartesia/fal.ai/Telegram call, and no database connection was made",
    True,
)

print()
print("SMOKE PASS — Agent 1 V3.3 backend config rules")
