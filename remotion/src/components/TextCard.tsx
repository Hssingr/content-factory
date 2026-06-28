import React from "react";
import { AbsoluteFill, interpolate, useCurrentFrame, useVideoConfig } from "remotion";
import type { TextCardStyle } from "../types";

interface TextCardProps {
  /** Overlay text to display — typically the beat's overlay_text field. */
  text: string;
  /** CSS transition/opacity style injected by MediaSection for crossfades. */
  style?: React.CSSProperties;
  /** Accent colour — defaults to the karaoke gold used elsewhere in the pipeline. */
  accentColor?: string;
  /** Visual style variant. Defaults to "default". */
  cardStyle?: TextCardStyle;
  /** Keep the generated background image visible behind the Remotion text layer. */
  transparentBackground?: boolean;
}

// ── Shared animation hook ──────────────────────────────────────────────────────

function useCardAnimations() {
  const frame = useCurrentFrame();
  const { durationInFrames } = useVideoConfig();
  const progress = durationInFrames > 1 ? frame / (durationInFrames - 1) : 0;
  const scale = interpolate(progress, [0, 1], [1.0, 1.05]);
  const opacity = interpolate(frame, [0, 8], [0, 1], { extrapolateRight: "clamp" });
  return { scale, opacity };
}

// ── Default variant ────────────────────────────────────────────────────────────
// Dark radial gradient, centred bold text, gold accent bar.

function DefaultCard({ text, accentColor, transparentBackground }: { text: string; accentColor: string; transparentBackground: boolean }) {
  const { scale, opacity } = useCardAnimations();
  return (
    <AbsoluteFill
      style={{
        background: transparentBackground ? "rgba(10, 10, 15, 0.34)" : "radial-gradient(ellipse at center, #1a1a2e 0%, #0a0a0f 70%)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        transform: `scale(${scale})`,
        overflow: "hidden",
      }}
    >
      {text && (
        <div style={{ opacity, padding: "0 80px", textAlign: "center", maxWidth: "90%" }}>
          <div
            style={{
              width: 64,
              height: 4,
              backgroundColor: accentColor,
              margin: "0 auto 28px",
              borderRadius: 2,
            }}
          />
          <span
            style={{
              color: "#ffffff",
              fontSize: 60,
              fontFamily: "Arial, Helvetica, sans-serif",
              fontWeight: "bold",
              lineHeight: 1.3,
              textShadow: "2px 2px 8px rgba(0,0,0,0.9)",
            }}
          >
            {text}
          </span>
        </div>
      )}
    </AbsoluteFill>
  );
}

// ── Chat variant ───────────────────────────────────────────────────────────────
// Simulates a messaging interface: dark background, speech bubble with rounded
// corners, as if a message is being received live.

function ChatCard({ text, accentColor, transparentBackground }: { text: string; accentColor: string; transparentBackground: boolean }) {
  const { scale, opacity } = useCardAnimations();
  return (
    <AbsoluteFill
      style={{
        background: transparentBackground ? "transparent" : "#0d0d14",
        display: "flex",
        alignItems: "center",
        justifyContent: "flex-start",
        flexDirection: "column",
        paddingTop: 120,
        transform: `scale(${scale})`,
        overflow: "hidden",
      }}
    >
      {/* Thin header bar mimicking a chat app */}
      <div
        style={{
          position: "absolute",
          top: 0,
          left: 0,
          right: 0,
          height: 80,
          background: "#1a1a2e",
          borderBottom: `2px solid ${accentColor}`,
          display: "flex",
          alignItems: "center",
          paddingLeft: 48,
          gap: 16,
        }}
      >
        {/* Avatar placeholder */}
        <div
          style={{
            width: 44,
            height: 44,
            borderRadius: "50%",
            backgroundColor: accentColor,
            opacity: 0.85,
          }}
        />
        <span style={{ color: "#aaaaaa", fontSize: 28, fontFamily: "Arial, Helvetica, sans-serif" }}>
          Anonymous
        </span>
      </div>

      {text && (
        <div
          style={{
            opacity,
            maxWidth: "75%",
            alignSelf: "flex-start",
            marginLeft: 60,
            marginTop: 60,
          }}
        >
          {/* Speech bubble */}
          <div
            style={{
              background: "#23233a",
              border: `2px solid ${accentColor}33`,
              borderRadius: "0 24px 24px 24px",
              padding: "36px 48px",
              position: "relative",
            }}
          >
            <span
              style={{
                color: "#e8e8e8",
                fontSize: 52,
                fontFamily: "Arial, Helvetica, sans-serif",
                lineHeight: 1.4,
              }}
            >
              {text}
            </span>
          </div>
          {/* Timestamp */}
          <div
            style={{
              color: "#555577",
              fontSize: 26,
              fontFamily: "Arial, Helvetica, sans-serif",
              marginTop: 12,
              marginLeft: 16,
            }}
          >
            now
          </div>
        </div>
      )}
    </AbsoluteFill>
  );
}

// ── Document variant ───────────────────────────────────────────────────────────
// Simulates a typed document or report: off-white background, dark text,
// subtle top header bar with a coloured stripe.

function DocumentCard({ text, accentColor, transparentBackground }: { text: string; accentColor: string; transparentBackground: boolean }) {
  const { scale, opacity } = useCardAnimations();
  return (
    <AbsoluteFill
      style={{
        background: transparentBackground ? "transparent" : "#f5f0e8",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        transform: `scale(${scale})`,
        overflow: "hidden",
      }}
    >
      {/* Top classification stripe */}
      <div
        style={{
          position: "absolute",
          top: 0,
          left: 0,
          right: 0,
          height: 12,
          backgroundColor: accentColor,
        }}
      />

      {text && (
        <div
          style={{
            opacity,
            maxWidth: "80%",
            background: "#fffdf8",
            border: "1px solid #c8c0aa",
            boxShadow: "0 4px 32px rgba(0,0,0,0.18)",
            padding: "64px 80px",
            position: "relative",
          }}
        >
          {/* Document fold corner */}
          <div
            style={{
              position: "absolute",
              top: 0,
              right: 0,
              width: 0,
              height: 0,
              borderStyle: "solid",
              borderWidth: "0 48px 48px 0",
              borderColor: `transparent #c8c0aa transparent transparent`,
            }}
          />

          {/* Ruled lines suggestion */}
          <div style={{ borderBottom: "1px solid #ddd8cc", paddingBottom: 24, marginBottom: 32 }}>
            <span
              style={{
                color: "#888070",
                fontSize: 24,
                fontFamily: "Arial, Helvetica, sans-serif",
                textTransform: "uppercase",
                letterSpacing: 4,
              }}
            >
              DOCUMENT
            </span>
          </div>

          <span
            style={{
              color: "#1a1610",
              fontSize: 52,
              fontFamily: "Georgia, 'Times New Roman', serif",
              lineHeight: 1.5,
              display: "block",
            }}
          >
            {text}
          </span>
        </div>
      )}
    </AbsoluteFill>
  );
}

// ── Statistic variant ──────────────────────────────────────────────────────────
// Designed for numbers, percentages, or short factual statements.
// Splits text at the first line break (or "|") into a large primary stat and
// smaller context line. Falls back to rendering all text as the large value.

function StatisticCard({ text, accentColor, transparentBackground }: { text: string; accentColor: string; transparentBackground: boolean }) {
  const { scale, opacity } = useCardAnimations();

  // Split on "|" or newline to separate stat from context label
  const parts = text.split(/\||\\n|\n/).map((p) => p.trim()).filter(Boolean);
  const mainStat = parts[0] ?? "";
  const contextLine = parts[1] ?? "";

  return (
    <AbsoluteFill
      style={{
        background: transparentBackground ? "rgba(10, 10, 15, 0.30)" : "linear-gradient(160deg, #0f0f1a 0%, #1a1030 60%, #0a0a0f 100%)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        flexDirection: "column",
        transform: `scale(${scale})`,
        overflow: "hidden",
      }}
    >
      {/* Background accent circle */}
      <div
        style={{
          position: "absolute",
          width: 700,
          height: 700,
          borderRadius: "50%",
          border: `2px solid ${accentColor}22`,
          pointerEvents: "none",
        }}
      />

      {text && (
        <div style={{ opacity, textAlign: "center", padding: "0 80px" }}>
          {/* Large stat value */}
          <div
            style={{
              color: accentColor,
              fontSize: 160,
              fontFamily: "Arial, Helvetica, sans-serif",
              fontWeight: "900",
              lineHeight: 1,
              letterSpacing: -4,
              textShadow: `0 0 60px ${accentColor}66`,
            }}
          >
            {mainStat}
          </div>

          {/* Divider */}
          <div
            style={{
              width: 80,
              height: 3,
              backgroundColor: accentColor,
              margin: "24px auto",
              borderRadius: 2,
            }}
          />

          {/* Context line */}
          {contextLine && (
            <div
              style={{
                color: "#cccccc",
                fontSize: 42,
                fontFamily: "Arial, Helvetica, sans-serif",
                fontWeight: 400,
                lineHeight: 1.3,
                letterSpacing: 1,
                textTransform: "uppercase",
              }}
            >
              {contextLine}
            </div>
          )}
        </div>
      )}
    </AbsoluteFill>
  );
}

// ── Quote variant ──────────────────────────────────────────────────────────────
// For verbatim quotes: decorative oversized quotation marks, italic text,
// subtle vignette background.

function QuoteCard({ text, accentColor, transparentBackground }: { text: string; accentColor: string; transparentBackground: boolean }) {
  const { scale, opacity } = useCardAnimations();
  return (
    <AbsoluteFill
      style={{
        background: transparentBackground ? "rgba(10, 8, 12, 0.32)" : "radial-gradient(ellipse at center, #1c1520 0%, #0a080c 80%)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        transform: `scale(${scale})`,
        overflow: "hidden",
      }}
    >
      {text && (
        <div
          style={{
            opacity,
            maxWidth: "82%",
            textAlign: "center",
            position: "relative",
            padding: "60px 0",
          }}
        >
          {/* Opening quotation mark */}
          <div
            style={{
              color: accentColor,
              fontSize: 200,
              fontFamily: "Georgia, 'Times New Roman', serif",
              lineHeight: 0.6,
              position: "absolute",
              top: 0,
              left: -24,
              opacity: 0.6,
              userSelect: "none",
            }}
          >
            "
          </div>

          <span
            style={{
              color: "#f0ecea",
              fontSize: 56,
              fontFamily: "Georgia, 'Times New Roman', serif",
              fontStyle: "italic",
              lineHeight: 1.5,
              textShadow: "1px 1px 6px rgba(0,0,0,0.8)",
              display: "block",
              paddingTop: 64,
            }}
          >
            {text}
          </span>

          {/* Closing accent bar */}
          <div
            style={{
              width: 48,
              height: 3,
              backgroundColor: accentColor,
              margin: "32px auto 0",
              borderRadius: 2,
            }}
          />
        </div>
      )}
    </AbsoluteFill>
  );
}

// ── Main export ────────────────────────────────────────────────────────────────

/**
 * Multi-variant text card for beats where the content is primarily readable
 * text (chat, document, statistics, quotes) or as a Flux generation fallback.
 *
 * All variants include the standard 1.0→1.05 slow-zoom to keep the frame alive.
 * Wired in MediaSection when section.visual_type === "text_card".
 */
export const TextCard: React.FC<TextCardProps> = ({
  text,
  style = {},
  accentColor = "#FFD700",
  cardStyle = "default",
  transparentBackground = false,
}) => {
  const cardProps = { text, accentColor, transparentBackground };

  const inner = (() => {
    switch (cardStyle) {
      case "chat":       return <ChatCard      {...cardProps} />;
      case "document":   return <DocumentCard  {...cardProps} />;
      case "statistic":  return <StatisticCard {...cardProps} />;
      case "quote":      return <QuoteCard     {...cardProps} />;
      default:           return <DefaultCard   {...cardProps} />;
    }
  })();

  // Wrap with optional outer style (e.g. crossfade opacity from MediaSection)
  return (
    <AbsoluteFill style={style}>
      {inner}
    </AbsoluteFill>
  );
};
