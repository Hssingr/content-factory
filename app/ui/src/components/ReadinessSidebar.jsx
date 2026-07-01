export default function ReadinessSidebar({ items, currentStep }) {
  const doneCount = items.filter(i => i.done).length
  const pct = Math.round((doneCount / items.length) * 100)

  return (
    <div className="readiness-panel">
      <div className="readiness-panel-header">
        <span className="readiness-panel-title">Readiness</span>
        <span className="live-pill">Live</span>
      </div>

      <div>
        <div className="readiness-progress-label">
          <span>Setup progress</span>
          <span>{pct}%</span>
        </div>
        <div className="readiness-progress-track">
          <div className="readiness-progress-fill" style={{ width: `${pct}%` }} />
        </div>
      </div>

      <div className="readiness-list">
        {items.map(item => {
          const isActive = item.id === currentStep
          let cls = 'readiness-item'
          if (item.done) cls += ' readiness-item--done'
          else if (isActive) cls += ' readiness-item--active'

          return (
            <div key={item.id} className={cls}>
              <span className="readiness-check">{item.done ? '✓' : ''}</span>
              <span className="readiness-item-label">{item.label}</span>
            </div>
          )
        })}
      </div>
    </div>
  )
}
