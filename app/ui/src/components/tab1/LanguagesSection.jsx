import AISuggestionField from '../AISuggestionField'
import { LANGUAGES } from '../../constants'

export default function LanguagesSection({ selected, onToggle, langNames, setLangNames, ctx, primaryLanguage, primaryName }) {
  return (
    <>
      <div className="field">
        <label className="field-label">Target languages</label>
        <div className="lang-grid">
          {LANGUAGES.map(l => (
            <button
              key={l.code}
              type="button"
              className={`lang-chip${selected.includes(l.code) ? ' selected' : ''}`}
              onClick={() => onToggle(l.code)}
            >
              {l.label}
            </button>
          ))}
        </div>
      </div>

      {selected.length > 0 && (
        <div className="field">
          <label className="field-label">Channel name per language</label>
          <div className="lang-names">
            {selected.map(code => (
              <div key={code} className="lang-name-row">
                <span className="lang-code-pill">{code}</span>
                {code === primaryLanguage ? (
                  // Primary language name mirrors the Basic Info name — not editable here
                  <div className="field" style={{ flex: 1 }}>
                    <input
                      className="field-input"
                      type="text"
                      value={primaryName}
                      disabled
                      placeholder="Filled from channel name above"
                    />
                  </div>
                ) : (
                  <AISuggestionField
                    field="name"
                    value={langNames[code] ?? ''}
                    onChange={v => setLangNames(prev => ({ ...prev, [code]: v }))}
                    context={{ ...ctx, language: code }}
                    placeholder={`Channel name in ${code}`}
                  />
                )}
              </div>
            ))}
          </div>
        </div>
      )}
    </>
  )
}
