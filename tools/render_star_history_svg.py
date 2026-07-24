#!/usr/bin/env python3
"""Render a self-hosted star-history SVG for the README.

Why self-hosted: star-history.com's public embed no longer works for this repo.
GitHub now restricts star TIMESTAMP data to a repo's own admins and
collaborators, so star-history's unauthenticated server gets a 401 and the
image 500s. This script fetches the data itself, using GITHUB_TOKEN when run in
CI (which is a collaborator-level token), and draws a small cumulative-stars
chart as a committable SVG. No third-party service, nothing to break.

    GITHUB_TOKEN=... python3 tools/render_star_history_svg.py

Env:
    GITHUB_TOKEN       optional locally, required to read star timestamps
                       (GitHub Actions provides it automatically).
    GITHUB_REPOSITORY  owner/repo (Actions provides it); defaults below.

If timestamps cannot be read (no token, or the API denies them), it still
writes a valid SVG: a clean card showing the current star count, so the README
image is never broken.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPO = os.environ.get("GITHUB_REPOSITORY") or "S0tman/irp-capture"
TOKEN = (os.environ.get("GITHUB_TOKEN") or "").strip()
API = "https://api.github.com"
W, H = 760, 400
AX = "#5B6675"       # axis / label ink
LINE = "#2745B8"     # star line
AREA = "#DCE6FA"     # area fill


def _get(url: str, media: str = "application/vnd.github+json"):
    req = urllib.request.Request(url)
    req.add_header("Accept", media)
    req.add_header("User-Agent", "irp-star-history")
    if TOKEN:
        req.add_header("Authorization", f"Bearer {TOKEN}")
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode("utf-8"))


def repo_info() -> tuple[int, datetime]:
    data = _get(f"{API}/repos/{REPO}")
    created = datetime.fromisoformat(str(data["created_at"]).replace("Z", "+00:00"))
    return int(data.get("stargazers_count", 0)), created


def star_timestamps() -> list[datetime]:
    """Sorted starred_at datetimes, or [] if the API will not give them."""
    out: list[datetime] = []
    page = 1
    while True:
        try:
            rows = _get(f"{API}/repos/{REPO}/stargazers?per_page=100&page={page}",
                        media="application/vnd.github.star+json")
        except urllib.error.HTTPError as e:
            print(f"  stargazers page {page}: HTTP {e.code} {e.reason}", file=sys.stderr)
            return []
        if not rows:
            break
        for row in rows:
            ts = row.get("starred_at") if isinstance(row, dict) else None
            if ts:
                out.append(datetime.fromisoformat(ts.replace("Z", "+00:00")))
        if len(rows) < 100:
            break
        page += 1
    return sorted(out)


def esc(s: str) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _card_open(title_extra: str) -> list[str]:
    return [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" '
        f'font-family="ui-sans-serif,system-ui,Segoe UI,Roboto,Helvetica,Arial,sans-serif" '
        f'role="img" aria-label="Star history for {esc(REPO)}">',
        f'<rect x="0" y="0" width="{W}" height="{H}" rx="14" fill="#ffffff" stroke="#E4E8EF"/>',
        f'<text x="34" y="42" font-size="20" font-weight="700" fill="#0F172A">Star history</text>',
        f'<text x="34" y="63" font-size="12.5" fill="{AX}">{esc(REPO)}{title_extra}</text>',
    ]


def render_chart(total: int, created: datetime, stars: list[datetime]) -> str:
    now = datetime.now(timezone.utc)
    # cumulative step series: start at 0 on the first star's day (or creation),
    # step up by one at each star, then hold flat to today.
    t0 = stars[0] if stars else created
    pts = [(t0, 0)]
    for i, ts in enumerate(sorted(stars)):
        pts.append((ts, i + 1))
    pts.append((now, len(stars)))

    px0, px1, py0, py1 = 54, W - 24, 74, H - 42
    span = (pts[-1][0] - pts[0][0]).total_seconds() or 1.0
    ymax = max(total, 1)

    def xf(t):
        return px0 + (t - pts[0][0]).total_seconds() / span * (px1 - px0)

    def yf(c):
        return py1 - c / ymax * (py1 - py0)

    p = _card_open(f" &middot; {total} star{'s' if total != 1 else ''}")

    # y gridlines + integer ticks (at most 5)
    step = max(1, -(-ymax // 4))
    c = 0
    while c <= ymax:
        y = yf(c)
        p.append(f'<line x1="{px0}" y1="{y:.1f}" x2="{px1}" y2="{y:.1f}" stroke="#EEF1F5"/>')
        p.append(f'<text x="{px0 - 8}" y="{y + 4:.1f}" font-size="10.5" text-anchor="end" fill="{AX}">{c}</text>')
        c += step

    # x date ticks (start, middle, end)
    for frac in (0.0, 0.5, 1.0):
        t = pts[0][0] + (pts[-1][0] - pts[0][0]) * frac
        x = xf(t)
        anchor = "start" if frac == 0 else ("end" if frac == 1 else "middle")
        p.append(f'<text x="{x:.1f}" y="{py1 + 22:.1f}" font-size="10.5" text-anchor="{anchor}" fill="{AX}">{t.strftime("%b %Y")}</text>')

    # staircase path (area, then line)
    d_line = [f'M {xf(pts[0][0]):.1f} {yf(0):.1f}']
    prev_c = 0
    for t, cc in pts[1:]:
        d_line.append(f'L {xf(t):.1f} {yf(prev_c):.1f}')
        d_line.append(f'L {xf(t):.1f} {yf(cc):.1f}')
        prev_c = cc
    line = " ".join(d_line)
    area = line + f' L {xf(pts[-1][0]):.1f} {py1:.1f} L {xf(pts[0][0]):.1f} {py1:.1f} Z'
    p.append(f'<path d="{area}" fill="{AREA}" fill-opacity="0.7"/>')
    p.append(f'<path d="{line}" fill="none" stroke="{LINE}" stroke-width="2.4" stroke-linejoin="round"/>')
    for t in stars:
        p.append(f'<circle cx="{xf(t):.1f}" cy="{yf(stars.index(t) + 1):.1f}" r="3.4" fill="{LINE}"/>')

    p.append(f'<text x="{W - 24}" y="{H - 14}" font-size="10" text-anchor="end" fill="#AAB2BF" '
             f'font-family="ui-monospace,SFMono-Regular,Menlo,monospace">self-hosted, refreshed by CI</text>')
    p.append("</svg>")
    return "\n".join(p) + "\n"


def render_count_only(total: int) -> str:
    p = _card_open("")
    p.append(f'<text x="{W/2:.0f}" y="215" font-size="86" font-weight="800" text-anchor="middle" fill="{LINE}">{total}</text>')
    p.append(f'<text x="{W/2:.0f}" y="255" font-size="15" text-anchor="middle" fill="{AX}">star{"s" if total != 1 else ""} so far</text>')
    p.append(f'<text x="{W/2:.0f}" y="322" font-size="12" text-anchor="middle" fill="#98A2B3">The dated history renders in CI, where a token can read star timestamps.</text>')
    p.append(f'<text x="{W - 24}" y="{H - 14}" font-size="10" text-anchor="end" fill="#AAB2BF" '
             f'font-family="ui-monospace,SFMono-Regular,Menlo,monospace">self-hosted, refreshed by CI</text>')
    p.append("</svg>")
    return "\n".join(p) + "\n"


def main() -> int:
    try:
        total, created = repo_info()
    except Exception as e:  # network / repo lookup failed
        print(f"repo lookup failed: {e}", file=sys.stderr)
        return 1
    stars = star_timestamps()
    svg = render_chart(total, created, stars) if stars else render_count_only(total)
    if "—" in svg:
        print("WARNING: em dash present in output", file=sys.stderr)
    out = ROOT / "assets" / "star-history.svg"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(svg, encoding="utf-8")
    mode = f"chart ({len(stars)} dated stars)" if stars else f"count-only ({total} stars, no timestamps)"
    print(f"wrote {out.relative_to(ROOT)}: {mode}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
