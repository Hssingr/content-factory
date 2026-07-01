export const LANGUAGES = [
  { code: 'fr', label: 'French' },
  { code: 'en', label: 'English' },
  { code: 'es', label: 'Spanish' },
  { code: 'de', label: 'German' },
  { code: 'it', label: 'Italian' },
  { code: 'pt', label: 'Portuguese' },
]

export const TONES = [
  { value: 'documentary',    label: 'Documentary' },
  { value: 'conversational', label: 'Conversational' },
  { value: 'educational',    label: 'Educational' },
  { value: 'entertaining',   label: 'Entertaining' },
  { value: 'investigative',  label: 'Investigative' },
]

export const EMOTIONS = [
  { value: 'neutral',       label: 'Neutral' },
  { value: 'enthusiastic',  label: 'Enthusiastic' },
  { value: 'calm',          label: 'Calm' },
  { value: 'authoritative', label: 'Authoritative' },
  { value: 'dramatic',      label: 'Dramatic' },
  { value: 'warm',          label: 'Warm' },
]

export const MUSIC_STYLES = [
  { value: 'cinematic',   label: 'Cinematic' },
  { value: 'upbeat',      label: 'Upbeat' },
  { value: 'ambient',     label: 'Ambient' },
  { value: 'dramatic',    label: 'Dramatic' },
  { value: 'minimal',     label: 'Minimal' },
  { value: 'electronic',  label: 'Electronic' },
  { value: 'orchestral',  label: 'Orchestral' },
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

export const USE_CASES = [
  { value: 'conversational',          label: 'Conversational' },
  { value: 'narrative_story',         label: 'Narration' },
  { value: 'characters_animation',    label: 'Characters' },
  { value: 'social_media',            label: 'Social Media' },
  { value: 'informative_educational', label: 'Educational' },
  { value: 'advertisement',           label: 'Advertisement' },
  { value: 'entertainment_tv',        label: 'Entertainment' },
]

export const SHORTS_RULES = [
  { value: 'auto',   label: 'Auto (when content allows)' },
  { value: 'always', label: 'Always create Shorts' },
  { value: 'never',  label: 'Never create Shorts' },
]

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
  { value: 'ai_generated',  label: 'AI / Claude-generated (Coming soon)', executable: false },
  { value: 'user_provided', label: 'User-provided script (Coming soon)',  executable: false },
  { value: 'hybrid',        label: 'Hybrid (Coming soon)',                executable: false },
]

export const OUTPUT_MODES = [
  { value: 'youtube_and_shorts', label: 'YouTube long-form + Shorts',     executable: true },
  { value: 'youtube_long_only',  label: 'YouTube long-form only (Coming soon)', executable: false },
  { value: 'shorts_only',        label: 'Shorts only (Coming soon)',      executable: false },
]

// Structured preset options for visual/image style dropdowns (Item 17).
// Values are free-form strings persisted to ChannelConfig.visual_style / .image_style
// (see CLAUDE.md §8.1) and injected into Agent 2 script prompts (blueprint, sections,
// short episodes) and Agent 4 storyboard prompts. Keep in sync with the storyboard
// system prompt's recognized visual-direction vocabulary.
export const VISUAL_STYLE_OPTIONS = [
  { value: 'documentary',       label: 'Documentary',
    description: 'Neutral, factual, observational. Naturalistic lighting and real-world settings — the default for news, explainers, and investigative storytelling.' },
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
  { value: 'oil_painting',      label: 'Oil Painting',
    description: 'Rich, textured brushwork with classical compositional depth. Well-suited to historical, biographical, and literary content.' },
  { value: 'watercolor',        label: 'Watercolor',
    description: 'Soft, painterly images with translucent washes of colour. Suits nostalgic, historical, or emotionally gentle storytelling.' },
  { value: 'anime',             label: 'Anime',
    description: 'Japanese animation style with bold outlines, expressive characters, and vibrant colour palettes.' },
]

export const OUTPUT_MODE_DESCRIPTIONS = {
  youtube_and_shorts: 'Generates a full-length YouTube video plus vertical Short episodes from the same story. Both are rendered and ready to publish.',
  youtube_long_only:  'Generates only the long-form YouTube video — no Shorts. (Coming soon)',
  shorts_only:        'Generates only vertical Short episodes, with no long-form video. (Coming soon)',
}
