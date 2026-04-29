"""IRP graph export — renders decision ledger as an interactive HTML graph.

    irp export graph --output GRAPH.html

Design rules:
  - No new schema. Reads .irp/ledger.jsonl only.
  - No LLM calls. No inference. Deterministic mapping only.
  - Edges derived from IRP id references in 'why' fields (regex only).
  - Single self-contained HTML file — Cytoscape.js via CDN.
  - Click a node → inspect full decision in side panel.
  - Click a reference link → jump to that node.
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
<script src="https://unpkg.com/cytoscape@3.28.1/dist/cytoscape.min.js"></script>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#0f1117;color:#e5e7eb;height:100vh;display:flex;flex-direction:column;overflow:hidden}
header{padding:12px 20px;border-bottom:1px solid #1f2937;display:flex;align-items:center;gap:14px;flex-shrink:0}
h1{font-size:14px;font-weight:600;color:#f9fafb}
.meta{font-size:12px;color:#6b7280}
.legend{display:flex;gap:16px;margin-left:auto}
.li{display:flex;align-items:center;gap:6px;font-size:12px;color:#9ca3af}
.dot{width:10px;height:10px;border-radius:50%;flex-shrink:0}
.main{display:flex;flex:1;overflow:hidden}
#cy{flex:1}
#detail{width:340px;border-left:1px solid #1f2937;padding:20px;overflow-y:auto;display:flex;flex-direction:column;gap:12px;flex-shrink:0}
#detail.empty{align-items:center;justify-content:center;color:#4b5563;font-size:13px}
.did{font-size:11px;font-weight:600;color:#6b7280;font-family:monospace;letter-spacing:.02em}
.dwhat{font-size:14px;font-weight:600;color:#f9fafb;line-height:1.45}
.dwhy{font-size:13px;color:#9ca3af;line-height:1.55}
.dmeta{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.badge{font-size:11px;padding:2px 8px;border-radius:999px;font-weight:600}
.bh{background:#14532d;color:#4ade80}
.bm{background:#451a03;color:#fb923c}
.bl{background:#450a0a;color:#f87171}
.bu{background:#1f2937;color:#9ca3af}
.tag{font-size:11px;padding:2px 8px;border-radius:4px;background:#1f2937;color:#9ca3af;font-family:monospace}
.dsec{font-size:10px;font-weight:700;color:#4b5563;text-transform:uppercase;letter-spacing:.08em;margin-top:4px}
.dsrc{font-size:12px;color:#6b7280;font-family:monospace}
.refs{display:flex;flex-direction:column;gap:4px}
.rl{font-size:12px;color:#60a5fa;font-family:monospace;cursor:pointer;text-decoration:underline}
footer{padding:7px 20px;border-top:1px solid #1f2937;font-size:11px;color:#374151;flex-shrink:0}
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
<div class="main">
  <div id="cy"></div>
  <div id="detail" class="empty"><span>Click a node to inspect</span></div>
</div>
<footer>Source: .irp/ledger.jsonl &nbsp;&middot;&nbsp; Edges = IRP id references in <em>why</em> fields &nbsp;&middot;&nbsp; Regenerate: <code>irp export graph</code></footer>
<script>
const decisions = __DECISIONS_JSON__;
const CONF_COLOR = {high:'#22c55e',medium:'#f59e0b',low:'#ef4444'};
const IRP_RE = /\bIRP-\d{4}-\d{2}-\d{2}-\d{3}\b/g;
const idSet = new Set(decisions.map(d => d.id));

const nodes = decisions.map(d => ({
  data:{
    id: d.id,
    label: d.id.replace('IRP-',''),
    what: d.what||'',
    why: d.why||'',
    confidence: d.confidence||'unknown',
    tags: d.tags||[],
    timestamp: d.timestamp||'',
    source: d.source||'',
    color: CONF_COLOR[d.confidence]||'#6b7280',
  }
}));

const edgeSet = new Set();
const edges = [];
decisions.forEach(d => {
  const refs = [...new Set((d.why||'').match(IRP_RE)||[])];
  refs.forEach(ref => {
    const key = `${d.id}->${ref}`;
    if(ref !== d.id && idSet.has(ref) && !edgeSet.has(key)){
      edgeSet.add(key);
      edges.push({data:{id:key, source:d.id, target:ref}});
    }
  });
});

const cy = cytoscape({
  container: document.getElementById('cy'),
  elements: {nodes, edges},
  style:[
    {selector:'node',style:{
      'background-color':'data(color)',
      'label':'data(label)',
      'color':'#e5e7eb',
      'font-size':'10px',
      'font-family':'ui-monospace,monospace',
      'text-valign':'bottom',
      'text-margin-y':'5px',
      'width':'30px',
      'height':'30px',
      'border-width':'2px',
      'border-color':'#111827',
    }},
    {selector:'node:selected',style:{
      'border-color':'#60a5fa',
      'border-width':'3px',
      'width':'36px',
      'height':'36px',
    }},
    {selector:'edge',style:{
      'width':1.5,
      'line-color':'#374151',
      'target-arrow-color':'#374151',
      'target-arrow-shape':'triangle',
      'curve-style':'bezier',
      'arrow-scale':0.7,
    }},
    {selector:'edge:selected',style:{
      'line-color':'#60a5fa',
      'target-arrow-color':'#60a5fa',
    }},
  ],
  layout:{
    name:'cose',
    nodeRepulsion: 10000,
    idealEdgeLength: 140,
    gravity: 0.6,
    animate: false,
    padding: 40,
  }
});

const detail = document.getElementById('detail');

function badgeClass(c){return {high:'bh',medium:'bm',low:'bl'}[c]||'bu';}

function showDetail(node){
  const d = node.data();
  const refs = [...new Set((d.why||'').match(IRP_RE)||[])].filter(r=>idSet.has(r)&&r!==d.id);
  detail.className='';
  detail.innerHTML=`
    <div class="did">${d.id}</div>
    <div class="dwhat">${escHtml(d.what)}</div>
    <div class="dmeta">
      <span class="badge ${badgeClass(d.confidence)}">${d.confidence}</span>
      ${d.tags.map(t=>`<span class="tag">${escHtml(t)}</span>`).join('')}
    </div>
    <div>
      <div class="dsec">Why</div>
      <div class="dwhy">${escHtml(d.why)||'&mdash;'}</div>
    </div>
    <div>
      <div class="dsec">Source &middot; Date</div>
      <div class="dsrc">${escHtml(d.source)||'&mdash;'} &middot; ${d.timestamp}</div>
    </div>
    ${refs.length?`
    <div>
      <div class="dsec">References</div>
      <div class="refs">${refs.map(r=>`<span class="rl" onclick="focusNode('${r}')">${r}</span>`).join('')}</div>
    </div>`:''}
  `;
}

cy.on('tap','node',evt=>showDetail(evt.target));
cy.on('tap',evt=>{
  if(evt.target===cy){
    detail.className='empty';
    detail.innerHTML='<span>Click a node to inspect</span>';
  }
});

function focusNode(id){
  const node=cy.getElementById(id);
  if(node.length){
    cy.animate({fit:{eles:node,padding:100},duration:400});
    cy.elements().unselect();
    node.select();
    showDetail(node);
  }
}

function escHtml(s){
  return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
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
        refs = set(IRP_ID_RE.findall(d.get("why") or ""))
        for ref in refs:
            key = f"{d['id']}->{ref}"
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
                f"Re-run with --force, or pass --output PATH to write elsewhere."
            ),
        }

    generated_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    edge_count = _count_edges(decisions)

    decisions_json = json.dumps(decisions, ensure_ascii=False, indent=None)

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
        f"Edges:  {edge_count} provenance reference(s)",
        "",
        "Open in any browser. Click a node to inspect.",
        "Regenerate any time with:",
        "  irp export graph",
    ])

    return {
        "command": "export.graph",
        "status": "ok",
        "output_path": str(output_path),
        "decision_count": len(decisions),
        "edge_count": edge_count,
        "text": text,
    }
