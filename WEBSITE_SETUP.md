# IRP Book Website Setup & Deployment

The website for the IRP book is ready to deploy. Here's how to get it live.

## Overview

- **Framework:** Astro 5.7 (static site generator)
- **Styling:** Tailwind CSS
- **Deployment:** Vercel (recommended), or any static host
- **Domain:** Configure in `web/astro.config.mjs` before deploying

## Quick Start (Local Development)

```bash
cd web

# Install dependencies (requires Node 18+ or Bun 1.0+)
npm install

# Start local dev server
npm run dev

# Visit http://localhost:3000
```

The site will rebuild on file changes (hot reload).

## Book Content

Book chapters are stored in `/book/` at the repo root:

```
/book/
  ch1-architecture.md
  ch2-state-conflicts.md
  ch3-capture.md
  ch4-validation.md
  ch5-figma-plugin.md
  ch6-extensibility.md
  ch7-epilogue.md
  appendix-practical-considerations.md
```

Site metadata is in `web/src/book.config.ts`:
- Parts (sections) and chapters
- Navigation and TOC
- Chapter descriptions

## Deployment to Vercel (5 minutes)

### Option 1: Vercel Dashboard (Easiest)

1. **Push to GitHub**
   ```bash
   git add .
   git commit -m "Add website infrastructure"
   git push origin main
   ```

2. **Connect to Vercel**
   - Go to https://vercel.com/new
   - Select this GitHub repo
   - **Root Directory:** Set to `web/`
   - Click Deploy

3. **Configure Custom Domain** (optional)
   - In Vercel dashboard, go to Settings > Domains
   - Add your domain (e.g., `irp.read`, `irp-book.com`, etc.)
   - Update nameservers or DNS records as Vercel instructs

4. **Update Site URL**
   - Edit `web/astro.config.mjs`
   - Change `site:` to your production URL
   - Commit and push (auto-redeploys)

### Option 2: CLI Deployment

```bash
# Install Vercel CLI
npm i -g vercel

# From repo root
cd web
vercel
```

Follow the prompts. Your site will be live at a `.vercel.app` domain immediately.

## After Deployment

### Update Configuration

Edit `web/astro.config.mjs`:
```javascript
export default defineConfig({
  site: 'https://your-domain.com/',  // Your actual domain
  // ... rest of config
});
```

This ensures canonical links and SEO are correct.

### Monitor Build Status

- Vercel deploys every push to main/master
- View builds at https://vercel.com/dashboard
- Rollback a deploy if needed (instant)

### Custom Domain

Once you own a domain:
1. In Vercel dashboard, add it under Settings > Domains
2. Update DNS records (CNAME or Nameservers) as instructed
3. SSL auto-provisions within minutes

## Alternative Hosts

The `dist/` folder is a static HTML site. Deploy to:

- **Netlify:** Connect GitHub, set build cmd to `npm run build`
- **GitHub Pages:** Push `dist/` to gh-pages branch
- **Cloudflare Pages:** Connect repo, auto-detects Astro
- **S3 + CloudFront:** Upload `dist/` to S3 bucket

## Features Included

✅ Dark mode (CSS variables, no JavaScript)
✅ Responsive design (mobile → desktop)
✅ Syntax highlighting (code blocks)
✅ Mermaid diagrams (flowcharts, state machines)
✅ Table of contents with navigation
✅ Previous/next chapter links
✅ Part dividers with epigraphs
✅ Fast static generation
✅ SEO optimized

## Updating the Book

When you update chapters in `/book/`:

1. Edit the markdown file
2. Commit and push to GitHub
3. Vercel auto-redeploys (usually <30 seconds)
4. Changes live at your domain

No rebuild command needed — it's automatic.

## Structure

```
irp-capture/
├── book/                     # Markdown chapters (source)
│   ├── ch1-architecture.md
│   ├── ... 7 chapters + appendix
│   └── appendix-practical-considerations.md
├── web/                      # Website (Astro project)
│   ├── src/
│   │   ├── pages/
│   │   │   ├── index.astro   # Landing page
│   │   │   └── [...slug].astro # Chapter pages (dynamic)
│   │   ├── layouts/
│   │   ├── components/
│   │   ├── styles/
│   │   ├── book.config.ts    # Metadata
│   │   └── content.config.ts # Content loader
│   ├── astro.config.mjs
│   ├── package.json
│   ├── vercel.json
│   └── README.md
└── ... (rest of IRP repo)
```

## Customization

### Change Colors/Fonts

Edit `web/src/styles/` and CSS variables in components:
```css
--color-terracotta: #d97706;
--color-charcoal: #1f2937;
--font-serif: 'Source Serif 4';
```

### Add Custom Components

Create React components in `web/src/components/`:
```astro
---
import MyComponent from '../components/MyComponent';
---

<MyComponent client:load />
```

### Update Landing Page

Edit `web/src/pages/index.astro` to customize hero, TOC, features, etc.

## Troubleshooting

**"Module not found" error:**
- Delete `node_modules/` and `package-lock.json`
- Run `npm install` again

**Styles not loading:**
- Restart dev server: Ctrl+C then `npm run dev`
- Clear browser cache

**Diagrams not rendering:**
- Use ` ```mermaid` (backticks, no language name)
- Check mermaid syntax (proper indentation, no trailing spaces)

**Build fails on deploy:**
- Check `web/astro.config.mjs` for syntax errors
- Verify `web/src/book.config.ts` chapter slugs match file names
- Check console output in Vercel dashboard for details

## Support

- **Astro Docs:** https://docs.astro.build
- **Tailwind Docs:** https://tailwindcss.com/docs
- **Vercel Docs:** https://vercel.com/docs

## Next Steps

1. ✅ Review site locally: `cd web && npm install && npm run dev`
2. ✅ Push to GitHub
3. ✅ Deploy to Vercel
4. ✅ Set custom domain (optional but recommended)
5. ✅ Update `book/` chapters and watch site auto-update

Your book is ready to share with the world.
