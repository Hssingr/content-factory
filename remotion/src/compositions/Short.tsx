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

// ── Metadata ──────────────────────────────────────────────────────────────────
// Duration is the standalone short narration window.

export const shortCalculateMetadata: CalculateMetadataFunction<ShortProps> = ({
  props,
}) => {
  const FPS = 30;

  const mainFrames = Math.ceil((props.duration_ms / 1000) * FPS);

  return {
    durationInFrames: Math.max(1, mainFrames),
    fps:    FPS,
    width:  1080,
    height: 1920,
  };
};

// ── Main composition ──────────────────────────────────────────────────────────

export const Short: React.FC<ShortProps> = ({
  audio_file,
  start_ms,
  duration_ms,
  sections,
  subtitles,
  part_label,
}) => {
  const { fps } = useVideoConfig();

  // Offset into the full-language audio file where this Short's narration begins.
  const audioStartFrom = Math.round((start_ms / 1000) * fps);

  const mainFrames = Math.ceil((duration_ms / 1000) * fps);

  return (
    <AbsoluteFill style={{ backgroundColor: "#0a0a0f" }}>

      <Sequence from={0} durationInFrames={mainFrames}>
        <Audio src={staticFile(audio_file)} startFrom={audioStartFrom} />
      </Sequence>

      {/* Visual sections synced directly to the child short narration */}
      {sections.map((section, idx) => {
        const sectionStartMs = section.audio_start_ms - start_ms;
        const sectionDurMs   = section.audio_end_ms - section.audio_start_ms;
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

      {/* Karaoke subtitles — wrapped so their internal frame counter starts at narration start */}
      {subtitles.captions.length > 0 && (
        <Sequence from={0}>
          <KaraokeSubtitlesWithOffset
            captions={subtitles.captions}
            startMs={start_ms}
          />
        </Sequence>
      )}

      {part_label && (
        <PartLabel label={part_label} />
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
  label: string;
}

const PartLabel: React.FC<PartLabelProps> = ({ label }) => {
  const frame = useCurrentFrame();
  const { durationInFrames } = useVideoConfig();

  // Fade in over first 15 frames, hold throughout, fade out over last 15 frames.
  const opacity = interpolate(
    frame,
    [0, 15, durationInFrames - 15, durationInFrames],
    [0, 1,  1,                      0],
    { extrapolateLeft: "clamp", extrapolateRight: "clamp" },
  );

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
