"""irp demo generate — synthetic thread and ledger entry generator for demos and onboarding."""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

from irp.core.store import append_ledger_entry, next_irp_id, read_ledger, rebuild_current, write_current

# ---------------------------------------------------------------------------
# Participant names (overridable later via flags if needed)
# ---------------------------------------------------------------------------
PARTICIPANTS = ["Johan", "Sven", "Nate"]

# ---------------------------------------------------------------------------
# Scenario templates — one entry per (scenario, confidence) pair.
# Each template defines:
#   thread : list of (speaker, message) tuples
#   what   : the decision text (honest to the confidence level)
#   why    : the reasoning (honest to the confidence level)
#   tags   : list of relevant tags
# ---------------------------------------------------------------------------
TEMPLATES: dict[str, dict[str, dict[str, Any]]] = {
    "product-decision": {
        "low": {
            "thread": [
                ("Johan", "Should we add commenting to the dashboard? Users have mentioned it a few times."),
                ("Sven",  "I think we should, but I'm not sure this sprint is the right time."),
                ("Nate",  "Agreed on the timing hesitation. We still have the onboarding work in flight."),
                ("Johan", "Could we do a very minimal version? Or just push it to next cycle?"),
                ("Sven",  "Either could work. No strong opinion from me right now."),
                ("Nate",  "Let's keep it open. Nothing decided today."),
                ("Johan", "Ok, no decision yet. We'll revisit once onboarding ships."),
            ],
            "what": "Commenting for the dashboard is under consideration — no decision made yet",
            "why": "Team is split on timing. Onboarding work in flight was cited as a blocker. Revisit pending.",
            "tags": ["product", "dashboard", "deferred"],
        },
        "medium": {
            "thread": [
                ("Johan", "We keep coming back to dashboard commenting. I think we should do a minimal version."),
                ("Sven",  "I can get behind that. Read-only threads with basic replies would cover most of the requests."),
                ("Nate",  "Scope feels manageable. I'd rather ship small and expand than wait for the full version."),
                ("Johan", "Agreed. Let's plan for a minimal commenting feature next cycle."),
                ("Sven",  "Works for me. We revisit scope after we see adoption."),
                ("Nate",  "Ok, I'm on board. Let's do it, conditionally — subject to onboarding landing cleanly first."),
            ],
            "what": "Build a minimal commenting feature for the dashboard in the next cycle",
            "why": "Covers most user requests while keeping scope manageable. Expansion decision deferred to post-adoption.",
            "tags": ["product", "dashboard", "commenting"],
        },
        "high": {
            "thread": [
                ("Johan", "We're adding commenting to the dashboard. This is confirmed."),
                ("Sven",  "Agreed. We've validated demand — teams need to annotate decisions inline, not in a separate doc."),
                ("Nate",  "Fully aligned. I'll scope the API changes this week and we ship in this cycle."),
                ("Johan", "Great. Commenting goes into the dashboard. Minimal first, threaded replies in v2."),
                ("Sven",  "Decision made. I'll update the roadmap and unblock design."),
                ("Nate",  "Moving to implementation."),
            ],
            "what": "Add commenting to the dashboard — minimal first, threaded replies in v2",
            "why": "Validated demand: teams need to annotate decisions inline. Scope is clear and implementation starts this cycle.",
            "tags": ["product", "dashboard", "commenting", "confirmed"],
        },
    },

    "architecture": {
        "low": {
            "thread": [
                ("Johan", "What should we use for the new data layer — Postgres or SQLite to start?"),
                ("Sven",  "Hard to say. Depends on how we scale this and whether we go multi-tenant."),
                ("Nate",  "SQLite is simpler. Postgres is safer long term. I don't have a strong view yet."),
                ("Johan", "We probably shouldn't decide this today without knowing the hosting model."),
                ("Sven",  "Agreed. Let's wait until we have more clarity on deployment."),
                ("Nate",  "Sounds right. No decision — let's come back to this."),
            ],
            "what": "Database layer choice (Postgres vs SQLite) is unresolved — decision deferred",
            "why": "Depends on hosting model and multi-tenancy requirements, which are not yet defined. Revisit after deployment decision.",
            "tags": ["architecture", "database", "deferred"],
        },
        "medium": {
            "thread": [
                ("Johan", "I'm leaning toward starting with SQLite for the local-first substrate. Simple, zero infra."),
                ("Sven",  "Makes sense for the initial phase. We can migrate to Postgres if we go hosted."),
                ("Nate",  "The migration path is manageable. I'd say let's start with SQLite and review at scale."),
                ("Johan", "Let's go with SQLite for now and mark it as a revisit point if/when we hit multi-user."),
                ("Sven",  "Agreed, provisionally."),
                ("Nate",  "On board. Not a permanent decision — just what we start with."),
            ],
            "what": "Start with SQLite as the local data layer — revisit if multi-user requirements emerge",
            "why": "Zero infrastructure overhead fits the local-first phase. Migration to Postgres is a known path if scale requires it.",
            "tags": ["architecture", "database", "sqlite"],
        },
        "high": {
            "thread": [
                ("Johan", "We are using SQLite for the local substrate. No exceptions. This is not up for re-debate."),
                ("Sven",  "Correct. File-based, no runtime dependency, works in air-gapped environments — it's the only choice that fits the architecture."),
                ("Nate",  "Confirmed. We do not introduce a hosted DB at this layer. If we need multi-user, we solve it at the sync layer, not the storage layer."),
                ("Johan", "Locked. SQLite only. Document this as an architectural constraint."),
                ("Sven",  "Done. I'll add it to the design doc and flag any PRs that introduce Postgres as a violation."),
            ],
            "what": "Use SQLite as the local substrate — this is an architectural constraint, not a default",
            "why": "File-based, no runtime dependency, works offline and in air-gapped environments. Multi-user solved at sync layer, not storage layer.",
            "tags": ["architecture", "database", "sqlite", "constraint"],
        },
    },

    "pricing": {
        "low": {
            "thread": [
                ("Johan", "We need to think about pricing at some point. Any initial thoughts?"),
                ("Sven",  "I've seen everything from usage-based to per-seat. Not sure what fits us."),
                ("Nate",  "Depends on who our buyer is. If it's devs, per-seat. If it's enterprise, probably something else."),
                ("Johan", "Too early to decide without knowing our first real customer profile."),
                ("Sven",  "Agreed. Let's not lock anything in right now."),
                ("Nate",  "No decision today. Just flagging we need to revisit this when we have beta users."),
            ],
            "what": "Pricing model is undecided — requires clarity on buyer profile and beta user feedback",
            "why": "Too early to commit. Buyer identity (developer vs enterprise) will determine the right model.",
            "tags": ["pricing", "business-model", "deferred"],
        },
        "medium": {
            "thread": [
                ("Johan", "I think per-seat is the right starting point for the developer phase."),
                ("Sven",  "Makes sense for PLG. Simple, transparent, no surprises."),
                ("Nate",  "Agreed, for now. We'd need a different model for enterprise — probably project or workspace-based."),
                ("Johan", "Let's start with per-seat for individual and small team plans. Enterprise pricing TBD."),
                ("Sven",  "Works for me. We can adjust once we understand enterprise buying patterns."),
                ("Nate",  "On board. Per-seat for now, with enterprise TBD."),
            ],
            "what": "Start with per-seat pricing for individual and small team plans — enterprise pricing TBD",
            "why": "Fits PLG developer motion. Simple and transparent. Enterprise model deferred until buying patterns are understood.",
            "tags": ["pricing", "per-seat", "business-model"],
        },
        "high": {
            "thread": [
                ("Johan", "We're going per-seat for the developer tier: $20/user/month. This is locked."),
                ("Sven",  "Confirmed. We tested sensitivity and $20 sits below the expense-approval threshold for most engineers."),
                ("Nate",  "Enterprise tier starts at $50k/year annual contract. We will not do monthly enterprise billing — too much churn risk."),
                ("Johan", "Locked: $20/user/month individual, $50k/year enterprise floor, no monthly enterprise. Document this."),
                ("Sven",  "Done. Pricing page reflects this. Sales told."),
            ],
            "what": "Developer tier: $20/user/month. Enterprise floor: $50k/year annual contract. No monthly enterprise billing.",
            "why": "$20 clears individual expense approval thresholds. Enterprise annual contract reduces churn risk. Model validated through pricing sensitivity testing.",
            "tags": ["pricing", "confirmed", "saas", "enterprise"],
        },
    },

    "workflow": {
        "low": {
            "thread": [
                ("Johan", "How should we handle the review process for new features? We don't have a clear flow."),
                ("Sven",  "I've been doing it ad hoc. Sometimes I ask for a PR review, sometimes I just ship."),
                ("Nate",  "We probably need something more structured, but I don't want too much overhead."),
                ("Johan", "Agreed. But I'm not sure what the right structure is yet."),
                ("Sven",  "Maybe we document what we've been doing and formalise it later?"),
                ("Nate",  "That works. No decision today — just alignment that we need a process."),
            ],
            "what": "Feature review process is informal and needs formalisation — no decision on structure yet",
            "why": "Current ad hoc approach is not scaling. Team aligned on needing structure but has not defined it.",
            "tags": ["workflow", "process", "deferred"],
        },
        "medium": {
            "thread": [
                ("Johan", "I think we need at least a lightweight PR review requirement before merging anything to main."),
                ("Sven",  "I'd support that. Even a one-person review catches most issues."),
                ("Nate",  "Agreed. Maybe we also add a short description requirement — what and why for each PR."),
                ("Johan", "Let's make that the rule: PR required, one review minimum, description with what and why."),
                ("Sven",  "Works. We can iterate on this if it creates friction."),
                ("Nate",  "On board. Let's trial it for four weeks and reassess."),
            ],
            "what": "All changes to main require a PR with at least one review and a what/why description — trial for 4 weeks",
            "why": "Lightweight gate catches most issues. Structured description improves traceability. Trial period allows adjustment.",
            "tags": ["workflow", "pr-process", "code-review"],
        },
        "high": {
            "thread": [
                ("Johan", "PR review is mandatory. No exceptions. Direct pushes to main are disabled."),
                ("Sven",  "Confirmed. I've enabled branch protection. At least one approval required before merge."),
                ("Nate",  "And we're enforcing the what/why PR description via a template. The commit hook also checks for IRP conflicts."),
                ("Johan", "Good. This is how we work. Document it in the contributing guide."),
                ("Sven",  "Done. CONTRIBUTING.md updated. New team members get this on day one."),
            ],
            "what": "PR review is mandatory for all changes to main — branch protection enabled, what/why description required",
            "why": "Direct pushes create audit gaps and break the IRP commit hook. Branch protection enforced via GitHub. PR template standardises reasoning capture.",
            "tags": ["workflow", "pr-process", "branch-protection", "confirmed"],
        },
    },

    "policy": {
        "low": {
            "thread": [
                ("Johan", "Should we have a policy on how long we keep old IRP entries in current.json?"),
                ("Sven",  "I haven't thought about it. The 10-entry window is there but I'm not sure it's right."),
                ("Nate",  "It works for now. Whether 10 is the right number, I genuinely don't know."),
                ("Johan", "Let's not change it today. We don't have enough data to know what the right number is."),
                ("Sven",  "Agreed. Keep the default, revisit when we have more ledger history."),
                ("Nate",  "No decision — keep current default and flag for review."),
            ],
            "what": "Current.json retention window (10 entries) is under review — no change decided",
            "why": "Insufficient data to determine the right window size. Default kept pending more ledger history.",
            "tags": ["policy", "substrate", "deferred"],
        },
        "medium": {
            "thread": [
                ("Johan", "I think 10 entries in current.json is probably fine for now, but we should make it configurable."),
                ("Sven",  "A config option makes sense. Different projects have different cadences."),
                ("Nate",  "We could read from an .irp/config.json if it exists, fall back to 10 if not."),
                ("Johan", "Let's plan for that. Keep 10 as the hardcoded default, add config support in the next sprint."),
                ("Sven",  "Agreed. Low priority but worth doing."),
                ("Nate",  "On board. Mark it for next sprint, not this one."),
            ],
            "what": "Current.json will remain at 10-entry default, with configurable window via .irp/config.json planned for next sprint",
            "why": "10 entries is sufficient for current use. Configurability needed for projects with different decision cadences.",
            "tags": ["policy", "substrate", "config"],
        },
        "high": {
            "thread": [
                ("Johan", "We are keeping the 10-entry window as the permanent default. It's not changing."),
                ("Sven",  "Agreed. We tested it with real projects and 10 captures 95% of relevant active context."),
                ("Nate",  "And it keeps current.json small enough that it can be read inline without performance concerns."),
                ("Johan", "Locked. 10 entries. Document it as a deliberate constraint, not a limitation."),
                ("Sven",  "Done. SPEC.md updated. This is a design decision, not a TODO."),
            ],
            "what": "Current.json active window is permanently set to 10 entries — this is a deliberate design constraint",
            "why": "10 entries captures 95% of relevant active context in tested projects. Keeps current.json lightweight and inline-readable.",
            "tags": ["policy", "substrate", "constraint", "confirmed"],
        },
    },
}

# ---------------------------------------------------------------------------
# Confidence metadata
# ---------------------------------------------------------------------------
_CONFIDENCE_NOTES: dict[str, str] = {
    "low":    "Thread shows ambiguity and partial agreement. No explicit commitment made.",
    "medium": "Thread shows leaning agreement with unresolved elements. Decision is provisional.",
    "high":   "Thread shows explicit decision language, clear reasoning, and strong commitment.",
}

# ---------------------------------------------------------------------------
# Thread renderer
# ---------------------------------------------------------------------------
def render_thread(thread: list[tuple[str, str]], scenario: str, confidence: str) -> str:
    """Format the synthetic thread as a readable plain-text block."""
    lines = [
        f"# Synthetic demo thread",
        f"# Scenario: {scenario}  |  Confidence: {confidence}",
        f"# Generated by: irp demo generate",
        f"# Note: this thread is synthetic and deterministic — not a real conversation.",
        "",
    ]
    for speaker, message in thread:
        lines.append(f"{speaker}: {message}")
    return "\n".join(lines)

# ---------------------------------------------------------------------------
# Main command runner
# ---------------------------------------------------------------------------
def run_demo(project_root: Path, irp_dir: Path, args) -> dict:
    """Dispatcher for irp demo subcommands."""
    action = getattr(args, "demo_action", None)
    if action == "generate":
        return run_demo_generate(project_root, irp_dir, args)
    return {
        "command": "demo",
        "status": "error",
        "text": "Unknown demo action. Use: irp demo generate --scenario <name> --confidence <low|medium|high>",
    }

def run_demo_generate(project_root: Path, irp_dir: Path, args) -> dict:
    scenario: str = args.scenario
    confidence: str = args.confidence
    write_thread: bool = getattr(args, "write_thread", False)
    post_to_slack: str | None = getattr(args, "post_to_slack", None)

    # Validate inputs
    valid_scenarios = list(TEMPLATES.keys())
    valid_confidence = ["low", "medium", "high"]
    if scenario not in valid_scenarios:
        return {
            "command": "demo generate",
            "status": "error",
            "text": f"Unknown scenario '{scenario}'. Valid options: {', '.join(valid_scenarios)}",
        }
    if confidence not in valid_confidence:
        return {
            "command": "demo generate",
            "status": "error",
            "text": f"Unknown confidence '{confidence}'. Valid options: low, medium, high",
        }

    # Pull template
    template = TEMPLATES[scenario][confidence]
    thread_tuples: list[tuple[str, str]] = template["thread"]
    what: str = template["what"]
    why: str = template["why"]
    tags: list[str] = list(template["tags"]) + ["demo"]

    # Render thread text
    thread_text = render_thread(thread_tuples, scenario, confidence)

    separator = "─" * 56

    # ── Slack mode ────────────────────────────────────────────────────────────
    # Post to Slack and let the Confirm button handle the ledger write.
    # Do NOT write to the local ledger in this mode.
    if post_to_slack:
        try:
            # Import here to keep the module loadable even without requests installed
            from irp.core.integrations.slack_post import post_demo_thread
        except ImportError as e:
            return {
                "command": "demo generate",
                "status": "error",
                "text": f"Cannot post to Slack — missing dependency: {e}\nInstall requests: pip install requests",
            }

        try:
            slack_result = post_demo_thread(
                channel_id=post_to_slack,
                thread_tuples=thread_tuples,
                what=what,
                why=why,
                confidence=confidence,
            )
        except Exception as e:
            return {
                "command": "demo generate",
                "status": "error",
                "text": f"Slack post failed: {e}",
            }

        lines = [
            "IRP",
            f"Project: {project_root}",
            "Command: demo generate --post-to-slack",
            "",
            separator,
            "GENERATED THREAD",
            separator,
            thread_text,
            "",
            separator,
            "POSTED TO SLACK",
            separator,
            f"  Channel:      {slack_result['channel_id']}",
            f"  Thread ts:    {slack_result['thread_ts']}",
            f"  Candidate ts: {slack_result['candidate_ts']}",
            "",
            f"  Confidence note: {_CONFIDENCE_NOTES[confidence]}",
            "",
            "  Next step: open the thread in Slack and click Confirm.",
            "  The Ledger bot will write to .irp/ledger.jsonl on confirmation.",
            separator,
        ]
        return {
            "command": "demo generate",
            "status": "posted_to_slack",
            "scenario": scenario,
            "confidence": confidence,
            "slack": slack_result,
            "thread": thread_text,
            "text": "\n".join(lines),
        }

    # ── Local mode (default) ──────────────────────────────────────────────────
    # Build ledger entry and write locally.
    ledger = read_ledger(irp_dir)
    entry: dict[str, Any] = {
        "type": "decision",
        "what": what,
        "why": why,
        "confidence": confidence,
        "tags": tags,
        "timestamp": date.today().isoformat(),
        "source": "demo",
        "origin_mode": "demo_generate",
        "scenario": scenario,
        "session_ref": f"demo-{scenario}-{confidence}-{date.today().isoformat()}",
        "id": next_irp_id(ledger),
    }

    append_ledger_entry(irp_dir, entry)
    updated_ledger = read_ledger(irp_dir)
    write_current(irp_dir, rebuild_current(updated_ledger))

    # Optionally save thread file
    thread_file_path: Path | None = None
    if write_thread:
        demo_threads_dir = irp_dir / "demo_threads"
        demo_threads_dir.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%Y%m%dT%H%M%S")
        thread_filename = f"{ts}-{scenario}-{confidence}.md"
        thread_file_path = demo_threads_dir / thread_filename
        thread_file_path.write_text(thread_text, encoding="utf-8")

    lines = [
        "IRP",
        f"Project: {project_root}",
        "Command: demo generate",
        "",
        separator,
        "GENERATED THREAD",
        separator,
        thread_text,
        "",
        separator,
        "GENERATED IRP EVENT",
        separator,
        json.dumps(entry, indent=2, ensure_ascii=False),
        "",
        separator,
        f"  Confidence note: {_CONFIDENCE_NOTES[confidence]}",
        f"  Ledger:          .irp/ledger.jsonl  ← appended",
        f"  Current:         .irp/current.json  ← rebuilt",
    ]
    if thread_file_path:
        lines.append(f"  Thread file:     .irp/demo_threads/{thread_file_path.name}  ← written")
    lines.append(separator)

    return {
        "command": "demo generate",
        "status": "generated",
        "scenario": scenario,
        "confidence": confidence,
        "entry": entry,
        "thread": thread_text,
        "thread_file": str(thread_file_path) if thread_file_path else None,
        "text": "\n".join(lines),
    }
