import React from "react";
import {
  AbsoluteFill,
  Audio,
  CalculateMetadataFunction,
  Sequence,
  staticFile,
  useVideoConfig,
} from "remotion";
import { MediaSection, transitionDurationFrames } from "../components/MediaSection";
import { StandardSubtitles } from "../components/StandardSubtitles";
import { MainVideoProps } from "../types";

export const mainVideoCalculateMetadata: CalculateMetadataFunction<MainVideoProps> = ({
  props,
}) => ({
  durationInFrames: Math.max(1, Math.round((props.duration_ms / 1000) * 30)),
  fps:    30,
  width:  1920,
  height: 1080,
});

export const MainVideo: React.FC<MainVideoProps> = ({
  audio_file,
  sections,
  subtitles,
  leading_transition,
}) => {
  const { fps } = useVideoConfig();
  const audioSrc = staticFile(audio_file);

  return (
    <AbsoluteFill style={{ backgroundColor: "#0a0a0f" }}>
      <Audio src={audioSrc} />

      {sections.map((section, idx) => {
        const sectionDurMs = section.audio_end_ms - section.audio_start_ms;
        // The PREVIOUS section's transition_to_next decides how long this section
        // overlaps with it (and which entrance style MediaSection plays). For a
        // chunked render, this chunk's own first section (idx === 0) has no local
        // predecessor to read that off of — leading_transition carries it in from
        // the real previous section, which lives in the prior chunk's own
        // composition (see renderer.py's render_main_video_chunked).
        const incomingTransition =
          idx > 0 ? sections[idx - 1].transition_to_next : leading_transition;
        const rawCrossfadeIn = idx > 0 ? transitionDurationFrames(incomingTransition) : 0;
        // Defensive clamp (roadmap Tier 1 R4 / deep-audit Finding 9): never let a
        // crossfade overlap more than either side's own real duration has room for.
        // Safe by construction today (Agent 4's INTENSITY_FLOOR_MS floors every beat
        // well above the longest possible crossfade), but the render layer should not
        // silently produce an overlap reaching past the previous section's own start
        // if that upstream invariant is ever violated by a future change.
        const ownDurFrames = Math.max(1, Math.round((sectionDurMs / 1000) * fps));
        const prevDurFrames = idx > 0
          ? Math.max(1, Math.round(
              ((sections[idx - 1].audio_end_ms - sections[idx - 1].audio_start_ms) / 1000) * fps,
            ))
          : 0;
        const crossfadeIn = idx > 0
          ? Math.min(rawCrossfadeIn, ownDurFrames, prevDurFrames)
          : 0;
        const from = Math.max(
          0,
          Math.round((section.audio_start_ms / 1000) * fps) - crossfadeIn,
        );
        const dur = Math.max(
          1,
          Math.round((sectionDurMs / 1000) * fps) + crossfadeIn,
        );

        return (
          <Sequence key={section.order} from={from} durationInFrames={dur}>
            <MediaSection section={section} crossfadeIn={crossfadeIn} incomingTransition={incomingTransition} />
          </Sequence>
        );
      })}

      {subtitles.captions.length > 0 && (
        <StandardSubtitles captions={subtitles.captions} />
      )}
    </AbsoluteFill>
  );
};
