import { useState, useEffect } from 'react'
import { api } from '../api/agent1'

// Resolve a backend issue code to a human-readable label.
// Exact codes come from activation_readiness.py; dynamic codes use prefix matching.
const EXACT_ISSUE_LABELS = {
  missing_config:                   'Channel configuration not saved',
  no_languages:                     'At least one publishing language is required',
  no_sources_for_reddit_mode:       'Reddit script source requires at least one subreddit / source',
  no_publish_timing:                'Publish timing not configured',
  no_platforms_selected:            'At least one platform with credentials is required',
  youtube_required_for_output_mode: 'YouTube & Shorts output mode requires a YouTube credential',
}

function resolveIssueLabel(code) {
  if (EXACT_ISSUE_LABELS[code]) return EXACT_ISSUE_LABELS[code]
  if (code.startsWith('missing_voice:'))       return 'Every language needs a configured voice'
  if (code.startsWith('unverified_platform:')) return 'All saved platform credentials must be verified'
  if (code.startsWith('v3_config:'))           return 'Content mode, script source, or output mode is not yet supported'
  return null
}

function ReadinessItem({ issue }) {
  const label = resolveIssueLabel(issue.code) || issue.message
  return (
    <div className="issue-item">
      <span className="issue-item-bullet">✕</span>
      <span>{label}</span>
    </div>
  )
}

function ReadinessChecklist({ readiness, loading, error }) {
  if (loading) {
    return <p className="voice-description" style={{ marginTop: 12 }}>Checking readiness…</p>
  }
  if (error) {
    return <p className="field-error" style={{ marginTop: 12 }}>{error}</p>
  }
  if (!readiness) return null

  if (readiness.ready) {
    return (
      <div className="readiness-pass">
        <span className="readiness-pass-icon">✓</span>
        <span>All requirements satisfied — ready to activate.</span>
      </div>
    )
  }

  return (
    <div className="issue-list" style={{ marginTop: 14 }}>
      <p style={{ fontSize: '0.82rem', color: 'var(--text-muted)', marginBottom: 8 }}>
        Resolve these before activating:
      </p>
      {readiness.issues.map(issue => (
        <ReadinessItem key={issue.code} issue={issue} />
      ))}
    </div>
  )
}

export default function ActivationStep({ channelId, proposal, onBack, onReset }) {
  const [preflight, setPreflight] = useState(null)
  const [preflightLoading, setPreflightLoading] = useState(true)
  const [preflightError, setPreflightError] = useState('')

  const [activating, setActivating] = useState(false)
  const [activated, setActivated] = useState(false)
  const [activationIssues, setActivationIssues] = useState([])
  const [activationError, setActivationError] = useState('')

  // Load readiness pre-flight check on mount (and after failed activation)
  const loadReadiness = async () => {
    if (!channelId) return
    setPreflightLoading(true)
    setPreflightError('')
    try {
      const result = await api.getReadiness(channelId)
      setPreflight(result)
    } catch (e) {
      setPreflightError(e.message)
    } finally {
      setPreflightLoading(false)
    }
  }

  useEffect(() => { loadReadiness() }, [channelId])

  const activate = async () => {
    setActivating(true)
    setActivationIssues([])
    setActivationError('')
    try {
      await api.activateChannel(channelId)
      setActivated(true)
    } catch (e) {
      // Backend returns "Channel is not ready to activate: <issue 1>; ..."
      const msg = e.message ?? ''
      const marker = 'Channel is not ready to activate: '
      if (msg.startsWith(marker)) {
        setActivationIssues(msg.slice(marker.length).split('; ').filter(Boolean))
      } else {
        setActivationError(msg)
      }
      // Refresh the pre-flight panel to reflect current state
      loadReadiness()
    } finally {
      setActivating(false)
    }
  }

  const canActivate = !preflightLoading && !preflightError && preflight?.ready

  if (activated) {
    return (
      <div className="step-shell">
        <div className="activation-card glow-card-green">
          <div className="activation-success-icon">✓</div>
          <h2 className="step-shell-title" style={{ fontSize: '1.4rem' }}>Channel successfully activated!</h2>
          <p className="step-shell-subtitle" style={{ margin: '10px auto 0' }}>
            Agent 2 will pick up this channel on the next discovery cycle and start
            sourcing, scoring, and sending stories for your Telegram approval.
          </p>

          <div className="activation-summary">
            <div className="activation-summary-row"><span>Channel</span><strong>{proposal.name || 'Untitled'}</strong></div>
            <div className="activation-summary-row"><span>Niche</span><strong>{proposal.niche || '—'}</strong></div>
            <div className="activation-summary-row"><span>Languages</span><strong>{proposal.languages.join(', ') || '—'}</strong></div>
            <div className="activation-summary-row"><span>Platforms</span><strong>{proposal.platforms.join(', ') || '—'}</strong></div>
            <div className="activation-summary-row"><span>Cadence</span><strong>{proposal.videosPerWeek} videos/week</strong></div>
          </div>

          <button type="button" className="btn-secondary" onClick={onReset}>← Back to channel list</button>
        </div>
      </div>
    )
  }

  return (
    <div className="step-shell">
      <span className="step-shell-eyebrow">Step 8 — Final Activation</span>
      <h2 className="step-shell-title">Ready to launch this channel?</h2>
      <p className="step-shell-subtitle">
        The backend verifies every requirement before activating. Resolve any items below first.
      </p>

      <div className="activation-card glow-card" style={{ marginTop: 22 }}>
        <div className="activation-icon-ring">🚀</div>
        <h3 className="mode-title" style={{ fontSize: '1.1rem' }}>Pre-flight checklist</h3>

        <div className="activation-summary" style={{ marginTop: 12 }}>
          <div className="activation-summary-row"><span>Content mode</span><strong>Single Story</strong></div>
          <div className="activation-summary-row"><span>Languages</span><strong>{proposal.languages.join(', ') || '—'}</strong></div>
          <div className="activation-summary-row"><span>Target platforms</span><strong>{proposal.platforms.join(', ') || '—'}</strong></div>
        </div>

        <ReadinessChecklist
          readiness={preflight}
          loading={preflightLoading}
          error={preflightError}
        />

        {activationError && <div className="error-banner" style={{ marginTop: 12 }}>{activationError}</div>}

        {/* Show activation-returned issues (from a direct API call) separately from the pre-flight list */}
        {activationIssues.length > 0 && (
          <div className="issue-list" style={{ marginTop: 8 }}>
            <p style={{ fontSize: '0.78rem', color: 'var(--text-muted)', marginBottom: 6 }}>
              Activation blocked:
            </p>
            {activationIssues.map((issue, i) => (
              <div key={i} className="issue-item">
                <span className="issue-item-bullet">✕</span>
                <span>{issue}</span>
              </div>
            ))}
          </div>
        )}

        <button
          type="button"
          className="btn-activate"
          onClick={activate}
          disabled={activating || !canActivate}
          style={{ marginTop: 18 }}
          title={!canActivate && !preflightLoading ? 'Resolve all blocking issues above first' : undefined}
        >
          {activating ? 'Activating…' : 'Activate Channel'}
        </button>

        {!canActivate && !preflightLoading && !preflightError && (
          <p className="voice-description" style={{ marginTop: 8, fontSize: '0.78rem' }}>
            Go back and fix the items above, then return here to activate.
          </p>
        )}
      </div>

      <div className="step-shell-footer">
        <button type="button" className="btn-secondary" onClick={onBack}>← Back to Credentials</button>
        <span />
      </div>
    </div>
  )
}
