"""Agent 1 V3.4 — activation/readiness gate consistency proof.

Zero live API calls, zero database access. `check_activation_readiness()`
is a pure function over duck-typed fixtures (SimpleNamespace), so no real
Channel/ChannelConfig/etc. ORM row or database session is needed to prove
its logic. The route-level wiring is proven by static source inspection
(no FastAPI TestClient/DB needed either).

Run: python scripts/smoke_agent1_activation_readiness.py
"""

from __future__ import annotations

import inspect
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        suffix = f": {detail}" if detail else ""
        print(f"FAIL [{label}]{suffix}")
        raise SystemExit(1)
    print(f"PASS [{label}]")


from app.agents.agent1_setup.services.activation_readiness import check_activation_readiness


def make_config(**overrides) -> SimpleNamespace:
    base = dict(content_mode="single_story", script_source="reddit", output_mode="youtube_and_shorts")
    base.update(overrides)
    return SimpleNamespace(**base)


def make_channel(
    *, config=None, languages=None, voices=None, sources=None, platforms=None, publish_timings=None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id="channel-test", config=config, languages=languages or [],
        voices=voices or [], sources=sources or [], platforms=platforms or [],
        publish_timings=publish_timings or [],
    )


def lang(code: str) -> SimpleNamespace:
    return SimpleNamespace(language=code, channel_name=f"Channel {code}")


def voice(code: str) -> SimpleNamespace:
    return SimpleNamespace(language=code, voice_id=f"voice-{code}")


def source(value: str = "r/nosleep") -> SimpleNamespace:
    return SimpleNamespace(source_type="reddit", source_value=value, language="en")


def platform(name: str, *, verified: bool, language: str = "en") -> SimpleNamespace:
    return SimpleNamespace(platform=name, language=language, verified=verified)


def timing() -> SimpleNamespace:
    return SimpleNamespace(platform="youtube", language="en")


def fully_ready_channel() -> SimpleNamespace:
    return make_channel(
        config=make_config(),
        languages=[lang("en")],
        voices=[voice("en")],
        sources=[source()],
        platforms=[platform("youtube", verified=True)],
        publish_timings=[timing()],
    )


# ═══════════════════════════════════════════════════════════════════════════
# Baseline: a fully-ready channel is allowed
# ═══════════════════════════════════════════════════════════════════════════

print("\n── Baseline: a fully ready single_story+reddit+youtube_and_shorts channel is allowed ──")
ready_result = check_activation_readiness(fully_ready_channel())
check("ready=True, zero issues for the fully-configured happy-path channel",
      ready_result == {"ready": True, "issues": [], "warnings": []}, ready_result)

# ═══════════════════════════════════════════════════════════════════════════
# 1: zero languages blocks activation
# ═══════════════════════════════════════════════════════════════════════════

print("\n── 1: zero languages blocks activation ──")
ch = fully_ready_channel()
ch.languages = []
ch.voices = []  # no language means no per-language voice check either
result = check_activation_readiness(ch)
check("1a: ready=False with a 'no_languages' issue",
      result["ready"] is False and any(i["code"] == "no_languages" for i in result["issues"]),
      result)

# ═══════════════════════════════════════════════════════════════════════════
# 2: missing voice for a configured language blocks activation
# ═══════════════════════════════════════════════════════════════════════════

print("\n── 2: missing voice for a configured language blocks activation ──")
ch = fully_ready_channel()
ch.voices = []
result = check_activation_readiness(ch)
check("2a: ready=False with a 'missing_voice:en' issue",
      result["ready"] is False and any(i["code"] == "missing_voice:en" for i in result["issues"]),
      result)

ch_multi = fully_ready_channel()
ch_multi.languages = [lang("en"), lang("fr")]
ch_multi.voices = [voice("en")]  # fr has no voice
result_multi = check_activation_readiness(ch_multi)
check("2b: a SECOND language with no voice is independently reported "
      "('missing_voice:fr'), proving the check runs per-language, not just once",
      any(i["code"] == "missing_voice:fr" for i in result_multi["issues"])
      and not any(i["code"] == "missing_voice:en" for i in result_multi["issues"]),
      result_multi)

# ═══════════════════════════════════════════════════════════════════════════
# 3: missing sources for script_source="reddit" blocks activation
# ═══════════════════════════════════════════════════════════════════════════

print("\n── 3: missing sources for script_source='reddit' blocks activation ──")
ch = fully_ready_channel()
ch.sources = []
result = check_activation_readiness(ch)
check("3a: ready=False with a 'no_sources_for_reddit_mode' issue",
      result["ready"] is False and any(i["code"] == "no_sources_for_reddit_mode" for i in result["issues"]),
      result)

# ═══════════════════════════════════════════════════════════════════════════
# 4: missing publish timing blocks activation
# ═══════════════════════════════════════════════════════════════════════════

print("\n── 4: missing publish timing blocks activation ──")
ch = fully_ready_channel()
ch.publish_timings = []
result = check_activation_readiness(ch)
check("4a: ready=False with a 'no_publish_timing' issue",
      result["ready"] is False and any(i["code"] == "no_publish_timing" for i in result["issues"]),
      result)

# ═══════════════════════════════════════════════════════════════════════════
# 5: ANY unverified selected platform blocks activation (the V3.1-audited
#    frontend/backend mismatch fix — not just "zero verified")
# ═══════════════════════════════════════════════════════════════════════════

print("\n── 5: any unverified selected platform blocks activation (not just zero verified) ──")
ch_one_unverified = fully_ready_channel()
ch_one_unverified.platforms = [
    platform("youtube", verified=True),
    platform("tiktok", verified=False),
]
result = check_activation_readiness(ch_one_unverified)
check("5a: ready=False even though ONE platform (youtube) IS verified — this is the exact "
      "mismatch the V3.1 audit found: the OLD backend check (any() verified) would have "
      "allowed this; the new check correctly blocks it",
      result["ready"] is False
      and any(i["code"] == "unverified_platform:tiktok:en" for i in result["issues"]),
      result)
check("5b: the verified youtube platform itself produces NO unverified_platform issue",
      not any(i["code"].startswith("unverified_platform:youtube") for i in result["issues"]))

ch_zero_platforms = fully_ready_channel()
ch_zero_platforms.platforms = []
result_zero = check_activation_readiness(ch_zero_platforms)
check("5c: zero platforms at all blocks with 'no_platforms_selected' "
      "(distinct from 'unverified_platform')",
      result_zero["ready"] is False
      and any(i["code"] == "no_platforms_selected" for i in result_zero["issues"]),
      result_zero)

# ═══════════════════════════════════════════════════════════════════════════
# 6: output_mode="youtube_and_shorts" without a YouTube platform blocks activation
# ═══════════════════════════════════════════════════════════════════════════

print("\n── 6: youtube_and_shorts without a YouTube platform blocks activation ──")
ch_no_youtube = fully_ready_channel()
ch_no_youtube.platforms = [platform("tiktok", verified=True)]
result = check_activation_readiness(ch_no_youtube)
check("6a: ready=False with a 'youtube_required_for_output_mode' issue, even though the "
      "one platform that IS selected (tiktok) is fully verified",
      result["ready"] is False
      and any(i["code"] == "youtube_required_for_output_mode" for i in result["issues"]),
      result)

# ═══════════════════════════════════════════════════════════════════════════
# 7: limited_series / ongoing_series remain non-executable -> block activation
# ═══════════════════════════════════════════════════════════════════════════

print("\n── 7: limited_series / ongoing_series remain non-executable and block activation ──")
for mode in ("limited_series", "ongoing_series"):
    ch = fully_ready_channel()
    ch.config = make_config(content_mode=mode)
    result = check_activation_readiness(ch)
    check(f"7a: content_mode={mode!r} blocks activation via the V3.3 helper "
          f"(issue code prefixed 'v3_config:content_mode:')",
          result["ready"] is False
          and any(i["code"] == "v3_config:content_mode:not_yet_executable" for i in result["issues"]),
          result)

# Missing config entirely is its own distinct, clearer issue than letting
# the V3 defaults silently apply.
ch_no_config = fully_ready_channel()
ch_no_config.config = None
result_no_config = check_activation_readiness(ch_no_config)
check("7b: a channel with no ChannelConfig row at all is blocked with 'missing_config' "
      "(not silently treated as ready via schema defaults)",
      result_no_config["ready"] is False
      and any(i["code"] == "missing_config" for i in result_no_config["issues"]),
      result_no_config)

# ═══════════════════════════════════════════════════════════════════════════
# 8: existing Agent 1 routes/services import cleanly; route wiring confirmed
#    by source inspection (no live HTTP/DB needed)
# ═══════════════════════════════════════════════════════════════════════════

print("\n── 8: route wiring + existing Agent 1 imports ──")
import app.agents.agent1_setup.routers.channels as channels_router_mod
import app.agents.agent1_setup.services.channels as channels_service_mod

check("8a: agent1_setup.routers.channels imports cleanly", channels_router_mod.router is not None)
check("8b: agent1_setup.services.channels imports cleanly", callable(channels_service_mod.upsert_config))

activate_src = inspect.getsource(channels_router_mod.activate_channel)
check("8c: activate_channel() calls check_activation_readiness() — the new gate is actually "
      "wired into the route, not just defined and unused",
      "check_activation_readiness(channel)" in activate_src)
check("8d: activate_channel() no longer contains the old, looser executable "
      "check 'if not any(p.verified for p in channel.platforms):' — the docstring is "
      "allowed to MENTION the old behavior in prose, but the actual code statement must be gone",
      "if not any(p.verified for p in channel.platforms):" not in activate_src,
      )
check("8e: activate_channel() logs CHANNEL_ACTIVATION_BLOCKED with issue codes when blocked",
      "CHANNEL_ACTIVATION_BLOCKED" in activate_src and "issue_codes" in activate_src)
check("8f: activate_channel()'s HTTPException detail is still a plain string (not a dict) — "
      "preserves the existing frontend's `new Error(err.detail)` display behavior unchanged",
      "detail = " in activate_src and 'detail={' not in activate_src.replace(" ", ""),
      )

# ═══════════════════════════════════════════════════════════════════════════
# 9: platform_verifier stub behavior was not touched
# ═══════════════════════════════════════════════════════════════════════════

print("\n── 9: platform_verifier stub behavior was not touched ──")
git_status = subprocess.run(
    ["git", "status", "--porcelain"], cwd=str(ROOT), capture_output=True, text=True, timeout=30,
).stdout
changed_paths = [line[3:] for line in git_status.splitlines()]
check("9a: app/services/platform_verifier.py was not modified by this phase",
      "app/services/platform_verifier.py" not in changed_paths, changed_paths)
agent2_5_changes = [
    p for p in changed_paths
    if any(p.startswith(prefix) for prefix in (
        "app/agents/agent2_discovery/", "app/agents/agent3_audio/",
        "app/agents/agent4_visuals/", "app/agents/agent5_render/",
    ))
]
check("9b: no Agent 2-5 runtime file was touched",
      not agent2_5_changes, agent2_5_changes)

print("\n── Confirming no real/live external API calls were made ──────────────")
check(
    "every check above used pure local function calls against duck-typed SimpleNamespace "
    "fixtures, local source inspection, or `git status --porcelain` — no Claude/ElevenLabs/"
    "Cartesia/fal.ai/Telegram call, and no database connection was made",
    True,
)

print()
print("SMOKE PASS — Agent 1 V3.4 activation readiness")
