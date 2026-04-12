import { useState, useCallback, useEffect, useRef } from "react";
import { motion, AnimatePresence } from "framer-motion";

// --- Types ---

interface ComponentNode {
  id: string;
  name: string;
  description: string;
  lines: string;
  keyProps: string[];
  reRenderTriggers: string[];
  isHotPath: boolean;
  children?: ComponentNode[];
}

// --- Data ---

const componentTree: ComponentNode = {
  id: "repl",
  name: "REPL",
  description:
    "Root orchestrator of the entire interactive experience. ~9 sections: imports, feature flags, state management, QueryGuard, message handling, tool permission flow, session management, keybinding setup, and render tree. Compiled by React Compiler throughout.",
  lines: "~5,000",
  keyProps: ["bootstrapState", "commands", "history", "sessionId"],
  reRenderTriggers: [
    "Message stream tokens",
    "Tool use status changes",
    "Permission dialog open/close",
    "Input mode changes",
  ],
  isHotPath: true,
  children: [
    {
      id: "message-list",
      name: "VirtualMessageList",
      description:
        "Renders conversation messages with virtual scrolling. Only mounts messages visible in the viewport plus a buffer. Height cache per message, invalidated on terminal column change. Jump handle for search navigation.",
      lines: "~800",
      keyProps: ["messages", "scrollTop", "viewportHeight", "searchQuery"],
      reRenderTriggers: [
        "New message added",
        "Scroll position change",
        "Search highlight update",
      ],
      isHotPath: true,
      children: [
        {
          id: "user-message",
          name: "UserMessage",
          description:
            "User input blocks. Wrapped in MessageRow. Includes the prompt text and any attached images.",
          lines: "~150",
          keyProps: ["content", "images", "index"],
          reRenderTriggers: ["Mount only (static content)"],
          isHotPath: false,
        },
        {
          id: "assistant-message",
          name: "StreamingMarkdown",
          description:
            "Model output with streaming markdown. Token caching via module-level LRU (500 entries). Fast-path detection bypasses GFM parser for plain text. Lazy syntax highlighting via React Suspense.",
          lines: "~400",
          keyProps: ["content", "isStreaming", "highlight"],
          reRenderTriggers: [
            "Every new token (10-50/sec)",
            "Syntax highlight resolve",
          ],
          isHotPath: true,
        },
        {
          id: "tool-result",
          name: "ToolUseBlock",
          description:
            "Tool execution results. Shows tool name, status (running/done/error), and collapsible output. Includes elapsed time counter while running.",
          lines: "~300",
          keyProps: ["toolName", "status", "result", "elapsed"],
          reRenderTriggers: [
            "Status change (running->done)",
            "Elapsed time tick",
          ],
          isHotPath: false,
        },
        {
          id: "offscreen-freeze",
          name: "OffscreenFreeze",
          description:
            "Performance optimization: caches React element and freezes subtree when message scrolls above viewport. Prevents timer-based updates (spinners, elapsed counters) in off-screen messages from triggering terminal resets.",
          lines: "~60",
          keyProps: ["isVisible", "children"],
          reRenderTriggers: ["Visibility change only"],
          isHotPath: false,
        },
      ],
    },
    {
      id: "input-area",
      name: "PromptInput",
      description:
        "Text input with keybinding support, vim mode, and autocomplete. Manages insert/normal mode state, cursor position, and multi-line editing.",
      lines: "~600",
      keyProps: ["mode", "value", "cursorPosition", "vimState"],
      reRenderTriggers: ["Every keystroke", "Mode change (insert/normal/vim)"],
      isHotPath: true,
      children: [
        {
          id: "prompt-line",
          name: "PromptLine",
          description:
            'The ">" prompt with mode indicator. Shows current mode (insert/normal/vim), pending chord prefix, and model name.',
          lines: "~80",
          keyProps: ["mode", "pendingChord", "modelName"],
          reRenderTriggers: ["Mode change", "Chord state change"],
          isHotPath: false,
        },
        {
          id: "multi-line-editor",
          name: "MultiLineEditor",
          description:
            "Text editor component handling multi-line input. Cursor declaration via useDeclaredCursor for IME/CJK support. Word wrap with grapheme boundary awareness.",
          lines: "~350",
          keyProps: ["value", "cursor", "selection", "wrap"],
          reRenderTriggers: ["Every keystroke", "Selection change"],
          isHotPath: true,
        },
      ],
    },
    {
      id: "status-bar",
      name: "StatusLine",
      description:
        "Bottom bar with model name, cumulative cost, token count, and background task indicators. Updates on every API response with new token/cost data.",
      lines: "~120",
      keyProps: ["model", "cost", "tokens", "activeTasks"],
      reRenderTriggers: ["API response (cost/token update)", "Task status change"],
      isHotPath: false,
    },
    {
      id: "permission-prompt",
      name: "PermissionRequest",
      description:
        "Modal dialog for tool permission approval. Shows tool name, description, suggested permissions. Handles y/n/a (allow once/deny/always allow) keybindings via Confirmation context.",
      lines: "~250",
      keyProps: ["toolName", "description", "suggestions", "onAllow", "onDeny"],
      reRenderTriggers: ["New permission request"],
      isHotPath: false,
    },
    {
      id: "keybinding-setup",
      name: "KeybindingSetup",
      description:
        "Wires keybinding providers: GlobalKeybindingHandlers, CommandKeybindingHandlers, CancelRequestHandler. Manages context registration and chord interceptor.",
      lines: "~200",
      keyProps: ["bindings", "contexts", "handlers"],
      reRenderTriggers: ["Context activation/deactivation"],
      isHotPath: false,
    },
    {
      id: "logo-header",
      name: "LogoHeader",
      description:
        "Session header with Claude branding, model info, and session ID. Rendered once at the top of the message list.",
      lines: "~40",
      keyProps: ["sessionId", "model"],
      reRenderTriggers: ["Mount only"],
      isHotPath: false,
    },
  ],
};

const dataFlowSteps = [
  { from: "input-area", label: "User types and presses Enter" },
  { from: "repl", label: "REPL calls query() with message" },
  { from: "assistant-message", label: "Tokens stream into StreamingMarkdown" },
  { from: "tool-result", label: "Tool use blocks appear for tool calls" },
  { from: "status-bar", label: "StatusLine updates cost/token counts" },
  { from: "message-list", label: "VirtualMessageList scrolls to bottom" },
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

function flattenTree(node: ComponentNode): ComponentNode[] {
  const result: ComponentNode[] = [node];
  if (node.children) {
    for (const child of node.children) {
      result.push(...flattenTree(child));
    }
  }
  return result;
}

// --- Component ---

interface Props {
  className?: string;
}

export default function REPLComponentTree({ className }: Props) {
  const isDark = useDarkMode();
  const [expandedIds, setExpandedIds] = useState<Set<string>>(
    new Set(["repl", "message-list", "input-area"])
  );
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [showDataFlow, setShowDataFlow] = useState(false);
  const [dataFlowStep, setDataFlowStep] = useState(-1);
  const dataFlowTimerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const colors = {
    text: isDark ? "#f5f4ed" : "#141413",
    textSecondary: "#87867f",
    cardBg: isDark ? "#1e1e1c" : "#ffffff",
    cardBorder: isDark ? "#333" : "#e8e6dc",
    terracotta: "#d97757",
    terracottaBg: isDark
      ? "rgba(217, 119, 87, 0.15)"
      : "rgba(217, 119, 87, 0.08)",
    surfaceBg: isDark ? "#141413" : "#f5f4ed",
    hotPath: isDark ? "rgba(237, 161, 0, 0.15)" : "rgba(237, 161, 0, 0.08)",
    hotPathBorder: "#eda100",
    treeLine: isDark ? "#444" : "#d4d2c8",
    selectedBg: isDark
      ? "rgba(217, 119, 87, 0.12)"
      : "rgba(217, 119, 87, 0.06)",
  };

  const toggleExpanded = useCallback((id: string) => {
    setExpandedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const toggleDataFlow = useCallback(() => {
    if (showDataFlow) {
      setShowDataFlow(false);
      setDataFlowStep(-1);
      if (dataFlowTimerRef.current) {
        clearInterval(dataFlowTimerRef.current);
        dataFlowTimerRef.current = null;
      }
      return;
    }

    setShowDataFlow(true);
    setDataFlowStep(0);
    // Expand all nodes to show the flow
    setExpandedIds(
      new Set(flattenTree(componentTree).map((n) => n.id))
    );

    let step = 0;
    dataFlowTimerRef.current = setInterval(() => {
      step++;
      if (step >= dataFlowSteps.length) {
        if (dataFlowTimerRef.current) {
          clearInterval(dataFlowTimerRef.current);
          dataFlowTimerRef.current = null;
        }
        return;
      }
      setDataFlowStep(step);
    }, 1200);
  }, [showDataFlow]);

  useEffect(() => {
    return () => {
      if (dataFlowTimerRef.current) clearInterval(dataFlowTimerRef.current);
    };
  }, []);

  const allNodes = flattenTree(componentTree);
  const selectedNode = selectedId
    ? allNodes.find((n) => n.id === selectedId)
    : null;

  function renderNode(node: ComponentNode, depth: number = 0) {
    const isExpanded = expandedIds.has(node.id);
    const hasChildren = node.children && node.children.length > 0;
    const isSelected = selectedId === node.id;
    const isDataFlowActive =
      showDataFlow &&
      dataFlowStep >= 0 &&
      dataFlowStep < dataFlowSteps.length &&
      dataFlowSteps[dataFlowStep].from === node.id;

    return (
      <div key={node.id}>
        <motion.div
          animate={{
            backgroundColor: isDataFlowActive
              ? colors.terracottaBg
              : isSelected
              ? colors.selectedBg
              : "transparent",
          }}
          transition={{ duration: 0.3 }}
          onClick={() => setSelectedId(isSelected ? null : node.id)}
          style={{
            display: "flex",
            alignItems: "center",
            gap: 8,
            padding: "6px 10px",
            paddingLeft: depth * 24 + 10,
            borderRadius: 6,
            cursor: "pointer",
            position: "relative",
            borderLeft: isDataFlowActive
              ? `2px solid ${colors.terracotta}`
              : "2px solid transparent",
            transition: "border-color 0.3s",
          }}
        >
          {/* Tree lines */}
          {depth > 0 &&
            Array.from({ length: depth }).map((_, i) => (
              <div
                key={i}
                style={{
                  position: "absolute",
                  left: i * 24 + 20,
                  top: 0,
                  bottom: 0,
                  width: 1,
                  background: colors.treeLine,
                }}
              />
            ))}

          {/* Expand/collapse toggle */}
          {hasChildren ? (
            <button
              onClick={(e) => {
                e.stopPropagation();
                toggleExpanded(node.id);
              }}
              style={{
                width: 18,
                height: 18,
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                background: "transparent",
                border: "none",
                cursor: "pointer",
                color: colors.textSecondary,
                fontSize: 12,
                flexShrink: 0,
                padding: 0,
                fontFamily: "var(--font-mono)",
              }}
            >
              {isExpanded ? (
                <svg width="10" height="10" viewBox="0 0 10 10">
                  <path
                    d="M2 3.5 L5 6.5 L8 3.5"
                    stroke="currentColor"
                    strokeWidth="1.5"
                    fill="none"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                  />
                </svg>
              ) : (
                <svg width="10" height="10" viewBox="0 0 10 10">
                  <path
                    d="M3.5 2 L6.5 5 L3.5 8"
                    stroke="currentColor"
                    strokeWidth="1.5"
                    fill="none"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                  />
                </svg>
              )}
            </button>
          ) : (
            <div style={{ width: 18, flexShrink: 0 }} />
          )}

          {/* Component name */}
          <span
            style={{
              fontFamily: "var(--font-mono)",
              fontSize: 13,
              fontWeight: isSelected ? 600 : 500,
              color: isDataFlowActive
                ? colors.terracotta
                : isSelected
                ? colors.terracotta
                : colors.text,
              transition: "color 0.2s",
            }}
          >
            {"<"}
            {node.name}
            {" />"}
          </span>

          {/* Hot path badge */}
          {node.isHotPath && (
            <span
              style={{
                padding: "1px 6px",
                fontFamily: "var(--font-mono)",
                fontSize: 9,
                fontWeight: 600,
                background: colors.hotPath,
                color: colors.hotPathBorder,
                borderRadius: 4,
                border: `1px solid ${colors.hotPathBorder}`,
                whiteSpace: "nowrap",
                textTransform: "uppercase",
                letterSpacing: "0.5px",
              }}
            >
              hot path
            </span>
          )}

          {/* Lines count */}
          <span
            style={{
              fontFamily: "var(--font-mono)",
              fontSize: 10,
              color: colors.textSecondary,
              marginLeft: "auto",
              whiteSpace: "nowrap",
            }}
          >
            {node.lines}
          </span>

          {/* Data flow arrow */}
          <AnimatePresence>
            {isDataFlowActive && (
              <motion.div
                initial={{ opacity: 0, x: -8 }}
                animate={{ opacity: 1, x: 0 }}
                exit={{ opacity: 0, x: 8 }}
                style={{
                  fontFamily: "var(--font-mono)",
                  fontSize: 11,
                  color: colors.terracotta,
                  whiteSpace: "nowrap",
                  maxWidth: 200,
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                }}
              >
                {dataFlowSteps[dataFlowStep].label}
              </motion.div>
            )}
          </AnimatePresence>
        </motion.div>

        {/* Children */}
        <AnimatePresence>
          {hasChildren && isExpanded && (
            <motion.div
              initial={{ opacity: 0, height: 0 }}
              animate={{ opacity: 1, height: "auto" }}
              exit={{ opacity: 0, height: 0 }}
              transition={{ duration: 0.2 }}
              style={{ overflow: "hidden" }}
            >
              {node.children!.map((child) => renderNode(child, depth + 1))}
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    );
  }

  return (
    <div className={className} style={{ fontFamily: "var(--font-serif)" }}>
      {/* Header */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          padding: "16px 20px",
          background: colors.cardBg,
          border: `1px solid ${colors.cardBorder}`,
          borderRadius: "12px 12px 0 0",
          borderBottom: "none",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <div
            style={{
              width: 8,
              height: 8,
              borderRadius: "50%",
              background: colors.terracotta,
            }}
          />
          <span
            style={{
              fontFamily: "var(--font-mono)",
              fontSize: 14,
              color: colors.terracotta,
              fontWeight: 600,
            }}
          >
            REPL Component Hierarchy
          </span>
          <span
            style={{
              fontFamily: "var(--font-mono)",
              fontSize: 11,
              padding: "2px 8px",
              background: colors.terracottaBg,
              color: colors.terracotta,
              borderRadius: 4,
              fontWeight: 700,
            }}
          >
            ~5,000 lines
          </span>
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          <button
            onClick={toggleDataFlow}
            style={{
              padding: "6px 12px",
              fontFamily: "var(--font-mono)",
              fontSize: 11,
              fontWeight: 600,
              background: showDataFlow ? colors.terracotta : "transparent",
              color: showDataFlow ? "#fff" : colors.textSecondary,
              border: `1px solid ${
                showDataFlow ? colors.terracotta : colors.cardBorder
              }`,
              borderRadius: 6,
              cursor: "pointer",
              transition: "all 0.2s",
            }}
          >
            {showDataFlow ? "Stop Flow" : "Show Data Flow"}
          </button>
          <button
            onClick={() =>
              setExpandedIds(
                expandedIds.size > 3
                  ? new Set(["repl"])
                  : new Set(flattenTree(componentTree).map((n) => n.id))
              )
            }
            style={{
              padding: "6px 12px",
              fontFamily: "var(--font-mono)",
              fontSize: 11,
              background: "transparent",
              color: colors.textSecondary,
              border: `1px solid ${colors.cardBorder}`,
              borderRadius: 6,
              cursor: "pointer",
            }}
          >
            {expandedIds.size > 3 ? "Collapse" : "Expand All"}
          </button>
        </div>
      </div>

      {/* Tree + Detail */}
      <div
        style={{
          display: "flex",
          gap: 0,
          borderRadius: "0 0 12px 12px",
          overflow: "hidden",
          border: `1px solid ${colors.cardBorder}`,
        }}
      >
        {/* Tree panel */}
        <div
          style={{
            flex: "1 1 auto",
            minWidth: 0,
            padding: "12px 8px",
            background: colors.cardBg,
            borderRight: selectedNode
              ? `1px solid ${colors.cardBorder}`
              : "none",
            maxHeight: 500,
            overflowY: "auto",
          }}
        >
          {renderNode(componentTree)}
        </div>

        {/* Detail panel */}
        <AnimatePresence>
          {selectedNode && (
            <motion.div
              initial={{ width: 0, opacity: 0 }}
              animate={{ width: 320, opacity: 1 }}
              exit={{ width: 0, opacity: 0 }}
              transition={{ duration: 0.2 }}
              style={{
                flexShrink: 0,
                background: colors.cardBg,
                overflow: "hidden",
              }}
            >
              <div style={{ padding: "16px 20px", width: 320 }}>
                {/* Component name */}
                <div
                  style={{
                    fontFamily: "var(--font-mono)",
                    fontSize: 15,
                    fontWeight: 600,
                    color: colors.terracotta,
                    marginBottom: 4,
                  }}
                >
                  {"<"}
                  {selectedNode.name}
                  {" />"}
                </div>
                <div
                  style={{
                    fontFamily: "var(--font-mono)",
                    fontSize: 11,
                    color: colors.textSecondary,
                    marginBottom: 12,
                  }}
                >
                  {selectedNode.lines} lines
                  {selectedNode.isHotPath && (
                    <span style={{ color: colors.hotPathBorder }}>
                      {" "}
                      -- hot path
                    </span>
                  )}
                </div>

                {/* Description */}
                <p
                  style={{
                    fontSize: 12,
                    color: colors.text,
                    lineHeight: 1.6,
                    marginBottom: 16,
                  }}
                >
                  {selectedNode.description}
                </p>

                {/* Key Props */}
                <div
                  style={{
                    fontFamily: "var(--font-mono)",
                    fontSize: 10,
                    color: colors.textSecondary,
                    textTransform: "uppercase",
                    letterSpacing: "0.5px",
                    marginBottom: 6,
                  }}
                >
                  Key Props
                </div>
                <div
                  style={{
                    display: "flex",
                    flexWrap: "wrap",
                    gap: 4,
                    marginBottom: 16,
                  }}
                >
                  {selectedNode.keyProps.map((prop) => (
                    <span
                      key={prop}
                      style={{
                        padding: "2px 8px",
                        fontFamily: "var(--font-mono)",
                        fontSize: 11,
                        background: colors.surfaceBg,
                        color: colors.text,
                        borderRadius: 4,
                        border: `1px solid ${colors.cardBorder}`,
                      }}
                    >
                      {prop}
                    </span>
                  ))}
                </div>

                {/* Re-render triggers */}
                <div
                  style={{
                    fontFamily: "var(--font-mono)",
                    fontSize: 10,
                    color: colors.textSecondary,
                    textTransform: "uppercase",
                    letterSpacing: "0.5px",
                    marginBottom: 6,
                  }}
                >
                  Re-render Triggers
                </div>
                <div
                  style={{
                    display: "flex",
                    flexDirection: "column",
                    gap: 3,
                  }}
                >
                  {selectedNode.reRenderTriggers.map((trigger) => (
                    <div
                      key={trigger}
                      style={{
                        display: "flex",
                        alignItems: "center",
                        gap: 6,
                        fontSize: 11,
                        color: colors.textSecondary,
                      }}
                    >
                      <div
                        style={{
                          width: 4,
                          height: 4,
                          borderRadius: "50%",
                          background: selectedNode.isHotPath
                            ? colors.hotPathBorder
                            : colors.terracotta,
                          flexShrink: 0,
                        }}
                      />
                      {trigger}
                    </div>
                  ))}
                </div>
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      </div>

      {/* Data flow legend */}
      <AnimatePresence>
        {showDataFlow && (
          <motion.div
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: "auto" }}
            exit={{ opacity: 0, height: 0 }}
            style={{
              marginTop: 12,
              padding: "12px 16px",
              background: colors.cardBg,
              border: `1px solid ${colors.cardBorder}`,
              borderRadius: 8,
              overflow: "hidden",
            }}
          >
            <div
              style={{
                fontFamily: "var(--font-mono)",
                fontSize: 11,
                color: colors.textSecondary,
                marginBottom: 8,
              }}
            >
              Message Flow: User Input to Rendered Output
            </div>
            <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
              {dataFlowSteps.map((step, index) => (
                <div
                  key={index}
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 6,
                  }}
                >
                  <div
                    style={{
                      width: 20,
                      height: 20,
                      borderRadius: "50%",
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "center",
                      fontFamily: "var(--font-mono)",
                      fontSize: 10,
                      fontWeight: 700,
                      background:
                        dataFlowStep >= index
                          ? colors.terracotta
                          : "transparent",
                      color:
                        dataFlowStep >= index ? "#fff" : colors.textSecondary,
                      border: `1.5px solid ${
                        dataFlowStep >= index
                          ? colors.terracotta
                          : colors.cardBorder
                      }`,
                      transition: "all 0.3s",
                      flexShrink: 0,
                    }}
                  >
                    {index + 1}
                  </div>
                  <span
                    style={{
                      fontFamily: "var(--font-mono)",
                      fontSize: 10,
                      color:
                        dataFlowStep === index
                          ? colors.terracotta
                          : colors.textSecondary,
                      transition: "color 0.3s",
                      whiteSpace: "nowrap",
                    }}
                  >
                    {step.label}
                  </span>
                  {index < dataFlowSteps.length - 1 && (
                    <svg
                      width="12"
                      height="12"
                      viewBox="0 0 12 12"
                      style={{ flexShrink: 0 }}
                    >
                      <path
                        d="M3 6h6M7 4l2 2-2 2"
                        stroke={
                          dataFlowStep > index
                            ? colors.terracotta
                            : colors.textSecondary
                        }
                        strokeWidth="1.5"
                        fill="none"
                        strokeLinecap="round"
                        strokeLinejoin="round"
                        style={{ transition: "stroke 0.3s" }}
                      />
                    </svg>
                  )}
                </div>
              ))}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
