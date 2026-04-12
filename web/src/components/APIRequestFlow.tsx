import { useState, useEffect, useRef, useCallback } from "react";
import { motion, AnimatePresence } from "framer-motion";

// --- Dark Mode Hook ---

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

// --- Types ---

type Participant = "loop" | "factory" | "api" | "cache";

interface SequenceStep {
  id: number;
  from: Participant;
  to: Participant;
  label: string;
  sublabel?: string;
  description: string;
  highlight?: "cache" | "watchdog" | "error" | "retry";
  tokens?: number;
}

// --- Step Data ---

const normalSteps: SequenceStep[] = [
  {
    id: 1,
    from: "loop",
    to: "factory",
    label: "createStream(messages)",
    description:
      "The query loop initiates an API call, passing the full message array and configuration.",
  },
  {
    id: 2,
    from: "factory",
    to: "factory",
    label: "Provider dispatch",
    sublabel: "Direct / Bedrock / Vertex",
    description:
      "Client factory selects the provider via environment variables. All four SDKs are cast to a uniform Anthropic interface.",
  },
  {
    id: 3,
    from: "factory",
    to: "api",
    label: "Headers + system prompt",
    sublabel: "beta headers, sticky latches, cache_control",
    description:
      "Beta headers assembled with sticky latches (once set to true, never reverts). System prompt split at the dynamic boundary for optimal caching.",
  },
  {
    id: 4,
    from: "api",
    to: "cache",
    label: "Cache prefix check",
    sublabel: "50-70K token prefix",
    description:
      "Server checks if the stable prompt prefix matches a cached entry. Static sections get global scope; dynamic sections get per-session scope.",
    highlight: "cache",
  },
  {
    id: 5,
    from: "cache",
    to: "api",
    label: "Cache HIT",
    sublabel: "Saved ~$0.12 on 60K tokens",
    description:
      "Cache hit on the static prefix. The server skips reprocessing 50-70K tokens of system prompt and early conversation history.",
    highlight: "cache",
    tokens: 0,
  },
  {
    id: 6,
    from: "api",
    to: "loop",
    label: "SSE stream begins",
    sublabel: "Raw Stream<BetaRawMessageStreamEvent>",
    description:
      "Response streams back as server-sent events. Uses raw SSE (not SDK's BetaMessageStream) to avoid O(n\u00B2) partial JSON parsing.",
    tokens: 0,
  },
  {
    id: 7,
    from: "loop",
    to: "loop",
    label: "Idle watchdog: 90s",
    sublabel: "Resets on each chunk",
    description:
      "A setTimeout resets on every received chunk. If no chunks arrive for 90 seconds, the stream is aborted and a non-streaming fallback fires.",
    highlight: "watchdog",
    tokens: 847,
  },
  {
    id: 8,
    from: "api",
    to: "loop",
    label: "Tokens streaming...",
    sublabel: "content_block_delta events",
    description:
      "Text and tool_use blocks arrive incrementally. The streaming executor can start concurrency-safe tools before the response completes.",
    tokens: 2431,
  },
  {
    id: 9,
    from: "api",
    to: "loop",
    label: "Stream complete",
    sublabel: "message_stop event",
    description:
      "The final SSE event arrives. Response is parsed into an AssistantMessage with text blocks and tool_use blocks.",
    tokens: 3892,
  },
  {
    id: 10,
    from: "loop",
    to: "loop",
    label: "Response parsed",
    sublabel: "AssistantMessage + tool_use blocks",
    description:
      "The loop processes the complete response: extracts tool calls, updates state, checks for errors, and prepares for the next iteration.",
    tokens: 3892,
  },
];

const errorSteps: SequenceStep[] = [
  ...normalSteps.slice(0, 6),
  {
    id: 7,
    from: "api",
    to: "loop",
    label: "529 Overloaded",
    sublabel: "Server at capacity",
    description:
      "The API returns a 529 status. The withRetry() generator yields a SystemAPIErrorMessage so the UI can show retry status.",
    highlight: "error",
    tokens: 0,
  },
  {
    id: 8,
    from: "loop",
    to: "loop",
    label: "Backoff: 1s",
    sublabel: "Attempt 1 of 3",
    description:
      "Exponential backoff begins. The retry progress appears as a natural part of the event stream, not a side-channel notification.",
    highlight: "retry",
    tokens: 0,
  },
  {
    id: 9,
    from: "loop",
    to: "api",
    label: "Retry request",
    sublabel: "Same parameters",
    description:
      "The request is resent with identical parameters. Optionally downgrades fast mode on 529.",
    highlight: "retry",
    tokens: 0,
  },
  {
    id: 10,
    from: "api",
    to: "loop",
    label: "529 again",
    sublabel: "Still overloaded",
    description: "Second failure. Backoff interval doubles.",
    highlight: "error",
    tokens: 0,
  },
  {
    id: 11,
    from: "loop",
    to: "loop",
    label: "Backoff: 2s",
    sublabel: "Attempt 2 of 3",
    description:
      "Longer wait. The generator yields status events that the UI renders as a loading indicator.",
    highlight: "retry",
    tokens: 0,
  },
  {
    id: 12,
    from: "loop",
    to: "api",
    label: "Retry request",
    sublabel: "Attempt 3",
    description: "Final retry attempt with 4s backoff if this fails too.",
    highlight: "retry",
    tokens: 0,
  },
  {
    id: 13,
    from: "api",
    to: "loop",
    label: "200 OK -- streaming",
    sublabel: "Recovery successful",
    description:
      "The request succeeds on retry. Normal streaming resumes. The earlier errors were withheld from the consumer.",
    tokens: 3892,
  },
];

// --- Participant Layout ---

const participants: { id: Participant; label: string; short: string }[] = [
  { id: "loop", label: "Query Loop", short: "Loop" },
  { id: "factory", label: "Client Factory", short: "Factory" },
  { id: "api", label: "Provider API", short: "API" },
  { id: "cache", label: "Cache", short: "Cache" },
];

const participantX: Record<Participant, number> = {
  loop: 0,
  factory: 1,
  api: 2,
  cache: 3,
};

// --- Component ---

export default function APIRequestFlow({
  className = "",
}: {
  className?: string;
}) {
  const isDark = useDarkMode();
  const [currentStep, setCurrentStep] = useState(-1);
  const [isPlaying, setIsPlaying] = useState(false);
  const [simulateError, setSimulateError] = useState(false);
  const [tokenCount, setTokenCount] = useState(0);
  const [watchdogTime, setWatchdogTime] = useState(90);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const watchdogRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const steps = simulateError ? errorSteps : normalSteps;

  const colors = {
    bg: isDark ? "#1e1e1c" : "#ffffff",
    surface: isDark ? "#2a2a28" : "#f5f4ed",
    text: isDark ? "#f5f4ed" : "#141413",
    textMuted: isDark ? "#87867f" : "#87867f",
    border: isDark ? "#444" : "#c2c0b6",
    terracotta: "#d97757",
    green: "#22c55e",
    red: "#ef4444",
    amber: "#eda100",
    blue: isDark ? "#60a5fa" : "#3b82f6",
    cacheBg: isDark
      ? "rgba(34, 197, 94, 0.12)"
      : "rgba(34, 197, 94, 0.08)",
    errorBg: isDark
      ? "rgba(239, 68, 68, 0.12)"
      : "rgba(239, 68, 68, 0.08)",
    retryBg: isDark
      ? "rgba(237, 161, 0, 0.12)"
      : "rgba(237, 161, 0, 0.08)",
    watchdogBg: isDark
      ? "rgba(96, 165, 250, 0.12)"
      : "rgba(59, 130, 246, 0.08)",
  };

  const cleanup = useCallback(() => {
    if (timerRef.current) {
      clearInterval(timerRef.current);
      timerRef.current = null;
    }
    if (watchdogRef.current) {
      clearInterval(watchdogRef.current);
      watchdogRef.current = null;
    }
  }, []);

  const reset = useCallback(() => {
    cleanup();
    setCurrentStep(-1);
    setIsPlaying(false);
    setTokenCount(0);
    setWatchdogTime(90);
  }, [cleanup]);

  // Cleanup on unmount
  useEffect(() => cleanup, [cleanup]);

  // Reset when toggling error mode
  useEffect(() => {
    reset();
  }, [simulateError, reset]);

  // Auto-play logic
  useEffect(() => {
    if (!isPlaying) return;

    if (currentStep >= steps.length - 1) {
      setIsPlaying(false);
      return;
    }

    const delay = steps[currentStep + 1]?.highlight === "retry" ? 1500 : 900;
    timerRef.current = setTimeout(() => {
      setCurrentStep((prev) => prev + 1);
    }, delay) as unknown as ReturnType<typeof setInterval>;

    return () => {
      if (timerRef.current) clearTimeout(timerRef.current);
    };
  }, [isPlaying, currentStep, steps]);

  // Update token count and watchdog on step change
  useEffect(() => {
    if (currentStep < 0) return;
    const step = steps[currentStep];
    if (step?.tokens !== undefined) {
      setTokenCount(step.tokens);
    }

    // Reset watchdog on streaming steps
    if (step?.highlight === "watchdog") {
      setWatchdogTime(90);
      // Start countdown
      watchdogRef.current = setInterval(() => {
        setWatchdogTime((prev) => {
          if (prev <= 0) {
            if (watchdogRef.current) clearInterval(watchdogRef.current);
            return 0;
          }
          return prev - 1;
        });
      }, 50) as ReturnType<typeof setInterval>;
    } else if (
      step?.from === "api" &&
      step?.to === "loop" &&
      !step?.highlight
    ) {
      // Reset watchdog on normal chunks
      setWatchdogTime(90);
    }
  }, [currentStep, steps]);

  const stepForward = () => {
    if (currentStep < steps.length - 1) {
      setCurrentStep((prev) => prev + 1);
    }
  };

  const play = () => {
    if (currentStep >= steps.length - 1) {
      reset();
      setTimeout(() => {
        setIsPlaying(true);
        setCurrentStep(0);
      }, 100);
    } else {
      setIsPlaying(true);
      if (currentStep < 0) setCurrentStep(0);
    }
  };

  const highlightColor = (h?: string) => {
    switch (h) {
      case "cache":
        return colors.green;
      case "watchdog":
        return colors.blue;
      case "error":
        return colors.red;
      case "retry":
        return colors.amber;
      default:
        return colors.terracotta;
    }
  };

  const highlightBg = (h?: string) => {
    switch (h) {
      case "cache":
        return colors.cacheBg;
      case "error":
        return colors.errorBg;
      case "retry":
        return colors.retryBg;
      case "watchdog":
        return colors.watchdogBg;
      default:
        return "transparent";
    }
  };

  // Compute column width based on container
  const colWidth = 160;
  const diagramWidth = colWidth * 4;

  return (
    <div
      className={className}
      style={{
        fontFamily: "var(--font-serif)",
        color: colors.text,
        maxWidth: 860,
        margin: "0 auto",
      }}
    >
      {/* Header */}
      <div style={{ marginBottom: 20 }}>
        <h3
          style={{
            fontFamily: "var(--font-serif)",
            fontSize: 20,
            fontWeight: 600,
            margin: "0 0 6px 0",
            color: colors.text,
          }}
        >
          API Request / Response Lifecycle
        </h3>
        <p
          style={{
            fontSize: 14,
            color: colors.textMuted,
            margin: 0,
            lineHeight: 1.5,
          }}
        >
          A single API call traced from the query loop through provider
          selection, caching, streaming, and error recovery.
        </p>
      </div>

      {/* Controls */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 12,
          marginBottom: 20,
          flexWrap: "wrap",
        }}
      >
        <button
          onClick={isPlaying ? () => setIsPlaying(false) : play}
          style={{
            padding: "8px 20px",
            borderRadius: 8,
            border: "none",
            background: colors.terracotta,
            color: "#fff",
            cursor: "pointer",
            fontFamily: "var(--font-mono)",
            fontSize: 13,
            fontWeight: 600,
          }}
        >
          {isPlaying
            ? "\u23F8 Pause"
            : currentStep >= steps.length - 1
              ? "\u21BB Replay"
              : "\u25B6 Play"}
        </button>

        <button
          onClick={stepForward}
          disabled={isPlaying || currentStep >= steps.length - 1}
          style={{
            padding: "8px 16px",
            borderRadius: 8,
            border: `1px solid ${colors.border}`,
            background: colors.surface,
            color:
              isPlaying || currentStep >= steps.length - 1
                ? colors.textMuted
                : colors.text,
            cursor:
              isPlaying || currentStep >= steps.length - 1
                ? "not-allowed"
                : "pointer",
            fontFamily: "var(--font-mono)",
            fontSize: 13,
          }}
        >
          Step \u25B6\u258F
        </button>

        <button
          onClick={reset}
          style={{
            padding: "8px 16px",
            borderRadius: 8,
            border: `1px solid ${colors.border}`,
            background: colors.surface,
            color: colors.text,
            cursor: "pointer",
            fontFamily: "var(--font-mono)",
            fontSize: 13,
          }}
        >
          Reset
        </button>

        <label
          style={{
            display: "flex",
            alignItems: "center",
            gap: 6,
            fontFamily: "var(--font-mono)",
            fontSize: 13,
            color: colors.textMuted,
            cursor: "pointer",
            marginLeft: 4,
          }}
        >
          <input
            type="checkbox"
            checked={simulateError}
            onChange={(e) => setSimulateError(e.target.checked)}
            style={{ accentColor: colors.red }}
          />
          Simulate 529 error
        </label>
      </div>

      {/* Status Bar */}
      <div
        style={{
          display: "flex",
          gap: 16,
          marginBottom: 16,
          flexWrap: "wrap",
        }}
      >
        {/* Token Counter */}
        <div
          style={{
            padding: "8px 14px",
            borderRadius: 8,
            background: colors.surface,
            border: `1px solid ${colors.border}`,
            fontFamily: "var(--font-mono)",
            fontSize: 12,
            display: "flex",
            alignItems: "center",
            gap: 8,
          }}
        >
          <span style={{ color: colors.textMuted }}>Tokens:</span>
          <motion.span
            key={tokenCount}
            initial={{ y: -4, opacity: 0 }}
            animate={{ y: 0, opacity: 1 }}
            style={{ fontWeight: 700, color: colors.terracotta }}
          >
            {tokenCount.toLocaleString()}
          </motion.span>
        </div>

        {/* Watchdog Timer */}
        {currentStep >= 0 && (
          <div
            style={{
              padding: "8px 14px",
              borderRadius: 8,
              background: colors.watchdogBg,
              border: `1px solid ${isDark ? "rgba(96,165,250,0.3)" : "rgba(59,130,246,0.2)"}`,
              fontFamily: "var(--font-mono)",
              fontSize: 12,
              display: "flex",
              alignItems: "center",
              gap: 8,
            }}
          >
            <span style={{ color: colors.blue }}>Watchdog:</span>
            <span
              style={{
                fontWeight: 700,
                color: watchdogTime < 30 ? colors.amber : colors.blue,
              }}
            >
              {watchdogTime}s
            </span>
          </div>
        )}

        {/* Cache Savings */}
        {currentStep >= 4 &&
          steps[Math.min(currentStep, steps.length - 1)]?.highlight ===
            "cache" && (
            <motion.div
              initial={{ scale: 0.9, opacity: 0 }}
              animate={{ scale: 1, opacity: 1 }}
              style={{
                padding: "8px 14px",
                borderRadius: 8,
                background: colors.cacheBg,
                border: `1px solid ${isDark ? "rgba(34,197,94,0.3)" : "rgba(34,197,94,0.2)"}`,
                fontFamily: "var(--font-mono)",
                fontSize: 12,
                display: "flex",
                alignItems: "center",
                gap: 6,
                color: colors.green,
                fontWeight: 600,
              }}
            >
              $ Cache saved ~60K tokens
            </motion.div>
          )}

        {/* Step counter */}
        <div
          style={{
            padding: "8px 14px",
            borderRadius: 8,
            background: colors.surface,
            border: `1px solid ${colors.border}`,
            fontFamily: "var(--font-mono)",
            fontSize: 12,
            color: colors.textMuted,
            marginLeft: "auto",
          }}
        >
          {currentStep + 1} / {steps.length}
        </div>
      </div>

      {/* Sequence Diagram */}
      <div
        style={{
          border: `1px solid ${colors.border}`,
          borderRadius: 12,
          overflow: "hidden",
          background: colors.surface,
        }}
      >
        {/* Participant Headers */}
        <div
          style={{
            display: "flex",
            borderBottom: `1px solid ${colors.border}`,
            background: isDark ? "#252523" : "#eae9e1",
          }}
        >
          {participants.map((p) => (
            <div
              key={p.id}
              style={{
                flex: 1,
                padding: "10px 8px",
                textAlign: "center",
                fontFamily: "var(--font-mono)",
                fontSize: 12,
                fontWeight: 600,
                color: colors.text,
              }}
            >
              <span className="hidden sm:inline">{p.label}</span>
              <span className="sm:hidden">{p.short}</span>
            </div>
          ))}
        </div>

        {/* Lifelines + Steps */}
        <div style={{ position: "relative", minHeight: 60 }}>
          {/* Vertical lifelines */}
          <div
            style={{
              position: "absolute",
              top: 0,
              left: 0,
              right: 0,
              bottom: 0,
              display: "flex",
              pointerEvents: "none",
            }}
          >
            {participants.map((p) => (
              <div
                key={p.id}
                style={{
                  flex: 1,
                  display: "flex",
                  justifyContent: "center",
                }}
              >
                <div
                  style={{
                    width: 1,
                    height: "100%",
                    background: colors.border,
                    opacity: 0.4,
                  }}
                />
              </div>
            ))}
          </div>

          {/* Step rows */}
          <AnimatePresence>
            {steps.map((step, idx) => {
              if (idx > currentStep) return null;

              const fromX = participantX[step.from];
              const toX = participantX[step.to];
              const isSelf = fromX === toX;
              const leftCol = Math.min(fromX, toX);
              const rightCol = Math.max(fromX, toX);
              const goingRight = toX >= fromX;

              return (
                <motion.div
                  key={`${step.id}-${simulateError ? "e" : "n"}`}
                  initial={{ opacity: 0, y: 8 }}
                  animate={{ opacity: 1, y: 0 }}
                  transition={{ duration: 0.3 }}
                  style={{
                    position: "relative",
                    padding: "10px 12px",
                    borderBottom: `1px solid ${isDark ? "rgba(68,68,68,0.3)" : "rgba(194,192,182,0.3)"}`,
                    background:
                      idx === currentStep
                        ? highlightBg(step.highlight)
                        : "transparent",
                  }}
                >
                  {/* Arrow visualization */}
                  <div
                    style={{
                      display: "flex",
                      position: "relative",
                      height: 24,
                      marginBottom: 4,
                    }}
                  >
                    {participants.map((p, pIdx) => {
                      const isFrom = pIdx === fromX;
                      const isTo = pIdx === toX;
                      const isBetween =
                        !isSelf && pIdx > leftCol && pIdx < rightCol;
                      const isEndpoint = isFrom || isTo;

                      return (
                        <div
                          key={p.id}
                          style={{
                            flex: 1,
                            display: "flex",
                            alignItems: "center",
                            justifyContent: "center",
                            position: "relative",
                          }}
                        >
                          {/* Dot at endpoints */}
                          {isEndpoint && (
                            <motion.div
                              initial={
                                idx === currentStep ? { scale: 0 } : undefined
                              }
                              animate={{ scale: 1 }}
                              style={{
                                width: 8,
                                height: 8,
                                borderRadius: "50%",
                                background: highlightColor(step.highlight),
                                zIndex: 2,
                              }}
                            />
                          )}

                          {/* Self-arrow (loop) */}
                          {isSelf && isFrom && (
                            <motion.div
                              initial={
                                idx === currentStep
                                  ? { scaleX: 0 }
                                  : undefined
                              }
                              animate={{ scaleX: 1 }}
                              style={{
                                position: "absolute",
                                right: -8,
                                top: -2,
                                width: 28,
                                height: 28,
                                border: `2px solid ${highlightColor(step.highlight)}`,
                                borderRadius: "0 12px 12px 0",
                                borderLeft: "none",
                                transformOrigin: "left center",
                              }}
                            />
                          )}

                          {/* Line between endpoints */}
                          {isBetween && (
                            <motion.div
                              initial={
                                idx === currentStep
                                  ? { scaleX: 0 }
                                  : undefined
                              }
                              animate={{ scaleX: 1 }}
                              transition={{ duration: 0.3 }}
                              style={{
                                position: "absolute",
                                left: 0,
                                right: 0,
                                height: 2,
                                background: highlightColor(step.highlight),
                                transformOrigin: goingRight
                                  ? "left center"
                                  : "right center",
                              }}
                            />
                          )}

                          {/* Line from 'from' to next */}
                          {isFrom && !isSelf && (
                            <motion.div
                              initial={
                                idx === currentStep
                                  ? { scaleX: 0 }
                                  : undefined
                              }
                              animate={{ scaleX: 1 }}
                              transition={{ duration: 0.3 }}
                              style={{
                                position: "absolute",
                                [goingRight ? "left" : "right"]: "50%",
                                [goingRight ? "right" : "left"]: 0,
                                height: 2,
                                background: highlightColor(step.highlight),
                                transformOrigin: goingRight
                                  ? "left center"
                                  : "right center",
                              }}
                            />
                          )}

                          {/* Line to 'to' from previous */}
                          {isTo && !isSelf && (
                            <motion.div
                              initial={
                                idx === currentStep
                                  ? { scaleX: 0 }
                                  : undefined
                              }
                              animate={{ scaleX: 1 }}
                              transition={{ duration: 0.3 }}
                              style={{
                                position: "absolute",
                                [goingRight ? "right" : "left"]: "50%",
                                [goingRight ? "left" : "right"]: 0,
                                height: 2,
                                background: highlightColor(step.highlight),
                                transformOrigin: goingRight
                                  ? "right center"
                                  : "left center",
                              }}
                            />
                          )}

                          {/* Arrowhead at 'to' */}
                          {isTo && !isSelf && (
                            <motion.div
                              initial={
                                idx === currentStep
                                  ? { opacity: 0, scale: 0 }
                                  : undefined
                              }
                              animate={{ opacity: 1, scale: 1 }}
                              transition={{ delay: 0.2 }}
                              style={{
                                position: "absolute",
                                [goingRight ? "left" : "right"]: "calc(50% - 6px)",
                                width: 0,
                                height: 0,
                                borderTop: "5px solid transparent",
                                borderBottom: "5px solid transparent",
                                [goingRight
                                  ? "borderLeft"
                                  : "borderRight"]: `6px solid ${highlightColor(step.highlight)}`,
                                zIndex: 3,
                              }}
                            />
                          )}
                        </div>
                      );
                    })}
                  </div>

                  {/* Label */}
                  <div
                    style={{
                      textAlign: "center",
                      fontFamily: "var(--font-mono)",
                      fontSize: 12,
                      lineHeight: 1.4,
                    }}
                  >
                    <span
                      style={{
                        fontWeight: 600,
                        color:
                          idx === currentStep
                            ? highlightColor(step.highlight)
                            : colors.text,
                      }}
                    >
                      {step.label}
                    </span>
                    {step.sublabel && (
                      <span
                        style={{
                          display: "block",
                          fontSize: 11,
                          color: colors.textMuted,
                          marginTop: 1,
                        }}
                      >
                        {step.sublabel}
                      </span>
                    )}
                  </div>
                </motion.div>
              );
            })}
          </AnimatePresence>

          {/* Empty state */}
          {currentStep < 0 && (
            <div
              style={{
                padding: "32px 16px",
                textAlign: "center",
                fontFamily: "var(--font-mono)",
                fontSize: 13,
                color: colors.textMuted,
              }}
            >
              Press Play or Step to begin the API call sequence.
            </div>
          )}
        </div>
      </div>

      {/* Current Step Detail */}
      <AnimatePresence mode="wait">
        {currentStep >= 0 && currentStep < steps.length && (
          <motion.div
            key={currentStep}
            initial={{ opacity: 0, y: 6 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -6 }}
            transition={{ duration: 0.2 }}
            style={{
              marginTop: 16,
              padding: "14px 18px",
              borderRadius: 8,
              background: colors.surface,
              border: `1px solid ${colors.border}`,
              fontSize: 13,
              lineHeight: 1.6,
              color: colors.textMuted,
            }}
          >
            <div
              style={{
                fontFamily: "var(--font-mono)",
                fontSize: 12,
                fontWeight: 600,
                color: highlightColor(steps[currentStep].highlight),
                marginBottom: 4,
              }}
            >
              Step {currentStep + 1}: {steps[currentStep].label}
            </div>
            {steps[currentStep].description}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
