export default function Section({ title, step, unlocked, saved, saving, onSave, saveLabel, children }) {
  return (
    <div className={`section${unlocked ? '' : ' section--locked'}`}>
      <div className="section-header">
        <span className="section-step">{step}</span>
        <h3 className="section-title">{title}</h3>
        {saved    && <span className="badge badge--success">✓ Saved</span>}
        {!unlocked && <span className="badge badge--locked">🔒</span>}
      </div>
      {unlocked && (
        <>
          <div className="section-body">{children}</div>
          <div className="section-footer">
            <button
              type="button"
              className="btn-primary"
              onClick={onSave}
              disabled={saving}
            >
              {saving ? 'Saving…' : (saveLabel ?? (saved ? 'Update' : 'Save & continue →'))}
            </button>
          </div>
        </>
      )}
    </div>
  )
}
