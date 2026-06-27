"""Smoke proof for Cartesia Sonic request formatting.

No live API calls. The Cartesia SDK class is replaced with a local fake that
captures the exact kwargs passed to client.tts.bytes(). Claude pause review is
stubbed to echo deterministic text.
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

ASSERTIONS = 0
CAPTURED_CALLS: list[dict] = []


def check(label: str, condition: bool) -> None:
    global ASSERTIONS
    ASSERTIONS += 1
    if not condition:
        raise AssertionError(label)


class FakeTTS:
    def bytes(self, **kwargs):
        CAPTURED_CALLS.append(kwargs)
        return b"fake-wav"


class FakeCartesia:
    def __init__(self, api_key=None):
        self.api_key = api_key
        self.tts = FakeTTS()


def make_voice(
    *,
    tts_model: str,
    emotion: str = "enthusiastic",
    speed_profile: str = "fast",
    speed_override: float | None = None,
    pronunciation_dict_id: str | None = None,
):
    return SimpleNamespace(
        provider="cartesia",
        voice_id="voice-test-id",
        tts_model=tts_model,
        emotion=emotion,
        speed_profile=speed_profile,
        speed_override=speed_override,
        cartesia_pronunciation_dict_id=pronunciation_dict_id,
    )


def run_cartesia(voice) -> dict:
    CAPTURED_CALLS.clear()
    tts._generate_cartesia_audio("Borrasca was written on the envelope.", voice)
    check("one Cartesia bytes request was sent", len(CAPTURED_CALLS) == 1)
    return CAPTURED_CALLS[0]


orig_cartesia = cartesia.Cartesia
orig_call_claude = tts.call_claude
orig_wav_to_mp3 = tts._wav_to_mp3
cartesia.Cartesia = FakeCartesia
tts.call_claude = lambda _system_prompt, user_message, **_kwargs: user_message
tts._wav_to_mp3 = lambda wav_bytes: b"fake-mp3:" + wav_bytes

try:
    legacy = run_cartesia(make_voice(tts_model="sonic-2", emotion="enthusiastic", speed_profile="fast"))
    check("sonic-2 keeps model id", legacy["model_id"] == "sonic-2")
    check("sonic-2 keeps old voice_id field", legacy["voice_id"] == "voice-test-id")
    check("sonic-2 keeps old controls key", "_experimental_voice_controls" in legacy)
    check("sonic-2 old speed is word label", legacy["_experimental_voice_controls"]["speed"] == "fast")
    check("sonic-2 old emotion is weighted list", legacy["_experimental_voice_controls"]["emotion"] == ["positivity:high"])
    check("sonic-2 does not send generation_config", "generation_config" not in legacy)
    check("sonic-2 does not send new voice object", "voice" not in legacy)

    modern = run_cartesia(
        make_voice(
            tts_model="sonic-3.5",
            emotion="dramatic",
            speed_profile="very_fast",
            speed_override=1.23,
            pronunciation_dict_id="pd_borrasca",
        )
    )
    check("sonic-3.5 keeps model id", modern["model_id"] == "sonic-3.5")
    check("sonic-3.5 uses new voice object", modern["voice"] == {"mode": "id", "id": "voice-test-id"})
    check("sonic-3.5 omits old voice_id field", "voice_id" not in modern)
    check("sonic-3.5 omits old controls key", "_experimental_voice_controls" not in modern)
    check("sonic-3.5 uses generation_config", set(modern["generation_config"]) == {"speed", "emotion"})
    check("sonic-3.5 emotion is a single value", isinstance(modern["generation_config"]["emotion"], str))
    check("sonic-3.5 emotion value mapped", modern["generation_config"]["emotion"] == "scared")
    check("sonic-3.5 speed is numeric", isinstance(modern["generation_config"]["speed"], float))
    check("sonic-3.5 speed uses override", modern["generation_config"]["speed"] == 1.23)
    check("pronunciation dictionary id included", modern["pronunciation_dict_id"] == "pd_borrasca")

    sonic3 = run_cartesia(make_voice(tts_model="sonic-3", emotion="enthusiastic", speed_profile="fast"))
    check("sonic-3 uses new request format", "generation_config" in sonic3 and "voice" in sonic3)
    check("sonic-3 emotion is a single value", sonic3["generation_config"]["emotion"] == "enthusiastic")
    check("sonic-3 speed is numeric from profile", sonic3["generation_config"]["speed"] == 1.05)
    check("pronunciation dictionary omitted when unset", "pronunciation_dict_id" not in sonic3)

    try:
        run_cartesia(make_voice(tts_model="sonic-unknown"))
    except ValueError as exc:
        unknown_error = str(exc)
    else:
        raise AssertionError("unsupported model did not fail")
    check("unknown model generation fails clearly", "Unsupported Cartesia TTS model" in unknown_error)
    check("unknown model is rejected before Cartesia call", CAPTURED_CALLS == [])
finally:
    cartesia.Cartesia = orig_cartesia
    tts.call_claude = orig_call_claude
    tts._wav_to_mp3 = orig_wav_to_mp3

print(f"SMOKE PASS - {ASSERTIONS} checks")
print(f"sonic_2_payload={legacy}")
print(f"sonic_35_payload={modern}")
print(f"sonic_3_payload={sonic3}")
print(f"unknown_model_error={unknown_error}")
