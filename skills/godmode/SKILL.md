---
name: godmode
description: Runs a swarm of 1-10000 independent Claude Code agents on one hard task. Each agent has its own context, strategy and isolated workspace. The swarm objectively verifies the solutions, merges equivalent ones and puts them to a vote by every agent, then returns the solution with the most votes. When the vote is contested, it refines adaptively. Use it when the user types /godmode X [prompt], especially for problems a single agent cannot crack.
argument-hint: <agents 1-10000 | resume> [--model M] [--effort E] [--concurrency N] [--budget USD] [--yes] <prompt>
disable-model-invocation: true
allowed-tools: Bash(python3 ${CLAUDE_SKILL_DIR}/scripts/godmode_engine.py *) Bash(python3 *godmode_engine.py*) Bash(python *godmode_engine.py*) Bash(py *godmode_engine.py*) Bash(git *) Bash(mkdir *) Read Write Edit Grep Glob
---

# GODMODE

You are the **orchestrator**, the one main agent. You do not solve the task yourself. Your job is to make
X independent agents succeed where a single attempt fails. You do that by:
- briefing them well,
- giving each one a genuinely different workflow,
- giving them an objective check,
- letting the engine run the swarm, the verification and the vote.

Then you report the most-voted solution and, if asked, apply it.

- **Arguments:** `$ARGUMENTS`
- **Engine:** `${CLAUDE_SKILL_DIR}/scripts/godmode_engine.py` (Python 3.8+, standard library only). Below it is
  called `ENGINE`. Always quote its path when you run it: `python3 "ENGINE" ...`. On Windows, or if `python3`
  is missing, use `python` or `py -3`. This also works when your shell tool is PowerShell.
- **Session effort:** `${CLAUDE_EFFORT}`.

**Same model everywhere.** Every agent and every voter must run on the model the user chose for this session. That
is the model you are running as right now. Always pass `--model <your exact model ID>`, for example
`--model claude-opus-5-5`, to `run` and `--pilot`, unless the user named a model with `--model`. The engine also
detects the session model on its own. `preflight` shows the model it detected, and the report lists every model the
calls actually used. If they differ, say so.

## Checklist

Copy this checklist and tick items off as you go:
```
- [ ] 1 Parse arguments        - [ ] 5 Validate (+ baseline)
- [ ] 2 Preflight              - [ ] 6 Pilot + cost check (X >= 50)
- [ ] 3 Recon + crux analysis  - [ ] 7 Run swarm
- [ ] 4 Write manifest         - [ ] 8 Report, apply, clean
```

## 1. Parse arguments

The format is `/godmode X [flags] prompt`, or `/godmode resume [RUN]`.
- **`resume`:** find the unfinished run. That is the given `RUN`, or else the newest directory in
  `<project>/.godmode/runs/` whose `progress.json` phase is not `done`. Re-run its `run` command with the same flags.
  Nothing is redone. Then go to step 8. Use this after the app was closed or the run was interrupted.
- **X** is the first token and must be an integer from 1 to 10000. The engine always runs exactly X agents.
- **Flags** can appear before the prompt:

| Flag | Engine option | Effect |
|---|---|---|
| `--model M` | `--model M` | Model for the solving agents. |
| `--vote-model M` | `--vote-model M` | Model for the votes. Defaults to `--model`. |
| `--effort low\|medium\|high\|xhigh\|max` | `--effort E` | Reasoning effort for the solving agents. |
| `--concurrency N` | `--concurrency N` | Maximum agents running at once. Default 8. It adapts down automatically on rate limits. |
| `--budget USD` | `--budget USD` | Hard spending stop. The run can be resumed. |
| `--max-refine N` | `--max-refine N` | Maximum refinement rounds. |
| `--vote-confidence C` | `--vote-confidence C` | A voter whose confidence is below C (default 70) re-examines its ballot at higher effort before its vote counts. |
| `--no-refine` | `--no-refine` | Never run a refinement round. |
| `--yes` | (none) | Skip the cost confirmation. |
| `--native` | (none) | Use native mode instead of the engine. |

- **Prompt:** the rest is the prompt. If the prompt is missing or X is invalid, reply with
  `Usage: /godmode X [flags] <prompt>   (1 <= X <= 10000)` and stop.

## 2. Preflight

Run `python3 "ENGINE" preflight`. If it reports `claude CLI: NOT FOUND`, or Python is missing, follow
[references/native-mode.md](references/native-mode.md) and stop following this file.

## 3. Recon and crux analysis

This is where you add the most value. Spend real effort here.

1. **Recon.** Read the relevant code and docs, and reproduce the problem if you can. Find out how success can be
   checked objectively: an existing test, a build, a script, or an acceptance check you write yourself.
2. **Crux.** In 3-6 bullets, write down why this task is hard: the key obstacle, the easy-looking traps, and the
   constraints any correct answer must satisfy.
3. **Dead ends.** List what has already failed, including anything tried earlier in this conversation. Workers
   will avoid repeating it.
4. **Mode.**
   - Use `code` if the deliverable is a change to files in the project. Each agent then gets a private git
     worktree, and its diff is captured and verified.
   - Use `reason` for answers, designs, analyses or research. Each agent then gets a scratch directory and
     read access to the project.
5. **Verifier (code mode).** Choose `verify_command`: a shell command that runs from the project root in each
   agent's worktree. It must be deterministic, non-interactive, exit 0 only if the solution is right, and run
   in minutes. Prefer the failing test plus the relevant regression tests. If no suitable test exists, write an
   acceptance script into the run directory and call it by absolute path. The verifier is what lets the swarm
   beat plain popularity: the engine filters on it before voting. For dependencies the worktree lacks
   (`node_modules`, `.venv`, build caches), add them to `link_paths`.
6. **Strategies.** Write down 8-20 distinct observations or plans for attacking the crux. Then turn the best
   of them into 4-24 **strategies**. Each strategy is a concrete workflow of 3-6 steps, and each one must
   differ in *approach*, not just wording. See [references/strategies.md](references/strategies.md) for
   archetypes and judge angles.
7. **Effort.** Set `worker_effort` to the session effort (`${CLAUDE_EFFORT}`), and never below `high`. Hard tasks
   deserve `"xhigh"` or `"max"` and a generous `--timeout`. Keep `voter_effort` at `high` or above. Voters must
   check every candidate and give evidence and a calibrated confidence. Unsure voters automatically re-examine
   at `xhigh`.

## 4. Write the manifest

1. Create `RUN = <project>/.godmode/runs/<YYYYMMDD-HHMMSS>-<slug>/`. The engine keeps `.godmode/` out of
   `git status` through `.git/info/exclude`, so do not edit `.gitignore`.
2. Write `RUN/manifest.json` with the Write tool. The schema and a full example are in
   [references/manifest.md](references/manifest.md).
   - Required: `task` (the prompt, verbatim), `agents`, `mode`, `cwd`.
   - Content: `brief`, `dead_ends`, `strategies` ({name, workflow}), `lenses`, `judge_angles`, `criteria`
     (correctness first), `answer_key_spec`, `solution_format`.
   - Code mode: `verify_command` and `link_paths`.
3. The brief is the workers' only context besides their tools. Make it complete and concise (at most about
   1,000 words):
   - the goal and the definition of done,
   - relevant paths and facts you found,
   - the crux,
   - the constraints,
   - what *not* to do.
4. Define `answer_key_spec` so that equivalent answers produce the same one-line key, for example "the final
   number only" or "the root cause as `file:function - defect`". Clustering depends on it.

## 5. Validate

Run `python3 "ENGINE" validate --run-dir "RUN" --baseline`, adding `--baseline` only in code mode with a
verifier.
- Fix every error it prints and re-run until it passes.
- The baseline runs the verifier on the unmodified snapshot. For a fix task it should fail *for the right
  reason*. If it fails with "command not found" or a missing dependency, fix `verify_command` or `link_paths`
  first.

## 6. Pilot and cost check (X >= 50)

1. Run `python3 "ENGINE" run --run-dir "RUN" --pilot 5 [flags]`. It solves agents 1-5 only and prints their
   cost, time and answer keys.
2. Read the pilot answers.
   - If agents misunderstood the task, fix the brief and re-run the pilot. Solutions from an outdated brief are
     redone automatically.
   - If all 5 already agree and pass verification, tell the user that a smaller X would likely suffice. Still
     run the X they asked for unless they change it.
3. If X >= 500, or `projected_total_cost_usd_range` exceeds $50, confirm with the user once, unless `--yes` was
   given. Show agents, projected cost, concurrency, and suggest `--budget`.

## 7. Run the swarm

Command: `python3 "ENGINE" run --run-dir "RUN" [--model M] [--effort E] [--concurrency N] [--budget B] [--no-refine]`

**How to launch it**
- **X <= 20:** run it in the foreground with the Bash timeout at 600000 ms. If the command times out, run
  the same command again; the engine resumes.
- **X > 20:** run it with `run_in_background: true`. Then check in with
  `python3 "ENGINE" status --run-dir "RUN" --wait 540`, using a Bash timeout of 600000 ms. That call returns early
  when the run finishes or stops. Between check-ins, give the user a one-line update: phase, done/total, cost, and
  any note such as usage-limit waits. Repeat until `engine_running` is false. Never use a sleep loop.
- **Big runs (X >= 200).**
  - Tell the user the run needs the app to stay open, the computer to stay awake, and the session to stay
    active. If anything interrupts it, `/godmode resume` continues where it stopped, and finished agents are
    never redone.
  - Use a concurrency the plan allows: 4-8 on Pro/Max plans, 16-64 on high-tier API keys.
  - When a plan usage limit is hit, the engine waits for the reset (up to `--max-wait-hours`, default 6)
    instead of failing agents.

**Exit codes**

| Code | Meaning | What to do |
|---|---|---|
| 0 | Done. | Continue to step 8. |
| 3 | Budget reached. | Offer to resume with a higher `--budget`. |
| 4 | Setup broken; the first calls all failed. | Read the error and fix the cause. |
| 5 | Another engine is already running this run. | Follow it with `status --wait`. |
| 130 | Interrupted. | Re-run the same command to resume. |

**What the engine does.** You do not have to do any of this yourself:
1. **Solve.** Each of the X agents solves in its own context with its own strategy. The shared brief is
   prompt-cached.
2. **Verify.** Each solution is checked with the verifier.
3. **Cluster.** Equivalent answers are merged.
4. **Qualifying round** (when there are more than 6 clusters). Every agent ranks a ballot of other agents'
   clusters.
5. **Final round.** Every agent votes on the finalists, anonymised and position-balanced. Each voter checks
   every candidate with concrete evidence, compares the top two head-to-head, and states a calibrated
   confidence. A voter below `--vote-confidence` re-examines its ballot at higher effort, and only that second,
   final vote counts. The most first-choice votes wins.
6. **Adaptive refinement.** Refinement runs if the vote is contested:
   - the winner has under 50% of the vote,
   - the margin is under 15%,
   - the winner's voters are unsure, or
   - verification failed.

   Then about 10% of the agents synthesise improved solutions from the top candidates and critiques, and all
   X agents vote again.

## 8. Report, apply, clean

1. Read `RUN/WINNER.md`, and `RUN/report.json` for details. Present:
   - The winning solution, in full.
   - A table: the winner's first-choice votes out of the total and its share, how many agents independently
     reached it, whether it was verified, and the top runners-up with their votes and answer keys.
   - The status (unanimous, clear or contested), whether refinement ran, the voters' mean confidence and
     re-checks, the strongest dissent if the margin was thin, and the agents, the model used, cost, cache hit
     rate and run directory.
   - The vote result exactly as it came out. If you believe the winner is wrong, say so *after* presenting it,
     with evidence.
2. If the prompt asked for a change to be made:
   - Run `python3 "ENGINE" apply --run-dir "RUN"`, then run the verifier or the tests in the real project and
     report the result.
   - If the patch doesn't apply because the tree changed since the snapshot, explain that and offer to re-run.
3. Code mode: run `python3 "ENGINE" clean --run-dir "RUN"` to remove leftover worktrees and the snapshot ref.
   Keep `RUN/` itself; it is the audit trail.

## Rules

- Run exactly X agents, and never more than 10000.
- Never start a swarm from inside a godmode agent; the engine refuses to.
- Only the orchestrator modifies the user's real project, and only through `apply` or edits the user asked for.
- Troubleshooting (rate limits, Windows, disk, auth): [references/troubleshooting.md](references/troubleshooting.md).
