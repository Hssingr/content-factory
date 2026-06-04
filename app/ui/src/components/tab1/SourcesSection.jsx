import { useState } from 'react'
import { api } from '../../api/agent1'
import { SOURCE_TYPES, LANGUAGES } from '../../constants'

export default function SourcesSection({ sources, setSources, languages, ctx }) {
  const [activeType,  setActiveType]  = useState('rss')
  const [manualInput, setManualInput] = useState('')
  const [suggestion,  setSuggestion]  = useState('')
  const [suggesting,  setSuggesting]  = useState(false)
  const [suggestErr,  setSuggestErr]  = useState('')

  const availableLangs = LANGUAGES.filter(l => languages.includes(l.code))

  const addSource = (value) => {
    if (!value.trim()) return
    setSources(prev => [...prev, {
      source_type:  activeType,
      source_value: value.trim(),
      language:     '',
      trust_score:  1.0,
    }])
  }

  const handleAdd = () => {
    addSource(manualInput)
    setManualInput('')
  }

  const handleSuggest = async () => {
    setSuggesting(true)
    setSuggestErr('')
    setSuggestion('')
    try {
      const res = await api.suggest('source', {
        ...ctx,
        source_type:      activeType,
        existing_sources: sources.map(s => s.source_value),
      })
      setSuggestion(res.suggestion)
    } catch (e) {
      setSuggestErr(e.message)
    } finally {
      setSuggesting(false)
    }
  }

  const acceptSuggestion = () => {
    addSource(suggestion)
    setSuggestion('')
  }

  const remove = (i) => setSources(prev => prev.filter((_, idx) => idx !== i))

  const updateLang = (i, lang) =>
    setSources(prev => prev.map((s, idx) => idx === i ? { ...s, language: lang } : s))

  return (
    <>
      <div className="source-builder-controls">
        <div className="field" style={{ flex: '0 0 120px' }}>
          <label className="field-label">Type</label>
          <select className="field-select" value={activeType} onChange={e => setActiveType(e.target.value)}>
            {SOURCE_TYPES.map(t => <option key={t.value} value={t.value}>{t.label}</option>)}
          </select>
        </div>

        <div className="field" style={{ flex: 1 }}>
          <label className="field-label">URL / handle</label>
          <div className="field-row">
            <input
              className="field-input"
              value={manualInput}
              onChange={e => setManualInput(e.target.value)}
              placeholder="https://… or r/subreddit"
              onKeyDown={e => e.key === 'Enter' && handleAdd()}
            />
            <button type="button" className="btn-secondary" onClick={handleAdd} disabled={!manualInput.trim()}>+ Add</button>
            <button type="button" className="btn-suggest" onClick={handleSuggest} disabled={suggesting} title="Get AI suggestion">
              {suggesting ? '…' : '✨'}
            </button>
          </div>
        </div>
      </div>

      {suggestErr && <p className="field-error">{suggestErr}</p>}

      {suggestion && (
        <div className="suggestion-card">
          <span className="suggestion-text">{suggestion}</span>
          <button type="button" className="btn-accept" onClick={acceptSuggestion}>Accept</button>
          <button type="button" className="btn-suggest" onClick={handleSuggest} disabled={suggesting} title="Try another">
            {suggesting ? '…' : '↻'}
          </button>
          <button type="button" className="btn-dismiss" onClick={() => setSuggestion('')}>✕</button>
        </div>
      )}

      {sources.length > 0 && (
        <div className="source-added-list">
          {sources.map((src, i) => (
            <div key={i} className="source-item">
              <span className="source-type-pill">{src.source_type}</span>
              <span className="source-value">{src.source_value}</span>
              <select
                className="field-select source-lang-select"
                value={src.language}
                onChange={e => updateLang(i, e.target.value)}
              >
                <option value="">Any</option>
                {availableLangs.map(l => <option key={l.code} value={l.code}>{l.label}</option>)}
              </select>
              <button type="button" className="btn-remove" onClick={() => remove(i)}>✕</button>
            </div>
          ))}
        </div>
      )}

      {sources.length === 0 && (
        <p className="placeholder" style={{ fontSize: '0.82rem' }}>No sources added yet — use ✨ Suggest or type one manually.</p>
      )}
    </>
  )
}
