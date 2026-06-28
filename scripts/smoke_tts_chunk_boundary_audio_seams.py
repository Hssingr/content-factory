"""Smoke/investigation proof for TTS chunk-boundary audio stitching.

No live API calls. Fake Cartesia returns deterministic WAV tones; the real
Agent 3 TTS path performs prepare_script_for_tts(), WAV->MP3 conversion, and
MP3 stitching. Claude pause review is stubbed as an echo.
"""

from __future__ import annotations

import math
import struct
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import cartesia

from app.agents.agent3_audio.services import tts

ASSERTIONS = 0
CAPTURED_CALLS: list[dict] = []
CLAUDE_REVIEW_CALLS: list[str] = []
FREQUENCIES = [440.0, 660.0, 880.0, 550.0]
SAMPLE_RATE = 44_100
CHUNK_SECONDS = 0.24


def check(label: str, condition: bool, detail: str = "") -> None:
    global ASSERTIONS
    ASSERTIONS += 1
    if not condition:
        raise AssertionError(f"{label}: {detail}" if detail else label)


def make_wav_tone(freq: float, seconds: float = CHUNK_SECONDS) -> bytes:
    frame_count = int(SAMPLE_RATE * seconds)
    frames = bytearray()
    amplitude = 18_000
    for i in range(frame_count):
        sample = int(amplitude * math.sin(2.0 * math.pi * freq * i / SAMPLE_RATE))
        frames.extend(struct.pack("<h", sample))

    with tempfile.NamedTemporaryFile(suffix=".wav") as tmp:
        with wave.open(tmp.name, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(SAMPLE_RATE)
            wav.writeframes(bytes(frames))
        return Path(tmp.name).read_bytes()


class FakeTTS:
    def bytes(self, **kwargs):
        call_index = len(CAPTURED_CALLS)
        CAPTURED_CALLS.append(kwargs)
        return make_wav_tone(FREQUENCIES[call_index])


class FakeCartesia:
    def __init__(self, api_key=None):
        self.api_key = api_key
        self.tts = FakeTTS()


class Voice:
    provider = "cartesia"
    voice_id = "voice-boundary-proof"
    tts_model = "sonic-3.5"
    emotion = "dramatic"
    speed_profile = "normal"
    speed_override = None
    cartesia_pronunciation_dict_id = None


def ffprobe_stream(path: Path) -> dict[str, str]:
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "a:0",
        "-show_entries", "stream=codec_name,sample_rate,channels",
        "-of", "default=noprint_wrappers=1", str(path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    check("ffprobe succeeds", proc.returncode == 0, proc.stderr[-200:])
    result: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            result[key] = value
    return result


def decode_pcm(path: Path) -> list[int]:
    raw_path = path.with_suffix(".s16le")
    cmd = [
        "ffmpeg", "-y", "-i", str(path),
        "-f", "s16le", "-ac", "1", "-ar", str(SAMPLE_RATE), str(raw_path),
    ]
    proc = subprocess.run(cmd, capture_output=True)
    check("ffmpeg decodes stitched mp3", proc.returncode == 0, proc.stderr[-200:].decode(errors="replace"))
    raw = raw_path.read_bytes()
    return list(struct.unpack("<" + "h" * (len(raw) // 2), raw))


def rms(samples: list[int]) -> float:
    if not samples:
        return 0.0
    return math.sqrt(sum(s * s for s in samples) / len(samples))


def estimate_frequency(samples: list[int]) -> float:
    if len(samples) < 2:
        return 0.0
    crossings = 0
    previous = samples[0]
    for sample in samples[1:]:
        if (previous < 0 <= sample) or (previous > 0 >= sample):
            crossings += 1
        previous = sample
    return crossings * SAMPLE_RATE / (2.0 * len(samples))


def nearest_expected(freq: float) -> int:
    distances = [abs(freq - expected) for expected in FREQUENCIES]
    return min(range(len(distances)), key=distances.__getitem__)


def analyze_pcm(samples: list[int]) -> tuple[list[int], int, int]:
    window = int(SAMPLE_RATE * 0.04)
    ordered_tones: list[int] = []
    silence_runs = 0
    current_silence = 0
    max_silence_run = 0

    for start in range(0, len(samples) - window, window):
        chunk = samples[start:start + window]
        level = rms(chunk)
        if level < 700:
            current_silence += 1
            max_silence_run = max(max_silence_run, current_silence)
            continue

        if current_silence >= 1:
            silence_runs += 1
        current_silence = 0

        if level > 4_000:
            tone_id = nearest_expected(estimate_frequency(chunk))
            if not ordered_tones or ordered_tones[-1] != tone_id:
                ordered_tones.append(tone_id)

    return ordered_tones, silence_runs, max_silence_run


SCRIPT = """[INTRO]
The recorder started on its own.

[SECTION 1: Buildup]
Then I found the missing photo inside the locked drawer.

[SECTION 2: The reveal]
But the voice on the tape was mine.

[OUTRO]
The house was silent after that.
"""

orig_cartesia = cartesia.Cartesia
orig_call_claude = tts.call_claude
orig_sdk_supports_generation_config = tts._cartesia_sdk_supports_generation_config
cartesia.Cartesia = FakeCartesia
tts.call_claude = lambda _system_prompt, user_message, **_kwargs: (CLAUDE_REVIEW_CALLS.append(user_message) or user_message)
# Hotfix A added a pre-flight check that the installed Cartesia SDK actually
# supports the generation_config request shape this fixture's sonic-3.5
# voice needs. This smoke replaces the Cartesia client/TTS entirely with a
# fake that accepts any kwargs, so it simulates an SDK that DOES support
# generation_config (an operator who has upgraded the 'cartesia' package).
# Real SDK-compatibility detection is proven separately in
# scripts/smoke_cartesia_sdk_compatibility.py.
tts._cartesia_sdk_supports_generation_config = lambda: True
try:
    audio_bytes = tts.generate_audio(SCRIPT, Voice(), is_short_episode=False)
finally:
    cartesia.Cartesia = orig_cartesia
    tts.call_claude = orig_call_claude
    tts._cartesia_sdk_supports_generation_config = orig_sdk_supports_generation_config

check("four fake Cartesia calls were made", len(CAPTURED_CALLS) == 4, str(len(CAPTURED_CALLS)))
check("Claude pause review was stubbed once per section", len(CLAUDE_REVIEW_CALLS) == 4, str(len(CLAUDE_REVIEW_CALLS)))
check("INTRO request first", "recorder started" in CAPTURED_CALLS[0]["transcript"])
check("SECTION 1 request second", "missing photo" in CAPTURED_CALLS[1]["transcript"])
check("SECTION 2 request third", "voice on the tape" in CAPTURED_CALLS[2]["transcript"])
check("OUTRO request fourth", "house was silent" in CAPTURED_CALLS[3]["transcript"])
check("stitched audio bytes returned", len(audio_bytes) > 1_000, str(len(audio_bytes)))

with tempfile.TemporaryDirectory() as tmp:
    out_path = Path(tmp) / "stitched.mp3"
    out_path.write_bytes(audio_bytes)
    stream = ffprobe_stream(out_path)
    check("stitched output is mp3", stream.get("codec_name") == "mp3", str(stream))
    check("stitched output sample rate is 44100", stream.get("sample_rate") == "44100", str(stream))
    check("stitched output channel count is stable", int(stream.get("channels", "0")) >= 1, str(stream))

    pcm = decode_pcm(out_path)
    tone_order, silence_runs, max_silence_run = analyze_pcm(pcm)

check("decoded PCM has samples", len(pcm) > SAMPLE_RATE // 2, str(len(pcm)))
check("tone order is preserved in stitched audio", tone_order[:4] == [0, 1, 2, 3], str(tone_order))
check("boundary silence padding is measurable", silence_runs >= 3, f"runs={silence_runs} max_run={max_silence_run}")
check("boundary silence lasts at least one analysis window", max_silence_run >= 1, str(max_silence_run))
check("Cartesia request format remains sonic-3.5 generation_config", "generation_config" in CAPTURED_CALLS[0])
check("legacy voice_id not sent for sonic-3.5", "voice_id" not in CAPTURED_CALLS[0])

print(f"SMOKE PASS - {ASSERTIONS} checks")
print(f"cartesia_call_count={len(CAPTURED_CALLS)}")
print(f"transcript_order={[call['transcript'].split('.')[0] for call in CAPTURED_CALLS]}")
print(f"stream={stream}")
print(f"tone_order={tone_order[:4]}")
print(f"silence_runs={silence_runs} max_silence_windows={max_silence_run}")
print(f"output_bytes={len(audio_bytes)}")
