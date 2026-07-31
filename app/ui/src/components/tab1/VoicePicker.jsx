import { useState, useEffect, useRef } from 'react'
import { api } from '../../api/agent1'

// App-level VoiceGender ("feminine"/"masculine") -> each provider's own
// gender filter vocabulary (CLAUDE.md §8/§8.4 — Cartesia: masculine |
// feminine | gender_neutral; ElevenLabs Voice Library: free-text, male | female).
const GENDER_FILTER = {
  cartesia:   { feminine: 'feminine', masculine: 'masculine' },
  elevenlabs: { feminine: 'female',   masculine: 'male' },
}

const PROVIDER_LABEL = { cartesia: 'Cartesia', elevenlabs: 'ElevenLabs' }

export default function VoicePicker({ provider, language, gender, value, onSelect }) {
  const [open,        setOpen]        = useState(false)
  const [search,       setSearch]      = useState('')
  const [anyGender,    setAnyGender]   = useState(false)
  const [voices,       setVoices]      = useState([])
  const [cursor,       setCursor]      = useState(null)
  const [hasMore,      setHasMore]     = useState(false)
  const [loading,      setLoading]     = useState(false)
  const [error,        setError]       = useState('')
  const [playing,      setPlaying]     = useState(null)
  const [previewLoading, setPreviewLoading] = useState(null)
  const [previewError, setPreviewError] = useState('')
  const [dropdownPos,  setDropdownPos] = useState({ top: 0, left: 0, width: 320 })
  const [manualId,     setManualId]    = useState('')

  const triggerRef = useRef(null)
  const audioRef   = useRef(null)
  const debounceRef = useRef(null)

  const genderFilter = anyGender ? undefined : GENDER_FILTER[provider]?.[gender]

  const fetchPage = (nextCursor, append) => {
    setLoading(true)
    setError('')
    api.getVoices({
      provider,
      language,
      gender: genderFilter,
      search: search.trim() || undefined,
      cursor: nextCursor || undefined,
      page_size: 20,
    })
      .then(res => {
        setVoices(prev => append ? [...prev, ...(res.voices ?? [])] : (res.voices ?? []))
        setCursor(res.next_cursor ?? null)
        setHasMore(Boolean(res.has_more))
      })
      .catch(e => setError(e.message))
      .finally(() => setLoading(false))
  }

  // Fresh search whenever the panel opens, or the provider/language/gender
  // scope changes while it's open.
  useEffect(() => {
    if (!open) return
    setVoices([])
    setCursor(null)
    setHasMore(false)
    fetchPage(null, false)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, provider, language, genderFilter])

  // Debounced free-text search.
  useEffect(() => {
    if (!open) return
    clearTimeout(debounceRef.current)
    debounceRef.current = setTimeout(() => fetchPage(null, false), 400)
    return () => clearTimeout(debounceRef.current)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [search])

  // Close on scroll so the fixed-position dropdown stays aligned, and on an
  // outside click.
  useEffect(() => {
    if (!open) return
    const closeOnScroll = () => setOpen(false)
    const closeOnOutsideClick = (e) => {
      if (!triggerRef.current?.parentElement?.contains(e.target)) setOpen(false)
    }
    window.addEventListener('scroll', closeOnScroll, { passive: true })
    document.addEventListener('mousedown', closeOnOutsideClick)
    return () => {
      window.removeEventListener('scroll', closeOnScroll)
      document.removeEventListener('mousedown', closeOnOutsideClick)
    }
  }, [open])

  const handleToggle = () => {
    if (!open && triggerRef.current) {
      const rect = triggerRef.current.getBoundingClientRect()
      setDropdownPos({ top: rect.bottom + 4, left: rect.left, width: Math.max(rect.width, 320) })
    }
    setOpen(o => !o)
  }

  // ElevenLabs voices carry a real, static preview_url — play it directly,
  // instantly, for free. Cartesia's account/API version returns no preview
  // sample at all, so its "preview" is a genuinely live synthesis call
  // (api.previewVoiceUrl) that takes a moment — the button shows a loading
  // state while the browser fetches/decodes it.
  const preview = (voice) => {
    const canLiveSynthesize = voice.provider === 'cartesia'
    const src = voice.preview_url || (canLiveSynthesize ? api.previewVoiceUrl(voice.provider, voice.voice_id) : null)
    if (!src) return

    if (audioRef.current) { audioRef.current.pause(); audioRef.current = null }
    if (playing === voice.voice_id) { setPlaying(null); setPreviewLoading(null); return }

    setPreviewError('')
    const audio = new Audio(src)
    audioRef.current = audio
    setPlaying(voice.voice_id)
    if (!voice.preview_url) setPreviewLoading(voice.voice_id)
    audio.oncanplay = () => setPreviewLoading(null)
    audio.onended = () => setPlaying(null)
    audio.onerror = () => {
      setPreviewLoading(null)
      setPlaying(null)
      setPreviewError('Preview unavailable for this voice — please try again.')
    }
    audio.play().catch(() => {
      setPreviewLoading(null)
      setPlaying(null)
    })
  }

  const select = (voice) => {
    onSelect(voice.voice_id)
    setOpen(false)
  }

  const useManualId = () => {
    if (!manualId.trim()) return
    onSelect(manualId.trim())
    setManualId('')
    setOpen(false)
  }

  const metaLine = (v) => [v.gender, v.age, v.accent, v.language].filter(Boolean).join(' · ')

  // The currently-saved value, resolved to a friendly name if it happens to
  // be among the voices already fetched this session — otherwise shown as
  // the raw ID (we may not have browsed this provider yet this session).
  const selectedVoice = voices.find(v => v.voice_id === value)

  return (
    <div className="voice-picker">
      <button
        ref={triggerRef}
        type="button"
        className="voice-picker-trigger"
        onClick={handleToggle}
      >
        {value ? (
          <>
            <span className="voice-name">{selectedVoice?.name ?? value}</span>
            {selectedVoice && <span className="voice-desc">{metaLine(selectedVoice)}</span>}
          </>
        ) : (
          <span className="voice-placeholder">Select or paste a {PROVIDER_LABEL[provider] ?? provider} voice ID…</span>
        )}
        <span className="voice-picker-chevron">{open ? '▲' : '▼'}</span>
      </button>

      {open && (
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
          <label style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '6px 14px', fontSize: '0.78rem', color: 'var(--text-dim)' }}>
            <input type="checkbox" checked={anyGender} onChange={e => setAnyGender(e.target.checked)} />
            Show all genders (default: {gender})
          </label>

          {error && <p className="field-error" style={{ padding: '0 14px' }}>{error}</p>}
          {previewError && <p className="field-error" style={{ padding: '0 14px' }}>{previewError}</p>}

          <div className="voice-list">
            {voices.map(v => (
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
                {(v.preview_url || v.provider === 'cartesia') && (
                  <button
                    type="button"
                    className={`voice-preview-btn${playing === v.voice_id ? ' playing' : ''}`}
                    onClick={e => { e.stopPropagation(); preview(v) }}
                    title={v.preview_url ? 'Preview' : 'Generate a live preview of this voice'}
                    disabled={previewLoading === v.voice_id}
                  >
                    {previewLoading === v.voice_id ? '…' : playing === v.voice_id ? '■' : '▶'}
                  </button>
                )}
              </div>
            ))}
            {!loading && voices.length === 0 && (
              <p className="voice-empty">No voices found{search.trim() ? ` for "${search.trim()}"` : ''}.</p>
            )}
            {loading && <p className="voice-loading">Loading voices…</p>}
          </div>

          {!loading && hasMore && (
            <button
              type="button"
              className="btn-secondary"
              style={{ width: '100%', borderRadius: 0 }}
              onClick={() => fetchPage(cursor, true)}
            >
              Load more
            </button>
          )}

          <div className="voice-picker-manual">
            <input
              className="field-input"
              placeholder="Or paste a voice ID directly…"
              value={manualId}
              onChange={e => setManualId(e.target.value)}
              onClick={e => e.stopPropagation()}
              onKeyDown={e => e.key === 'Enter' && useManualId()}
            />
            <button type="button" className="btn-secondary" disabled={!manualId.trim()} onClick={useManualId}>
              Use ID
            </button>
          </div>
        </div>
      )}
    </div>
  )
}
