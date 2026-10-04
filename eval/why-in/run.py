"""Run the "human why in" eval against models on OpenRouter.

    python run.py --review                 print the task set for human review
    python run.py --freeze                 write preregistration.json (hashes)
    python run.py --set dev --runs 1       pilot on the dev set
    python run.py --set test               the scored run (needs a matching freeze)
    python run.py --score results/X.jsonl  score a finished run

The API key comes from OPENROUTER_API_KEY, or from --env-file. It is never
printed or written to the results.
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

import whyin

HERE = Path(__file__).resolve().parent
PREREG = HERE / "preregistration.json"
URL = "https://openrouter.ai/api/v1/chat/completions"
MODELS = ("anthropic/claude-haiku-4.5", "google/gemini-3.8-flash", "openai/gpt-oss-120b")
MAX_TOKENS = 2000


def _ssl_context() -> ssl.SSLContext:
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _load_key(env_file: str | None) -> str:
    if env_file:
        for line in Path(env_file).expanduser().read_text().splitlines():
            if line.startswith("OPENROUTER_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        sys.exit("OPENROUTER_API_KEY not set (use the environment or --env-file).")
    return key


def call(model: str, prompt: dict, key: str, ctx: ssl.SSLContext) -> dict:
    body = json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": prompt["system"]},
                     {"role": "user", "content": prompt["user"]}],
        "max_tokens": MAX_TOKENS,
        "usage": {"include": True},
    }).encode()
    req = urllib.request.Request(URL, data=body, headers={
        "Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    last = ""
    for attempt in range(4):
        t0 = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=120, context=ctx) as resp:
                data = json.loads(resp.read())
            msg = (data.get("choices") or [{}])[0].get("message") or {}
            usage = data.get("usage") or {}
            return {"content": msg.get("content") or "",
                    "latency_s": round(time.monotonic() - t0, 2),
                    "tokens_in": usage.get("prompt_tokens"),
                    "tokens_out": usage.get("completion_tokens"),
                    "cost": usage.get("cost"),
                    "provider": data.get("provider"), "error": None}
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code not in (408, 429, 500, 502, 503, 504):
                break
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            last = type(e).__name__
        time.sleep(2 ** attempt)
    return {"content": "", "latency_s": None, "tokens_in": None, "tokens_out": None,
            "cost": None, "provider": None, "error": last}


def _check_freeze() -> None:
    if not PREREG.exists():
        sys.exit("No preregistration.json: freeze the task set before a scored run.")
    want = json.loads(PREREG.read_text())["hashes"]
    have = whyin.freeze_hashes()
    changed = [k for k in want if want[k] != have.get(k)]
    if changed:
        sys.exit(f"Frozen files changed since preregistration: {changed}")


def run(args) -> Path:
    tasks = whyin.load_tasks(HERE / ("tasks.json" if args.set == "test" else "dev.json"))
    for t in tasks:
        whyin.validate_task(t)
    if args.set == "test":
        _check_freeze()
    if args.limit:
        tasks = tasks[: args.limit]
    models = args.models or list(MODELS)
    jobs = [(t, arm, m, r) for r in range(args.runs) for t in tasks
            for arm in whyin.ARMS for m in models]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
    out = HERE / "results" / f"{stamp}-{args.set}.jsonl"
    out.parent.mkdir(exist_ok=True)
    key, ctx = _load_key(args.env_file), _ssl_context()

    def one(job):
        t, arm, m, r = job
        order = whyin.option_order(t, r)
        res = call(m, whyin.build_prompt(t, arm, order), key, ctx)
        letter = whyin.parse_choice(res["content"])
        chosen, outcome = whyin.classify(t, order, letter)
        return {"task": t["id"], "type": t["type"], "arm": arm, "model": m, "run": r,
                "order": order, "letter": letter, "key": chosen, "outcome": outcome,
                "raw": res["content"][:2000], **{k: v for k, v in res.items() if k != "content"}}

    meta = {"meta": {"set": args.set, "models": models, "runs": args.runs,
                     "tasks": len(tasks), "calls": len(jobs), "started": stamp,
                     "max_tokens": MAX_TOKENS, "hashes": whyin.freeze_hashes()}}
    done = 0
    with out.open("w") as f, ThreadPoolExecutor(max_workers=args.workers) as pool:
        f.write(json.dumps(meta) + "\n")
        for row in pool.map(one, jobs):
            f.write(json.dumps(row) + "\n")
            f.flush()
            done += 1
            if done % 50 == 0 or done == len(jobs):
                print(f"{done}/{len(jobs)}", flush=True)
    return out


def load_rows(path: Path) -> list[dict]:
    return [r for r in (json.loads(l) for l in path.read_text().splitlines() if l.strip())
            if "meta" not in r]


def report(path: Path) -> dict:
    rows = load_rows(path)
    s = whyin.score(rows)
    cost = sum(r.get("cost") or 0 for r in rows)
    s["cost_usd"] = round(cost, 4)
    s["errors"] = sum(1 for r in rows if r.get("error"))
    (path.with_suffix(".score.json")).write_text(json.dumps(s, indent=2) + "\n")
    pct = lambda v: "–" if v is None else f"{100 * v:.0f}%"
    print(f"\n{path.name}: {len(rows)} calls, {s['errors']} errors, ${s['cost_usd']}")
    print("\n| model | Extend: bare / reason / filler / stale | gap (95% CI) | Hold wrongful: bare / reason | invalid |")
    print("|---|---|---|---|---|")
    for m, v in s["models"].items():
        e, h, ci = v["extend"], v["hold_wrongful"], v["extend_gap_ci"]
        ci_s = f"{pct(ci[0])} to {pct(ci[1])}" if ci else "–"
        print(f"| {m} | {pct(e['bare'])} / {pct(e['reason'])} / {pct(e['filler'])} / {pct(e['stale'])} "
              f"| {pct(v['extend_gap'])} ({ci_s}) | {pct(h['bare'])} / {pct(h['reason'])} | {v['invalid']} |")
    v = s["verdict"]
    print(f"\nVerdict: claim {v['claim'].upper()} (models clearing {v['models_clearing']}/{v['of']}, "
          f"hold safety {v['hold_safety']}, wrongful delta {pct(v['hold_wrongful_delta'])})")
    return s


def review() -> None:
    for t in whyin.load_tasks(HERE / "tasks.json"):
        print(f"\n### {t['id']} · {t['type']} · {t['domain']}\n")
        print(f"- **Rule:** {t['rule']}\n- **Why:** {t['why']}\n- **Turned down:** {t['turned_down']}")
        print(f"- **Stale why:** {t['stale_why']}\n- **Background:** {t['background']}")
        print(f"- **Situation:** {t['situation']}\n- **Options:**")
        for k, text in t["options"].items():
            mark = "✅" if k in t["accept"] else ("❌" if k != "ask" else "↪︎")
            print(f"  - {mark} `{k}`: {text}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--set", choices=("dev", "test"), default="dev")
    p.add_argument("--runs", type=int, default=3)
    p.add_argument("--models", nargs="*")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--workers", type=int, default=6)
    p.add_argument("--env-file")
    p.add_argument("--score", type=Path)
    p.add_argument("--freeze", action="store_true")
    p.add_argument("--review", action="store_true")
    args = p.parse_args()
    if args.review:
        review()
    elif args.freeze:
        PREREG.write_text(json.dumps({
            "registered": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "hashes": whyin.freeze_hashes(),
            "thresholds": {"extend_gap": whyin.EXTEND_GAP, "models_needed": whyin.MODELS_NEEDED,
                           "hold_limit": whyin.HOLD_LIMIT, "bootstrap_n": whyin.BOOTSTRAP_N,
                           "bootstrap_seed": whyin.BOOTSTRAP_SEED},
        }, indent=2) + "\n")
        print(f"Wrote {PREREG.name}")
    elif args.score:
        report(args.score)
    else:
        report(run(args))


if __name__ == "__main__":
    main()
