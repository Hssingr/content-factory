import {
  SHORTS_RULES, LANGUAGES,
} from '../../constants'

const ALL_DAYS = ['monday','tuesday','wednesday','thursday','friday','saturday','sunday']

function DayPicker({ selected, onChange }) {
  const toggle = day => {
    if (selected.includes(day)) onChange(selected.filter(d => d !== day))
    else onChange([...selected, day])
  }
  return (
    <div className="lang-grid" style={{ gap: 4 }}>
      {ALL_DAYS.map(d => (
        <button
          key={d}
          type="button"
          className={`lang-chip${selected.includes(d) ? ' selected' : ''}`}
          style={{ padding: '4px 8px', fontSize: '0.72rem', borderRadius: 12 }}
          onClick={() => toggle(d)}
        >
          {d.slice(0, 3)}
        </button>
      ))}
    </div>
  )
}

export default function ScheduleSection({
  videosPerWeek, setVideosPerWeek,
  shortsRule, setShortsRule,
  timings, setTimings,
  onSuggestTiming, suggestingTiming,
  languagesSaved,
  channelId,
}) {
  const langLabel = code => LANGUAGES.find(l => l.code === code)?.label ?? code

  const updateTiming = (lang, field, value) =>
    setTimings(prev => prev.map(t => t.language === lang ? { ...t, [field]: value } : t))

  return (
    <>
      {/* ── Schedule config ─────────────────────────── */}
      <div className="field">
        <label className="field-label">Videos per week</label>
        <input
          className="field-number"
          type="number"
          min={1}
          max={14}
          value={videosPerWeek}
          onChange={e => setVideosPerWeek(Number(e.target.value))}
          style={{ maxWidth: 100 }}
        />
      </div>
      <div className="field">
        <label className="field-label">Shorts rule</label>
        <select
          className="field-select"
          value={shortsRule}
          onChange={e => setShortsRule(e.target.value)}
          style={{ maxWidth: 280 }}
        >
          {SHORTS_RULES.map(r => (
            <option key={r.value} value={r.value}>{r.label}</option>
          ))}
        </select>
      </div>

      {/* ── Timing suggestion ───────────────────────── */}
      {channelId && (
        <div className="field">
          <label className="field-label">Optimal publish times</label>
          <button
            type="button"
            className="btn-suggest"
            style={{ alignSelf: 'flex-start' }}
            onClick={onSuggestTiming}
            disabled={suggestingTiming || !languagesSaved}
            title={!languagesSaved ? 'Save languages first (Section 2)' : 'Ask Claude for optimal publish schedule'}
          >
            {suggestingTiming ? '…' : '✨ Suggest timing'}
          </button>
          {!languagesSaved && (
            <p className="placeholder" style={{ fontSize: '0.78rem', marginTop: 4 }}>
              Save languages first to get timing suggestions.
            </p>
          )}
        </div>
      )}

      {/* ── Editable timing grid ────────────────────── */}
      {timings.length > 0 && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
          {timings.map(t => (
            <div key={t.language} className="voice-block">
              <p className="voice-block-title">{langLabel(t.language)}</p>

              <div className="field" style={{ marginBottom: 10 }}>
                <label className="field-label">Publish days</label>
                <DayPicker
                  selected={t.optimal_days || []}
                  onChange={days => updateTiming(t.language, 'optimal_days', days)}
                />
              </div>

              <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 2fr', gap: 10 }}>
                <div className="field">
                  <label className="field-label">Hour start</label>
                  <input
                    className="field-number"
                    type="number"
                    min={0}
                    max={23}
                    value={t.optimal_hour_start ?? 18}
                    onChange={e => updateTiming(t.language, 'optimal_hour_start', Number(e.target.value))}
                  />
                </div>
                <div className="field">
                  <label className="field-label">Hour end</label>
                  <input
                    className="field-number"
                    type="number"
                    min={0}
                    max={23}
                    value={t.optimal_hour_end ?? 20}
                    onChange={e => updateTiming(t.language, 'optimal_hour_end', Number(e.target.value))}
                  />
                </div>
                <div className="field">
                  <label className="field-label">Timezone</label>
                  <input
                    className="field-input"
                    type="text"
                    value={t.timezone ?? 'UTC'}
                    onChange={e => updateTiming(t.language, 'timezone', e.target.value)}
                    placeholder="e.g. Europe/Paris"
                  />
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </>
  )
}
