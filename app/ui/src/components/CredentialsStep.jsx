import { useState, useEffect } from 'react'
import { api } from '../api/agent1'
import CredentialRow from './tab2/CredentialRow'
import StepShell from './StepShell'

export default function CredentialsStep({ channelId, languages, platforms, onBack, onNext }) {
  const [verifiedCount, setVerifiedCount] = useState(0)
  const [initialVerifiedMap, setInitialVerifiedMap] = useState({})
  const [loadingState, setLoadingState] = useState(true)

  const rows = platforms.flatMap(p => languages.map(l => ({ platform: p, language: l })))

  useEffect(() => {
    if (!channelId) { setLoadingState(false); return }
    api.getChannel(channelId)
      .then(ch => {
        const map = {}
        let count = 0
        ch.platforms.forEach(p => {
          if (p.verified) { map[`${p.platform}-${p.language}`] = true; count++ }
        })
        setInitialVerifiedMap(map)
        setVerifiedCount(count)
      })
      .catch(console.error)
      .finally(() => setLoadingState(false))
  }, [channelId])

  const allVerified = rows.length > 0 && verifiedCount >= rows.length

  if (rows.length === 0) {
    return (
      <StepShell
        eyebrow="Step 7 — Platform Credentials"
        title="No platforms selected"
        subtitle="Go back to Target platforms and select at least one platform before connecting credentials."
        onBack={onBack}
      />
    )
  }

  return (
    <StepShell
      eyebrow="Step 7 — Platform Credentials"
      title="Securely connect target platforms"
      subtitle="Save and verify credentials for every platform × language combination. Credentials are Fernet-encrypted before they ever reach the database."
      onBack={onBack}
      onNext={onNext}
      nextLabel="Continue to Activation →"
      nextDisabled={!allVerified}
    >
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
      {!allVerified && (
        <p className="cred-hint">All platforms must be verified to continue ({verifiedCount} / {rows.length} verified).</p>
      )}
    </StepShell>
  )
}
