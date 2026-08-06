import { PLATFORMS, OUTPUT_MODE_PLATFORM_RESTRICTIONS } from '../../constants'

// Platform Synchronization bug fix: if the operator already picked target
// platform(s) during the Concept step (an applied Research/Validate
// recommendation), this step must not offer platforms outside that set —
// e.g. a YouTube-only concept stays restricted to YouTube here. When no
// platform was chosen at Concept time (allowedPlatforms is empty), every
// platform remains freely selectable, exactly as before this fix.
//
// A second, independent restriction comes from `outputMode`: a channel
// configured as "YouTube long-form only" never produces a Short, so
// TikTok/Instagram/Facebook have no asset to publish and are hidden here
// too — see OUTPUT_MODE_PLATFORM_RESTRICTIONS.
export default function PlatformsSection({ selected, onToggle, allowedPlatforms = [], outputMode }) {
  const outputModeAllowed = OUTPUT_MODE_PLATFORM_RESTRICTIONS[outputMode] ?? null
  let options = PLATFORMS
  if (outputModeAllowed) options = options.filter(p => outputModeAllowed.includes(p.id))
  if (allowedPlatforms.length > 0) options = options.filter(p => allowedPlatforms.includes(p.id))
  const restricted = options.length < PLATFORMS.length

  return (
    <>
      {outputModeAllowed && (
        <p className="voice-description" style={{ marginBottom: 10 }}>
          "YouTube long-form only" never produces a Short, so only YouTube is available here.
          Go back to Concept and change Output mode to publish to other platforms.
        </p>
      )}
      {!outputModeAllowed && restricted && (
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
