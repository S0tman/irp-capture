export interface PartConfig {
  number: number;
  title: string;
  epigraph: string;
  chapters: number[];
}

export interface ChapterConfig {
  number: number;
  slug: string;
  title: string;
  description: string;
}

export const parts: PartConfig[] = [
  {
    number: 1,
    title: 'Core Framework',
    epigraph: 'A decision is an artifact. Make it durable.',
    chapters: [1, 2, 3],
  },
  {
    number: 2,
    title: 'Integration & Extension',
    epigraph: 'Decisions live everywhere. So should their ledger.',
    chapters: [4, 5, 6],
  },
  {
    number: 3,
    title: 'Application & Practice',
    epigraph: 'The patterns are universal. Your decisions are not.',
    chapters: [7, 8],
  },
];

export const chapters: ChapterConfig[] = [
  { number: 1, slug: 'ch1-architecture', title: 'The Problem & Core Abstractions', description: 'Why decisions vanish, the ledger-as-source-of-truth pattern, and immutable audit logs' },
  { number: 2, slug: 'ch2-state-conflicts', title: 'State & Conflict Detection', description: 'Derived state, the check algorithm, lightweight heuristics, and non-blocking validation' },
  { number: 3, slug: 'ch3-capture', title: 'Capturing Intent', description: 'Interactive and stdin capture modes, sensor architecture, and context enrichment' },
  { number: 4, slug: 'ch4-validation', title: 'Decision Validation', description: 'The check command, keyword-based conflict detection, and team resolution' },
  { number: 5, slug: 'ch5-figma-plugin', title: 'The Figma Plugin Architecture', description: 'Three-layer architecture, bridge pattern, message-based relay, and conflict checking integration' },
  { number: 6, slug: 'ch6-extensibility', title: 'Extensibility & Cross-Engine Context', description: 'REST APIs, context injection for AI models, sovereign stack integrations (Obsidian + MemPalace), and adding new sensors' },
  { number: 7, slug: 'ch7-epilogue', title: 'Application & Synthesis', description: 'Pattern-to-problem matching, design tradeoffs, evolution roadmap, and how to read this book again' },
  { number: 8, slug: 'appendix-practical-considerations', title: 'Appendix: Practical Considerations', description: 'Team scaling, multi-repository setups, ledger maintenance, security, and troubleshooting' },
];

export function getPartForChapter(chapterNumber: number): PartConfig | undefined {
  return parts.find(p => p.chapters.includes(chapterNumber));
}

export function getChapterNumber(slug: string): number {
  const match = slug.match(/^(ch|appendix)/);
  if (match) {
    // For appendix, return 8; for chapters, parse the number
    if (slug.startsWith('appendix')) return 8;
    const numMatch = slug.match(/^ch(\d+)/);
    return numMatch ? parseInt(numMatch[1], 10) : 0;
  }
  return 0;
}

export function getAdjacentChapters(chapterNumber: number) {
  const idx = chapters.findIndex(c => c.number === chapterNumber);
  return {
    prev: idx > 0 ? chapters[idx - 1] : null,
    next: idx < chapters.length - 1 ? chapters[idx + 1] : null,
  };
}

export function isFirstChapterOfPart(chapterNumber: number): boolean {
  return parts.some(p => p.chapters[0] === chapterNumber);
}
