import { useState } from 'react'
import AISuggestionField from '../AISuggestionField'
import VoicePicker from './VoicePicker'
import { EMOTIONS, MUSIC_STYLES, USE_CASES } from '../../constants'
import { api } from '../../api/agent1'

export default function VoicesSection({
  languages, voices, setVoices,
  sharedUseCase, setSharedUseCase,
  sharedEmotion, setSharedEmotion,
  sharedMusicStyle, setSharedMusicStyle,
  ctx,
}) {
  const [autoSelecting, setAutoSelecting] = useState({})

  const setVoiceId = (lang, id) =>
    setVoices(prev => ({ ...prev, [lang]: { ...prev[lang], voice_id: id } }))

  const autoSelect = async (lang) => {
    if (!sharedUseCase) return
    setAutoSelecting(prev => ({ ...prev, [lang]: true }))
    try {
      const data      = await api.getVoices(lang, sharedUseCase)
      const currentId = voices[lang]?.voice_id
      // Exclude the already-selected voice so Claude picks a different one on re-click
      const available = (data.voices ?? []).filter(v => v.voice_id !== currentId)
      if (!available.length) return
      const res = await api.suggest('voice_id', {
        ...ctx,
        language:         lang,
        available_voices: available.map(v => ({
          voice_id:    v.voice_id,
          name:        v.name,
          gender:      v.gender,
          age:         v.age,
          descriptive: v.descriptive,
          description: v.description,
        })),
      })
      setVoiceId(lang, res.suggestion)
    } catch (e) {
      console.error('Auto-select failed for', lang, e)
    } finally {
      setAutoSelecting(prev => ({ ...prev, [lang]: false }))
    }
  }

  return (
    <>
      <AISuggestionField
        label="Voice use case (all languages)"
        field="voice_use_case"
        value={sharedUseCase}
        onChange={setSharedUseCase}
        context={ctx}
        options={[{ value: '', label: 'Select a use case…' }, ...USE_CASES]}
      />

      <div className="voice-shared-row">
        <AISuggestionField
          label="Narrator emotion (all languages)"
          field="voice_emotion"
          value={sharedEmotion}
          onChange={setSharedEmotion}
          context={ctx}
          options={EMOTIONS}
        />
        <AISuggestionField
          label="Music style (all languages)"
          field="music_style"
          value={sharedMusicStyle}
          onChange={setSharedMusicStyle}
          context={ctx}
          options={MUSIC_STYLES}
        />
      </div>

      {!sharedUseCase && (
        <p className="placeholder" style={{ fontSize: '0.82rem' }}>
          Select a use case above to browse and auto-select voices for each language.
        </p>
      )}

      {languages.map(lang => (
        <div key={lang} className={`voice-block${!sharedUseCase ? ' voice-block--locked' : ''}`}>
          <div className="voice-block-header">
            <p className="voice-block-title">{lang.toUpperCase()}</p>
            {sharedUseCase && (
              <button
                type="button"
                className="btn-suggest"
                onClick={() => autoSelect(lang)}
                disabled={autoSelecting[lang]}
                title="AI auto-select best voice"
              >
                {autoSelecting[lang] ? '…' : '✨ Auto-select'}
              </button>
            )}
          </div>
          <VoicePicker
            language={lang}
            useCase={sharedUseCase}
            value={voices[lang]?.voice_id ?? ''}
            onChange={id => setVoiceId(lang, id)}
          />
        </div>
      ))}
    </>
  )
}
