import { useState } from 'react'
import { api } from '../api/agent1'

export default function AISuggestionField({
  label, field, value, onChange, context,
  type = 'text', options, placeholder, disabled, multiline = false,
  disableSuggest = false,
}) {
  const [suggestion, setSuggestion] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  const suggest = async () => {
    setLoading(true)
    setError('')
    setSuggestion('')
    try {
      const res = await api.suggest(field, context)
      setSuggestion(res.suggestion)
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }

  const renderInput = () => {
    if (options) {
      return (
        <select
          className="field-select"
          value={value}
          onChange={e => onChange(e.target.value)}
          disabled={disabled}
        >
          {options.map(o => (
            <option key={o.value} value={o.value}>{o.label}</option>
          ))}
        </select>
      )
    }
    if (multiline) {
      return (
        <textarea
          className="field-input field-textarea"
          value={value}
          onChange={e => onChange(e.target.value)}
          placeholder={placeholder}
          disabled={disabled}
          rows={3}
        />
      )
    }
    return (
      <input
        className="field-input"
        type={type}
        value={value}
        onChange={e => onChange(e.target.value)}
        placeholder={placeholder}
        disabled={disabled}
      />
    )
  }

  return (
    <div className="field">
      {label && <label className="field-label">{label}</label>}
      <div className={`field-row${multiline ? ' field-row--multiline' : ''}`}>
        {renderInput()}
        <button
          type="button"
          className="btn-suggest"
          onClick={suggest}
          disabled={loading || disabled || disableSuggest}
          title="Get AI suggestion"
        >
          {loading ? '…' : '✨'}
        </button>
      </div>
      {error && <p className="field-error">{error}</p>}
      {suggestion && (
        <div className="suggestion-card">
          <span className="suggestion-text">{suggestion}</span>
          <button type="button" className="btn-accept" onClick={() => { onChange(suggestion); setSuggestion('') }}>
            Accept
          </button>
          <button type="button" className="btn-dismiss" onClick={() => setSuggestion('')}>✕</button>
        </div>
      )}
    </div>
  )
}
