import { useState, useEffect, useCallback } from "react";
import { motion, AnimatePresence } from "framer-motion";

// --- Types ---

type View = "grid" | "decision" | "oauth";

interface TransportType {
  id: string;
  name: string;
  category: string;
  categoryColor: string;
  description: string;
  howItWorks: string;
  whenToUse: string;
  connectionFlow: string[];
}

interface DecisionNode {
  id: string;
  question: string;
  options: { label: string; next: string }[];
  result?: string;
  resultTransport?: string;
}

interface OAuthStep {
  id: number;
  title: string;
  description: string;
  detail: string;
}

// --- Data ---

const transports: TransportType[] = [
  {
    id: "stdio",
    name: "stdio",
    category: "Local",
    categoryColor: "#4ade80",
    description: "Subprocess with stdin/stdout JSON-RPC. Default when type is omitted.",
    howItWorks: "Claude Code spawns a child process. JSON-RPC messages are piped through stdin (client to server) and stdout (server to client). No network, no auth.",
    whenToUse: "Local tools: filesystem access, database queries, custom scripts. Most common transport.",
    connectionFlow: ["Spawn subprocess", "Pipe stdin/stdout", "Send tools/list", "Ready"],
  },
  {
    id: "sse",
    name: "SSE (Server-Sent Events)",
    category: "Remote",
    categoryColor: "#60a5fa",
    description: "Legacy HTTP transport. Client POSTs requests, server pushes responses via SSE stream.",
    howItWorks: "Client establishes an SSE connection for server-to-client messages. Client-to-server messages are sent via HTTP POST. Widely deployed but being superseded.",
    whenToUse: "Legacy MCP servers deployed before 2025. Still common in the ecosystem.",
    connectionFlow: ["HTTP GET /sse", "SSE stream established", "POST requests", "SSE responses"],
  },
  {
    id: "http",
    name: "Streamable HTTP",
    category: "Remote",
    categoryColor: "#60a5fa",
    description: "Current spec recommendation. POST with optional SSE for streaming responses.",
    howItWorks: "Client sends JSON-RPC via HTTP POST. Server can respond with JSON (simple) or upgrade to SSE stream (streaming). Bidirectional via session IDs.",
    whenToUse: "New remote MCP servers. The current specification recommendation.",
    connectionFlow: ["POST /mcp", "Response: JSON or SSE", "Session ID tracked", "Retry on -32001"],
  },
  {
    id: "ws",
    name: "WebSocket",
    category: "Remote",
    categoryColor: "#60a5fa",
    description: "Full-duplex bidirectional communication. Rare in practice.",
    howItWorks: "Standard WebSocket connection. JSON-RPC messages flow in both directions. Bun and Node have different WebSocket APIs -- runtime split required.",
    whenToUse: "When bidirectional server-initiated communication is needed. Rare outside IDE integrations.",
    connectionFlow: ["WS handshake", "Bidirectional channel", "JSON-RPC both ways", "Close on disconnect"],
  },
  {
    id: "sdk",
    name: "SDK Transport",
    category: "In-Process",
    categoryColor: "#a78bfa",
    description: "Control messages over stdin/stdout for SDK-embedded scenarios.",
    howItWorks: "Used when Claude Code runs as a subprocess via the SDK. Control messages (MCP requests) are multiplexed over the same stdin/stdout used for agent communication.",
    whenToUse: "When building on top of Claude Code via the official SDK.",
    connectionFlow: ["SDK spawns Claude Code", "Multiplex control messages", "MCP over stdin/stdout", "Shared channel"],
  },
  {
    id: "sse-ide",
    name: "IDE stdio",
    category: "IDE",
    categoryColor: "#f472b6",
    description: "VS Code or JetBrains extension communicating via stdio channel.",
    howItWorks: "IDE extension provides an MCP server through its extension API. Communication uses the IDE's built-in stdio channel rather than network.",
    whenToUse: "VS Code extensions that expose MCP tools through the IDE's native channel.",
    connectionFlow: ["IDE extension loads", "stdio channel opened", "MCP handshake", "Tools available"],
  },
  {
    id: "ws-ide",
    name: "IDE WebSocket",
    category: "IDE",
    categoryColor: "#f472b6",
    description: "IDE remote connection via WebSocket. Runtime-specific (Bun vs Node).",
    howItWorks: "WebSocket connection to an IDE extension running remotely. Bun's WebSocket accepts proxy/TLS natively; Node requires the ws package.",
    whenToUse: "Remote IDE connections (e.g., JetBrains Gateway, VS Code Remote).",
    connectionFlow: ["WS connect to IDE", "Runtime detection", "Bun native / Node ws", "MCP ready"],
  },
  {
    id: "inprocess",
    name: "In-Process",
    category: "In-Process",
    categoryColor: "#a78bfa",
    description: "Linked transport pairs. Direct function calls. 63 lines total.",
    howItWorks: "Two InProcessTransport instances are linked as peers. send() delivers via queueMicrotask() to prevent stack depth issues. close() cascades to peer.",
    whenToUse: "Same-process MCP servers: Chrome MCP, Computer Use MCP. Zero network overhead.",
    connectionFlow: ["Create linked pair", "queueMicrotask delivery", "Direct function calls", "Cascade close"],
  },
];

const decisionTree: DecisionNode[] = [
  {
    id: "start",
    question: "Where is your MCP server?",
    options: [
      { label: "Same machine (local process)", next: "local" },
      { label: "Remote service (HTTP/WS)", next: "remote" },
      { label: "Same process (embedded)", next: "inprocess" },
      { label: "IDE extension", next: "ide" },
    ],
  },
  {
    id: "local",
    question: "",
    options: [],
    result: "Use stdio -- no network, no auth, just pipes. The default and most common transport.",
    resultTransport: "stdio",
  },
  {
    id: "remote",
    question: "Does the server need streaming responses?",
    options: [
      { label: "Yes, streaming needed", next: "remote-stream" },
      { label: "No, simple request/response", next: "remote-simple" },
      { label: "Need full bidirectional", next: "remote-bidi" },
    ],
  },
  {
    id: "remote-stream",
    question: "Is the server a legacy (pre-2025) deployment?",
    options: [
      { label: "Yes, legacy server", next: "remote-legacy" },
      { label: "No, new server", next: "remote-new" },
    ],
  },
  {
    id: "remote-legacy",
    question: "",
    options: [],
    result: "Use SSE -- legacy but widely deployed. Server pushes responses via Server-Sent Events.",
    resultTransport: "sse",
  },
  {
    id: "remote-new",
    question: "",
    options: [],
    result: "Use Streamable HTTP -- current spec recommendation. POST with optional SSE upgrade.",
    resultTransport: "http",
  },
  {
    id: "remote-simple",
    question: "",
    options: [],
    result: "Use Streamable HTTP -- works for simple JSON responses too. The spec default for remote.",
    resultTransport: "http",
  },
  {
    id: "remote-bidi",
    question: "",
    options: [],
    result: "Use WebSocket -- full-duplex bidirectional. Note: Bun/Node runtime split for ws package.",
    resultTransport: "ws",
  },
  {
    id: "inprocess",
    question: "Is the server built with the MCP SDK?",
    options: [
      { label: "Yes, SDK-based", next: "inprocess-sdk" },
      { label: "No, custom server in same process", next: "inprocess-linked" },
    ],
  },
  {
    id: "inprocess-sdk",
    question: "",
    options: [],
    result: "Use SDK transport -- multiplexes MCP over the existing stdin/stdout channel.",
    resultTransport: "sdk",
  },
  {
    id: "inprocess-linked",
    question: "",
    options: [],
    result: "Use InProcessTransport -- linked pairs with queueMicrotask delivery. Only 63 lines.",
    resultTransport: "inprocess",
  },
  {
    id: "ide",
    question: "Is the IDE local or remote?",
    options: [
      { label: "Local IDE (VS Code, JetBrains)", next: "ide-local" },
      { label: "Remote IDE (Gateway, Remote SSH)", next: "ide-remote" },
    ],
  },
  {
    id: "ide-local",
    question: "",
    options: [],
    result: "Use IDE stdio -- communicates through the IDE's built-in extension channel.",
    resultTransport: "sse-ide",
  },
  {
    id: "ide-remote",
    question: "",
    options: [],
    result: "Use IDE WebSocket -- connects remotely. Handles Bun/Node runtime differences.",
    resultTransport: "ws-ide",
  },
];

const oauthSteps: OAuthStep[] = [
  {
    id: 1,
    title: "Server Returns 401",
    description: "MCP server requires authentication",
    detail: "The initial request to the MCP server returns HTTP 401 Unauthorized. This triggers the OAuth discovery chain.",
  },
  {
    id: 2,
    title: "RFC 9728 Discovery",
    description: "Probe /.well-known/oauth-protected-resource",
    detail: "GET request to the server's well-known endpoint. If found, extract authorization_servers[0] and proceed to RFC 8414 discovery against that URL.",
  },
  {
    id: 3,
    title: "RFC 8414 Metadata",
    description: "Discover authorization server metadata",
    detail: "Fetch the OpenID/OAuth metadata document. Contains: token_endpoint, authorization_endpoint, supported scopes, PKCE requirements. Falls back to path-aware probing if not found.",
  },
  {
    id: 4,
    title: "OAuth 2.0 + PKCE Flow",
    description: "Browser-based authorization with code verifier",
    detail: "PKCE (Proof Key for Code Exchange) prevents authorization code interception. Generate code_verifier, compute code_challenge, redirect user to authorize, exchange code for tokens.",
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

// --- Component ---

interface Props {
  className?: string;
}

export default function MCPTransports({ className }: Props) {
  const isDark = useDarkMode();
  const [view, setView] = useState<View>("grid");
  const [selectedTransport, setSelectedTransport] = useState<string | null>(null);
  const [decisionPath, setDecisionPath] = useState<string[]>(["start"]);
  const [activeOAuthStep, setActiveOAuthStep] = useState<number | null>(null);

  const colors = {
    accent: "#d97757",
    accentBg: isDark ? "rgba(217, 119, 87, 0.08)" : "rgba(217, 119, 87, 0.05)",
    accentBorder: "rgba(217, 119, 87, 0.5)",
    text: isDark ? "#f5f4ed" : "#141413",
    textSecondary: "#87867f",
    cardBg: isDark ? "#1e1e1c" : "#ffffff",
    cardBorder: isDark ? "#333" : "#e8e6dc",
    surfaceBg: isDark ? "#30302e" : "#f5f4ed",
    green: "#4ade80",
    greenBg: isDark ? "rgba(74, 222, 128, 0.1)" : "rgba(74, 222, 128, 0.08)",
  };

  const currentDecisionNode = decisionTree.find(
    (n) => n.id === decisionPath[decisionPath.length - 1]
  );

  const advanceDecision = useCallback(
    (nextId: string) => {
      setDecisionPath((prev) => [...prev, nextId]);
    },
    []
  );

  const resetDecision = useCallback(() => {
    setDecisionPath(["start"]);
  }, []);

  const goBackDecision = useCallback(() => {
    setDecisionPath((prev) => (prev.length > 1 ? prev.slice(0, -1) : prev));
  }, []);

  return (
    <div className={className} style={{ fontFamily: "var(--font-serif)" }}>
      {/* View tabs */}
      <div
        style={{
          display: "flex",
          gap: 0,
          marginBottom: 24,
          borderBottom: `1px solid ${colors.cardBorder}`,
        }}
      >
        {([
          { id: "grid" as View, label: "8 Transports" },
          { id: "decision" as View, label: "Which Should I Use?" },
          { id: "oauth" as View, label: "OAuth Discovery" },
        ]).map((tab) => (
          <button
            key={tab.id}
            onClick={() => setView(tab.id)}
            style={{
              padding: "12px 20px",
              fontSize: 13,
              fontWeight: 600,
              fontFamily: "var(--font-mono)",
              background: "none",
              border: "none",
              borderBottom:
                view === tab.id
                  ? `2px solid ${colors.accent}`
                  : "2px solid transparent",
              color: view === tab.id ? colors.accent : colors.textSecondary,
              cursor: "pointer",
              transition: "all 0.2s",
            }}
          >
            {tab.label}
          </button>
        ))}
      </div>

      <AnimatePresence mode="wait">
        {view === "grid" && (
          <motion.div
            key="grid"
            initial={{ opacity: 0, y: 10 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -10 }}
            transition={{ duration: 0.2 }}
          >
            <TransportGrid
              colors={colors}
              isDark={isDark}
              selectedTransport={selectedTransport}
              setSelectedTransport={setSelectedTransport}
            />
          </motion.div>
        )}
        {view === "decision" && (
          <motion.div
            key="decision"
            initial={{ opacity: 0, y: 10 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -10 }}
            transition={{ duration: 0.2 }}
          >
            <DecisionTree
              colors={colors}
              isDark={isDark}
              currentNode={currentDecisionNode!}
              path={decisionPath}
              onAdvance={advanceDecision}
              onReset={resetDecision}
              onBack={goBackDecision}
            />
          </motion.div>
        )}
        {view === "oauth" && (
          <motion.div
            key="oauth"
            initial={{ opacity: 0, y: 10 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -10 }}
            transition={{ duration: 0.2 }}
          >
            <OAuthFlow
              colors={colors}
              isDark={isDark}
              activeStep={activeOAuthStep}
              setActiveStep={setActiveOAuthStep}
            />
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

// --- Transport Grid ---

function TransportGrid({
  colors,
  isDark,
  selectedTransport,
  setSelectedTransport,
}: {
  colors: Record<string, string>;
  isDark: boolean;
  selectedTransport: string | null;
  setSelectedTransport: (id: string | null) => void;
}) {
  const categories = ["Local", "Remote", "In-Process", "IDE"];

  return (
    <div>
      {/* Category legend */}
      <div style={{ display: "flex", gap: 16, marginBottom: 16, flexWrap: "wrap" }}>
        {categories.map((cat) => {
          const t = transports.find((tr) => tr.category === cat);
          return (
            <div key={cat} style={{ display: "flex", alignItems: "center", gap: 6 }}>
              <div
                style={{
                  width: 10,
                  height: 10,
                  borderRadius: "50%",
                  background: t?.categoryColor || "#888",
                }}
              />
              <span style={{ fontSize: 11, fontFamily: "var(--font-mono)", color: colors.textSecondary }}>
                {cat}
              </span>
            </div>
          );
        })}
      </div>

      {/* Grid */}
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(auto-fill, minmax(240px, 1fr))",
          gap: 10,
          marginBottom: 16,
        }}
      >
        {transports.map((transport) => {
          const isSelected = selectedTransport === transport.id;
          return (
            <motion.button
              key={transport.id}
              onClick={() =>
                setSelectedTransport(isSelected ? null : transport.id)
              }
              whileHover={{ scale: 1.02 }}
              whileTap={{ scale: 0.98 }}
              style={{
                padding: "14px 16px",
                borderRadius: 10,
                border: `1px solid ${isSelected ? transport.categoryColor : colors.cardBorder}`,
                background: isSelected
                  ? `${transport.categoryColor}10`
                  : colors.cardBg,
                cursor: "pointer",
                textAlign: "left",
                transition: "all 0.2s",
                position: "relative",
              }}
            >
              {/* Category dot */}
              <div
                style={{
                  position: "absolute",
                  top: 14,
                  right: 14,
                  width: 8,
                  height: 8,
                  borderRadius: "50%",
                  background: transport.categoryColor,
                }}
              />

              <div
                style={{
                  fontSize: 14,
                  fontWeight: 600,
                  fontFamily: "var(--font-mono)",
                  color: isSelected ? transport.categoryColor : colors.text,
                  marginBottom: 4,
                  paddingRight: 20,
                }}
              >
                {transport.name}
              </div>
              <div
                style={{
                  fontSize: 10,
                  fontFamily: "var(--font-mono)",
                  color: transport.categoryColor,
                  marginBottom: 8,
                }}
              >
                {transport.category}
              </div>
              <div style={{ fontSize: 11, color: colors.textSecondary, lineHeight: 1.5 }}>
                {transport.description}
              </div>
            </motion.button>
          );
        })}
      </div>

      {/* Selected transport detail */}
      <AnimatePresence>
        {selectedTransport && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.2 }}
            style={{ overflow: "hidden" }}
          >
            {(() => {
              const t = transports.find((tr) => tr.id === selectedTransport);
              if (!t) return null;
              return (
                <div
                  style={{
                    padding: "18px 22px",
                    borderRadius: 12,
                    border: `1px solid ${t.categoryColor}40`,
                    background: `${t.categoryColor}08`,
                  }}
                >
                  <div
                    style={{
                      fontSize: 15,
                      fontWeight: 600,
                      color: colors.text,
                      marginBottom: 16,
                      fontFamily: "var(--font-mono)",
                    }}
                  >
                    {t.name}
                  </div>

                  <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 16, marginBottom: 16 }}>
                    <div>
                      <div
                        style={{
                          fontSize: 11,
                          fontWeight: 600,
                          color: t.categoryColor,
                          fontFamily: "var(--font-mono)",
                          marginBottom: 6,
                          textTransform: "uppercase",
                          letterSpacing: "0.05em",
                        }}
                      >
                        How It Works
                      </div>
                      <div style={{ fontSize: 12, color: colors.textSecondary, lineHeight: 1.6 }}>
                        {t.howItWorks}
                      </div>
                    </div>
                    <div>
                      <div
                        style={{
                          fontSize: 11,
                          fontWeight: 600,
                          color: t.categoryColor,
                          fontFamily: "var(--font-mono)",
                          marginBottom: 6,
                          textTransform: "uppercase",
                          letterSpacing: "0.05em",
                        }}
                      >
                        When To Use
                      </div>
                      <div style={{ fontSize: 12, color: colors.textSecondary, lineHeight: 1.6 }}>
                        {t.whenToUse}
                      </div>
                    </div>
                  </div>

                  {/* Connection flow */}
                  <div
                    style={{
                      fontSize: 11,
                      fontWeight: 600,
                      color: t.categoryColor,
                      fontFamily: "var(--font-mono)",
                      marginBottom: 8,
                      textTransform: "uppercase",
                      letterSpacing: "0.05em",
                    }}
                  >
                    Connection Flow
                  </div>
                  <div style={{ display: "flex", alignItems: "center", gap: 0 }}>
                    {t.connectionFlow.map((step, i) => (
                      <div key={i} style={{ display: "flex", alignItems: "center", flex: 1 }}>
                        <div
                          style={{
                            flex: 1,
                            textAlign: "center",
                            padding: "8px 6px",
                            borderRadius: 8,
                            background: isDark ? "rgba(255,255,255,0.05)" : "rgba(0,0,0,0.03)",
                            fontSize: 11,
                            fontFamily: "var(--font-mono)",
                            color: colors.text,
                          }}
                        >
                          {step}
                        </div>
                        {i < t.connectionFlow.length - 1 && (
                          <svg width="16" height="12" viewBox="0 0 16 12" fill="none" style={{ flexShrink: 0 }}>
                            <path d="M2 6H14M14 6L10 2M14 6L10 10" stroke={t.categoryColor} strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
                          </svg>
                        )}
                      </div>
                    ))}
                  </div>
                </div>
              );
            })()}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

// --- Decision Tree ---

function DecisionTree({
  colors,
  isDark,
  currentNode,
  path,
  onAdvance,
  onReset,
  onBack,
}: {
  colors: Record<string, string>;
  isDark: boolean;
  currentNode: DecisionNode;
  path: string[];
  onAdvance: (id: string) => void;
  onReset: () => void;
  onBack: () => void;
}) {
  const isResult = !!currentNode.result;

  return (
    <div>
      {/* Path breadcrumb */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 6,
          marginBottom: 20,
          flexWrap: "wrap",
        }}
      >
        {path.map((nodeId, i) => {
          const node = decisionTree.find((n) => n.id === nodeId);
          return (
            <div key={nodeId} style={{ display: "flex", alignItems: "center", gap: 6 }}>
              {i > 0 && (
                <svg width="12" height="12" viewBox="0 0 12 12" fill="none">
                  <path d="M4 2L8 6L4 10" stroke={colors.textSecondary} strokeWidth="1.5" strokeLinecap="round" />
                </svg>
              )}
              <span
                style={{
                  fontSize: 11,
                  fontFamily: "var(--font-mono)",
                  color: i === path.length - 1 ? colors.accent : colors.textSecondary,
                  fontWeight: i === path.length - 1 ? 600 : 400,
                }}
              >
                {node?.result ? "Result" : node?.question?.split("?")[0] || "Start"}
              </span>
            </div>
          );
        })}
      </div>

      {/* Current node */}
      <motion.div
        key={currentNode.id}
        initial={{ opacity: 0, x: 20 }}
        animate={{ opacity: 1, x: 0 }}
        transition={{ duration: 0.2 }}
        style={{
          padding: "24px 28px",
          borderRadius: 14,
          border: `1px solid ${isResult ? colors.green : colors.cardBorder}`,
          background: isResult ? colors.greenBg : colors.cardBg,
          marginBottom: 20,
        }}
      >
        {isResult ? (
          <div>
            <div
              style={{
                fontSize: 11,
                fontFamily: "var(--font-mono)",
                fontWeight: 600,
                color: colors.green,
                textTransform: "uppercase",
                letterSpacing: "0.05em",
                marginBottom: 8,
              }}
            >
              Recommendation
            </div>
            <div style={{ fontSize: 15, color: colors.text, lineHeight: 1.6, marginBottom: 16 }}>
              {currentNode.result}
            </div>
            {currentNode.resultTransport && (
              <div
                style={{
                  display: "inline-flex",
                  alignItems: "center",
                  gap: 8,
                  padding: "8px 14px",
                  borderRadius: 8,
                  background: isDark ? "rgba(255,255,255,0.05)" : "rgba(0,0,0,0.03)",
                  fontFamily: "var(--font-mono)",
                  fontSize: 13,
                  fontWeight: 600,
                  color: transports.find((t) => t.id === currentNode.resultTransport)?.categoryColor || colors.accent,
                }}
              >
                <div
                  style={{
                    width: 8,
                    height: 8,
                    borderRadius: "50%",
                    background: transports.find((t) => t.id === currentNode.resultTransport)?.categoryColor || colors.accent,
                  }}
                />
                {transports.find((t) => t.id === currentNode.resultTransport)?.name}
              </div>
            )}
          </div>
        ) : (
          <div>
            <div
              style={{
                fontSize: 18,
                fontWeight: 600,
                color: colors.text,
                marginBottom: 20,
              }}
            >
              {currentNode.question}
            </div>
            <div style={{ display: "grid", gap: 8 }}>
              {currentNode.options.map((opt) => (
                <motion.button
                  key={opt.next}
                  onClick={() => onAdvance(opt.next)}
                  whileHover={{ scale: 1.01, x: 4 }}
                  whileTap={{ scale: 0.99 }}
                  style={{
                    padding: "14px 18px",
                    borderRadius: 10,
                    border: `1px solid ${colors.cardBorder}`,
                    background: colors.surfaceBg,
                    cursor: "pointer",
                    textAlign: "left",
                    fontSize: 13,
                    color: colors.text,
                    fontWeight: 500,
                    display: "flex",
                    alignItems: "center",
                    gap: 12,
                    transition: "border-color 0.2s",
                  }}
                >
                  <svg width="16" height="16" viewBox="0 0 16 16" fill="none" style={{ flexShrink: 0 }}>
                    <path d="M6 4L10 8L6 12" stroke={colors.accent} strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
                  </svg>
                  {opt.label}
                </motion.button>
              ))}
            </div>
          </div>
        )}
      </motion.div>

      {/* Navigation */}
      <div style={{ display: "flex", gap: 8 }}>
        {path.length > 1 && (
          <button
            onClick={onBack}
            style={{
              padding: "8px 16px",
              borderRadius: 8,
              border: `1px solid ${colors.cardBorder}`,
              background: "transparent",
              color: colors.textSecondary,
              fontSize: 12,
              fontWeight: 600,
              fontFamily: "var(--font-mono)",
              cursor: "pointer",
            }}
          >
            Back
          </button>
        )}
        {path.length > 1 && (
          <button
            onClick={onReset}
            style={{
              padding: "8px 16px",
              borderRadius: 8,
              border: `1px solid ${colors.cardBorder}`,
              background: "transparent",
              color: colors.textSecondary,
              fontSize: 12,
              fontWeight: 600,
              fontFamily: "var(--font-mono)",
              cursor: "pointer",
            }}
          >
            Start Over
          </button>
        )}
      </div>
    </div>
  );
}

// --- OAuth Flow ---

function OAuthFlow({
  colors,
  isDark,
  activeStep,
  setActiveStep,
}: {
  colors: Record<string, string>;
  isDark: boolean;
  activeStep: number | null;
  setActiveStep: (step: number | null) => void;
}) {
  return (
    <div>
      <div style={{ fontSize: 13, fontWeight: 600, color: colors.text, marginBottom: 6 }}>
        RFC 9728 + RFC 8414 OAuth Discovery Chain
      </div>
      <div style={{ fontSize: 12, color: colors.textSecondary, marginBottom: 20, lineHeight: 1.5 }}>
        When an MCP server returns 401, Claude Code walks through a multi-step discovery chain to find the authorization server.
        Click each step to see details.
      </div>

      <div style={{ position: "relative", paddingLeft: 28 }}>
        {/* Vertical connector */}
        <div
          style={{
            position: "absolute",
            left: 14,
            top: 20,
            bottom: 20,
            width: 2,
            background: colors.cardBorder,
          }}
        />

        {oauthSteps.map((step, i) => {
          const isActive = activeStep === step.id;
          return (
            <div key={step.id} style={{ position: "relative", marginBottom: i < oauthSteps.length - 1 ? 10 : 0 }}>
              {/* Dot */}
              <motion.div
                animate={{
                  background: isActive ? colors.accent : colors.textSecondary,
                  scale: isActive ? 1.3 : 1,
                }}
                style={{
                  position: "absolute",
                  left: -28 + 14 - 5,
                  top: 18,
                  width: 10,
                  height: 10,
                  borderRadius: "50%",
                  zIndex: 1,
                  transition: "all 0.2s",
                }}
              />

              <motion.button
                onClick={() => setActiveStep(isActive ? null : step.id)}
                whileHover={{ scale: 1.01 }}
                style={{
                  width: "100%",
                  padding: "14px 18px",
                  borderRadius: 10,
                  border: `1px solid ${isActive ? colors.accent : colors.cardBorder}`,
                  background: isActive ? colors.accentBg : colors.cardBg,
                  cursor: "pointer",
                  textAlign: "left",
                  transition: "all 0.2s",
                }}
              >
                <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                  <span
                    style={{
                      fontSize: 11,
                      fontWeight: 700,
                      fontFamily: "var(--font-mono)",
                      color: isActive ? colors.accent : colors.textSecondary,
                      minWidth: 24,
                    }}
                  >
                    {String(step.id).padStart(2, "0")}
                  </span>
                  <span
                    style={{
                      fontSize: 14,
                      fontWeight: 600,
                      color: isActive ? colors.accent : colors.text,
                      flex: 1,
                    }}
                  >
                    {step.title}
                  </span>
                </div>
                <div
                  style={{
                    fontSize: 11,
                    color: colors.textSecondary,
                    marginTop: 4,
                    marginLeft: 34,
                  }}
                >
                  {step.description}
                </div>

                <AnimatePresence>
                  {isActive && (
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
                          color: colors.textSecondary,
                          marginTop: 10,
                          marginLeft: 34,
                          padding: "10px 14px",
                          borderRadius: 8,
                          background: colors.surfaceBg,
                          lineHeight: 1.6,
                        }}
                      >
                        {step.detail}
                      </div>
                    </motion.div>
                  )}
                </AnimatePresence>
              </motion.button>
            </div>
          );
        })}
      </div>

      {/* Fallback chain */}
      <div
        style={{
          marginTop: 20,
          padding: "14px 18px",
          borderRadius: 12,
          border: `1px solid ${colors.cardBorder}`,
          background: colors.cardBg,
        }}
      >
        <div style={{ fontSize: 12, fontWeight: 600, color: colors.text, marginBottom: 8 }}>
          Fallback Chain
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 0, flexWrap: "wrap" }}>
          {[
            { label: "RFC 9728", desc: "Protected Resource" },
            { label: "RFC 8414", desc: "Auth Server Metadata" },
            { label: "Path-aware probing", desc: "Against MCP server URL" },
            { label: "authServerMetadataUrl", desc: "Escape hatch config" },
          ].map((step, i) => (
            <div key={step.label} style={{ display: "flex", alignItems: "center" }}>
              <div
                style={{
                  textAlign: "center",
                  padding: "8px 10px",
                  borderRadius: 8,
                  background: colors.surfaceBg,
                  minWidth: 80,
                }}
              >
                <div style={{ fontSize: 10, fontWeight: 600, fontFamily: "var(--font-mono)", color: colors.accent }}>
                  {step.label}
                </div>
                <div style={{ fontSize: 9, color: colors.textSecondary, marginTop: 2 }}>{step.desc}</div>
              </div>
              {i < 3 && (
                <div style={{ padding: "0 4px" }}>
                  <svg width="16" height="12" viewBox="0 0 16 12" fill="none">
                    <path d="M2 6H14M14 6L10 2M14 6L10 10" stroke={colors.textSecondary} strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
                  </svg>
                </div>
              )}
            </div>
          ))}
        </div>
        <div style={{ fontSize: 11, color: colors.textSecondary, marginTop: 10, lineHeight: 1.5 }}>
          The <code style={{ fontFamily: "var(--font-mono)", fontSize: 10 }}>authServerMetadataUrl</code> escape hatch exists because some OAuth servers implement neither RFC.
        </div>
      </div>
    </div>
  );
}
