import { useState, useEffect, useRef } from 'react'
import './form.css'
import { api } from './api/agent1'
import ChannelList from './components/ChannelList'
import StepIndicator from './components/StepIndicator'
import ReadinessSidebar from './components/ReadinessSidebar'
import StepShell from './components/StepShell'
import ModeStep from './components/ModeStep'
import CredentialsStep from './components/CredentialsStep'
import ActivationStep from './components/ActivationStep'
import BasicInfoSection from './components/tab1/BasicInfoSection'
import LanguagesSection from './components/tab1/LanguagesSection'
import VoicesSection from './components/tab1/VoicesSection'
import ScheduleSection from './components/tab1/ScheduleSection'
import PlatformsSection from './components/tab1/PlatformsSection'

const STEPS = [
  { id: 'mode',        label: 'Mode' },
  { id: 'basics',      label: 'Concept' },
  { id: 'languages',   label: 'Languages' },
  { id: 'voices',      label: 'Voices' },
  { id: 'schedule',    label: 'Schedule' },
  { id: 'platforms',   label: 'Platforms' },
  { id: 'credentials', label: 'Credentials' },
  { id: 'activation',  label: 'Activation' },
]

export default function App() {
  const [view, setView] = useState('list')
  const [currentStep, setCurrentStep] = useState('mode')
  const [completedSteps, setCompletedSteps] = useState([])
  const [channelId, setChannelId] = useState(null)
  const [userLanguage, setUserLanguage] = useState('en')
  const [error, setError] = useState('')
  const [saving, setSaving] = useState(false)

  // ── Content Factory V3 — content mode (Discovery) ──────────────
  const [contentMode, setContentMode] = useState('single_story')

  // ── Basic info ───────────────────────────────────────────────
  const [description, setDescription] = useState('')
  const [name, setName] = useState('')
  const [niche, setNiche] = useState('')
  const [tone, setTone] = useState('documentary')

  // ── Languages ────────────────────────────────────────────────
  const [languages, setLanguages] = useState([])
  const [langNames, setLangNames] = useState({})

  // ── Voices ───────────────────────────────────────────────────
  const [voices, setVoices] = useState({})

  // ── Schedule + Content Factory V3 fields ────────────────────
  const [videosPerWeek, setVideosPerWeek] = useState(3)
  const [shortsRule, setShortsRule] = useState('auto')
  const [timings, setTimings] = useState([])
  const [suggestingTime, setSuggestingTime] = useState(false)
  const [scriptSource, setScriptSource] = useState('reddit')
  const [outputMode, setOutputMode] = useState('youtube_and_shorts')
  const [visualStyle, setVisualStyle] = useState('documentary')
  const [imageStyle, setImageStyle] = useState('photorealistic')

  // ── Sources ──────────────────────────────────────────────────
  const [sources, setSources] = useState([])

  // ── Platforms ────────────────────────────────────────────────
  const [platforms, setPlatforms] = useState([])

  // ── Basics step action gating ────────────────────────────────
  // basicsReady: true once the operator has seen a result (or is editing an existing channel)
  const [basicsReady, setBasicsReady] = useState(false)
  const [basicsLoading, setBasicsLoading] = useState(false)
  const basicsTriggerRef = useRef(null)

  useEffect(() => {
    api.getMe().then(u => setUserLanguage(u.primary_language ?? 'en')).catch(console.error)
  }, [])

  // ── Restore an existing channel ─────────────────────────────
  useEffect(() => {
    if (!channelId) return
    api.getChannel(channelId).then(ch => {
      setDescription(ch.description ?? '')
      setName(ch.name ?? '')
      setNiche(ch.niche ?? '')
      setTone(ch.tone ?? 'documentary')

      const langs = ch.languages.map(l => l.language)
      setLanguages(langs)
      const names = {}
      ch.languages.forEach(l => { names[l.language] = l.channel_name })
      setLangNames(names)

      const voicesObj = {}
      ch.voices.forEach(v => {
        voicesObj[v.language] = {
          provider: v.provider || 'cartesia',
          tts_model: v.tts_model || (v.provider === 'elevenlabs' ? 'eleven_v3' : 'sonic-3.5'),
          voice_id: v.voice_id || '',
          voice_validated: Boolean(v.voice_id),
        }
      })
      setVoices(voicesObj)
      if (ch.config) {
        setVideosPerWeek(ch.config.videos_per_week ?? 3)
        setShortsRule(ch.config.shorts_rule ?? 'auto')
        setScriptSource(ch.config.script_source ?? 'reddit')
        setOutputMode(ch.config.output_mode ?? 'youtube_and_shorts')
        setVisualStyle(ch.config.visual_style ?? 'documentary')
        setImageStyle(ch.config.image_style ?? 'photorealistic')
        setContentMode(ch.config.content_mode ?? 'single_story')
      }

      if (ch.publish_timings && ch.publish_timings.length > 0) {
        const seen = new Set()
        const restored = ch.publish_timings
          .filter(t => { if (seen.has(t.language)) return false; seen.add(t.language); return true })
          .map(t => ({
            language: t.language,
            timezone: t.timezone,
            optimal_days: t.optimal_days,
            optimal_hour_start: t.optimal_hour_start,
            optimal_hour_end: t.optimal_hour_end,
            shorts_spread_hours: t.shorts_spread_hours,
          }))
        setTimings(restored)
      }

      setSources(ch.sources.map(s => ({
        source_type: s.source_type,
        source_value: s.source_value,
        language: s.language,
        trust_score: s.trust_score,
      })))

      const uniquePlatforms = [...new Set(ch.platforms.map(p => p.platform))]
      setPlatforms(uniquePlatforms)

      setCompletedSteps(prev => {
        const next = new Set(prev)
        next.add('mode')
        next.add('basics')
        if (langs.length > 0) next.add('languages')
        if (ch.voices.length > 0) next.add('voices')
        if (ch.config) next.add('schedule')
        if (uniquePlatforms.length > 0) next.add('platforms')
        return [...next]
      })
    }).catch(e => setError(e.message))
  }, [channelId])

  const ctx = () => ({
    description, name, niche, tone,
    languages,
    language_names: langNames,
    videos_per_week: videosPerWeek,
    shorts_rule: shortsRule,
  })

  const markDone = (step) => setCompletedSteps(prev => prev.includes(step) ? prev : [...prev, step])

  const run = async (fn) => {
    setSaving(true)
    setError('')
    try {
      await fn()
      return true
    } catch (e) {
      setError(e.message)
      return false
    } finally {
      setSaving(false)
    }
  }

  // ── Step handlers ────────────────────────────────────────────
  const handleMode = () => { markDone('mode'); setCurrentStep('basics') }


  const applyResearchRecommendation = (result) => {
    const rec = result?.primary_recommendation
    const config = rec?.editable_config
    if (!config) return

    setName(config.channel_name || rec.suggested_channel_names?.[0] || name)
    setDescription(config.description || description)
    setNiche(config.niche || niche)
    setTone(config.tone || rec.recommended_tone || tone)
    setScriptSource(config.script_source || 'reddit')
    setOutputMode(config.output_mode || rec.recommended_output_mode || 'youtube_and_shorts')
    setVisualStyle(config.visual_style || rec.recommended_visual_style || visualStyle)
    setImageStyle(config.image_style || rec.recommended_image_style || imageStyle)
    setVideosPerWeek(config.videos_per_week || videosPerWeek)

    if (Array.isArray(config.languages) && config.languages.length > 0) {
      setLanguages(config.languages)
      setLangNames(prev => {
        const next = { ...prev }
        config.languages.forEach((lang, index) => {
          if (!next[lang]) next[lang] = index === 0 ? (config.channel_name || name) : ''
        })
        return next
      })
    }

    if (Array.isArray(config.platforms) && config.platforms.length > 0) {
      setPlatforms(config.platforms)
    }

    if (config.script_source === 'reddit' && Array.isArray(config.subreddits)) {
      setSources(config.subreddits.map(source => ({
        source_type: 'reddit',
        source_value: source,
        language: config.languages?.[0] || userLanguage,
        trust_score: 1.0,
      })))
    }
  }

  const handleBasics = () => run(async () => {
    let id = channelId
    if (!id) {
      const ch = await api.createChannel({ name, description, niche, tone })
      id = ch.id
      setChannelId(id)
    } else {
      await api.updateChannel(channelId, { name, description, niche, tone })
    }
    await api.upsertConfig(id, {
      content_mode: contentMode,
      script_source: scriptSource,
      output_mode: outputMode,
      visual_style: visualStyle,
      image_style: imageStyle,
      videos_per_week: videosPerWeek,
      shorts_rule: shortsRule,
    })
    const sourceEntries = sources
      .filter(s => s.source_value.trim())
      .map(s => ({
        source_type: s.source_type,
        source_value: s.source_value.trim(),
        language: s.language || (languages[0] ?? 'en'),
        trust_score: s.trust_score,
      }))
    if (sourceEntries.length > 0) await api.replaceSources(id, sourceEntries)
    markDone('basics')
    setCurrentStep('languages')
  })

  const handleLanguages = () => run(async () => {
    const entries = languages.map(lang => ({
      language: lang,
      channel_name: lang === userLanguage ? name : (langNames[lang] ?? ''),
    }))
    await api.replaceLanguages(channelId, entries)
    setVoices(prev => {
      const next = { ...prev }
      languages.forEach(l => {
        if (!next[l]) {
          next[l] = { provider: 'cartesia', tts_model: 'sonic-3.5', voice_id: '', voice_validated: false }
        }
      })
      return next
    })
    markDone('languages')
    setCurrentStep('voices')
  })

  const handleVoices = () => run(async () => {
    const entries = languages.map(lang => {
      const voice = voices[lang] || {}
      const provider = voice.provider || 'cartesia'
      return {
        language: lang,
        provider,
        tts_model: voice.tts_model || (provider === 'elevenlabs' ? 'eleven_v3' : 'sonic-3.5'),
        voice_id: voice.voice_id ?? '',
        emotion: null,
        music_style: null,
        use_case: null,
      }
    })
    await api.replaceVoices(channelId, entries)
    markDone('voices')
    setCurrentStep('schedule')
  })

  const suggestTiming = async () => {
    setSuggestingTime(true)
    setError('')
    try {
      const suggestions = await api.suggestTiming(channelId)
      setTimings(suggestions.filter(s => !s.error))
    } catch (e) {
      setError(e.message)
    } finally {
      setSuggestingTime(false)
    }
  }

  const handleSchedule = () => run(async () => {
    await api.upsertConfig(channelId, {
      videos_per_week: videosPerWeek, shorts_rule: shortsRule,
    })
    if (timings.length > 0) {
      const entries = timings.map(t => ({
        platform: 'youtube',
        language: t.language,
        timezone: t.timezone || 'UTC',
        optimal_days: t.optimal_days || [],
        optimal_hour_start: t.optimal_hour_start ?? 18,
        optimal_hour_end: t.optimal_hour_end ?? 20,
        shorts_spread_hours: t.shorts_spread_hours ?? 6,
      }))
      await api.upsertTimings(channelId, entries)
    }
    markDone('schedule')
    setCurrentStep('platforms')
  })

  const handlePlatforms = () => {
    markDone('platforms')
    setCurrentStep('credentials')
  }

  const toggleLang = code => setLanguages(p => p.includes(code) ? p.filter(c => c !== code) : [...p, code])
  const togglePlatform = id => setPlatforms(p => p.includes(id) ? p.filter(x => x !== id) : [...p, id])

  const openCreate = () => {
    setChannelId(null)
    setCompletedSteps([])
    setError('')
    setLanguages([])
    setLangNames({})
    setVoices({})
    setPlatforms([])
    setSources([])
    setTimings([])
    setContentMode('single_story')
    setName(''); setDescription(''); setNiche(''); setTone('documentary')
    setBasicsReady(false)
    setBasicsLoading(false)
    setCurrentStep('mode')
    setView('setup')
  }

  const openEdit = (id) => {
    setChannelId(id)
    setCompletedSteps([])
    setError('')
    setBasicsReady(true)   // existing channel — operator can save directly
    setBasicsLoading(false)
    setCurrentStep('basics')
    setView('setup')
  }

  const backToList = () => {
    setView('list')
    setCurrentStep('mode')
  }

  if (view === 'list') {
    return (
      <div className="app">
        <div className="app-header">
          <div className="app-logo">⚡</div>
          <div>
            <h1>Content Factory</h1>
            <div><span className="app-live-dot" /><span className="app-subtitle">My Channels</span></div>
          </div>
        </div>
        <ChannelList onEdit={openEdit} onCreate={openCreate} />
      </div>
    )
  }

  const proposal = { name, niche, languages, platforms, videosPerWeek }

  const readinessItems = [
    { id: 'mode', label: 'Content mode selected', done: completedSteps.includes('mode') },
    { id: 'basics', label: 'Channel concept saved', done: completedSteps.includes('basics') },
    { id: 'languages', label: 'Languages configured', done: completedSteps.includes('languages') },
    { id: 'voices', label: 'Voices configured', done: completedSteps.includes('voices') },
    { id: 'schedule', label: 'Schedule calibrated', done: completedSteps.includes('schedule') },
    { id: 'platforms', label: 'Platforms selected', done: completedSteps.includes('platforms') },
    { id: 'credentials', label: 'Credentials verified', done: completedSteps.includes('credentials') },
  ]

  const showSidebar = currentStep !== 'mode' && currentStep !== 'activation'

  return (
    <div className="app">
      <StepIndicator
        steps={STEPS}
        currentStep={currentStep}
        completedSteps={completedSteps}
        onNavigate={setCurrentStep}
      />

      <div className={showSidebar ? 'wizard-body' : ''}>
        <div className={showSidebar ? 'wizard-main' : ''}>
          {currentStep === 'mode' && (
            <ModeStep
              contentMode={contentMode}
              setContentMode={setContentMode}
              onNext={handleMode}
              onCancel={backToList}
            />
          )}

          {currentStep === 'basics' && (
            <StepShell
              eyebrow="Step 2 — Concept"
              title="Describe this channel"
              subtitle="Research an idea or validate your concept — Claude will suggest configuration including sources. Then save and continue."
              onBack={() => setCurrentStep('mode')}
              onNext={basicsReady
                ? handleBasics
                : () => basicsTriggerRef.current?.()}
              nextLabel={basicsReady
                ? 'Save & continue →'
                : (description.trim() ? 'Validate →' : 'Research Ideas →')}
              nextLoading={basicsReady ? saving : basicsLoading}
              nextLoadingLabel={basicsReady ? 'Saving…' : (description.trim() ? 'Validating…' : 'Researching…')}
              error={error}
            >
              <BasicInfoSection
                key={channelId || 'new'}
                description={description} setDescription={setDescription}
                name={name} setName={setName}
                niche={niche} setNiche={setNiche}
                tone={tone} setTone={setTone}
                ctx={ctx()}
                contentMode={contentMode}
                languages={languages}
                platforms={platforms}
                onUseRecommendation={applyResearchRecommendation}
                outputMode={outputMode} setOutputMode={setOutputMode}
                visualStyle={visualStyle} setVisualStyle={setVisualStyle}
                imageStyle={imageStyle} setImageStyle={setImageStyle}
                scriptSource={scriptSource} setScriptSource={setScriptSource}
                sources={sources} setSources={setSources}
                initialShowEditable={!!channelId}
                onReady={() => setBasicsReady(true)}
                onRegisterTrigger={fn => { basicsTriggerRef.current = fn }}
                onLoadingChange={setBasicsLoading}
              />
            </StepShell>
          )}

          {currentStep === 'languages' && (
            <StepShell
              eyebrow="Step 3 — Languages"
              title="Select active languages"
              subtitle="Every language gets its own scripts, audio, and captions generated independently."
              onBack={() => setCurrentStep('basics')}
              onNext={handleLanguages}
              nextLoading={saving}
              nextDisabled={languages.length === 0}
              error={error}
            >
              <LanguagesSection
                selected={languages} onToggle={toggleLang} langNames={langNames} setLangNames={setLangNames}
                ctx={ctx()} primaryLanguage={userLanguage} primaryName={name}
              />
            </StepShell>
          )}

          {currentStep === 'voices' && (
            <StepShell
              eyebrow="Step 4 — Voices"
              title="Configure narration voices"
              subtitle="Pick a provider, model, and voice per language — used directly by Agent 3's TTS step."
              onBack={() => setCurrentStep('languages')}
              onNext={handleVoices}
              nextLoading={saving}
              error={error}
            >
              <VoicesSection
                languages={languages} voices={voices} setVoices={setVoices}
              />
            </StepShell>
          )}

          {currentStep === 'schedule' && (
            <StepShell
              eyebrow="Step 5 — Schedule"
              title="Set cadence and content configuration"
              subtitle="Set publication cadence and Shorts policy for Agent 2's discovery loop."
              onBack={() => setCurrentStep('voices')}
              onNext={handleSchedule}
              nextLoading={saving}
              error={error}
            >
              <ScheduleSection
                videosPerWeek={videosPerWeek} setVideosPerWeek={setVideosPerWeek}
                shortsRule={shortsRule} setShortsRule={setShortsRule}
                timings={timings} setTimings={setTimings}
                onSuggestTiming={suggestTiming} suggestingTiming={suggestingTime}
                languagesSaved={completedSteps.includes('languages')}
                channelId={channelId}
              />
            </StepShell>
          )}

          {currentStep === 'platforms' && (
            <StepShell
              eyebrow="Step 6 — Target platforms"
              title="Select target platforms"
              subtitle="Each platform selected here gets its own credential row in the next step, per language."
              onBack={() => setCurrentStep('schedule')}
              onNext={handlePlatforms}
              nextDisabled={platforms.length === 0}
            >
              <PlatformsSection selected={platforms} onToggle={togglePlatform} />
            </StepShell>
          )}

          {currentStep === 'credentials' && (
            <CredentialsStep
              channelId={channelId}
              languages={languages}
              platforms={platforms}
              onBack={() => setCurrentStep('platforms')}
              onNext={() => { markDone('credentials'); setCurrentStep('activation') }}
            />
          )}

          {currentStep === 'activation' && (
            <ActivationStep
              channelId={channelId}
              proposal={proposal}
              onBack={() => setCurrentStep('credentials')}
              onReset={backToList}
            />
          )}
        </div>

        {showSidebar && (
          <div className="wizard-sidebar">
            <ReadinessSidebar items={readinessItems} currentStep={currentStep} />
          </div>
        )}
      </div>
    </div>
  )
}
