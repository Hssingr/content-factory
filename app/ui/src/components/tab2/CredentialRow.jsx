import { useState } from 'react'
import { api } from '../../api/agent1'
import { CREDENTIAL_FIELDS } from './platformFields'
import { PLATFORMS } from '../../constants'

export default function CredentialRow({ channelId, platform, language, onVerified, initialVerified = false }) {
  const fields       = CREDENTIAL_FIELDS[platform] ?? []
  const platformInfo = PLATFORMS.find(p => p.id === platform)

  const [values,   setValues]   = useState(() => Object.fromEntries(fields.map(f => [f.key, ''])))
  const [verified, setVerified] = useState(initialVerified)
  const [saving,   setSaving]   = useState(false)
  const [expanded, setExpanded] = useState(!initialVerified) // pre-verified rows start collapsed
  const [error,    setError]    = useState('')

  const update    = (key, val) => setValues(prev => ({ ...prev, [key]: val }))
  const canVerify = fields.every(f => values[f.key]?.trim())

  const handleVerify = async () => {
    setSaving(true)
    setError('')
    try {
      // saveCredentials verifies before storing (verify-before-store pattern,
      // CLAUDE.md §8.3) — no separate verifyCredential call needed.
      await api.saveCredentials(channelId, { language, platform, credentials: values })
      setVerified(true)
      setExpanded(false)
      onVerified()
    } catch (e) {
      setError(e.message)
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className={`cred-row${verified ? ' cred-row--verified' : ''}`}>
      <div className="cred-row-header" onClick={() => setExpanded(e => !e)}>
        <span className="cred-platform-icon">{platformInfo?.icon}</span>
        <span className="cred-platform-label">{platformInfo?.label}</span>
        <span className="lang-code-pill">{language}</span>
        {verified
          ? <span className="badge badge--success">✓ Verified</span>
          : <span className="badge badge--pending">Pending</span>
        }
        <span className="cred-chevron">{expanded ? '▲' : '▼'}</span>
      </div>

      {expanded && (
        <div className="cred-row-body">
          <div className="cred-fields">
            {fields.map(f => (
              <div key={f.key} className="field">
                <label className="field-label">{f.label}</label>
                <input
                  className="field-input"
                  type={f.type}
                  value={values[f.key]}
                  onChange={e => update(f.key, e.target.value)}
                  placeholder={f.placeholder}
                  autoComplete="off"
                />
              </div>
            ))}
          </div>

          {error && <p className="field-error" style={{ marginTop: 8 }}>{error}</p>}

          <div className="cred-row-footer">
            <button
              type="button"
              className="btn-primary"
              onClick={handleVerify}
              disabled={saving || !canVerify}
            >
              {saving ? 'Verifying…' : verified ? 'Re-verify' : 'Save & Verify'}
            </button>
            {!canVerify && (
              <span className="cred-hint">Fill all fields to verify</span>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
