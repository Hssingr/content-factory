import { PLATFORMS } from '../../constants'

export default function PlatformsSection({ selected, onToggle }) {
  return (
    <div className="platform-grid">
      {PLATFORMS.map(p => (
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
  )
}
