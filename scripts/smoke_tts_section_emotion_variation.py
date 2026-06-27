"""Smoke proof for deterministic per-section Cartesia delivery variation.

No live API calls. Fake Cartesia captures the exact kwargs sent to
client.tts.bytes(); the Claude pause reviewer and WAV conversion are stubbed.
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


def make_voice(*, model: str, emotion: str = "calm", speed_profile: str = "normal"):
    return SimpleNamespace(
        provider="cartesia",
        voice_id="voice-section-test",
        tts_model=model,
        emotion=emotion,
        speed_profile=speed_profile,
        speed_override=None,
        cartesia_pronunciation_dict_id=None,
    )


LONG_SCRIPT = """[INTRO]
The house did not look abandoned. Then the window moved by itself.
[SECTION 1: The first warning]
They found scratches under the locked basement door. But nobody admitted hearing them.
[SECTION 3: The reveal]
The recording finally played. The missing child was whispering from inside the wall.
[OUTRO]
By morning, the house was silent again. Nobody tried to open the basement door.
"""


def run_cartesia(script: str, voice, *, is_short_episode: bool = False) -> list[dict]:
    CAPTURED_CALLS.clear()
    tts._generate_cartesia_audio(script, voice, is_short_episode=is_short_episode)
    return list(CAPTURED_CALLS)


orig_cartesia = cartesia.Cartesia
orig_call_claude = tts.call_claude
orig_wav_to_mp3 = tts._wav_to_mp3
orig_concat_mp3_chunks = tts._concat_mp3_chunks
cartesia.Cartesia = FakeCartesia
tts.call_claude = lambda _system_prompt, user_message, **_kwargs: user_message
tts._wav_to_mp3 = lambda wav_bytes: b"fake-mp3:" + wav_bytes
tts._concat_mp3_chunks = lambda chunks: b"".join(chunks)

try:
    sonic35_calls = run_cartesia(LONG_SCRIPT, make_voice(model="sonic-3.5", emotion="calm"))
    check("long-form marked script emits one request per section", len(sonic35_calls) == 4)
    intro35, buildup35, reveal35, outro35 = sonic35_calls
    check("INTRO differs from reveal on sonic-3.5", intro35["generation_config"] != reveal35["generation_config"])
    check("INTRO receives curious sonic-3.5 emotion", intro35["generation_config"]["emotion"] == "curious")
    check("INTRO receives restrained numeric speed", intro35["generation_config"]["speed"] == 0.85)
    check("early buildup receives tense/scared sonic-3.5 emotion", buildup35["generation_config"]["emotion"] == "scared")
    check("reveal receives scared sonic-3.5 emotion", reveal35["generation_config"]["emotion"] == "scared")
    check("reveal receives faster numeric speed", reveal35["generation_config"]["speed"] == 1.05)
    check("OUTRO receives lower resolved sonic-3.5 emotion", outro35["generation_config"]["emotion"] == "sad")
    check("OUTRO receives restrained numeric speed", outro35["generation_config"]["speed"] == 0.85)
    check("sonic-3.5 keeps single-string emotion", all(isinstance(c["generation_config"]["emotion"], str) for c in sonic35_calls))
    check("sonic-3.5 sends no legacy emotion controls", all("_experimental_voice_controls" not in c for c in sonic35_calls))

    sonic2_calls = run_cartesia(LONG_SCRIPT, make_voice(model="sonic-2", emotion="calm"))
    check("sonic-2 marked script emits one request per section", len(sonic2_calls) == 4)
    intro2, _buildup2, reveal2, outro2 = sonic2_calls
    check("sonic-2 keeps legacy controls", all("_experimental_voice_controls" in c for c in sonic2_calls))
    check("sonic-2 INTRO receives weighted curious emotion", intro2["_experimental_voice_controls"]["emotion"] == ["curiosity:medium"])
    check("sonic-2 reveal receives weighted scared emotion", reveal2["_experimental_voice_controls"]["emotion"] == ["surprise:high", "sadness:medium"])
    check("sonic-2 OUTRO receives weighted somber emotion", outro2["_experimental_voice_controls"]["emotion"] == ["sadness:medium"])
    check("sonic-2 reveal speed remains legacy word label", reveal2["_experimental_voice_controls"]["speed"] == "fast")

    fallback_calls = run_cartesia(
        "Flat narration without any section marker. Then the old static behavior remains.",
        make_voice(model="sonic-3.5", emotion="warm", speed_profile="very_fast"),
    )
    check("missing section metadata emits one fallback request", len(fallback_calls) == 1)
    fallback = fallback_calls[0]
    check("missing metadata falls back to channel emotion", fallback["generation_config"]["emotion"] == "content")
    check("missing metadata falls back to channel speed", fallback["generation_config"]["speed"] == 1.12)

    child_calls = run_cartesia(
        "This standalone short starts fast and stays on the channel delivery.",
        make_voice(model="sonic-3.5", emotion="dramatic", speed_profile="fast"),
        is_short_episode=True,
    )
    check("child short flat narration emits one safe request", len(child_calls) == 1)
    check("child short keeps channel emotion policy", child_calls[0]["generation_config"]["emotion"] == "scared")
    check("child short keeps channel speed policy", child_calls[0]["generation_config"]["speed"] == 1.05)
finally:
    cartesia.Cartesia = orig_cartesia
    tts.call_claude = orig_call_claude
    tts._wav_to_mp3 = orig_wav_to_mp3
    tts._concat_mp3_chunks = orig_concat_mp3_chunks

print(f"SMOKE PASS - {ASSERTIONS} checks")
print(f"sonic35_intro={intro35['generation_config']}")
print(f"sonic35_reveal={reveal35['generation_config']}")
print(f"sonic35_outro={outro35['generation_config']}")
print(f"sonic2_intro={intro2['_experimental_voice_controls']}")
print(f"sonic2_reveal={reveal2['_experimental_voice_controls']}")
print(f"fallback={fallback['generation_config']}")
print(f"child_short={child_calls[0]['generation_config']}")
