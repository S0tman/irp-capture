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
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0f1117;color:#e5e7eb;height:100vh;display:flex;flex-direction:column;overflow:hidden}
header{padding:11px 20px;border-bottom:1px solid #1f2937;display:flex;align-items:center;gap:14px;row-gap:8px;flex-wrap:wrap;flex-shrink:0;z-index:10;position:relative}
h1{font-size:14px;font-weight:600;color:#f9fafb;white-space:nowrap}
.meta{font-size:12px;color:#6b7280;white-space:nowrap}
.legend{display:flex;gap:14px;margin-left:auto;align-items:center}
/* The header wraps to a second row rather than squeezing, so narrow embeds
   (the book iframe is ~760px) keep their canvas. As width drops, the
   decorative chrome yields its row before the functional controls do: the
   legend first (confidence is also on every node overlay), then the
   timestamp. These must sit after the rules they override: a media query
   adds no specificity, so source order decides. */
@media (max-width:900px){.legend{display:none}}
@media (max-width:620px){.meta{display:none}}
.li{display:flex;align-items:center;gap:5px;font-size:11px;color:#9ca3af}
.dot{width:10px;height:10px;border-radius:50%;flex-shrink:0}
.views{display:flex;gap:4px;align-items:center;margin-left:18px}
.vbtn{font:600 10px ui-monospace,"SF Mono",monospace;letter-spacing:.04em;text-transform:uppercase;color:#6b7280;background:#111827;border:1px solid #1f2937;border-radius:5px;padding:4px 9px;cursor:pointer;user-select:none}
.vbtn:hover{color:#9ca3af;border-color:#374151}
.vbtn.on{color:#0f1117;background:#60a5fa;border-color:#60a5fa}
.seedbadge{font:10px ui-monospace,"SF Mono",monospace;color:#60a5fa;margin-left:6px}
.search{position:relative;margin-left:auto}
#q{width:200px;font:11px ui-monospace,"SF Mono",monospace;color:#e5e7eb;background:#111827;border:1px solid #1f2937;border-radius:5px;padding:5px 9px;outline:none}
#q:focus{border-color:#60a5fa;width:260px}
#q::placeholder{color:#4b5563}
#hits{position:absolute;top:100%;right:0;margin-top:5px;width:340px;max-height:290px;overflow-y:auto;background:#0a0c12;border:1px solid #374151;border-radius:7px;display:none;z-index:200;box-shadow:0 8px 28px rgba(0,0,0,.75)}
#hits.on{display:block}
.hit{padding:7px 10px;border-bottom:1px solid #111827;cursor:pointer}
.hit:last-child{border-bottom:none}
.hit:hover,.hit.sel{background:#1f2937}
.hit-id{font:700 9px ui-monospace,"SF Mono",monospace;color:#60a5fa;letter-spacing:.03em}
.hit-what{font-size:11px;color:#d1d5db;line-height:1.4;margin-top:2px}
.hit-none{padding:9px 10px;font-size:11px;color:#6b7280}
.hit-count{padding:5px 10px;font:9px ui-monospace,monospace;color:#4b5563;text-transform:uppercase;letter-spacing:.06em;border-bottom:1px solid #111827;background:#111827}
.hint{font-size:11px;color:#9ca3af;padding:7px 20px;border-bottom:1px solid #111827;z-index:10;position:relative}
.main{display:flex;flex:1;overflow:hidden;position:relative}
#graph{flex:1;cursor:grab;position:relative}
.node-label{position:absolute;pointer-events:none;transform:translate(-50%,-140%);font:bold 9px ui-monospace,"SF Mono",monospace;color:rgba(229,231,235,0.82);white-space:nowrap;text-shadow:0 1px 3px rgba(0,0,0,0.9)}
#graph:active{cursor:grabbing}
#graph canvas{display:block}
#overlay{position:fixed;display:none;background:#111827;border:1px solid #374151;border-radius:9px;padding:14px 16px;max-width:380px;z-index:100;pointer-events:none;box-shadow:0 4px 20px rgba(0,0,0,.6)}
#overlay.locked{pointer-events:auto;border-color:#60a5fa;box-shadow:0 0 0 1px #60a5fa,0 6px 28px rgba(0,0,0,.85)}
.did{font-size:11px;font-weight:700;color:#6b7280;font-family:monospace;letter-spacing:.03em}
.dwhat{font-size:14px;font-weight:600;color:#f9fafb;line-height:1.45}
.dwhy{font-size:12px;color:#9ca3af;line-height:1.55}
.dmeta{display:flex;gap:5px;flex-wrap:wrap;align-items:center}
.badge{font-size:10px;padding:2px 7px;border-radius:999px;font-weight:700}
.bh{background:#14532d;color:#4ade80}
.bm{background:#451a03;color:#fb923c}
.bl{background:#450a0a;color:#f87171}
.bu{background:#1f2937;color:#9ca3af}
.tag{font-size:10px;padding:2px 6px;border-radius:4px;background:#1f2937;color:#9ca3af;font-family:monospace}
.dsec{font-size:10px;font-weight:700;color:#374151;text-transform:uppercase;letter-spacing:.08em;margin-top:2px}
.dsrc{font-size:11px;color:#4b5563;font-family:monospace}
.refs{display:flex;flex-direction:column;gap:3px}
.rl{font-size:11px;color:#60a5fa;font-family:monospace;cursor:pointer;text-decoration:underline}
footer{padding:6px 20px;border-top:1px solid #111827;font-size:11px;color:#9ca3af;flex-shrink:0;z-index:10;position:relative;display:flex;justify-content:space-between;align-items:center}
#toggle-labels{color:#6b7280;cursor:pointer;text-decoration:none;user-select:none}#toggle-labels:hover{color:#9ca3af}
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
  <div class="search">
    <input id="q" type="text" autocomplete="off" spellcheck="false" placeholder="Search  /  or  &#8984;K">
    <div id="hits"></div>
  </div>
  <div class="legend">
    <div class="li"><div class="dot" style="background:#22c55e"></div>high</div>
    <div class="li"><div class="dot" style="background:#f59e0b"></div>medium</div>
    <div class="li"><div class="dot" style="background:#ef4444"></div>low</div>
    <div class="li"><div class="dot" style="background:#6b7280"></div>unknown</div>
  </div>
</header>
<div class="hint"><strong>Drag</strong> to orbit &nbsp;&middot;&nbsp; <strong>Scroll</strong> to zoom &nbsp;&middot;&nbsp; <strong>Hover</strong> to preview &nbsp;&middot;&nbsp; <strong>Click node</strong> to lock details &nbsp;&middot;&nbsp; <strong>Click reference links</strong> to follow lineage &nbsp;&middot;&nbsp; <strong>Right-drag</strong> to pan</div>
<div class="main">
  <div id="graph"></div>
</div>
<div id="overlay"></div>
<footer><span>Source: .irp/ledger.jsonl &nbsp;&middot;&nbsp; Edges = IRP id cross-references in <em>why</em> fields &nbsp;&middot;&nbsp; <code>irp export graph --force</code> to regenerate</span><a id="toggle-labels" onclick="toggleLabels()">Hide IDs</a></footer>

<script>
const decisions = __DECISIONS_JSON__;
const IRP_RE = /\bIRP-\d{4}-\d{2}-\d{2}-\d{3}\b/g;
const idSet = new Set(decisions.map(d => d.id));
const byId = Object.fromEntries(decisions.map(d => [d.id, d]));

const CONF_COLOR = { high: '#22c55e', medium: '#f59e0b', low: '#ef4444' };

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

function nodeColor(d) {
  if (d.id === lockedId) return '#D3D3D3';
  // Filter dim wins over search dim. `dimmed` means "outside the range you
  // asked for", a more permanent statement than "not what you just typed".
  if (d.dimmed) return '#2d3748';
  const base = CONF_COLOR[d.confidence] || '#6b7280';
  if (searchHits && !searchHits.has(d.id)) return hexToRgba(base, 0.07);
  if (view === 'structure') return base;
  const p = (lensScores[d.id] || 0) / maxLens;
  return hexToRgba(base, 0.12 + 0.88 * Math.sqrt(p));
}

function nodeVal(d) {
  if (d.dimmed) return 1;
  if (view === 'structure') return d.confidence === 'high' ? 6 : d.confidence === 'medium' ? 4 : 3;
  return 2 + 14 * Math.sqrt((foundationScores[d.id] || 0) / maxFound);
}

function isWalk(l) { return view === 'structure' || l.relation === WALK_REL; }

function refreshChrome() {
  ['structure', 'foundations', 'lineage', 'impact'].forEach(v => {
    const el = document.getElementById('v-' + v);
    if (el) el.classList.toggle('on', v === view);
  });
  const badge = document.getElementById('seed-badge');
  if (badge) {
    badge.textContent = (view === 'lineage' || view === 'impact')
      ? (seedId ? 'seed ' + seedId : 'click a node to seed')
      : '';
  }
}

function applyView() {
  computeLens();
  Graph.graphData({ nodes, links: linksForView() });
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
    Graph.graphData({ nodes, links: linksForView() });
    Graph.nodeColor(nodeColor);
    Graph.nodeVal(nodeVal);
    refreshChrome();
    return;
  }
  applyView();
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
  const margin = 14;
  const ow = Math.min(380, window.innerWidth - margin * 2);
  let left = cursorX + 18;
  let top  = cursorY - 18;
  if (left + ow > window.innerWidth  - margin) left = cursorX - ow - 18;
  const oh = overlay.offsetHeight || 200;
  if (top + oh > window.innerHeight - margin) top = window.innerHeight - oh - margin;
  if (top < margin) top = margin;
  overlay.style.left = left + 'px';
  overlay.style.top  = top  + 'px';
}

function esc(s) {
  return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function shortId(id) {
  const m = (id||'').match(/IRP-\d{4}-(\d{2})-(\d{2})-(\d+)/);
  return m ? 'IRP-' + m[1] + m[2] + '-' + m[3] : id;
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
    ${overlayLocked?`<div style="margin-top:8px;font-size:10px;color:#374151">Click node again or background to dismiss</div>`:'<div style="margin-top:8px;font-size:10px;color:#374151">Click to lock &middot; links become clickable</div>'}
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
  .backgroundColor('#0f1117')
  .graphData({ nodes, links })

  // Nodes — no library tooltip; overlay handles all interaction
  .nodeLabel(() => '')
  .nodeColor(nodeColor)
  .nodeVal(nodeVal)
  .nodeOpacity(0.92)

  // Links / provenance edges. In a lens view the non-walk relations
  // (gates, mentions) stay visible but are dimmed and carry no particles,
  // because they carry no probability.
  .linkColor(l => isWalk(l) ? 'rgba(96,165,250,0.6)' : 'rgba(107,114,128,0.22)')
  .linkWidth(1.5)
  .linkDirectionalArrowLength(5)
  .linkDirectionalArrowRelPos(1)
  .linkDirectionalArrowColor(l => isWalk(l) ? 'rgba(96,165,250,0.9)' : 'rgba(107,114,128,0.3)')
  .linkDirectionalParticles(l => isWalk(l) ? 3 : 0)
  .linkDirectionalParticleWidth(1.5)
  .linkDirectionalParticleColor(() => '#60a5fa')
  .linkDirectionalParticleSpeed(0.006)

  // Interactions
  .onNodeClick((node, event) => {
    event && event.stopPropagation();
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
controls.autoRotate = true;
controls.autoRotateSpeed = 0.4;

let idleTimer;
let nodeHovered = false;

function resetIdle() {
  controls.autoRotate = false;
  clearTimeout(idleTimer);
  idleTimer = setTimeout(() => { if (!nodeHovered) controls.autoRotate = true; }, 2000);
}
graphEl.addEventListener('pointerdown', resetIdle);
graphEl.addEventListener('wheel', resetIdle);

// Stop rotation on hover; show preview overlay (unless a node is locked)
Graph.onNodeHover(node => {
  nodeHovered = !!node;
  if (nodeHovered) {
    controls.autoRotate = false;
    clearTimeout(idleTimer);
    if (!overlayLocked) showOverlay(node, false);
  } else {
    resetIdle();
    if (!overlayLocked) { overlay.style.display = 'none'; }
  }
});

// Resize handler
function resize() {
  Graph.width(graphEl.clientWidth).height(graphEl.clientHeight);
}
window.addEventListener('resize', resize);
resize();
refreshChrome();

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

document.addEventListener('click', e => {
  if (!e.target.closest('.search')) hitsEl.classList.remove('on');
});

// ── Label visibility toggle ────────────────────────────────────────────────
let labelsVisible = true;
function toggleLabels() {
  labelsVisible = !labelsVisible;
  document.querySelectorAll('.node-label').forEach(el => {
    el.style.display = labelsVisible ? '' : 'none';
  });
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
(function tickLabels() {
  nodes.forEach(node => {
    const el = labelEls[node.id];
    if (!el) return;
    const pos = Graph.graph2ScreenCoords(node.x || 0, node.y || 0, node.z || 0);
    el.style.left = pos.x + 'px';
    el.style.top  = pos.y + 'px';
  });
  requestAnimationFrame(tickLabels);
})();
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
