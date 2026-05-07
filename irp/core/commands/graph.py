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
header{padding:11px 20px;border-bottom:1px solid #1f2937;display:flex;align-items:center;gap:14px;flex-shrink:0;z-index:10;position:relative}
h1{font-size:14px;font-weight:600;color:#f9fafb}
.meta{font-size:12px;color:#6b7280}
.legend{display:flex;gap:14px;margin-left:auto;align-items:center}
.li{display:flex;align-items:center;gap:5px;font-size:11px;color:#9ca3af}
.dot{width:10px;height:10px;border-radius:50%;flex-shrink:0}
.hint{font-size:11px;color:#9ca3af;padding:7px 20px;border-bottom:1px solid #111827;z-index:10;position:relative}
.main{display:flex;flex:1;overflow:hidden;position:relative}
#graph{flex:1;cursor:grab;position:relative}
.node-label{position:absolute;pointer-events:none;transform:translate(-50%,-140%);font:bold 9px ui-monospace,"SF Mono",monospace;color:rgba(229,231,235,0.82);white-space:nowrap;text-shadow:0 1px 3px rgba(0,0,0,0.9)}
#graph:active{cursor:grabbing}
#graph canvas{display:block}
#detail{width:320px;border-left:1px solid #1f2937;padding:18px;overflow-y:auto;display:flex;flex-direction:column;gap:11px;flex-shrink:0;background:#0a0c12;z-index:10}
#detail.empty{align-items:center;justify-content:center;color:#4b5563;font-size:13px;text-align:center;gap:8px}
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
  <div class="legend">
    <div class="li"><div class="dot" style="background:#22c55e"></div>high</div>
    <div class="li"><div class="dot" style="background:#f59e0b"></div>medium</div>
    <div class="li"><div class="dot" style="background:#ef4444"></div>low</div>
    <div class="li"><div class="dot" style="background:#6b7280"></div>unknown</div>
  </div>
</header>
<div class="hint"><strong>Drag</strong> to orbit &nbsp;&middot;&nbsp; <strong>Scroll</strong> to zoom &nbsp;&middot;&nbsp; <strong>Hover</strong> a node to inspect &nbsp;&middot;&nbsp; <strong>Click references</strong> in tooltip to follow lineage &nbsp;&middot;&nbsp; <strong>Right-drag</strong> to pan</div>
<div class="main">
  <div id="graph"></div>
  <div id="detail" class="empty"><span>Hover a node to inspect</span><span style="font-size:11px;color:#374151">Click references in the tooltip<br>to follow provenance lineage</span></div>
</div>
<footer><span>Source: .irp/ledger.jsonl &nbsp;&middot;&nbsp; Edges = IRP id cross-references in <em>why</em> fields &nbsp;&middot;&nbsp; <code>irp export graph --force</code> to regenerate</span><a id="toggle-labels" onclick="toggleLabels()">Hide IDs</a></footer>

<script>
const decisions = __DECISIONS_JSON__;
const IRP_RE = /\bIRP-\d{4}-\d{2}-\d{2}-\d{3}\b/g;
const idSet = new Set(decisions.map(d => d.id));
const byId = Object.fromEntries(decisions.map(d => [d.id, d]));

const CONF_COLOR = { high: '#22c55e', medium: '#f59e0b', low: '#ef4444' };
const nodeColor = d => d.id === lockedId ? '#D3D3D3' : d.dimmed ? '#2d3748' : (CONF_COLOR[d.confidence] || '#6b7280');

// Build provenance edges from IRP id cross-refs in why fields
const edgeSet = new Set();
const links = [];
decisions.forEach(d => {
  [...new Set((d.why || '').match(IRP_RE) || [])].forEach(ref => {
    const key = `${d.id}|${ref}`;
    if (ref !== d.id && idSet.has(ref) && !edgeSet.has(key)) {
      edgeSet.add(key);
      links.push({ source: d.id, target: ref });
    }
  });
});

const nodes = decisions.map(d => ({ ...d }));

// ── Detail panel ──────────────────────────────────────────────────────────
const detail = document.getElementById('detail');
let lockedId = null;

function esc(s) {
  return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

function shortId(id) {
  const m = (id||'').match(/IRP-\d{4}-(\d{2})-(\d{2})-(\d+)/);
  return m ? 'IRP-' + m[1] + m[2] + '-' + m[3] : id;
}
function badgeClass(c) { return {high:'bh',medium:'bm',low:'bl'}[c]||'bu'; }

function showDetail(d) {
  const refs = [...new Set((d.why||'').match(IRP_RE)||[])].filter(r=>idSet.has(r)&&r!==d.id);
  detail.className = '';
  detail.innerHTML = `
    <div class="did">${esc(d.id)}</div>
    <div class="dwhat">${esc(d.what)}</div>
    <div class="dmeta">
      <span class="badge ${badgeClass(d.confidence)}">${d.confidence||'unknown'}</span>
      ${(d.tags||[]).map(t=>`<span class="tag">${esc(t)}</span>`).join('')}
    </div>
    <div><div class="dsec">Why</div><div class="dwhy">${esc(d.why)||'&mdash;'}</div></div>
    <div><div class="dsec">Source &middot; Date</div><div class="dsrc">${esc(d.source)||'&mdash;'} &middot; ${esc(d.timestamp)}</div></div>
    ${refs.length?`<div><div class="dsec">References</div><div class="refs">${
      refs.map(r=>`<span class="rl" onclick="focusNode('${r}')">${r}</span>`).join('')
    }</div></div>`:''}
  `;
}

function clearDetail() {
  lockedId = null;
  Graph.nodeColor(nodeColor);
  detail.className = 'empty';
  detail.innerHTML = '<span>Click a node to inspect</span>';
}

// ── 3D Graph ──────────────────────────────────────────────────────────────
const graphEl = document.getElementById('graph');

const Graph = ForceGraph3D({ controlType: 'orbit' })(graphEl)
  .backgroundColor('#0f1117')
  .graphData({ nodes, links })

  // Nodes
  .nodeLabel(d => {
    const e = s => (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    const confColor = {high:'#22c55e',medium:'#f59e0b',low:'#ef4444'}[d.confidence||''] || '#6b7280';
    const tags = (d.tags||[]).map(t=>`<span style="background:#1f2937;color:#9ca3af;padding:1px 5px;border-radius:3px;font-size:10px;font-family:monospace">${e(t)}</span>`).join(' ');
    const refs = [...new Set((d.why||'').match(IRP_RE)||[])].filter(r=>idSet.has(r)&&r!==d.id);
    return `<div style="font:12px/1.55 -apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#111827;color:#e5e7eb;padding:11px 13px;border-radius:9px;border:1px solid #374151;max-width:380px;white-space:normal;box-shadow:0 4px 20px rgba(0,0,0,.6)">
      <div style="font-size:10px;color:#6b7280;font-family:monospace;letter-spacing:.04em;margin-bottom:5px">${e(d.id)}</div>
      <div style="font-weight:600;font-size:13px;color:#f9fafb;margin-bottom:7px">${e(d.what)}</div>
      ${d.why?`<div style="font-size:11px;color:#9ca3af;margin-bottom:8px;padding-top:6px;border-top:1px solid #1f2937"><span style="color:#6b7280;font-size:10px;text-transform:uppercase;letter-spacing:.06em">Why</span><br>${e(d.why)}</div>`:''}
      ${refs.length?`<div style="padding-top:6px;border-top:1px solid #1f2937;margin-bottom:6px"><span style="color:#6b7280;font-size:10px;text-transform:uppercase;letter-spacing:.06em">References</span><div style="display:flex;flex-wrap:wrap;gap:5px;margin-top:4px">${refs.map(r=>`<span onclick="event.stopPropagation();focusNode('${r}')" style="color:#60a5fa;font-size:10px;font-family:monospace;cursor:pointer;text-decoration:underline;padding:1px 4px;border-radius:3px;background:#1e3a5f">${e(r)}</span>`).join('')}</div></div>`:''}
      <div style="display:flex;gap:5px;flex-wrap:wrap;align-items:center">
        ${d.confidence?`<span style="color:${confColor};font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.04em">${e(d.confidence)}</span>`:''}
        ${tags}
        ${d.timestamp?`<span style="color:#4b5563;font-size:10px;margin-left:auto">${e(String(d.timestamp).slice(0,10))}</span>`:''}
      </div>
    </div>`;
  })
  .nodeColor(nodeColor)
  .nodeVal(d => d.dimmed ? 1 : (d.confidence === 'high' ? 6 : d.confidence === 'medium' ? 4 : 3))
  .nodeOpacity(0.92)

  // Links / provenance edges
  .linkColor(() => 'rgba(96,165,250,0.6)')
  .linkWidth(1.5)
  .linkDirectionalArrowLength(5)
  .linkDirectionalArrowRelPos(1)
  .linkDirectionalArrowColor(() => 'rgba(96,165,250,0.9)')
  .linkDirectionalParticles(3)
  .linkDirectionalParticleWidth(1.5)
  .linkDirectionalParticleColor(() => '#60a5fa')
  .linkDirectionalParticleSpeed(0.006)

  // Interactions
  .onNodeClick((node, event) => {
    event && event.stopPropagation();
    if (lockedId === node.id) {
      clearDetail();
    } else {
      lockedId = node.id;
      Graph.nodeColor(nodeColor);
      showDetail(node);
      // Animate camera towards clicked node
      const dist = 120;
      const distRatio = 1 + dist / Math.hypot(node.x||1, node.y||1, node.z||1);
      Graph.cameraPosition(
        { x: (node.x||0) * distRatio, y: (node.y||0) * distRatio, z: (node.z||0) * distRatio },
        node,
        800
      );
    }
  })
  .onBackgroundClick(() => clearDetail());

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

// Stop rotation immediately on node hover; resume idle countdown on leave
Graph.onNodeHover(node => {
  nodeHovered = !!node;
  if (nodeHovered) {
    controls.autoRotate = false;
    clearTimeout(idleTimer);
  } else {
    resetIdle();
  }
});

// Resize handler
function resize() {
  Graph.width(graphEl.clientWidth).height(graphEl.clientHeight);
}
window.addEventListener('resize', resize);
resize();

// ── Focus a node by id (called from ref links) ────────────────────────────
function focusNode(id) {
  const node = nodes.find(n => n.id === id);
  if (!node) return;
  lockedId = id;
  Graph.nodeColor(nodeColor);
  showDetail(node);
  const dist = 120;
  const distRatio = 1 + dist / Math.hypot(node.x||1, node.y||1, node.z||1);
  Graph.cameraPosition(
    { x: (node.x||0) * distRatio, y: (node.y||0) * distRatio, z: (node.z||0) * distRatio },
    node,
    800
  );
}

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

    detail_lines = [f"Nodes:  {len(decisions)} decision(s){demo_note}"]
    if filter_note:
        detail_lines.append(filter_note)
    detail_lines.append(f"Edges:  {edge_count} provenance reference(s) with animated particles")

    text = "\n".join(header + [
        f"Wrote {output_path}",
    ] + detail_lines + [
        "",
        "Open in any browser. Drag to orbit · scroll to zoom · click to inspect.",
        "Regenerate any time with:",
        regen_cmd,
    ])

    return {
        "command": "export.graph",
        "status": "ok",
        "output_path": str(output_path),
        "decision_count": len(decisions),
        "in_range_count": in_range_count if has_filter else len(decisions),
        "edge_count": edge_count,
        "filters": {"from_date": from_date, "to_date": to_date, "project": project},
        "text": text,
    }
