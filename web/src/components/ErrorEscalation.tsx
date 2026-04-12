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

// --- Data Types ---

interface EscalationStep {
  id: number;
  label: string;
  description: string;
  detail: string;
  successCriteria: string;
}

interface ErrorType {
  id: string;
  title: string;
  code: string;
  icon: string;
  steps: EscalationStep[];
}

// --- Error Type Data ---

const errorTypes: ErrorType[] = [
  {
    id: "prompt-too-long",
    title: "Prompt Too Long",
    code: "413",
    icon: "\u26A0",
    steps: [
      {
        id: 1,
        label: "Context Collapse Drain",
        description:
          "Drains staged context collapses -- removes verbose tool results and earlier conversation sections that were already marked for removal.",
        detail:
          "The context pipeline stages collapses proactively. This step just flushes them. Cheap and fast.",
        successCriteria: "Token count drops below model's context window",
      },
      {
        id: 2,
        label: "Reactive Compact",
        description:
          "Emergency summarization via a dedicated compact sub-agent. Rewrites the entire conversation into a condensed summary.",
        detail:
          "One-shot guard: hasAttemptedReactiveCompact prevents infinite loops. Fires once per error type, never again.",
        successCriteria:
          "Compaction succeeds and new token count fits the context window",
      },
      {
        id: 3,
        label: "Surface Error & Exit",
        description:
          "All recovery exhausted. The error is finally surfaced to the user and the loop terminates.",
        detail:
          'Returns Terminal { reason: "prompt_too_long" }. The withholding pattern ends here -- this is the first time the user sees the error.',
        successCriteria: "N/A -- terminal state",
      },
    ],
  },
  {
    id: "max-output-tokens",
    title: "Max Output Tokens",
    code: "max_tokens",
    icon: "\u2702",
    steps: [
      {
        id: 1,
        label: "8K \u2192 64K Escalation",
        description:
          "Default output cap is 8,000 tokens (p99 output is 4,911). When hit, escalate to 64K via maxOutputTokensOverride.",
        detail:
          "Only <1% of requests hit the 8K cap. The low default saves significant cost at fleet scale.",
        successCriteria: "Response completes within 64K tokens",
      },
      {
        id: 2,
        label: "Multi-Turn Recovery (\u00D73)",
        description:
          "Still hitting the cap at 64K. The model's partial response is kept, and a continuation request is sent. Up to 3 attempts.",
        detail:
          "maxOutputTokensRecoveryCount tracks attempts. Each continuation appends the partial output and asks the model to continue.",
        successCriteria:
          "Model finishes its response within 3 continuation attempts",
      },
      {
        id: 3,
        label: "Surface Error & Exit",
        description:
          "3 recovery attempts exhausted. The accumulated partial output is kept, but the loop exits.",
        detail:
          'Returns Terminal { reason: "completed" } with the partial output. The user sees what the model managed to produce.',
        successCriteria: "N/A -- terminal state",
      },
    ],
  },
  {
    id: "media-size",
    title: "Media / Size Errors",
    code: "media_error",
    icon: "\uD83D\uDDBC",
    steps: [
      {
        id: 1,
        label: "Retry Without Media",
        description:
          "Strips media attachments (images, PDFs) from the request and retries. Uses reactive compact to rebuild context without the oversized content.",
        detail:
          "Triggered by ImageSizeError, ImageResizeError, or similar. The one-shot hasAttemptedReactiveCompact guard applies here too.",
        successCriteria:
          "Request succeeds after media removal and context recompaction",
      },
      {
        id: 2,
        label: "Surface Error & Exit",
        description:
          "Media removal did not resolve the issue. The error is surfaced to the user.",
        detail:
          'Returns Terminal { reason: "image_error" }. Distinct terminal reason allows callers to show media-specific guidance.',
        successCriteria: "N/A -- terminal state",
      },
    ],
  },
];

// --- Step Status ---

type StepStatus = "idle" | "active" | "success" | "failure";

// --- Component ---

export default function ErrorEscalation({
  className = "",
}: {
  className?: string;
}) {
  const isDark = useDarkMode();
  const [selectedError, setSelectedError] = useState<string>("prompt-too-long");
  const [stepStatuses, setStepStatuses] = useState<Record<string, StepStatus>>(
    {}
  );
  const [recoveryStep, setRecoveryStep] = useState<number>(1);
  const [isAnimating, setIsAnimating] = useState(false);
  const [showWithholding, setShowWithholding] = useState(false);
  const animationRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const currentError = errorTypes.find((e) => e.id === selectedError)!;
  const maxSteps = currentError.steps.length;

  // Colors
  const colors = {
    bg: isDark ? "#1e1e1c" : "#ffffff",
    surface: isDark ? "#2a2a28" : "#f5f4ed",
    surfaceHover: isDark ? "#333331" : "#e8e6dc",
    text: isDark ? "#f5f4ed" : "#141413",
    textMuted: isDark ? "#87867f" : "#87867f",
    border: isDark ? "#444" : "#c2c0b6",
    terracotta: "#d97757",
    green: "#22c55e",
    red: "#ef4444",
    greenBg: isDark ? "rgba(34, 197, 94, 0.1)" : "rgba(34, 197, 94, 0.08)",
    redBg: isDark ? "rgba(239, 68, 68, 0.1)" : "rgba(239, 68, 68, 0.08)",
    terracottaBg: isDark
      ? "rgba(217, 119, 87, 0.15)"
      : "rgba(217, 119, 87, 0.1)",
    withholdBg: isDark
      ? "rgba(237, 161, 0, 0.12)"
      : "rgba(237, 161, 0, 0.08)",
  };

  const resetAnimation = useCallback(() => {
    if (animationRef.current) clearTimeout(animationRef.current);
    setStepStatuses({});
    setIsAnimating(false);
    setShowWithholding(false);
  }, []);

  // Reset when error type changes
  useEffect(() => {
    resetAnimation();
  }, [selectedError, resetAnimation]);

  const triggerError = useCallback(() => {
    if (isAnimating) return;
    resetAnimation();
    setIsAnimating(true);
    setShowWithholding(true);

    const steps = currentError.steps;
    let delay = 600;

    // Animate through steps
    for (let i = 0; i < steps.length; i++) {
      const step = steps[i];
      const stepKey = `${currentError.id}-${step.id}`;
      const isRecoveryPoint = i + 1 === recoveryStep;
      const isLastStep = i === steps.length - 1;

      // Mark active
      const activateDelay = delay;
      animationRef.current = setTimeout(() => {
        setStepStatuses((prev) => ({ ...prev, [stepKey]: "active" }));
      }, activateDelay);
      delay += 1200;

      // Mark result
      const resultDelay = delay;
      if (isRecoveryPoint && !isLastStep) {
        // Recovery succeeds here
        animationRef.current = setTimeout(() => {
          setStepStatuses((prev) => ({ ...prev, [stepKey]: "success" }));
          setShowWithholding(false);
          setTimeout(() => setIsAnimating(false), 400);
        }, resultDelay);
        break;
      } else if (isLastStep) {
        // Terminal failure
        animationRef.current = setTimeout(() => {
          setStepStatuses((prev) => ({ ...prev, [stepKey]: "failure" }));
          setShowWithholding(false);
          setTimeout(() => setIsAnimating(false), 400);
        }, resultDelay);
      } else {
        // Step fails, escalate
        animationRef.current = setTimeout(() => {
          setStepStatuses((prev) => ({ ...prev, [stepKey]: "failure" }));
        }, resultDelay);
        delay += 600;
      }
    }
  }, [isAnimating, currentError, recoveryStep, resetAnimation]);

  const getStepStatus = (stepId: number): StepStatus => {
    return stepStatuses[`${currentError.id}-${stepId}`] || "idle";
  };

  const statusIcon = (status: StepStatus) => {
    switch (status) {
      case "success":
        return "\u2713";
      case "failure":
        return "\u2717";
      case "active":
        return "\u25CF";
      default:
        return null;
    }
  };

  const statusColor = (status: StepStatus) => {
    switch (status) {
      case "success":
        return colors.green;
      case "failure":
        return colors.red;
      case "active":
        return colors.terracotta;
      default:
        return colors.border;
    }
  };

  const statusBg = (status: StepStatus) => {
    switch (status) {
      case "success":
        return colors.greenBg;
      case "failure":
        return colors.redBg;
      case "active":
        return colors.terracottaBg;
      default:
        return "transparent";
    }
  };

  return (
    <div
      className={className}
      style={{
        fontFamily: "var(--font-serif)",
        color: colors.text,
        maxWidth: 820,
        margin: "0 auto",
      }}
    >
      {/* Header */}
      <div style={{ marginBottom: 24 }}>
        <h3
          style={{
            fontFamily: "var(--font-serif)",
            fontSize: 20,
            fontWeight: 600,
            margin: "0 0 6px 0",
            color: colors.text,
          }}
        >
          Error Recovery Escalation Ladder
        </h3>
        <p
          style={{
            fontSize: 14,
            color: colors.textMuted,
            margin: 0,
            lineHeight: 1.5,
          }}
        >
          Errors are{" "}
          <strong style={{ color: colors.terracotta }}>
            withheld from the stream
          </strong>{" "}
          while recovery attempts happen silently. The user only sees an error if
          all steps fail.
        </p>
      </div>

      {/* Error Type Tabs */}
      <div
        style={{
          display: "flex",
          gap: 8,
          marginBottom: 20,
          flexWrap: "wrap",
        }}
      >
        {errorTypes.map((err) => {
          const isSelected = selectedError === err.id;
          return (
            <button
              key={err.id}
              onClick={() => setSelectedError(err.id)}
              style={{
                padding: "8px 16px",
                borderRadius: 8,
                border: `1px solid ${isSelected ? colors.terracotta : colors.border}`,
                background: isSelected ? colors.terracottaBg : colors.surface,
                color: isSelected ? colors.terracotta : colors.text,
                cursor: "pointer",
                fontFamily: "var(--font-mono)",
                fontSize: 13,
                fontWeight: isSelected ? 600 : 400,
                transition: "all 0.15s ease",
              }}
            >
              <span style={{ marginRight: 6 }}>{err.icon}</span>
              {err.title}
              <span
                style={{
                  marginLeft: 8,
                  fontSize: 11,
                  opacity: 0.6,
                }}
              >
                {err.code}
              </span>
            </button>
          );
        })}
      </div>

      {/* Controls Row */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 16,
          marginBottom: 20,
          flexWrap: "wrap",
        }}
      >
        <button
          onClick={triggerError}
          disabled={isAnimating}
          style={{
            padding: "8px 20px",
            borderRadius: 8,
            border: "none",
            background: isAnimating ? colors.textMuted : colors.terracotta,
            color: "#fff",
            cursor: isAnimating ? "not-allowed" : "pointer",
            fontFamily: "var(--font-mono)",
            fontSize: 13,
            fontWeight: 600,
            transition: "all 0.15s ease",
          }}
        >
          {isAnimating ? "Recovering..." : "Trigger Error"}
        </button>

        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 8,
            fontSize: 13,
            color: colors.textMuted,
          }}
        >
          <label
            style={{
              fontFamily: "var(--font-mono)",
              whiteSpace: "nowrap",
            }}
          >
            Recovery succeeds at step:
          </label>
          <div style={{ display: "flex", gap: 4 }}>
            {currentError.steps.map((step, i) => {
              const isTerminal = i === currentError.steps.length - 1;
              return (
                <button
                  key={step.id}
                  onClick={() => {
                    if (!isAnimating) {
                      setRecoveryStep(step.id);
                      resetAnimation();
                    }
                  }}
                  disabled={isAnimating}
                  style={{
                    width: 32,
                    height: 32,
                    borderRadius: 6,
                    border: `1px solid ${recoveryStep === step.id ? colors.terracotta : colors.border}`,
                    background:
                      recoveryStep === step.id
                        ? colors.terracottaBg
                        : colors.surface,
                    color:
                      recoveryStep === step.id
                        ? colors.terracotta
                        : isTerminal
                          ? colors.red
                          : colors.text,
                    cursor: isAnimating ? "not-allowed" : "pointer",
                    fontFamily: "var(--font-mono)",
                    fontSize: 13,
                    fontWeight: recoveryStep === step.id ? 700 : 400,
                  }}
                  title={
                    isTerminal
                      ? "All recovery fails"
                      : `Recovery succeeds at step ${step.id}`
                  }
                >
                  {isTerminal ? "\u2717" : step.id}
                </button>
              );
            })}
          </div>
        </div>
      </div>

      {/* Withholding Banner */}
      <AnimatePresence>
        {showWithholding && (
          <motion.div
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: "auto" }}
            exit={{ opacity: 0, height: 0 }}
            style={{
              background: colors.withholdBg,
              border: `1px solid rgba(237, 161, 0, 0.3)`,
              borderRadius: 8,
              padding: "10px 16px",
              marginBottom: 16,
              fontFamily: "var(--font-mono)",
              fontSize: 12,
              color: "#eda100",
              display: "flex",
              alignItems: "center",
              gap: 8,
            }}
          >
            <motion.span
              animate={{ opacity: [1, 0.4, 1] }}
              transition={{ duration: 1.5, repeat: Infinity }}
            >
              \u25CF
            </motion.span>
            <span>
              Error withheld from stream -- recovery in progress. User sees
              nothing.
            </span>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Escalation Ladder */}
      <div
        style={{
          border: `1px solid ${colors.border}`,
          borderRadius: 12,
          overflow: "hidden",
          background: colors.surface,
        }}
      >
        <AnimatePresence mode="wait">
          <motion.div
            key={currentError.id}
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -8 }}
            transition={{ duration: 0.2 }}
          >
            {currentError.steps.map((step, idx) => {
              const status = getStepStatus(step.id);
              const isLast = idx === currentError.steps.length - 1;

              return (
                <div key={step.id}>
                  <motion.div
                    animate={{
                      backgroundColor: statusBg(status),
                    }}
                    transition={{ duration: 0.3 }}
                    style={{
                      padding: "16px 20px",
                      position: "relative",
                    }}
                  >
                    {/* Step Header */}
                    <div
                      style={{
                        display: "flex",
                        alignItems: "flex-start",
                        gap: 12,
                      }}
                    >
                      {/* Step Number / Status */}
                      <motion.div
                        animate={{
                          borderColor: statusColor(status),
                          color: statusColor(status),
                        }}
                        transition={{ duration: 0.3 }}
                        style={{
                          width: 32,
                          height: 32,
                          borderRadius: "50%",
                          border: `2px solid ${statusColor(status)}`,
                          display: "flex",
                          alignItems: "center",
                          justifyContent: "center",
                          fontFamily: "var(--font-mono)",
                          fontSize: 14,
                          fontWeight: 700,
                          flexShrink: 0,
                        }}
                      >
                        {status === "active" ? (
                          <motion.span
                            animate={{ scale: [1, 1.3, 1] }}
                            transition={{
                              duration: 0.8,
                              repeat: Infinity,
                            }}
                          >
                            {statusIcon(status)}
                          </motion.span>
                        ) : statusIcon(status) ? (
                          <motion.span
                            initial={{ scale: 0 }}
                            animate={{ scale: 1 }}
                            transition={{
                              type: "spring",
                              stiffness: 300,
                              damping: 15,
                            }}
                          >
                            {statusIcon(status)}
                          </motion.span>
                        ) : (
                          step.id
                        )}
                      </motion.div>

                      {/* Step Content */}
                      <div style={{ flex: 1, minWidth: 0 }}>
                        <div
                          style={{
                            display: "flex",
                            alignItems: "baseline",
                            gap: 8,
                            marginBottom: 4,
                          }}
                        >
                          <span
                            style={{
                              fontFamily: "var(--font-mono)",
                              fontSize: 14,
                              fontWeight: 600,
                              color:
                                status !== "idle"
                                  ? statusColor(status)
                                  : colors.text,
                            }}
                          >
                            {step.label}
                          </span>
                          {status === "success" && (
                            <motion.span
                              initial={{ opacity: 0, x: -8 }}
                              animate={{ opacity: 1, x: 0 }}
                              style={{
                                fontFamily: "var(--font-mono)",
                                fontSize: 12,
                                color: colors.green,
                                fontWeight: 600,
                              }}
                            >
                              Recovered!
                            </motion.span>
                          )}
                          {status === "failure" && isLast && (
                            <motion.span
                              initial={{ opacity: 0, x: -8 }}
                              animate={{ opacity: 1, x: 0 }}
                              style={{
                                fontFamily: "var(--font-mono)",
                                fontSize: 12,
                                color: colors.red,
                                fontWeight: 600,
                              }}
                            >
                              All recovery exhausted
                            </motion.span>
                          )}
                        </div>
                        <p
                          style={{
                            fontSize: 13,
                            color: colors.textMuted,
                            margin: "0 0 6px 0",
                            lineHeight: 1.5,
                          }}
                        >
                          {step.description}
                        </p>
                        <div
                          style={{
                            fontFamily: "var(--font-mono)",
                            fontSize: 11,
                            color: colors.textMuted,
                            opacity: 0.8,
                            lineHeight: 1.5,
                          }}
                        >
                          {step.detail}
                        </div>

                        {/* Success criteria */}
                        <div
                          style={{
                            marginTop: 8,
                            fontSize: 12,
                            fontFamily: "var(--font-mono)",
                            color:
                              status === "success"
                                ? colors.green
                                : colors.textMuted,
                            opacity: status === "idle" ? 0.5 : 0.9,
                          }}
                        >
                          <span style={{ opacity: 0.6 }}>\u2192 </span>
                          {step.successCriteria}
                        </div>
                      </div>
                    </div>
                  </motion.div>

                  {/* Escalation Arrow */}
                  {!isLast && (
                    <div
                      style={{
                        display: "flex",
                        alignItems: "center",
                        paddingLeft: 34,
                        height: 28,
                        position: "relative",
                      }}
                    >
                      <motion.div
                        animate={{
                          opacity:
                            getStepStatus(step.id) === "failure" ? 1 : 0.3,
                          color:
                            getStepStatus(step.id) === "failure"
                              ? colors.red
                              : colors.border,
                        }}
                        transition={{ duration: 0.3 }}
                        style={{
                          fontFamily: "var(--font-mono)",
                          fontSize: 12,
                          display: "flex",
                          alignItems: "center",
                          gap: 8,
                        }}
                      >
                        <span style={{ fontSize: 16 }}>\u2193</span>
                        <span>
                          {getStepStatus(step.id) === "failure"
                            ? "Failed \u2014 escalating..."
                            : "escalates to"}
                        </span>
                      </motion.div>
                    </div>
                  )}
                </div>
              );
            })}
          </motion.div>
        </AnimatePresence>
      </div>

      {/* Death Spiral Guards */}
      <div
        style={{
          marginTop: 16,
          padding: "12px 16px",
          borderRadius: 8,
          background: colors.surface,
          border: `1px solid ${colors.border}`,
          fontSize: 12,
          fontFamily: "var(--font-mono)",
          color: colors.textMuted,
          lineHeight: 1.6,
        }}
      >
        <div style={{ fontWeight: 600, marginBottom: 6, color: colors.text }}>
          Death Spiral Guards
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          <span>
            \u2022 <code>hasAttemptedReactiveCompact</code> -- one-shot flag,
            fires once per error
          </span>
          <span>
            \u2022 <code>MAX_OUTPUT_TOKENS_RECOVERY_LIMIT = 3</code> -- hard
            cap on continuations
          </span>
          <span>
            \u2022 Circuit breaker on auto-compact after 3 consecutive failures
          </span>
          <span>
            \u2022 No stop hooks on error responses (prevents error \u2192 hook
            \u2192 retry \u2192 error loops)
          </span>
        </div>
      </div>
    </div>
  );
}
