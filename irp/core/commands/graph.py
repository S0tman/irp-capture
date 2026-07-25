"""IRP graph export — renders decision ledger as an interactive 3D force graph.

    irp export graph --output GRAPH.html
    irp export graph --from 2026-05-01 --to 2026-05-31
    irp export graph --project irp-capture

Design rules:
  - No new schema. Reads .irp/ledger.jsonl only.
  - No LLM calls. No inference. Deterministic mapping only.
  - Edges derived from IRP id references in 'why' fields (regex only).
  - Single self-contained HTML — 3d-force-graph (Three.js/WebGL) via CDN.
  - Drag to orbit the globe. Scroll to zoom. Click to inspect.
  - Animated particles travel along provenance edges.
  - Date/project filters dim out-of-range nodes without removing them.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import dynamics
from store import read_ledger

IRP_ID_RE = re.compile(r"\bIRP-\d{4}-\d{2}-\d{2}-\d{3}\b")

_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>IRP Decision Graph</title>
<script src="https://unpkg.com/3d-force-graph@1/dist/3d-force-graph.min.js"></script>
<style>
/* ─── IRP graph identity ───────────────────────────────────────────────────
   The layout work made the graph say something. This makes the INTERFACE say
   it too. What was here before was default dark-mode tooling: the stock system
   font stack, the framework-default blue-grey palette, uniform rounded pill
   buttons, boxed inputs. It looked like a dev tool because componentwise it was
   one, and that is what read as generic no matter what the nodes did.

   The line held throughout: differentiate on EXPRESSIVE surfaces (type,
   palette, rules, motion, spatial metaphor), stay conventional on INTERACTIVE
   affordances (search behaves like search, active states are obvious, targets
   stay large). Nielsen 8 licenses the first, Nielsen 4 protects the second.

   Two committed themes, because every competing graph demo is dark and an
   archival light one is genuinely uncommon. Both carry the same single accent:
   brass, reserved for foundation weight and never spent on decoration.
   Type is system-resident on purpose. Embedding a webfont would either add a
   network dependency or bloat the base64, and this file's contract is to be one
   self-contained HTML.  */

:root {
  --serif: "Iowan Old Style","Palatino Linotype",Palatino,"Hoefler Text","Times New Roman",Times,serif;
  --ui: "Avenir Next",Avenir,"Segoe UI Variable Text","Segoe UI",Inter,system-ui,sans-serif;
  --mono: ui-monospace,"SF Mono",Menlo,Consolas,monospace;
  --t: 140ms cubic-bezier(.2,.6,.2,1);

  --ink: #0b0c0f;          /* the ground */
  --surface: #14161c;      /* cards, dropdowns */
  --surface-2: #0e1014;
  --rule: rgba(214,196,160,.15);   /* warm hairlines, not cold borders */
  --rule-firm: rgba(214,196,160,.50);   /* component boundaries: 1.4.11 wants 3:1 */
  --text: #e9e3d6;         /* bone, not blue-grey */
  --text-dim: #b0a99a;     /* raised to keep a step above --text-faint */
  --text-faint: #948d80;   /* was #6d675d: 3.23:1 on the card surface, failed AA */
  --brass: #c19b60;
  --brass-text: #c19b60;   /* 7.3:1 on ink, clears AAA as is */
  --brass-lit: #e6ca97;
  --on-brass: #14110a;
  --label: rgba(233,227,214,.94);
  --label-plate: rgba(11,12,15,.62);   /* the label's own ground */
  --label-shadow: rgba(0,0,0,.92);
  --shadow: 0 10px 34px rgba(0,0,0,.62);
}

:root[data-theme="light"] {
  --ink: #f2ece0;          /* safe paper */
  --surface: #fbf7ee;
  --surface-2: #ece5d6;
  --rule: rgba(58,48,32,.20);
  --rule-firm: rgba(58,48,32,.62);      /* component boundaries: 1.4.11 wants 3:1 */
  --text: #221f18;
  --text-dim: #4f4941;
  --text-faint: #6b665c;   /* was #8b8474: 3.16:1 on paper, failed AA */
  --brass: #8a6a2f;
  --brass-text: #6f5320;   /* graphic brass measured 4.07:1 as text; this is 5.8:1 */
  --brass-lit: #6b5122;
  --on-brass: #fbf7ee;
  --label: rgba(34,31,24,.96);
  --label-plate: rgba(248,244,235,.88);
  --label-shadow: rgba(255,255,255,.9);
  --shadow: 0 10px 30px rgba(70,58,38,.18);
}

*{box-sizing:border-box;margin:0;padding:0}
.theme-swap *,.theme-swap *::before,.theme-swap *::after{transition:none !important}
body{font-family:var(--ui);background:var(--ink);color:var(--text);height:100vh;display:flex;flex-direction:column;overflow:hidden;-webkit-font-smoothing:antialiased}

/* z-index must beat .hint below. Both used to be 10, and .hint comes later in
   the DOM, so the hint bar painted over the top of the search dropdown and ate
   the clicks on its first result: the list looked like it only became clickable
   from the second row down. The dropdown lives inside this stacking context, so
   raising the header is what lifts it clear. */
header{padding:13px 22px;border-bottom:1px solid var(--rule);display:flex;align-items:center;gap:14px;row-gap:9px;flex-wrap:wrap;flex-shrink:0;z-index:30;position:relative}

/* Serif wordmark with a struck brass mark: a record's masthead, not an app bar. */
h1{font:400 16px var(--serif);letter-spacing:.005em;color:var(--text);white-space:nowrap;display:flex;align-items:center;gap:9px}
h1::before{content:"";width:7px;height:7px;background:var(--brass);flex-shrink:0}
.meta{font:10px var(--mono);letter-spacing:.07em;text-transform:uppercase;color:var(--text-faint);white-space:nowrap}

.legend{display:flex;gap:15px;margin-left:auto;align-items:center}
/* The header wraps to a second row rather than squeezing, so narrow embeds
   (the book iframe is ~760px) keep their canvas. As width drops, the
   decorative chrome yields its row before the functional controls do: group
   captions first, then the legend, then the timestamp. These must sit after the
   rules they override: a media query adds no specificity, so source order
   decides. */
@media (max-width:1460px){.grouplabel{display:none}}
@media (max-width:1200px){.legend{display:none}}
@media (max-width:760px){.meta{display:none}}
.li{display:flex;align-items:center;gap:6px;font:10px var(--mono);letter-spacing:.05em;color:var(--text-faint);text-transform:uppercase}
.dot{width:8px;height:8px;border-radius:50%;flex-shrink:0}

/* Controls are set text with an underline for state, not pills. The pill button
   was the single loudest "default component" tell in the whole interface. The
   active state stays unmistakable (accent colour plus a 2px rule) so trading
   the box for a rule costs no clarity. */
.views{display:flex;gap:11px;align-items:center;margin-left:16px}
.vbtn{font:500 10px var(--ui);letter-spacing:.1em;text-transform:uppercase;color:var(--text-faint);background:none;border:none;border-bottom:2px solid transparent;border-radius:0;padding:3px 1px 2px;cursor:pointer;user-select:none;transition:color var(--t),border-color var(--t)}
.vbtn:hover{color:var(--text-dim)}
.vbtn.on{color:var(--brass-text);border-bottom-color:var(--brass)}
.vbtn.off{opacity:.55;cursor:default}   /* disabled: exempt from 1.4.3, still legible */
.vbtn.off:hover{color:var(--text-faint)}

/* Fixed slot. This badge is empty in structure/foundations and populated in
   lineage/impact, and letting it size to its content changed the header's total
   width on every view switch, which pushed the search field onto a second row
   and back. Reserving the space keeps the chrome still while views change. */
.seedbadge{font:10px var(--mono);letter-spacing:.04em;color:var(--brass-text);margin-left:7px;display:inline-block;min-width:132px;white-space:nowrap}
@media (max-width:1100px){.seedbadge{min-width:0}}
.grouplabel{font:9px var(--mono);letter-spacing:.15em;text-transform:uppercase;color:var(--text-faint);margin:0 3px 0 0}
#modebar{margin-left:14px;padding-left:14px;border-left:1px solid var(--rule)}

/* Ruled input rather than a boxed one: same behaviour, none of the chrome. */
.search{position:relative;margin-left:auto}
#q{width:158px;font:11px var(--mono);letter-spacing:.02em;color:var(--text);background:none;border:none;border-bottom:1px solid var(--rule-firm);border-radius:0;padding:5px 2px;outline:none;transition:width var(--t),border-color var(--t)}
#q:focus{border-bottom-color:var(--brass);width:250px}
#q::placeholder{color:var(--text-faint)}
#hits{position:absolute;top:100%;right:0;margin-top:7px;width:344px;max-height:290px;overflow-y:auto;background:var(--surface);border:1px solid var(--rule-firm);border-top:2px solid var(--brass);border-radius:2px;display:none;z-index:200;box-shadow:var(--shadow)}
#hits.on{display:block}
.hit{padding:8px 12px;border-bottom:1px solid var(--rule);cursor:pointer}
.hit:last-child{border-bottom:none}
.hit:hover,.hit.sel{background:var(--surface-2)}
.hit-id{font:600 9px var(--mono);color:var(--brass-text);letter-spacing:.06em}
.hit-what{font:12px var(--serif);color:var(--text);line-height:1.45;margin-top:3px}
.hit-none{padding:10px 12px;font:12px var(--serif);font-style:italic;color:var(--text-dim)}
.hit-count{padding:6px 12px;font:9px var(--mono);color:var(--text-faint);text-transform:uppercase;letter-spacing:.1em;border-bottom:1px solid var(--rule);background:var(--surface-2)}

/* Editorial, not a toolbar tooltip strip. It leads with how to READ the graph,
   in plain words, and only then how to move it. */
.hint{font:italic 12.5px var(--serif);color:var(--text-dim);padding:9px 22px;border-bottom:1px solid var(--rule);z-index:10;position:relative;line-height:1.5}
.hint b{font-style:normal;font-weight:400;color:var(--text);font-family:var(--ui);font-size:11px;letter-spacing:.02em}
#aspect-note b{color:var(--brass-text)}
#aspect-note i{font-family:var(--mono);font-style:normal;font-size:10px;text-transform:uppercase;letter-spacing:.08em}
#aspect-dismiss{font:9px var(--mono);letter-spacing:.1em;text-transform:uppercase;color:var(--text-faint);cursor:pointer;border-bottom:1px solid var(--rule-firm);margin-left:6px}
#aspect-dismiss:hover{color:var(--text)}

.main{display:flex;flex:1;overflow:hidden;position:relative}
#graph{flex:1;cursor:grab;position:relative}
#graph:active{cursor:grabbing}
#graph canvas{display:block}
.node-label{position:absolute;pointer-events:none;transform:translate(-50%,-165%);font:600 9px var(--mono);letter-spacing:.04em;color:var(--label);white-space:nowrap;background:var(--label-plate);padding:1px 4px;border-radius:1px}

/* The record card. Rules and set text, with the brass edge struck along the top
   the way a ledger page is headed. A long `why` must scroll inside the card
   rather than run off the screen; bounding the height also keeps the card from
   covering the canvas, which is what made it feel stuck: clicks meant for the
   background landed on it. */
#overlay{position:fixed;display:none;background:var(--surface);border:1px solid var(--rule-firm);border-top:2px solid var(--brass);border-radius:2px;padding:15px 17px;max-width:390px;max-height:min(70vh,520px);overflow-y:auto;overscroll-behavior:contain;z-index:100;pointer-events:none;box-shadow:var(--shadow)}
#overlay::-webkit-scrollbar{width:8px}
#overlay::-webkit-scrollbar-thumb{background:var(--rule-firm);border-radius:0}
#overlay::-webkit-scrollbar-track{background:transparent}
#overlay.locked{pointer-events:auto;border-color:var(--brass);animation:press 190ms cubic-bezier(.2,.7,.2,1)}
/* The stamp: on lock, the card presses in. The only motion in the interface
   that is not a camera move, and it marks the moment a person committed to
   reading a specific decision. */
@keyframes press{from{transform:scale(.985);opacity:.4}to{transform:scale(1);opacity:1}}

.did{font:400 12px var(--serif);color:var(--brass-text);letter-spacing:.06em}
.dwhat{font:400 16px var(--serif);color:var(--text);line-height:1.36;margin-top:2px}
.dwhy{font:12.5px var(--ui);color:var(--text-dim);line-height:1.62}
.dmeta{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-top:2px}
/* Outlined, not filled. Filled pills read as framework chips; an outline reads
   as a stamp and lets confidence keep its own colour without shouting. */
.badge{font:600 9px var(--mono);letter-spacing:.09em;text-transform:uppercase;padding:2px 6px;border:1px solid currentColor;border-radius:2px;background:none}
.bh{color:#7fa06a}
.bm{color:#c8974a}
.bl{color:#bd6a5c}
.bu{color:var(--text-faint)}
:root[data-theme="light"] .bh{color:#4a6b38}
:root[data-theme="light"] .bm{color:#8a5f14}
:root[data-theme="light"] .bl{color:#8f3a2c}
.tag{font:9px var(--mono);letter-spacing:.05em;padding:2px 6px;border:1px solid var(--rule);border-radius:2px;background:none;color:var(--text-faint)}
.dsec{font:9px var(--mono);letter-spacing:.14em;text-transform:uppercase;color:var(--text-faint);border-top:1px solid var(--rule);padding-top:9px;margin-top:11px}
.dsrc{font:11px var(--mono);color:var(--text-dim)}
.refs{display:flex;flex-direction:column;gap:4px}
.rl{font:11px var(--mono);color:var(--brass-text);cursor:pointer;text-decoration:none;border-bottom:1px solid var(--rule-firm);align-self:flex-start;transition:border-color var(--t)}
.rl:hover{border-bottom-color:var(--brass)}
/* The honest trust line, on the record itself rather than buried in docs. */
.attest{font:9px var(--mono);letter-spacing:.11em;text-transform:uppercase;color:var(--text-faint);border-top:1px solid var(--rule);padding-top:9px;margin-top:12px;line-height:1.7}

/* Mobile: the record is a sheet, not a floating card. */
@media (max-width:640px){
  #overlay{left:10px !important;right:10px;top:auto !important;bottom:10px;
           max-width:none;width:auto;max-height:min(58vh,420px)}
  /* Drop the pointer instructions: they describe a mouse, and the sentence that
     explains how to READ the graph is the part that still applies. */
  .hint b{display:none}
  .hint{font-size:12px;padding:8px 14px}
  header{padding:11px 14px;gap:10px}
  #modebar{margin-left:0;padding-left:0;border-left:none}
  .search{margin-left:0;width:100%}
  #q{width:100%}
  #q:focus{width:100%}
  #hits{width:auto;left:0;right:0}
  footer{padding:8px 14px;font-size:10px;gap:10px}
}
footer{padding:8px 22px;border-top:1px solid var(--rule);font:11px var(--ui);color:var(--text-faint);flex-shrink:0;z-index:10;position:relative;display:flex;justify-content:space-between;align-items:center;gap:18px}
footer em{font-family:var(--serif);font-size:12px}
.fbtns{display:flex;gap:16px;flex-shrink:0}
#toggle-labels,#toggle-theme{font:9px var(--mono);letter-spacing:.12em;text-transform:uppercase;color:var(--text-faint);cursor:pointer;text-decoration:none;user-select:none;border-bottom:1px solid transparent;transition:color var(--t),border-color var(--t)}
#toggle-labels:hover,#toggle-theme:hover{color:var(--text);border-bottom-color:var(--rule-firm)}
</style>
</head>
<body>
<header>
  <h1>IRP Decision Graph</h1>
  <span class="meta">__GENERATED_AT__ &middot; __DECISION_COUNT__ decisions &middot; __EDGE_COUNT__ provenance edges__FILTER_BADGE__</span>
  <div class="views">
    <span class="vbtn" id="v-structure" onclick="setView('structure')">structure</span>
    <span class="vbtn" id="v-foundations" onclick="setView('foundations')">foundations</span>
    <span class="vbtn" id="v-lineage" onclick="setView('lineage')">lineage</span>
    <span class="vbtn" id="v-impact" onclick="setView('impact')">impact</span>
    <span class="seedbadge" id="seed-badge"></span>
  </div>
  <div class="views" id="modebar">
    <span class="grouplabel">view</span>
    <span class="vbtn" id="m-classic" onclick="setMode('classic')">classic</span>
    <span class="vbtn" id="m-bedrock" onclick="setMode('bedrock')">bedrock</span>
    <span class="grouplabel">slice</span>
    <span class="vbtn" id="s-all" onclick="setSlice('all')">all</span>
    <span class="vbtn" id="s-core" onclick="setSlice('core')">core</span>
    <span class="vbtn" id="s-path" onclick="setSlice('path')">path</span>
  </div>
  <div class="search">
    <input id="q" type="text" autocomplete="off" spellcheck="false" placeholder="Search  /  or  &#8984;K">
    <div id="hits"></div>
  </div>
  <div class="legend">
    <div class="li"><div class="dot" style="background:#b08d57"></div>most load-bearing</div>
    <div class="li"><div class="dot" style="background:#3a4150"></div>rests on others</div>
    <div class="li">deep&nbsp;=&nbsp;foundational &middot; high&nbsp;=&nbsp;recent</div>
  </div>
</header>
<div class="hint">Depth is weight. The deeper a decision sits, the more of this record rests on it, and the lines are the references one decision made to another. <b>Hover to read &middot; click to keep open &middot; drag to look around &middot; scroll to zoom</b><span id="aspect-note" hidden> &nbsp;<b>This window is very wide, so the stack reads small. A narrower window, or the <i>core</i> slice, gives a closer view.</b> <a id="aspect-dismiss" onclick="dismissAspectNote()">dismiss</a></span></div>
<div class="main">
  <div id="graph"></div>
</div>
<div id="overlay"></div>
<footer><span><em>Appended, never rewritten.</em> &nbsp;Every line above was read from a decision's own <em>why</em>, in <code>.irp/ledger.jsonl</code>.</span><span class="fbtns"><a id="toggle-theme" onclick="setTheme(theme==='dark'?'light':'dark')">Paper</a><a id="toggle-labels" onclick="toggleLabels()">Hide IDs</a></span></footer>

<script>
const decisions = __DECISIONS_JSON__;
const IRP_RE = /\bIRP-\d{4}-\d{2}-\d{2}-\d{3}\b/g;
const idSet = new Set(decisions.map(d => d.id));
const byId = Object.fromEntries(decisions.map(d => [d.id, d]));

// De-generic pass: retire the green/amber/red confidence ramp as the primary
// hue. Colour now reads the foundation lens (slate to brass), the load-bearing
// bedrock lit in the one reserved accent. Confidence moves to alpha, freeing
// hue to carry a single meaning the jury can read at a glance.
const CONF_COLOR = { high: '#22c55e', medium: '#f59e0b', low: '#ef4444' }; // retained for reference, no longer the primary encoding
// The scene is WebGL, so it cannot read the CSS custom properties the interface
// uses. These mirror them. Both themes carry the same single accent, brass,
// spent only on foundation weight.
const THEMES = {
  dark: {
    bg: '#0b0c0f',
    low: [122, 116, 104], high: [193, 155, 96], locked: '#f7edd6',
    dimAlpha: 0.32, outOfRange: 0.38,
    edge: 'rgba(206,196,178,.50)',  edgeArrow: 'rgba(222,212,192,.68)',
    quiet: 'rgba(190,198,212,.50)', quietArrow: 'rgba(190,198,212,.62)',
    faint: 'rgba(150,158,172,.30)', faintArrow: 'rgba(150,158,172,.38)',
    particle: '#f1e7d0',
    walk: 'rgba(96,165,250,.62)', walkArrow: 'rgba(96,165,250,.9)', walkParticle: '#60a5fa',
  },
  light: {
    bg: '#f2ece0',
    low: [104, 96, 82], high: [138, 106, 47], locked: '#3a2f18',
    dimAlpha: 0.60, outOfRange: 0.66,
    edge: 'rgba(60,54,44,.64)',    edgeArrow: 'rgba(48,42,34,.76)',
    quiet: 'rgba(64,60,52,.64)',   quietArrow: 'rgba(52,48,42,.74)',
    faint: 'rgba(80,76,66,.34)',   faintArrow: 'rgba(80,76,66,.42)',
    particle: '#5c4a26',
    walk: 'rgba(41,98,180,.6)', walkArrow: 'rgba(41,98,180,.85)', walkParticle: '#2962b4',
  },
};
let theme = 'dark';
const T = () => THEMES[theme];

const CONF_ALPHA = { high: 1.0, medium: 0.82, low: 0.62 };
function _lerp(a, b, t) { return Math.round(a + (b - a) * t); }
function foundationColor(p, alpha) {
  const t = Math.max(0, Math.min(1, Math.sqrt(p)));
  const lo = T().low, hi = T().high;
  return `rgba(${_lerp(lo[0],hi[0],t)},${_lerp(lo[1],hi[1],t)},${_lerp(lo[2],hi[2],t)},${alpha})`;
}

// ── IRP Dynamics: typed provenance edges + provenance lenses ───────────────
// Edges are typed server-side (depends_on / gates / mentions) and embedded
// here, so the browser walks exactly the graph the CLI scored. Only
// depends_on carries probability. Gates and mentions stay visible but are
// excluded from the walk: that is what stops a foundation's "gates 002"
// forward reference and 002's "builds on 001" back reference from forming a
// two-node cycle that circulates probability forever and inflates both.
// Never derive probability from 3D positions. The force layout is a
// rendering artifact and says nothing about dependence.
const typedEdges = __EDGES_JSON__;
const WALK_REL = 'depends_on';
const ALPHA = 0.85, EPS = 1e-9, MAX_IT = 200;
let view = '__INITIAL_VIEW__' || 'structure';
let seedId = '__INITIAL_SEED__' || null;

// Random walk with restart. Transitions are uniform (1/outdegree): influence
// is not confidence, and attestation proves properties of the record, not its
// importance. Mirrors dynamics.personalized_pagerank server-side.
function pagerank(seed, reverse) {
  const ids = decisions.map(d => d.id);
  const n = ids.length;
  const idx = new Map(ids.map((id, i) => [id, i]));
  const out = Array.from({ length: n }, () => []);
  typedEdges.forEach(e => {
    if (e.relation !== WALK_REL) return;
    const s = idx.get(reverse ? e.target : e.source);
    const t = idx.get(reverse ? e.source : e.target);
    if (s !== undefined && t !== undefined) out[s].push(t);
  });
  const tele = new Array(n).fill(0);
  if (seed && idx.has(seed)) tele[idx.get(seed)] = 1;
  else for (let i = 0; i < n; i++) tele[i] = 1 / n;
  let r = tele.slice();
  for (let it = 0; it < MAX_IT; it++) {
    const nxt = tele.map(t => (1 - ALPHA) * t);
    let dangling = 0;
    for (let i = 0; i < n; i++) {
      if (!out[i].length) { dangling += r[i]; continue; }
      const share = ALPHA * r[i] / out[i].length;
      for (const j of out[i]) nxt[j] += share;
    }
    if (dangling) for (let i = 0; i < n; i++) nxt[i] += ALPHA * dangling * tele[i];
    let delta = 0;
    for (let i = 0; i < n; i++) delta += Math.abs(nxt[i] - r[i]);
    r = nxt;
    if (delta < EPS) break;
  }
  const total = r.reduce((a, b) => a + b, 0) || 1;
  const scores = {};
  ids.forEach((id, i) => scores[id] = r[i] / total);
  return scores;
}

// Node SIZE is structural centrality (stable across lenses). Node GLOW is the
// active lens probability. Confidence stays its own colour dimension.
const foundationScores = pagerank(null, false);
const maxFound = Math.max(1e-12, ...Object.values(foundationScores));
let lensScores = {};
let maxLens = 1e-12;

// Decisions ordered by foundation weight, ties broken oldest first. Used by both
// the strata heights and the label budget, so the two can never disagree.
const foundationOrder = decisions.map(d => d.id).sort((a, b) => {
  const d0 = (foundationScores[b] || 0) - (foundationScores[a] || 0);
  return d0 !== 0 ? d0
    : String((byId[a] || {}).timestamp || '').localeCompare(String((byId[b] || {}).timestamp || ''));
});

// ── Nielsen #7, flexibility and efficiency of use ──────────────────────────
// Two ways to read the same ledger, because two different people arrive here.
//   classic: the familiar free-floating force graph. Nothing is pinned, colour
//            is confidence, you orbit and explore. Good for a first look.
//   bedrock: the opinionated view. Height IS foundation rank, so the thing
//            everything rests on sits at the base. Faster to read once you know
//            what you are looking for.
// Neither is a mode the other can be mistaken for, and switching is one click
// with no state lost.
let mode = 'bedrock';    // 'classic' | 'bedrock'
let slice = 'all';       // 'all' | 'core' | 'path'
let idleMs = 4500;

function computeLens() {
  if (view === 'foundations') lensScores = foundationScores;
  else if (view === 'lineage' && seedId) lensScores = pagerank(seedId, false);
  else if (view === 'impact' && seedId) lensScores = pagerank(seedId, true);
  else lensScores = {};
  const vals = Object.values(lensScores);
  maxLens = vals.length ? Math.max(1e-12, ...vals) : 1e-12;
}

function linksForView() {
  const rev = (view === 'impact');
  return typedEdges.map(e => ({
    source: (rev && e.relation === WALK_REL) ? e.target : e.source,
    target: (rev && e.relation === WALK_REL) ? e.source : e.target,
    relation: e.relation
  }));
}

function hexToRgba(hex, a) {
  const h = hex.replace('#', '');
  return `rgba(${parseInt(h.substring(0,2),16)},${parseInt(h.substring(2,4),16)},${parseInt(h.substring(4,6),16)},${a})`;
}

// Classic: the baseline encoding, kept faithfully. Colour is confidence, size is
// confidence in structure view, and the lens reads as glow.
function nodeColorClassic(d) {
  if (d.id === lockedId) return '#D3D3D3';
  if (d.dimmed) return '#2d3748';
  const base = CONF_COLOR[d.confidence] || '#6b7280';
  if (searchHits && !searchHits.has(d.id)) return hexToRgba(base, 0.07);
  if (view === 'structure') return base;
  const p = (lensScores[d.id] || 0) / maxLens;
  return hexToRgba(base, 0.12 + 0.88 * Math.sqrt(p));
}

function nodeValClassic(d) {
  if (d.dimmed) return 1;
  if (view === 'structure') return d.confidence === 'high' ? 6 : d.confidence === 'medium' ? 4 : 3;
  return 2 + 14 * Math.sqrt((foundationScores[d.id] || 0) / maxFound);
}

function nodeColorBedrock(d) {
  if (d.id === lockedId) return T().locked;
  // Filter dim wins over search dim. `dimmed` means "outside the range you
  // asked for", a more permanent statement than "not what you just typed".
  if (d.dimmed) return foundationColor(0, T().outOfRange);
  const pFound = (foundationScores[d.id] || 0) / maxFound;
  const alpha = CONF_ALPHA[d.confidence] || 0.6;
  if (searchHits && !searchHits.has(d.id)) return foundationColor(pFound, T().dimAlpha);
  if (view === 'lineage' || view === 'impact') {
    if (!seedId) return foundationColor(pFound, alpha);
    const pl = (lensScores[d.id] || 0) / maxLens;
    return pl > 0 ? foundationColor(0.4 + 0.6 * pl, 0.4 + 0.6 * Math.sqrt(pl))
                  : foundationColor(pFound, Math.max(T().dimAlpha, alpha * 0.12));
  }
  // structure / foundations: colour reads the foundation lens, always.
  return foundationColor(pFound, alpha);
}

function nodeValBedrock(d) {
  if (d.dimmed) return 1;
  // Size = foundation weight, stable across lenses. The lens read now lives in
  // vertical position (gravity), not in glow, so size can stay one honest thing.
  return 3 + 13 * Math.sqrt((foundationScores[d.id] || 0) / maxFound);
}

// Single dispatch point, so every caller stays mode-agnostic.
function nodeColor(d) { return mode === 'classic' ? nodeColorClassic(d) : nodeColorBedrock(d); }
function nodeVal(d)   { return mode === 'classic' ? nodeValClassic(d)   : nodeValBedrock(d); }

function isWalk(l) { return view === 'structure' || l.relation === WALK_REL; }

// Typed edges become three visible behaviours, not one on/off opacity.
// depends_on is a taut structural strut with warm particles falling toward the
// antecedent it rests on; gates is a cool neutral barrier line; mentions is a
// faint hairline. This makes the typed model the visible star.
// Alphas here are the final rendered values because linkOpacity is pinned to 1
// below. Left at its 0.2 default the library multiplies it into every link
// colour, which is what reduced these struts to near-invisible hairlines.
// Alphas here are the final rendered values because linkOpacity is pinned to 1
// below. Left at its 0.2 default the library multiplies it into every link
// colour, which is what reduced these struts to near-invisible hairlines.
// Brass is reserved for ONE meaning: how load-bearing a decision is. Spending
// it on the struts too made the accent ambiguous, so the edges are neutral
// slate and stay quiet. Edge TYPE is carried by form (width, and whether
// anything travels along it), never by competing for the accent hue.
const EDGE_FORM = {
  depends_on: { width: 1.5, parts: 3, key: 'edge'  },
  gates:      { width: 1.1, parts: 0, key: 'quiet' },
  mentions:   { width: 0.7, parts: 0, key: 'faint' },
};
// Classic keeps the baseline blue walk / grey non-walk distinction, per theme.
// Opacity is lifted from the library default of 0.2, which rendered these
// near-invisible in the original too: a bug, not a look worth preserving.
function edgeStyle(l) {
  const t = T();
  if (mode === 'classic') {
    return isWalk(l)
      ? { color: t.walk,  width: 1.5, arrow: t.walkArrow,  parts: 3 }
      : { color: t.quiet, width: 1.0, arrow: t.quietArrow, parts: 0 };
  }
  const f = EDGE_FORM[l.relation] || EDGE_FORM.mentions;
  return { color: t[f.key], width: f.width, arrow: t[f.key + 'Arrow'], parts: f.parts };
}
function particleColor() { return mode === 'classic' ? T().walkParticle : T().particle; }

// ── Slice: what is on screen at all ────────────────────────────────────────
// At real ledger scale the answer to "is this readable" is not only layout, it
// is how much you choose to show. 135 decisions at once is a structure you can
// admire and not much else, so the slice control narrows it to something a
// person can actually work with, without ever changing the underlying ranks.
let visibleIds = new Set(decisions.map(d => d.id));

// Ancestors and descendants of a seed, walking only depends_on (the acyclic
// relation), so a "path" slice is the seed's real provenance and its real reach.
function pathClosure(seed) {
  const up = new Map(), down = new Map();
  typedEdges.forEach(e => {
    if (e.relation !== WALK_REL) return;
    if (!up.has(e.source)) up.set(e.source, []);
    up.get(e.source).push(e.target);        // source rests on target
    if (!down.has(e.target)) down.set(e.target, []);
    down.get(e.target).push(e.source);      // target carries source
  });
  const out = new Set([seed]);
  for (const adj of [up, down]) {
    const queue = [seed];
    while (queue.length) {
      const cur = queue.shift();
      for (const nxt of (adj.get(cur) || [])) {
        if (!out.has(nxt)) { out.add(nxt); queue.push(nxt); }
      }
    }
  }
  return out;
}

// Proportional, so "core" always actually narrows. A fixed 24 was a no-op on an
// 18-decision ledger: the button appeared broken because everything was already
// in the core.
const CORE_N = Math.max(6, Math.min(24, Math.ceil(decisions.length * 0.35)));
function computeSlice() {
  if (slice === 'core') {
    visibleIds = new Set(foundationOrder.slice(0, Math.min(CORE_N, foundationOrder.length)));
  } else if (slice === 'path' && seedId) {
    visibleIds = pathClosure(seedId);
  } else {
    visibleIds = new Set(decisions.map(d => d.id));
  }
}

// Heights come from each decision's rank in the WHOLE ledger, never from the
// slice, so narrowing the view never silently re-ranks anything.
function visibleData() {
  const ns = nodes.filter(n => visibleIds.has(n.id));
  const ls = linksForView().filter(l => {
    const s = typeof l.source === 'object' ? l.source.id : l.source;
    const t = typeof l.target === 'object' ? l.target.id : l.target;
    return visibleIds.has(s) && visibleIds.has(t);
  });
  return { nodes: ns, links: ls };
}

function refreshChrome() {
  ['structure', 'foundations', 'lineage', 'impact'].forEach(v => {
    const el = document.getElementById('v-' + v);
    if (el) el.classList.toggle('on', v === view);
  });
  const badge = document.getElementById('seed-badge');
  if (badge) {
    // Short form, so the populated badge fits the reserved slot above.
    badge.textContent = (view === 'lineage' || view === 'impact')
      ? (seedId ? 'seed ' + shortId(seedId) : 'click a node to seed')
      : '';
  }
  ['classic', 'bedrock'].forEach(m => {
    const el = document.getElementById('m-' + m);
    if (el) el.classList.toggle('on', m === mode);
  });
  ['all', 'core', 'path'].forEach(s => {
    const el = document.getElementById('s-' + s);
    if (el) el.classList.toggle('on', s === slice);
  });
  // Heuristics 1 and 9: a control that cannot work right now should say so and
  // say what to do about it, rather than silently doing nothing when clicked.
  const pathBtn = document.getElementById('s-path');
  if (pathBtn) {
    pathBtn.classList.toggle('off', !seedId);
    pathBtn.title = seedId
      ? 'provenance and reach of ' + seedId
      : 'click a decision first: a path needs a starting point';
  }
  const coreBtn = document.getElementById('s-core');
  if (coreBtn) coreBtn.title = 'the ' + Math.min(CORE_N, decisions.length) +
    ' most load-bearing decisions, of ' + decisions.length;
}

function applyView() {
  computeLens();
  computeSlice();
  Graph.graphData(visibleData());
  Graph.nodeColor(nodeColor);
  Graph.nodeVal(nodeVal);
  refreshChrome();
}

function setView(next) {
  view = next;
  if ((next === 'lineage' || next === 'impact') && !seedId) {
    // Awaiting a seed. Clear the previous lens rather than leaving its glow on
    // screen under a different lens name, which would show stale probability.
    lensScores = {};
    maxLens = 1e-12;
    computeSlice();
    Graph.graphData(visibleData());
    Graph.nodeColor(nodeColor);
    Graph.nodeVal(nodeVal);
    refreshChrome();
    return;
  }
  applyView();
}

// ── Mode and slice switching ───────────────────────────────────────────────
function setMode(next) {
  if (next === mode) return;
  mode = next;
  userTookOver = false;
  applyMode();
}

function setSlice(next) {
  userTookOver = false;
  // A path slice without a seed would blank the canvas, so it falls back to all
  // and the chrome says why rather than leaving the user staring at nothing.
  slice = (next === 'path' && !seedId) ? 'all' : next;
  applyView();
  framed = false;
  frameHero(true);
}

computeLens();
const links = linksForView();
const nodes = decisions.map(d => ({ ...d }));

// ── Floating overlay ──────────────────────────────────────────────────────
let lockedId = null;
let overlayLocked = false;
let cursorX = 0, cursorY = 0;
const overlay = document.getElementById('overlay');

window.addEventListener('mousemove', e => {
  cursorX = e.clientX; cursorY = e.clientY;
  if (!overlayLocked && overlay.style.display === 'block') positionOverlay();
});

function positionOverlay() {
  // Below this width the stylesheet pins the record to the bottom of the screen,
  // so leave its position alone entirely.
  if (window.innerWidth <= 640) {
    overlay.style.left = '';
    overlay.style.top = '';
    return;
  }
  const margin = 14;
  const ow = overlay.offsetWidth || Math.min(390, window.innerWidth - margin * 2);
  const oh = overlay.offsetHeight || 200;
  let left = cursorX + 18;
  let top  = cursorY - 18;
  if (left + ow > window.innerWidth - margin) left = cursorX - ow - 18;
  if (top + oh > window.innerHeight - margin) top = window.innerHeight - oh - margin;
  // Clamp both axes. Without the left clamp a tap near the right edge put the
  // card at a negative x and its first characters were cut off the screen.
  left = Math.max(margin, Math.min(left, window.innerWidth - ow - margin));
  top  = Math.max(margin, top);
  overlay.style.left = left + 'px';
  overlay.style.top  = top  + 'px';
}

function esc(s) {
  return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

// Drops the "IRP-" prefix: every node in this graph is an IRP record, so the
// prefix is 26% of the label width carrying no information. Narrower labels
// collide less, which means more decisions keep their name. The overlay still
// shows the full id.
function shortId(id) {
  const m = (id||'').match(/IRP-\d{4}-(\d{2})-(\d{2})-(\d+)/);
  return m ? m[1] + m[2] + '-' + m[3] : id;
}
function badgeClass(c) { return {high:'bh',medium:'bm',low:'bl'}[c]||'bu'; }

function buildOverlayContent(d) {
  const refs = [...new Set((d.why||'').match(IRP_RE)||[])].filter(r=>idSet.has(r)&&r!==d.id);
  return `
    <div class="did">${esc(d.id)}</div>
    <div class="dwhat">${esc(d.what||'')}</div>
    <div class="dmeta">
      <span class="badge ${badgeClass(d.confidence)}">${d.confidence||'unknown'}</span>
      ${(d.tags||[]).map(t=>`<span class="tag">${esc(t)}</span>`).join('')}
      ${d.timestamp?`<span style="font-size:10px;color:#4b5563;margin-left:auto">${esc(String(d.timestamp).slice(0,10))}</span>`:''}
    </div>
    ${d.why?`<div><div class="dsec">Why</div><div class="dwhy">${esc(d.why)}</div></div>`:''}
    ${d.source?`<div><div class="dsec">Source</div><div class="dsrc">${esc(d.source)}</div></div>`:''}
    ${refs.length?`<div><div class="dsec">References</div><div class="refs">${
      refs.map(r=>`<span class="rl" onclick="event.stopPropagation();focusNode('${r}')">${r}</span>`).join('')
    }</div></div>`:''}
    <div class="attest">Appended, never rewritten${d.timestamp?` &middot; recorded ${esc(String(d.timestamp).slice(0,10))}`:''}<br>
      ${overlayLocked ? 'Click the decision again, or the background, to close'
                      : 'Click to keep this open and follow its references'}</div>
  `;
}

function showOverlay(d, locked) {
  overlayLocked = locked;
  overlay.innerHTML = buildOverlayContent(d);
  overlay.style.display = 'block';
  overlay.className = locked ? 'locked' : '';
  if (!locked) positionOverlay();
}

function clearOverlay() {
  lockedId = null;
  overlayLocked = false;
  overlay.style.display = 'none';
  overlay.className = '';
  Graph.nodeColor(nodeColor);
}

// ── 3D Graph ──────────────────────────────────────────────────────────────
const graphEl = document.getElementById('graph');

const Graph = ForceGraph3D({ controlType: 'orbit' })(graphEl)
  .backgroundColor(THEMES.dark.bg)
  .showNavInfo(false)   // stock nav copy; the hint bar says this in our own words
  .graphData({ nodes, links })

  // Nodes — no library tooltip; overlay handles all interaction
  .nodeLabel(() => '')
  .nodeColor(nodeColor)
  .nodeVal(nodeVal)
  .nodeOpacity(0.92)

  // Links / provenance edges. In a lens view the non-walk relations
  // (gates, mentions) stay visible but are dimmed and carry no particles,
  // because they carry no probability.
  .linkColor(l => edgeStyle(l).color)
  .linkWidth(l => edgeStyle(l).width)
  .linkOpacity(1)   // alpha lives in linkColor; see EDGE
  .linkDirectionalArrowLength(l => l.relation === 'depends_on' ? 4 : 0)
  .linkDirectionalArrowRelPos(1)
  .linkDirectionalArrowColor(l => edgeStyle(l).arrow)
  .linkDirectionalParticles(l => edgeStyle(l).parts)
  // Wider than the strut it travels inside, and much brighter, so the motion
  // actually reads. Cool white, not warm: the travelling mark must not read as
  // the brass accent either.
  .linkDirectionalParticleWidth(3.0)
  .linkDirectionalParticleColor(() => '#d7e0ee')
  .linkDirectionalParticleSpeed(0.0075)

  // Interactions
  .onNodeClick((node, event) => {
    event && event.stopPropagation();
    lastNodeClick = Date.now();
    // In a seeded lens, clicking re-seeds the walk on that decision.
    if (view === 'lineage' || view === 'impact') {
      seedId = node.id;
      applyView();
    }
    if (lockedId === node.id) {
      clearOverlay();
    } else {
      lockedId = node.id;
      Graph.nodeColor(nodeColor);
      positionOverlay();
      showOverlay(node, true);
      const dist = 120;
      const distRatio = 1 + dist / Math.hypot(node.x||1, node.y||1, node.z||1);
      Graph.cameraPosition(
        { x: (node.x||0) * distRatio, y: (node.y||0) * distRatio, z: (node.z||0) * distRatio },
        node,
        800
      );
    }
  })
  .onBackgroundClick(() => clearOverlay());

// ── Inertia + idle auto-rotation ─────────────────────────────────────────────
const controls = Graph.controls();
controls.enableDamping = true;
controls.dampingFactor = 0.08;
// Starts still: the first frame has to read as a settled cross-section, not a
// spinning ball. A slow idle drift then takes over after a few seconds of no
// interaction, which adds life without turning the piece back into the generic
// spinning graph. Any interaction stops it, and it only resumes once the viewer
// has gone quiet again. It orbits the stack centre set by frameHero, so the
// bedrock stays anchored at the base throughout.
controls.autoRotate = false;
controls.autoRotateSpeed = 0.32;   // gentle; the default 2.0 is the cliche

let idleTimer;
let nodeHovered = false;
let hoveredId = null;   // the label layout always keeps this one named
function resetIdle() {
  controls.autoRotate = false;
  clearTimeout(idleTimer);
  idleTimer = setTimeout(() => { if (!nodeHovered) controls.autoRotate = true; }, idleMs);
}
function claimCamera() { userTookOver = true; resetIdle(); }
graphEl.addEventListener('pointerdown', claimCamera);
graphEl.addEventListener('wheel', claimCamera);
// Moving the cursor over the canvas counts as being present: it holds the drift
// off without claiming the camera. Throttled, since this fires constantly.
let lastMove = 0;
graphEl.addEventListener('pointermove', () => {
  const now = Date.now();
  if (now - lastMove > 200) { lastMove = now; resetIdle(); }
});
resetIdle();   // arm it on load, so the drift begins a few seconds in

// ── Foundation gravity ────────────────────────────────────────────────────
// Pull each node to a target height set by its foundation score, so the
// load-bearing decisions sink to the base and the graph stratifies into a
// bedrock-to-canopy cross-section. Only the acyclic depends_on structure feeds
// the score (gates/mentions never did). Position renders the lens; it is never
// an input to it.
// Height is the foundation score: bedrock at the base, canopy up top.
// Vertical range has to stay clearly larger than the horizontal spread, or the
// stack reads as a flat fan and the "what everything rests on" point is lost.
// Widening the canopy is close to free: on a landscape viewport the camera
// distance is set by the VERTICAL extent, so horizontal spread can grow until
// halfH/aspect approaches halfV before the camera pulls back at all. That buys
// screen separation between canopy nodes, which is what lets every label fit.
// Tuned to sit just inside that limit: halfH lands near halfV*aspect, the point
// where widening further starts pulling the camera back and costs more screen
// separation than the extra width buys.
// Both scale with ledger size: 18 decisions and 135 decisions need very
// different room. STRATA grows so ranked neighbours stay distinguishable rather
// than compressing into a line; SPREAD stays inside halfV*aspect so the camera
// distance keeps being set by the vertical extent (see frameHero).
const STRATA = Math.min(2000, Math.max(820, nodes.length * 11));
const SPREAD = Math.min(620, Math.max(490, nodes.length * 4));
// Height is the foundation lens as a PERCENTILE RANK, not the raw score.
// Raw scores are brutally long-tailed: measured on a 135-decision ledger there
// were only 22 distinct score values and 102 decisions tied at the very bottom,
// so mapping height from the raw number piled most of the graph into a few dense
// bands and the stratification stopped meaning anything. Ranking spreads
// decisions evenly through the full height, which is what keeps the strata
// readable at real ledger scale.
//
// Magnitude is not lost, it moves: node SIZE still comes from the raw score, so
// height answers "where does this sit in the order" and size answers "by how
// much". Ranking also dissolves the tie problem, since tied decisions take
// consecutive ranks instead of sharing one height. Ties break by timestamp, so
// the older decision sits deeper.
const stratumById = (() => {
  const ordered = nodes.slice().sort((a, b) => {
    const d = (foundationScores[b.id] || 0) - (foundationScores[a.id] || 0);
    return d !== 0 ? d : String(a.timestamp || '').localeCompare(String(b.timestamp || ''));
  });
  const out = {};
  const last = Math.max(1, ordered.length - 1);
  ordered.forEach((node, i) => { out[node.id] = (i / last - 0.5) * STRATA; });
  return out;
})();
function stratumY(n) {
  const y = stratumById[n.id];
  return Number.isFinite(y) ? y : 0;
}

// Seed every position deterministically, then pin the height with a force.
// Two separate guarantees, because relying on either alone was the bug:
//   1. Seeding means the composition is already correct at frame zero. A
//      simulation tick is not required, so a throttled or briefly hidden tab
//      cannot leave the stack unstratified.
//   2. A force re-pins the height on every tick. Setting `fy` alone was not
//      enough: the pin is only honoured while the engine is running, so once
//      it cooled the strata collapsed back into a blob.
// Wide on X, SHALLOW on Z: a cross-section slab, not a spherical cloud. This is
// what finally made the strata readable. In a deep cloud, perspective trades
// depth against height, so two decisions in genuinely different strata could
// project onto the same screen row (measured: 2px apart while their scores were
// 0.074 and 0.167). Flattening depth means screen height reads as foundation
// weight, full stop, and it looks like an architectural section rather than a
// ball. Rotation still gives parallax; it no longer scrambles the reading.
// Seeded on a golden-angle spiral so nothing starts clumped. Depth is left free
// rather than pinned to a thin slab: pinning it looked right in principle (a
// cross-section) but left only the X axis to spread along, so nodes bunched and
// more labels were lost than the flattening saved.
nodes.forEach((n, i) => {
  const golden = i * 2.399963;
  const r = SPREAD * Math.sqrt((i + 0.5) / nodes.length);
  n.y = n.fy = stratumY(n);
  n.x = Math.cos(golden) * r;
  n.z = Math.sin(golden) * r;
});

function strataForce() {
  let ns = [];
  // Guards on mode itself rather than trusting removal: passing null to
  // d3Force('strata', null) did not detach it, so in classic the heights stayed
  // pinned to the exact strata span and the layout never relaxed.
  const f = () => {
    if (mode !== 'bedrock') return;
    for (const n of ns) {
      n.y = stratumY(n); n.vy = 0;
      // Soft wall at SPREAD: keeps the cross-section a bounded composition
      // instead of one that slowly inflates past its own frame.
      const r = Math.hypot(n.x || 0, n.z || 0);
      if (r > SPREAD) {
        const k = SPREAD / r;
        n.x *= k; n.z *= k; n.vx = 0; n.vz = 0;
      }
    }
  };
  f.initialize = _ns => { ns = _ns; };
  return f;
}
// Forces are not configured here: applyMode() below owns them, so classic and
// bedrock cannot end up sharing half of each other's physics.

// Frame the settled stack at a gentle 3/4 hero angle: depth for the
// cross-section, and it pulls the crowded canopy apart in screen space. The
// distance is derived from the data extent rather than from zoomToFit, which
// divides by the viewport and poisons the camera with NaN when the canvas has
// not been sized yet (that NaN camera was why nothing rendered at all).
let framed = false;
let viewW = 0, viewH = 0;   // the size actually handed to the renderer

// True once the library has actually built its internal layout and started
// ticking. Reheating before that point is fatal: the reheat flips the engine to
// "running" while the layout object does not exist yet, and the next animation
// frame calls tick() on undefined and takes the whole render loop down (the
// symptom is a black canvas with only the DOM labels painted). It never
// reproduced under a throttled render loop, because there the fatal frame never
// arrives.
// Two signals, because neither alone is sufficient: onEngineTick proves the
// layout exists but does not fire in every build, and the timer is a floor that
// is comfortably past the library's first refresh.
// Set the moment the viewer takes the camera. After that, nothing auto-frames:
// the layout settling, a timer, or a window resize must never snap the view back
// while someone is reading, which is what made it feel like it "reverted to a
// default mode". Explicit view changes (mode, slice, theme) reset this, because
// there the viewer is asking for a fresh look.
let userTookOver = false;
let booted = false;
Graph.onEngineTick(() => { booted = true; });
setTimeout(() => { booted = true; }, 1500);
function safeReheat() {
  if (!booted) return;   // the first layout pass is about to run on its own
  try { Graph.d3ReheatSimulation(); } catch (e) { /* layout not ready; harmless */ }
}
function frameHero(force) {
  if (!force && userTookOver) return;   // the viewer owns the camera now
  if (framed && !force) return;
  if (!viewW || !viewH) return;  // wait until the renderer has a real size
  framed = true;
  // Frame what is actually on screen, so narrowing to a slice reframes to it
  // instead of holding the whole ledger's extents and leaving it tiny.
  const shown = nodes.filter(n => visibleIds.has(n.id));
  const set = shown.length ? shown : nodes;
  const ys = set.map(n => n.y);
  const lo = Math.min(...ys), hi = Math.max(...ys);
  const cy = (lo + hi) / 2, span = Math.max(1, hi - lo);
  // Frame the real extents against the real field of view, treating height and
  // width as separate constraints. Fitting the bounding sphere instead pushed
  // the camera much too far back on a wide window: the stack shrank into the
  // middle third of the frame, which also crowds the canopy in screen space and
  // costs labels that would otherwise fit.
  const halfV = Math.max(1, ...set.map(n => Math.abs((n.y || 0) - cy)));
  // Worst case at any rotation angle, since the idle drift orbits the Y axis.
  const halfH = Math.max(1, ...set.map(n => Math.hypot(n.x || 0, n.z || 0)));
  const fov = ((Graph.camera() && Graph.camera().fov) || 50) * Math.PI / 180;
  const t = Math.tan(fov / 2);
  const aspect = Math.max(0.35, viewW / viewH);
  // The `+ halfH` term is perspective, and leaving it out was clipping the top
  // of the composition. Framing on the extents alone assumes every node sits at
  // the centre plane, but the nearest ones are up to halfH closer to the camera,
  // so their offsets are magnified (a node 490 nearer at D~1100 is scaled ~1.8x)
  // and they leave the frame. Solving at the NEAR plane instead of the centre
  // plane is what makes the whole stack fit at any rotation.
  const theta = -0.44;   // ~25 degrees off dead-on
  // Applied with no transition on purpose. A tweened move depends on the
  // animation loop actually running, and the very first frame is the one that
  // has to read as an anchored cross-section, not a camera still travelling.
  const place = (dist, th) => Graph.cameraPosition(
    { x: dist * Math.sin(th), y: cy + span * 0.06, z: dist * Math.cos(th) },
    { x: 0, y: cy, z: 0 },
    0
  );

  // Measured across a full turn, not derived. Three closed-form attempts were
  // each wrong about something (viewport aspect, then near-node magnification,
  // then the tilt from looking slightly down), and measuring at only the current
  // angle is no better, because the idle drift keeps turning the composition: a
  // fit that holds now fails a second later.
  //
  // So the test is the honest one. Try a distance, sample the projection at
  // angles all the way round, and accept it only when every visible node stays
  // inside the frame at every angle. The radial bound in strataForce is what
  // keeps this convergent. The margin covers the ID label, drawn above its node
  // and therefore clipped before the node is.
  // Moving the camera is not enough to measure against it: three.js only
  // recomputes the camera's world matrix on render, and graph2ScreenCoords
  // projects through that matrix. Without this the fitter reads the PREVIOUS
  // camera every iteration, never sees its own correction, and runs away to
  // absurd distances while still reporting a clipped node. This one line is what
  // made the whole measured approach work.
  const syncCamera = () => {
    const cam = Graph.camera();
    if (cam) { cam.updateProjectionMatrix(); cam.updateMatrixWorld(true); }
  };

  // Framed from the composition's BOUNDS, not from a measurement of where the
  // nodes happen to be right now. This is the fix that finally holds, and it
  // works because the layout is bounded by construction: height can never exceed
  // STRATA and radius is clamped to SPREAD by strataForce. Those are constants,
  // so the distance is deterministic and cannot be thrown off by when it is
  // called.
  //
  // Everything else tried here failed on timing or on stale state. Closed-form
  // attempts from measured extents were wrong three times (aspect, then
  // near-node magnification, then the downward tilt). Measuring the projection
  // synchronously is invalid, because cameraPosition moves the camera while
  // OrbitControls applies its ORIENTATION on the next render. And zoomToFit
  // frames whatever bounding box exists at the instant it runs, which on load is
  // not the settled one, so it left the camera far too close.
  //
  // The + halfH term is perspective: the nearest nodes sit that much closer than
  // the centre plane, so their offsets are magnified. Solving at the near plane
  // covers every rotation angle at once. The 1.15 is measured headroom, verified
  // clipping-free at 16 angles round a full turn.
  const halfVb = Math.max(halfV, STRATA / 2);
  const halfHb = Math.max(halfH, SPREAD);
  // On a very wide window the vertical extent is what binds, so the piece ends
  // up small with unused width either side. Trimming the headroom there is the
  // middle ground: it fills more of the frame, and the horizontal slack absorbs
  // the risk. Verified clipping-free at 16 angles at both settings.
  const headroom = aspect > 2.0 ? 1.06 : 1.15;
  const D = (Math.max(halfVb / t, halfHb / (t * aspect)) + halfHb) * headroom;
  place(D, theta);
  syncCamera();
}
// Reframe on settle, not just once: the first frame is computed while the
// layout is still moving, so the extents it used are provisional.
Graph.onEngineStop(() => { if (userTookOver) return; framed = false; frameHero(); });
setTimeout(frameHero, 1200);
setTimeout(frameHero, 3000);

// All colour accessors in one place, so a mode change and a theme change reach
// the scene by the same path and cannot drift apart.
function restyle() {
  Graph.nodeColor(nodeColor).nodeVal(nodeVal)
    .linkColor(l => edgeStyle(l).color)
    .linkWidth(l => edgeStyle(l).width)
    .linkDirectionalArrowColor(l => edgeStyle(l).arrow)
    .linkDirectionalParticles(l => edgeStyle(l).parts)
    .linkDirectionalParticleColor(particleColor);
}

// Theme switching restyles only. It deliberately does NOT re-commit the data or
// reframe: changing how the record looks must not disturb where you were
// reading, which is also why the camera and the layout are left alone.
function setTheme(next) {
  theme = (next === 'light') ? 'light' : 'dark';
  const root = document.documentElement;
  root.classList.add('theme-swap');
  root.setAttribute('data-theme', theme);
  // Two frames: one for the swap to paint, one before transitions come back.
  requestAnimationFrame(() => requestAnimationFrame(() => root.classList.remove('theme-swap')));
  Graph.backgroundColor(T().bg);
  restyle();
  const el = document.getElementById('toggle-theme');
  if (el) el.textContent = theme === 'dark' ? 'Paper' : 'Ink';
}

// Every per-mode difference lives here, physics and styling together, so a
// switch can never leave half the previous mode's encoding behind.
function applyMode() {
  const lf = Graph.d3Force('link');
  if (mode === 'classic') {
    // Release the pins AND re-seed, so the free layout is visible immediately
    // instead of inheriting the strata it just left. Deterministic (derived from
    // the index, no Math.random) so a switch is reproducible.
    nodes.forEach((n, i) => {
      delete n.fy;
      const golden = i * 2.399963;
      const r = 190 * Math.cbrt((i + 0.5) / nodes.length);
      n.x = Math.cos(golden) * r;
      n.y = ((i * 7919 % 200) / 200 - 0.5) * 260;
      n.z = Math.sin(golden) * r;
      n.vx = n.vy = n.vz = 0;
    });
    Graph.d3Force('strata', null);
    Graph.d3Force('charge').strength(-95);
    if (lf && lf.distance) lf.distance(60);
    if (lf && lf.strength) lf.strength(1);
    controls.autoRotateSpeed = 0.4;
    idleMs = 2000;
  } else {
    nodes.forEach(n => { n.fy = stratumY(n); });
    Graph.d3Force('strata', strataForce());
    Graph.d3Force('charge').strength(-560);
    // Weak links on purpose: at full strength every child is hauled on top of
    // its antecedent, undoing the even seeding and stacking nodes into the same
    // screen position. The struts still show structure, they just stop dictating
    // the spacing.
    if (lf && lf.distance) lf.distance(110);
    if (lf && lf.strength) lf.strength(0.12);
    controls.autoRotateSpeed = 0.32;
    idleMs = 4500;
  }
  restyle();
  // Re-commit the data so the layout is genuinely rebuilt under the new forces.
  // Reheating alone was not enough: an already-cooled simulation stays exactly
  // where it stopped, so switching modes changed the colours but left the old
  // layout in place. Re-committing also copies the fy pins straight onto y, so
  // bedrock snaps into its strata without waiting for a single tick.
  Graph.graphData(visibleData());
  safeReheat();
  framed = false;
  resetIdle();
  refreshChrome();
  setTimeout(frameHero, 900);
}
applyMode();   // install the default mode's physics and styling authoritatively

// Reading a node must not fight a moving camera, so hovering stops the drift
// outright and leaving re-arms the idle timer.
Graph.onNodeHover(node => {
  nodeHovered = !!node;
  hoveredId = node ? node.id : null;
  if (nodeHovered) {
    controls.autoRotate = false;
    clearTimeout(idleTimer);
    if (!overlayLocked) showOverlay(node, false);
  } else {
    resetIdle();
    if (!overlayLocked) { overlay.style.display = 'none'; }
  }
});

// Resize handler. A 0-size container must never reach the renderer: a 0x0
// canvas draws no geometry while the DOM labels keep painting, which looks
// exactly like "the graph is broken, only the IDs show up". Fall back to the
// document size, and watch the container so a late layout gets picked up.
let aspectNoteDismissed = false;
function dismissAspectNote() {
  aspectNoteDismissed = true;
  const note = document.getElementById('aspect-note');
  if (note) note.hidden = true;
}

function resize() {
  const w = graphEl.clientWidth  || document.documentElement.clientWidth  || 1000;
  const h = graphEl.clientHeight || Math.max(360, (document.documentElement.clientHeight || 760) - 150);
  const changed = Math.abs(w - viewW) > 8 || Math.abs(h - viewH) > 8;
  const note = document.getElementById('aspect-note');
  if (note && !aspectNoteDismissed) note.hidden = (w / Math.max(1, h)) < 2.4;
  viewW = w; viewH = h;
  Graph.width(w).height(h);
  // Re-frame on a real size change. Framing once was not enough: the chrome
  // reflows after first paint (font metrics, a wrapped header row), which left
  // the composition framed for a taller canvas and clipped its top labels.
  if (changed && !userTookOver) framed = false;
  frameHero();
}
window.addEventListener('resize', () => { resize(); if (overlay.style.display === 'block') positionOverlay(); });
if (window.ResizeObserver) new ResizeObserver(resize).observe(graphEl);
resize();
refreshChrome();

// Last line of defence: never present a silent blank canvas. If WebGL or the
// library did not come up, say so in plain words instead of leaving a dark void.
setTimeout(() => {
  const c = graphEl.querySelector('canvas');
  if (c && c.width > 0 && c.height > 0) return;
  const note = document.createElement('div');
  note.style.cssText = 'position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);max-width:420px;text-align:center;font-size:13px;line-height:1.6;color:#c19b60;background:#111827;border:1px solid #374151;border-radius:9px;padding:16px 18px;z-index:50';
  note.innerHTML = 'The 3D canvas has no size, so nothing can be drawn.<br><br>'
    + 'Open this file over http rather than straight off disk, for example:<br>'
    + '<code style="color:#e5e7eb">python3 -m http.server</code> in its folder.';
  graphEl.appendChild(note);
}, 2500);

// ── Focus a node by id (called from reference links in overlay) ───────────
function focusNode(id) {
  const node = nodes.find(n => n.id === id);
  if (!node) return;
  lockedId = id;
  Graph.nodeColor(nodeColor);
  showOverlay(node, true);
  const dist = 120;
  const distRatio = 1 + dist / Math.hypot(node.x||1, node.y||1, node.z||1);
  Graph.cameraPosition(
    { x: (node.x||0) * distRatio, y: (node.y||0) * distRatio, z: (node.z||0) * distRatio },
    node,
    800
  );
}

// ── Search: find the decision you half-remember ───────────────────────────
// Semantics match `irp find`: a case-insensitive regex across every string
// field. A half-typed regex (you just hit "(") falls back to a literal
// substring instead of erroring mid-keystroke.
// Search is transient focus, not another visual encoding. The view already
// spends colour on confidence, size on centrality and glow on lens
// probability. So matches keep their normal appearance and everything else
// recedes while a query is live, restoring the moment it is cleared.
const qEl = document.getElementById('q');
const hitsEl = document.getElementById('hits');
let searchHits = null;   // null means no active query
let hitList = [];
let selIdx = -1;

function compilePattern(q) {
  try { return new RegExp(q, 'i'); }
  catch (e) { return new RegExp(q.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'i'); }
}

// NUL-joined so a match cannot span two unrelated fields.
function searchableText(d) {
  return [d.id, d.what, d.why, d.source].concat(d.tags || []).filter(Boolean).join('\u0000');
}

function runSearch(raw) {
  const q = (raw || '').trim();
  if (!q) { clearSearch(); return; }
  const re = compilePattern(q);
  hitList = decisions.filter(d => re.test(searchableText(d)));
  searchHits = new Set(hitList.map(d => d.id));
  selIdx = hitList.length ? 0 : -1;
  renderHits(q);
  Graph.nodeColor(nodeColor);
}

function renderHits(q) {
  if (!hitList.length) {
    hitsEl.innerHTML = '<div class="hit-none">No decision matches <strong>' + esc(q) + '</strong></div>';
    hitsEl.classList.add('on');
    return;
  }
  const shown = hitList.slice(0, 40);
  const rows = shown.map((d, i) =>
    '<div class="hit' + (i === selIdx ? ' sel' : '') + '" onclick="pickHit(\'' + esc(d.id) + '\')">' +
      '<div class="hit-id">' + esc(d.id) + '</div>' +
      '<div class="hit-what">' + esc((d.what || '').slice(0, 92)) + '</div>' +
    '</div>').join('');
  const more = hitList.length > shown.length ? ', showing 40' : '';
  hitsEl.innerHTML = '<div class="hit-count">' + hitList.length +
    (hitList.length === 1 ? ' match' : ' matches') + more + '</div>' + rows;
  hitsEl.classList.add('on');
}

function pickHit(id) {
  hitsEl.classList.remove('on');
  qEl.blur();
  focusNode(id);
}

function clearSearch() {
  searchHits = null; hitList = []; selIdx = -1;
  hitsEl.classList.remove('on');
  hitsEl.innerHTML = '';
  Graph.nodeColor(nodeColor);
}

function moveSel(delta) {
  if (!hitList.length) return;
  selIdx = (selIdx + delta + hitList.length) % hitList.length;
  renderHits(qEl.value);
  const el = hitsEl.querySelector('.hit.sel');
  if (el) el.scrollIntoView({ block: 'nearest' });
}

qEl.addEventListener('input', () => runSearch(qEl.value));
qEl.addEventListener('focus', () => { if (hitList.length) hitsEl.classList.add('on'); });
qEl.addEventListener('keydown', e => {
  if (e.key === 'ArrowDown') { e.preventDefault(); moveSel(1); }
  else if (e.key === 'ArrowUp') { e.preventDefault(); moveSel(-1); }
  else if (e.key === 'Enter') { e.preventDefault(); if (selIdx >= 0) pickHit(hitList[selIdx].id); }
  else if (e.key === 'Escape') { e.preventDefault(); qEl.value = ''; clearSearch(); qEl.blur(); }
});

window.addEventListener('keydown', e => {
  const t = (e.target && e.target.tagName) || '';
  if (t === 'INPUT' || t === 'TEXTAREA') return;
  if (e.key === '/' || ((e.metaKey || e.ctrlKey) && (e.key === 'k' || e.key === 'K'))) {
    e.preventDefault(); qEl.focus(); qEl.select();
  } else if (e.key === 'Escape') {
    qEl.value = ''; clearSearch(); clearOverlay();
  }
});

let lastNodeClick = 0;      // set by onNodeClick, which owns node clicks
let pressX = 0, pressY = 0, wasDrag = false;
document.addEventListener('pointerdown', e => {
  pressX = e.clientX; pressY = e.clientY; wasDrag = false;
}, true);
document.addEventListener('pointerup', e => {
  wasDrag = Math.hypot(e.clientX - pressX, e.clientY - pressY) > 6;
}, true);

document.addEventListener('click', e => {
  if (!e.target.closest('.search')) hitsEl.classList.remove('on');

  // Dismiss the locked card when clicking anywhere outside it. Two things are
  // deliberately excluded: clicks inside the card (so you can select its text
  // and follow reference pills), and clicks on the canvas, which the graph's
  // own onNodeClick / onBackgroundClick already handle. Without that second
  // exclusion this handler would fire on the same click that opened the card
  // and close it instantly.
  if (overlay.style.display !== 'block') return;
  if (e.target.closest('#overlay')) return;                    // reading the card
  if (Date.now() - lastNodeClick < 250) return;   // the node handler has it
  if (wasDrag) { wasDrag = false; return; }       // that was a camera drag, not a click
  clearOverlay();
});

// ── Label visibility toggle ────────────────────────────────────────────────
let labelsVisible = true;
function toggleLabels() {
  // Only flips the flag: the label loop owns visibility, because it also hides
  // labels that lose their collision slot. Setting display here too would fight it.
  labelsVisible = !labelsVisible;
  document.getElementById('toggle-labels').textContent = labelsVisible ? 'Hide IDs' : 'Show IDs';
}

// ── DOM label overlay — projects each node's 3D position to screen coords ─────
const labelEls = {};
nodes.forEach(node => {
  const el = document.createElement('div');
  el.className = 'node-label';
  el.textContent = shortId(node.id);
  graphEl.appendChild(el);
  labelEls[node.id] = el;
});
// Measure each label once, while they are all still visible. Real text widths
// let the collision test pack them as tightly as they honestly fit, instead of
// discarding labels against a worst-case guess.
nodes.forEach(node => {
  const el = labelEls[node.id];
  el.__w = el.offsetWidth || 78;
});
// Labels are de-collided every frame. The canopy holds many decisions at
// similar heights, so raw projection stacks their IDs on top of each other and
// the whole top of the graph turns to mush. Most load-bearing decisions claim
// their slot first (they are the ones worth reading); a label that still cannot
// find clear space after nudging is hidden rather than drawn over its neighbour.
const LBL_H = 12, LBL_GAP = 4, NUDGE = 14;

// A label is NEVER moved off its node. Nudging colliding labels into clear air
// was worse than the collision it solved: they drifted up to 200px away, read as
// belonging to nothing, and snapped back as soon as the camera moved. So the
// only two states are "drawn exactly on its node" or "not drawn".
//
// When two labels would overlap, the more load-bearing decision keeps its name
// and the other is dropped, which is the right thing to lose: the bedrock is
// what the reader needs identified. The node itself stays visible either way,
// and hover always names it.
const labelPriority = nodes.slice().sort((a, b) =>
  (foundationScores[b.id] || 0) - (foundationScores[a.id] || 0));

// Only the most load-bearing decisions are named by default. "Label everything"
// is the thing that cannot survive real scale: at 135 decisions it drew 103 IDs
// and the result was a hairball where the bedrock could no longer be found, which
// defeats the entire point of the view. A small ledger still gets every label.
// Everything else is one hover away, and the search names any decision directly.
// Recomputed per pass, because the budget follows the SLICE, not the ledger:
// narrowing to 20 visible decisions should name all 20, even though the full
// ledger of 135 would only name its top 15.
function namedSet() {
  const budget = visibleIds.size <= 24 ? visibleIds.size : 15;
  const out = new Set();
  for (const id of foundationOrder) {
    if (out.size >= budget) break;
    if (visibleIds.has(id)) out.add(id);
  }
  return out;
}

function layoutLabels() {
  // The hovered and locked decisions claim their slot before anyone else, so
  // whatever you are actually reading is always named.
  const order = labelPriority.slice().sort((a, b) =>
    ((b.id === lockedId || b.id === hoveredId) ? 1 : 0) -
    ((a.id === lockedId || a.id === hoveredId) ? 1 : 0));

  const eligible = namedSet();
  const placed = [];
  for (const node of order) {
    const el = labelEls[node.id];
    if (!el) continue;
    if (!labelsVisible) { el.style.display = 'none'; continue; }
    if (!visibleIds.has(node.id)) { el.style.display = 'none'; continue; }
    const named = eligible.has(node.id) || node.id === lockedId || node.id === hoveredId;
    if (!named) { el.style.display = 'none'; continue; }
    const p = Graph.graph2ScreenCoords(node.x || 0, node.y || 0, node.z || 0);
    // Off screen or behind the camera: nothing to draw.
    if (!p || !Number.isFinite(p.x) || !Number.isFinite(p.y) ||
        p.x < -160 || p.x > viewW + 160 || p.y < -160 || p.y > viewH + 160) {
      el.style.display = 'none';
      continue;
    }
    const w = el.__w || 78;
    const mustShow = (node.id === lockedId || node.id === hoveredId);
    const hits = (yy) => placed.some(r =>
      Math.abs(r.x - p.x) < (r.w + w) / 2 + LBL_GAP && Math.abs(r.y - yy) < LBL_H);

    // At most ONE small step, and only to break a near-exact overlap. Two
    // decisions at different depths can project onto the same screen row no
    // matter how the layout is tuned, so a tiny nudge is the only way to keep
    // both named. Capped hard at NUDGE px: the earlier version allowed up to
    // 208px of lift, which is what tore labels off their nodes.
    let y = p.y;
    if (hits(y) && !mustShow) {
      if (!hits(p.y - NUDGE))      y = p.y - NUDGE;
      else if (!hits(p.y + NUDGE)) y = p.y + NUDGE;
      else { el.style.display = 'none'; continue; }
    }
    placed.push({ x: p.x, y, w });
    el.style.display = '';
    el.style.left = p.x + 'px';
    el.style.top  = y + 'px';
  }
}

// Driven by the render loop, with a timer as a safety net. Hiding a label is a
// hard state, so a single early frame taken before the canvas was sized would
// otherwise leave every ID hidden for good if the render loop then stalls.
window.__layoutLabels = layoutLabels;   // debug hook: force one label placement pass
(function tickLabels() { layoutLabels(); requestAnimationFrame(tickLabels); })();
setInterval(layoutLabels, 400);
</script>
</body>
</html>
"""


def build_graph_html(
    decisions: list[dict[str, Any]],
    filter_badge: str = "",
    title_suffix: str = "",
) -> str:
    """Render a self-contained graph HTML from a pre-built decisions list.

    Nodes with ``dimmed=True`` are rendered small and dark (causal context).
    Nodes without that flag are full-brightness (matched / in-focus).
    """
    from datetime import datetime, timezone as _tz
    generated_at = datetime.now(_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    edge_count = _count_edges(decisions)
    decisions_json = json.dumps(decisions, ensure_ascii=False)

    title = f"IRP Decision Graph{(' — ' + title_suffix) if title_suffix else ''}"
    typed_edges = dynamics.derive_typed_edges(decisions)
    html = (
        _HTML_TEMPLATE
        .replace("<title>IRP Decision Graph</title>", f"<title>{title}</title>")
        .replace("<h1>IRP Decision Graph</h1>", f"<h1>{title}</h1>")
        .replace("__GENERATED_AT__", generated_at)
        .replace("__DECISION_COUNT__", str(len(decisions)))
        .replace("__EDGE_COUNT__", str(edge_count))
        .replace("__FILTER_BADGE__", filter_badge)
        .replace("__DECISIONS_JSON__", decisions_json)
        .replace("__EDGES_JSON__", json.dumps(typed_edges, ensure_ascii=False))
        .replace("__INITIAL_VIEW__", dynamics.STRUCTURE_VIEW)
        .replace("__INITIAL_SEED__", "")
    )
    return html


def _is_decision(entry: dict[str, Any]) -> bool:
    if entry.get("type") == "decision":
        return True
    return bool(entry.get("what")) and bool(entry.get("why")) and entry.get("type") in (None, "")


def _count_edges(decisions: list[dict[str, Any]]) -> int:
    id_set = {d["id"] for d in decisions}
    seen: set[str] = set()
    count = 0
    for d in decisions:
        for ref in set(IRP_ID_RE.findall(d.get("why") or "")):
            key = f"{d['id']}|{ref}"
            if ref != d["id"] and ref in id_set and key not in seen:
                seen.add(key)
                count += 1
    return count


_SAMPLE_DECISIONS: list[dict[str, Any]] = json.loads(
    '[{"id":"IRP-2026-01-10-001","type":"decision","what":"Adopt a shared design token system across all product surfaces","why":"Every team was maintaining separate colour and spacing values, causing visual drift and expensive rework at every brand refresh. Foundational decision that gates IRP-2026-01-15-002 and IRP-2026-02-01-004.","confidence":"high","tags":["design-system","tokens","brand"],"source":"slack","timestamp":"2026-01-10T09:00:00Z"},'
    '{"id":"IRP-2026-01-15-002","type":"decision","what":"Use Figma variables as the single source of truth for all design tokens","why":"Teams already live in Figma. Variables enable multi-mode switching (light/dark, brand A/B) without duplication. Rejected Storybook tokens as primary source — too dev-centric for a design-led org. Builds on IRP-2026-01-10-001.","confidence":"high","tags":["figma","tokens","design-system"],"source":"slack","timestamp":"2026-01-15T10:30:00Z"},'
    '{"id":"IRP-2026-01-20-003","type":"decision","what":"Tokens sync from Figma to code via automated pipeline — no manual export","why":"Manual export creates drift between design and code within 48 hours. Every launch was blocked on a last-minute sync sprint. References IRP-2026-01-15-002 as upstream source. Rejected manual handoff — it failed three consecutive quarters.","confidence":"high","tags":["automation","figma","handoff"],"source":"stdin","timestamp":"2026-01-20T14:00:00Z"},'
    '{"id":"IRP-2026-02-01-004","type":"decision","what":"Build component library on Radix UI primitives, not from scratch","why":"Accessibility compliance is a hard blocker for enterprise sales. Radix handles ARIA patterns correctly out of the box. Estimated 6 months to build from scratch with equivalent a11y coverage. Rejected scratch build — risk too high. Extends IRP-2026-01-10-001 system direction.","confidence":"high","tags":["components","a11y","radix","enterprise"],"source":"slack","timestamp":"2026-02-01T11:00:00Z"},'
    '{"id":"IRP-2026-02-05-005","type":"decision","what":"All components must support light and dark mode via token modes, not separate stylesheets","why":"Three enterprise accounts requested dark mode in Q1 contracts. IRP-2026-01-15-002 token system makes this feasible without duplication — mode switching costs near zero once tokens are wired. Rejected separate stylesheets — 2× maintenance burden.","confidence":"high","tags":["dark-mode","tokens","components"],"source":"stdin","timestamp":"2026-02-05T09:00:00Z"},'
    '{"id":"IRP-2026-02-10-006","type":"decision","what":"Motion uses a single easing curve: ease-out at 200 ms for micro, 400 ms for page transitions","why":"Animation inconsistency was the top complaint in UX research (cited by 67% of testers). Rejected spring physics — unpredictable for handoff and hard to QA across browsers. Aligns with IRP-2026-01-10-001 system coherence goal.","confidence":"high","tags":["motion","animation","ux"],"source":"slack","timestamp":"2026-02-10T16:00:00Z"},'
    '{"id":"IRP-2026-02-20-007","type":"decision","what":"Design decisions require a rationale note in Figma before engineering handoff","why":"Lost reasoning was the root cause of 80% of design-dev conflicts in Q4 audit. Without documented why, engineers make assumptions that require expensive rework. References IRP-2026-02-01-004 — accessibility decisions especially need traceable rationale.","confidence":"high","tags":["process","handoff","rationale","figma"],"source":"stdin","timestamp":"2026-02-20T10:00:00Z"},'
    '{"id":"IRP-2026-03-01-008","type":"decision","what":"Design critiques are timeboxed to 45 minutes with a pre-agreed decision owner","why":"Critiques were averaging 2.5 hours without resolution. Decision fatigue was leading to poor outcomes in the last 30 minutes. IRP-2026-02-20-007 requires someone to own the rationale note — ownership must be assigned before the critique, not after.","confidence":"medium","tags":["process","critique","meetings"],"source":"stdin","timestamp":"2026-03-01T09:00:00Z"},'
    '{"id":"IRP-2026-03-10-009","type":"decision","what":"Brand voice is expert but human — no technical jargon without plain-language follow-up","why":"User research: 43% of creative directors felt alienated by product copy. Enterprise buyers want confidence without complexity. Connects IRP-2026-01-10-001 visual system coherence to content tone. Rejected purely technical voice — wrong for the ICP.","confidence":"high","tags":["brand","copy","voice","content"],"source":"slack","timestamp":"2026-03-10T11:00:00Z"},'
    '{"id":"IRP-2026-03-15-010","type":"decision","what":"All illustrations use 1.5 px line weight at base scale — no exceptions","why":"Inconsistent line weights made multi-page documents look unpolished in enterprise demos. The design team had five competing standards. References IRP-2026-01-10-001 — visual system must be internally consistent. Rejected per-team latitude — too hard to enforce at scale.","confidence":"high","tags":["illustration","visual-system","brand"],"source":"stdin","timestamp":"2026-03-15T14:00:00Z"},'
    '{"id":"IRP-2026-03-20-011","type":"decision","what":"REST over GraphQL for the asset delivery API","why":"The creative tools team has zero GraphQL experience. REST is sufficient for current query patterns. GraphQL adds a learning curve with no query-complexity benefit at this stage. References IRP-2026-02-01-004 — keep the component API simple for enterprise onboarding.","confidence":"high","tags":["api","rest","architecture"],"source":"slack","timestamp":"2026-03-20T10:00:00Z"},'
    '{"id":"IRP-2026-04-01-012","type":"decision","what":"All exported assets served from CDN edge nodes — no origin fallback for large files","why":"Large creative files were hitting 4–8 s load times from origin. CDN edge cuts this to under 400 ms. Rejected client-side compression — too complex for the file format diversity. Builds on IRP-2026-03-20-011 delivery architecture.","confidence":"high","tags":["cdn","performance","assets"],"source":"stdin","timestamp":"2026-04-01T09:00:00Z"},'
    '{"id":"IRP-2026-04-05-013","type":"decision","what":"Multi-brand theming via token sets — not separate codebases per brand","why":"Two brands requested separate codebases. Token sets from IRP-2026-01-15-002 cover 90% of brand differentiation via mode switching. Maintaining separate codebases would triple release overhead. References IRP-2026-02-05-005 multi-mode foundation.","confidence":"high","tags":["multi-brand","tokens","architecture"],"source":"slack","timestamp":"2026-04-05T11:00:00Z"},'
    '{"id":"IRP-2026-04-10-014","type":"decision","what":"Accessibility audit runs on every PR — no merge without WCAG AA pass","why":"Enterprise legal flagged WCAG compliance as a contractual requirement in Q1 SOWs. IRP-2026-02-01-004 Radix foundation makes automated WCAG AA achievable. Rejected spot-audits — too easy to slip under deadline pressure.","confidence":"high","tags":["a11y","ci","wcag","process"],"source":"slack","timestamp":"2026-04-10T10:00:00Z"},'
    '{"id":"IRP-2026-04-15-015","type":"decision","what":"AI-assisted design suggestions are opt-in, not surfaced by default","why":"Privacy-sensitive enterprise clients require explicit consent for AI features. Four accounts flagged opt-out fatigue with AI defaults. References IRP-2026-02-01-004 enterprise trust model. Rejected always-on — two prospects cited it as a blocker.","confidence":"high","tags":["ai","privacy","enterprise","ux"],"source":"slack","timestamp":"2026-04-15T14:00:00Z"},'
    '{"id":"IRP-2026-04-20-016","type":"decision","what":"Component documentation lives in Storybook — Figma descriptions are summaries only","why":"Two sources of truth for component docs was causing spec drift. Storybook is canonical for behaviour, Figma for visual intent. Eliminates sync burden. References IRP-2026-02-20-007 handoff rationale and IRP-2026-02-01-004 component library direction.","confidence":"medium","tags":["docs","storybook","figma","components"],"source":"stdin","timestamp":"2026-04-20T10:00:00Z"},'
    '{"id":"IRP-2026-04-22-017","type":"decision","what":"Semantic versioning enforced for the component library — breaking changes require a major bump","why":"Three teams were bitten by undocumented breaking changes in minor releases. References IRP-2026-02-01-004 component library direction. Rejected loose versioning — trust cost outweighed flexibility.","confidence":"high","tags":["versioning","components","process"],"source":"slack","timestamp":"2026-04-22T09:00:00Z"},'
    '{"id":"IRP-2026-04-25-018","type":"decision","what":"Design system has a quarterly review cycle — no ad-hoc deprecations between reviews","why":"Ad-hoc deprecations were disrupting product team sprints without warning. A quarterly cadence gives consuming teams time to migrate. References IRP-2026-04-22-017 versioning discipline. Builds on IRP-2026-01-10-001 shared system governance model.","confidence":"medium","tags":["governance","process","design-system"],"source":"stdin","timestamp":"2026-04-25T11:00:00Z"}]'
)


def _parse_date(date_str: str | None) -> str | None:
    """Validate and normalise a YYYY-MM-DD date string. Returns None on invalid input."""
    if not date_str:
        return None
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
        return date_str
    except ValueError:
        return None


def _node_in_range(entry: dict[str, Any], from_date: str | None, to_date: str | None, project: str | None) -> bool:
    """Return True if this entry falls within all active filters."""
    ts = (entry.get("timestamp") or "")[:10]  # YYYY-MM-DD slice
    if from_date and ts and ts < from_date:
        return False
    if to_date and ts and ts > to_date:
        return False
    if project:
        tags = [t.lower() for t in (entry.get("tags") or [])]
        if project.lower() not in tags:
            return False
    return True


def run_export_graph(project_root: Path, irp_dir: Path, args) -> dict:
    output_arg = getattr(args, "output", None)
    force = bool(getattr(args, "force", False))
    demo = bool(getattr(args, "demo", False))
    from_date = _parse_date(getattr(args, "from_date", None))
    to_date = _parse_date(getattr(args, "to_date", None))
    project = getattr(args, "project", None) or None
    has_filter = bool(from_date or to_date or project)

    if demo:
        decisions = _SAMPLE_DECISIONS
        default_name = "GRAPH-demo.html"
    else:
        ledger = read_ledger(irp_dir)
        decisions = [row for row in ledger if _is_decision(row)]
        default_name = "GRAPH.html"
        if not decisions:
            return {
                "command": "export.graph",
                "status": "empty",
                "text": (
                    "No decisions found in .irp/ledger.jsonl\n\n"
                    "Capture your first decision with:\n"
                    "  irp capture\n\n"
                    "Or explore a populated example (18 decisions, 22 edges):\n"
                    "  irp export graph --demo"
                ),
            }

    # Apply dimming: mark nodes outside the active filter range.
    # Out-of-range nodes are kept for context but rendered small and dark.
    if has_filter:
        decisions = [
            {**d, "dimmed": not _node_in_range(d, from_date, to_date, project)}
            for d in decisions
        ]
    in_range_count = sum(1 for d in decisions if not d.get("dimmed"))

    # ── IRP Dynamics: derived typed edges + optional provenance lens ─────────
    # The typed edge layer is always computed so the HTML can offer the lenses
    # interactively. It is only written to .irp/derived/ when a lens is asked
    # for, so a plain `irp export graph` still touches nothing but its output.
    view = getattr(args, "view", None) or dynamics.STRUCTURE_VIEW
    seed = getattr(args, "seed", None) or None
    known_ids = {d["id"] for d in decisions if d.get("id")}

    if view in dynamics.SEEDED_VIEWS and not seed:
        return {
            "command": "export.graph",
            "status": "error",
            "text": (
                f"--view {view} needs a decision to seed on.\n\n"
                f"  irp export graph --view {view} --seed IRP-YYYY-MM-DD-NNN\n\n"
                "Pick any decision id from your ledger, or explore the sample:\n"
                "  irp export graph --demo --view foundations"
            ),
        }
    if seed and seed not in known_ids:
        return {
            "command": "export.graph",
            "status": "error",
            "text": (
                f"Seed decision not found in this graph: {seed}\n\n"
                "It must be one of the decisions being exported "
                "(check your --from/--to/--project filters)."
            ),
        }

    snapshot = dynamics.snapshot_hash(irp_dir, decisions, demo=demo)
    edge_layer = dynamics.build_edge_layer(decisions, snapshot, demo=demo)
    typed_edges = edge_layer["edges"]
    rel_counts = dynamics.relation_counts(typed_edges)

    analysis = None
    derived_paths: list[Path] = []
    if view in dynamics.LENS_VIEWS:
        analysis = dynamics.compute_lens(
            decisions, view, seed=seed, snapshot=snapshot, edges=typed_edges
        )
        derived_paths.append(dynamics.write_edge_layer(irp_dir, edge_layer))
        derived_paths.append(dynamics.write_analysis(irp_dir, analysis))

    output_path = Path(output_arg) if output_arg else (project_root / default_name)
    if not output_path.is_absolute():
        output_path = (project_root / output_path).resolve()

    if output_path.exists() and not force:
        return {
            "command": "export.graph",
            "status": "exists",
            "output_path": str(output_path),
            "decision_count": len(decisions),
            "text": (
                f"Refusing to overwrite existing file: {output_path}\n"
                "Re-run with --force, or pass --output PATH to write elsewhere."
            ),
        }

    # Build filter badge for the HTML header.
    filter_parts: list[str] = []
    if from_date:
        filter_parts.append(f"from {from_date}")
    if to_date:
        filter_parts.append(f"to {to_date}")
    if project:
        filter_parts.append(f"project:{project}")
    if filter_parts:
        filter_badge = (
            f" &nbsp;&middot;&nbsp; <span style='color:#60a5fa'>"
            f"{in_range_count} in range</span>"
            f" <span style='color:#374151'>({' '.join(filter_parts)})"
            f" · {len(decisions) - in_range_count} dimmed</span>"
        )
    else:
        filter_badge = ""

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    edge_count = _count_edges(decisions)
    decisions_json = json.dumps(decisions, ensure_ascii=False)

    html = (
        _HTML_TEMPLATE
        .replace("__GENERATED_AT__", generated_at)
        .replace("__DECISION_COUNT__", str(len(decisions)))
        .replace("__EDGE_COUNT__", str(edge_count))
        .replace("__FILTER_BADGE__", filter_badge)
        .replace("__DECISIONS_JSON__", decisions_json)
        .replace("__EDGES_JSON__", json.dumps(typed_edges, ensure_ascii=False))
        .replace("__INITIAL_VIEW__", view)
        .replace("__INITIAL_SEED__", seed or "")
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")

    header = [
        "IRP V1.5 dispatcher",
        f"Project: {project_root}",
        "Command: export graph",
        "",
    ]
    demo_note = " (sample data — your ledger is not modified)" if demo else ""
    if has_filter:
        dimmed_count = len(decisions) - in_range_count
        filter_note = f"  Filter: {', '.join(filter_parts)} → {in_range_count} in range, {dimmed_count} dimmed"
    else:
        filter_note = None
    regen_cmd = "  irp export graph --demo --force" if demo else "  irp export graph --force"
    if view != dynamics.STRUCTURE_VIEW:
        regen_cmd += f" --view {view}" + (f" --seed {seed}" if seed else "")

    detail_lines = [f"Nodes:  {len(decisions)} decision(s){demo_note}"]
    if filter_note:
        detail_lines.append(filter_note)
    detail_lines.append(f"Edges:  {edge_count} provenance reference(s) with animated particles")

    if analysis is not None:
        walked = rel_counts.get(dynamics.WALK_RELATION, 0)
        excluded = sum(v for k, v in rel_counts.items() if k != dynamics.WALK_RELATION)
        seed_note = f" seeded at {seed}" if seed else ""
        what_by_id = {d["id"]: (d.get("what") or "") for d in decisions}
        detail_lines.append(
            f"Lens:   {view}{seed_note} "
            f"({walked} depends_on walked, {excluded} excluded: gates/mentions)"
        )
        detail_lines.append("Top:")
        for nid, score in list(analysis["scores"].items())[:5]:
            detail_lines.append(f"          {score:.4f}  {nid}  {what_by_id.get(nid, '')[:52]}")
        for path in derived_paths:
            detail_lines.append(f"Derived: {path}")

    text = "\n".join(header + [
        f"Wrote {output_path}",
    ] + detail_lines + [
        "",
        "Open in any browser. Drag to orbit · scroll to zoom · click to inspect.",
        "Regenerate any time with:",
        regen_cmd,
    ])

    result = {
        "command": "export.graph",
        "status": "ok",
        "output_path": str(output_path),
        "decision_count": len(decisions),
        "in_range_count": in_range_count if has_filter else len(decisions),
        "edge_count": edge_count,
        "filters": {"from_date": from_date, "to_date": to_date, "project": project},
        "view": view,
        "seed": seed,
        "relations": rel_counts,
        "text": text,
    }
    if analysis is not None:
        # Derived analysis, never evidence (DYN-I2). Recomputable from the
        # snapshot hash it carries (DYN-I4).
        result["analysis"] = analysis
        result["derived_paths"] = [str(p) for p in derived_paths]
    return result
