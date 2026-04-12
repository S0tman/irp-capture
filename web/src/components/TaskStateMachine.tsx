import { useState, useCallback, useEffect, useRef } from "react";
import { motion, AnimatePresence } from "framer-motion";

// --- Types ---

type TaskStatus =
  | "pending"
  | "running"
  | "completed"
  | "failed"
  | "killed";

type CommunicationPattern = "foreground" | "background" | "coordinator";

interface Transition {
  from: TaskStatus;
  to: TaskStatus;
  label: string;
  trigger: string;
  detail: string;
}

// --- Data ---

const transitions: Transition[] = [
  {
    from: "pending",
    to: "running",
    label: "execution starts",
    trigger: "Task registered and first execution begins",
    detail:
      "The brief state between registration and first execution. Task moves to running when the agent loop or shell process starts.",
  },
  {
    from: "running",
    to: "completed",
    label: "normal finish",
    trigger: "Agent finishes work successfully or shell exits 0",
    detail:
      "The task produced its result. Output written to disk file. notified flag flips to true when parent is told.",
  },
  {
    from: "running",
    to: "failed",
    label: "error",
    trigger: "Unhandled exception, API error, or tool failure",
    detail:
      "An error terminated execution. The error is captured in the task output file and reported via task-notification XML.",
  },
  {
    from: "running",
    to: "killed",
    label: "abort / user stop",
    trigger: "User presses ESC, coordinator calls TaskStop, or abort signal",
    detail:
      "Explicitly stopped. The abort controller fires, cleanup runs in the finally block. No result is produced.",
  },
];

const statusPositions: Record<TaskStatus, { x: number; y: number }> = {
  pending: { x: 80, y: 100 },
  running: { x: 280, y: 100 },
  completed: { x: 480, y: 40 },
  failed: { x: 480, y: 100 },
  killed: { x: 480, y: 160 },
};

const statusColors: Record<TaskStatus, string> = {
  pending: "#87867f",
  running: "#d97757",
  completed: "#4ade80",
  failed: "#ef4444",
  killed: "#f59e0b",
};

const communicationPatterns: Record<
  CommunicationPattern,
  { title: string; description: string; details: string[] }
> = {
  foreground: {
    title: "Foreground (Sync)",
    description:
      "Parent iterates runAgent() generator directly. Messages yield up the call stack.",
    details: [
      "Parent calls runAgent() and iterates the async generator",
      "Each message yields back to the parent immediately",
      "Shares parent's abort controller (ESC kills both)",
      "Can transition to background mid-execution via Promise.race",
      "No disk output needed -- messages flow through the generator chain",
    ],
  },
  background: {
    title: "Background (Async)",
    description:
      "Three channels: disk output files, task-notifications, and pending message queue.",
    details: [
      "Disk: every task writes to an outputFile (JSONL transcript)",
      "Notifications: XML <task-notification> injected into parent's conversation",
      "Queue: SendMessage targets a running agent via pendingMessages array",
      "Messages drained at tool-round boundaries (not mid-execution)",
      "notified flag prevents duplicate completion messages",
    ],
  },
  coordinator: {
    title: "Coordinator Mode",
    description:
      "Manager-worker hierarchy. Coordinator gets only 3 tools: Agent, SendMessage, TaskStop.",
    details: [
      "Coordinator thinks, plans, decomposes -- never touches code directly",
      "Workers get full tool set minus coordination tools",
      "4 phases: Research -> Synthesis -> Implementation -> Verification",
      '"Never delegate understanding" -- coordinator synthesizes research',
      "Scratchpad enables cross-worker knowledge sharing via filesystem",
    ],
  },
};

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

function isTerminalStatus(status: TaskStatus): boolean {
  return (
    status === "completed" || status === "failed" || status === "killed"
  );
}

// --- Component ---

interface Props {
  className?: string;
}

export default function TaskStateMachine({ className }: Props) {
  const isDark = useDarkMode();
  const [currentStatus, setCurrentStatus] = useState<TaskStatus>("pending");
  const [activeTransition, setActiveTransition] = useState<Transition | null>(
    null
  );
  const [selectedPattern, setSelectedPattern] =
    useState<CommunicationPattern>("foreground");
  const animatingRef = useRef(false);

  const colors = {
    text: isDark ? "#f5f4ed" : "#141413",
    textSecondary: "#87867f",
    cardBg: isDark ? "#1e1e1c" : "#ffffff",
    cardBorder: isDark ? "#333" : "#e8e6dc",
    terracotta: "#d97757",
    terracottaBg: isDark
      ? "rgba(217, 119, 87, 0.12)"
      : "rgba(217, 119, 87, 0.08)",
    surfaceBg: isDark ? "#141413" : "#f5f4ed",
    connectorLine: isDark ? "#444" : "#c2c0b6",
  };

  const performTransition = useCallback(
    (transition: Transition) => {
      if (animatingRef.current) return;
      if (transition.from !== currentStatus) return;

      animatingRef.current = true;
      setActiveTransition(transition);

      setTimeout(() => {
        setCurrentStatus(transition.to);
        setTimeout(() => {
          setActiveTransition(null);
          animatingRef.current = false;
        }, 300);
      }, 600);
    },
    [currentStatus]
  );

  const reset = useCallback(() => {
    setCurrentStatus("pending");
    setActiveTransition(null);
    animatingRef.current = false;
  }, []);

  const availableTransitions = transitions.filter(
    (t) => t.from === currentStatus
  );

  return (
    <div className={className} style={{ fontFamily: "var(--font-serif)" }}>
      {/* State Machine Diagram */}
      <div
        style={{
          padding: "20px",
          background: colors.cardBg,
          border: `1px solid ${colors.cardBorder}`,
          borderRadius: 12,
          marginBottom: 20,
        }}
      >
        {/* SVG State Diagram */}
        <svg
          viewBox="0 0 580 210"
          style={{ width: "100%", height: "auto", display: "block" }}
        >
          {/* Transition arrows */}
          {transitions.map((t) => {
            const from = statusPositions[t.from];
            const to = statusPositions[t.to];
            const isActive = activeTransition === t;
            const midX = (from.x + to.x) / 2;
            const midY = (from.y + to.y) / 2;
            const dx = to.x - from.x;
            const dy = to.y - from.y;
            const len = Math.sqrt(dx * dx + dy * dy);
            const offsetX = (dx / len) * 50;
            const offsetY = (dy / len) * 50;

            return (
              <g key={`${t.from}-${t.to}`}>
                <defs>
                  <marker
                    id={`arrow-${t.from}-${t.to}`}
                    markerWidth="8"
                    markerHeight="8"
                    refX="8"
                    refY="4"
                    orient="auto"
                  >
                    <path
                      d="M0,0 L8,4 L0,8"
                      fill={isActive ? colors.terracotta : colors.connectorLine}
                    />
                  </marker>
                </defs>
                <line
                  x1={from.x + offsetX}
                  y1={from.y + offsetY}
                  x2={to.x - offsetX}
                  y2={to.y - offsetY}
                  stroke={isActive ? colors.terracotta : colors.connectorLine}
                  strokeWidth={isActive ? 2.5 : 1.5}
                  markerEnd={`url(#arrow-${t.from}-${t.to})`}
                  style={{ transition: "stroke 0.3s, stroke-width 0.3s" }}
                />
                <text
                  x={midX}
                  y={midY - 10}
                  textAnchor="middle"
                  fill={isActive ? colors.terracotta : colors.textSecondary}
                  fontSize="10"
                  fontFamily="var(--font-mono)"
                  style={{ transition: "fill 0.3s" }}
                >
                  {t.label}
                </text>
              </g>
            );
          })}

          {/* State nodes */}
          {(Object.keys(statusPositions) as TaskStatus[]).map((status) => {
            const pos = statusPositions[status];
            const isCurrent = currentStatus === status;
            const isTarget = activeTransition?.to === status;
            const nodeColor = statusColors[status];
            const isTerminal = isTerminalStatus(status);

            return (
              <g key={status}>
                {/* Glow for current */}
                {isCurrent && (
                  <circle
                    cx={pos.x}
                    cy={pos.y}
                    r={38}
                    fill="none"
                    stroke={nodeColor}
                    strokeWidth="2"
                    opacity="0.3"
                  >
                    <animate
                      attributeName="r"
                      values="38;44;38"
                      dur="2s"
                      repeatCount="indefinite"
                    />
                    <animate
                      attributeName="opacity"
                      values="0.3;0.1;0.3"
                      dur="2s"
                      repeatCount="indefinite"
                    />
                  </circle>
                )}

                {/* Node circle */}
                <circle
                  cx={pos.x}
                  cy={pos.y}
                  r={32}
                  fill={
                    isCurrent
                      ? isDark
                        ? `${nodeColor}20`
                        : `${nodeColor}15`
                      : colors.cardBg
                  }
                  stroke={isCurrent || isTarget ? nodeColor : colors.cardBorder}
                  strokeWidth={isCurrent ? 2.5 : 1.5}
                  style={{ transition: "all 0.4s" }}
                />

                {/* Terminal state double circle */}
                {isTerminal && (
                  <circle
                    cx={pos.x}
                    cy={pos.y}
                    r={27}
                    fill="none"
                    stroke={
                      isCurrent || isTarget ? nodeColor : colors.cardBorder
                    }
                    strokeWidth={1}
                    opacity={0.5}
                    style={{ transition: "all 0.4s" }}
                  />
                )}

                <text
                  x={pos.x}
                  y={pos.y + 4}
                  textAnchor="middle"
                  fill={isCurrent ? nodeColor : colors.text}
                  fontSize="11"
                  fontWeight={isCurrent ? "700" : "500"}
                  fontFamily="var(--font-mono)"
                  style={{ transition: "fill 0.3s" }}
                >
                  {status}
                </text>
              </g>
            );
          })}
        </svg>
      </div>

      {/* Controls + Transition Info */}
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "1fr 1fr",
          gap: 16,
          marginBottom: 20,
        }}
      >
        {/* Transition buttons */}
        <div
          style={{
            padding: "16px 20px",
            background: colors.cardBg,
            border: `1px solid ${colors.cardBorder}`,
            borderRadius: 12,
          }}
        >
          <div
            style={{
              fontSize: 12,
              fontFamily: "var(--font-mono)",
              color: colors.textSecondary,
              marginBottom: 12,
              textTransform: "uppercase",
              letterSpacing: "0.05em",
            }}
          >
            Available Transitions
          </div>

          {availableTransitions.length > 0 ? (
            <div
              style={{
                display: "flex",
                flexDirection: "column",
                gap: 8,
              }}
            >
              {availableTransitions.map((t) => (
                <button
                  key={`${t.from}-${t.to}`}
                  onClick={() => performTransition(t)}
                  disabled={animatingRef.current}
                  style={{
                    padding: "10px 16px",
                    borderRadius: 8,
                    border: `1px solid ${statusColors[t.to]}40`,
                    background: `${statusColors[t.to]}10`,
                    color: statusColors[t.to],
                    fontSize: 13,
                    fontWeight: 600,
                    fontFamily: "var(--font-mono)",
                    cursor: "pointer",
                    textAlign: "left",
                    transition: "background 0.2s",
                  }}
                >
                  {t.label}
                  <span
                    style={{
                      float: "right",
                      opacity: 0.6,
                      fontSize: 11,
                    }}
                  >
                    {t.from} -&gt; {t.to}
                  </span>
                </button>
              ))}
            </div>
          ) : (
            <div style={{ fontSize: 13, color: colors.textSecondary }}>
              Terminal state reached.
              <button
                onClick={reset}
                style={{
                  display: "block",
                  marginTop: 10,
                  padding: "8px 16px",
                  borderRadius: 8,
                  border: "none",
                  background: colors.terracotta,
                  color: "#fff",
                  fontSize: 13,
                  fontWeight: 600,
                  fontFamily: "var(--font-mono)",
                  cursor: "pointer",
                }}
              >
                Reset to pending
              </button>
            </div>
          )}
        </div>

        {/* Transition detail */}
        <div
          style={{
            padding: "16px 20px",
            background: colors.cardBg,
            border: `1px solid ${colors.cardBorder}`,
            borderRadius: 12,
          }}
        >
          <div
            style={{
              fontSize: 12,
              fontFamily: "var(--font-mono)",
              color: colors.textSecondary,
              marginBottom: 12,
              textTransform: "uppercase",
              letterSpacing: "0.05em",
            }}
          >
            Transition Detail
          </div>

          <AnimatePresence mode="wait">
            {activeTransition ? (
              <motion.div
                key={`${activeTransition.from}-${activeTransition.to}`}
                initial={{ opacity: 0, y: 6 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, y: -6 }}
                transition={{ duration: 0.2 }}
              >
                <div
                  style={{
                    fontSize: 14,
                    fontWeight: 600,
                    fontFamily: "var(--font-mono)",
                    color: colors.terracotta,
                    marginBottom: 8,
                  }}
                >
                  {activeTransition.from} -&gt; {activeTransition.to}
                </div>
                <div
                  style={{
                    fontSize: 12,
                    fontWeight: 600,
                    color: colors.text,
                    marginBottom: 4,
                  }}
                >
                  Trigger:
                </div>
                <div
                  style={{
                    fontSize: 12,
                    color: colors.textSecondary,
                    marginBottom: 10,
                  }}
                >
                  {activeTransition.trigger}
                </div>
                <div
                  style={{
                    fontSize: 12,
                    color: colors.textSecondary,
                    lineHeight: 1.6,
                  }}
                >
                  {activeTransition.detail}
                </div>
              </motion.div>
            ) : (
              <motion.div
                key="idle"
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                exit={{ opacity: 0 }}
              >
                <div style={{ fontSize: 13, color: colors.textSecondary }}>
                  Click a transition to see details and animate the state change.
                </div>
                <div
                  style={{
                    marginTop: 10,
                    padding: "8px 12px",
                    borderRadius: 8,
                    background: colors.surfaceBg,
                    fontSize: 12,
                    fontFamily: "var(--font-mono)",
                    color: colors.textSecondary,
                  }}
                >
                  isTerminalTaskStatus({currentStatus}) ={" "}
                  <span
                    style={{
                      color: isTerminalStatus(currentStatus)
                        ? statusColors.completed
                        : colors.terracotta,
                      fontWeight: 600,
                    }}
                  >
                    {String(isTerminalStatus(currentStatus))}
                  </span>
                </div>
              </motion.div>
            )}
          </AnimatePresence>
        </div>
      </div>

      {/* Communication Patterns */}
      <div
        style={{
          padding: "16px 20px",
          background: colors.cardBg,
          border: `1px solid ${colors.cardBorder}`,
          borderRadius: 12,
        }}
      >
        <div
          style={{
            fontSize: 12,
            fontFamily: "var(--font-mono)",
            color: colors.textSecondary,
            marginBottom: 14,
            textTransform: "uppercase",
            letterSpacing: "0.05em",
          }}
        >
          Communication Patterns
        </div>

        {/* Tabs */}
        <div
          style={{
            display: "flex",
            gap: 4,
            marginBottom: 16,
            padding: 3,
            background: colors.surfaceBg,
            borderRadius: 8,
            border: `1px solid ${colors.cardBorder}`,
          }}
        >
          {(Object.keys(communicationPatterns) as CommunicationPattern[]).map(
            (pattern) => {
              const isActive = selectedPattern === pattern;
              return (
                <button
                  key={pattern}
                  onClick={() => setSelectedPattern(pattern)}
                  style={{
                    flex: 1,
                    padding: "8px 12px",
                    borderRadius: 6,
                    border: "none",
                    background: isActive ? colors.cardBg : "transparent",
                    color: isActive ? colors.terracotta : colors.textSecondary,
                    fontSize: 12,
                    fontWeight: isActive ? 600 : 400,
                    fontFamily: "var(--font-mono)",
                    cursor: "pointer",
                    transition: "all 0.2s",
                    boxShadow: isActive
                      ? isDark
                        ? "0 1px 3px rgba(0,0,0,0.3)"
                        : "0 1px 3px rgba(0,0,0,0.1)"
                      : "none",
                  }}
                >
                  {communicationPatterns[pattern].title}
                </button>
              );
            }
          )}
        </div>

        {/* Pattern content */}
        <AnimatePresence mode="wait">
          <motion.div
            key={selectedPattern}
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -8 }}
            transition={{ duration: 0.2 }}
          >
            <div
              style={{
                fontSize: 14,
                fontWeight: 600,
                color: colors.text,
                marginBottom: 8,
              }}
            >
              {communicationPatterns[selectedPattern].description}
            </div>
            <div
              style={{
                display: "flex",
                flexDirection: "column",
                gap: 6,
              }}
            >
              {communicationPatterns[selectedPattern].details.map(
                (detail, i) => (
                  <div
                    key={i}
                    style={{
                      display: "flex",
                      alignItems: "flex-start",
                      gap: 10,
                      padding: "8px 12px",
                      borderRadius: 8,
                      background: colors.surfaceBg,
                      border: `1px solid ${colors.cardBorder}`,
                    }}
                  >
                    <span
                      style={{
                        fontSize: 11,
                        fontFamily: "var(--font-mono)",
                        fontWeight: 700,
                        color: colors.terracotta,
                        minWidth: 18,
                        marginTop: 1,
                      }}
                    >
                      {i + 1}.
                    </span>
                    <span
                      style={{
                        fontSize: 12,
                        color: colors.text,
                        lineHeight: 1.5,
                      }}
                    >
                      {detail}
                    </span>
                  </div>
                )
              )}
            </div>
          </motion.div>
        </AnimatePresence>
      </div>
    </div>
  );
}
