import { useState, useCallback, useEffect, useRef } from "react";
import { motion, AnimatePresence } from "framer-motion";

// --- Types ---

type MemoryType = "user" | "feedback" | "project" | "reference";

type PipelineStage =
  | "idle"
  | "prefetch"
  | "manifest"
  | "evaluate"
  | "select"
  | "inject";

interface MemoryFile {
  filename: string;
  name: string;
  description: string;
  type: MemoryType;
  age: string;
  selected: boolean;
}

interface QueryPreset {
  query: string;
  memories: MemoryFile[];
}

// --- Data ---

const memoryTypeColors: Record<MemoryType, { color: string; bg: string; label: string }> = {
  user: {
    color: "#60a5fa",
    bg: "rgba(96, 165, 250, 0.12)",
    label: "User",
  },
  feedback: {
    color: "#d97757",
    bg: "rgba(217, 119, 87, 0.12)",
    label: "Feedback",
  },
  project: {
    color: "#4ade80",
    bg: "rgba(74, 222, 128, 0.12)",
    label: "Project",
  },
  reference: {
    color: "#a78bfa",
    bg: "rgba(167, 139, 250, 0.12)",
    label: "Reference",
  },
};

const queryPresets: QueryPreset[] = [
  {
    query: "How do I set up the database?",
    memories: [
      {
        filename: "feedback_testing.md",
        name: "Testing Policy",
        description: "Integration tests must hit real DB, not mocks",
        type: "feedback",
        age: "3 days",
        selected: true,
      },
      {
        filename: "reference_drizzle.md",
        name: "Drizzle Setup",
        description: "Drizzle ORM config, migration commands, studio URL",
        type: "reference",
        age: "2 weeks",
        selected: true,
      },
      {
        filename: "user_role.md",
        name: "Developer Role",
        description: "Senior backend engineer, prefers concise explanations",
        type: "user",
        age: "1 month",
        selected: false,
      },
      {
        filename: "project_migration.md",
        name: "DB Migration",
        description: "Currently migrating from Prisma to Drizzle ORM",
        type: "project",
        age: "5 days",
        selected: true,
      },
      {
        filename: "feedback_schemas.md",
        name: "Schema Conventions",
        description: "Always use camelCase columns, snake_case table names",
        type: "feedback",
        age: "2 weeks",
        selected: false,
      },
      {
        filename: "reference_aws.md",
        name: "AWS Config",
        description: "RDS instance details, connection strings, IAM roles",
        type: "reference",
        age: "3 weeks",
        selected: false,
      },
      {
        filename: "project_sprint.md",
        name: "Current Sprint",
        description: "Auth module refactor, deadline March 15",
        type: "project",
        age: "1 week",
        selected: false,
      },
      {
        filename: "feedback_env.md",
        name: "Env Variables",
        description: "Never commit .env files, use vault for secrets",
        type: "feedback",
        age: "6 days",
        selected: false,
      },
    ],
  },
  {
    query: "What's the coding style?",
    memories: [
      {
        filename: "feedback_style.md",
        name: "Code Style",
        description: "Use Biome for formatting, no semicolons, 2-space indent",
        type: "feedback",
        age: "2 days",
        selected: true,
      },
      {
        filename: "feedback_testing.md",
        name: "Testing Policy",
        description: "Integration tests must hit real DB, not mocks",
        type: "feedback",
        age: "3 days",
        selected: true,
      },
      {
        filename: "feedback_naming.md",
        name: "Naming Conventions",
        description: "camelCase for functions, PascalCase for components/types",
        type: "feedback",
        age: "1 week",
        selected: true,
      },
      {
        filename: "user_role.md",
        name: "Developer Role",
        description: "Senior backend engineer, prefers concise explanations",
        type: "user",
        age: "1 month",
        selected: false,
      },
      {
        filename: "reference_drizzle.md",
        name: "Drizzle Setup",
        description: "Drizzle ORM config, migration commands, studio URL",
        type: "reference",
        age: "2 weeks",
        selected: false,
      },
      {
        filename: "project_migration.md",
        name: "DB Migration",
        description: "Currently migrating from Prisma to Drizzle ORM",
        type: "project",
        age: "5 days",
        selected: false,
      },
      {
        filename: "feedback_schemas.md",
        name: "Schema Conventions",
        description: "Always use camelCase columns, snake_case table names",
        type: "feedback",
        age: "2 weeks",
        selected: true,
      },
      {
        filename: "feedback_env.md",
        name: "Env Variables",
        description: "Never commit .env files, use vault for secrets",
        type: "feedback",
        age: "6 days",
        selected: false,
      },
    ],
  },
  {
    query: "Who is working on auth?",
    memories: [
      {
        filename: "project_sprint.md",
        name: "Current Sprint",
        description: "Auth module refactor, deadline March 15",
        type: "project",
        age: "1 week",
        selected: true,
      },
      {
        filename: "user_role.md",
        name: "Developer Role",
        description: "Senior backend engineer, prefers concise explanations",
        type: "user",
        age: "1 month",
        selected: true,
      },
      {
        filename: "project_team.md",
        name: "Team Assignments",
        description: "Alex on auth, Sara on billing, Dev on frontend",
        type: "project",
        age: "4 days",
        selected: true,
      },
      {
        filename: "reference_linear.md",
        name: "Linear Board",
        description: "Project tracker URL, sprint board, backlog link",
        type: "reference",
        age: "3 weeks",
        selected: false,
      },
      {
        filename: "feedback_testing.md",
        name: "Testing Policy",
        description: "Integration tests must hit real DB, not mocks",
        type: "feedback",
        age: "3 days",
        selected: false,
      },
      {
        filename: "reference_drizzle.md",
        name: "Drizzle Setup",
        description: "Drizzle ORM config, migration commands, studio URL",
        type: "reference",
        age: "2 weeks",
        selected: false,
      },
      {
        filename: "feedback_schemas.md",
        name: "Schema Conventions",
        description: "Always use camelCase columns, snake_case table names",
        type: "feedback",
        age: "2 weeks",
        selected: false,
      },
      {
        filename: "project_migration.md",
        name: "DB Migration",
        description: "Currently migrating from Prisma to Drizzle ORM",
        type: "project",
        age: "5 days",
        selected: false,
      },
    ],
  },
];

const pipelineStages: {
  id: PipelineStage;
  label: string;
  description: string;
}[] = [
  {
    id: "prefetch",
    label: "Prefetch fires",
    description: "Async, parallel to user query processing",
  },
  {
    id: "manifest",
    label: "Build manifest",
    description: "scanMemoryFiles reads all .md files, parses frontmatter (30 lines max)",
  },
  {
    id: "evaluate",
    label: "Sonnet evaluates",
    description: "Side-query receives manifest + user query + recently-used tools",
  },
  {
    id: "select",
    label: "Select relevant",
    description: "Sonnet returns up to 5 filenames via structured JSON output",
  },
  {
    id: "inject",
    label: "Inject with staleness",
    description: "Selected memories attached with age warnings for stale content",
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

export default function MemoryRecall({ className }: Props) {
  const isDark = useDarkMode();
  const [queryIndex, setQueryIndex] = useState(0);
  const [stage, setStage] = useState<PipelineStage>("idle");
  const [stageIndex, setStageIndex] = useState(-1);
  const [isAnimating, setIsAnimating] = useState(false);
  const abortRef = useRef(false);

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
    highlight: "#eda100",
  };

  const currentPreset = queryPresets[queryIndex];
  const selectedCount = currentPreset.memories.filter((m) => m.selected).length;

  const runPipeline = useCallback(async () => {
    if (isAnimating) return;
    abortRef.current = false;
    setIsAnimating(true);
    setStageIndex(-1);
    setStage("idle");

    const stages: PipelineStage[] = [
      "prefetch",
      "manifest",
      "evaluate",
      "select",
      "inject",
    ];
    const delays = [800, 1000, 1200, 800, 600];

    for (let i = 0; i < stages.length; i++) {
      if (abortRef.current) break;
      setStage(stages[i]);
      setStageIndex(i);
      await new Promise((r) => setTimeout(r, delays[i]));
    }

    if (!abortRef.current) {
      setIsAnimating(false);
    }
  }, [isAnimating]);

  const resetPipeline = useCallback(() => {
    abortRef.current = true;
    setIsAnimating(false);
    setStage("idle");
    setStageIndex(-1);
  }, []);

  const selectQuery = useCallback(
    (index: number) => {
      if (isAnimating) return;
      setQueryIndex(index);
      resetPipeline();
    },
    [isAnimating, resetPipeline]
  );

  const stageReached = (target: PipelineStage) => {
    const order: PipelineStage[] = [
      "prefetch",
      "manifest",
      "evaluate",
      "select",
      "inject",
    ];
    return order.indexOf(stage) >= order.indexOf(target);
  };

  return (
    <div className={className} style={{ fontFamily: "var(--font-serif)" }}>
      {/* Query selector */}
      <div
        style={{
          display: "flex",
          flexWrap: "wrap",
          alignItems: "center",
          gap: 10,
          marginBottom: 20,
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
            marginRight: 4,
          }}
        >
          Query:
        </div>
        {queryPresets.map((preset, i) => (
          <button
            key={i}
            onClick={() => selectQuery(i)}
            style={{
              padding: "6px 14px",
              borderRadius: 6,
              border: `1px solid ${
                queryIndex === i ? colors.terracotta : colors.cardBorder
              }`,
              background:
                queryIndex === i ? colors.terracottaBg : "transparent",
              color: queryIndex === i ? colors.terracotta : colors.textSecondary,
              fontSize: 12,
              fontFamily: "var(--font-mono)",
              cursor: "pointer",
              transition: "all 0.2s",
              fontWeight: queryIndex === i ? 600 : 400,
            }}
          >
            {preset.query}
          </button>
        ))}

        <div style={{ flex: 1, minWidth: 20 }} />

        <button
          onClick={isAnimating ? resetPipeline : runPipeline}
          style={{
            padding: "8px 20px",
            borderRadius: 8,
            border: "none",
            background: isAnimating ? colors.textSecondary : colors.terracotta,
            color: "#fff",
            fontSize: 13,
            fontWeight: 600,
            fontFamily: "var(--font-mono)",
            cursor: "pointer",
            transition: "background 0.2s",
          }}
        >
          {isAnimating ? "Reset" : "Run Recall"}
        </button>
      </div>

      {/* Simulated query */}
      <div
        style={{
          padding: "14px 20px",
          background: colors.surfaceBg,
          border: `1px solid ${colors.cardBorder}`,
          borderRadius: 10,
          marginBottom: 20,
          display: "flex",
          alignItems: "center",
          gap: 10,
        }}
      >
        <span
          style={{
            fontSize: 12,
            fontFamily: "var(--font-mono)",
            color: colors.textSecondary,
          }}
        >
          &gt;
        </span>
        <span
          style={{
            fontSize: 15,
            color: colors.text,
            fontWeight: 500,
          }}
        >
          {currentPreset.query}
        </span>
      </div>

      {/* Pipeline stages */}
      <div style={{ marginBottom: 20 }}>
        <div
          style={{
            display: "flex",
            gap: 6,
            overflow: "hidden",
          }}
        >
          {pipelineStages.map((s, i) => {
            const isReached = stageIndex >= i;
            const isCurrent = stageIndex === i && isAnimating;

            return (
              <motion.div
                key={s.id}
                style={{
                  flex: 1,
                  padding: "10px 12px",
                  borderRadius: 8,
                  border: `1px solid ${
                    isCurrent
                      ? colors.terracotta
                      : isReached
                        ? `${colors.terracotta}60`
                        : colors.cardBorder
                  }`,
                  background: isCurrent
                    ? colors.terracottaBg
                    : isReached
                      ? isDark
                        ? "rgba(217, 119, 87, 0.06)"
                        : "rgba(217, 119, 87, 0.04)"
                      : colors.cardBg,
                  transition: "all 0.3s",
                  opacity: isReached || stage === "idle" ? 1 : 0.4,
                }}
              >
                <div
                  style={{
                    fontSize: 11,
                    fontWeight: 700,
                    fontFamily: "var(--font-mono)",
                    color: isCurrent
                      ? colors.terracotta
                      : isReached
                        ? colors.terracotta
                        : colors.textSecondary,
                    marginBottom: 3,
                  }}
                >
                  {i + 1}. {s.label}
                </div>
                <div
                  style={{
                    fontSize: 10,
                    color: colors.textSecondary,
                    lineHeight: 1.4,
                  }}
                >
                  {s.description}
                </div>

                {isCurrent && (
                  <motion.div
                    style={{
                      marginTop: 6,
                      height: 2,
                      background: colors.terracotta,
                      borderRadius: 1,
                      transformOrigin: "left",
                    }}
                    initial={{ scaleX: 0 }}
                    animate={{ scaleX: 1 }}
                    transition={{
                      duration:
                        [0.8, 1.0, 1.2, 0.8, 0.6][i] * 0.9,
                      ease: "linear",
                    }}
                  />
                )}
              </motion.div>
            );
          })}
        </div>
      </div>

      {/* Memory files */}
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
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            marginBottom: 14,
          }}
        >
          <div
            style={{
              fontSize: 12,
              fontFamily: "var(--font-mono)",
              color: colors.textSecondary,
              textTransform: "uppercase",
              letterSpacing: "0.05em",
            }}
          >
            Memory Manifest ({currentPreset.memories.length} files)
          </div>

          {/* Memory type legend */}
          <div
            style={{
              display: "flex",
              gap: 12,
              fontSize: 11,
              fontFamily: "var(--font-mono)",
            }}
          >
            {(Object.keys(memoryTypeColors) as MemoryType[]).map((type) => (
              <span
                key={type}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 4,
                  color: memoryTypeColors[type].color,
                }}
              >
                <span
                  style={{
                    width: 8,
                    height: 8,
                    borderRadius: 2,
                    background: memoryTypeColors[type].color,
                  }}
                />
                {memoryTypeColors[type].label}
              </span>
            ))}
          </div>
        </div>

        <div
          style={{
            display: "flex",
            flexDirection: "column",
            gap: 6,
          }}
        >
          {currentPreset.memories.map((memory, i) => {
            const typeInfo = memoryTypeColors[memory.type];
            const isSelected = memory.selected;
            const showAsSelected = isSelected && stageReached("select");
            const showEvaluating =
              stageReached("evaluate") && !stageReached("select") && isAnimating;
            const showStaleness = isSelected && stageReached("inject");

            return (
              <motion.div
                key={memory.filename}
                layout
                style={{
                  padding: "10px 14px",
                  borderRadius: 8,
                  border: `1px solid ${
                    showAsSelected
                      ? typeInfo.color
                      : colors.cardBorder
                  }`,
                  background: showAsSelected
                    ? typeInfo.bg
                    : colors.surfaceBg,
                  transition: "all 0.4s",
                  position: "relative",
                  overflow: "hidden",
                }}
              >
                {/* Scanning effect */}
                {showEvaluating && (
                  <motion.div
                    style={{
                      position: "absolute",
                      top: 0,
                      left: 0,
                      right: 0,
                      bottom: 0,
                      background: `linear-gradient(90deg, transparent, ${typeInfo.bg}, transparent)`,
                      pointerEvents: "none",
                    }}
                    animate={{
                      x: ["-100%", "100%"],
                    }}
                    transition={{
                      duration: 1.5,
                      repeat: Infinity,
                      delay: i * 0.1,
                    }}
                  />
                )}

                <div
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 10,
                  }}
                >
                  {/* Type badge */}
                  <span
                    style={{
                      fontSize: 10,
                      fontFamily: "var(--font-mono)",
                      fontWeight: 600,
                      padding: "2px 6px",
                      borderRadius: 4,
                      background: typeInfo.bg,
                      color: typeInfo.color,
                      minWidth: 52,
                      textAlign: "center",
                    }}
                  >
                    {typeInfo.label}
                  </span>

                  {/* Filename */}
                  <span
                    style={{
                      fontSize: 12,
                      fontFamily: "var(--font-mono)",
                      fontWeight: 600,
                      color: showAsSelected ? typeInfo.color : colors.text,
                      transition: "color 0.3s",
                    }}
                  >
                    {memory.filename}
                  </span>

                  {/* Staleness badge */}
                  <AnimatePresence>
                    {showStaleness && (
                      <motion.span
                        initial={{ opacity: 0, scale: 0.8 }}
                        animate={{ opacity: 1, scale: 1 }}
                        exit={{ opacity: 0, scale: 0.8 }}
                        style={{
                          fontSize: 10,
                          fontFamily: "var(--font-mono)",
                          padding: "2px 8px",
                          borderRadius: 4,
                          background: isDark
                            ? "rgba(237, 161, 0, 0.15)"
                            : "rgba(237, 161, 0, 0.1)",
                          color: colors.highlight,
                          fontWeight: 600,
                        }}
                      >
                        {memory.age}
                      </motion.span>
                    )}
                  </AnimatePresence>

                  {/* Selected indicator */}
                  <AnimatePresence>
                    {showAsSelected && (
                      <motion.span
                        initial={{ opacity: 0, scale: 0.5 }}
                        animate={{ opacity: 1, scale: 1 }}
                        exit={{ opacity: 0, scale: 0.5 }}
                        style={{ marginLeft: "auto" }}
                      >
                        <svg
                          width="16"
                          height="16"
                          viewBox="0 0 16 16"
                          fill="none"
                        >
                          <path
                            d="M3 8.5L6.5 12L13 4"
                            stroke={typeInfo.color}
                            strokeWidth="2"
                            strokeLinecap="round"
                            strokeLinejoin="round"
                          />
                        </svg>
                      </motion.span>
                    )}
                  </AnimatePresence>
                </div>

                {/* Description */}
                <div
                  style={{
                    fontSize: 11,
                    color: colors.textSecondary,
                    marginTop: 4,
                    marginLeft: 62,
                  }}
                >
                  {memory.description}
                </div>
              </motion.div>
            );
          })}
        </div>

        {/* Result summary */}
        <AnimatePresence>
          {stageReached("inject") && !isAnimating && (
            <motion.div
              initial={{ opacity: 0, y: 10 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0 }}
              transition={{ duration: 0.3, delay: 0.1 }}
              style={{
                marginTop: 14,
                padding: "12px 16px",
                borderRadius: 8,
                background: colors.terracottaBg,
                border: `1px solid rgba(217, 119, 87, 0.3)`,
              }}
            >
              <div
                style={{
                  fontSize: 13,
                  fontFamily: "var(--font-mono)",
                  fontWeight: 600,
                  color: colors.terracotta,
                  marginBottom: 4,
                }}
              >
                {selectedCount} memories recalled
              </div>
              <div
                style={{
                  fontSize: 12,
                  color: colors.textSecondary,
                  lineHeight: 1.5,
                }}
              >
                Selected memories injected as relevant_memories attachment with
                staleness warnings. Memories older than yesterday include age
                caveat: "Before recommending from memory, verify against current
                code."
              </div>
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    </div>
  );
}
