"""Smoke test for dead legacy parent-short cleanup.

Zero API calls, zero DB access.
Run: python scripts/smoke_dead_legacy_short_cleanup.py
"""

import importlib.util
import inspect
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
_failures = 0


def check(label: str, condition: bool) -> None:
    global _failures
    print(f"  [{PASS if condition else FAIL}] {label}")
    if not condition:
        _failures += 1


import app.agents.agent3_audio.services.audio as audio
import app.agents.agent5_render.services.video as video
from app.services.model_routing import MODEL_ROUTING

_src_audio = inspect.getsource(audio)
_src_process = inspect.getsource(video._process_language)
_src_run_renders = inspect.getsource(video._run_renders)
_src_run_short = inspect.getsource(video._run_short_render)

print("\n── Agent 3 legacy short audio cleanup ──")
check("breakpoints.py module is not importable", importlib.util.find_spec("app.agents.agent3_audio.services.breakpoints") is None)
check("generate_short_bookends removed", not hasattr(audio, "generate_short_bookends"))
check("semantic_splits not referenced in run_audio_generation", "semantic_splits" not in _src_audio)
check("bookends task removed from model routing", "bookends" not in MODEL_ROUTING)
check("semantic_splits task removed from model routing", "semantic_splits" not in MODEL_ROUTING)

print("\n── Parent vs child short render invariants ──")
check("parent _run_renders does not call render_short", "render_short(" not in _src_run_renders)
check("parent _run_renders does not create format=short", 'format="short"' not in _src_run_renders and "format='short'" not in _src_run_renders)
check("child path still builds short props", "build_short_props(" in _src_process and "is_short_episode" in _src_process)
check("child _run_short_render still calls render_short", "render_short(" in _src_run_short)
check("child _run_short_render still creates format=short", 'format="short"' in _src_run_short)

print()
if _failures:
    print(f"SMOKE FAIL - {_failures} assertion(s) failed")
    sys.exit(1)
print("SMOKE PASS - dead legacy short cleanup")
