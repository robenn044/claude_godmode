---
name: godmode
description: Spawn a swarm of 1-10000 independent Claude agents on one task, each with its own context, strategy and workflow; every agent then votes and the solution with the most votes wins. Usage - /godmode X [prompt]
argument-hint: <agents 1-10000> [--concurrency N] [--model M] [--budget USD] [--fresh-voters] [--yes] <prompt>
disable-model-invocation: true
allowed-tools: Bash(python3:*), Bash(python:*), Bash(py:*), Bash(claude --version), Bash(mkdir:*), Read, Write, Grep, Glob, Agent, SendMessage
---

# GODMODE: swarm solve and vote

You are the **orchestrator**, the one main agent. You do not solve the task yourself. You:
1. understand the task,
2. brief and organise X worker agents so each gets the right task, context and a distinct
   workflow,
3. run the swarm and the vote,
4. report the winner and, if the task asked for changes, apply them.

Raw arguments: `$ARGUMENTS`

---

## Step 0: Parse the arguments

Format: `/godmode X [options] prompt`

- `X`, the first token, is the number of agents. It must be an integer from **1 to 10000**. If it is
  missing, not a number, or out of range, stop and show:
  `Usage: /godmode X [--concurrency N] [--model M] [--budget USD] [--fresh-voters] [--yes] <prompt>  (1 <= X <= 10000)`
- Optional flags, which can appear anywhere before the prompt text:
  - `--concurrency N`: how many agents run at once (default 8, max 256). Raise it only if the
    user's rate limits allow.
  - `--model M`: the model for every agent, for example `sonnet`, `opus` or `haiku`. By default
    they use the user's default model.
  - `--budget USD`: a hard stop once spend reaches this many dollars. The run can be resumed
    afterwards.
  - `--fresh-voters`: agents vote in a fresh context instead of resuming their own session.
    This is cheaper.
  - `--native`: use in-session subagents instead of the CLI engine (see Step 3B).
  - `--yes`: skip the confirmation for large swarms.
- Everything else is the **prompt**. If the prompt is empty, ask the user for it.

## Step 1: Understand the task and gather context

Before you spawn anything, understand the task well enough to brief a team.
- If the task concerns the current project or codebase, investigate it with Read, Grep and Glob.
  Find the relevant files, conventions, constraints, and how success is verified (tests or
  build commands).
- Decide what a *complete* solution looks like. This is the definition of done.
- Decide the **solution format** voters will compare. Examples:
  - code change: "A unified diff against the current files (paths relative to the repo root),
    followed by a short rationale and how to verify it."
  - question or analysis: "A direct answer first, then the supporting reasoning."
  - writing or naming: "The final text only, then one line of rationale."
- Decide 3-6 **judging criteria**, most important first. Correctness nearly always comes first.

## Step 2: Design the division of labour

Diversity is what makes a swarm better than one agent. Design:
- **strategies**: `min(X, 4..24)` genuinely different approaches, each with a name and
  concrete instructions. Examples: a first-principles derivation, the minimal change, a robust
  or defensive version, a performance-first version, a "study prior art in the codebase and follow it"
  approach, a contrarian that challenges the obvious approach, a test-first approach, or
  "simplest thing that could work". Tailor them to the task, because generic ones are weak.
- **lenses**: 2-6 secondary priorities, such as correctness, simplicity, performance, edge
  cases, readability and user experience. Agent `i` gets strategy `i mod S` and a lens, so even
  10000 agents are spread across every strategy × lens combination and each agent also gets a
  unique variation seed.
- **brief**: a concise briefing (under ~800 words) with everything a fresh agent needs. Include
  relevant file paths, key facts you found, constraints, the definition of done, and what *not* to
  do. Workers start with zero context, so this is their only knowledge besides the tools.
- **worker_tools**: the read-only tools workers may use. The default is
  `Read,Grep,Glob,WebSearch,WebFetch`. Workers are always blocked from editing files.
  Add `Bash(git log:*)`-style read-only patterns only if they truly need them.

## Step 3: Run the swarm

### 3A. Engine mode (default and recommended for any X)

The engine runs every agent as a separate headless `claude -p` process with its own session, so
each has its own context window. It is resumable and keeps your own context small.

1. **Locate the engine.** It is `scripts/godmode_engine.py` inside this skill's base directory (shown
   above when this skill loaded). If you cannot see it, find it:
   `python3 -c "import glob,os;print((glob.glob(os.path.expanduser('~/.claude/skills/godmode/scripts/godmode_engine.py'))+glob.glob('.claude/skills/godmode/scripts/godmode_engine.py')+glob.glob(os.path.expanduser('~/.claude/plugins/**/godmode_engine.py'),recursive=True))[0])"`
   Use `python` or `py` instead of `python3` on Windows if needed.
2. **Check the CLI:** `python3 <engine> check`. If `claude` is not found, use Step 3B.
3. **Create a run directory:** `.godmode/runs/<YYYYMMDD-HHMMSS>-<short-slug>/` in the current
   project. Also make sure `.godmode/` is git-ignored. If `.gitignore` exists and lacks it,
   append `.godmode/`.
4. **Write `manifest.json`** into the run directory with the Write tool:
   ```json
   {
     "task": "<the user's prompt, verbatim>",
     "agents": X,
     "cwd": "<absolute path of the project the agents should inspect>",
     "brief": "<your briefing from Step 2>",
     "strategies": [{"name": "Minimal change", "instructions": "..."}, "..."],
     "lenses": ["Correctness above all", "Simplicity", "..."],
     "criteria": ["Correctness", "Completeness", "..."],
     "solution_format": "<from Step 1>",
     "max_words": 1500,
     "worker_tools": "Read,Grep,Glob,WebSearch,WebFetch"
   }
   ```
5. **Confirm large swarms.** If X >= 500 and `--yes` was not given, run the engine with
   `--dry-run` first. Then tell the user the plan: X agents, about 3·X Claude calls (solve, qualifying
   vote, final vote), the concurrency, and that cost scales linearly. Suggest `--budget`. Ask once
   whether to proceed.
6. **Run it:**
   ```
   python3 <engine> run --run-dir <run-dir> --concurrency <N> [--model M] [--max-budget-usd B] [--fresh-voters]
   ```
   - If X <= 20, run it in the foreground with the Bash timeout set to 600000 ms. If it times out,
     re-run the same command, because the engine resumes where it stopped.
   - If X > 20, run it with `run_in_background: true`. You are notified when it exits.
     Meanwhile, if the user asks, report progress with
     `python3 <engine> status --run-dir <run-dir>`. Do not poll in a sleep loop.
   - Exit code 3 means the budget was reached. Tell the user and offer to resume with a higher budget.
7. When it finishes, read `<run-dir>/WINNER.md`. Use `report.json` for details.

### 3B. Native mode (fallback when the `claude` CLI is unavailable, or `--native`)

Use the Agent tool. Only do this for X <= 50, because every result lands in your own context. For larger X,
tell the user to install the Claude Code CLI, which engine mode needs.
1. Build each worker prompt exactly like the engine's worker template: the TASK, BRIEFING, its
   strategy + lens + agent number, the read-only rule, the criteria and the mandatory
   `<summary>` / `<solution>` output.
2. Spawn workers with the Agent tool (`subagent_type: general-purpose`), up to 10 per message in
   parallel, until all X are done. Record each agent's id and its `<solution>`.
3. Merge identical solutions (ignoring whitespace and case) into one candidate.
4. **Vote.** Every agent votes. Continue each worker with SendMessage so it votes with its own
   context. If SendMessage is unavailable, spawn a fresh voter per agent. The ballot holds all
   candidates, anonymised as A, B, C… in a different random order for each voter, with the
   instruction to reply `<vote>LETTER</vote><reason>…</reason>`. If there are more than 10
   candidates, run a qualifying round first. In it, each agent votes on a ballot of 8 *other*
   candidates, and the 10 with the highest (votes + authors) / (appearances + authors) go to
   the final.
5. Count the final votes yourself. The winner is the candidate with the most votes. Break ties by
   qualifying score, then by number of authors.

## Step 4: Report

Present the following to the user:
- **The winning solution**, in full.
- The vote: winner's votes out of the total and its share, how many agents wrote it
  independently, and the top 3-5 runners-up with their vote counts and one-line summaries. Show
  these as a small table.
- Agents, unique candidates, failures, Claude calls, total cost and the run directory.
- A note if the winning margin was thin or there was a tie, with the runner-up's key difference.

If the user's prompt asked for changes to be *made* (fix, implement, refactor and so on), apply the
winning solution to the working tree yourself, then run the project's checks or tests and report
the result. Otherwise do not modify files.

## Rules
- Never exceed 10000 agents, and never start a swarm from inside a godmode worker. The engine
  enforces both.
- Workers are read-only. Only you, the orchestrator, apply the winner.
- Report the vote result as it is. Do not substitute your own preferred answer. If you believe the
  winner is wrong, say so after presenting it.
