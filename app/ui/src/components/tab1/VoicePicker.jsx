import { useState, useEffect, useRef } from 'react'
import { api } from '../../api/agent1'

const _cache = new Map()

export default function VoicePicker({ language, useCase, value, onChange }) {
  const [open,         setOpen]         = useState(false)
  const [voices,       setVoices]       = useState([])
  const [loading,      setLoading]      = useState(false)
  const [search,       setSearch]       = useState('')
  const [playing,      setPlaying]      = useState(null)
  const [dropdownPos,  setDropdownPos]  = useState({ top: 0, left: 0, width: 300 })
  const triggerRef = useRef(null)
  const audioRef   = useRef(null)

  const selectedVoice = voices.find(v => v.voice_id === value)

  useEffect(() => {
    if (!open || !useCase) return
    const key = `${language}:${useCase}`
    if (_cache.has(key)) { setVoices(_cache.get(key)); return }
    setLoading(true)
    api.getVoices(language, useCase)
      .then(data => {
        const list = data.voices ?? []
        _cache.set(key, list)
        setVoices(list)
      })
      .catch(console.error)
      .finally(() => setLoading(false))
  }, [open, language, useCase])

  useEffect(() => {
    setVoices([])
    setSearch('')
  }, [useCase])

  // Close on scroll so the fixed dropdown stays aligned
  useEffect(() => {
    if (!open) return
    const close = () => setOpen(false)
    window.addEventListener('scroll', close, { passive: true })
    return () => window.removeEventListener('scroll', close)
  }, [open])

  const handleToggle = () => {
    if (!useCase) return
    if (!open && triggerRef.current) {
      const rect = triggerRef.current.getBoundingClientRect()
      setDropdownPos({ top: rect.bottom + 4, left: rect.left, width: rect.width })
    }
    setOpen(o => !o)
  }

  const preview = (voice) => {
    if (!voice.preview_url) return
    if (audioRef.current) audioRef.current.pause()
    const audio = new Audio(voice.preview_url)
    audioRef.current = audio
    setPlaying(voice.voice_id)
    audio.play()
    audio.onended = () => setPlaying(null)
  }

  const select = (voice) => {
    onChange(voice.voice_id)
    setOpen(false)
    setSearch('')
  }

  const filtered = voices.filter(v => {
    const q = search.toLowerCase()
    return (
      v.name.toLowerCase().includes(q) ||
      (v.descriptive ?? '').toLowerCase().includes(q) ||
      (v.description ?? '').toLowerCase().includes(q)
    )
  })

  const triggerLabel = () => {
    if (!useCase) return 'Select a use case first…'
    if (selectedVoice) return selectedVoice.name
    return `Select voice for ${language.toUpperCase()}…`
  }

  const metaLine = (v) =>
    [v.gender, v.age, v.descriptive].filter(Boolean).join(' · ')

  return (
    <div className="voice-picker">
      <button
        ref={triggerRef}
        type="button"
        className="voice-picker-trigger"
        onClick={handleToggle}
        disabled={!useCase}
      >
        <span className={selectedVoice ? 'voice-name' : 'voice-placeholder'}>
          {triggerLabel()}
        </span>
        {selectedVoice && <span className="voice-desc">{metaLine(selectedVoice)}</span>}
        <span className="voice-picker-chevron">{open ? '▲' : '▼'}</span>
      </button>

      {open && useCase && (
        <div
          className="voice-picker-dropdown"
          style={{ position: 'fixed', top: dropdownPos.top, left: dropdownPos.left, width: dropdownPos.width }}
        >
          <input
            className="field-input voice-search"
            placeholder="Search by name or description…"
            value={search}
            onChange={e => setSearch(e.target.value)}
            autoFocus
          />
          {loading && <p className="voice-loading">Loading voices…</p>}
          <div className="voice-list">
            {filtered.map(v => (
              <div
                key={v.voice_id}
                className={`voice-row${v.voice_id === value ? ' voice-row--selected' : ''}`}
                onClick={() => select(v)}
              >
                <div className="voice-row-info">
                  <span className="voice-name">{v.name}</span>
                  <span className="voice-desc">{metaLine(v)}</span>
                  {v.description && <span className="voice-description">{v.description}</span>}
                </div>
                {v.preview_url && (
                  <button
                    type="button"
                    className={`voice-preview-btn${playing === v.voice_id ? ' playing' : ''}`}
                    onClick={e => { e.stopPropagation(); preview(v) }}
                    title="Preview"
                  >
                    {playing === v.voice_id ? '■' : '▶'}
                  </button>
                )}
              </div>
            ))}
            {!loading && filtered.length === 0 && voices.length > 0 && (
              <p className="voice-empty">No voices match "{search}"</p>
            )}
            {!loading && voices.length === 0 && (
              <p className="voice-empty">No voices found for this combination.</p>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
