import { useState, useEffect } from 'react'
import '../form.css'
import { api } from '../api/agent1'
import CredentialRow from './tab2/CredentialRow'

export default function Tab2Credentials({ channelId, languages, platforms, onBack, onCancel }) {
  const [verifiedCount,     setVerifiedCount]     = useState(0)
  const [activating,        setActivating]        = useState(false)
  const [activated,         setActivated]         = useState(false)
  const [error,             setError]             = useState('')
  const [initialVerifiedMap, setInitialVerifiedMap] = useState({})
  const [loadingState,      setLoadingState]      = useState(true)

  const rows = platforms.flatMap(p => languages.map(l => ({ platform: p, language: l })))

  // Restore verified state from DB on mount
  useEffect(() => {
    if (!channelId) { setLoadingState(false); return }
    api.getChannel(channelId)
      .then(ch => {
        const map   = {}
        let   count = 0
        ch.platforms.forEach(p => {
          if (p.verified) { map[`${p.platform}-${p.language}`] = true; count++ }
        })
        setInitialVerifiedMap(map)
        setVerifiedCount(count)
        setActivated(ch.active)
      })
      .catch(console.error)
      .finally(() => setLoadingState(false))
  }, [channelId])

  const activate = async () => {
    setActivating(true)
    setError('')
    try {
      await api.activateChannel(channelId)
      setActivated(true)
    } catch (e) {
      setError(e.message)
    } finally {
      setActivating(false)
    }
  }

  const allVerified = rows.length > 0 && verifiedCount >= rows.length

  if (rows.length === 0) {
    return (
      <div className="tab-panel">
        <h2>Platform Credentials</h2>
        <p className="placeholder">No platforms or languages selected — go back to Channel Config.</p>
        <button type="button" className="btn-secondary" onClick={onCancel} style={{ marginTop: 16 }}>Cancel</button>
      </div>
    )
  }

  return (
    <div>
      <button type="button" className="btn-secondary btn-sm" onClick={onBack} style={{ marginBottom: 20 }}>
        ← Back to Channel Config
      </button>

      {error    && <div className="error-banner">{error}</div>}
      {activated && (
        <div className="success-banner">
          ✓ Pipeline activated — Agent 2 will start discovering content on the next schedule tick.
        </div>
      )}

      {!loadingState && (
        <div className="cred-grid">
          {rows.map(({ platform, language }) => (
            <CredentialRow
              key={`${platform}-${language}`}
              channelId={channelId}
              platform={platform}
              language={language}
              initialVerified={initialVerifiedMap[`${platform}-${language}`] ?? false}
              onVerified={() => setVerifiedCount(c => c + 1)}
            />
          ))}
        </div>
      )}

      <div className="cred-activate-bar">
        <button
          type="button"
          className="btn-activate"
          disabled={!allVerified || activating || activated}
          onClick={activate}
        >
          {activated   ? '✓ Pipeline Active'
           : activating ? 'Activating…'
           : `Activate Pipeline (${verifiedCount} / ${rows.length} verified)`}
        </button>
        {!allVerified && !activated && (
          <p className="cred-hint">All platforms must be verified to activate.</p>
        )}
        <div style={{ display: 'flex', gap: 10, marginTop: 12 }}>
          <button type="button" className="btn-primary" onClick={onCancel} disabled={activated}>
            Save as Draft
          </button>
          <button type="button" className="btn-secondary" onClick={onCancel}>
            Cancel
          </button>
        </div>
      </div>
    </div>
  )
}
