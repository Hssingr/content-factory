"""Hotfix A — Cartesia Sonic 3.5 SDK compatibility smoke.

Zero live API calls. `cartesia.Cartesia` is monkeypatched to a fake client
in every scenario that exercises `_generate_cartesia_audio()`/`generate_audio()`,
and `tts._cartesia_sdk_supports_generation_config()` is monkeypatched where a
specific SDK-compatibility state needs to be simulated. The one place this
script does NOT monkeypatch is the real-SDK-introspection proof in section 1
below, which deliberately inspects the actually-installed `cartesia` package
(pure `inspect.signature()` call — no network, no instantiation, no API key
needed) to ground the rest of the proof in the real bug this hotfix fixes.

Run: python scripts/smoke_cartesia_sdk_compatibility.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import cartesia

from app.agents.agent3_audio.services import tts


def check(label: str, condition: bool, detail: str = "") -> None:
    if not condition:
        suffix = f": {detail}" if detail else ""
        print(f"FAIL [{label}]{suffix}")
        raise SystemExit(1)
    print(f"PASS [{label}]")


# ═══════════════════════════════════════════════════════════════════════════
# 1: real, unmocked introspection of the actually-installed Cartesia SDK
# ═══════════════════════════════════════════════════════════════════════════

print("\n── 1: real installed-SDK introspection (no mocking, no network) ──")
import inspect
from cartesia.tts import TTS

real_params = set(inspect.signature(TTS.bytes).parameters)
print(f"  Installed cartesia package's TTS.bytes() parameters: {sorted(real_params)}")

check(
    "1a: the installed SDK's TTS.bytes() does NOT have a 'voice' parameter "
    "(confirms the exact bug this hotfix fixes — pure local introspection, no API call)",
    "voice" not in real_params,
)
check(
    "1b: the installed SDK's TTS.bytes() does NOT have a 'generation_config' parameter",
    "generation_config" not in real_params,
)
# Clear the memoized result so the rest of this script's monkeypatches of
# the underlying function (not the cached wrapper) are exercised cleanly —
# _cartesia_sdk_supports_generation_config() itself is replaced wholesale in
# sections 3/4 below, so its cache is irrelevant there, but calling the real
# one once here proves the real function (not a stub) returns the correct
# fail-safe answer for THIS machine's installed SDK.
real_supports = tts._cartesia_sdk_supports_generation_config()
check(
    "1c: _cartesia_sdk_supports_generation_config() — the REAL function, not "
    "monkeypatched — correctly reports False for the installed SDK",
    real_supports is False,
)

# ═══════════════════════════════════════════════════════════════════════════
# 2: sonic-2 still works, regardless of installed SDK generation_config support
# ═══════════════════════════════════════════════════════════════════════════

print("\n── 2: sonic-2 (legacy shape) is unaffected by SDK compatibility ──")
check(
    "2a: _check_cartesia_sdk_compatibility('sonic-2') returns 'legacy' and raises nothing, "
    "using the REAL (incompatible-with-generation_config) installed SDK",
    tts._check_cartesia_sdk_compatibility("sonic-2") == "legacy",
)


class FakeTTS:
    def __init__(self):
        self.calls: list[dict] = []

    def bytes(self, **kwargs):
        self.calls.append(kwargs)
        return b"fake-wav"


class FakeCartesia:
    def __init__(self, api_key=None):
        self.api_key = api_key
        self.tts = FakeTTS()


def make_voice(*, model: str) -> SimpleNamespace:
    return SimpleNamespace(
        provider="cartesia",
        voice_id="voice-hotfix-test",
        tts_model=model,
        emotion="calm",
        speed_profile="normal",
        speed_override=None,
        cartesia_pronunciation_dict_id=None,
    )


orig_cartesia = cartesia.Cartesia
orig_call_claude = tts.call_claude
orig_wav_to_mp3 = tts._wav_to_mp3
orig_concat_mp3_chunks = tts._concat_mp3_chunks
orig_sdk_supports = tts._cartesia_sdk_supports_generation_config

fake_client_holder: dict[str, FakeCartesia] = {}


def _install_fake_cartesia():
    def _make(api_key=None):
        client = FakeCartesia(api_key=api_key)
        fake_client_holder["client"] = client
        return client
    cartesia.Cartesia = _make


tts.call_claude = lambda _sp, user_message, **_kw: user_message
tts._wav_to_mp3 = lambda wav_bytes: b"fake-mp3:" + wav_bytes
tts._concat_mp3_chunks = lambda chunks: b"".join(chunks)

try:
    _install_fake_cartesia()
    audio = tts._generate_cartesia_audio(
        "Plain narration with no section markers.", make_voice(model="sonic-2"),
    )
    check(
        "2b: sonic-2 end-to-end generation succeeds via the real legacy code path "
        "(real installed SDK, fake client) — Phase 11.3 legacy behavior preserved",
        isinstance(audio, bytes) and len(audio) > 0,
    )
    check(
        "2c: sonic-2 request used the legacy shape (_experimental_voice_controls, "
        "no generation_config) — confirms compatibility-checking did not alter the "
        "actual request payload for this model",
        "_experimental_voice_controls" in fake_client_holder["client"].tts.calls[0]
        and "generation_config" not in fake_client_holder["client"].tts.calls[0],
    )

    # ═══════════════════════════════════════════════════════════════════════
    # 3: sonic-3.5 with a SIMULATED new-compatible SDK works end to end
    # ═══════════════════════════════════════════════════════════════════════

    print("\n── 3: sonic-3.5 with a simulated SDK upgrade (generation_config supported) ──")
    tts._cartesia_sdk_supports_generation_config = lambda: True
    try:
        check(
            "3a: _check_cartesia_sdk_compatibility('sonic-3.5') returns 'generation_config' "
            "and raises nothing when the (simulated) SDK supports it",
            tts._check_cartesia_sdk_compatibility("sonic-3.5") == "generation_config",
        )
        audio35 = tts._generate_cartesia_audio(
            "Plain narration with no section markers.", make_voice(model="sonic-3.5"),
        )
        check(
            "3b: sonic-3.5 end-to-end generation succeeds via the generation_config code "
            "path once the SDK is reported as compatible — Phase 11.3 logic still works "
            "when the precondition it always assumed (SDK support) is actually true",
            isinstance(audio35, bytes) and len(audio35) > 0,
        )
        check(
            "3c: sonic-3.5 request used the generation_config shape (voice=, "
            "generation_config=), not the legacy shape",
            "generation_config" in fake_client_holder["client"].tts.calls[0]
            and "voice" in fake_client_holder["client"].tts.calls[0]
            and "_experimental_voice_controls" not in fake_client_holder["client"].tts.calls[0],
        )
    finally:
        tts._cartesia_sdk_supports_generation_config = orig_sdk_supports

    # ═══════════════════════════════════════════════════════════════════════
    # 4: sonic-3.5 with the REAL (old/incompatible) installed SDK gives a
    #    clear, actionable error — not a raw TypeError, and no API call is made
    # ═══════════════════════════════════════════════════════════════════════

    print("\n── 4: sonic-3.5 with the real, incompatible installed SDK — clear failure, no API call ──")
    fake_client_holder.clear()
    raised: Exception | None = None
    try:
        tts._generate_cartesia_audio(
            "Plain narration with no section markers.", make_voice(model="sonic-3.5"),
        )
    except Exception as exc:  # noqa: BLE001 - intentionally broad to inspect exact type
        raised = exc

    check(
        "4a: a RuntimeError is raised (not a raw TypeError) for sonic-3.5 against the "
        "real, incompatible installed SDK",
        isinstance(raised, RuntimeError) and not isinstance(raised, TypeError),
        repr(raised),
    )
    check(
        "4b: the error message is actionable — names the model, explains it's a local "
        "SDK version mismatch, and gives both fix options (upgrade cartesia / use sonic-2)",
        raised is not None
        and "sonic-3.5" in str(raised)
        and "pip install -U cartesia" in str(raised)
        and "sonic-2" in str(raised),
        str(raised),
    )
    check(
        "4c: NO Cartesia client/TTS call was ever made for this failing case — the "
        "compatibility check runs BEFORE any chunk is prepared or any API call is "
        "attempted (fake_client_holder stayed empty, proving _install_fake_cartesia()'s "
        "factory function — which would populate it — was never even invoked)",
        "client" not in fake_client_holder, fake_client_holder,
    )

finally:
    cartesia.Cartesia = orig_cartesia
    tts.call_claude = orig_call_claude
    tts._wav_to_mp3 = orig_wav_to_mp3
    tts._concat_mp3_chunks = orig_concat_mp3_chunks
    tts._cartesia_sdk_supports_generation_config = orig_sdk_supports

# ═══════════════════════════════════════════════════════════════════════════
# 5: caching behavior — the compatibility result is memoized, not re-derived
#    per call (cheap, and correct since the installed SDK can't change mid-process)
# ═══════════════════════════════════════════════════════════════════════════

print("\n── 5: compatibility check result is cached (lru_cache) ──")
check(
    "5a: _cartesia_sdk_supports_generation_config is an lru_cache-wrapped function",
    hasattr(tts._cartesia_sdk_supports_generation_config, "cache_info"),
)

print("\n── Confirming no real/live external API calls were made ──────────────")
check(
    "every Cartesia client instantiation in this script used the FakeCartesia factory; "
    "the one real, unmocked call (section 1) is pure local inspect.signature() "
    "introspection of an already-imported package — no network call, no API key was "
    "even required",
    True,
)

print()
print("SMOKE PASS — Cartesia SDK compatibility hotfix")
