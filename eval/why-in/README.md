# Eval: does the human why help agents follow rules?

IRP's README says agents can read the reasons behind your decisions "so they don't reopen settled debates or break a rule they can't see the point of". This eval tests that sentence. It was designed and registered before any scored run, and the results are published whatever they say.

## The question

When an agent gets the reason behind a rule, does it apply the rule better to cases the rule doesn't name? Or does it only do better because the prompt got longer? And does knowing the reason make it take exceptions it shouldn't?

This is a prompt-level test: it gives the agent the decision record the way `irp_inherit` would. It doesn't test the MCP plumbing.

## Design

**24 tasks** ([tasks.json](tasks.json)). Each task has a decision record, some background, a situation and three options. The model answers in JSON with a letter, so scoring is mechanical and no judge model is involved.

| Type | Count | What it tests | Counted as correct |
|---|---|---|---|
| Plain | 4 | The rule clearly applies. A sanity check. | comply |
| Extend | 10 | The rule doesn't name this case, but its reason covers it. | the option the reason implies |
| Hold | 10 | The rule applies, but its reason looks satisfied this time. | comply, or ask the owner |

Every task offers an "ask the owner" option. On Extend tasks, asking is reported separately and doesn't count as correct. On Hold tasks, choosing the deviating option counts as a **wrongful exception**.

**Four arms.** Each task is run in every arm, and only the decision record changes:

1. **bare:** the rule alone.
2. **reason:** the rule, why, and the options turned down. This is IRP's record form.
3. **filler:** the rule plus unrelated office notes, within 15% of the length of the reason arm. It tells "the reason helped" apart from "more text helped".
4. **stale:** the rule plus an outdated why, which the background shows no longer holds. The rule is still active because nobody superseded it. This arm is a probe for wrongful exceptions.

**Three models from different families,** called through OpenRouter: `anthropic/claude-haiku-4.5`, `google/gemini-3.8-flash` and `openai/gpt-oss-120b`. Each uses its default temperature, with `max_tokens` set to 2,000.

**Three runs.** The option order is shuffled per task and run, and it's the same in every arm and model within a run, so the arms are compared on the same order. That's 24 × 4 × 3 × 3 = **864 calls**.

**Review.** Claude drafted the tasks. The maintainer reviewed every expected answer ([review.md](review.md)) on 4 October 2026, before the freeze.

**Dev set** ([dev.json](dev.json)). Four separate tasks, used only to pilot the harness. They're never scored or used to tune the test set.

## Metrics

- **Extend accuracy** per model and arm, with the **reason minus filler gap** and a 95% paired bootstrap interval over tasks (2,000 resamples, seed 20261004).
- **Wrongful-exception rate** on Hold, per arm.
- **Plain adherence.** This should be near 100%. If it isn't, the harness is broken.
- **The stale probe:** wrongful exceptions on Plain and Hold tasks in the stale arm, compared with bare. Reported, but not part of the verdict.
- Every result is also shown with each model left out, because Claude wrote the tasks and Claude Haiku is one of the models.

## Kill criteria (registered)

- **The claim is supported** if, in at least 2 of the 3 models, reason beats filler on Extend by **10 points or more** and the 95% interval excludes zero.
- **The claim is killed** if reason raises wrongful exceptions on Hold by **more than 5 points** against bare, pooled across models. It is also killed if, in at least 2 models, the whole interval sits below 10 points.
- **Anything else is inconclusive.** The README sentence stays only if the claim is supported.
- **Power, honestly:** 10 Extend tasks × 3 runs gives 30 trials per arm per model. A 10-point gap is three trials, so a small effect will come out inconclusive rather than supported.

The thresholds are in [whyin.py](whyin.py). [preregistration.json](preregistration.json) holds sha256 hashes of the task files and the code, and `run.py --set test` refuses to run if any of them has changed.

## Run it

```bash
cd eval/why-in
python run.py --review                         # the task set, as a review sheet
python run.py --set dev --runs 1               # pilot (needs OPENROUTER_API_KEY)
python run.py --set test                       # the scored run
python run.py --score results/<file>.jsonl     # rescore a finished run
```

Raw answers and scores are written to `results/`. The whole scored run costs well under $1.

## Provenance

Designed in a three-model review on 4 October 2026: Claude and GPT diverged, and Gemini triaged. GPT proposed the four-arm design with the filler control, and Gemini endorsed it.
