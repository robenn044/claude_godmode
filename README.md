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
| **Every agent votes from a fresh context, using its own judging angle, and has to be sure.** For each candidate it states the core claim, the concrete evidence and a verdict, then makes a head-to-head comparison and gives a calibrated confidence. Unsure voters re-examine the ballot at higher effort. | A voter doesn't know which candidate is its own, which removes self-preference bias (Panickssery et al. 2024). The different angles (correctness tracer, edge-case hunter and others) make the votes more independent. |
| **Anonymised, rotated ballots.** Each finalist appears in every position equally often, and supporter counts are hidden. | Controls position bias and herding (Zheng et al. 2023). |
| **Rubric first, then ranking.** | Checking each candidate against the criteria before comparing them resists distractors. |
| **Adaptive refinement only when contested:** the winner has under 50% of votes, leads by under 15%, or fails verification. | Recursive Self-Aggregation and Self-MoA get gains from synthesis rounds. CATTS-style gating spends that compute only where it pays off. Open debate is avoided because it mostly adds nothing beyond voting (*Debate or Vote*, 2025). |
| **Shared bytes first, per-agent bytes last.** A primer call runs first and the rest follow once it has started responding. Per-process flags are chosen for cross-process cache hits. | Anthropic's prompt caching serves a cache entry only after the first response begins. In a measured real run, each agent after the first cost about a third as much. |
| An orchestrator–worker setup with explicit briefs: objective, output format, tools and boundaries. | Anthropic, *How we built our multi-agent research system*. |

---

## Installation

- **Claude Desktop** users: follow [Install for Claude Desktop](#install-for-claude-desktop-step-by-step) below.
- **Terminal (`claude` CLI)** users: see [Install for the terminal](#install-for-the-terminal-claude-cli).
- **Both:** one install covers both. They share the same `~/.claude/skills` folder.

### Install for Claude Desktop (step by step)

`/godmode` runs in the **Code** tab of the Claude Desktop app, in a **Local** session on your computer. It doesn't
work in Chat or Cowork, and it isn't meant for Cloud sessions, which don't read `~/.claude/skills`.

#### What you need

| | macOS | Windows |
|---|---|---|
| Claude Desktop app with a **Pro, Max, Team or Enterprise** plan (the Code tab needs one) | [claude.com/download](https://claude.com/download) | [claude.com/download](https://claude.com/download) (x64 or ARM64) |
| **Python 3.8 or newer** | Open **Terminal** and run `python3 --version`. If macOS offers to install the *Command Line Developer Tools*, click **Install**; that gives you Python 3 and git. You can also install from [python.org](https://www.python.org/downloads/). | Install from [python.org](https://www.python.org/downloads/) or the Python install manager. In the installer, tick **"Add python.exe to PATH"**. Check it by opening **PowerShell** and running `python --version`. |
| **Git** (code tasks use git worktrees) | Included with the Command Line Developer Tools (`git --version`) | Install [Git for Windows](https://git-scm.com/download/win). Claude Code also uses its Git Bash as its shell. |

You do **not** need Node.js or a separate `claude` install. The Desktop app includes Claude Code, and `/godmode`
uses the copy that is already running.

#### Step 1: Download /godmode

Either:
- **ZIP:** open <https://github.com/robenn044/claude_godmode>, click **Code → Download ZIP**, and unzip it, for
  example into your Downloads folder, or
- **git:** `git clone https://github.com/robenn044/claude_godmode.git`

#### Step 2: Run the installer

**macOS**
1. Open the unzipped `claude_godmode` folder in Finder.
2. **Right-click `install-mac.command` → Open → Open.** Right-clicking is needed the first time because the file
   was downloaded from the internet.
3. A Terminal window opens, installs the skill to `~/.claude/skills/godmode`, and prints a check. Press Enter to close it.

   Terminal alternative: `bash ~/Downloads/claude_godmode-main/install.sh`

**Windows**
1. Open the unzipped `claude_godmode` folder in File Explorer.
2. **Double-click `install-windows.bat`.** If SmartScreen warns you, click **More info → Run anyway**.
3. The skill is installed to `%USERPROFILE%\.claude\skills\godmode` and a check is printed. Press any key to close.

   PowerShell alternative: `powershell -ExecutionPolicy Bypass -File "$HOME\Downloads\claude_godmode-main\install.ps1"`

The check at the end prints lines like these:
```
godmode engine 2.1.0 | python 3.12.4 | Darwin
claude CLI: ... (2.x.x (Claude Code))        <- may say NOT FOUND when run outside Claude; that is fine
```

#### Step 3: Restart Claude Desktop

**Fully quit** the app so it reloads skills and your PATH, then reopen it:
- **macOS:** Cmd+Q.
- **Windows:** File → Exit, or right-click the tray icon → Quit.

#### Step 4: Open a Local Code session in your project

1. Click the **Code** tab at the top.
2. In the environment selector, choose **Local**.
3. Click **Select folder** and pick your project folder. A git repository is best for code tasks.
4. Pick the **model** from the dropdown next to the send button. **Every `/godmode` agent and voter will use this
   same model.**
5. Optionally, open the effort menu (Cmd+Shift+E on macOS, Ctrl+Shift+E on Windows) and choose `high`, `xhigh` or
   `max` for hard problems. The agents inherit it.

#### Step 5: Check that /godmode is available

1. Type `/` in the prompt box, or click **+ → Slash commands**. You should see **godmode**. If it's missing, start
   a new session with **+ New session**, or type `/reload-skills`.
2. Run a tiny test:
   ```
   /godmode 3 What is 17 * 23?
   ```
3. Claude asks permission before it runs the engine's `python3 …/godmode_engine.py` commands. Approve them. To
   avoid the prompts on big runs, pick the **Accept edits** or **Auto** permission mode next to the send button,
   or allow the command permanently in `~/.claude/settings.json`:
   ```json
   { "permissions": { "allow": ["Bash(python3 *godmode_engine.py*)", "Bash(python *godmode_engine.py*)"] } }
   ```
4. You should get a report saying 3 agents voted, with the answer **391**, the model used and the cost.

#### Step 6: Use it

```
/godmode 20 Fix the flaky test in tests/test_pool.py
/godmode 200 --budget 50 Find the root cause of the memory leak in the ingest worker
/godmode resume          <- continues an interrupted run (for example after closing the app)
```

For runs of 50 or more agents, `/godmode` first runs a 5-agent pilot and shows the projected cost. Keep the app open
and the computer awake during big runs. If a run is interrupted, `/godmode resume` continues where it stopped.

#### Update or uninstall (Desktop)

- **Update:** download the new ZIP and run the installer again, then start a new Code session.
- **Uninstall:** delete the `godmode` folder:
  - macOS: `~/.claude/skills/godmode`
  - Windows: `%USERPROFILE%\.claude\skills\godmode`

  Or run `install.sh --uninstall` / `install.ps1 -Uninstall`.

### Install for the terminal (`claude` CLI)

1. Install Claude Code (`npm install -g @anthropic-ai/claude-code`, or <https://claude.com/claude-code>) and sign
   in (`claude` → `/login`). You also need Python 3.8+ and git.
2. Install the skill globally, to `~/.claude/skills/godmode`, for every project:
   ```bash
   git clone https://github.com/robenn044/claude_godmode.git && cd claude_godmode && ./install.sh
   # or, without cloning:
   curl -fsSL https://raw.githubusercontent.com/robenn044/claude_godmode/main/install.sh | bash
   ```
   On Windows PowerShell, use `.\install.ps1`, or
   `irm https://raw.githubusercontent.com/robenn044/claude_godmode/main/install.ps1 | iex`.
3. Start `claude` in your project and type `/godmode 3 What is 17 * 23?`.

**Other install options**
- **Just one project, shared with your team through git:**
  ```bash
  /path/to/claude_godmode/install.sh --project && git add .claude/skills/godmode
  ```
- **As a plugin:** run `/plugin marketplace add robenn044/claude_godmode`, then
  `/plugin install godmode@claude-godmode`. In Desktop, use **+ → Plugins → Add plugin**. It is invoked as
  `/godmode`, or as `/godmode:godmode` if another command uses the same name.
- **Check the install at any time:**
  ```bash
  python3 ~/.claude/skills/godmode/scripts/godmode_engine.py preflight
  ```

## Usage

| Flag | Meaning |
|---|---|
| `X` | Number of agents, **1 to 10000**. Exactly X agents solve and all X vote. |
| `--model M` / `--vote-model M` | Override the model. **By default every agent and every voter uses the model selected for your session**, i.e. the model dropdown in Claude Desktop or `/model` in the CLI. The report lists the models the calls actually used. |
| `--effort E` | Worker effort: `low`, `medium`, `high`, `xhigh` or `max`. The orchestrator chooses one if you don't. |
| `--concurrency N` | Maximum agents running at once. Default 8, maximum 256. It halves automatically on rate limits and recovers slowly. |
| `--budget USD` | Hard spending stop. Re-run the same command later to resume. |
| `--max-refine N` / `--no-refine` | Controls the adaptive refinement rounds. Default is at most 1. |
| `--vote-confidence C` | Voters must state a calibrated confidence. Below C (default 70), a voter re-examines its ballot at `xhigh` effort, and only that second vote counts. |
| `resume` | `/godmode resume` continues the newest unfinished run. Nothing is redone. |
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
python3 -m unittest discover -s tests -v     # 40 tests, no API calls (tests/fake_claude.py)
claude plugin validate .
```
CI runs the suite on Linux, macOS and Windows. [`evals/`](evals/) contains orchestrator scenarios in the Agent Skills evaluation format.

## Troubleshooting

See [`skills/godmode/references/troubleshooting.md`](skills/godmode/references/troubleshooting.md).

## License

MIT
