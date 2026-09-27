# claude_godmode: `/godmode` for Claude Code

```
/godmode X [prompt]          X = 1 … 10000 agents
```

`/godmode` puts **X independent Claude agents** on one task. Each agent has its own context, its own strategy and its own private workspace. Their solutions are **verified** objectively and **merged** where equivalent. Then **every agent votes**, and the solution with the **most votes** wins. When the vote is contested, a refinement round runs automatically before a second vote.

It is built for problems a single agent fails on, and to be economical at scale: the engine uses prompt caching, adapts to rate limits and resumes interrupted runs.

```
/godmode 5 Why does our login redirect loop only in Safari? Find the root cause.
/godmode 40 --effort xhigh Fix the flaky test tests/test_pool.py::test_concurrent_release
/godmode 1000 --concurrency 32 --budget 300 Find the minimal weighings for 40 coins with unknown heavier/lighter fake
```

---

## How it works

```
 /godmode X prompt
        │
        ▼
 ORCHESTRATOR (your Claude Code session)
   recon → crux analysis → dead ends → 4-24 distinct strategy workflows,
   judge angles, answer-key spec, verifier → manifest.json → validate → pilot
        │
        ▼
 ENGINE  (skills/godmode/scripts/godmode_engine.py, Python stdlib)
   1 SOLVE     X × `claude -p`, each with its own context, strategy × lens, and a private
               git worktree (code) or scratch dir (reasoning). Shared brief = cached prefix.
   2 VERIFY    verify_command runs in each worktree → PASSED/FAILED + output
   3 CLUSTER   exact + semantic merge of equivalent answers (supporters pool together)
   4 QUALIFY   (only if > 6 clusters) every agent ranks a balanced ballot of 6 OTHER clusters
   5 FINAL     every agent votes on the ≤ 6 finalists → MOST FIRST-CHOICE VOTES WINS
   6 REFINE    only if contested: ~10% of agents synthesise improved solutions from the
               top candidates + voter critiques → re-cluster → every agent votes again
        │
        ▼
 WINNER.md + report.json → orchestrator presents the result; `apply` patches your project
```

### Design choices and the research behind them

| Choice | Why |
|---|---|
| **Verify before voting.** Code solutions are run against a test, and passing ones are preferred. | Plain majority voting plateaus, and can even get *worse* as calls increase on hard tasks. Filtering on execution closes much of the gap between "some agent found it" and "we picked it". Sources: Chen et al. 2024, *Are More LLM Calls All You Need?*; Brown et al. 2024, *Large Language Monkeys*; AlphaCode; CodeT. |
| **Semantic clustering** of answer keys. | Equivalent answers phrased differently would otherwise split the vote. This is the idea behind Universal Self-Consistency. |
| **Strategy diversity, not temperature.** The orchestrator writes distinct workflows. | Diverse plans beat repeated sampling. Sources: PlanSearch, DIV-SE. |
| **Every agent votes from a fresh context, using its own judging angle.** | A voter doesn't know which candidate is its own, which removes self-preference bias (Panickssery et al. 2024). The different angles (correctness tracer, edge-case hunter and others) make the votes more independent. |
| **Anonymised, rotated ballots.** Each finalist appears in every position equally often, and supporter counts are hidden. | Controls position bias and herding (Zheng et al. 2023). |
| **Rubric first, then ranking.** | Checking each candidate against the criteria before comparing them resists distractors. |
| **Adaptive refinement only when contested:** the winner has under 50% of votes, leads by under 15%, or fails verification. | Recursive Self-Aggregation and Self-MoA get gains from synthesis rounds. CATTS-style gating spends that compute only where it pays off. Open debate is avoided because it mostly adds nothing beyond voting (*Debate or Vote*, 2025). |
| **Shared bytes first, per-agent bytes last.** A primer call runs first and the rest follow once it has started responding. Per-process flags are chosen for cross-process cache hits. | Anthropic's prompt caching serves a cache entry only after the first response begins. In a measured real run, each agent after the first cost about a third as much. |
| An orchestrator–worker setup with explicit briefs: objective, output format, tools and boundaries. | Anthropic, *How we built our multi-agent research system*. |

---

## Installation (step by step)

### 1. Requirements

| Requirement | Check | Install |
|---|---|---|
| Claude Code (CLI, or Claude Desktop with Claude Code) | `claude --version` | `npm install -g @anthropic-ai/claude-code`, or <https://claude.com/claude-code> |
| Signed in | `claude` → `/login` | |
| Python 3.8+ | `python3 --version` (Windows: `python --version`) | <https://python.org> |
| git | `git --version` | Needed for code tasks: isolated worktrees and patches |

**Claude Desktop.** The Claude Code tab in Claude Desktop reads the same `~/.claude/skills` folder, so the global install below also covers Desktop. You don't need a separate `claude` on your PATH: the engine uses the Claude binary that Desktop is already running, found through `CLAUDE_CODE_EXECPATH`.

### 2A. Global install (every project, CLI and Desktop). Recommended.

**macOS / Linux / WSL**
```bash
git clone https://github.com/robenn044/claude_godmode.git
cd claude_godmode
./install.sh                      # copies skills/godmode → ~/.claude/skills/godmode
```
Without cloning: `curl -fsSL https://raw.githubusercontent.com/robenn044/claude_godmode/main/install.sh | bash`

**Windows (PowerShell)**
```powershell
git clone https://github.com/robenn044/claude_godmode.git
cd claude_godmode
powershell -ExecutionPolicy Bypass -File .\install.ps1   # → %USERPROFILE%\.claude\skills\godmode
```
Without cloning: `irm https://raw.githubusercontent.com/robenn044/claude_godmode/main/install.ps1 | iex`

**Manual install:** copy `skills/godmode/` to `~/.claude/skills/godmode/`.

### 2B. One project only (shared with your team through git)

```bash
/path/to/claude_godmode/install.sh --project     # → ./.claude/skills/godmode
git add .claude/skills/godmode && git commit -m "Add /godmode skill"
```

### 2C. As a plugin

Run these in Claude Code:
```
/plugin marketplace add robenn044/claude_godmode
/plugin install godmode@claude-godmode
```
Invoke it as `/godmode`, or as `/godmode:godmode` if the name clashes with another command.

> **Before this is merged into `main`:** install from the branch. After cloning, run `git checkout <branch>` before `./install.sh`. For the one-liners, fetch the branch's `install.sh` and pipe it to `GODMODE_REF=<branch> bash`, or pass `-Ref <branch>` to `install.ps1`.

### 3. Verify the install

1. Restart Claude Code, or open a new Desktop session.
2. Type `/` and check that `godmode` appears in the list.
3. Run:
   ```bash
   python3 ~/.claude/skills/godmode/scripts/godmode_engine.py preflight
   ```
   It should print your Claude CLI path, version and features.
4. Try a small run: `/godmode 3 What is 17 * 23?`

### Update or uninstall

- Update: re-run `./install.sh`.
- Uninstall: `./install.sh --uninstall` (add `--project` for a per-project install), or `/plugin uninstall godmode@claude-godmode`.

---

## Usage

| Flag | Meaning |
|---|---|
| `X` | Number of agents, **1 to 10000**. Exactly X agents solve and all X vote. |
| `--model M` / `--vote-model M` | Model for solving and for voting, e.g. `opus`, `sonnet`, `haiku` or a full ID. |
| `--effort E` | Worker effort: `low`, `medium`, `high`, `xhigh` or `max`. The orchestrator chooses one if you don't. |
| `--concurrency N` | Maximum agents running at once. Default 8, maximum 256. It halves automatically on rate limits and recovers slowly. |
| `--budget USD` | Hard spending stop. Re-run the same command later to resume. |
| `--max-refine N` / `--no-refine` | Controls the adaptive refinement rounds. Default is at most 1. |
| `--yes` | Skip the confirmation. Without it, you are asked to confirm when X ≥ 500 or the projected cost is over $50. |
| `--native` | Run without the engine, using in-session subagents. Limited to 50 agents. |

**What you'll see in the terminal.**
1. The orchestrator explores the task and writes the plan.
2. For X ≥ 50, it runs a 5-agent pilot with a projected cost.
3. It runs the swarm. Large runs go in the background, and you can ask for progress.
4. It presents the winning solution, the vote table, the contest status, the verification result and the cost. For change requests, it applies the winning patch and runs your tests.

### Cost and speed

- **Calls.** Calls ≈ X (solve) + X (final vote) + up to X (qualifying round, only when there are more than 6 distinct answers). A refinement round, only when contested, adds about 0.1·X solves plus X votes. Unanimous runs skip voting entirely.
- **What makes votes cheap.** Votes are cheap, tool-less calls with a short system prompt. The ballot is shared by ~X/6 voters, so it is cached.
- **Measured.** Real runs with 4 agents cost $0.50 to $1.00, with a 54–90% cache hit rate.
- **Estimating.** Solve cost dominates and scales linearly. Use the pilot's projection, set `--budget`, and choose a cheaper `--model` or `--vote-model` for large X.

### Run directory: `.godmode/runs/<timestamp>-<slug>/`

This directory is hidden from `git status` through `.git/info/exclude`.

| Path | Contents |
|---|---|
| `manifest.json` | The orchestrator's plan: brief, strategies, lenses, judge angles, criteria, verifier. |
| `shared_brief.md` | The identical, cached system-prompt addition every solver receives. |
| `agents/agent-NNNNN/` | Each agent's prompt, `result.json` (answer key, solution, confidence, verification) and `patch.diff`. |
| `clusters/rN.json` | Clusters for each round. |
| `votes/qN/*.json`, `votes/fN/*.json` | Every ballot, with ranking, per-candidate verdicts and reason. |
| `refine/rN/…` | Solutions from refinement round N. |
| `report.json`, `WINNER.md` | The result. |
| `engine.log`, `progress.json` | Audit trail and progress. |

### Using the engine directly

```bash
E=~/.claude/skills/godmode/scripts/godmode_engine.py
python3 $E validate --run-dir RUN --baseline      # check the manifest; test the verifier on the snapshot
python3 $E run      --run-dir RUN --pilot 5       # solve 5 agents only, print projected cost
python3 $E run      --run-dir RUN --concurrency 16 --budget 50
python3 $E status   --run-dir RUN
python3 $E apply    --run-dir RUN                 # apply the winning patch to the project
python3 $E clean    --run-dir RUN                 # remove worktrees and the snapshot ref
```
The manifest format is described in [`skills/godmode/references/manifest.md`](skills/godmode/references/manifest.md). Run `run --help` to see every option.

---

## Safety model

- Workers get full autonomy (`acceptEdits` plus an allowlist of every built-in tool), but only inside their **own** worktree. The worktree is built from a snapshot of your current tree, including uncommitted and untracked files, using a temporary git index. Your real index, HEAD and working tree are never touched.
- Only the orchestrator's `apply` modifies your project.
- Workers can't spawn subagents or start another swarm, and MCP servers are off unless the manifest enables them.

## Development

```bash
python3 -m unittest discover -s tests -v     # 30+ tests, no API calls (tests/fake_claude.py)
claude plugin validate .
```
CI runs the suite on Linux, macOS and Windows. [`evals/`](evals/) contains orchestrator scenarios in the Agent Skills evaluation format.

## Troubleshooting

See [`skills/godmode/references/troubleshooting.md`](skills/godmode/references/troubleshooting.md).

## License

MIT
