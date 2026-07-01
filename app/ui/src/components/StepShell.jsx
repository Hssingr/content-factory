export default function StepShell({
  eyebrow, title, subtitle, children,
  onBack, backLabel = '← Back',
  onNext, nextLabel = 'Save & continue →',
  nextDisabled = false, nextLoading = false,
  nextLoadingLabel = 'Saving…',
  error,
}) {
  return (
    <div className="step-shell">
      <span className="step-shell-eyebrow">{eyebrow}</span>
      <h2 className="step-shell-title">{title}</h2>
      {subtitle && <p className="step-shell-subtitle">{subtitle}</p>}

      {error && <div className="error-banner" style={{ marginTop: 16 }}>{error}</div>}

      <div className="step-shell-card">{children}</div>

      <div className="step-shell-footer">
        {onBack ? (
          <button type="button" className="btn-secondary" onClick={onBack}>{backLabel}</button>
        ) : <span />}
        {onNext && (
          <button
            type="button"
            className="btn-primary"
            onClick={onNext}
            disabled={nextDisabled || nextLoading}
          >
            {nextLoading ? nextLoadingLabel : nextLabel}
          </button>
        )}
      </div>
    </div>
  )
}
