import { useState } from "react";

interface Props {
  className?: string;
}

export default function DecisionGraph({ className }: Props) {
  const [loaded, setLoaded] = useState(false);

  return (
    <div
      className={className}
      style={{
        width: "100%",
        position: "relative",
        borderRadius: 12,
        overflow: "hidden",
        background: "#0f1117",
        border: "1px solid #1f2937",
      }}
    >
      {/* Loading shimmer shown until iframe fires onLoad */}
      {!loaded && (
        <div
          style={{
            position: "absolute",
            inset: 0,
            display: "flex",
            flexDirection: "column",
            alignItems: "center",
            justifyContent: "center",
            gap: 12,
            background: "#0f1117",
            zIndex: 2,
          }}
        >
          <div
            style={{
              width: 40,
              height: 40,
              border: "2px solid #22c55e33",
              borderTop: "2px solid #22c55e",
              borderRadius: "50%",
              animation: "irp-spin 0.9s linear infinite",
            }}
          />
          <span style={{ fontSize: 12, color: "#6b7280", fontFamily: "monospace" }}>
            Loading decision graph…
          </span>
          <style>{`@keyframes irp-spin { to { transform: rotate(360deg); } }`}</style>
        </div>
      )}

      <iframe
        src="/demo-graph.html"
        title="IRP Interactive Decision Graph — 18 design-system decisions with provenance edges"
        onLoad={() => setLoaded(true)}
        style={{
          width: "100%",
          height: 560,
          border: "none",
          display: "block",
          opacity: loaded ? 1 : 0,
          transition: "opacity 0.4s ease",
        }}
        allow="fullscreen"
      />
    </div>
  );
}
