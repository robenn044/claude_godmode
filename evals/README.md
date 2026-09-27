# Evaluations

`evals.json` lists scenarios in the Agent Skills evaluation format: `skills`, `query`, `files` and `expected_behavior`. Claude Code has no built-in runner for these, so run them by hand:

```bash
# from a scratch copy of this repository
claude -p "/godmode 8 A bat and a ball cost \$1.10 ..." --output-format json
```

Then check the transcript and the run directory (`.godmode/runs/...`) against `expected_behavior`.

The `calc_repo` scenario needs the fixture to be its own git repository. Set that up with:

```bash
cd evals/fixtures/calc_repo && git init -q && git add -A && git commit -qm init
```

The engine's automated tests (`python3 -m unittest discover -s tests`) cover the mechanics without making any API calls. The evals above cover the orchestrator's behaviour. That behaviour depends on the model, so run the evals on every model you plan to use.

## Measuring whether the swarm helps

On a set of tasks with known answers, compare:
- **Single-agent accuracy**: `/godmode 1 ...`.
- **Coverage**: whether any agent in the swarm found the correct answer. Count it from the `answer_key` values in `RUN/agents/*/result.json`.
- **Selected accuracy**: whether the winner is correct.

The gap between coverage and selected accuracy is what verification, clustering and voting are meant to close. Track `usage.cost_usd` and `usage.cache_hit_ratio` in `report.json` to see what that costs.
