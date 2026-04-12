# Appendix: Practical Considerations

This appendix covers implementation details and team scaling scenarios not addressed in the main chapters.

---

## Team Scaling: 10 People vs. 100

IRP's design scales, but scaling changes how you use it.

### Small Teams (2-15 people)

**Decision capture:** Everyone can capture. Use interactive mode. Preview before confirming.

**Active window:** Last 10 decisions is fine. Decisions are recent, relevant.

**Conflict detection:** Keywords + stopwords work well. Few false positives because domain is focused.

**Access:** Everyone has .irp/ access. JSONL is readable by anyone with filesystem access.

**Typical cadence:** 2-5 decisions per week. Ledger grows ~100-200 entries/year.

### Medium Teams (15-50 people)

**Decision capture:** Consider role-based capture (only architects/leads can capture). Or: everyone captures, but requires async review before ledger write.

**Active window:** Increase to 15-20. More decisions, more recent context needed.

**Conflict detection:** Start tuning stopword list. Domain-specific language creates false positives. Track hit rate.

**Access:** Consider encrypted .irp/ or file-level permissions. Who should read sensitive decisions?

**Typical cadence:** 10-20 decisions/week. Ledger grows ~500-1000 entries/year.

### Large Teams (50+ people)

**Decision capture:** Centralize. Design review board or architecture committee captures formal decisions. Sensor auto-capture (Slack, GitHub) for informal decisions.

**Active window:** Reduce to 5-8. Signal-to-noise ratio matters at scale.

**Conflict detection:** Embeddings become viable. Keywords alone create too much noise. Consider migrating to semantic matching.

**Access:** Role-based ledger access. Decisions by domain/team. Cross-team decisions in shared namespace.

**Typical cadence:** 50+ decisions/week. Ledger grows 2000+ entries/year. Consider archiving or sharding by year.

**Note:** At 50+, IRP becomes a system component, not a tool. Expect infrastructure investment (indexing, API caching, audit logging).

---

## Multi-Repository Setups

IRP assumes one .irp/ per project. What if your organization has 50 repos?

### Option 1: Federated (Recommended for Most Teams)

Each repo has its own .irp/. Decisions are local.

**Pros:**
- Simple (no cross-repo sync)
- Clear ownership (repo owns its decisions)
- Portable (each repo is self-contained)

**Cons:**
- Cross-repo decisions require duplication or linking
- No unified search across all decisions

**Use case:** Microservices, monorepos with team boundaries, separate projects.

### Option 2: Centralized

Single shared .irp/ that all repos reference.

**Pros:**
- One source of truth
- Easy cross-repo search
- Unified governance

**Cons:**
- Shared state (one team's decision affects others)
- Merge conflicts if multiple teams write simultaneously
- Complex access control

**Use case:** Tightly coupled monoliths, shared infrastructure decisions.

### Option 3: Hierarchical

Root .irp/ for org-level decisions. Sub-projects have their own .irp/.

**Pros:**
- Scales to many repos
- Separates concerns (org vs. project)
- Clear inheritance (projects inherit org decisions)

**Cons:**
- More complex (need inheritance logic)
- Multiple sources of truth (which .irp/ wins in a conflict?)

**Use case:** Large organizations with subsidiaries or business units.

**Recommendation:** Start with federated (Option 1). Migrate to centralized or hierarchical only if you hit pain points (many cross-repo decisions, difficult search).

---

## Ledger Maintenance

IRP's ledger is append-only. It never deletes. But you'll eventually want to maintain it.

### Archiving Old Decisions

After 2-3 years, you might have 1000+ decisions. Most are irrelevant. Keep the ledger trim.

**Process:**
1. Define archive threshold (e.g., decisions older than 2 years)
2. Copy old entries to `ledger.archive.jsonl`
3. Remove from `ledger.jsonl`
4. Rebuild `current.json`

**Consequence:** Old decisions become unsearchable via `/why --id`. Keep `ledger.archive.jsonl` for archaeology.

**Alternative:** Don't archive. JSONL files are tiny (1000 entries ≈ 100KB). Searching is still fast.

### Cleanup: Removing Duplicates

If you captured the same decision twice, you'll have duplicates:

```
{"id":"IRP-2026-04-12-001","what":"Use React",...}
{"id":"IRP-2026-04-12-002","what":"Use React",...}  // duplicate
```

**Fix:**
1. Identify the duplicate
2. Add a note entry: `{"type":"note","what":"IRP-2026-04-12-002 is duplicate of IRP-2026-04-12-001"}`
3. Remove the duplicate line from ledger
4. Rebuild `current.json`

**Risk:** Never edit `.irp/ledger.jsonl` directly. Always add a note entry explaining the change.

### Backups

Your ledger is your audit trail. Back it up.

```bash
# Daily backup
cp .irp/ledger.jsonl backups/ledger.$(date +%Y-%m-%d).jsonl

# Or: commit to git (if .irp/ is git-tracked)
git add .irp/ledger.jsonl
git commit -m "Ledger checkpoint"
```

---

## Security: Who Can Read .irp/?

By default, anyone with filesystem access to your project can read .irp/. For most teams, that's fine. For sensitive decisions, it's not.

### Scenario 1: All Decisions are Public

Everyone on the team should see all decisions. No encryption needed.

**Implementation:**
- .irp/ is readable by all team members
- Decisions are version-controlled (commit .irp/)
- No special access control

### Scenario 2: Some Decisions are Sensitive

Financial, legal, or strategic decisions should be visible only to leadership.

**Implementation Option A: Separate Ledgers**
- Main .irp/ for public decisions
- Confidential/.irp/ for sensitive decisions
- CI/CD, bots, external systems only read main

**Implementation Option B: Encrypted Entries**
- All decisions in one .irp/
- Sensitive entries are encrypted: `{"type":"decision","what":"ENCRYPTED","encrypted_data":"..."}`
- Only authorized systems decrypt

**Implementation Option C: Access Control Layer**
- Ledger stays on filesystem
- REST API enforces auth: `/inherit` only returns decisions user has access to
- Requires centralized API (not local filesystem access)

**Recommendation:** Start with Scenario 1 (all decisions public). If you need secrecy, migrate to Option A (separate ledgers) or Option C (API with auth). Option B (encrypted) is complex and rarely needed.

### Scenario 3: Regulatory Compliance

Some industries (finance, healthcare) require audit trails and access logs.

**Implementation:**
- All writes to .irp/ are logged: user, timestamp, decision ID
- Reads are logged when fetched via REST API
- Logs are immutable (append-only, like the ledger)
- Automated retention policy (keep 7 years for compliance)

**Tools:** You'll need logging infrastructure (not built into IRP). Add API logging before using IRP in regulated environments.

---

## Performance: When Does IRP Get Slow?

IRP's heuristics are simple, but they have costs.

### Linear Scan (O(n))

Conflict detection scans all active decisions:

```
for each active_decision:
  if overlap(proposal, active_decision):
    return match
```

At 10 active decisions: < 1ms  
At 100 active decisions: < 10ms  
At 1000 active decisions: slow (10s+)

**Solution:** Keep active window small (5-10 for large teams). Archive old decisions.

### Ledger Rebuild (O(n))

On every decision capture, IRP scans the entire ledger to rebuild current.json:

```
active = [d for d in ledger if d.type == "decision"][-10:]
```

At 1000 entries: < 10ms  
At 10,000 entries: ~100ms  
At 100,000 entries: ~1s (too slow)

**Solution:** Shard by year. Rebuild only the current year's decisions.

### REST API Queries

/inherit returns all active decisions. At 100+ decisions, response size gets large.

**Solution:** Add pagination or filtering. Return top-N decisions, let client request more.

---

## When NOT to Use IRP

IRP is designed for decisions that need durability and portability. Not all decisions fit.

**Good fit for IRP:**
- Architecture decisions (database choice, framework)
- Design decisions (color scheme, navigation)
- Process decisions (deployment strategy, code review policy)
- Team decisions (tool preferences, working hours)

**Poor fit for IRP:**
- Tactical decisions (which function to refactor next week)
- Operational decisions (who's on call today)
- Ephemeral decisions (what to build in the next 2 hours)
- Personal decisions (which issue to work on)

**Rule of thumb:** If the decision will be relevant for > 3 months, or if 10+ people need to know about it, capture it in IRP. Otherwise, use Slack.

---

## Extending IRP: Common Customizations

These are patterns teams have used to extend IRP:

### Custom Stopwords

Your domain might have domain-specific stopwords:

```python
# For infrastructure teams, "deploy" is frequent and not a conflict signal
stopwords = DEFAULT_STOPWORDS + ["deploy", "infrastructure", "service"]

# For frontend teams, "component" is common
stopwords = DEFAULT_STOPWORDS + ["component", "ui", "view"]
```

### Custom Metadata Fields

Your decisions might benefit from extra fields:

```json
{
  "type": "decision",
  "what": "Use React",
  "why": "Team expertise",
  "owner": "frontend-team",     // custom
  "priority": "high",            // custom
  "review_date": "2026-07-12",  // custom: when to re-evaluate
  "risk_level": "low"            // custom
}
```

Add these to your capture prompt, validate in the bridge server.

### Integration Hooks

When a decision is captured, notify external systems:

```python
# After ledger append:
send_slack_message(f"New decision: {entry['what']}")
create_github_issue(f"Implement: {entry['what']}")
post_to_wiki(entry)
```

---

## Troubleshooting

### "Conflict detection is too noisy"

Too many false positives. Solutions:
1. Tune stopwords (add domain-specific words)
2. Reduce active window (fewer decisions = fewer matches)
3. Switch to semantic matching (use embeddings, harder to implement)

### "I can't find old decisions"

Your ledger is 5000+ entries. Linear search is slow.

Solutions:
1. Archive old decisions (move to separate file)
2. Add indexing (map decision IDs to ledger offsets)
3. Migrate to database (lose portability, gain speed)

### ".irp/ is getting too big"

Ledger is 10MB+. Git history is bloated.

Solutions:
1. Move .irp/ out of git version control (use git-lfs or separate backup)
2. Archive old decisions
3. Compress history (rare, usually not worth it)

---

**End of Appendix**
