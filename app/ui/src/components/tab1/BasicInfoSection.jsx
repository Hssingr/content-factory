import { useEffect, useRef, useState } from 'react'
import AISuggestionField from '../AISuggestionField'
import { api } from '../../api/agent1'
import { TONES, OUTPUT_MODES, OUTPUT_MODE_DESCRIPTIONS, VISUAL_STYLE_OPTIONS, IMAGE_STYLE_OPTIONS, SCRIPT_SOURCES } from '../../constants'

const LOADING_STEPS = [
  'Analyzing niche opportunities…',
  'Checking platform fit…',
  'Estimating monetization potential…',
  'Preparing channel recommendation…',
]

const SOURCE_LABELS = {
  reddit: 'Reddit',
  claude_generated: 'Claude Generated',
  ai_generated: 'Claude Generated',
}

const OUTPUT_LABELS = {
  youtube_and_shorts: 'YouTube + Shorts',
  shorts_only: 'Shorts Only',
  youtube_long_only: 'YouTube long-form only',
}

function prettyPotential(value) {
  return String(value || '').replace('_', ' ') || 'unknown'
}

// Dialog overlay showing full research/validation result.
// Closed by "Use this recommendation" or the ✕ button.
function ResearchDialog({ result, onUse, onClose }) {
  if (!result?.primary_recommendation) return null
  const rec = result.primary_recommendation

  return (
    <div className="research-dialog-backdrop" onClick={onClose}>
      <div className="research-dialog" onClick={e => e.stopPropagation()}>
        <button type="button" className="research-dialog-close" onClick={onClose} aria-label="Close">✕</button>

        <span className="research-kicker">AI market research estimate — not verified platform analytics</span>
        <h3 className="research-dialog-title">{rec.recommended_channel_concept}</h3>

        <div className="research-metrics">
          <div><span>RPM potential</span><strong>{prettyPotential(rec.rpm_potential)}</strong></div>
          <div><span>Growth potential</span><strong>{prettyPotential(rec.follower_growth_potential)}</strong></div>
          <div><span>Script source</span><strong>{SOURCE_LABELS[rec.best_script_source] || rec.best_script_source}</strong></div>
          <div><span>Output mode</span><strong>{OUTPUT_LABELS[rec.recommended_output_mode] || rec.recommended_output_mode}</strong></div>
        </div>

        <div className="research-section">
          <h4>Why this subject?</h4>
          <p>{rec.why_selected}</p>
        </div>

        <div className="research-grid">
          <div className="research-section">
            <h4>Platform suitability</h4>
            <ul>
              {(rec.platform_suitability || []).map(item => (
                <li key={item.platform}><strong>{item.platform}</strong>: {prettyPotential(item.fit)} — {item.reasoning}</li>
              ))}
            </ul>
          </div>
          <div className="research-section">
            <h4>Creative direction</h4>
            <ul>
              <li>Visual style: {rec.recommended_visual_style}</li>
              <li>Image style: {rec.recommended_image_style}</li>
              <li>Tone: {rec.recommended_tone}</li>
              <li>Languages: {(rec.recommended_target_languages || []).join(', ') || 'none'}</li>
              <li>Platforms: {(rec.recommended_platforms || []).join(', ') || 'none'}</li>
            </ul>
          </div>
        </div>

        <div className="research-grid">
          <div className="research-section">
            <h4>Suggested names</h4>
            <ul>{(rec.suggested_channel_names || []).map(name => <li key={name}>{name}</li>)}</ul>
          </div>
          <div className="research-section">
            <h4>Example video ideas</h4>
            <ul>{(rec.example_video_ideas || []).map(idea => <li key={idea}>{idea}</li>)}</ul>
          </div>
        </div>

        <div className="research-section">
          <h4>Risks / difficulty</h4>
          <ul>{(rec.risks_difficulty || []).map(risk => <li key={risk}>{risk}</li>)}</ul>
        </div>

        {(rec.editable_config?.subreddits || []).length > 0 && (
          <div className="research-section">
            <h4>Recommended subreddits</h4>
            <p>{rec.editable_config.subreddits.join(', ')}</p>
          </div>
        )}

        {(result.references_used || []).length > 0 && (
          <div className="research-section">
            <h4>Sources consulted</h4>
            <ul>{result.references_used.map(ref => <li key={ref}>{ref}</li>)}</ul>
          </div>
        )}

        {rec.assumption_note && <p className="research-note">Note: {rec.assumption_note}</p>}
        <p className="research-summary">{rec.final_recommendation_summary}</p>

        <div className="research-dialog-actions">
          <button type="button" className="btn-primary" onClick={() => onUse(result)}>
            Use this recommendation
          </button>
          <button type="button" className="btn-secondary" onClick={onClose}>
            Continue without applying
          </button>
        </div>
      </div>
    </div>
  )
}

export default function BasicInfoSection({
  description, setDescription, name, setName, niche, setNiche, tone, setTone, ctx,
  contentMode, languages, platforms, onUseRecommendation,
  outputMode, setOutputMode,
  visualStyle, setVisualStyle,
  imageStyle, setImageStyle,
  scriptSource, setScriptSource,
  sources, setSources,
  // Flow control props — wired by App.jsx to drive the StepShell button
  initialShowEditable = false,
  onReady,
  onRegisterTrigger,
  onLoadingChange,
}) {
  const hasDescription = description.trim().length > 0

  const [subInput, setSubInput] = useState('')
  const [loading, setLoadingRaw] = useState(false)
  const [loadingStep, setLoadingStep] = useState(0)
  const [error, setError] = useState('')
  const [result, setResult] = useState(null)
  const [dialogOpen, setDialogOpen] = useState(false)
  // Progressive reveal: editable fields appear only after the user takes an action
  const [showEditable, setShowEditable] = useState(initialShowEditable)

  const setLoading = (val) => {
    setLoadingRaw(val)
    onLoadingChange?.(val)
  }

  // Keep a stable trigger ref that always calls the right action for current state
  const triggerRef = useRef()
  triggerRef.current = () => runAction(hasDescription ? 'validate' : 'research')

  useEffect(() => {
    onRegisterTrigger?.(() => triggerRef.current())
  }, [onRegisterTrigger])

  useEffect(() => {
    if (!loading) return undefined
    const timer = setInterval(() => {
      setLoadingStep(prev => Math.min(prev + 1, LOADING_STEPS.length - 1))
    }, 900)
    return () => clearInterval(timer)
  }, [loading])

  const runAction = async (actionType) => {
    if (loading) return
    setLoading(true)
    setLoadingStep(0)
    setError('')
    try {
      const res = await api.researchIdeas({
        channel_description: description,
        mode: actionType === 'research' ? 'explore' : 'validate',
        content_mode: contentMode || 'single_story',
        target_languages: languages || [],
        target_platforms: platforms || [],
      })
      setResult(res)
      setDialogOpen(true)
    } catch (e) {
      setError(e.message)
    } finally {
      setLoading(false)
    }
  }

  const handleUseRecommendation = (res) => {
    setDialogOpen(false)
    setShowEditable(true)
    onUseRecommendation(res)
    onReady?.()
  }

  const handleCloseDialog = () => {
    setDialogOpen(false)
    setShowEditable(true)
    onReady?.()
  }

  const handleSkip = () => {
    setShowEditable(true)
    onReady?.()
  }

  return (
    <>
      <div className="field">
        <label className="field-label">Channel description</label>
        <textarea
          className="field-input field-textarea"
          value={description}
          onChange={e => setDescription(e.target.value)}
          placeholder="Describe what you want to create — topic, audience, style, goal… (or leave blank and let Research Ideas find an idea for you)"
          rows={4}
        />
      </div>

      {/* Two-path action panel */}
      <section className="research-action-panel">
        <div className="research-action-paths">
          {/* Path A: no description yet — Research Ideas to find an idea */}
          <div className={`research-path${!hasDescription ? ' research-path--active' : ''}`}>
            <h4>Don't have an idea yet?</h4>
            <p>Claude will suggest a channel concept, estimate niche opportunity, platform fit, and monetization potential.</p>
            <button
              type="button"
              className="btn-primary btn-research"
              onClick={() => runAction('research')}
              disabled={hasDescription || loading}
            >
              {loading && !hasDescription ? 'Researching…' : '✨ Research Ideas'}
            </button>
          </div>

          <div className="research-path-divider">or</div>

          {/* Path B: description written — Validate your idea */}
          <div className={`research-path${hasDescription ? ' research-path--active' : ''}`}>
            <h4>Already have a concept?</h4>
            <p>Claude will analyse your description, validate the niche, and suggest refinements before you continue.</p>
            <button
              type="button"
              className="btn-secondary btn-research"
              onClick={() => runAction('validate')}
              disabled={!hasDescription || loading}
            >
              {loading && hasDescription ? 'Analysing…' : '✨ Validate Description'}
            </button>
          </div>
        </div>

        {loading && (
          <div className="research-loading">
            {LOADING_STEPS.map((step, index) => (
              <div key={step} className={index <= loadingStep ? 'active' : ''}>{step}</div>
            ))}
          </div>
        )}
        {error && <p className="field-error">{error}</p>}

        {!showEditable && !loading && (
          <button type="button" className="btn-ghost" onClick={handleSkip} style={{ marginTop: 8 }}>
            Skip — I'll fill in details manually
          </button>
        )}
      </section>

      {/* Result dialog */}
      {dialogOpen && result && (
        <ResearchDialog
          result={result}
          onUse={handleUseRecommendation}
          onClose={handleCloseDialog}
        />
      )}

      {/* Editable proposal fields — revealed after action or skip */}
      {showEditable && (
        <>
          <AISuggestionField
            label="Channel name"
            field="name"
            value={name}
            onChange={setName}
            context={ctx}
            placeholder="e.g. Decoded History"
          />
          <AISuggestionField
            label="Niche"
            field="niche"
            value={niche}
            onChange={setNiche}
            context={ctx}
            placeholder="e.g. cold war espionage"
          />
          <AISuggestionField
            label="Tone"
            field="tone"
            value={tone}
            onChange={setTone}
            context={ctx}
            options={TONES}
          />

          {/* ── Content direction ─────────────────────── */}
          <div className="field">
            <label className="field-label">Output mode</label>
            <select
              className="field-select"
              value={outputMode}
              onChange={e => setOutputMode(e.target.value)}
              style={{ maxWidth: 340 }}
            >
              {OUTPUT_MODES.map(o => (
                <option key={o.value} value={o.value} disabled={!o.executable}>{o.label}</option>
              ))}
            </select>
            {OUTPUT_MODE_DESCRIPTIONS[outputMode] && (
              <p className="voice-description" style={{ marginTop: 4 }}>
                {OUTPUT_MODE_DESCRIPTIONS[outputMode]}
              </p>
            )}
          </div>

          <div className="field-row">
            <div className="field">
              <label className="field-label">Visual style</label>
              <select
                className="field-select"
                value={visualStyle}
                onChange={e => setVisualStyle(e.target.value)}
              >
                {VISUAL_STYLE_OPTIONS.map(o => (
                  <option key={o.value} value={o.value}>{o.label}</option>
                ))}
              </select>
              {VISUAL_STYLE_OPTIONS.find(o => o.value === visualStyle)?.description && (
                <p className="voice-description" style={{ marginTop: 4 }}>
                  {VISUAL_STYLE_OPTIONS.find(o => o.value === visualStyle).description}
                </p>
              )}
            </div>
            <div className="field">
              <label className="field-label">Image style</label>
              <select
                className="field-select"
                value={imageStyle}
                onChange={e => setImageStyle(e.target.value)}
              >
                {IMAGE_STYLE_OPTIONS.map(o => (
                  <option key={o.value} value={o.value}>{o.label}</option>
                ))}
              </select>
              {IMAGE_STYLE_OPTIONS.find(o => o.value === imageStyle)?.description && (
                <p className="voice-description" style={{ marginTop: 4 }}>
                  {IMAGE_STYLE_OPTIONS.find(o => o.value === imageStyle).description}
                </p>
              )}
            </div>
          </div>

          {/* ── Script source ─────────────────────── */}
          <div className="field">
            <label className="field-label">Script source</label>
            <select
              className="field-select"
              value={scriptSource}
              onChange={e => setScriptSource(e.target.value)}
              style={{ maxWidth: 320 }}
            >
              {SCRIPT_SOURCES.map(s => (
                <option key={s.value} value={s.value} disabled={!s.executable}>{s.label}</option>
              ))}
            </select>
          </div>

          {/* ── Sources (subreddits) ─────────────── */}
          {scriptSource === 'reddit' && (
            <div className="field">
              <label className="field-label">Reddit sources</label>
              {sources.length > 0 && (
                <div className="source-added-list" style={{ marginBottom: 8 }}>
                  {sources.map((src, i) => (
                    <div key={i} className="source-item">
                      <span className="source-type-pill">{src.source_type}</span>
                      <span className="source-value">{src.source_value}</span>
                      <button
                        type="button"
                        className="btn-remove"
                        onClick={() => setSources(prev => prev.filter((_, idx) => idx !== i))}
                      >✕</button>
                    </div>
                  ))}
                </div>
              )}
              <div className="field-row" style={{ gap: 8 }}>
                <input
                  className="field-input"
                  value={subInput}
                  onChange={e => setSubInput(e.target.value)}
                  placeholder="r/subreddit or subreddit name"
                  onKeyDown={e => {
                    if (e.key !== 'Enter' || !subInput.trim()) return
                    setSources(prev => [...prev, { source_type: 'reddit', source_value: subInput.trim(), language: languages?.[0] || 'en', trust_score: 1.0 }])
                    setSubInput('')
                  }}
                  style={{ maxWidth: 280 }}
                />
                <button
                  type="button"
                  className="btn-secondary"
                  disabled={!subInput.trim()}
                  onClick={() => {
                    setSources(prev => [...prev, { source_type: 'reddit', source_value: subInput.trim(), language: languages?.[0] || 'en', trust_score: 1.0 }])
                    setSubInput('')
                  }}
                >+ Add</button>
              </div>
            </div>
          )}
        </>
      )}
    </>
  )
}
