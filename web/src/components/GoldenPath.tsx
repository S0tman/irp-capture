import { useState, useEffect, useCallback, useRef } from "react";
import { motion, AnimatePresence } from "framer-motion";

// --- Types ---

type PlayMode = "idle" | "playing" | "paused" | "done";

interface Participant {
  id: string;
  label: string;
  shortLabel: string;
}

interface SequenceMessage {
  id: number;
  from: string;
  to: string;
  label: string;
  detail: string;
  isLoopBack?: boolean;
  terminalText?: string;
  durationMs: number;
}

// --- Data ---

const participants: Participant[] = [
  { id: "user", label: "User / REPL", shortLabel: "User" },
  { id: "query", label: "Query Loop", shortLabel: "Query" },
  { id: "model", label: "Model API", shortLabel: "Model" },
  { id: "executor", label: "StreamingToolExecutor", shortLabel: "Executor" },
  { id: "tools", label: "Tool System", shortLabel: "Tools" },
];

const sequence: SequenceMessage[] = [
  {
    id: 1,
    from: "user",
    to: "query",
    label: "UserMessage",
    detail: "REPL captures input, wraps as UserMessage, feeds to query loop generator",
    terminalText: '> add error handling to the login function\n\n  Thinking...',
    durationMs: 200,
  },
  {
    id: 2,
    from: "query",
    to: "query",
    label: "Token count check",
    detail: "Check context window usage. Auto-compact if conversation exceeds budget",
    terminalText: '> add error handling to the login function\n\n  Thinking... (context: 12,847 tokens)',
    durationMs: 150,
  },
  {
    id: 3,
    from: "query",
    to: "model",
    label: "callModel() streams request",
    detail: "System prompt + memory + conversation history sent to Claude API",
    terminalText: '> add error handling to the login function\n\n  Streaming response...',
    durationMs: 300,
  },
  {
    id: 4,
    from: "model",
    to: "query",
    label: "Tokens stream back",
    detail: "Response arrives as streaming chunks: text blocks and tool_use blocks interleaved",
    terminalText: '> add error handling to the login function\n\n  I\'ll read the login function first...',
    durationMs: 400,
  },
  {
    id: 5,
    from: "model",
    to: "executor",
    label: "Detects tool_use blocks",
    detail: "StreamingToolExecutor intercepts tool_use blocks mid-stream before response completes",
    terminalText: '> add error handling to the login function\n\n  I\'ll read the login function first...\n  [tool_use: Read /src/auth/login.ts]',
    durationMs: 250,
  },
  {
    id: 6,
    from: "executor",
    to: "tools",
    label: "Start concurrency-safe tools early",
    detail: "Read-only tools (Read, Grep) start speculatively while model still streams",
    terminalText: '> add error handling to the login function\n\n  Read /src/auth/login.ts (speculative)',
    durationMs: 300,
  },
  {
    id: 7,
    from: "tools",
    to: "executor",
    label: "Results (may finish before model)",
    detail: "Tool results queued. If model invalidates the call, results are discarded",
    terminalText: '> add error handling to the login function\n\n  Read /src/auth/login.ts -> 156 lines',
    durationMs: 200,
  },
  {
    id: 8,
    from: "query",
    to: "tools",
    label: "Execute remaining tools",
    detail: "Serial/concurrent batch: Validate -> Hooks -> Permissions -> Execute",
    terminalText: '> add error handling to the login function\n\n  Edit /src/auth/login.ts (42 lines changed)',
    durationMs: 350,
  },
  {
    id: 9,
    from: "tools",
    to: "query",
    label: "ToolResultMessages",
    detail: "Results mapped to ContentBlock[], budgeted to per-tool size caps, appended to history",
    terminalText: '> add error handling to the login function\n\n  Edit applied. Checking if more changes needed...',
    durationMs: 200,
  },
  {
    id: 10,
    from: "query",
    to: "query",
    label: "Stop check: loop?",
    detail: "More tool calls pending? Continue loop. No more calls? Generate final response",
    isLoopBack: true,
    terminalText: '> add error handling to the login function\n\n  More tool calls detected -> loop continues',
    durationMs: 200,
  },
  {
    id: 11,
    from: "query",
    to: "model",
    label: "Loop: updated context",
    detail: "Tool results appended to message history. Model called again with full context",
    isLoopBack: true,
    terminalText: '> add error handling to the login function\n\n  Re-entering query loop with tool results...',
    durationMs: 300,
  },
  {
    id: 12,
    from: "model",
    to: "query",
    label: "Final text response",
    detail: "No more tool_use blocks. Model generates final text response to user",
    terminalText: '> add error handling to the login function\n\n  Done. Added try-catch blocks with specific\n  error types for auth failures, rate limits,\n  and network errors.',
    durationMs: 400,
  },
  {
    id: 13,
    from: "query",
    to: "user",
    label: "Yield Messages -> render",
    detail: "Generator yields final messages. REPL renders markdown to terminal via Ink",
    terminalText: '> add error handling to the login function\n\n  Done. Added try-catch blocks with specific\n  error types for auth failures, rate limits,\n  and network errors.\n\n  Files changed: /src/auth/login.ts',
    durationMs: 250,
  },
];

const TOTAL_DURATION = "~3.2 seconds";

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

function ArrowIcon({ direction, color }: { direction: "right" | "left"; color: string }) {
  return (
    <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
      {direction === "right" ? (
        <path d="M2 7h10M9 4l3 3-3 3" stroke={color} strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
      ) : (
        <path d="M12 7H2M5 4L2 7l3 3" stroke={color} strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
      )}
    </svg>
  );
}

function LoopIcon({ color }: { color: string }) {
  return (
    <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
      <path d="M11 7a4 4 0 0 1-7.46 2M3 7a4 4 0 0 1 7.46-2" stroke={color} strokeWidth="1.5" strokeLinecap="round" />
      <path d="M3 11V9h2M11 3v2h-2" stroke={color} strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

// --- Component ---

interface Props {
  className?: string;
}

export default function GoldenPath({ className }: Props) {
  const isDark = useDarkMode();
  const [mode, setMode] = useState<PlayMode>("idle");
  const [currentStep, setCurrentStep] = useState(-1);
  const abortRef = useRef(false);
  const timeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const colors = {
    accent: "#d97757",
    accentDim: "rgba(217, 119, 87, 0.3)",
    accentBg: isDark ? "rgba(217, 119, 87, 0.08)" : "rgba(217, 119, 87, 0.05)",
    text: isDark ? "#f5f4ed" : "#141413",
    textSecondary: "#87867f",
    cardBg: isDark ? "#1e1e1c" : "#ffffff",
    cardBorder: isDark ? "#333" : "#e8e6dc",
    terminalBg: isDark ? "#0a0a09" : "#141413",
    terminalText: "#22c55e",
    terminalDim: "#87867f",
    loopBg: isDark ? "rgba(217, 119, 87, 0.12)" : "rgba(217, 119, 87, 0.08)",
  };

  const reset = useCallback(() => {
    abortRef.current = true;
    if (timeoutRef.current) clearTimeout(timeoutRef.current);
    setMode("idle");
    setCurrentStep(-1);
  }, []);

  const advanceStep = useCallback((step: number) => {
    if (step >= sequence.length) {
      setMode("done");
      return;
    }
    setCurrentStep(step);
  }, []);

  const playAll = useCallback(async () => {
    reset();
    await new Promise((r) => setTimeout(r, 50));
    abortRef.current = false;
    setMode("playing");

    for (let i = 0; i < sequence.length; i++) {
      if (abortRef.current) return;
      setCurrentStep(i);
      await new Promise<void>((resolve) => {
        timeoutRef.current = setTimeout(resolve, sequence[i].durationMs + 400);
      });
    }
    if (!abortRef.current) {
      setMode("done");
    }
  }, [reset]);

  const stepForward = useCallback(() => {
    if (mode === "idle") {
      setMode("paused");
      setCurrentStep(0);
    } else if (mode === "paused" || mode === "done") {
      const next = currentStep + 1;
      if (next >= sequence.length) {
        setMode("done");
      } else {
        setMode("paused");
        setCurrentStep(next);
      }
    }
  }, [mode, currentStep]);

  useEffect(() => {
    return () => {
      abortRef.current = true;
      if (timeoutRef.current) clearTimeout(timeoutRef.current);
    };
  }, []);

  const getParticipantIndex = (id: string) => participants.findIndex((p) => p.id === id);

  const currentMessage = currentStep >= 0 && currentStep < sequence.length ? sequence[currentStep] : null;

  return (
    <div className={className} style={{ fontFamily: "var(--font-serif)" }}>
      {/* Controls */}
      <div
        style={{
          display: "flex",
          flexWrap: "wrap",
          alignItems: "center",
          gap: 12,
          marginBottom: 24,
          padding: "16px 20px",
          background: colors.cardBg,
          border: `1px solid ${colors.cardBorder}`,
          borderRadius: 12,
        }}
      >
        <button
          onClick={mode === "playing" ? reset : playAll}
          style={{
            padding: "8px 20px",
            borderRadius: 8,
            border: "none",
            background: mode === "playing" ? colors.textSecondary : colors.accent,
            color: "#fff",
            fontSize: 13,
            fontWeight: 600,
            fontFamily: "var(--font-mono)",
            cursor: "pointer",
            transition: "background 0.2s",
          }}
        >
          {mode === "playing" ? "Stop" : mode === "done" ? "Replay" : "Play"}
        </button>

        <button
          onClick={stepForward}
          disabled={mode === "playing"}
          style={{
            padding: "8px 20px",
            borderRadius: 8,
            border: `1px solid ${colors.cardBorder}`,
            background: colors.cardBg,
            color: mode === "playing" ? colors.textSecondary : colors.text,
            fontSize: 13,
            fontWeight: 600,
            fontFamily: "var(--font-mono)",
            cursor: mode === "playing" ? "not-allowed" : "pointer",
            transition: "background 0.2s",
            opacity: mode === "playing" ? 0.5 : 1,
          }}
        >
          Step
        </button>

        <button
          onClick={reset}
          disabled={mode === "idle"}
          style={{
            padding: "8px 16px",
            borderRadius: 8,
            border: `1px solid ${colors.cardBorder}`,
            background: colors.cardBg,
            color: mode === "idle" ? colors.textSecondary : colors.text,
            fontSize: 13,
            fontFamily: "var(--font-mono)",
            cursor: mode === "idle" ? "not-allowed" : "pointer",
            opacity: mode === "idle" ? 0.5 : 1,
          }}
        >
          Reset
        </button>

        <div style={{ flex: 1, minWidth: 20 }} />

        <span
          style={{
            fontSize: 12,
            fontFamily: "var(--font-mono)",
            color: colors.textSecondary,
          }}
        >
          {currentStep >= 0 ? `Step ${currentStep + 1}/${sequence.length}` : "Ready"}{" "}
          &middot; Total: {TOTAL_DURATION}
        </span>
      </div>

      {/* Participant columns header */}
      <div
        style={{
          display: "grid",
          gridTemplateColumns: `repeat(${participants.length}, 1fr)`,
          gap: 8,
          marginBottom: 16,
        }}
      >
        {participants.map((p) => {
          const isActive =
            currentMessage &&
            (currentMessage.from === p.id || currentMessage.to === p.id);
          return (
            <motion.div
              key={p.id}
              animate={{
                borderColor: isActive ? colors.accent : colors.cardBorder,
                background: isActive ? colors.accentBg : colors.cardBg,
              }}
              transition={{ duration: 0.3 }}
              style={{
                padding: "10px 8px",
                borderRadius: 10,
                border: `1.5px solid ${colors.cardBorder}`,
                textAlign: "center",
              }}
            >
              <div
                style={{
                  fontSize: 12,
                  fontWeight: 600,
                  fontFamily: "var(--font-mono)",
                  color: isActive ? colors.accent : colors.text,
                  whiteSpace: "nowrap",
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                }}
              >
                <span className="golden-path-full-label">{p.label}</span>
                <span className="golden-path-short-label">{p.shortLabel}</span>
              </div>
            </motion.div>
          );
        })}
      </div>

      <style>{`
        .golden-path-short-label { display: none; }
        @media (max-width: 640px) {
          .golden-path-full-label { display: none; }
          .golden-path-short-label { display: inline; }
        }
      `}</style>

      {/* Sequence messages */}
      <div style={{ position: "relative" }}>
        {sequence.map((msg, i) => {
          const fromIdx = getParticipantIndex(msg.from);
          const toIdx = getParticipantIndex(msg.to);
          const isSelf = fromIdx === toIdx;
          const leftCol = Math.min(fromIdx, toIdx);
          const rightCol = Math.max(fromIdx, toIdx);
          const colSpan = rightCol - leftCol + 1;
          const goesRight = toIdx > fromIdx;

          const isVisible = i <= currentStep;
          const isCurrent = i === currentStep;

          return (
            <AnimatePresence key={msg.id}>
              {isVisible && (
                <motion.div
                  initial={{ opacity: 0, y: 10 }}
                  animate={{ opacity: 1, y: 0 }}
                  transition={{ duration: 0.3 }}
                  style={{
                    display: "grid",
                    gridTemplateColumns: `repeat(${participants.length}, 1fr)`,
                    gap: 8,
                    marginBottom: 6,
                  }}
                >
                  <motion.div
                    animate={{
                      background: isCurrent
                        ? msg.isLoopBack
                          ? colors.loopBg
                          : colors.accentBg
                        : "transparent",
                      borderColor: isCurrent ? colors.accent : "transparent",
                    }}
                    transition={{ duration: 0.3 }}
                    style={{
                      gridColumn: `${leftCol + 1} / ${leftCol + colSpan + 1}`,
                      padding: "8px 12px",
                      borderRadius: 8,
                      border: "1px solid transparent",
                      display: "flex",
                      alignItems: "center",
                      gap: 8,
                      minHeight: 36,
                    }}
                  >
                    {msg.isLoopBack ? (
                      <LoopIcon color={isCurrent ? colors.accent : colors.textSecondary} />
                    ) : isSelf ? (
                      <LoopIcon color={isCurrent ? colors.accent : colors.textSecondary} />
                    ) : goesRight ? (
                      <ArrowIcon direction="right" color={isCurrent ? colors.accent : colors.textSecondary} />
                    ) : (
                      <ArrowIcon direction="left" color={isCurrent ? colors.accent : colors.textSecondary} />
                    )}

                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div
                        style={{
                          fontSize: 13,
                          fontWeight: 600,
                          fontFamily: "var(--font-mono)",
                          color: isCurrent ? colors.accent : colors.text,
                          opacity: isCurrent ? 1 : 0.6,
                        }}
                      >
                        {msg.label}
                      </div>
                      {isCurrent && (
                        <motion.div
                          initial={{ height: 0, opacity: 0 }}
                          animate={{ height: "auto", opacity: 1 }}
                          transition={{ duration: 0.2 }}
                          style={{
                            fontSize: 12,
                            color: colors.textSecondary,
                            marginTop: 2,
                            lineHeight: 1.4,
                          }}
                        >
                          {msg.detail}
                        </motion.div>
                      )}
                    </div>

                    <span
                      style={{
                        fontSize: 10,
                        fontFamily: "var(--font-mono)",
                        color: colors.textSecondary,
                        whiteSpace: "nowrap",
                        opacity: isCurrent ? 1 : 0.4,
                      }}
                    >
                      {msg.durationMs}ms
                    </span>
                  </motion.div>
                </motion.div>
              )}
            </AnimatePresence>
          );
        })}
      </div>

      {/* Mini terminal preview */}
      <AnimatePresence>
        {currentMessage?.terminalText && (
          <motion.div
            initial={{ opacity: 0, y: 10 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -5 }}
            transition={{ duration: 0.3 }}
            style={{
              marginTop: 20,
              borderRadius: 12,
              overflow: "hidden",
              border: `1px solid ${colors.cardBorder}`,
            }}
          >
            {/* Terminal title bar */}
            <div
              style={{
                background: isDark ? "#1a1a18" : "#1e1e1c",
                padding: "8px 14px",
                display: "flex",
                alignItems: "center",
                gap: 6,
              }}
            >
              <div style={{ width: 10, height: 10, borderRadius: "50%", background: "#ef4444" }} />
              <div style={{ width: 10, height: 10, borderRadius: "50%", background: "#eab308" }} />
              <div style={{ width: 10, height: 10, borderRadius: "50%", background: "#22c55e" }} />
              <span
                style={{
                  marginLeft: 8,
                  fontSize: 11,
                  fontFamily: "var(--font-mono)",
                  color: "#87867f",
                }}
              >
                Terminal
              </span>
            </div>
            {/* Terminal body */}
            <div
              style={{
                background: colors.terminalBg,
                padding: "14px 16px",
                minHeight: 80,
              }}
            >
              <pre
                style={{
                  margin: 0,
                  fontSize: 12,
                  fontFamily: "var(--font-mono)",
                  color: colors.terminalText,
                  lineHeight: 1.6,
                  whiteSpace: "pre-wrap",
                  wordBreak: "break-word",
                }}
              >
                {currentMessage.terminalText}
              </pre>
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Completion badge */}
      <AnimatePresence>
        {mode === "done" && (
          <motion.div
            initial={{ opacity: 0, y: 10 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.3 }}
            style={{
              marginTop: 16,
              padding: "14px 20px",
              borderRadius: 12,
              border: `1px solid ${colors.accentDim}`,
              background: colors.accentBg,
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              flexWrap: "wrap",
              gap: 8,
            }}
          >
            <div>
              <div
                style={{
                  fontSize: 14,
                  fontWeight: 600,
                  color: colors.accent,
                  fontFamily: "var(--font-mono)",
                }}
              >
                Golden path complete
              </div>
              <div style={{ fontSize: 12, color: colors.textSecondary, marginTop: 2 }}>
                13 messages, 1 tool loop iteration, {TOTAL_DURATION} end-to-end
              </div>
            </div>
            <div
              style={{
                fontSize: 11,
                fontFamily: "var(--font-mono)",
                color: colors.textSecondary,
                padding: "4px 10px",
                borderRadius: 6,
                background: isDark ? "rgba(217, 119, 87, 0.15)" : "rgba(217, 119, 87, 0.12)",
              }}
            >
              Generator yielded Terminal.Success
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
