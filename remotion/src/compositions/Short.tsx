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
}) => {
  // Use the stored bridge_duration_ms (measured by mutagen at generation time) so the
  // composition boundary matches the actual audio length exactly.
  // Legacy fallback: if bridge_duration_ms is absent (old DB rows), use 2 s (60 frames at 30 fps).
  const bridgeExtraFrames = props.bridge_file
    ? (props.bridge_duration_ms
        ? Math.ceil((props.bridge_duration_ms / 1000) * 30)
        : 60)   // 2 s legacy fallback
    : 0;

  return {
    durationInFrames: Math.max(1, Math.round((props.duration_ms / 1000) * 30) + bridgeExtraFrames),
    fps:    30,
    width:  1080,
    height: 1920,
  };
};

export const Short: React.FC<ShortProps> = ({
  audio_file,
  start_ms,
  duration_ms,
  sections,
  subtitles,
  part_label,
  hook_modified,
  rehook_file,
  bridge_file,
  rehook_duration_ms,
  bridge_duration_ms,
  rehook_text,
}) => {
  const { fps } = useVideoConfig();
  const audioSrc = staticFile(audio_file);

  // Audio plays from the Short's start offset in the full language audio file
  const audioStartFrom     = Math.round((start_ms    / 1000) * fps);
  const audioDurationFrames = Math.round((duration_ms / 1000) * fps);

  // Rehook frames: use stored duration when available; fallback to full narration length
  // (the rehook audio is typically 2–4 s; it plays simultaneously with the narration intro)
  const rehookFrames = rehook_file
    ? (rehook_duration_ms
        ? Math.ceil((rehook_duration_ms / 1000) * fps)
        : audioDurationFrames)   // legacy: no stored duration → display through full narration
    : 0;

  // Bridge start: immediately after the narration audio ends
  const bridgeStartFrame = audioDurationFrames;

  return (
    <AbsoluteFill style={{ backgroundColor: "#0a0a0f" }}>
      <Audio src={audioSrc} startFrom={audioStartFrom} />

      {/* Rehook audio: plays from frame 0, layered over the narration intro */}
      {rehook_file && <Audio src={staticFile(rehook_file)} />}

      {/* Bridge audio: plays after the narration ends (composition is extended) */}
      {bridge_file && (
        <Sequence from={bridgeStartFrame}>
          <Audio src={staticFile(bridge_file)} />
        </Sequence>
      )}

      {/* Rehook text overlay: shown during rehook audio to re-hook the viewer.
          Bold text card styled with the karaoke accent colour, displayed over
          the first section's visual for the duration of the rehook clip. */}
      {rehook_file && rehook_text && rehookFrames > 0 && (
        <Sequence from={0} durationInFrames={rehookFrames}>
          <RehookOverlay text={rehook_text} />
        </Sequence>
      )}

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

// ── Rehook text overlay ───────────────────────────────────────────────────────

interface RehookOverlayProps {
  text: string;
}

const RehookOverlay: React.FC<RehookOverlayProps> = ({ text }) => {
  const frame = useCurrentFrame();
  const { durationInFrames } = useVideoConfig();

  const opacity = interpolate(
    frame,
    [0, 6, durationInFrames - 8, durationInFrames],
    [0, 1, 1, 0],
    { extrapolateLeft: "clamp", extrapolateRight: "clamp" },
  );

  return (
    <AbsoluteFill
      style={{
        pointerEvents:   "none",
        backgroundColor: "rgba(0,0,0,0.55)",
        display:         "flex",
        alignItems:      "center",
        justifyContent:  "center",
        opacity,
      }}
    >
      <div
        style={{
          padding:   "0 64px",
          textAlign: "center",
          maxWidth:  "90%",
        }}
      >
        <span
          style={{
            color:       "#FFD700",
            fontSize:    68,
            fontFamily:  "Arial, Helvetica, sans-serif",
            fontWeight:  "bold",
            lineHeight:  1.3,
            textShadow:  "2px 2px 8px rgba(0,0,0,0.9)",
          }}
        >
          {text}
        </span>
      </div>
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
