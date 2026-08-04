import { PLATFORMS } from '../../constants'

// Platform Synchronization bug fix: if the operator already picked target
// platform(s) during the Concept step (an applied Research/Validate
// recommendation), this step must not offer platforms outside that set —
// e.g. a YouTube-only concept stays restricted to YouTube here. When no
// platform was chosen at Concept time (allowedPlatforms is empty), every
// platform remains freely selectable, exactly as before this fix.
export default function PlatformsSection({ selected, onToggle, allowedPlatforms = [] }) {
  const restricted = allowedPlatforms.length > 0
  const options = restricted ? PLATFORMS.filter(p => allowedPlatforms.includes(p.id)) : PLATFORMS

  return (
    <>
      {restricted && (
        <p className="voice-description" style={{ marginBottom: 10 }}>
          Restricted to the platform{options.length > 1 ? 's' : ''} chosen during the Concept step
          ({options.map(p => p.label).join(', ')}). Go back to Concept to change this.
        </p>
      )}
      <div className="platform-grid">
        {options.map(p => (
          <div
            key={p.id}
            className={`platform-card${selected.includes(p.id) ? ' selected' : ''}`}
            onClick={() => onToggle(p.id)}
          >
            <span className="platform-icon">{p.icon}</span>
            <span className="platform-label">{p.label}</span>
          </div>
        ))}
      </div>
    </>
  )
}
