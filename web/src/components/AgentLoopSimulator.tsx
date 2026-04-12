import { useState, useEffect, useRef, useCallback } from "react";
import { motion, AnimatePresence } from "framer-motion";

// --- Step Data ---

interface Step {
  id: number;
  title: string;
  shortTitle: string;
  description: string;
  visual: React.ReactNode;
}

function StepVisual({ children }: { children: React.ReactNode }) {
  return (
    <div
      style={{
        background: "rgba(217, 119, 87, 0.08)",
        borderRadius: 8,
        padding: "12px 16px",
        fontFamily: "var(--font-mono)",
        fontSize: 13,
        lineHeight: 1.6,
      }}
    >
      {children}
    </div>
  );
}

const steps: Step[] = [
  {
    id: 1,
    title: "Receive Input",
    shortTitle: "Input",
    description:
      "The loop begins when a user sends a message or when tool results from the previous iteration arrive. These become the new messages appended to the conversation history.",
    visual: (
      <StepVisual>
        <div style={{ opacity: 0.5, marginBottom: 4 }}>// new messages</div>
        <div>
          {"{"} role: <span style={{ color: "#d97757" }}>"user"</span>, content:{" "}
          <span style={{ color: "#d97757" }}>"Read the config file"</span> {"}"}
        </div>
      </StepVisual>
    ),
  },
  {
    id: 2,
    title: "Context Management",
    shortTitle: "Context",
    description:
      "Before calling the model, the system checks token usage. If the conversation is too long, compression layers kick in: tool result budgeting, snip compaction, or full summarization.",
    visual: (
      <StepVisual>
        <div>
          tokens used: <strong>142,800</strong> / 200,000
        </div>
        <div style={{ color: "#22c55e", marginTop: 4 }}>
          Under threshold (80%). No compression needed.
        </div>
      </StepVisual>
    ),
  },
  {
    id: 3,
    title: "Stream to Model",
    shortTitle: "Stream",
    description:
      "The full message array is sent to the Claude API. Response tokens stream back in real-time. The system processes each chunk as it arrives, building up text and tool_use blocks.",
    visual: (
      <StepVisual>
        <div style={{ opacity: 0.5 }}>// streaming response...</div>
        <div style={{ marginTop: 4 }}>
          <span style={{ color: "#87867f" }}>{">"}</span> Let me read that
          config file for you
          <span
            style={{
              display: "inline-block",
              width: 8,
              height: 16,
              background: "#d97757",
              marginLeft: 2,
              verticalAlign: "middle",
              animation: "blink 1s step-end infinite",
            }}
          />
        </div>
        <style>{`@keyframes blink { 50% { opacity: 0; } }`}</style>
      </StepVisual>
    ),
  },
  {
    id: 4,
    title: "Parse Response",
    shortTitle: "Parse",
    description:
      "Once streaming completes, the response is parsed into content blocks. Text blocks become visible output. tool_use blocks are extracted with their names, IDs, and input parameters.",
    visual: (
      <StepVisual>
        <div>
          blocks: [<br />
          &nbsp;&nbsp;{"{"} type:{" "}
          <span style={{ color: "#87867f" }}>"text"</span>, text: "Let me
          read..." {"}"},<br />
          &nbsp;&nbsp;{"{"} type:{" "}
          <span style={{ color: "#d97757" }}>"tool_use"</span>, name: "Read",
          <br />
          &nbsp;&nbsp;&nbsp;&nbsp;input: {"{"} file_path: "./config.ts" {"}"}{" "}
          {"}"}
          <br />]
        </div>
      </StepVisual>
    ),
  },
  {
    id: 5,
    title: "Execute Tools",
    shortTitle: "Tools",
    description:
      "Tool calls are executed through a 14-step pipeline. Read-only tools (Read, Glob, Grep) run in parallel. Write tools (Edit, Write, Bash) run serially. Each goes through permission checks.",
    visual: (
      <StepVisual>
        <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
          <div>
            <span
              style={{
                display: "inline-block",
                width: 8,
                height: 8,
                borderRadius: "50%",
                background: "#22c55e",
                marginRight: 8,
              }}
            />
            Read("./config.ts")
            <span style={{ color: "#22c55e", marginLeft: 8 }}>running</span>
          </div>
          <div>
            <span
              style={{
                display: "inline-block",
                width: 8,
                height: 8,
                borderRadius: "50%",
                background: "#22c55e",
                marginRight: 8,
              }}
            />
            Glob("**/*.json")
            <span style={{ color: "#22c55e", marginLeft: 8 }}>running</span>
          </div>
          <div style={{ opacity: 0.5, fontSize: 11, marginTop: 2 }}>
            read-only tools execute in parallel
          </div>
        </div>
      </StepVisual>
    ),
  },
  {
    id: 6,
    title: "Collect Results",
    shortTitle: "Results",
    description:
      "Tool outputs are gathered and formatted as tool_result messages. Large outputs are truncated to fit within the per-message token budget. Results are paired with their tool_use IDs.",
    visual: (
      <StepVisual>
        <div>
          {"{"} role:{" "}
          <span style={{ color: "#87867f" }}>"tool_result"</span>,
          <br />
          &nbsp;&nbsp;tool_use_id: "toolu_01X...",
          <br />
          &nbsp;&nbsp;content:{" "}
          <span style={{ color: "#d97757" }}>
            "export default {"{"} port: 3001... {"}"}"
          </span>
          <br />
          {"}"}
        </div>
      </StepVisual>
    ),
  },
  {
    id: 7,
    title: "Post-Processing",
    shortTitle: "Hooks",
    description:
      "PostToolUse hooks fire, allowing extensions to inspect or modify results. The system checks stop conditions: did a hook request early termination? Did the model hit a stop sequence?",
    visual: (
      <StepVisual>
        <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          <div>
            PostToolUse hooks:{" "}
            <span style={{ color: "#22c55e" }}>2 registered</span>
          </div>
          <div>
            &nbsp;&nbsp;block-any-type.ts{" "}
            <span style={{ color: "#22c55e" }}>passed</span>
          </div>
          <div>
            &nbsp;&nbsp;auto-compact.ts{" "}
            <span style={{ color: "#22c55e" }}>passed</span>
          </div>
          <div style={{ marginTop: 4 }}>
            Stop conditions:{" "}
            <span style={{ color: "#87867f" }}>none triggered</span>
          </div>
        </div>
      </StepVisual>
    ),
  },
  {
    id: 8,
    title: "Decision",
    shortTitle: "Decide",
    description:
      "The model examines its response. If there are pending tool calls, it loops back to step 1 with the tool results. If it has enough information, it produces a final text response and exits.",
    visual: null, // handled specially
  },
];

// --- Component ---

interface Props {
  className?: string;
}

export default function AgentLoopSimulator({ className }: Props) {
  const [currentStep, setCurrentStep] = useState(0); // 0 = not started, 1-8 = steps
  const [decision, setDecision] = useState<"loop" | "exit" | null>(null);
  const [iteration, setIteration] = useState(1);
  const [autoPlay, setAutoPlay] = useState(false);
  const [isDark, setIsDark] = useState(false);
  const autoPlayRef = useRef(autoPlay);
  autoPlayRef.current = autoPlay;

  // Dark mode detection
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

  // Auto-play
  useEffect(() => {
    if (!autoPlay) return;
    const interval = setInterval(() => {
      if (!autoPlayRef.current) return;
      setCurrentStep((prev) => {
        if (prev >= 8) {
          setAutoPlay(false);
          return prev;
        }
        return prev + 1;
      });
    }, 2000);
    return () => clearInterval(interval);
  }, [autoPlay]);

  const handleNext = useCallback(() => {
    if (currentStep < 8) {
      setCurrentStep((s) => s + 1);
      setDecision(null);
    }
  }, [currentStep]);

  const handlePrev = useCallback(() => {
    if (currentStep > 1) {
      setCurrentStep((s) => s - 1);
      setDecision(null);
    }
  }, [currentStep]);

  const handleReset = useCallback(() => {
    setCurrentStep(0);
    setDecision(null);
    setIteration(1);
    setAutoPlay(false);
  }, []);

  const handleDecision = useCallback(
    (d: "loop" | "exit") => {
      setDecision(d);
      if (d === "loop") {
        setTimeout(() => {
          setCurrentStep(1);
          setDecision(null);
          setIteration((i) => i + 1);
        }, 1500);
      }
    },
    [],
  );

  const colors = {
    active: "#d97757",
    completed: "rgba(217, 119, 87, 0.3)",
    upcoming: "#c2c0b6",
    text: isDark ? "#f5f4ed" : "#141413",
    textMuted: isDark ? "#87867f" : "#87867f",
    bg: isDark ? "#1e1e1c" : "#ffffff",
    border: isDark ? "#333" : "#c2c0b6",
    panelBg: isDark ? "rgba(30,30,28,0.5)" : "rgba(255,255,255,0.5)",
  };

  const getStepColor = (stepId: number) => {
    if (stepId === currentStep) return colors.active;
    if (stepId < currentStep) return colors.completed;
    return colors.upcoming;
  };

  const getStepTextColor = (stepId: number) => {
    if (stepId === currentStep) return colors.text;
    if (stepId < currentStep) return colors.active;
    return colors.textMuted;
  };

  const activeStep = steps.find((s) => s.id === currentStep);

  return (
    <div className={className} style={{ fontFamily: "var(--font-serif)" }}>
      {/* Iteration badge */}
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          marginBottom: 16,
        }}
      >
        <span
          style={{
            fontSize: 13,
            fontFamily: "var(--font-mono)",
            color: colors.textMuted,
          }}
        >
          Iteration {iteration}
        </span>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <label
            style={{
              display: "flex",
              alignItems: "center",
              gap: 6,
              fontSize: 13,
              color: colors.textMuted,
              cursor: "pointer",
              fontFamily: "var(--font-mono)",
            }}
          >
            <input
              type="checkbox"
              checked={autoPlay}
              onChange={(e) => {
                setAutoPlay(e.target.checked);
                if (e.target.checked && currentStep === 0) setCurrentStep(1);
              }}
              style={{ accentColor: colors.active }}
            />
            auto-play
          </label>
        </div>
      </div>

      {/* Pipeline */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 0,
          overflowX: "auto",
          padding: "8px 4px",
          marginBottom: 24,
        }}
      >
        {steps.map((step, i) => (
          <div
            key={step.id}
            style={{ display: "flex", alignItems: "center", flexShrink: 0 }}
          >
            {/* Node */}
            <div
              onClick={() => {
                setCurrentStep(step.id);
                setDecision(null);
              }}
              style={{
                display: "flex",
                flexDirection: "column",
                alignItems: "center",
                cursor: "pointer",
                gap: 6,
              }}
            >
              <motion.div
                animate={{
                  backgroundColor: getStepColor(step.id),
                  scale: step.id === currentStep ? 1.15 : 1,
                }}
                transition={{ duration: 0.3 }}
                style={{
                  width: 36,
                  height: 36,
                  borderRadius: "50%",
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  color: step.id <= currentStep ? "#fff" : colors.textMuted,
                  fontSize: 14,
                  fontWeight: 700,
                  fontFamily: "var(--font-mono)",
                }}
              >
                {step.id}
              </motion.div>
              <span
                style={{
                  fontSize: 10,
                  fontFamily: "var(--font-mono)",
                  color: getStepTextColor(step.id),
                  fontWeight: step.id === currentStep ? 600 : 400,
                  whiteSpace: "nowrap",
                  transition: "color 0.3s",
                }}
              >
                {step.shortTitle}
              </span>
            </div>
            {/* Connector */}
            {i < steps.length - 1 && (
              <div
                style={{
                  width: 24,
                  height: 2,
                  background:
                    step.id < currentStep ? colors.completed : colors.upcoming,
                  marginInline: 4,
                  marginBottom: 20,
                  transition: "background 0.3s",
                }}
              />
            )}
          </div>
        ))}
      </div>

      {/* Detail panel */}
      <AnimatePresence mode="wait">
        {currentStep === 0 ? (
          <motion.div
            key="start"
            initial={{ opacity: 0, y: 10 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -10 }}
            style={{
              border: `1px solid ${colors.border}`,
              borderRadius: 12,
              padding: "32px 24px",
              textAlign: "center",
              background: colors.panelBg,
            }}
          >
            <div
              style={{
                fontSize: 16,
                fontWeight: 600,
                color: colors.text,
                marginBottom: 8,
              }}
            >
              The Agent Loop
            </div>
            <div
              style={{
                fontSize: 14,
                color: colors.textMuted,
                marginBottom: 20,
                maxWidth: 480,
                marginInline: "auto",
                lineHeight: 1.6,
              }}
            >
              Step through one iteration of Claude Code's core loop. Each
              iteration receives input, calls the model, executes tools, and
              decides whether to continue or respond.
            </div>
            <button
              onClick={() => setCurrentStep(1)}
              style={{
                background: colors.active,
                color: "#fff",
                border: "none",
                borderRadius: 8,
                padding: "10px 24px",
                fontSize: 14,
                fontWeight: 600,
                cursor: "pointer",
                fontFamily: "var(--font-mono)",
              }}
            >
              Start Loop
            </button>
          </motion.div>
        ) : activeStep && currentStep !== 8 ? (
          <motion.div
            key={`step-${currentStep}`}
            initial={{ opacity: 0, y: 10 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -10 }}
            transition={{ duration: 0.25 }}
            style={{
              border: `1px solid ${colors.border}`,
              borderRadius: 12,
              padding: 24,
              background: colors.panelBg,
            }}
          >
            <div
              style={{
                fontSize: 11,
                fontFamily: "var(--font-mono)",
                color: colors.active,
                marginBottom: 6,
                textTransform: "uppercase",
                letterSpacing: "0.05em",
              }}
            >
              Step {activeStep.id} of 8
            </div>
            <div
              style={{
                fontSize: 18,
                fontWeight: 700,
                color: colors.text,
                marginBottom: 10,
              }}
            >
              {activeStep.title}
            </div>
            <div
              style={{
                fontSize: 14,
                color: colors.textMuted,
                lineHeight: 1.7,
                marginBottom: 16,
                maxWidth: 600,
              }}
            >
              {activeStep.description}
            </div>
            <div style={{ color: colors.text }}>{activeStep.visual}</div>
          </motion.div>
        ) : currentStep === 8 ? (
          <motion.div
            key="decision"
            initial={{ opacity: 0, y: 10 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -10 }}
            transition={{ duration: 0.25 }}
            style={{
              border: `1px solid ${colors.border}`,
              borderRadius: 12,
              padding: 24,
              background: colors.panelBg,
            }}
          >
            <div
              style={{
                fontSize: 11,
                fontFamily: "var(--font-mono)",
                color: colors.active,
                marginBottom: 6,
                textTransform: "uppercase",
                letterSpacing: "0.05em",
              }}
            >
              Step 8 of 8
            </div>
            <div
              style={{
                fontSize: 18,
                fontWeight: 700,
                color: colors.text,
                marginBottom: 10,
              }}
            >
              Decision
            </div>
            <div
              style={{
                fontSize: 14,
                color: colors.textMuted,
                lineHeight: 1.7,
                marginBottom: 20,
                maxWidth: 600,
              }}
            >
              {steps[7].description}
            </div>

            {decision === null ? (
              <div style={{ display: "flex", gap: 12, flexWrap: "wrap" }}>
                <button
                  onClick={() => handleDecision("loop")}
                  style={{
                    background: "transparent",
                    border: `2px solid ${colors.active}`,
                    color: colors.active,
                    borderRadius: 8,
                    padding: "10px 20px",
                    fontSize: 14,
                    fontWeight: 600,
                    cursor: "pointer",
                    fontFamily: "var(--font-mono)",
                    display: "flex",
                    alignItems: "center",
                    gap: 8,
                  }}
                >
                  <svg
                    width="16"
                    height="16"
                    viewBox="0 0 16 16"
                    fill="none"
                    style={{ flexShrink: 0 }}
                  >
                    <path
                      d="M2 8a6 6 0 0 1 10.47-4.03L10 6h5V1l-2.11 2.11A7.98 7.98 0 0 0 0 8h2z"
                      fill="currentColor"
                    />
                  </svg>
                  Loop back (more tool calls)
                </button>
                <button
                  onClick={() => handleDecision("exit")}
                  style={{
                    background: colors.active,
                    border: `2px solid ${colors.active}`,
                    color: "#fff",
                    borderRadius: 8,
                    padding: "10px 20px",
                    fontSize: 14,
                    fontWeight: 600,
                    cursor: "pointer",
                    fontFamily: "var(--font-mono)",
                    display: "flex",
                    alignItems: "center",
                    gap: 8,
                  }}
                >
                  <svg
                    width="16"
                    height="16"
                    viewBox="0 0 16 16"
                    fill="none"
                    style={{ flexShrink: 0 }}
                  >
                    <path
                      d="M3 8h8m0 0L7 4m4 4L7 12"
                      stroke="currentColor"
                      strokeWidth="2"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                    />
                  </svg>
                  Exit (respond to user)
                </button>
              </div>
            ) : (
              <motion.div
                initial={{ opacity: 0, scale: 0.95 }}
                animate={{ opacity: 1, scale: 1 }}
                style={{
                  padding: "16px 20px",
                  borderRadius: 8,
                  background:
                    decision === "loop"
                      ? "rgba(217, 119, 87, 0.1)"
                      : "rgba(34, 197, 94, 0.1)",
                  border: `1px solid ${decision === "loop" ? "rgba(217, 119, 87, 0.3)" : "rgba(34, 197, 94, 0.3)"}`,
                }}
              >
                <div
                  style={{
                    fontSize: 15,
                    fontWeight: 600,
                    color: colors.text,
                    marginBottom: 4,
                  }}
                >
                  {decision === "loop"
                    ? "Iteration complete -- looping back to step 1"
                    : "Iteration complete -- responding to user"}
                </div>
                <div
                  style={{
                    fontSize: 13,
                    color: colors.textMuted,
                  }}
                >
                  {decision === "loop"
                    ? `The model needs more information. Starting iteration ${iteration + 1}...`
                    : "The model has gathered enough information and is producing a final response."}
                </div>
                {decision === "exit" && (
                  <button
                    onClick={handleReset}
                    style={{
                      marginTop: 12,
                      background: "transparent",
                      border: `1px solid ${colors.border}`,
                      color: colors.textMuted,
                      borderRadius: 6,
                      padding: "6px 14px",
                      fontSize: 13,
                      cursor: "pointer",
                      fontFamily: "var(--font-mono)",
                    }}
                  >
                    Start over
                  </button>
                )}
              </motion.div>
            )}
          </motion.div>
        ) : null}
      </AnimatePresence>

      {/* Controls */}
      {currentStep > 0 && (
        <div
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            marginTop: 16,
          }}
        >
          <button
            onClick={handlePrev}
            disabled={currentStep <= 1}
            style={{
              background: "transparent",
              border: `1px solid ${colors.border}`,
              color: currentStep <= 1 ? colors.upcoming : colors.text,
              borderRadius: 6,
              padding: "8px 16px",
              fontSize: 13,
              cursor: currentStep <= 1 ? "default" : "pointer",
              fontFamily: "var(--font-mono)",
              opacity: currentStep <= 1 ? 0.4 : 1,
            }}
          >
            Previous
          </button>
          <button
            onClick={handleReset}
            style={{
              background: "transparent",
              border: "none",
              color: colors.textMuted,
              fontSize: 12,
              cursor: "pointer",
              fontFamily: "var(--font-mono)",
              textDecoration: "underline",
              textUnderlineOffset: 3,
            }}
          >
            Reset
          </button>
          <button
            onClick={handleNext}
            disabled={currentStep >= 8}
            style={{
              background:
                currentStep >= 8 ? colors.upcoming : colors.active,
              border: "none",
              color: "#fff",
              borderRadius: 6,
              padding: "8px 16px",
              fontSize: 13,
              cursor: currentStep >= 8 ? "default" : "pointer",
              fontFamily: "var(--font-mono)",
              fontWeight: 600,
              opacity: currentStep >= 8 ? 0.4 : 1,
            }}
          >
            Next Step
          </button>
        </div>
      )}
    </div>
  );
}
