import { useState, useEffect, useCallback } from "react";
import { motion, AnimatePresence } from "framer-motion";

// --- Dark mode hook ---

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

// --- Data ---

type Provider = "direct" | "bedrock" | "vertex";

interface PromptSection {
  id: string;
  label: string;
  tier: "static" | "boundary" | "dynamic";
  description: string;
  tokenEstimate: string;
  cacheScope: string;
  contents: string[];
}

const PROMPT_SECTIONS: PromptSection[] = [
  {
    id: "identity",
    label: "Identity & Intro",
    tier: "static",
    description: "System identity, role description, and behavioral foundations",
    tokenEstimate: "~200",
    cacheScope: "global",
    contents: ["You are Claude Code, an expert software engineer...", "Core behavioral rules and safety guidelines"],
  },
  {
    id: "behavior",
    label: "System Behavior Rules",
    tier: "static",
    description: "Response formatting, safety constraints, refusal patterns",
    tokenEstimate: "~500",
    cacheScope: "global",
    contents: ["Tool calling conventions", "Error handling rules", "Safety and content policy"],
  },
  {
    id: "tasks",
    label: "Doing Tasks Guidance",
    tier: "static",
    description: "How to approach multi-step tasks, planning, and verification",
    tokenEstimate: "~400",
    cacheScope: "global",
    contents: ["Task decomposition rules", "Verification requirements", "When to ask vs. proceed"],
  },
  {
    id: "actions",
    label: "Actions Guidance",
    tier: "static",
    description: "Tool definitions, schemas, and usage instructions",
    tokenEstimate: "~2,000",
    cacheScope: "global",
    contents: ["Read, Write, Edit, Bash, Glob, Grep tool schemas", "Tool selection heuristics", "File manipulation rules"],
  },
  {
    id: "tools",
    label: "Tool Usage Instructions",
    tier: "static",
    description: "Detailed per-tool usage patterns and constraints",
    tokenEstimate: "~3,000",
    cacheScope: "global",
    contents: ["Git workflow rules", "Search before create patterns", "File editing best practices"],
  },
  {
    id: "tone",
    label: "Tone & Style",
    tier: "static",
    description: "Output formatting, conciseness rules, communication style",
    tokenEstimate: "~300",
    cacheScope: "global",
    contents: ["Concise by default", "No unnecessary preamble", "Technical precision"],
  },
  {
    id: "efficiency",
    label: "Output Efficiency",
    tier: "static",
    description: "Rules for minimizing token output while maximizing usefulness",
    tokenEstimate: "~200",
    cacheScope: "global",
    contents: ["Avoid restating the question", "Only show relevant code", "Batch tool calls"],
  },
  {
    id: "boundary",
    label: "=== DYNAMIC BOUNDARY ===",
    tier: "boundary",
    description: "Cache breakpoint: everything above is shared globally across all users. Everything below is per-session. Moving sections across this boundary affects fleet-wide cache performance.",
    tokenEstimate: "marker",
    cacheScope: "break",
    contents: ["Each conditional below is a runtime bit that would otherwise multiply the Blake2b prefix hash variants (2^N)"],
  },
  {
    id: "session",
    label: "Session Guidance",
    tier: "dynamic",
    description: "Session-specific behavior overrides and feature flags",
    tokenEstimate: "~300",
    cacheScope: "per-session",
    contents: ["Current permission mode", "Active feature flags", "Session type (REPL vs one-shot)"],
  },
  {
    id: "memory",
    label: "Memory (CLAUDE.md)",
    tier: "dynamic",
    description: "Project-specific instructions loaded from filesystem",
    tokenEstimate: "~2,000-50,000",
    cacheScope: "per-session",
    contents: ["User's CLAUDE.md content", "Project conventions", "Custom rules and preferences"],
  },
  {
    id: "environment",
    label: "Environment Info",
    tier: "dynamic",
    description: "Git status, working directory, OS, shell information",
    tokenEstimate: "~500",
    cacheScope: "per-session",
    contents: ["Git branch, status, recent commits", "Working directory path", "OS and shell version"],
  },
  {
    id: "language",
    label: "Language Preference",
    tier: "dynamic",
    description: "User's preferred response language",
    tokenEstimate: "~50",
    cacheScope: "per-session",
    contents: ["Respond in the user's language"],
  },
  {
    id: "mcp",
    label: "MCP Instructions",
    tier: "dynamic",
    description: "DANGEROUS: User-specific MCP tool definitions. Disables global cache scope when present because MCP definitions are unique per user.",
    tokenEstimate: "~1,000-10,000",
    cacheScope: "UNCACHED",
    contents: ["MCP server tool definitions", "Per-tool instructions", "Server connection details"],
  },
  {
    id: "output-style",
    label: "Output Style",
    tier: "dynamic",
    description: "Session-specific output formatting preferences",
    tokenEstimate: "~100",
    cacheScope: "per-session",
    contents: ["Verbose mode settings", "Expanded view preferences"],
  },
];

const PROVIDER_INFO: Record<Provider, { label: string; authDesc: string; envVar: string; color: string }> = {
  direct: {
    label: "Direct API",
    authDesc: "API key or OAuth token",
    envVar: "ANTHROPIC_API_KEY",
    color: "#d97757",
  },
  bedrock: {
    label: "AWS Bedrock",
    authDesc: "AWS credentials (IAM role / access keys)",
    envVar: "ANTHROPIC_BEDROCK_BASE_URL",
    color: "#ff9900",
  },
  vertex: {
    label: "Google Vertex AI",
    authDesc: "Google Auth (service account / ADC)",
    envVar: "ANTHROPIC_VERTEX_PROJECT_ID",
    color: "#4285f4",
  },
};

interface ToggleFeature {
  id: string;
  label: string;
  default: boolean;
  effect: string;
}

const TOGGLE_FEATURES: ToggleFeature[] = [
  { id: "extended-thinking", label: "Extended thinking", default: false, effect: "Adds thinking budget to request body -- changes cache key" },
  { id: "mcp-tools", label: "MCP tools", default: false, effect: "Adds user-specific tool definitions -- disables global cache scope" },
  { id: "auto-mode", label: "Auto mode (AFK)", default: false, effect: "Adds beta header -- once latched, stays for session" },
];

// --- Component ---

interface Props {
  className?: string;
}

export default function APICallLifecycle({ className }: Props) {
  const isDark = useDarkMode();
  const [selectedProvider, setSelectedProvider] = useState<Provider>("direct");
  const [hoveredSection, setHoveredSection] = useState<string | null>(null);
  const [features, setFeatures] = useState<Record<string, boolean>>({
    "extended-thinking": false,
    "mcp-tools": false,
    "auto-mode": false,
  });

  const colors = {
    terracotta: "#d97757",
    text: isDark ? "#f5f4ed" : "#141413",
    textSecondary: isDark ? "#87867f" : "#87867f",
    bg: isDark ? "#1e1e1c" : "#ffffff",
    bgCard: isDark ? "#2a2a28" : "#f8f7f2",
    border: isDark ? "#333" : "#c2c0b6",
    // Cache tiers
    staticBg: isDark ? "rgba(34,197,94,0.1)" : "rgba(34,197,94,0.06)",
    staticBorder: isDark ? "rgba(34,197,94,0.3)" : "rgba(34,197,94,0.2)",
    staticAccent: "#22c55e",
    boundaryBg: isDark ? "rgba(217,119,87,0.15)" : "rgba(217,119,87,0.08)",
    boundaryBorder: isDark ? "rgba(217,119,87,0.5)" : "rgba(217,119,87,0.4)",
    dynamicBg: isDark ? "rgba(245,158,11,0.1)" : "rgba(245,158,11,0.06)",
    dynamicBorder: isDark ? "rgba(245,158,11,0.3)" : "rgba(245,158,11,0.2)",
    dynamicAccent: "#f59e0b",
    uncachedBg: isDark ? "rgba(239,68,68,0.1)" : "rgba(239,68,68,0.06)",
    uncachedBorder: isDark ? "rgba(239,68,68,0.3)" : "rgba(239,68,68,0.2)",
    uncachedAccent: "#ef4444",
  };

  const toggleFeature = useCallback((id: string) => {
    setFeatures((prev) => ({ ...prev, [id]: !prev[id] }));
  }, []);

  // Compute effective sections based on features
  const effectiveSections = PROMPT_SECTIONS.filter((section) => {
    if (section.id === "mcp" && !features["mcp-tools"]) return false;
    return true;
  });

  // Calculate total tokens for static vs dynamic
  const staticTokens = effectiveSections
    .filter((s) => s.tier === "static")
    .reduce((sum, s) => {
      const match = s.tokenEstimate.match(/[\d,]+/);
      return sum + (match ? parseInt(match[0].replace(",", "")) : 0);
    }, 0);

  const hasMcp = features["mcp-tools"];
  const globalCacheDisabled = hasMcp;

  const getSectionBackground = (section: PromptSection) => {
    if (section.tier === "boundary") return colors.boundaryBg;
    if (section.tier === "static") return colors.staticBg;
    if (section.id === "mcp") return colors.uncachedBg;
    return colors.dynamicBg;
  };

  const getSectionBorder = (section: PromptSection) => {
    if (section.tier === "boundary") return colors.boundaryBorder;
    if (section.tier === "static") return colors.staticBorder;
    if (section.id === "mcp") return colors.uncachedBorder;
    return colors.dynamicBorder;
  };

  const getSectionAccent = (section: PromptSection) => {
    if (section.tier === "boundary") return colors.terracotta;
    if (section.tier === "static") return colors.staticAccent;
    if (section.id === "mcp") return colors.uncachedAccent;
    return colors.dynamicAccent;
  };

  const getCacheScopeLabel = (section: PromptSection) => {
    if (section.tier === "boundary") return "BREAK";
    if (section.tier === "static") {
      return globalCacheDisabled ? "per-session (MCP present)" : "global";
    }
    if (section.id === "mcp") return "UNCACHED";
    return "per-session";
  };

  return (
    <div className={className} style={{ fontFamily: "var(--font-serif)" }}>
      {/* Provider selector */}
      <div
        style={{
          display: "flex",
          gap: 8,
          justifyContent: "center",
          marginBottom: 16,
          flexWrap: "wrap",
        }}
      >
        <span
          style={{
            fontSize: 12,
            color: colors.textSecondary,
            fontFamily: "var(--font-mono)",
            alignSelf: "center",
          }}
        >
          Provider:
        </span>
        {(Object.keys(PROVIDER_INFO) as Provider[]).map((provider) => {
          const info = PROVIDER_INFO[provider];
          const isActive = selectedProvider === provider;
          return (
            <button
              key={provider}
              onClick={() => setSelectedProvider(provider)}
              style={{
                background: isActive ? info.color : "transparent",
                color: isActive ? "#fff" : colors.textSecondary,
                border: `1px solid ${isActive ? info.color : colors.border}`,
                borderRadius: 6,
                padding: "4px 12px",
                fontSize: 12,
                fontFamily: "var(--font-mono)",
                cursor: "pointer",
                transition: "all 0.2s",
              }}
            >
              {info.label}
            </button>
          );
        })}
      </div>

      {/* Provider info strip */}
      <AnimatePresence mode="wait">
        <motion.div
          key={selectedProvider}
          initial={{ opacity: 0, y: -4 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: 4 }}
          transition={{ duration: 0.2 }}
          style={{
            textAlign: "center",
            marginBottom: 16,
            fontSize: 12,
            fontFamily: "var(--font-mono)",
            color: colors.textSecondary,
          }}
        >
          Auth: {PROVIDER_INFO[selectedProvider].authDesc} -- env: <code style={{ fontSize: 11, padding: "1px 4px", borderRadius: 3, background: isDark ? "#333" : "#e8e6dc" }}>{PROVIDER_INFO[selectedProvider].envVar}</code>
          <div style={{ fontSize: 11, marginTop: 2, opacity: 0.7 }}>
            All providers are cast to <code style={{ fontSize: 10, padding: "1px 4px", borderRadius: 3, background: isDark ? "#333" : "#e8e6dc" }}>Anthropic</code> via type erasure -- consumers never branch on provider.
          </div>
        </motion.div>
      </AnimatePresence>

      {/* Main layout: prompt stack + details */}
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "1fr 280px",
          gap: 16,
          background: colors.bg,
          border: `1px solid ${colors.border}`,
          borderRadius: 8,
          padding: 16,
        }}
      >
        {/* Left: Prompt section stack */}
        <div>
          <div
            style={{
              fontSize: 11,
              fontFamily: "var(--font-mono)",
              textTransform: "uppercase",
              letterSpacing: 1,
              color: colors.textSecondary,
              marginBottom: 10,
            }}
          >
            System Prompt Structure
          </div>

          <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
            {effectiveSections.map((section) => {
              const isHovered = hoveredSection === section.id;
              const accent = getSectionAccent(section);
              const isBoundary = section.tier === "boundary";

              return (
                <motion.div
                  key={section.id}
                  onMouseEnter={() => setHoveredSection(section.id)}
                  onMouseLeave={() => setHoveredSection(null)}
                  animate={{
                    scale: isHovered ? 1.01 : 1,
                    borderColor: isHovered ? accent : getSectionBorder(section),
                  }}
                  transition={{ duration: 0.15 }}
                  style={{
                    background: getSectionBackground(section),
                    border: `1px solid ${getSectionBorder(section)}`,
                    borderRadius: isBoundary ? 0 : 6,
                    padding: isBoundary ? "8px 12px" : "8px 12px",
                    cursor: "pointer",
                    position: "relative",
                    borderLeft: isBoundary ? `3px solid ${colors.terracotta}` : `3px solid ${accent}`,
                  }}
                >
                  <div
                    style={{
                      display: "flex",
                      justifyContent: "space-between",
                      alignItems: "center",
                    }}
                  >
                    <span
                      style={{
                        fontSize: isBoundary ? 11 : 12,
                        fontFamily: "var(--font-mono)",
                        fontWeight: isBoundary ? 700 : 500,
                        color: isBoundary ? colors.terracotta : colors.text,
                        letterSpacing: isBoundary ? 1 : 0,
                      }}
                    >
                      {section.label}
                    </span>
                    <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                      {!isBoundary && (
                        <span
                          style={{
                            fontSize: 10,
                            fontFamily: "var(--font-mono)",
                            color: colors.textSecondary,
                            opacity: 0.8,
                          }}
                        >
                          {section.tokenEstimate}
                        </span>
                      )}
                      <span
                        style={{
                          fontSize: 9,
                          fontFamily: "var(--font-mono)",
                          padding: "1px 6px",
                          borderRadius: 3,
                          background: isDark ? "rgba(255,255,255,0.08)" : "rgba(0,0,0,0.06)",
                          color: accent,
                          fontWeight: 600,
                        }}
                      >
                        {getCacheScopeLabel(section)}
                      </span>
                    </div>
                  </div>

                  {/* Expanded on hover */}
                  <AnimatePresence>
                    {isHovered && !isBoundary && (
                      <motion.div
                        initial={{ opacity: 0, height: 0 }}
                        animate={{ opacity: 1, height: "auto" }}
                        exit={{ opacity: 0, height: 0 }}
                        transition={{ duration: 0.15 }}
                        style={{ overflow: "hidden" }}
                      >
                        <div
                          style={{
                            marginTop: 6,
                            fontSize: 11,
                            color: colors.textSecondary,
                            lineHeight: 1.5,
                          }}
                        >
                          {section.description}
                        </div>
                        <div
                          style={{
                            marginTop: 4,
                            fontSize: 10,
                            fontFamily: "var(--font-mono)",
                            color: colors.textSecondary,
                            opacity: 0.8,
                          }}
                        >
                          {section.contents.map((c, i) => (
                            <div key={i} style={{ paddingLeft: 8, borderLeft: `1px solid ${accent}40`, marginBottom: 2 }}>
                              {c}
                            </div>
                          ))}
                        </div>
                      </motion.div>
                    )}
                    {isHovered && isBoundary && (
                      <motion.div
                        initial={{ opacity: 0, height: 0 }}
                        animate={{ opacity: 1, height: "auto" }}
                        exit={{ opacity: 0, height: 0 }}
                        transition={{ duration: 0.15 }}
                        style={{ overflow: "hidden" }}
                      >
                        <div
                          style={{
                            marginTop: 6,
                            fontSize: 11,
                            color: colors.terracotta,
                            lineHeight: 1.5,
                          }}
                        >
                          {section.description}
                        </div>
                      </motion.div>
                    )}
                  </AnimatePresence>
                </motion.div>
              );
            })}
          </div>
        </div>

        {/* Right: Info panel */}
        <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
          {/* Cache indicator */}
          <div
            style={{
              background: colors.bgCard,
              borderRadius: 6,
              padding: "12px 14px",
            }}
          >
            <div
              style={{
                fontSize: 11,
                fontFamily: "var(--font-mono)",
                textTransform: "uppercase",
                letterSpacing: 1,
                color: colors.textSecondary,
                marginBottom: 8,
              }}
            >
              Cache Status
            </div>
            <div
              style={{
                display: "flex",
                alignItems: "center",
                gap: 8,
                marginBottom: 6,
              }}
            >
              <div
                style={{
                  width: 10,
                  height: 10,
                  borderRadius: "50%",
                  background: globalCacheDisabled ? colors.dynamicAccent : colors.staticAccent,
                }}
              />
              <span style={{ fontSize: 12, fontFamily: "var(--font-mono)", color: colors.text }}>
                {globalCacheDisabled ? "Global cache disabled" : "Global cache active"}
              </span>
            </div>
            <div style={{ fontSize: 11, color: colors.textSecondary, lineHeight: 1.5 }}>
              {globalCacheDisabled
                ? "MCP tool definitions are user-specific. They fragment the global cache into millions of unique prefixes."
                : `Static sections (~${staticTokens.toLocaleString()} tokens) are cached across all Claude Code users, sessions, and organizations.`}
            </div>
          </div>

          {/* Feature toggles */}
          <div
            style={{
              background: colors.bgCard,
              borderRadius: 6,
              padding: "12px 14px",
            }}
          >
            <div
              style={{
                fontSize: 11,
                fontFamily: "var(--font-mono)",
                textTransform: "uppercase",
                letterSpacing: 1,
                color: colors.textSecondary,
                marginBottom: 8,
              }}
            >
              Feature Toggles
            </div>

            {TOGGLE_FEATURES.map((feature) => {
              const isOn = features[feature.id];
              return (
                <div key={feature.id} style={{ marginBottom: 10 }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 3 }}>
                    <button
                      onClick={() => toggleFeature(feature.id)}
                      style={{
                        width: 36,
                        height: 20,
                        borderRadius: 10,
                        border: "none",
                        cursor: "pointer",
                        position: "relative",
                        background: isOn ? colors.terracotta : (isDark ? "#444" : "#ccc"),
                        transition: "background 0.2s",
                        flexShrink: 0,
                      }}
                    >
                      <motion.div
                        animate={{ x: isOn ? 16 : 0 }}
                        transition={{ type: "spring", stiffness: 500, damping: 30 }}
                        style={{
                          width: 16,
                          height: 16,
                          borderRadius: "50%",
                          background: "#fff",
                          position: "absolute",
                          top: 2,
                          left: 2,
                          boxShadow: "0 1px 2px rgba(0,0,0,0.2)",
                        }}
                      />
                    </button>
                    <span style={{ fontSize: 12, color: colors.text }}>{feature.label}</span>
                  </div>
                  <AnimatePresence>
                    {isOn && (
                      <motion.div
                        initial={{ opacity: 0, height: 0 }}
                        animate={{ opacity: 1, height: "auto" }}
                        exit={{ opacity: 0, height: 0 }}
                        transition={{ duration: 0.15 }}
                        style={{
                          fontSize: 10,
                          fontFamily: "var(--font-mono)",
                          color: colors.terracotta,
                          paddingLeft: 44,
                          lineHeight: 1.4,
                          overflow: "hidden",
                        }}
                      >
                        {feature.effect}
                      </motion.div>
                    )}
                  </AnimatePresence>
                </div>
              );
            })}
          </div>

          {/* 2^N explanation */}
          <div
            style={{
              background: isDark ? "rgba(217,119,87,0.08)" : "rgba(217,119,87,0.05)",
              border: `1px solid ${isDark ? "rgba(217,119,87,0.2)" : "rgba(217,119,87,0.15)"}`,
              borderRadius: 6,
              padding: "10px 12px",
              fontSize: 11,
              color: colors.textSecondary,
              lineHeight: 1.6,
            }}
          >
            <div style={{ fontWeight: 600, color: colors.terracotta, marginBottom: 4, fontFamily: "var(--font-mono)", fontSize: 10 }}>
              THE 2^N PROBLEM
            </div>
            Each conditional before the boundary doubles the number of unique global cache entries.
            {Object.values(features).filter(Boolean).length > 0 && (
              <span style={{ color: colors.terracotta, fontWeight: 600 }}>
                {" "}Current active toggles: {Object.values(features).filter(Boolean).length} = {Math.pow(2, Object.values(features).filter(Boolean).length)} cache variants.
              </span>
            )}
            {" "}Static sections are deliberately unconditional to prevent cache fragmentation.
          </div>

          {/* Legend */}
          <div
            style={{
              fontSize: 11,
              fontFamily: "var(--font-mono)",
              color: colors.textSecondary,
              lineHeight: 1.8,
            }}
          >
            <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 2 }}>
              <span style={{ display: "inline-block", width: 12, height: 12, borderRadius: 2, background: colors.staticBg, border: `1px solid ${colors.staticBorder}` }} />
              <span>Static (cached globally)</span>
            </div>
            <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 2 }}>
              <span style={{ display: "inline-block", width: 12, height: 12, borderRadius: 2, background: colors.boundaryBg, border: `1px solid ${colors.boundaryBorder}` }} />
              <span>Dynamic boundary</span>
            </div>
            <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 2 }}>
              <span style={{ display: "inline-block", width: 12, height: 12, borderRadius: 2, background: colors.dynamicBg, border: `1px solid ${colors.dynamicBorder}` }} />
              <span>Dynamic (per-session)</span>
            </div>
            <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
              <span style={{ display: "inline-block", width: 12, height: 12, borderRadius: 2, background: colors.uncachedBg, border: `1px solid ${colors.uncachedBorder}` }} />
              <span>Uncached (DANGEROUS)</span>
            </div>
          </div>

          {/* Hover hint */}
          <div
            style={{
              textAlign: "center",
              fontSize: 11,
              color: colors.textSecondary,
              fontStyle: "italic",
            }}
          >
            Hover on sections for details
          </div>
        </div>
      </div>

      {/* DANGEROUS naming convention callout */}
      <div
        style={{
          marginTop: 12,
          display: "grid",
          gridTemplateColumns: "1fr 1fr",
          gap: 12,
        }}
      >
        <div
          style={{
            background: colors.bgCard,
            borderRadius: 6,
            padding: "10px 12px",
            fontSize: 11,
            fontFamily: "var(--font-mono)",
            color: colors.textSecondary,
            lineHeight: 1.6,
          }}
        >
          <div style={{ fontWeight: 600, color: colors.staticAccent, marginBottom: 4 }}>
            systemPromptSection()
          </div>
          Safe. Content goes before the boundary. Cached globally. No runtime conditionals allowed.
        </div>
        <div
          style={{
            background: colors.bgCard,
            borderRadius: 6,
            padding: "10px 12px",
            fontSize: 11,
            fontFamily: "var(--font-mono)",
            color: colors.textSecondary,
            lineHeight: 1.6,
          }}
        >
          <div style={{ fontWeight: 600, color: colors.uncachedAccent, marginBottom: 4 }}>
            DANGEROUS_uncachedSystemPromptSection(_reason)
          </div>
          Cache-breaking. Requires a reason string. The _reason param is mandatory documentation in source.
        </div>
      </div>
    </div>
  );
}
