#!/usr/bin/env python3
"""Give the repo-visualizer SVG a padded viewBox so nothing is clipped.

The githubocto/repo-visualizer action emits `<svg width="1000" height="1000"
style="...overflow:visible">` with NO viewBox. Node circles and file-name
labels are positioned by nested `translate()` transforms and can sit slightly
outside the 1000px box, so GitHub clips them when it renders the file as an
image. This walks the transform tree to find the true content bounds, then
injects a viewBox with even padding. width/height are kept, so the framed
content simply scales to fit (centred, with a small margin).

    python3 tools/frame_repo_diagram.py [path]   # defaults to assets/repo-diagram.svg
"""
from __future__ import annotations

import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

PAD = 40


def _translate(transform: str | None) -> tuple[float, float]:
    if not transform:
        return 0.0, 0.0
    m = re.search(r"translate\(\s*([-\d.]+)\s*[ ,]\s*([-\d.]+)\s*\)", transform)
    return (float(m.group(1)), float(m.group(2))) if m else (0.0, 0.0)


def _walk(el: ET.Element, ox: float, oy: float, box: list[float]) -> None:
    dx, dy = _translate(el.get("transform"))
    ox, oy = ox + dx, oy + dy
    tag = el.tag.rsplit("}", 1)[-1]
    if tag == "circle":
        r = float(el.get("r", 0) or 0)
        cx = ox + float(el.get("cx", 0) or 0)
        cy = oy + float(el.get("cy", 0) or 0)
        box[0] = min(box[0], cx - r); box[1] = min(box[1], cy - r)
        box[2] = max(box[2], cx + r); box[3] = max(box[3], cy + r)
    elif tag in ("text", "tspan"):
        tx = ox + float(el.get("x", 0) or 0)
        ty = oy + float(el.get("y", 0) or 0)
        width = len("".join(el.itertext())) * 7.5  # rough glyph width at ~14px
        anchor = el.get("text-anchor", "start")
        if anchor == "middle":
            x0, x1 = tx - width / 2, tx + width / 2
        elif anchor == "end":
            x0, x1 = tx - width, tx
        else:
            x0, x1 = tx, tx + width
        box[0] = min(box[0], x0); box[2] = max(box[2], x1)
        box[1] = min(box[1], ty - 13); box[3] = max(box[3], ty + 5)
    for child in el:
        _walk(child, ox, oy, box)


def main() -> int:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "assets/repo-diagram.svg")
    raw = path.read_text(encoding="utf-8")
    root = ET.fromstring(raw)

    box = [1e9, 1e9, -1e9, -1e9]
    _walk(root, 0.0, 0.0, box)
    if box[2] <= box[0] or box[3] <= box[1]:
        print("no drawable content found; leaving SVG unchanged", file=sys.stderr)
        return 1

    vx, vy = box[0] - PAD, box[1] - PAD
    vw, vh = (box[2] + PAD) - vx, (box[3] + PAD) - vy
    view_box = f"{vx:.0f} {vy:.0f} {vw:.0f} {vh:.0f}"

    if 'viewBox="' in raw.split(">", 1)[0]:
        out = re.sub(r'(<svg\b[^>]*?)\sviewBox="[^"]*"', rf'\1 viewBox="{view_box}"', raw, count=1)
    else:
        out = re.sub(r"<svg\b", f'<svg viewBox="{view_box}"', raw, count=1)

    path.write_text(out, encoding="utf-8")
    print(f"framed {path} with viewBox=\"{view_box}\" (content bounds "
          f"{box[0]:.0f},{box[1]:.0f} .. {box[2]:.0f},{box[3]:.0f})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
