import { useState, useEffect } from 'react'
import '../form.css'
import { api } from '../api/agent1'
import Section from './Section'
import BasicInfoSection   from './tab1/BasicInfoSection'
import LanguagesSection   from './tab1/LanguagesSection'
import VoicesSection      from './tab1/VoicesSection'
import ScheduleSection    from './tab1/ScheduleSection'
import SourcesSection     from './tab1/SourcesSection'
import PlatformsSection   from './tab1/PlatformsSection'

export default function Tab1Config({ channelId, userLanguage = 'en', onChannelCreated, onLanguagesChange, onPlatformsChange, onNext, onCancel }) {
  // ── Section save state ──────────────────────────────────────
  const [saved,  setSaved]  = useState({ s1: false, s2: false, s3: false, s4: false, s5: false, s6: false })
  const [saving, setSaving] = useState({ s1: false, s2: false, s3: false, s4: false, s5: false, s6: false })
  const [error,  setError]  = useState('')

  // ── Section 1 — Basic info ──────────────────────────────────
  const [description, setDescription] = useState('')
  const [name,        setName]        = useState('')
  const [niche,       setNiche]       = useState('')
  const [tone,        setTone]        = useState('documentary')

  // ── Section 2 — Languages ───────────────────────────────────
  const [languages, setLanguages] = useState([])
  const [langNames, setLangNames] = useState({})

  // ── Section 3 — Voices ──────────────────────────────────────
  const [voices,           setVoices]          = useState({})
  const [sharedUseCase,    setSharedUseCase]   = useState('')
  const [sharedEmotion,    setSharedEmotion]   = useState('neutral')
  const [sharedMusicStyle, setSharedMusicStyle] = useState('cinematic')

  // ── Section 4 — Schedule + Timing ──────────────────────────
  const [videosPerWeek,  setVideosPerWeek]  = useState(3)
  const [shortsRule,     setShortsRule]     = useState('auto')
  const [timings,        setTimings]        = useState([])
  const [suggestingTime, setSuggestingTime] = useState(false)

  // ── Section 5 — Sources ─────────────────────────────────────
  const [sources, setSources] = useState([])

  // ── Section 6 — Platforms ───────────────────────────────────
  const [platforms, setPlatforms] = useState([])

  // ── Restore from DB when channelId is set (e.g. navigating back) ──
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
      ch.voices.forEach(v => { voicesObj[v.language] = { voice_id: v.voice_id } })
      setVoices(voicesObj)
      if (ch.voices.length > 0) {
        setSharedUseCase(ch.voices[0].use_case ?? '')
        setSharedEmotion(ch.voices[0].emotion ?? 'neutral')
        setSharedMusicStyle(ch.voices[0].music_style ?? 'cinematic')
      }

      if (ch.config) {
        setVideosPerWeek(ch.config.videos_per_week ?? 3)
        setShortsRule(ch.config.shorts_rule ?? 'auto')
      }

      // Restore publish timings (one per language from the most common platform row)
      if (ch.publish_timings && ch.publish_timings.length > 0) {
        // De-duplicate by language (take first row per language)
        const seen = new Set()
        const restored = ch.publish_timings
          .filter(t => { if (seen.has(t.language)) return false; seen.add(t.language); return true })
          .map(t => ({
            language:           t.language,
            timezone:           t.timezone,
            optimal_days:       t.optimal_days,
            optimal_hour_start: t.optimal_hour_start,
            optimal_hour_end:   t.optimal_hour_end,
            shorts_spread_hours: t.shorts_spread_hours,
          }))
        setTimings(restored)
      }

      setSources(ch.sources.map(s => ({
        source_type:  s.source_type,
        source_value: s.source_value,
        language:     s.language,
        trust_score:  s.trust_score,
      })))

      const uniquePlatforms = [...new Set(ch.platforms.map(p => p.platform))]
      setPlatforms(uniquePlatforms)
      onLanguagesChange(langs)
      onPlatformsChange(uniquePlatforms)

      setSaved({
        s1: true,
        s2: langs.length > 0,
        s3: ch.voices.length > 0,
        s4: !!ch.config,
        s5: ch.sources.length > 0,
        s6: false,
      })
    }).catch(err => console.error('Failed to load channel:', err))
  }, [channelId])

  // ── Shared context for AI suggestions ───────────────────────
  const ctx = () => ({
    description, name, niche, tone,
    languages,
    language_names: langNames,
    videos_per_week: videosPerWeek,
    shorts_rule: shortsRule,
  })

  // ── Unlock logic ─────────────────────────────────────────────
  const unlocked = n => [false, true, saved.s1, saved.s2, saved.s3, saved.s4, saved.s5][n]

  // ── Save helpers ─────────────────────────────────────────────
  const run = async (key, fn) => {
    setSaving(p => ({ ...p, [key]: true }))
    setError('')
    try {
      await fn()
      setSaved(p => ({ ...p, [key]: true }))
    } catch (e) {
      setError(e.message)
    } finally {
      setSaving(p => ({ ...p, [key]: false }))
    }
  }

  // ── Section save handlers ────────────────────────────────────
  const saveS1 = () => run('s1', async () => {
    if (!channelId) {
      const ch = await api.createChannel({ name, description, niche, tone })
      onChannelCreated(ch.id)
    } else {
      await api.updateChannel(channelId, { name, description, niche, tone })
    }
  })

  const saveS2 = () => run('s2', async () => {
    const entries = languages.map(lang => ({
      language:     lang,
      // Primary language mirrors the Basic Info name; others use the per-language input
      channel_name: lang === userLanguage ? name : (langNames[lang] ?? ''),
    }))
    await api.replaceLanguages(channelId, entries)
    setVoices(prev => {
      const next = { ...prev }
      languages.forEach(l => { if (!next[l]) next[l] = { voice_id: '' } })
      return next
    })
    onLanguagesChange(languages)
  })

  const saveS3 = () => run('s3', async () => {
    const entries = languages.map(lang => ({
      language:    lang,
      provider:    'elevenlabs',
      voice_id:    voices[lang]?.voice_id ?? '',
      emotion:     sharedEmotion,
      music_style: sharedMusicStyle,
      use_case:    sharedUseCase,
    }))
    await api.replaceVoices(channelId, entries)
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

  const saveS4 = () => run('s4', async () => {
    await api.upsertConfig(channelId, { videos_per_week: videosPerWeek, shorts_rule: shortsRule })
    if (timings.length > 0) {
      // Save one timing row per language (platform = "all" placeholder — Agent 7 uses platform-specific rows)
      const entries = timings.map(t => ({
        platform:           'youtube',   // default platform; duplicated per verified platform at publish time
        language:           t.language,
        timezone:           t.timezone   || 'UTC',
        optimal_days:       t.optimal_days || [],
        optimal_hour_start: t.optimal_hour_start ?? 18,
        optimal_hour_end:   t.optimal_hour_end   ?? 20,
        shorts_spread_hours: t.shorts_spread_hours ?? 6,
      }))
      await api.upsertTimings(channelId, entries)
    }
  })

  const saveS5 = () => run('s5', async () => {
    const entries = sources
      .filter(s => s.source_value.trim())
      .map(s => ({
        source_type:  s.source_type,
        source_value: s.source_value.trim(),
        language:     s.language || (languages[0] ?? 'en'),
        trust_score:  s.trust_score,
      }))
    await api.replaceSources(channelId, entries)
  })

  const saveS6 = () => run('s6', async () => {
    onPlatformsChange(platforms)
    onNext()
  })

  const toggleLang     = code => setLanguages(p => p.includes(code) ? p.filter(c => c !== code) : [...p, code])
  const togglePlatform = id   => setPlatforms(p => p.includes(id)   ? p.filter(x => x !== id)   : [...p, id])

  return (
    <div>
      {error && <div className="error-banner">{error}</div>}
      <div className="form-sections">

        <Section title="Basic info" step={1} unlocked={unlocked(1)} saved={saved.s1} saving={saving.s1} onSave={saveS1}>
          <BasicInfoSection
            description={description} setDescription={setDescription}
            name={name} setName={setName}
            niche={niche} setNiche={setNiche}
            tone={tone} setTone={setTone}
            ctx={ctx()}
          />
        </Section>

        <Section title="Languages" step={2} unlocked={unlocked(2)} saved={saved.s2} saving={saving.s2} onSave={saveS2}>
          <LanguagesSection
            selected={languages} onToggle={toggleLang} langNames={langNames} setLangNames={setLangNames}
            ctx={ctx()} primaryLanguage={userLanguage} primaryName={name}
          />
        </Section>

        <Section title="Voices" step={3} unlocked={unlocked(3)} saved={saved.s3} saving={saving.s3} onSave={saveS3}>
          <VoicesSection
            languages={languages} voices={voices} setVoices={setVoices}
            sharedUseCase={sharedUseCase} setSharedUseCase={setSharedUseCase}
            sharedEmotion={sharedEmotion} setSharedEmotion={setSharedEmotion}
            sharedMusicStyle={sharedMusicStyle} setSharedMusicStyle={setSharedMusicStyle}
            ctx={ctx()}
          />
        </Section>

        <Section title="Schedule" step={4} unlocked={unlocked(4)} saved={saved.s4} saving={saving.s4} onSave={saveS4}>
          <ScheduleSection
            videosPerWeek={videosPerWeek}    setVideosPerWeek={setVideosPerWeek}
            shortsRule={shortsRule}          setShortsRule={setShortsRule}
            timings={timings}               setTimings={setTimings}
            onSuggestTiming={suggestTiming} suggestingTiming={suggestingTime}
            languagesSaved={saved.s2}
            channelId={channelId}
          />
        </Section>

        <Section title="Content sources" step={5} unlocked={unlocked(5)} saved={saved.s5} saving={saving.s5} onSave={saveS5}>
          <SourcesSection sources={sources} setSources={setSources} languages={languages} ctx={ctx()} />
        </Section>

        <Section title="Target platforms" step={6} unlocked={unlocked(6)} saved={saved.s6} saving={saving.s6} onSave={saveS6} saveLabel="Continue to Credentials →">
          <PlatformsSection selected={platforms} onToggle={togglePlatform} />
        </Section>

      </div>

      <div style={{ marginTop: 16, paddingTop: 16, borderTop: '1px solid #2e2e40' }}>
        <button type="button" className="btn-secondary" onClick={onCancel}>Cancel</button>
      </div>
    </div>
  )
}
