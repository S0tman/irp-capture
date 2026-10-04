"""Run the irp_inherit vs AGENTS.md eval.

    python run.py --review                 print the task set for human review
    python run.py --contexts               print the four contexts for one project
    python run.py --freeze                 write preregistration.json (hashes)
    python run.py --set test               the scored run (needs a matching freeze)
    python run.py --score results/X.jsonl  score a finished run

Keys come from eval/.env (OPENROUTER_API_KEY, GRUNDEN_API_KEY), or the
environment. They are never printed or written to the results.
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import ivsa

HERE = Path(__file__).resolve().parent
PREREG = HERE / "preregistration.json"
PRIMARY = ("anthropic/claude-haiku-4.5", "google/gemini-3.8-flash", "openai/gpt-oss-120b")
EXTRA = ("grunden/glm-5.3",)
MAX_TOKENS = 2000


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _load_keys(env_files: list[str]) -> dict[str, str]:
    keys = {k: os.environ.get(k, "") for k in ("OPENROUTER_API_KEY", "GRUNDEN_API_KEY")}
    for f in env_files:
        p = Path(f).expanduser()
        if not p.exists():
            continue
        for line in p.read_text().splitlines():
            name, _, value = line.partition("=")
            value = value.strip().strip('"').strip("'")
            if name.strip() in keys and value and not keys[name.strip()]:
                keys[name.strip()] = value
    return keys


def call(model: str, prompt: dict, keys: dict, ctx: ssl.SSLContext) -> dict:
    url, upstream, extra, key_name = ivsa.route(model)
    body = {"model": upstream, "max_tokens": MAX_TOKENS,
            "messages": [{"role": "system", "content": prompt["system"]},
                         {"role": "user", "content": prompt["user"]}], **extra}
    if "openrouter" in url:
        body["usage"] = {"include": True}
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={
        "Authorization": f"Bearer {keys[key_name]}", "Content-Type": "application/json"})
    last = ""
    for attempt in range(4):
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=120, context=ctx) as resp:
                data = json.loads(resp.read())
            msg = (data.get("choices") or [{}])[0].get("message") or {}
            usage = data.get("usage") or {}
            return {"content": msg.get("content") or "", "served": data.get("model"),
                    "latency_s": round(time.monotonic() - t0, 2),
                    "tokens_in": usage.get("prompt_tokens"), "tokens_out": usage.get("completion_tokens"),
                    "cost": usage.get("cost"), "error": None}
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code not in (408, 429, 500, 502, 503, 504):
                break
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last = type(e).__name__
        time.sleep(2 ** attempt)
    return {"content": "", "served": None, "latency_s": None, "tokens_in": None,
            "tokens_out": None, "cost": None, "error": last}


def _check_freeze() -> None:
    if not PREREG.exists():
        sys.exit("No preregistration.json: freeze the task set before a scored run.")
    want = json.loads(PREREG.read_text())["hashes"]
    have = ivsa.freeze_hashes()
    changed = [k for k in want if want[k] != have.get(k)]
    if changed:
        sys.exit(f"Frozen files changed since preregistration: {changed}")


def run(args) -> Path:
    projects = ivsa.load_projects(HERE / "projects.json")
    tasks = ivsa.load_tasks(HERE / "tasks.json")
    for t in tasks:
        ivsa.validate_task(t, projects)
    if args.set == "test":
        _check_freeze()
    if args.only:
        tasks = [t for t in tasks if t["id"] in args.only]
    elif args.limit:
        tasks = tasks[: args.limit]
    models = args.models or list(PRIMARY + EXTRA)
    keys = _load_keys(args.env_file)
    for m in models:
        if not keys[ivsa.route(m)[3]]:
            sys.exit(f"Missing {ivsa.route(m)[3]} for {m}.")
    jobs = [(t, arm, m, r) for r in range(args.runs) for t in tasks
            for arm in ivsa.ARMS for m in models]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    out = HERE / "results" / f"{stamp}-{args.set}.jsonl"
    out.parent.mkdir(exist_ok=True)
    ctx = _ssl_context()

    def one(job):
        t, arm, m, r = job
        order = ivsa.option_order(t, r)
        res = call(m, ivsa.build_prompt(t, projects[t["project"]], arm, order), keys, ctx)
        letter = ivsa.parse_choice(res["content"])
        chosen, outcome = ivsa.classify(t, order, letter)
        return {"task": t["id"], "type": t["type"], "project": t["project"], "arm": arm,
                "model": m, "run": r, "order": order, "letter": letter, "key": chosen,
                "outcome": outcome, "raw": res["content"][:2000],
                **{k: v for k, v in res.items() if k != "content"}}

    lengths = {arm: {k: len(ivsa.context(p, arm)) for k, p in projects.items()} for arm in ivsa.ARMS}
    meta = {"meta": {"set": args.set, "models": models, "primary": list(PRIMARY), "runs": args.runs,
                     "tasks": len(tasks), "calls": len(jobs), "started": stamp,
                     "max_tokens": MAX_TOKENS, "context_chars": lengths,
                     "hashes": ivsa.freeze_hashes()}}
    done = 0
    with out.open("w") as f, ThreadPoolExecutor(max_workers=args.workers) as pool:
        f.write(json.dumps(meta) + "\n")
        for row in pool.map(one, jobs):
            f.write(json.dumps(row) + "\n")
            f.flush()
            done += 1
            if done % 100 == 0 or done == len(jobs):
                print(f"{done}/{len(jobs)}", flush=True)
    return out


def report(path: Path) -> dict:
    rows = [r for r in (json.loads(l) for l in path.read_text().splitlines() if l.strip())
            if "meta" not in r]
    s = ivsa.score(rows, primary=PRIMARY)
    s["cost_usd"] = round(sum(r.get("cost") or 0 for r in rows), 4)
    s["errors"] = sum(1 for r in rows if r.get("error"))
    s["served"] = sorted({f"{r['model']} -> {r.get('served')}" for r in rows if r.get("served")})
    path.with_suffix(".score.json").write_text(json.dumps(s, indent=2) + "\n")
    pct = lambda v: "–" if v is None else f"{100 * v:.0f}%"  # noqa: E731
    print(f"\n{path.name}: {len(rows)} calls, {s['errors']} errors, ${s['cost_usd']}")
    print("\n| model | Superseded correct: none / rules / full / irp | irp − full (95% CI) | Reopen adopted: none / rules / full / irp | invalid |")
    print("|---|---|---|---|---|")
    for m, v in s["models"].items():
        a, b, ci = v["superseded"], v["reopen_adopt"], v["superseded_gap_ci"]
        ci_s = f"{pct(ci[0])} to {pct(ci[1])}" if ci else "–"
        tag = "" if m in PRIMARY else " (extra)"
        print(f"| {m}{tag} | {pct(a['none'])} / {pct(a['agents-rules'])} / {pct(a['agents-full'])} / {pct(a['irp'])} "
              f"| {pct(v['superseded_gap'])} ({ci_s}) "
              f"| {pct(b['none'])} / {pct(b['agents-rules'])} / {pct(b['agents-full'])} / {pct(b['irp'])} | {v['invalid']} |")
    v = s["verdict"]
    print(f"\nVerdict (primary models): supersession {v['supersession'].upper()} "
          f"({v['primary_clearing']}/{v['of']} clear the bar)")
    return s


def review() -> None:
    projects = ivsa.load_projects(HERE / "projects.json")
    for key, p in projects.items():
        print(f"\n## {p['name']} ({key})\n")
        gone = ivsa._superseded_ids(p)
        for d in p["decisions"]:
            mark = "~~" if d["id"] in gone else ""
            sup = f" (supersedes {d['supersedes']})" if d.get("supersedes") else ""
            print(f"- `{d['id']}` {mark}{d['what']}{mark}{sup}")
    for t in ivsa.load_tasks(HERE / "tasks.json"):
        print(f"\n### {t['id']} · {t['type']} · {t['project']}\n")
        print(f"- **Hinges on:** {', '.join(f'{k} `{v}`' for k, v in t['hinges_on'].items())}")
        print(f"- **Situation:** {t['situation']}\n- **Options:**")
        for k, text in t["options"].items():
            mark = "✅" if k in t["accept"] else ("↪︎" if k == "ask" else "❌")
            print(f"  - {mark} `{k}`: {text}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--set", choices=("test", "pilot"), default="test")
    p.add_argument("--runs", type=int, default=3)
    p.add_argument("--models", nargs="*")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--only", nargs="*", help="run only these task ids (pilot)")
    p.add_argument("--workers", type=int, default=6)
    p.add_argument("--env-file", action="append", default=[str(HERE.parent / ".env")])
    p.add_argument("--score", type=Path)
    p.add_argument("--freeze", action="store_true")
    p.add_argument("--review", action="store_true")
    p.add_argument("--contexts", metavar="PROJECT")
    p.add_argument("--pilot", action="store_true", help="unscored smoke run, skips the freeze check")
    args = p.parse_args()
    if args.review:
        review()
    elif args.contexts:
        proj = ivsa.load_projects(HERE / "projects.json")[args.contexts]
        for arm in ivsa.ARMS:
            print(f"\n===== {arm} =====\n{ivsa.context(proj, arm)}")
    elif args.freeze:
        PREREG.write_text(json.dumps({
            "registered": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "hashes": ivsa.freeze_hashes(),
            "primary_models": list(PRIMARY), "extra_models": list(EXTRA),
            "thresholds": {"superseded_gap": ivsa.GAP, "models_needed": ivsa.MODELS_NEEDED,
                           "bootstrap_n": ivsa.whyin.BOOTSTRAP_N, "bootstrap_seed": ivsa.whyin.BOOTSTRAP_SEED},
        }, indent=2) + "\n")
        print(f"Wrote {PREREG.name}")
    elif args.score:
        report(args.score)
    else:
        if args.pilot:
            args.set = "pilot"  # unscored plumbing check; never run on the test verdict
        report(run(args))


if __name__ == "__main__":
    main()
