import { useState, useEffect, useCallback, useRef } from "react";
import { motion, AnimatePresence } from "framer-motion";

// --- Types ---

type StageStatus = "idle" | "active" | "done";

interface PipelineStage {
  id: number;
  name: string;
  shortName: string;
  description: string;
  metric: string;
  timeMs: number;
  color: string;
}

// --- Data ---

const stages: PipelineStage[] = [
  {
    id: 1,
    name: "React Commit",
    shortName: "Commit",
    description: "React reconciles virtual DOM. State updates processed, commitUpdate diffs props. resetAfterCommit triggers Yoga layout.",
    metric: "12 nodes",
    timeMs: 0.3,
    color: "#60a5fa",
  },
  {
    id: 2,
    name: "Yoga Layout",
    shortName: "Yoga",
    description: "CSS flexbox layout via Yoga WASM. Resolves flex-grow, shrink, padding, margin, gap, alignment. Custom measureTextNode for word wrapping.",
    metric: "48 measured",
    timeMs: 0.5,
    color: "#818cf8",
  },
  {
    id: 3,
    name: "DOM-to-Screen",
    shortName: "Render",
    description: "Depth-first walk writes characters and styles into a packed Screen buffer. Each cell = 2 Int32 words. Blit fast-path copies unchanged subtrees.",
    metric: "384 cells",
    timeMs: 0.4,
    color: "#a78bfa",
  },
  {
    id: 4,
    name: "Selection/Search Overlay",
    shortName: "Overlay",
    description: "Text selection (inverse video) and search highlighting modify screen buffer in-place. Sets prevFrameContaminated flag.",
    metric: "0 modified",
    timeMs: 0.1,
    color: "#c084fc",
  },
  {
    id: 5,
    name: "Diff",
    shortName: "Diff",
    description: "Cell-by-cell comparison: 2 integer comparisons per cell. Only walks the damage rectangle. Steady-state: ~3 cells changed out of 24,000.",
    metric: "47 changed",
    timeMs: 0.3,
    color: "#f472b6",
  },
  {
    id: 6,
    name: "Optimize",
    shortName: "Optimize",
    description: "Merge adjacent patches on same row. Eliminate redundant cursor moves. Style transitions via StylePool.transition() cache. 30-50% byte reduction.",
    metric: "23 writes",
    timeMs: 0.2,
    color: "#fb923c",
  },
  {
    id: 7,
    name: "Terminal Write",
    shortName: "Write",
    description: "Single stdout.write() wrapped in BSU/ESU (synchronized update markers). Entire frame appears atomically -- no tearing.",
    metric: "1.2 KB",
    timeMs: 0.5,
    color: "#4ade80",
  },
];

const TOTAL_FRAME_MS = stages.reduce((sum, s) => sum + s.timeMs, 0);

// --- Helpers ---

function useDarkMode() {
  const [isDark, setIsDark] = useState(false);
  useEffect(() => {
    const check = () =>
      setIsDark(document.documentElement.classList.contains("dark"));
    check();
    window.addEventListener("theme-changed", check);
    const observer = new MutationObserver(check);
    observer.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ["class"],
    });
    return () => {
      window.removeEventListener("theme-changed", check);
      observer.disconnect();
    };
  }, []);
  return isDark;
}

// --- Double buffer mini grid ---

function MiniGrid({
  label,
  cells,
  highlight,
  isDark,
}: {
  label: string;
  cells: number[][];
  highlight: boolean;
  isDark: boolean;
}) {
  return (
    <div style={{ textAlign: "center" }}>
      <div
        style={{
          fontSize: 10,
          fontFamily: "var(--font-mono)",
          color: "#87867f",
          marginBottom: 4,
          fontWeight: 600,
        }}
      >
        {label}
      </div>
      <div
        style={{
          display: "inline-grid",
          gridTemplateColumns: `repeat(${cells[0].length}, 1fr)`,
          gap: 1,
          padding: 3,
          borderRadius: 6,
          border: highlight
            ? "2px solid #d97757"
            : `1px solid ${isDark ? "#333" : "#e8e6dc"}`,
          background: isDark ? "#1e1e1c" : "#fff",
          transition: "border-color 0.3s",
        }}
      >
        {cells.flat().map((val, i) => (
          <motion.div
            key={i}
            animate={{
              background:
                val === 0
                  ? isDark
                    ? "#2a2a28"
                    : "#f5f4ed"
                  : val === 1
                    ? "rgba(217, 119, 87, 0.4)"
                    : val === 2
                      ? "rgba(217, 119, 87, 0.7)"
                      : "rgba(74, 222, 128, 0.5)",
            }}
            style={{
              width: 12,
              height: 12,
              borderRadius: 2,
              transition: "background 0.15s",
            }}
          />
        ))}
      </div>
    </div>
  );
}

// --- Component ---

interface Props {
  className?: string;
}

export default function RenderingPipeline({ className }: Props) {
  const isDark = useDarkMode();
  const [stageStatuses, setStageStatuses] = useState<StageStatus[]>(() =>
    stages.map((): StageStatus => "idle")
  );
  const [isRunning, setIsRunning] = useState(false);
  const [frameTime, setFrameTime] = useState<number | null>(null);
  const [selectedStage, setSelectedStage] = useState<number | null>(null);
  const [showBlit, setShowBlit] = useState(false);
  const [activeBuffer, setActiveBuffer] = useState<"front" | "back">("front");
  const abortRef = useRef(false);

  // Double-buffer state
  const [frontCells, setFrontCells] = useState(() =>
    Array.from({ length: 6 }, () => Array.from({ length: 8 }, () => 0))
  );
  const [backCells, setBackCells] = useState(() =>
    Array.from({ length: 6 }, () => Array.from({ length: 8 }, () => 0))
  );

  const colors = {
    accent: "#d97757",
    accentBg: isDark
      ? "rgba(217, 119, 87, 0.08)"
      : "rgba(217, 119, 87, 0.05)",
    text: isDark ? "#f5f4ed" : "#141413",
    textSecondary: "#87867f",
    cardBg: isDark ? "#1e1e1c" : "#ffffff",
    cardBorder: isDark ? "#333" : "#e8e6dc",
    surfaceBg: isDark ? "#30302e" : "#f5f4ed",
  };

  const renderFrame = useCallback(async () => {
    abortRef.current = false;
    setIsRunning(true);
    setFrameTime(null);
    setStageStatuses(stages.map((): StageStatus => "idle"));

    let elapsed = 0;

    for (let i = 0; i < stages.length; i++) {
      if (abortRef.current) return;

      setStageStatuses((prev) =>
        prev.map((s, idx) => (idx === i ? "active" : idx < i ? "done" : s))
      );

      // Simulate buffer updates during render stage
      if (i === 2) {
        // DOM-to-Screen: write to back buffer
        setActiveBuffer("back");
        const newBack = Array.from({ length: 6 }, (_, r) =>
          Array.from({ length: 8 }, (_, c) =>
            (r >= 3 && r <= 5 && c >= 1 && c <= 6) ? 2 : 0
          )
        );
        setBackCells(newBack);
      }
      if (i === 4) {
        // Diff: highlight changed cells
        setBackCells((prev) =>
          prev.map((row, r) =>
            row.map((cell, c) =>
              cell === 2 && r === 4 && c >= 3 ? 3 : cell
            )
          )
        );
      }

      const stageTime = stages[i].timeMs * 400; // scale for animation
      await new Promise((r) => setTimeout(r, stageTime));
      if (abortRef.current) return;

      elapsed += stages[i].timeMs;
      setFrameTime(Math.round(elapsed * 10) / 10);
    }

    // Swap buffers
    setActiveBuffer("front");
    setFrontCells((prev) => {
      const newFront = Array.from({ length: 6 }, (_, r) =>
        Array.from({ length: 8 }, (_, c) =>
          (r >= 3 && r <= 5 && c >= 1 && c <= 6) ? 1 : 0
        )
      );
      return newFront;
    });
    setBackCells(Array.from({ length: 6 }, () => Array.from({ length: 8 }, () => 0)));

    setStageStatuses(stages.map((): StageStatus => "done"));
    setIsRunning(false);
  }, []);

  const reset = useCallback(() => {
    abortRef.current = true;
    setIsRunning(false);
    setFrameTime(null);
    setStageStatuses(stages.map((): StageStatus => "idle"));
    setSelectedStage(null);
    setActiveBuffer("front");
    setFrontCells(Array.from({ length: 6 }, () => Array.from({ length: 8 }, () => 0)));
    setBackCells(Array.from({ length: 6 }, () => Array.from({ length: 8 }, () => 0)));
  }, []);

  return (
    <div className={className} style={{ fontFamily: "var(--font-serif)" }}>
      {/* Controls */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 12,
          marginBottom: 20,
          padding: "14px 20px",
          background: colors.cardBg,
          border: `1px solid ${colors.cardBorder}`,
          borderRadius: 12,
          flexWrap: "wrap",
        }}
      >
        <button
          onClick={isRunning ? reset : renderFrame}
          style={{
            padding: "8px 20px",
            borderRadius: 8,
            border: "none",
            background: isRunning ? colors.textSecondary : colors.accent,
            color: "#fff",
            fontSize: 13,
            fontWeight: 600,
            fontFamily: "var(--font-mono)",
            cursor: "pointer",
            transition: "background 0.2s",
          }}
        >
          {isRunning ? "Reset" : "Render Frame"}
        </button>

        <label
          style={{
            display: "flex",
            alignItems: "center",
            gap: 6,
            fontSize: 12,
            color: colors.textSecondary,
            cursor: "pointer",
          }}
        >
          <input
            type="checkbox"
            checked={showBlit}
            onChange={(e) => setShowBlit(e.target.checked)}
            style={{ accentColor: colors.accent }}
          />
          Show double-buffer
        </label>

        <div style={{ flex: 1 }} />

        {frameTime !== null && (
          <motion.div
            initial={{ opacity: 0, scale: 0.9 }}
            animate={{ opacity: 1, scale: 1 }}
            style={{
              display: "flex",
              alignItems: "baseline",
              gap: 4,
            }}
          >
            <span style={{ fontSize: 11, fontFamily: "var(--font-mono)", color: colors.textSecondary }}>
              Frame:
            </span>
            <span
              style={{
                fontSize: 22,
                fontWeight: 700,
                fontFamily: "var(--font-mono)",
                color: colors.accent,
              }}
            >
              {frameTime}
            </span>
            <span style={{ fontSize: 11, fontFamily: "var(--font-mono)", color: colors.textSecondary }}>
              ms
            </span>
          </motion.div>
        )}
      </div>

      {/* Pipeline visualization */}
      <div
        style={{
          display: "flex",
          gap: 0,
          overflowX: "auto",
          paddingBottom: 8,
          marginBottom: 16,
        }}
      >
        {stages.map((stage, i) => {
          const status = stageStatuses[i];
          const isActive = status === "active";
          const isDone = status === "done";
          const isSelected = selectedStage === stage.id;

          return (
            <div
              key={stage.id}
              style={{ display: "flex", alignItems: "stretch", flex: 1, minWidth: 0 }}
            >
              <motion.button
                onClick={() =>
                  setSelectedStage(isSelected ? null : stage.id)
                }
                animate={{
                  scale: isActive ? 1.05 : 1,
                  borderColor: isActive
                    ? stage.color
                    : isSelected
                      ? colors.accent
                      : colors.cardBorder,
                }}
                style={{
                  flex: 1,
                  minWidth: 0,
                  padding: "12px 8px",
                  borderRadius: 10,
                  border: `2px solid ${colors.cardBorder}`,
                  background: isActive
                    ? `${stage.color}15`
                    : isDone
                      ? colors.accentBg
                      : colors.cardBg,
                  cursor: "pointer",
                  textAlign: "center",
                  transition: "background 0.2s",
                  position: "relative",
                  overflow: "hidden",
                }}
              >
                {/* Progress bar */}
                {isActive && (
                  <motion.div
                    initial={{ width: "0%" }}
                    animate={{ width: "100%" }}
                    transition={{ duration: stage.timeMs * 0.4, ease: "linear" }}
                    style={{
                      position: "absolute",
                      bottom: 0,
                      left: 0,
                      height: 3,
                      background: stage.color,
                    }}
                  />
                )}
                {isDone && (
                  <div
                    style={{
                      position: "absolute",
                      bottom: 0,
                      left: 0,
                      width: "100%",
                      height: 3,
                      background: stage.color,
                      opacity: 0.5,
                    }}
                  />
                )}

                <div
                  style={{
                    fontSize: 10,
                    fontWeight: 700,
                    fontFamily: "var(--font-mono)",
                    color: isActive ? stage.color : isDone ? colors.accent : colors.textSecondary,
                    marginBottom: 4,
                  }}
                >
                  {stage.id}
                </div>
                <div
                  style={{
                    fontSize: 11,
                    fontWeight: 600,
                    color: isActive ? stage.color : colors.text,
                    whiteSpace: "nowrap",
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                  }}
                >
                  {stage.shortName}
                </div>

                <AnimatePresence>
                  {(isActive || isDone) && (
                    <motion.div
                      initial={{ opacity: 0, height: 0 }}
                      animate={{ opacity: 1, height: "auto" }}
                      exit={{ opacity: 0, height: 0 }}
                      style={{
                        fontSize: 10,
                        fontFamily: "var(--font-mono)",
                        color: stage.color,
                        marginTop: 6,
                        fontWeight: 600,
                      }}
                    >
                      {stage.metric}
                    </motion.div>
                  )}
                </AnimatePresence>

                {isDone && (
                  <div
                    style={{
                      fontSize: 9,
                      fontFamily: "var(--font-mono)",
                      color: colors.textSecondary,
                      marginTop: 2,
                    }}
                  >
                    {stage.timeMs}ms
                  </div>
                )}
              </motion.button>

              {i < stages.length - 1 && (
                <div style={{ display: "flex", alignItems: "center", padding: "0 2px" }}>
                  <motion.div
                    animate={{
                      background: isDone ? colors.accent : colors.cardBorder,
                    }}
                    style={{
                      width: 16,
                      height: 2,
                      transition: "background 0.3s",
                    }}
                  />
                </div>
              )}
            </div>
          );
        })}
      </div>

      {/* Blit optimization callout */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 8,
          padding: "8px 14px",
          marginBottom: 16,
          borderRadius: 8,
          background: colors.surfaceBg,
          fontSize: 11,
          fontFamily: "var(--font-mono)",
          color: colors.textSecondary,
        }}
      >
        <svg width="14" height="14" viewBox="0 0 16 16" fill="none">
          <path d="M8 2L14 8L8 14L2 8L8 2Z" stroke="#d97757" strokeWidth="1.5" />
        </svg>
        <span>
          <strong style={{ color: colors.accent }}>Blit fast-path:</strong> Unchanged subtrees copy cells directly from prevScreen.
          On steady-state frames, 99% of cells are blitted -- only the spinner re-renders.
        </span>
      </div>

      {/* Selected stage detail */}
      <AnimatePresence>
        {selectedStage !== null && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.2 }}
            style={{ overflow: "hidden", marginBottom: 16 }}
          >
            {(() => {
              const stage = stages.find((s) => s.id === selectedStage);
              if (!stage) return null;
              return (
                <div
                  style={{
                    padding: "14px 18px",
                    borderRadius: 10,
                    border: `1px solid ${stage.color}40`,
                    background: `${stage.color}08`,
                  }}
                >
                  <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 8 }}>
                    <div
                      style={{
                        width: 24,
                        height: 24,
                        borderRadius: "50%",
                        background: stage.color,
                        color: "#fff",
                        display: "flex",
                        alignItems: "center",
                        justifyContent: "center",
                        fontSize: 12,
                        fontWeight: 700,
                        fontFamily: "var(--font-mono)",
                      }}
                    >
                      {stage.id}
                    </div>
                    <span style={{ fontSize: 14, fontWeight: 600, color: colors.text }}>
                      Stage {stage.id}: {stage.name}
                    </span>
                    <span
                      style={{
                        fontSize: 11,
                        fontFamily: "var(--font-mono)",
                        color: stage.color,
                        fontWeight: 600,
                      }}
                    >
                      ~{stage.timeMs}ms
                    </span>
                  </div>
                  <div style={{ fontSize: 12, color: colors.textSecondary, lineHeight: 1.6 }}>
                    {stage.description}
                  </div>
                </div>
              );
            })()}
          </motion.div>
        )}
      </AnimatePresence>

      {/* Double buffer visualization */}
      <AnimatePresence>
        {showBlit && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.2 }}
            style={{ overflow: "hidden", marginBottom: 16 }}
          >
            <div
              style={{
                padding: "16px 20px",
                borderRadius: 12,
                border: `1px solid ${colors.cardBorder}`,
                background: colors.cardBg,
              }}
            >
              <div
                style={{
                  fontSize: 13,
                  fontWeight: 600,
                  color: colors.text,
                  marginBottom: 12,
                }}
              >
                Double-Buffer Rendering
              </div>
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  gap: 32,
                  marginBottom: 12,
                }}
              >
                <MiniGrid
                  label="Front Buffer (displayed)"
                  cells={frontCells}
                  highlight={activeBuffer === "front"}
                  isDark={isDark}
                />
                <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 4 }}>
                  <svg width="32" height="16" viewBox="0 0 32 16" fill="none">
                    <path d="M2 8H28M28 8L22 3M28 8L22 13" stroke={colors.accent} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
                  </svg>
                  <span style={{ fontSize: 9, fontFamily: "var(--font-mono)", color: colors.textSecondary }}>swap</span>
                </div>
                <MiniGrid
                  label="Back Buffer (rendering)"
                  cells={backCells}
                  highlight={activeBuffer === "back"}
                  isDark={isDark}
                />
              </div>
              <div style={{ fontSize: 11, color: colors.textSecondary, textAlign: "center", lineHeight: 1.5 }}>
                Frames swap via pointer assignment -- zero allocation. Old front becomes next back for blit optimization and diffing.
              </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Frame event breakdown */}
      <div
        style={{
          padding: "14px 18px",
          borderRadius: 12,
          border: `1px solid ${colors.cardBorder}`,
          background: colors.cardBg,
        }}
      >
        <div
          style={{
            fontSize: 13,
            fontWeight: 600,
            color: colors.text,
            marginBottom: 10,
          }}
        >
          FrameEvent Timing Breakdown
        </div>
        <div style={{ display: "flex", alignItems: "flex-end", gap: 4, height: 80, marginBottom: 8 }}>
          {stages.map((stage) => {
            const status = stageStatuses[stage.id - 1];
            const isDone = status === "done";
            const heightPct = (stage.timeMs / TOTAL_FRAME_MS) * 100;
            return (
              <motion.div
                key={stage.id}
                animate={{
                  height: isDone ? `${heightPct}%` : "8%",
                  background: isDone ? stage.color : colors.surfaceBg,
                }}
                style={{
                  flex: 1,
                  borderRadius: 4,
                  minHeight: 4,
                  transition: "all 0.3s",
                  cursor: "pointer",
                }}
                onClick={() => setSelectedStage(selectedStage === stage.id ? null : stage.id)}
                title={`${stage.shortName}: ${stage.timeMs}ms`}
              />
            );
          })}
        </div>
        <div style={{ display: "flex", gap: 4 }}>
          {stages.map((stage) => (
            <div
              key={stage.id}
              style={{
                flex: 1,
                textAlign: "center",
                fontSize: 8,
                fontFamily: "var(--font-mono)",
                color: colors.textSecondary,
                overflow: "hidden",
                textOverflow: "ellipsis",
                whiteSpace: "nowrap",
              }}
            >
              {stage.shortName}
            </div>
          ))}
        </div>
        <div style={{ textAlign: "center", marginTop: 8, fontSize: 11, color: colors.textSecondary }}>
          Total: <strong style={{ color: colors.accent, fontFamily: "var(--font-mono)" }}>{TOTAL_FRAME_MS.toFixed(1)}ms</strong> per frame
          {" "} -- {Math.round(1000 / TOTAL_FRAME_MS)}fps theoretical maximum
        </div>
      </div>
    </div>
  );
}
