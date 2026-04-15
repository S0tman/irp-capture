# IRP Book Website

This is the website for the IRP (Intent Record Protocol) technical book. Built with Astro, React, and Tailwind CSS.

## Setup

### Prerequisites
- Node.js 18+ or Bun 1.0+
- npm or bun package manager

### Installation

```bash
# Install dependencies
npm install
# or
bun install

# Start the dev server
npm run dev
# or
bun run dev

# Build for production
npm run build
# or
bun run build

# Preview the production build
npm run preview
# or
bun run preview
```

The site will be available at `http://localhost:3000`

## Structure

```
web/
├── src/
│   ├── pages/
│   │   ├── index.astro          # Landing page
│   │   ├── [...slug].astro      # Dynamic chapter pages
│   │   └── changelog.astro      # Changelog page (reads CHANGELOG.md)
│   ├── layouts/
│   │   ├── BaseLayout.astro
│   │   └── ChapterLayout.astro
│   ├── components/              # React & Astro components
│   ├── styles/                  # Tailwind CSS config
│   ├── book.config.ts           # Book metadata (chapters, parts)
│   └── content.config.ts        # Content collection loader
├── public/                       # Static assets
├── astro.config.mjs             # Astro configuration
├── package.json
└── tsconfig.json
```

## Content

Book chapters are stored in the root `/book/` directory as markdown files:

```
book/
├── ch1-architecture.md
├── ch2-state-conflicts.md
├── ch3-capture.md
├── ch4-validation.md
├── ch5-figma-plugin.md
├── ch6-extensibility.md
├── ch7-epilogue.md
└── appendix-practical-considerations.md
```

The site automatically:
- Renders markdown to HTML with Shiki syntax highlighting
- Renders Mermaid diagrams as interactive SVGs (code fences with ` ```mermaid`)
- Generates static pages for each chapter
- Builds table of contents from book.config.ts
- Serves the repo CHANGELOG.md at `/changelog/`

## Configuration

### Update Book Metadata

Edit `src/book.config.ts`:
- Define parts (sections of the book)
- List chapters with titles and descriptions
- Customize chapter navigation

### Update Site Settings

Edit `astro.config.mjs`:
- Change `site` URL for SEO and canonical links
- Modify Markdown rendering options
- Add integrations as needed

### Customize Styling

Edit `src/styles/` and component files:
- Tailwind CSS configuration
- CSS variables for colors and fonts
- Dark mode is built-in via CSS variable switching

## Deployment

### Vercel (Recommended)

1. Push to GitHub
2. Connect to Vercel at https://vercel.com
3. Select this directory as the root
4. Vercel will auto-detect Astro and deploy

Environment variables (if needed):
- `SITE_URL` - Full URL of deployed site

### GitHub Pages

```bash
npm run build
# Deploy dist/ folder to gh-pages branch
```

### Other Platforms

Astro builds to a `dist/` folder. Deploy that folder to:
- Netlify
- Cloudflare Pages
- AWS S3 + CloudFront
- Any static hosting

## Features

- ✅ Dark mode (CSS variables, no JavaScript)
- ✅ Responsive design (mobile, tablet, desktop)
- ✅ Syntax highlighting (Shiki, dual-theme)
- ✅ Mermaid diagrams (flowcharts, architecture, etc.) — client-side rendering
- ✅ Table of contents with navigation
- ✅ Previous/next chapter links
- ✅ Part dividers with epigraphs
- ✅ Changelog page (auto-generated from repo CHANGELOG.md)
- ✅ Focus mode (hide sidebars for distraction-free reading)
- ✅ Fast page loads (static generation)
- ✅ SEO optimized

## Development

### Add a new chapter

1. Create `book/chN-slug.md` with markdown content
2. Add entry to `chapters` array in `src/book.config.ts`
3. Assign to a part in `parts` array
4. Site auto-generates the page and updates nav

### Customize components

- `src/layouts/ChapterLayout.astro` - Per-chapter template
- `src/components/Header.astro` - Navigation header
- `src/pages/index.astro` - Landing page

### Add interactive elements

Use React components:
```astro
---
import MyComponent from '../components/MyComponent';
---

<MyComponent client:load />
```

Astro renders them to HTML/CSS by default (no JS), but you can add `client:load` to hydrate with interactivity.

## Troubleshooting

### Port already in use
```bash
npm run dev -- --port 3001
```

### Content not updating
- Restart dev server: `Ctrl+C` then `npm run dev`
- Check content.config.ts glob pattern matches your files

### Mermaid diagrams not rendering
- Use ` ```mermaid` code fences in your markdown files
- Shiki (bundled with Astro 5) handles `mermaid` as a native language
- The client-side `mermaid.js` script in `ChapterLayout.astro` detects mermaid code blocks by content prefix and renders them as interactive SVG diagrams
- If diagrams show as raw code, check the browser console for mermaid rendering errors

## License

See LICENSE in the root directory.
