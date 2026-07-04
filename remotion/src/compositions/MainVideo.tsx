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
}) => {
  const { fps } = useVideoConfig();
  const audioSrc = staticFile(audio_file);

  return (
    <AbsoluteFill style={{ backgroundColor: "#0a0a0f" }}>
      <Audio src={audioSrc} />

      {sections.map((section, idx) => {
        const sectionDurMs = section.audio_end_ms - section.audio_start_ms;
        // The PREVIOUS section's transition_to_next decides how long this section
        // overlaps with it (and which entrance style MediaSection plays).
        const incomingTransition = idx > 0 ? sections[idx - 1].transition_to_next : undefined;
        const crossfadeIn = idx > 0 ? transitionDurationFrames(incomingTransition) : 0;
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
