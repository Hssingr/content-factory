import React from "react";
import {
  AbsoluteFill,
  Img,
  OffthreadVideo,
  Sequence,
  interpolate,
  staticFile,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import {
  ClipData,
  ColorGrade,
  Effect,
  OverlayPosition,
  SectionData,
  Transition,
} from "../types";
import { TextCard } from "./TextCard";

interface Props {
  section:      SectionData;
  /** Frames to spend fading in from the previous section (0 for the very first). */
  crossfadeIn?: number;
  /** The previous section's transition_to_next — drives the entrance style/timing. */
  incomingTransition?: Transition;
}

// Frames for crossfade between clips within a section
const CLIP_CROSSFADE = 12;   // 0.4 s at 30 fps

// CSS filter strings per color grade
//
// cold_blue (Phase 14.11 fix): was `hue-rotate(200deg)`, a near-180-degree
// rotation of the ENTIRE hue wheel — not a subtle cool nudge. CSS hue-rotate
// shifts every hue by the given angle, so 200deg pushes skin tones (~25-35deg,
// orange) into the blue family (~225-235deg) and green foliage (~90-130deg)
// into violet/magenta (~290-330deg) — this is the confirmed root cause of the
// reported "unnatural purple/blue tint ... including foliage and skin tones"
// defect (code_report/phase_14_11_color_grade_cast_investigation.md). Reduced
// to a small nudge that cannot push a recognizable hue into an unrelated
// color family; saturate/brightness (already present) carry the "cool/muted"
// feel instead of an extreme hue rotation.
const GRADE_FILTER: Record<ColorGrade, string> = {
  desaturated:   "saturate(30%) brightness(85%)",
  cold_blue:     "saturate(70%) hue-rotate(15deg) brightness(80%)",
  warm_amber:    "saturate(120%) sepia(40%) brightness(105%)",
  dark_contrast: "contrast(140%) brightness(65%) saturate(75%)",
  neutral:       "none",
};

const DARK_FALLBACK_STYLE: React.CSSProperties = {
  width:      "100%",
  height:     "100%",
  background: "radial-gradient(ellipse at center, #1a1a2e 0%, #0a0a0f 70%)",
};

const isFallback = (url: string) =>
  !url ||
  url === "__dark_fallback__" ||
  url === "__runway_pending__" ||
  url === "__generated_pending__";

// ── Overlay text placement ─────────────────────────────────────────────────────

const OVERLAY_POSITION_STYLE: Record<OverlayPosition, React.CSSProperties> = {
  center:      { top: "50%", left: "50%", transform: "translate(-50%, -50%)", textAlign: "center" },
  lower_third: { bottom: "12%", left: "8%", right: "8%", textAlign: "left" },
  top_left:    { top: "8%", left: "8%", textAlign: "left" },
  top_right:   { top: "8%", right: "8%", textAlign: "right" },
  none:        { display: "none" },
};

interface TextOverlayProps {
  text:     string;
  position: OverlayPosition;
}

const TextOverlay: React.FC<TextOverlayProps> = ({ text, position }) => {
  if (!text || position === "none") return null;

  return (
    <AbsoluteFill style={{ pointerEvents: "none" }}>
      <div style={{ position: "absolute", maxWidth: "84%", ...OVERLAY_POSITION_STYLE[position] }}>
        <span
          style={{
            color:       "#ffffff",
            fontSize:    56,
            fontFamily:  "Arial, Helvetica, sans-serif",
            fontWeight:  "bold",
            lineHeight:  1.25,
            textShadow:  "2px 2px 6px rgba(0,0,0,0.85)",
          }}
        >
          {text}
        </span>
      </div>
    </AbsoluteFill>
  );
};

// Placeholder rendered for generated_visual beats until AI-image generation lands
const GeneratedPlaceholder: React.FC = () => (
  <AbsoluteFill style={{ ...DARK_FALLBACK_STYLE, display: "flex", alignItems: "center", justifyContent: "center" }}>
    <span
      style={{
        color:         "rgba(255,255,255,0.25)",
        fontSize:      28,
        fontFamily:    "Arial, Helvetica, sans-serif",
        letterSpacing: 4,
        textTransform: "uppercase",
      }}
    >
      Generated visual
    </span>
  </AbsoluteFill>
);

export const MediaSection: React.FC<Props> = ({ section, crossfadeIn = 0, incomingTransition }) => {
  const { fps } = useVideoConfig();
  const frame   = useCurrentFrame();

  // Section duration derived from audio timings — NOT from composition durationInFrames,
  // which would be wrong here because useVideoConfig returns the root composition config.
  const sectionDurMs     = section.audio_end_ms - section.audio_start_ms;
  const sectionDurFrames = Math.max(1, Math.round((sectionDurMs / 1000) * fps));

  const transitionStyle = getTransitionStyle(incomingTransition, frame, crossfadeIn);
  const overlayPosition = section.overlay_position ?? "none";

  // Phase 14.10b — secondary double-text mechanism fix: this section's own
  // Sequence is mounted `crossfadeIn` frames before its narration actually
  // starts (so the IMAGE can crossfade in smoothly — see the from/dur math
  // in MainVideo.tsx/Short.tsx). During those crossfadeIn frames the
  // PREVIOUS section's MediaSection (and its own overlay, if any) is still
  // mounted too. Delaying this section's own TextOverlay/TextCard until its
  // crossfade-in has finished means at most one section's overlay is ever
  // visible at a time — it does not, by itself, touch the primary
  // overlay-vs-global-subtitles fix (see StandardSubtitles.tsx/
  // KaraokeSubtitles.tsx + computeOverlaySuppressWindows() below).
  const showOverlay = frame >= crossfadeIn;

  // Resolve clips — fall back to legacy single-media fields when clips array is absent
  const clips: ClipData[] =
    section.clips?.length
      ? section.clips
      : [{ url: section.media_url, thumb: section.media_thumb, type: section.media_type }];

  const validClips = clips.filter((c) => !isFallback(c.url));

  // No usable local media — render a background placeholder + any overlay text.
  //
  // Differentiation by visual_type:
  //   text_card         -> TextCard.tsx fallback when background generation failed
  //   generated_visual  → GeneratedPlaceholder (AI generation pending in MODE A,
  //                        or a hard MEDIA_FAILED beat that slipped through in MODE B)
  //   text_overlay      → dark background; render props must carry a local Flux/cache
  //                        asset or an approved local fallback. Remote URLs are forbidden before render.
  //   anything else     → GeneratedPlaceholder (shouldn't happen in a healthy pipeline)
  if (validClips.length === 0) {
    const effectiveOverlayPos = overlayPosition === "none" ? "center" : overlayPosition;
    if (section.visual_type === "text_card") {
      return showOverlay ? (
        <TextCard
          text={section.overlay_text ?? ""}
          style={transitionStyle}
          cardStyle={section.text_card_style}
        />
      ) : null;
    }
    if (section.visual_type === "text_overlay") {
      return (
        <AbsoluteFill style={{ ...DARK_FALLBACK_STYLE, ...transitionStyle }}>
          {showOverlay && <TextOverlay text={section.overlay_text ?? ""} position={effectiveOverlayPos} />}
        </AbsoluteFill>
      );
    }
    return (
      <AbsoluteFill style={transitionStyle}>
        <GeneratedPlaceholder />
        {showOverlay && <TextOverlay text={section.overlay_text ?? ""} position={overlayPosition} />}
      </AbsoluteFill>
    );
  }

  // Each clip gets an equal share of the section, with an overlap for crossfade
  const clipShareFrames = Math.ceil(sectionDurFrames / validClips.length);

  return (
    <AbsoluteFill style={{ overflow: "hidden", ...transitionStyle }}>
      {validClips.map((clip, i) => {
        // Clip starts CLIP_CROSSFADE frames early (except the first)
        const clipFrom = Math.max(0, i * clipShareFrames - (i > 0 ? CLIP_CROSSFADE : 0));
        const clipEnd  =
          i === validClips.length - 1
            ? sectionDurFrames
            : (i + 1) * clipShareFrames;
        const clipDur = Math.max(1, clipEnd - clipFrom);

        return (
          <Sequence key={`${section.order}-${i}`} from={clipFrom} durationInFrames={clipDur}>
            <SingleClip
              clip={clip}
              effect={section.effect}
              colorGrade={section.color_grade}
              crossfadeIn={i > 0 ? CLIP_CROSSFADE : 0}
              durationFrames={clipDur}
            />
          </Sequence>
        );
      })}
      {showOverlay && (
        section.visual_type === "text_card" ? (
          <TextCard
            text={section.overlay_text ?? ""}
            cardStyle={section.text_card_style}
            transparentBackground
          />
        ) : (
          <TextOverlay text={section.overlay_text ?? ""} position={overlayPosition} />
        )
      )}
    </AbsoluteFill>
  );
};

// ── Single clip renderer ───────────────────────────────────────────────────────

interface ClipProps {
  clip:          ClipData;
  effect:        Effect;
  colorGrade:    ColorGrade;
  crossfadeIn:   number;   // frames to fade in from 0 → 1
  durationFrames: number;  // full duration of this clip sequence
}

const SingleClip: React.FC<ClipProps> = ({
  clip,
  effect,
  colorGrade,
  crossfadeIn,
  durationFrames,
}) => {
  const frame    = useCurrentFrame();
  const progress = durationFrames > 1 ? frame / (durationFrames - 1) : 0;

  const opacity = crossfadeIn > 0
    ? interpolate(frame, [0, crossfadeIn], [0, 1], { extrapolateRight: "clamp" })
    : 1;

  const mediaStyle: React.CSSProperties = {
    width:     "100%",
    height:    "100%",
    objectFit: "cover",
    filter:    GRADE_FILTER[colorGrade] ?? "none",
    transform: getEffectTransform(effect, progress, frame),
    opacity,
  };

  return (
    <AbsoluteFill style={{ overflow: "hidden" }}>
      {clip.type === "video" ? (
        <OffthreadVideo src={staticFile(clip.url)} style={mediaStyle} muted />
      ) : (
        <Img src={staticFile(clip.url)} style={mediaStyle} />
      )}
    </AbsoluteFill>
  );
};

// ── Effect transform ───────────────────────────────────────────────────────────

function getEffectTransform(effect: Effect, progress: number, frame: number): string {
  switch (effect) {
    case "slow_zoom":
      return `scale(${interpolate(progress, [0, 1], [1.0, 1.08])})`;

    case "zoom_out":
      return `scale(${interpolate(progress, [0, 1], [1.08, 1.0])})`;

    case "pan":
      return `translateX(${interpolate(progress, [0, 1], [0, -5])}%) scale(1.06)`;

    case "push_in":
      return `scale(${interpolate(progress, [0, 1], [1.0, 1.18])})`;

    case "parallax":
      return `scale(${interpolate(progress, [0, 1], [1.05, 1.14])}) translateX(${interpolate(progress, [0, 1], [-3, 3])}%)`;

    case "shake": {
      // Small per-frame jitter — sine/cosine at different rates avoid a circular pattern
      const jitterX = Math.sin(frame * 1.4) * 1.2;
      const jitterY = Math.cos(frame * 1.7) * 1.0;
      return `translate(${jitterX}px, ${jitterY}px) scale(1.04)`;
    }

    case "fade_in":
    case "cut":
    default:
      return "scale(1.0)";
  }
}

// ── Transition timing & style ──────────────────────────────────────────────────

/**
 * How many frames a section should overlap with the previous one for a given
 * transition_to_next value. "cut"/"none" read as a hard cut (no overlap).
 */
export function transitionDurationFrames(transition?: Transition): number {
  switch (transition) {
    case "crossfade":    return 15;  // 0.5 s — classic dissolve
    case "dip_to_black": return 20;  // 0.67 s — fades through black
    case "whip_pan":     return 10;  // 0.33 s — fast directional blur
    case "zoom_blur":    return 12;  // 0.4 s — punch-in blur
    case "match_cut":    return 6;   // 0.2 s — near-instant, slight overlap
    case "cut":
    case "none":
    default:             return 0;
  }
}

// ── Subtitle/overlay collision fix (Phase 14.10b) ──────────────────────────
// A section renders its own TextOverlay/TextCard for its full on-screen
// window (see the validClips>0/validClips===0 branches above). The
// composition-level subtitle component (StandardSubtitles/KaraokeSubtitles)
// has no awareness of that on its own — see
// code_report/phase_14_10_double_subtitle_investigation.md for the
// confirmed root cause. The helpers below let MainVideo.tsx/Short.tsx
// compute, directly from the same `sections` prop MediaSection itself reads,
// which absolute-time windows should suppress the global subtitle layer.

export interface OverlaySuppressWindow {
  start_ms: number;
  end_ms:   number;
}

/**
 * True when this section will render its own per-section text layer
 * (TextOverlay or TextCard) for its on-screen window — i.e. design-decision
 * condition from Phase 14.10b: "overlay_text non-empty and
 * overlay_position != 'none', OR visual_type === 'text_card'".
 */
export function sectionHasActiveOverlay(section: SectionData): boolean {
  if (section.visual_type === "text_card") return true;
  const text     = section.overlay_text ?? "";
  const position = section.overlay_position ?? "none";
  return Boolean(text) && position !== "none";
}

/**
 * Windows (in the same absolute ms coordinate space as the subtitle
 * captions passed to StandardSubtitles/KaraokeSubtitles) during which the
 * global subtitle layer should render nothing, because this section's own
 * overlay is showing instead. `offsetMs` lets Short.tsx shift these into
 * the same Short-local coordinate space it already shifts captions into
 * (via KaraokeSubtitlesWithOffset) — pass `start_ms` there; MainVideo.tsx
 * passes no offset (sections are already absolute there).
 *
 * Uses each section's nominal (non-crossfade-extended) audio_start_ms/
 * audio_end_ms — deliberately not adjusted for the secondary crossfade-in
 * delay above (`showOverlay`), so a section's suppression window can begin
 * very slightly before its own overlay actually becomes visible. This is an
 * intentional, documented trade-off (code_report/phase_14_10b_subtitle_overlay_collision_fix.md):
 * a few-hundred-ms window with neither subtitles nor an overlay visible is
 * preferable to ever risking the two layers colliding again.
 */
export function computeOverlaySuppressWindows(
  sections: SectionData[],
  offsetMs: number = 0,
): OverlaySuppressWindow[] {
  return sections
    .filter(sectionHasActiveOverlay)
    .map((s) => ({
      start_ms: s.audio_start_ms - offsetMs,
      end_ms:   s.audio_end_ms   - offsetMs,
    }));
}

/** Per-frame entrance style for the given incoming transition. */
function getTransitionStyle(
  transition: Transition | undefined,
  frame: number,
  crossfadeIn: number,
): React.CSSProperties {
  if (crossfadeIn <= 0 || frame >= crossfadeIn) {
    return { opacity: 1 };
  }
  const t = frame / crossfadeIn; // 0 → 1 across the transition window

  switch (transition) {
    case "dip_to_black":
      return {
        opacity: t,
        filter:  `brightness(${interpolate(t, [0, 1], [0, 100])}%)`,
      };

    case "whip_pan":
      return {
        opacity:   t,
        transform: `translateX(${interpolate(t, [0, 1], [25, 0])}%)`,
        filter:    `blur(${interpolate(t, [0, 1], [16, 0])}px)`,
      };

    case "zoom_blur":
      return {
        opacity:   t,
        transform: `scale(${interpolate(t, [0, 1], [1.25, 1])})`,
        filter:    `blur(${interpolate(t, [0, 1], [10, 0])}px)`,
      };

    case "match_cut":
    case "crossfade":
    default:
      return { opacity: t };
  }
}
