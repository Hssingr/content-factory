import React from "react";
import {
  AbsoluteFill,
  Audio,
  CalculateMetadataFunction,
  Sequence,
  interpolate,
  staticFile,
  useCurrentFrame,
  useVideoConfig,
} from "remotion";
import { KaraokeSubtitles } from "../components/KaraokeSubtitles";
import { MediaSection, transitionDurationFrames } from "../components/MediaSection";
import { ShortProps } from "../types";

export const shortCalculateMetadata: CalculateMetadataFunction<ShortProps> = ({
  props,
}) => ({
  durationInFrames: Math.max(1, Math.round((props.duration_ms / 1000) * 30)),
  fps:    30,
  width:  1080,
  height: 1920,
});

export const Short: React.FC<ShortProps> = ({
  audio_file,
  start_ms,
  sections,
  subtitles,
  part_label,
  hook_modified,
}) => {
  const { fps } = useVideoConfig();
  const audioSrc = staticFile(audio_file);

  // Audio plays from the Short's start offset in the full language audio file
  const audioStartFrom = Math.round((start_ms / 1000) * fps);

  return (
    <AbsoluteFill style={{ backgroundColor: "#0a0a0f" }}>
      <Audio src={audioSrc} startFrom={audioStartFrom} />

      {sections.map((section, idx) => {
        const sectionStartMs = section.audio_start_ms - start_ms;
        const sectionDurMs   = section.audio_end_ms - section.audio_start_ms;
        // The PREVIOUS section's transition_to_next decides how long this section
        // overlaps with it (and which entrance style MediaSection plays). Start
        // early for the overlap, but never before frame 0.
        const incomingTransition = idx > 0 ? sections[idx - 1].transition_to_next : undefined;
        const crossfadeIn = idx > 0 ? transitionDurationFrames(incomingTransition) : 0;
        const from = Math.max(
          0,
          Math.round((sectionStartMs / 1000) * fps) - crossfadeIn,
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
        <KaraokeSubtitlesWithOffset
          captions={subtitles.captions}
          startMs={start_ms}
        />
      )}

      {part_label && (
        <PartLabel label={part_label} hook_modified={hook_modified} />
      )}
    </AbsoluteFill>
  );
};

// ── KaraokeSubtitles wrapper — shifts absolute timings to Short-local ─────────

interface KaraokeOffsetProps {
  captions: ShortProps["subtitles"]["captions"];
  startMs:  number;
}

const KaraokeSubtitlesWithOffset: React.FC<KaraokeOffsetProps> = ({ captions, startMs }) => {
  const shifted = captions.map((chunk) => ({
    ...chunk,
    start_ms: chunk.start_ms - startMs,
    end_ms:   chunk.end_ms   - startMs,
    words:    chunk.words.map((w) => ({
      ...w,
      s: w.s - startMs,
      e: w.e - startMs,
    })),
  }));
  return <KaraokeSubtitles captions={shifted} />;
};

// ── Part label overlay ────────────────────────────────────────────────────────

interface PartLabelProps {
  label:         string;
  hook_modified: boolean;
}

const PartLabel: React.FC<PartLabelProps> = ({ label }) => {
  const frame = useCurrentFrame();

  const opacity = interpolate(frame, [0, 15, 90, 120], [0, 1, 1, 0], {
    extrapolateRight: "clamp",
  });

  return (
    <AbsoluteFill style={{ pointerEvents: "none" }}>
      <div
        style={{
          position:        "absolute",
          top:             48,
          right:           48,
          backgroundColor: "rgba(0,0,0,0.65)",
          borderRadius:    8,
          padding:         "8px 18px",
          opacity,
        }}
      >
        <span
          style={{
            color:       "#FFD700",
            fontSize:    36,
            fontFamily:  "Arial, Helvetica, sans-serif",
            fontWeight:  "bold",
            textShadow:  "1px 1px 3px rgba(0,0,0,0.9)",
          }}
        >
          {label}
        </span>
      </div>
    </AbsoluteFill>
  );
};
