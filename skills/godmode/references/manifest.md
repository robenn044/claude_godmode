# manifest.json reference

The engine reads `RUN/manifest.json`. `python3 ENGINE validate --run-dir RUN` checks it and lists every problem it finds.

## Contents
- Fields
- How fields map to prompts (prompt caching)
- Example: code mode
- Example: reason mode

## Fields

| Field | Type | Default | Purpose |
|---|---|---|---|
| `task` | string | **required** | The user's prompt, verbatim. |
| `agents` | int | **required** | X, from 1 to 10000. The engine runs exactly this many agents. |
| `mode` | `"code"` \| `"reason"` | `"reason"` | `code`: each agent works in a private git worktree, and its diff is captured and verified. `reason`: each agent gets a scratch directory plus read access to `cwd`. |
| `cwd` | abs path | current dir | Project root. |
| `brief` | string | `""` | Shared briefing: the goal, definition of done, key paths and facts, the crux, constraints, and what not to do. Aim for 1,000 words or fewer. |
| `dead_ends` | [string] | `[]` | Approaches already tried or known to fail. |
| `strategies` | [{name, workflow}] | 1 generic | 4 to 24 genuinely different approaches, each written as a concrete step-by-step workflow. Agent *i* gets strategy `(i-1) mod S`. |
| `lenses` | [string] | balanced | Secondary priorities. Agent *i* gets lens `((i-1) div S) mod L`, so the S×L combinations are spread across the swarm. |
| `judge_angles` | [string] | 4 defaults | Each agent votes from one of these angles, for example correctness tracer or edge-case hunter. This gives every agent an independent way of checking. |
| `criteria` | [string] | 4 defaults | The ranked judging rubric. Put correctness first. |
| `answer_key_spec` | string | generic | How agents write the one-line `answer_key`. Equivalent answers **must** produce the same key, because clustering depends on it. |
| `solution_format` | string | generic | What the solution text must contain. |
| `max_words` | int | 1500 | Soft cap on solution length. |
| `verify_command` | string \| null | null | Code mode only. A shell command run from the project root inside each worktree. Exit 0 means pass. |
| `verify_timeout` | int (s) | 900 | Timeout for the verifier. |
| `link_paths` | [string] | `[]` | Relative paths to symlink into each worktree instead of copying, such as `node_modules`, `.venv` or `target`. |
| `worker_tools` | string \| null | all | Restricts the built-in tools workers get, for example `"Bash,Read,Edit,Write,Grep,Glob"`. |
| `worker_subagents` | bool | false | Allows workers to spawn their own subagents. This multiplies cost. |
| `worker_mcp` | bool | false | Loads the user's MCP servers in every worker. This is slower. |
| `voter_tools` | string \| null | auto | Tools the voters get, so they can check claims against the project. `null` means `"Read,Grep,Glob"` in code mode and none in reason mode. |
| `worker_effort` / `voter_effort` | effort | high / high | One of `low`, `medium`, `high`, `xhigh`, `max`. A voter below `--vote-confidence` re-checks at `xhigh`. |
| `model` / `vote_model` | string \| null | session model | Leave these null so every agent runs on the session's model. They are resolved in this order: `--model`, manifest, the model recorded for the run, then the detected session model. |
| `fallback_model` | string \| null | null | Leave this null. A fallback would put some agents on a different model. |

## How fields map to prompts (prompt caching)

- The engine renders `task`, `brief`, `dead_ends`, `criteria`, `answer_key_spec`, `solution_format` and the rules into one
  `shared_brief.md`. Every solving agent receives that file as the same appended system prompt, so after the first
  agent it is served from the prompt cache.
- Only the short `<assignment>` differs between agents: its strategy, lens and seed.
- If you edit any of these fields after a pilot, the solutions made under the old wording are solved again automatically.

## Example: code mode

```json
{
  "task": "Fix the race condition that makes tests/test_pool.py::test_concurrent_release flaky",
  "agents": 40,
  "mode": "code",
  "cwd": "/home/me/proj",
  "brief": "Goal: test_concurrent_release passes 200/200 runs without slowing the pool. Pool lives in src/pool.py (Pool.release, Pool._reap). The test fails ~1/30 runs with a double-release assertion. Crux: release() and _reap() both touch _idle without a common lock; naive global locking deadlocks with _reap's callback. Constraints: public API unchanged, no sleeps. Done = verify_command passes and the change is minimal.",
  "dead_ends": ["Wrapping release() in a global lock: deadlocks test_reap_callback", "Adding retries/sleeps in the test"],
  "strategies": [
    {"name": "Instrument then fix", "workflow": "1) Add temporary logging and run the test in a loop to capture the interleaving. 2) Identify the exact racy sequence. 3) Fix with the smallest synchronization change. 4) Remove logging, loop the test 200x."},
    {"name": "Invariant-first", "workflow": "1) Write down the invariants of _idle/_busy. 2) Find every method that can break them concurrently. 3) Restructure so each invariant is owned by one lock. 4) Verify."},
    {"name": "Lock-free redesign", "workflow": "1) Replace the shared list with a queue.Queue-based handoff. 2) Keep the public API. 3) Verify, including test_reap_callback."},
    {"name": "Contrarian", "workflow": "1) Assume the test itself is wrong. 2) Prove or refute that with a minimal reproduction. 3) Fix whichever side is actually wrong."}
  ],
  "lenses": ["Minimal diff", "Robustness under load"],
  "judge_angles": ["Correctness tracer: walk the interleavings", "Regression hunter: what else could this break?", "Spec compliance: API unchanged, no sleeps", "Simplicity auditor"],
  "criteria": ["Fixes the race for real (not masked)", "No regressions or deadlocks", "Minimal and clear change"],
  "answer_key_spec": "The root cause and fix as 'cause -> fix' in one line",
  "solution_format": "Root cause, the change, why it is correct under all interleavings, and how you verified it.",
  "verify_command": "for i in $(seq 1 30); do python -m pytest -q tests/test_pool.py || exit 1; done",
  "link_paths": [".venv"],
  "worker_effort": "xhigh"
}
```

## Example: reason mode

```json
{
  "task": "Find the minimal number of weighings to identify the fake among 40 coins (heavier or lighter unknown) and give the strategy",
  "agents": 100,
  "mode": "reason",
  "cwd": "/home/me",
  "brief": "Classic counterfeit-coin variant with an unknown direction. Done = exact minimum with a proof of optimality (information bound) AND an explicit, checkable weighing strategy.",
  "strategies": [
    {"name": "Information bound first", "workflow": "Derive the lower bound from outcomes vs. hypotheses, then construct a strategy meeting it."},
    {"name": "Constructive search", "workflow": "Write a small program in your scratch dir that searches or verifies weighing schemes; report what it proves."},
    {"name": "Generalise from small n", "workflow": "Solve n=3,4,12,13 by hand, find the pattern and known formula, then apply it to 40 and double-check."}
  ],
  "criteria": ["Correct minimum", "Proof of optimality", "Strategy is explicit and verifiable"],
  "answer_key_spec": "The minimum number of weighings as a bare integer",
  "worker_effort": "high"
}
```
