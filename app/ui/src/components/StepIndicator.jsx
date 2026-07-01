// Top sticky step nav + a single honest "why this step" context panel.
// Unlike the reference design this mirrors, there is no second "AI agent
// state" panel here — that panel in the reference implied a background AI
// process that isn't real, and CLAUDE.md requires no pretense of
// unimplemented behavior. Real AI-assisted fields (the ✨ suggest buttons)
// already speak for themselves at the field level.

const STEP_CONTEXT = {
  mode: 'Content mode determines the production pipeline this channel runs end to end. Only Single Story is wired up today — Limited Series and Ongoing Series are visible so you can see the roadmap, but they cannot be activated yet.',
  basics: 'Your description, name, niche, and tone seed every AI-assisted field below and steer the discovery and scripting agents downstream. Use the ✨ buttons for a Claude-generated suggestion at any time.',
  languages: 'Every active language gets its own scripts, audio, and captions end to end — Agent 2 and Agent 3 generate a complete, independent set per language, not a translation pass after the fact.',
  voices: 'Provider, model, and voice ID are configured independently per publishing language and used directly by Agent 3’s TTS step.',
  schedule: 'Cadence, Shorts policy, and the V3 content/script/output mode fields all feed the scheduler and Agent 2’s discovery loop. Suggest timing asks Claude for an optimal publish schedule once your languages are saved.',
  sources: 'Content sources are where Agent 2 actually pulls candidate stories from. At least one is required when Script source is Reddit (the only executable source today).',
  platforms: 'Select every platform this channel should publish to. Each platform you pick here gets its own credential row in the next step, per language.',
  credentials: 'Every platform you selected needs verified credentials before the channel can activate — credentials are encrypted before they ever reach the database (see CLAUDE.md §30).',
  activation: 'Activation is gated entirely by the backend’s readiness check — every requirement below must be satisfied for every selected platform before the pipeline can go live.',
}

export default function StepIndicator({ steps, currentStep, completedSteps, onNavigate }) {
  const context = STEP_CONTEXT[currentStep]

  return (
    <div className="step-nav-bar">
      <div className="step-nav-inner">
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <div className="app-logo">⚡</div>
          <div>
            <h1 style={{ margin: 0 }}>Content Factory</h1>
            <div>
              <span className="app-live-dot" />
              <span className="app-subtitle">Channel Setup</span>
            </div>
          </div>
        </div>

        <div className="step-nav-badges">
          {steps.map((s, i) => {
            const isDone = completedSteps.includes(s.id)
            const isActive = currentStep === s.id
            const canClick = isDone || isActive
            let cls = 'step-badge'
            if (isActive) cls += ' step-badge--active'
            else if (isDone) cls += ' step-badge--done'
            if (canClick) cls += ' step-badge--clickable'

            return (
              <button
                key={s.id}
                type="button"
                className={cls}
                disabled={!canClick}
                onClick={() => canClick && onNavigate(s.id)}
              >
                <span className="step-badge-num">{isDone ? '✓' : i + 1}</span>
                <span>{s.label}</span>
              </button>
            )
          })}
        </div>
      </div>

      {context && (
        <div className="context-panel">
          <div className="context-panel-icon">i</div>
          <p><strong>Why this step:</strong> {context}</p>
        </div>
      )}
    </div>
  )
}
