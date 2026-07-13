import { LANGUAGES, VOICE_PROVIDERS, VOICE_MODELS_BY_PROVIDER, DEFAULT_VOICE_MODEL_BY_PROVIDER } from '../../constants'

const languageLabel = (code) => LANGUAGES.find(l => l.code === code)?.label ?? code.toUpperCase()

// Roadmap Phase D1 — one voice per (language, gender). Feminine is the
// recommended default (used whenever a story has no clear masculine
// protagonist); masculine is optional, used automatically when the story
// blueprint identifies a masculine protagonist.
const GENDERS = [
  {
    value: 'feminine',
    label: 'Feminine Voice',
    description: 'Used by default, and for any story with no single clear masculine protagonist.',
  },
  {
    value: 'masculine',
    label: 'Masculine Voice (optional)',
    description: "Used automatically when a story's protagonist is identified as masculine. Leave blank to always use the feminine voice.",
  },
]

const normalizeVoice = (voice = {}) => {
  const provider = voice.provider || 'cartesia'
  const fallbackModel = DEFAULT_VOICE_MODEL_BY_PROVIDER[provider] || 'sonic-3.5'
  return {
    provider,
    tts_model: voice.tts_model || fallbackModel,
    voice_id: voice.voice_id || '',
    voice_validated: Boolean(voice.voice_validated),
  }
}

export default function VoicesSection({ languages, voices, setVoices }) {
  const updateVoice = (lang, gender, patch) => {
    setVoices(prev => {
      const langVoices = prev[lang] || {}
      return {
        ...prev,
        [lang]: {
          ...langVoices,
          [gender]: {
            ...normalizeVoice(langVoices[gender]),
            ...patch,
          },
        },
      }
    })
  }

  const changeProvider = (lang, gender, provider) => {
    updateVoice(lang, gender, {
      provider,
      tts_model: DEFAULT_VOICE_MODEL_BY_PROVIDER[provider] || '',
      voice_id: '',
      voice_validated: false,
    })
  }

  const changeModel = (lang, gender, ttsModel) => {
    updateVoice(lang, gender, { tts_model: ttsModel, voice_validated: false })
  }

  const changeVoiceId = (lang, gender, voiceId) => {
    updateVoice(lang, gender, { voice_id: voiceId, voice_validated: false })
  }

  const validateVoice = (lang, gender) => {
    const voice = normalizeVoice((voices[lang] || {})[gender])
    if (!voice.voice_id.trim()) return
    updateVoice(lang, gender, { voice_validated: true })
  }

  if (!languages.length) {
    return (
      <p className="placeholder" style={{ fontSize: '0.82rem' }}>
        Select publishing languages first. A separate voice card will appear for each language.
      </p>
    )
  }

  return (
    <div className="voice-card-list">
      {languages.map(lang => (
        <div key={lang} className="voice-language-group">
          <p className="voice-language-heading">{languageLabel(lang)}</p>
          {GENDERS.map(({ value: gender, label, description }) => {
            const voice = normalizeVoice((voices[lang] || {})[gender])
            const models = VOICE_MODELS_BY_PROVIDER[voice.provider] || []
            const isValidated = voice.voice_validated && voice.voice_id.trim().length > 0

            return (
              <div key={gender} className="voice-block voice-card">
                <div className="voice-block-header">
                  <div>
                    <p className="voice-block-title">{label}</p>
                    <span className="voice-description">{description}</span>
                  </div>
                  <span className={`voice-status${isValidated ? ' voice-status--valid' : ''}`}>
                    {isValidated ? 'Saved for review ✓' : 'Not saved'}
                  </span>
                </div>

                <div className="voice-card-grid">
                  <label className="field">
                    <span className="field-label">Provider</span>
                    <select
                      className="field-select"
                      value={voice.provider}
                      onChange={e => changeProvider(lang, gender, e.target.value)}
                    >
                      {VOICE_PROVIDERS.map(provider => (
                        <option key={provider.value} value={provider.value}>{provider.label}</option>
                      ))}
                    </select>
                  </label>

                  <label className="field">
                    <span className="field-label">Model</span>
                    <select
                      className="field-select"
                      value={voice.tts_model}
                      onChange={e => changeModel(lang, gender, e.target.value)}
                    >
                      {models.map(model => (
                        <option key={model.value} value={model.value}>{model.label}</option>
                      ))}
                    </select>
                  </label>
                </div>

                <label className="field">
                  <span className="field-label">Voice ID</span>
                  <input
                    className="field-input"
                    value={voice.voice_id}
                    onChange={e => changeVoiceId(lang, gender, e.target.value)}
                    placeholder="Paste provider voice ID"
                  />
                </label>

                <div className="voice-card-footer">
                  <span className="voice-description">
                    {isValidated
                      ? 'Voice ID saved — will be verified when the channel first runs audio generation.'
                      : 'Enter a Voice ID and click Save to confirm your selection.'}
                  </span>
                  <button
                    type="button"
                    className="btn-secondary"
                    onClick={() => validateVoice(lang, gender)}
                    disabled={!voice.voice_id.trim()}
                  >
                    Save Voice ID
                  </button>
                </div>
              </div>
            )
          })}
        </div>
      ))}
    </div>
  )
}
