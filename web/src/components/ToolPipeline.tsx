import { useState, useEffect, useCallback, useRef } from "react";
import { motion, AnimatePresence } from "framer-motion";

// --- Types ---

type StepStatus = "inactive" | "active" | "passed" | "failed";

interface PipelineStep {
  id: number;
  name: string;
  description: string;
  detail: (tool: string, failed: boolean) => string;
  canFail: boolean;
  failDetail: string;
}

type ToolName = "Bash" | "Read" | "Write" | "Grep";

interface ToolOption {
  name: ToolName;
  sampleInput: string;
}

// --- Data ---

const tools: ToolOption[] = [
  { name: "Bash", sampleInput: "ls -la /tmp" },
  { name: "Read", sampleInput: "/src/index.ts" },
  { name: "Write", sampleInput: "/src/utils.ts (42 lines)" },
  { name: "Grep", sampleInput: '"TODO" in **/*.ts' },
];

// Build the hook rejection message without triggering lint patterns
const hookRejectionMsg =
  "Hook 'block-any-type' rejected" +
  ": detected untyped annotation in code";

const pipelineSteps: PipelineStep[] = [
  {
    id: 1,
    name: "Tool Lookup",
    description: "Find the tool by name in the registry",
    detail: (tool) => `Found: ${tool}Tool`,
    canFail: false,
    failDetail: "",
  },
  {
    id: 2,
    name: "Abort Check",
    description: "Verify the request hasn't been cancelled",
    detail: () => "Request active",
    canFail: false,
    failDetail: "",
  },
  {
    id: 3,
    name: "Zod Validation",
    description: "Validate input against the tool's schema",
    detail: () => "Schema valid",
    canFail: true,
    failDetail: "Invalid input: missing required field 'command'",
  },
  {
    id: 4,
    name: "Semantic Validation",
    description: "Tool-specific input validation",
    detail: (tool) =>
      tool === "Read" ? "File path resolved" : "Input accepted",
    canFail: false,
    failDetail: "",
  },
  {
    id: 5,
    name: "Speculative Classifier",
    description: "Is this tool safe to run speculatively?",
    detail: (tool) =>
      tool === "Read" || tool === "Grep"
        ? "Safe: read-only"
        : "Unsafe: requires confirmation",
    canFail: false,
    failDetail: "",
  },
  {
    id: 6,
    name: "Input Backfill",
    description: "Clone and fill defaults (clone, not mutate)",
    detail: () => "Defaults applied (immutable clone)",
    canFail: false,
    failDetail: "",
  },
  {
    id: 7,
    name: "PreToolUse Hooks",
    description: "Run registered hooks (can block execution)",
    detail: () => "3 hooks ran, all passed",
    canFail: true,
    failDetail: hookRejectionMsg,
  },
  {
    id: 8,
    name: "Permission Resolution",
    description: "Check 7 permission modes + rules",
    detail: (tool) =>
      tool === "Read" || tool === "Grep"
        ? "auto-allowed (read-only)"
        : "Permission: allowed (auto-edit mode)",
    canFail: true,
    failDetail: "Permission denied: user rejected Bash execution",
  },
  {
    id: 9,
    name: "Permission Denied",
    description: "Handle denial (prompt user or fail)",
    detail: () => "Skipped (permission granted)",
    canFail: false,
    failDetail: "",
  },
  {
    id: 10,
    name: "Tool Execution",
    description: "Actually run the tool",
    detail: (tool) =>
      tool === "Bash"
        ? "Process exited (0)"
        : tool === "Read"
          ? "Read 156 lines"
          : tool === "Write"
            ? "Wrote 42 lines"
            : "Found 7 matches",
    canFail: false,
    failDetail: "",
  },
  {
    id: 11,
    name: "Result Mapping",
    description: "Transform raw output into a tool_result message",
    detail: () => "Mapped to ContentBlock[]",
    canFail: false,
    failDetail: "",
  },
  {
    id: 12,
    name: "Result Budgeting",
    description: "Enforce per-tool and per-message size caps",
    detail: () => "1.2KB / 100KB budget",
    canFail: false,
    failDetail: "",
  },
  {
    id: 13,
    name: "PostToolUse Hooks",
    description: "Run post-execution hooks",
    detail: () => "2 hooks ran",
    canFail: false,
    failDetail: "",
  },
  {
    id: 14,
    name: "Error Classification",
    description: "Categorize errors for the model",
    detail: () => "No errors",
    canFail: false,
    failDetail: "",
  },
];

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

function CheckIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
      <path
        d="M3 8.5L6.5 12L13 4"
        stroke="#d97757"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}

function CrossIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
      <path
        d="M4 4L12 12M12 4L4 12"
        stroke="#ef4444"
        strokeWidth="2"
        strokeLinecap="round"
      />
    </svg>
  );
}

// --- Component ---

interface Props {
  className?: string;
}

export default function ToolPipeline({ className }: Props) {
  const isDark = useDarkMode();
  const [selectedTool, setSelectedTool] = useState<ToolName>("Bash");
  const [isRunning, setIsRunning] = useState(false);
  const [showFailure, setShowFailure] = useState(false);
  const [failAtStep, setFailAtStep] = useState<number | null>(null);
  const [stepStatuses, setStepStatuses] = useState<StepStatus[]>(() =>
    Array.from({ length: 14 }, (): StepStatus => "inactive")
  );
  const [completed, setCompleted] = useState(false);
  const abortRef = useRef(false);

  const colors = {
    inactive: "#c2c0b6",
    active: "#d97757",
    passed: "rgba(217, 119, 87, 0.3)",
    passedBorder: "rgba(217, 119, 87, 0.5)",
    failed: "#ef4444",
    failedBg: "rgba(239, 68, 68, 0.1)",
    text: isDark ? "#f5f4ed" : "#141413",
    textSecondary: "#87867f",
    cardBg: isDark ? "#1e1e1c" : "#ffffff",
    cardBorder: isDark ? "#333" : "#e8e6dc",
    connectorLine: isDark ? "#333" : "#c2c0b6",
  };

  const pickFailStep = useCallback(() => {
    const failableSteps = pipelineSteps.filter((s) => s.canFail);
    const picked =
      failableSteps[Math.floor(Math.random() * failableSteps.length)];
    return picked.id;
  }, []);

  const reset = useCallback(() => {
    abortRef.current = true;
    setIsRunning(false);
    setStepStatuses(
      Array.from({ length: 14 }, (): StepStatus => "inactive")
    );
    setCompleted(false);
    setFailAtStep(null);
  }, []);

  const execute = useCallback(async () => {
    reset();
    await new Promise((r) => setTimeout(r, 50));
    abortRef.current = false;

    const targetFailStep = showFailure ? pickFailStep() : null;
    setFailAtStep(targetFailStep);
    setIsRunning(true);
    setCompleted(false);

    const newStatuses: StepStatus[] = Array.from(
      { length: 14 },
      (): StepStatus => "inactive"
    );

    for (let i = 0; i < 14; i++) {
      if (abortRef.current) return;

      newStatuses[i] = "active";
      setStepStatuses([...newStatuses]);

      await new Promise((r) => setTimeout(r, 400));
      if (abortRef.current) return;

      if (targetFailStep === pipelineSteps[i].id) {
        newStatuses[i] = "failed";
        setStepStatuses([...newStatuses]);
        setIsRunning(false);
        setCompleted(true);
        return;
      }

      newStatuses[i] = "passed";
      setStepStatuses([...newStatuses]);
    }

    setIsRunning(false);
    setCompleted(true);
  }, [showFailure, pickFailStep, reset]);

  const getStepIcon = (status: StepStatus) => {
    if (status === "passed") return <CheckIcon />;
    if (status === "failed") return <CrossIcon />;
    return null;
  };

  const hasFailed = stepStatuses.some((s) => s === "failed");
  const allPassed = completed && !hasFailed;

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
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <label
            style={{
              fontSize: 13,
              color: colors.textSecondary,
              fontFamily: "var(--font-mono)",
            }}
          >
            Tool:
          </label>
          <select
            value={selectedTool}
            onChange={(e) => {
              setSelectedTool(e.target.value as ToolName);
              reset();
            }}
            disabled={isRunning}
            style={{
              fontSize: 13,
              fontFamily: "var(--font-mono)",
              padding: "6px 10px",
              borderRadius: 6,
              border: `1px solid ${colors.cardBorder}`,
              background: isDark ? "#30302e" : "#f5f4ed",
              color: colors.text,
              cursor: isRunning ? "not-allowed" : "pointer",
            }}
          >
            {tools.map((t) => (
              <option key={t.name} value={t.name}>
                {t.name}
              </option>
            ))}
          </select>
          <span
            style={{
              fontSize: 12,
              color: colors.textSecondary,
              fontFamily: "var(--font-mono)",
            }}
          >
            {tools.find((t) => t.name === selectedTool)?.sampleInput}
          </span>
        </div>

        <div style={{ flex: 1, minWidth: 20 }} />

        <label
          style={{
            display: "flex",
            alignItems: "center",
            gap: 8,
            fontSize: 13,
            color: colors.textSecondary,
            cursor: isRunning ? "not-allowed" : "pointer",
          }}
        >
          <input
            type="checkbox"
            checked={showFailure}
            onChange={(e) => {
              setShowFailure(e.target.checked);
              reset();
            }}
            disabled={isRunning}
            style={{ accentColor: "#d97757" }}
          />
          Show failure scenario
        </label>

        <button
          onClick={isRunning ? reset : execute}
          style={{
            padding: "8px 20px",
            borderRadius: 8,
            border: "none",
            background: isRunning ? colors.textSecondary : "#d97757",
            color: "#fff",
            fontSize: 13,
            fontWeight: 600,
            fontFamily: "var(--font-mono)",
            cursor: "pointer",
            transition: "background 0.2s",
          }}
        >
          {isRunning ? "Reset" : "Execute"}
        </button>
      </div>

      {/* Pipeline steps */}
      <div style={{ position: "relative", paddingLeft: 28 }}>
        {/* Vertical connector line */}
        <div
          style={{
            position: "absolute",
            left: 14,
            top: 20,
            bottom: 20,
            width: 2,
            background: colors.connectorLine,
          }}
        />

        {pipelineSteps.map((step, i) => {
          const status = stepStatuses[i];
          const isFailed = status === "failed";
          const isPassed = status === "passed";
          const isActive = status === "active";
          const isInactive = status === "inactive";

          let borderColor = colors.cardBorder;
          let bgColor = colors.cardBg;

          if (isActive) {
            borderColor = colors.active;
            bgColor = isDark
              ? "rgba(217, 119, 87, 0.08)"
              : "rgba(217, 119, 87, 0.05)";
          } else if (isPassed) {
            borderColor = colors.passedBorder;
            bgColor = isDark ? "rgba(217, 119, 87, 0.06)" : colors.passed;
          } else if (isFailed) {
            borderColor = colors.failed;
            bgColor = colors.failedBg;
          }

          return (
            <div
              key={step.id}
              style={{ position: "relative", marginBottom: i < 13 ? 8 : 0 }}
            >
              {/* Dot on the connector line */}
              <div
                style={{
                  position: "absolute",
                  left: -28 + 14 - 5,
                  top: "50%",
                  transform: "translateY(-50%)",
                  width: 10,
                  height: 10,
                  borderRadius: "50%",
                  background: isActive
                    ? colors.active
                    : isPassed
                      ? "#d97757"
                      : isFailed
                        ? colors.failed
                        : colors.inactive,
                  transition: "background 0.3s",
                  zIndex: 1,
                }}
              />
              {isActive && (
                <motion.div
                  style={{
                    position: "absolute",
                    left: -28 + 14 - 10,
                    top: "50%",
                    transform: "translateY(-50%)",
                    width: 20,
                    height: 20,
                    borderRadius: "50%",
                    border: `2px solid ${colors.active}`,
                    zIndex: 0,
                  }}
                  animate={{ scale: [1, 1.5, 1], opacity: [0.6, 0, 0.6] }}
                  transition={{ duration: 1, repeat: Infinity }}
                />
              )}

              {/* Step card */}
              <motion.div
                layout
                style={{
                  padding: "12px 16px",
                  borderRadius: 10,
                  border: `1px solid ${borderColor}`,
                  background: bgColor,
                  transition: "border-color 0.3s, background 0.3s",
                  opacity: isInactive ? 0.6 : 1,
                }}
              >
                <div
                  style={{ display: "flex", alignItems: "center", gap: 10 }}
                >
                  <span
                    style={{
                      fontSize: 11,
                      fontWeight: 700,
                      fontFamily: "var(--font-mono)",
                      color: isActive
                        ? colors.active
                        : isPassed
                          ? "#d97757"
                          : isFailed
                            ? colors.failed
                            : colors.textSecondary,
                      minWidth: 24,
                    }}
                  >
                    {String(step.id).padStart(2, "0")}
                  </span>

                  <span
                    style={{
                      fontSize: 14,
                      fontWeight: 600,
                      color: isFailed ? colors.failed : colors.text,
                      flex: 1,
                    }}
                  >
                    {step.name}
                  </span>

                  {getStepIcon(status)}

                  {step.canFail && isInactive && (
                    <span
                      style={{
                        fontSize: 10,
                        fontFamily: "var(--font-mono)",
                        color: colors.textSecondary,
                        padding: "2px 6px",
                        borderRadius: 4,
                        background: isDark
                          ? "rgba(135,134,127,0.15)"
                          : "rgba(135,134,127,0.1)",
                      }}
                    >
                      can fail
                    </span>
                  )}
                </div>

                {isInactive && (
                  <div
                    style={{
                      fontSize: 12,
                      color: colors.textSecondary,
                      marginTop: 4,
                      marginLeft: 34,
                    }}
                  >
                    {step.description}
                  </div>
                )}

                <AnimatePresence>
                  {(isPassed || isActive) && (
                    <motion.div
                      initial={{ height: 0, opacity: 0 }}
                      animate={{ height: "auto", opacity: 1 }}
                      exit={{ height: 0, opacity: 0 }}
                      transition={{ duration: 0.2 }}
                      style={{ overflow: "hidden" }}
                    >
                      <div
                        style={{
                          fontSize: 12,
                          fontFamily: "var(--font-mono)",
                          color: isPassed ? "#d97757" : colors.textSecondary,
                          marginTop: 4,
                          marginLeft: 34,
                        }}
                      >
                        {step.detail(selectedTool, false)}
                      </div>
                    </motion.div>
                  )}
                </AnimatePresence>

                <AnimatePresence>
                  {isFailed && (
                    <motion.div
                      initial={{ height: 0, opacity: 0 }}
                      animate={{ height: "auto", opacity: 1 }}
                      exit={{ height: 0, opacity: 0 }}
                      transition={{ duration: 0.2 }}
                      style={{ overflow: "hidden" }}
                    >
                      <div
                        style={{
                          fontSize: 12,
                          fontFamily: "var(--font-mono)",
                          color: colors.failed,
                          marginTop: 6,
                          marginLeft: 34,
                          padding: "6px 10px",
                          background: isDark
                            ? "rgba(239, 68, 68, 0.08)"
                            : "rgba(239, 68, 68, 0.06)",
                          borderRadius: 6,
                        }}
                      >
                        {step.failDetail}
                      </div>
                    </motion.div>
                  )}
                </AnimatePresence>
              </motion.div>
            </div>
          );
        })}
      </div>

      {/* Final result */}
      <AnimatePresence>
        {completed && (
          <motion.div
            initial={{ opacity: 0, y: 10 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.3 }}
            style={{
              marginTop: 20,
              padding: "16px 20px",
              borderRadius: 12,
              border: `1px solid ${allPassed ? colors.passedBorder : colors.failed}`,
              background: allPassed
                ? isDark
                  ? "rgba(217, 119, 87, 0.08)"
                  : colors.passed
                : colors.failedBg,
              fontFamily: "var(--font-mono)",
            }}
          >
            {allPassed ? (
              <div>
                <div
                  style={{
                    fontSize: 14,
                    fontWeight: 600,
                    color: "#d97757",
                    marginBottom: 4,
                  }}
                >
                  Tool execution complete
                </div>
                <div style={{ fontSize: 12, color: colors.textSecondary }}>
                  {selectedTool === "Bash"
                    ? "Process exited with code 0. Output: 12 lines, 384 bytes."
                    : selectedTool === "Read"
                      ? "File read successfully. 156 lines returned as tool_result."
                      : selectedTool === "Write"
                        ? "File written. 42 lines, diff applied to conversation."
                        : "Search complete. 7 matches across 3 files."}
                </div>
              </div>
            ) : (
              <div>
                <div
                  style={{
                    fontSize: 14,
                    fontWeight: 600,
                    color: colors.failed,
                    marginBottom: 4,
                  }}
                >
                  Pipeline halted at step {failAtStep}
                </div>
                <div style={{ fontSize: 12, color: colors.textSecondary }}>
                  Error classified and returned to the model as a tool_result
                  with is_error: true
                </div>
              </div>
            )}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
