#!/usr/bin/env python3
"""Render a static SVG of the IRP demo decision graph for the README.

    python3 tools/render_lineage_svg.py        # writes assets/decision-lineage.svg

The point of this script is to dogfood IRP Dynamics: it takes the built-in
demo decisions (the same 18 the interactive `irp export graph --demo` uses),
derives the typed provenance edges from the reasoning text, runs the
foundations lens (personalized PageRank with a uniform teleport), and draws
the result as a plain, committable SVG that GitHub can render inline.

Layout is a small deterministic force-directed pass (no randomness, no external
dependency), so the same input always produces the same SVG. It reads ONLY the
demo dataset and never touches a real `.irp/ledger.jsonl`, so nothing
confidential can appear here.
"""
from __future__ import annotations

import html
import math
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "irp" / "core"))

import dynamics  # noqa: E402


def load_demo_decisions() -> list[dict]:
    """Load the demo decisions from graph.py without importing its heavy bits."""
    try:
        from commands.graph import _SAMPLE_DECISIONS  # type: ignore
        return list(_SAMPLE_DECISIONS)
    except Exception:
        src = (ROOT / "irp" / "core" / "commands" / "graph.py").read_text(encoding="utf-8")
        import json
        return [json.loads(o) for o in re.findall(r'\{"id":"[^"]+","type":"decision".*?\}', src)]


# ── canvas ───────────────────────────────────────────────────────────────────

W, H = 880, 600
PAD_L, PAD_R, PAD_T = 40, 40, 92
PLOT_W = W - PAD_L - PAD_R
PLOT_H = 408
PLOT_B = PAD_T + PLOT_H

RELATION_COLOR = {"depends_on": "#6C8FBF", "gates": "#E0A500", "mentions": "#C3C8D0"}


def mmdd(entry: dict) -> str:
    return str(entry.get("timestamp", ""))[5:10]  # "MM-DD"


# Short, human topic labels for the built-in demo decisions, so each node says
# what it is at a glance. Falls back to a truncated `what` for any other id.
NODE_LABELS = {
    "IRP-2026-01-10-001": "Design tokens",
    "IRP-2026-01-15-002": "Figma vars",
    "IRP-2026-01-20-003": "Token sync",
    "IRP-2026-02-01-004": "Component lib",
    "IRP-2026-02-05-005": "Dark mode",
    "IRP-2026-02-10-006": "Motion easing",
    "IRP-2026-02-20-007": "Rationale notes",
    "IRP-2026-03-01-008": "Timeboxed crits",
    "IRP-2026-03-10-009": "Brand voice",
    "IRP-2026-03-15-010": "Line weight",
    "IRP-2026-03-20-011": "REST API",
    "IRP-2026-04-01-012": "CDN delivery",
    "IRP-2026-04-05-013": "Multi-brand",
    "IRP-2026-04-10-014": "A11y audit",
    "IRP-2026-04-15-015": "AI opt-in",
    "IRP-2026-04-20-016": "Storybook docs",
    "IRP-2026-04-22-017": "Semver",
    "IRP-2026-04-25-018": "Quarterly review",
}


def node_label(entry: dict) -> str:
    nid = entry.get("id", "")
    if nid in NODE_LABELS:
        return NODE_LABELS[nid]
    what = str(entry.get("what", "")).strip()
    return (what[:16] + "...") if len(what) > 17 else what


def score_color(t: float) -> str:
    """Sequential blue ramp, light (low) to indigo (high)."""
    t = max(0.0, min(1.0, t))
    lo, hi = (0xAD, 0xCC, 0xEE), (0x27, 0x45, 0xB8)
    r, g, b = (int(lo[i] + (hi[i] - lo[i]) * t) for i in range(3))
    return f"#{r:02X}{g:02X}{b:02X}"


# ── deterministic force-directed layout ──────────────────────────────────────

def force_layout(node_ids: list[str], edges: list[dict], iters: int = 420) -> dict[str, list[float]]:
    n = len(node_ids)
    if n == 0:
        return {}
    cx, cy = PLOT_W / 2, PLOT_H / 2
    radius = min(PLOT_W, PLOT_H) * 0.36
    # Deterministic ring seed: node i at angle 2*pi*i/n. No randomness.
    pos = {nid: [cx + radius * math.cos(2 * math.pi * i / n),
                 cy + radius * math.sin(2 * math.pi * i / n)] for i, nid in enumerate(node_ids)}
    k = 0.82 * math.sqrt((PLOT_W * PLOT_H) / n)
    links = [(e["source"], e["target"]) for e in edges if e["source"] in pos and e["target"] in pos]
    temp = PLOT_W * 0.10

    for _ in range(iters):
        disp = {nid: [0.0, 0.0] for nid in node_ids}
        for i in range(n):
            a = node_ids[i]
            for j in range(i + 1, n):
                b = node_ids[j]
                dx, dy = pos[a][0] - pos[b][0], pos[a][1] - pos[b][1]
                dist = math.hypot(dx, dy) or 0.01
                f = k * k / dist
                ux, uy = dx / dist, dy / dist
                disp[a][0] += ux * f; disp[a][1] += uy * f
                disp[b][0] -= ux * f; disp[b][1] -= uy * f
        for u, v in links:
            dx, dy = pos[u][0] - pos[v][0], pos[u][1] - pos[v][1]
            dist = math.hypot(dx, dy) or 0.01
            f = dist * dist / k
            ux, uy = dx / dist, dy / dist
            disp[u][0] -= ux * f; disp[u][1] -= uy * f
            disp[v][0] += ux * f; disp[v][1] += uy * f
        for nid in node_ids:  # mild gravity keeps the cluster compact
            disp[nid][0] += (cx - pos[nid][0]) * 0.018
            disp[nid][1] += (cy - pos[nid][1]) * 0.018
        for nid in node_ids:
            dx, dy = disp[nid]
            d = math.hypot(dx, dy) or 0.01
            step = min(d, temp)
            pos[nid][0] += dx / d * step
            pos[nid][1] += dy / d * step
        temp = max(temp * 0.965, 0.5)

    # normalise the cluster into an explicit box, leaving clearance at the
    # bottom for the node labels so they do not collide with the legend.
    xs = [p[0] for p in pos.values()]
    ys = [p[1] for p in pos.values()]
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    sx = (maxx - minx) or 1.0
    sy = (maxy - miny) or 1.0
    pad = 56
    x0, x1 = PAD_L + pad, W - PAD_R - pad
    y0, y1 = PAD_T + pad, PLOT_B - 46
    for nid, p in pos.items():
        p[0] = x0 + (p[0] - minx) / sx * (x1 - x0)
        p[1] = y0 + (p[1] - miny) / sy * (y1 - y0)
    return pos


# ── svg ──────────────────────────────────────────────────────────────────────

def esc(s: str) -> str:
    return html.escape(str(s), quote=True)


def render_svg(decisions: list[dict], edges: list[dict], scores: dict[str, float]) -> str:
    node_ids = [d["id"] for d in decisions if d.get("id")]
    by_id = {d["id"]: d for d in decisions if d.get("id")}
    pos = force_layout(node_ids, edges)
    smax = max(scores.values()) if scores else 1.0
    top_id = max(scores, key=scores.get) if scores else None

    def radius(nid: str) -> float:
        return 6.0 + 17.0 * math.sqrt((scores.get(nid, 0.0) / smax) if smax else 0.0)

    p: list[str] = []
    p.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" '
        f'font-family="ui-sans-serif,system-ui,Segoe UI,Roboto,Helvetica,Arial,sans-serif" '
        f'role="img" aria-label="IRP demo decision graph, a force-directed view">'
    )
    p.append(f'<rect x="0" y="0" width="{W}" height="{H}" rx="14" fill="#ffffff" stroke="#E4E8EF"/>')
    p.append(f'<text x="{PAD_L}" y="42" font-size="21" font-weight="700" fill="#0F172A">How IRP sees its own decisions</text>')
    p.append(
        f'<text x="{PAD_L}" y="66" font-size="12.5" fill="#5B6675">'
        f'Each circle is one decision in the built-in demo ledger ({len(node_ids)} of them), the data behind '
        f'<tspan font-family="ui-monospace,Menlo,monospace" font-size="11.5">irp export graph --demo</tspan>.</text>'
    )

    # edges behind nodes
    for e in edges:
        s, t = pos.get(e["source"]), pos.get(e["target"])
        if not s or not t:
            continue
        color = RELATION_COLOR.get(e["relation"], "#C3C8D0")
        dx, dy = t[0] - s[0], t[1] - s[1]
        mx, my = (s[0] + t[0]) / 2 - dy * 0.12, (s[1] + t[1]) / 2 + dx * 0.12
        p.append(
            f'<path d="M {s[0]:.1f} {s[1]:.1f} Q {mx:.1f} {my:.1f} {t[0]:.1f} {t[1]:.1f}" '
            f'fill="none" stroke="{color}" stroke-width="1.3" stroke-opacity="0.5"/>'
        )

    # nodes
    for nid in node_ids:
        x, y = pos[nid]
        r = radius(nid)
        t = math.sqrt((scores.get(nid, 0.0) / smax)) if smax else 0.0
        if nid == top_id:  # halo on the most load-bearing decision
            p.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r + 6:.1f}" fill="none" stroke="#2745B8" stroke-opacity="0.28" stroke-width="2"/>')
        p.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r:.1f}" fill="{score_color(t)}" '
            f'stroke="#ffffff" stroke-width="1.5"/>'
        )
        p.append(
            f'<text x="{x:.1f}" y="{y + r + 12:.1f}" font-size="9.5" text-anchor="middle" '
            f'fill="#5B6675" paint-order="stroke" stroke="#ffffff" stroke-width="3" '
            f'stroke-linejoin="round">{esc(node_label(by_id[nid]))}</text>'
        )
    if top_id:
        tx, ty = pos[top_id]
        p.append(
            f'<text x="{tx:.1f}" y="{ty - radius(top_id) - 9:.1f}" font-size="9.5" font-weight="600" '
            f'text-anchor="middle" fill="#2745B8">most load-bearing</text>'
        )

    # ── legend: two rows that actually explain the encoding ──
    counts = dynamics.relation_counts(edges)
    ly1 = PLOT_B + 30
    p.append(f'<line x1="{PAD_L}" y1="{PLOT_B + 8}" x2="{W - PAD_R}" y2="{PLOT_B + 8}" stroke="#EEF1F5"/>')

    # row 1: size / shade = foundations lens
    p.append(f'<circle cx="{PAD_L + 7}" cy="{ly1 - 4}" r="4" fill="{score_color(0.15)}"/>')
    p.append(f'<circle cx="{PAD_L + 30}" cy="{ly1 - 4}" r="10" fill="{score_color(0.95)}"/>')
    p.append(
        f'<text x="{PAD_L + 50}" y="{ly1 - 1}" font-size="11.5" fill="#5B6675">'
        f'<tspan font-weight="600" fill="#3B4553">Bigger and darker = more load-bearing.</tspan> '
        f'Node size and shade are the foundations lens (PageRank).</text>'
    )

    # row 2: edge relations
    ly2 = ly1 + 26
    p.append(f'<text x="{PAD_L}" y="{ly2 + 3}" font-size="11.5" font-weight="600" fill="#3B4553">Edges</text>')
    lx = PAD_L + 52
    p.append(f'<text x="{lx}" y="{ly2 + 3}" font-size="11.5" fill="#5B6675">provenance derived from the reasoning text:</text>')
    lx += 262
    for rel, label in (("depends_on", "depends on"), ("gates", "gates"), ("mentions", "mentions")):
        c = RELATION_COLOR[rel]
        n = counts.get(rel, 0)
        p.append(f'<line x1="{lx}" y1="{ly2 - 1}" x2="{lx + 20}" y2="{ly2 - 1}" stroke="{c}" stroke-width="2.6"/>')
        p.append(f'<text x="{lx + 26}" y="{ly2 + 3}" font-size="11" fill="#5B6675">{label} ({n})</text>')
        lx += 26 + 8.6 * (len(label) + 5)

    p.append(
        f'<text x="{W - PAD_R}" y="{H - 14}" font-size="10" text-anchor="end" fill="#AAB2BF" '
        f'font-family="ui-monospace,SFMono-Regular,Menlo,monospace">demo data, regenerate with tools/render_lineage_svg.py</text>'
    )
    p.append("</svg>")
    return "\n".join(p) + "\n"


def main() -> int:
    decisions = load_demo_decisions()
    node_ids = [d["id"] for d in decisions if d.get("id")]
    edges = dynamics.derive_typed_edges(decisions)
    scores, iterations = dynamics.personalized_pagerank(node_ids, edges, seed=None)

    svg = render_svg(decisions, edges, scores)
    out = ROOT / "assets" / "decision-lineage.svg"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(svg, encoding="utf-8")

    counts = dynamics.relation_counts(edges)
    print(f"wrote {out.relative_to(ROOT)}")
    print(f"  {len(node_ids)} nodes, {len(edges)} edges {counts}, pagerank in {iterations} iterations")
    if "—" in svg:
        print("  WARNING: em dash present in output", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
