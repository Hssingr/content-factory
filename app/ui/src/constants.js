export const LANGUAGES = [
  { code: 'fr', label: 'French' },
  { code: 'en', label: 'English' },
  { code: 'es', label: 'Spanish' },
  { code: 'de', label: 'German' },
  { code: 'it', label: 'Italian' },
  { code: 'pt', label: 'Portuguese' },
]

// Channel.tone flows verbatim into every script and storyboard prompt as
// "Channel tone: <value>" — plain spoken-register words work best. The list
// must cover tension/dread registers: the audited P1-9 incident (a horror
// channel locked to tone=documentary, producing flat neutral narration) was
// previously FORCED by this dropdown only offering neutral/informational
// values. Keep in sync with the tone line in
// app/agents/agent1_setup/system_prompt.py's suggest prompt.
export const TONES = [
  { value: 'suspenseful',    label: 'Suspenseful' },
  { value: 'ominous',        label: 'Ominous / Dark' },
  { value: 'dramatic',       label: 'Dramatic' },
  { value: 'conversational', label: 'Conversational' },
  { value: 'documentary',    label: 'Documentary' },
  { value: 'educational',    label: 'Educational' },
  { value: 'entertaining',   label: 'Entertaining' },
  { value: 'investigative',  label: 'Investigative' },
  { value: 'humorous',       label: 'Humorous' },
  { value: 'inspirational',  label: 'Inspirational' },
]

export const VOICE_PROVIDERS = [
  { value: 'cartesia', label: 'Cartesia' },
  { value: 'elevenlabs', label: 'ElevenLabs' },
]

export const VOICE_MODELS_BY_PROVIDER = {
  cartesia: [
    { value: 'sonic-3.5', label: 'sonic-3.5' },
    { value: 'sonic-3', label: 'sonic-3' },
    { value: 'sonic-2', label: 'sonic-2' },
  ],
  elevenlabs: [
    { value: 'eleven_v3', label: 'eleven_v3' },
    { value: 'eleven_multilingual_v2', label: 'eleven_multilingual_v2' },
  ],
}

export const DEFAULT_VOICE_MODEL_BY_PROVIDER = {
  cartesia: 'sonic-3.5',
  elevenlabs: 'eleven_v3',
}

export const SOURCE_TYPES = [
  { value: 'rss',     label: 'RSS Feed' },
  { value: 'reddit',  label: 'Reddit' },
  { value: 'youtube', label: 'YouTube Channel' },
  { value: 'web',     label: 'Website' },
]

export const PLATFORMS = [
  { id: 'youtube',   label: 'YouTube',   icon: '📺' },
  { id: 'tiktok',    label: 'TikTok',    icon: '🎵' },
  { id: 'instagram', label: 'Instagram', icon: '📸' },
  { id: 'facebook',  label: 'Facebook',  icon: '👥' },
]

// ── Content Factory V3 (Phase Agent1-V3.5) ──────────────────────────────────
// `executable` mirrors app/agents/agent1_setup/services/v3_config_rules.py's
// is_executable_*() helpers — see CLAUDE.md §8.2/§8.4. Keep these two lists
// in sync by hand; there is no shared source of truth between the frontend
// and that backend module yet (a future phase could expose it via an API
// instead of duplicating the matrix here).
export const CONTENT_MODES = [
  { value: 'single_story',    label: 'Single Story',    executable: true,
    description: 'One discovered story per cycle — today’s only real behavior.' },
  { value: 'limited_series',  label: 'Limited Series',  executable: false,
    description: 'A fixed-length multi-episode arc. Coming soon.' },
  { value: 'ongoing_series',  label: 'Ongoing Series',   executable: false,
    description: 'An open-ended recurring series. Coming soon.' },
]

export const SCRIPT_SOURCES = [
  { value: 'reddit',        label: 'Reddit (discovered stories)',        executable: true },
  { value: 'ai_generated',  label: 'AI-generated original story',        executable: true },
  { value: 'user_provided', label: 'User-provided script (Coming soon)',  executable: false },
  { value: 'hybrid',        label: 'Hybrid (Coming soon)',                executable: false },
]

export const OUTPUT_MODES = [
  { value: 'youtube_and_shorts', label: 'YouTube long-form + Shorts',  executable: true },
  { value: 'youtube_long_only',  label: 'YouTube long-form only',      executable: true },
  { value: 'shorts_only',        label: 'Shorts only',                 executable: true },
]

// Structured preset options for visual/image style dropdowns (Item 17).
// Values are free-form strings persisted to ChannelConfig.visual_style / .image_style
// (see CLAUDE.md §8.1) and injected into Agent 2 script prompts (blueprint, sections,
// short episodes) and Agent 4 storyboard prompts. Keep in sync with the storyboard
// system prompt's recognized visual-direction vocabulary.
export const VISUAL_STYLE_OPTIONS = [
  { value: 'story_driven',      label: 'Story-Driven',
    description: 'Grounded, narrative-first visuals that follow the story’s emotional beats rather than a fixed genre look — the default for most channels; matches whatever register the configured niche/tone imply.' },
  { value: 'documentary',       label: 'Documentary',
    description: 'Neutral, factual, observational. Naturalistic lighting and real-world settings — best for news, explainers, and investigative storytelling.' },
  { value: 'true_crime',        label: 'True Crime',
    description: 'Dark suburban realism, evidence photography, police tape, and institutional interiors. Tightly framed, high-tension visual language.' },
  { value: 'investigative',     label: 'Investigative',
    description: 'Evidence-board framing, surveillance aesthetics, low-angle shots, and close-up document details. Suits exposé and deep-dive reporting channels.' },
  { value: 'cinematic',         label: 'Cinematic',
    description: 'Elevated film-quality sequences with dramatic angles, lens flares, rich shadow play, and depth of field. Works well for emotionally charged stories.' },
  { value: 'historical',        label: 'Historical',
    description: 'Period-accurate settings with vintage textures, archival-photograph aesthetics, and painterly depth. Ideal for history, biography, and era-specific stories.' },
  { value: 'noir',              label: 'Noir',
    description: 'Dark, high-contrast visuals with deep shadows, rain-slicked streets, and desaturated tones. Suits mystery, crime, and psychological suspense.' },
  { value: 'suspense_thriller', label: 'Suspense / Thriller',
    description: 'Extreme shallow depth of field, tight close-ups, Dutch angles, and high-tension lighting. Amplifies dread and uncertainty in every frame.' },
  { value: 'nature',            label: 'Nature / Wildlife',
    description: 'Lush environments, macro detail, golden-hour light, and wide-open vistas. Best for science, ecology, and outdoor exploration channels.' },
  { value: 'educational',       label: 'Educational',
    description: 'Clean, flat-lay compositions, clear diagram-style framing, and well-lit subjects. Optimised for clarity and audience comprehension.' },
  { value: 'retro',             label: 'Retro / Vintage',
    description: 'Film grain, VHS aesthetics, saturated 70s/80s palettes, and analog warmth. Suits nostalgia-driven, pop-culture, or archival content.' },
]

export const IMAGE_STYLE_OPTIONS = [
  { value: 'photorealistic',    label: 'Photorealistic',
    description: 'Lifelike images indistinguishable from professional photography — the default for most content types.' },
  { value: 'cinematic_realism', label: 'Cinematic Realism',
    description: 'Film-quality photography with deliberate colour grading, shallow depth of field, and widescreen cinematic composition.' },
  { value: 'dark_realistic',    label: 'Dark Realistic',
    description: 'Gritty, desaturated realism with dramatic shadow work and moody high-contrast lighting. Ideal for crime, thriller, and dark-history content.' },
  { value: 'vintage_film',      label: 'Vintage / Film',
    description: '35mm grain, muted tones, and the warm/cool shift of analog photography. Suits archival storytelling and nostalgia-driven channels.' },
  { value: 'digital_art',       label: 'Digital Art',
    description: 'Polished digital illustration with clean lines and vivid colours. Works well for tech, science, and modern editorial storytelling.' },
  { value: 'cinematic_cartoon',           label: 'Cinematic cartoon',
    description: 'Bold, simplified character and environment design with clean outlines and flat, bright colour fills — friendlier and more stylized than digital art, but without anime linework or comic-panel framing. Suits approachable historical, educational, or biography storytelling.' },
  { value: 'oil_painting',      label: 'Oil Painting',
    description: 'Rich, textured brushwork with classical compositional depth. Well-suited to historical, biographical, and literary content.' },
  { value: 'watercolor',        label: 'Watercolor',
    description: 'Soft, painterly images with translucent washes of colour. Suits nostalgic, historical, or emotionally gentle storytelling.' },
  { value: 'anime',             label: 'Anime',
    description: 'Japanese animation style with bold outlines, expressive characters, and vibrant colour palettes.' },
]

// Narration perspective/register (roadmap 4a / audit P1-9). Persisted to
// ChannelConfig.narration_pov and threaded into Agent 2 script prompts
// (blueprint, sections, native adaptation, short episodes) alongside
// visual_style/image_style.
export const NARRATION_POV_OPTIONS = [
  { value: 'third_person',           label: 'Third Person (default)',
    description: 'Standard narrator voice — refers to the people in the story by name and third-person pronouns (he/she/they).' },
  { value: 'first_person_storytime', label: 'First Person / Storytime',
    description: 'Narrated as if you are the protagonist retelling your own experience directly to the viewer, using "I"/"me"/"my" — the r/nosleep-style storytime format. Best when the source material has one clear protagonist.' },
]

export const OUTPUT_MODE_DESCRIPTIONS = {
  youtube_and_shorts: 'Generates a full-length YouTube video plus vertical Short episodes from the same story. Both are rendered and ready to publish.',
  youtube_long_only:  'Generates only the long-form YouTube video — the standalone Shorts planner is skipped entirely.',
  shorts_only:        'Generates one standalone vertical Short per discovery cycle, written directly from the discovered/generated story — no parent video and no long-form script are ever produced for this channel.',
}
