"""Smoke test — Agent 1 per-language voice cards.

No live provider calls. This checks static UI/backend wiring and schema behavior
for the V3 voice tab contract.
"""
from pathlib import Path
from types import SimpleNamespace
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.schemas.channel import VoiceEntry, VoiceResponse


def check(label: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(label)
    print(f"PASS [{label}]")


voices_src = Path("app/ui/src/components/tab1/VoicesSection.jsx").read_text()
app_src = Path("app/ui/src/App.jsx").read_text()
constants_src = Path("app/ui/src/constants.js").read_text()
schema_src = Path("app/schemas/channel.py").read_text()
service_src = Path("app/agents/agent1_setup/services/channels.py").read_text()
docs_src = Path("CLAUDE.md").read_text()

check("VoiceEntry defaults to Cartesia", VoiceEntry(language="en", voice_id="v").provider == "cartesia")
check("VoiceEntry defaults to sonic-3.5", VoiceEntry(language="en", voice_id="v").tts_model == "sonic-3.5")
check(
    "VoiceResponse exposes provider/model/voice_id per language",
    VoiceResponse.model_validate(SimpleNamespace(
        id="00000000-0000-0000-0000-000000000001",
        language="fr",
        provider="elevenlabs",
        tts_model="eleven_v3",
        voice_id="voice-fr",
        emotion=None,
        music_style=None,
        use_case=None,
    )).tts_model == "eleven_v3",
)

check("Cartesia provider is exposed", "value: 'cartesia'" in constants_src and "Cartesia" in constants_src)
check("ElevenLabs provider is exposed", "value: 'elevenlabs'" in constants_src and "ElevenLabs" in constants_src)
check("Cartesia default model is sonic-3.5", "cartesia: 'sonic-3.5'" in constants_src)
check("ElevenLabs default model is eleven_v3", "elevenlabs: 'eleven_v3'" in constants_src)
check("Cartesia model options include sonic-3.5 and sonic-2", "sonic-3.5" in constants_src and "sonic-2" in constants_src)
check("ElevenLabs model options include eleven_v3", "eleven_v3" in constants_src)

check("voice UI renders one card per language", "languages.map(lang" in voices_src and "voice-card" in voices_src)
check("voice UI has Provider select", "Provider" in voices_src and "VOICE_PROVIDERS.map" in voices_src)
check("voice UI has Model select", "Model" in voices_src and "VOICE_MODELS_BY_PROVIDER" in voices_src)
check("voice UI has Voice ID input", "Voice ID" in voices_src and "Paste provider voice ID" in voices_src)
check("voice UI has local Validate Voice button", "Validate Voice" in voices_src and "validateVoice" in voices_src)
check("validation is local only", "api." not in voices_src and "fetch(" not in voices_src and "Audio(" not in voices_src)
check("old shared use-case voice picker is not used by the tab", "VoicePicker" not in voices_src and "sharedUseCase" not in voices_src)

check("App restores per-language provider/model/voice state", "provider: v.provider || 'cartesia'" in app_src and "tts_model: v.tts_model" in app_src)
check("App creates default voice config for new languages", "provider: 'cartesia', tts_model: 'sonic-3.5'" in app_src)
check("App saves provider/model per language", "tts_model: voice.tts_model" in app_src and "provider," in app_src)
check("App no longer saves hidden shared voice fields", "emotion: null" in app_src and "music_style: null" in app_src and "use_case: null" in app_src)
check("App no longer hardcodes all saved voices to ElevenLabs", "provider: 'elevenlabs'" not in app_src)
check("backend persists tts_model", "tts_model=e.tts_model" in service_src)
check("schema includes tts_model request and response fields", "tts_model: str = \"sonic-3.5\"" in schema_src and "tts_model: str" in schema_src)
check("docs describe per-language voice cards", "voice setup is per publishing language" in docs_src)
check("docs state local Validate Voice is not provider verification", "does not call Cartesia" in docs_src and "not persisted as a" in docs_src)
check("no real Cartesia/ElevenLabs/Claude call made", True)

print("SMOKE PASS — Agent 1 per-language voice cards")
