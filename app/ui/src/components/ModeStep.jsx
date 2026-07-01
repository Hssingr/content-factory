import { CONTENT_MODES } from '../constants'

const MODE_ICON = { single_story: '✨', limited_series: '📚', ongoing_series: '🔁' }
const MODE_BENEFITS = {
  single_story: [
    'One full pipeline run per discovery cycle',
    'Long-form video + standalone Shorts, independently rendered',
    'The only mode wired into Agent 2–5 today',
  ],
}

export default function ModeStep({ contentMode, setContentMode, onNext, onCancel }) {
  const selected = CONTENT_MODES.find(m => m.value === contentMode)

  return (
    <div className="step-shell">
      <span className="step-shell-eyebrow">Step 1 — Discovery</span>
      <h2 className="step-shell-title">What kind of content do you want to create?</h2>
      <p className="step-shell-subtitle">
        Content mode selects the production pipeline this channel runs. Different
        modes activate different discovery, scripting, and scheduling behavior.
      </p>

      <div className="mode-grid" style={{ marginTop: 22 }}>
        {CONTENT_MODES.map(mode => {
          const isSelected = contentMode === mode.value
          const disabled = !mode.executable
          let cls = 'mode-card'
          if (disabled) cls += ' mode-card--disabled'
          else if (isSelected) cls += ' mode-card--selected'

          return (
            <div
              key={mode.value}
              className={cls}
              onClick={() => !disabled && setContentMode(mode.value)}
            >
              <div>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start' }}>
                  <div className="mode-icon-box">{MODE_ICON[mode.value] ?? '•'}</div>
                  <div className="mode-badges">
                    <span className="mode-badge">{mode.executable ? 'Active mode' : 'Coming soon'}</span>
                  </div>
                </div>
                <h3 className="mode-title" style={{ marginTop: 14 }}>{mode.label}</h3>
                <p className="mode-description">{mode.description}</p>

                {mode.executable && MODE_BENEFITS[mode.value] && (
                  <ul className="mode-benefits">
                    {MODE_BENEFITS[mode.value].map(b => <li key={b}>{b}</li>)}
                  </ul>
                )}
              </div>

              {!disabled && (
                <span className="mode-select-pill">{isSelected ? 'Selected ✓' : 'Select mode'}</span>
              )}
            </div>
          )
        })}
      </div>

      {selected && !selected.executable && (
        <div className="coming-soon-banner" style={{ marginTop: 16 }}>
          {selected.label} is coming soon — only Single Story is available today.
        </div>
      )}

      <div className="step-shell-footer">
        <button type="button" className="btn-secondary" onClick={onCancel}>Cancel</button>
        <button
          type="button"
          className="btn-primary"
          onClick={onNext}
          disabled={!selected?.executable}
        >
          Continue to Concept →
        </button>
      </div>
    </div>
  )
}
