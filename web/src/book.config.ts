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
  {
    number: 4,
    title: 'A Plain-Language Guide to EU AI Act Compliance',
    epigraph: "The EU AI Act doesn't regulate AI. It regulates whose hands are on the steering wheel.",
    chapters: [9, 10, 11, 12, 13, 14, 15, 16, 17],
  },
];

export const chapters: ChapterConfig[] = [
  { number: 1, slug: 'ch1-architecture', title: 'The Problem & Core Abstractions', description: 'Why decisions vanish, the ledger-as-source-of-truth pattern, and immutable audit logs' },
  { number: 2, slug: 'ch2-state-conflicts', title: 'State & Conflict Detection', description: 'Derived state, the check algorithm, lightweight heuristics, and non-blocking validation' },
  { number: 3, slug: 'ch3-capture', title: 'Capturing Intent', description: 'Interactive and stdin capture modes, sensor architecture, and context enrichment' },
  { number: 4, slug: 'ch4-validation', title: 'Decision Validation', description: 'The check command, keyword-based conflict detection, and team resolution' },
  { number: 5, slug: 'ch5-figma-plugin', title: 'The Figma Plugin Architecture', description: 'Three-layer architecture, bridge pattern, message-based relay, and conflict checking integration' },
  { number: 6, slug: 'ch6-extensibility', title: 'Extensibility & Cross-Engine Context', description: 'REST APIs, MCP protocol for agents, context injection for AI models, sovereign stack integrations (Obsidian + MemPalace), and adding new sensors' },
  { number: 7, slug: 'ch7-epilogue', title: 'Application & Synthesis', description: 'Pattern-to-problem matching, design tradeoffs, evolution roadmap, and how to read this book again' },
  { number: 8, slug: 'appendix-practical-considerations', title: 'Appendix: Practical Considerations', description: 'Team scaling, multi-repository setups, ledger maintenance, security, and troubleshooting' },
  { number: 9,  slug: 'ch9-why-this-law-exists',    title: 'Why This Law Exists',                         description: 'The incidents that moved legislators, what the governance gap actually was, and why the AI Act is different from GDPR' },
  { number: 10, slug: 'ch10-who-it-applies-to',      title: 'Who It Applies To',                           description: 'Provider, Deployer, Importer — the role distinction that changes everything, and why "we just use AI" is no longer safe' },
  { number: 11, slug: 'ch11-the-risk-ladder',        title: 'The Risk Ladder',                             description: 'Four tiers from Unacceptable to Minimal, Annex III high-risk categories, and how to self-assess before the regulator does' },
  { number: 12, slug: 'ch12-article-12-logging',     title: 'Article 12 — The Black Box Requirement',      description: 'What logging means under the AI Act, why saving prompts to a database does not qualify, and what a compliant audit trail looks like' },
  { number: 13, slug: 'ch13-article-13-ifu',         title: 'Article 13 — The Instruction Manual',         description: 'Why every high-risk AI system needs an Instructions for Use document, who writes it, and what happens when it is missing' },
  { number: 14, slug: 'ch14-article-14-oversight',   title: 'Article 14 — The Human in the Loop',          description: 'What human oversight means legally vs operationally, how to prove a human was in control, and the documentation that survives an audit' },
  { number: 15, slug: 'ch15-article-27-fria',        title: 'Article 27 — The Rights Audit',               description: 'What a Fundamental Rights Impact Assessment requires, who must conduct one, and why public-sector deployers will be audited on this first' },
  { number: 16, slug: 'ch16-article-72-monitoring',  title: 'Article 72 — The Forever Job',                description: 'Post-market monitoring as an ongoing obligation, what changes trigger re-assessment, and how to build a compliance system rather than a compliance project' },
  { number: 17, slug: 'ch17-the-path-forward',       title: 'The Path Forward',                            description: 'Building compliance as a capability, the compliance stack by company size, and how to use the IRP Compliance Assessment as your starting point' },
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
