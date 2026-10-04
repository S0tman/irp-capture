# Eval: does IRP help an agent beyond a well-kept AGENTS.md?

The [human-why-in eval](../why-in/README.md) showed that agents follow rules better when they get the reason. But an AGENTS.md can carry reasons too. This eval asks what IRP adds **on top of the same facts**. It was designed and registered before any scored run, and the results are published whatever they say.

## The question

1. **Superseded decisions.** When a decision has been replaced, does an agent follow the current one more reliably with IRP's active decisions (`irp_inherit`) than with an AGENTS.md where the history has piled up?
2. **Rejected options.** When a request leans towards an option the team turned down, does the agent hold the line? This is reported for every arm, without a verdict.

## Design

**Four synthetic projects** ([projects.json](projects.json)): a payments API, a design system, a data platform and an HR app. Each is a record of 10 to 11 decisions built up over time. Nine decisions supersede earlier ones, and most name the options turned down.

**24 tasks** ([tasks.json](tasks.json)). Each task belongs to one project and names the decision IDs it depends on, and the tests check those against the project record. The model answers in JSON with a letter, so scoring is mechanical.

| Type | Count | Situation | Counted as correct |
|---|---|---|---|
| Superseded | 10 | Following the old decision leads to the wrong choice | the current decision; asking is reported separately |
| Reopen | 10 | The request leans towards a rejected option | keeping the decision, or asking the owner |
| Plain | 4 | The rule clearly applies (sanity check) | following the rule |

**Four arms.** The task stays the same, and only the project context changes:

1. **none:** no project context.
2. **agents-rules:** an AGENTS.md listing the current rules only, which is how most teams write it.
3. **agents-full:** an AGENTS.md carrying every fact the IRP record holds: dates, reasons, alternatives and the full history, oldest first. Superseded decisions stay in the file. Later entries are headed "Update:" but don't say what they replace, the way a growing file usually reads.
4. **irp:** exactly what the `irp_inherit` MCP tool returns. It's built by the real `run_inherit` from a seeded ledger per project, so it contains active decisions only, each with its why, alternatives and `supersedes` link.

**The comparison that matters is irp against agents-full.** Both hold the same facts; irp is curated and structured. Note that irp is shorter, because superseded entries are dropped. That's part of what IRP does, but it means length isn't held constant. The run records each context's length.

`irp_inherit` was fixed before this eval (commit 8dfa7b2): it used to return the last 10 entries whatever their status. The eval tests the fixed tool, and its source is among the frozen files.

**Models.**
- The primary models, which decide the verdict: `anthropic/claude-haiku-4.5`, `google/gemini-3.8-flash` and `openai/gpt-oss-120b`, through OpenRouter.
- An extra model, reported but not in the verdict: `glm-5.3` through Grunden.ai, with `reasoning_effort: "low"`, the way IRP Compliance runs it.
- All use their default temperature, with `max_tokens` set to 2,000.

**Three runs.** The option order is shuffled per task and run, the same for every arm and model. That's 24 × 4 × 4 × 3 = **1,152 calls**.

**Pilot.** A plumbing check on the sanity task P04 only: 12 calls to the OpenRouter models, then 4 to GLM-5.3 once a valid Grunden key was in place (the first GLM attempt failed with HTTP 401 because a key ID had been pasted instead of the key). It checked for errors and readable answers, and nothing in the test set was changed because of it. The GLM pilot file is in `results/`.

## Metrics

- **Superseded accuracy** per model and arm, with the **irp minus agents-full gap** and a 95% paired bootstrap interval over tasks (2,000 resamples, seed 20261004, code shared with the first eval).
- **Reopen:** the share of answers that adopt the rejected option, per arm, with the agents-full minus irp gap and its interval.
- **Plain adherence.** This should be near 100%.
- **Ask rates** on Superseded and Reopen, per arm.

## Kill criteria (registered)

- **Supersession supported:** in at least 2 of the 3 primary models, irp beats agents-full on Superseded tasks by **10 points or more** and the 95% interval excludes zero.
- **Supersession killed:** in at least 2 primary models, the whole interval sits below 10 points.
- **Anything else is inconclusive.**
- **Rejected options:** no verdict. The honest expectation, registered here, is that irp and agents-full will be close, because both state the rejected options. Both should beat agents-rules.
- **If irp ties agents-full on supersession:** IRP's edge isn't in how an agent reads the record. It's in keeping the record: confirmed when the decision is made, kept current through `supersedes`, and tamper-evident. The README would then say that a well-kept AGENTS.md with reasons steers as well, and that IRP's job is to keep it that way.

## Run it

```bash
cd eval/inherit-vs-agents
python run.py --review                     # tasks and projects, as a review sheet
python run.py --contexts ledgerly          # the four contexts for one project
python run.py --set test                   # the scored run (keys in eval/.env)
python run.py --score results/<file>.jsonl
```

Keys go in `eval/.env` (copy [../.env.example](../.env.example)). They are never printed or written to the results.

## Review

Claude drafted the projects and tasks. The maintainer reviewed every expected answer ([review.md](review.md)) before the freeze.

## Results (scored run, 4 October 2026)

Run `20261004-2139-test`: 1,152 calls, 0 errors, $0.62 (GLM-5.3 was free). There were 2 unreadable answers, both GLM replies missing their closing brace, which the frozen parser couldn't read; both are excluded. Raw answers: [results/20261004-2139-test.jsonl](results/20261004-2139-test.jsonl). Scores: [results/20261004-2139-test.score.json](results/20261004-2139-test.score.json).

**Verdict: supersession killed.** No primary model shows any gap: irp and agents-full were both perfect.

| Model | Superseded correct: none / agents-rules / agents-full / irp | irp minus agents-full | Reopen: rejected option adopted, any arm |
|---|---|---|---|
| claude-haiku-4.5 | 3% / 100% / 100% / 100% | 0 points | 0% |
| gemini-3.8-flash | 0% / 100% / 100% / 100% | 0 points | 0% |
| gpt-oss-120b | 23% / 100% / 100% / 100% | 0 points | 0% |
| glm-5.3 (extra) | 14% / 100% / 100% / 100% | 0 points | 0% (3% with no context) |

**What it shows:**

- **At this size, a plain AGENTS.md is enough.** With about 10 decisions per project and dated "Update:" entries, every model picked the current decision every time, whether it came from IRP or from a file with the full history. IRP's structure added nothing to how the agent read the record.
- **Context mattered; its form didn't.** Without any context, models mostly asked the owner (97 of 119 valid answers on Superseded) and rarely guessed right. Any of the three context forms fixed that completely.
- **The Reopen tasks didn't discriminate.** No model adopted a rejected option in any arm, even with no context at all. The "keep" options were simply better engineering, so these tasks don't test whether the record helps. That's a flaw in the task design, recorded here rather than fixed after the fact.
- **The irp context was the longest, not the shortest.** The registered design assumed irp would be shorter because it drops superseded entries. In fact the JSON the MCP tool returns ran to 12,215 characters across the four projects, against 7,502 for agents-full and 2,265 for agents-rules. That's a real cost, and a finding about `irp_inherit` itself: a compact text form would serve agents better.

**What this means for IRP** (the consequence registered above): the edge isn't in how an agent reads the record. A well-kept AGENTS.md with reasons steers just as well. IRP's job is keeping it well kept: capturing the decision when it's made, recording what each new decision supersedes, and keeping a tamper-evident history. `irp export context --target agents.md` already writes the AGENTS.md from the ledger.

**Limits, and what a harder test would need:** small records (10 to 11 decisions), explicit "Update:" headings, and tasks that name their topic directly. Curation should matter more with hundreds of decisions, supersessions buried far from what they replace, and no update markers. That's a separate eval, to be designed and registered on its own, not a reason to reread this one.
