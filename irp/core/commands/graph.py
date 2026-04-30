"""IRP graph export — renders decision ledger as an interactive 3D force graph.

    irp export graph --output GRAPH.html

Design rules:
  - No new schema. Reads .irp/ledger.jsonl only.
  - No LLM calls. No inference. Deterministic mapping only.
  - Edges derived from IRP id references in 'why' fields (regex only).
  - Single self-contained HTML — 3d-force-graph (Three.js/WebGL) via CDN.
  - Drag to orbit the globe. Scroll to zoom. Click to inspect.
  - Animated particles travel along provenance edges.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from irp.core.store import read_ledger

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
  <span class="meta">__GENERATED_AT__ &middot; __DECISION_COUNT__ decisions &middot; __EDGE_COUNT__ provenance edges</span>
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
const nodeColor = d => d.id === lockedId ? '#D3D3D3' : (CONF_COLOR[d.confidence] || '#6b7280');

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
  .nodeVal(d => (d.confidence === 'high' ? 6 : d.confidence === 'medium' ? 4 : 3))
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

def run_export_graph(project_root: Path, irp_dir: Path, args) -> dict:
    output_arg = getattr(args, "output", None)
    force = bool(getattr(args, "force", False))

    output_path = Path(output_arg) if output_arg else (project_root / "GRAPH.html")
    if not output_path.is_absolute():
        output_path = (project_root / output_path).resolve()

    ledger = read_ledger(irp_dir)
    decisions = [row for row in ledger if _is_decision(row)]

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

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    edge_count = _count_edges(decisions)
    decisions_json = json.dumps(decisions, ensure_ascii=False)

    html = (
        _HTML_TEMPLATE
        .replace("__GENERATED_AT__", generated_at)
        .replace("__DECISION_COUNT__", str(len(decisions)))
        .replace("__EDGE_COUNT__", str(edge_count))
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
    text = "\n".join(header + [
        f"Wrote {output_path}",
        f"Nodes:  {len(decisions)} decision(s)",
        f"Edges:  {edge_count} provenance reference(s) with animated particles",
        "",
        "Open in any browser. Drag to orbit · scroll to zoom · click to inspect.",
        "Regenerate any time with:",
        "  irp export graph --force",
    ])

    return {
        "command": "export.graph",
        "status": "ok",
        "output_path": str(output_path),
        "decision_count": len(decisions),
        "edge_count": edge_count,
        "text": text,
    }
