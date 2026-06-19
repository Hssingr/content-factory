"""Child short audio smoke test — zero API calls, zero DB access.

Verifies:
  1. prepare_script_for_tts importable with is_short_episode parameter.
  2. Short episode raises the pause cap: more '...' markers than long-form on a
     trigger-heavy script (8 reveal phrases > long-form cap of 6).
  3. Short episode skips the slow-open: first sentence unchanged for dramatic tone.
  4. Long-form slow-open fires on the same script (control check for assertion 3).
  5. generate_audio importable and accepts is_short_episode kwarg (signature check).
  6. run_audio_generation importable (confirms audio.py is not broken by edits).

Run: python scripts/smoke_standalone_shortb.py
Expected output: all lines prefixed with PASS, then SMOKE PASS
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def assert_ok(name: str, cond: bool, detail: str = "") -> None:
    if not cond:
        msg = f"FAIL [{name}]"
        if detail:
            msg += f": {detail}"
        print(msg)
        sys.exit(1)
    print(f"PASS [{name}]")


# ── 1. Import check ───────────────────────────────────────────────────────────

import inspect

from app.agents.agent3_audio.services.tts import prepare_script_for_tts, generate_audio
from app.agents.agent3_audio.services.audio import run_audio_generation

assert_ok("imports", True)

# ── 2. is_short_episode parameter exists on prepare_script_for_tts ───────────

sig = inspect.signature(prepare_script_for_tts)
assert_ok(
    "prepare_script_for_tts has is_short_episode param",
    "is_short_episode" in sig.parameters,
    f"params: {list(sig.parameters)}",
)

# ── 3. Pause cap raised for Short episodes ────────────────────────────────────
# Script has 8 reveal-trigger phrases. Long-form cap = 6 → 6 '...' max.
# Short episode cap = 10 → all 8 triggers fire → 8 '...' markers.
# Also: long-form adds 1 slow-open '...' for dramatic tone (total = 6, already at cap).
# Short-form: no slow-open, 8 reveal '...' markers.

_TRIGGER_SCRIPT = (
    "She disappeared without a trace. "
    "That changed everything for the family. "
    "Then the letter arrived at the police station. "
    "But nobody believed it at first. "
    "Until the detective opened the second envelope. "
    "What they found inside was beyond explanation. "
    "The answer had been sitting there the entire time. "
    "Then the final piece clicked into place. "
    "The truth shocked everyone in the room. "
    "It turned out the whole story had been a lie."
)

normal_out = prepare_script_for_tts(_TRIGGER_SCRIPT, "en", "dramatic", is_short_episode=False)
short_out  = prepare_script_for_tts(_TRIGGER_SCRIPT, "en", "dramatic", is_short_episode=True)

normal_count = normal_out.count("...")
short_count  = short_out.count("...")

assert_ok(
    "short episode has more pause markers than long-form",
    short_count > normal_count,
    f"short={short_count} normal={normal_count}; short_out snippet: {short_out[:120]!r}",
)

# ── 4. Short episode skips slow-open for dramatic tone ────────────────────────
# Script with a short first sentence (≤12 words) + dramatic tone.
# Long-form: first sentence gets "..." appended → "She was gone..."
# Short-form: first sentence unchanged → no "She was gone..."

_SLOW_OPEN_SCRIPT = "She was gone. Nobody knew where she had disappeared to."

normal_slow = prepare_script_for_tts(_SLOW_OPEN_SCRIPT, "en", "dramatic", is_short_episode=False)
short_slow  = prepare_script_for_tts(_SLOW_OPEN_SCRIPT, "en", "dramatic", is_short_episode=True)

assert_ok(
    "short episode does NOT apply slow-open on first sentence",
    "She was gone..." not in short_slow,
    f"unexpected slow-open in short: {short_slow[:80]!r}",
)

# ── 5. Long-form slow-open fires as control check ────────────────────────────

assert_ok(
    "long-form DOES apply slow-open on first sentence (control)",
    "She was gone..." in normal_slow,
    f"expected slow-open in normal: {normal_slow[:80]!r}",
)

# ── 6. generate_audio accepts is_short_episode kwarg ─────────────────────────

sig_audio = inspect.signature(generate_audio)
assert_ok(
    "generate_audio has is_short_episode param",
    "is_short_episode" in sig_audio.parameters,
    f"params: {list(sig_audio.parameters)}",
)

# ── Done ──────────────────────────────────────────────────────────────────────

print()
print("SMOKE PASS")
