# claude_godmode: `/godmode` for Claude Code

`/godmode` runs **1 to 10,000 independent Claude agents** on one task. Each agent has its own
context, strategy and workflow. When they finish, **every agent votes**, and you get the solution
with the **most votes**.

```
/godmode X [prompt]
```

```
/godmode 5 What's the cleanest way to add rate limiting to our Express API?
/godmode 100 Find the root cause of the flaky test in tests/api/session.test.ts and fix it
/godmode 1000 --concurrency 32 --budget 200 Write the best possible one-paragraph pitch for this repo
```

---

## How it works

```
                        ┌─────────────────────────────┐
  /godmode X prompt ──▶ │  ORCHESTRATOR (main agent)  │  reads the codebase, writes the briefing,
                        │  your Claude Code session   │  designs strategies × lenses, assigns work
                        └──────────────┬──────────────┘
                                       │ manifest.json
                                       ▼
                        ┌─────────────────────────────┐
                        │   godmode_engine.py         │  runs `claude -p` processes in parallel
                        └──────────────┬──────────────┘  (--concurrency), resumable, with a budget
          ┌────────────┬───────────────┼───────────────┬────────────┐
          ▼            ▼               ▼               ▼            ▼
       agent 1      agent 2   ...   agent i   ...   agent X-1    agent X     1. SOLVE (read-only,
       strat A      strat B         strat i%S       ...          ...           own session each)
          └────────────┴───────────────┼───────────────┴────────────┘
                                       ▼
                    2. DEDUPE: identical answers are merged (they count as supporters)
                                       ▼
            3. QUALIFYING VOTE (only if >10 unique candidates): every agent resumes
               its OWN session and votes on a shuffled, anonymised ballot of 8 OTHER
               agents' solutions. The top 10 by vote share become finalists.
                                       ▼
            4. FINAL VOTE: every agent votes on the finalists → MOST VOTES WINS
                                       ▼
                        WINNER.md + report.json → the orchestrator presents the winner
                        (and applies it, if you asked for a change to be made)
```

- **One main agent organises everything.** The orchestrator is your Claude Code session running the
  skill. It investigates the task, writes a shared briefing, invents 4 to 24 task-specific strategies
  plus 2 to 6 lenses, and hands each agent its own combination and a unique variation seed. This
  spreads even 10,000 agents across every approach.
- **Each agent has its own context.** Every agent is a separate `claude -p` process with its own
  session ID. At voting time, each agent resumes *its own* session, so it votes knowing what it
  tried itself. Use `--fresh-voters` for cheaper, context-free voting.
- **All agents vote.** Every agent votes in the final. Voting scales to 10,000 because the qualifying
  round gives each agent a small, balanced ballot, and each candidate appears about the same number
  of times.
- **Workers are read-only.** They cannot edit files. Only the orchestrator applies the winner.
- **Runs are resumable.** Everything is saved in `.godmode/runs/<timestamp>-<slug>/`. If a run is
  interrupted, the same command resumes it.

---

## Installation (step by step)

### Requirements

| Requirement | Check | Install |
|---|---|---|
| Claude Code CLI | `claude --version` | `npm install -g @anthropic-ai/claude-code`, or the native installer from <https://claude.com/claude-code> |
| Logged in to Claude Code | run `claude` once and sign in | `claude` then `/login` |
| Python 3.8+ | `python3 --version` (Windows: `python --version`) | <https://python.org> or your package manager |
| git (optional) | `git --version` | used by the installer; it falls back to `curl` |

### Option A: global install, available in every project (recommended)

This installs the skill to `~/.claude/skills/godmode/`, so `/godmode` works in every project and every
Claude Code session: terminal, VS Code, JetBrains and the desktop app.

**macOS / Linux / WSL**
1. Open a terminal.
2. Run:
   ```bash
   git clone https://github.com/robenn044/claude_godmode.git
   cd claude_godmode
   ./install.sh
   ```
   Or, without cloning:
   ```bash
   curl -fsSL https://raw.githubusercontent.com/robenn044/claude_godmode/main/install.sh | bash
   ```
3. The installer copies the skill to `~/.claude/skills/godmode/` and checks for Python and the `claude` CLI.
4. **Restart Claude Code.** Exit any running session and start `claude` again.
5. Type `/godmode` to check that it appears in the slash-command menu.

**Windows (PowerShell)**
1. Open PowerShell.
2. Run:
   ```powershell
   git clone https://github.com/robenn044/claude_godmode.git
   cd claude_godmode
   powershell -ExecutionPolicy Bypass -File .\install.ps1
   ```
   Or, without cloning:
   ```powershell
   irm https://raw.githubusercontent.com/robenn044/claude_godmode/main/install.ps1 | iex
   ```
3. The skill is installed to `%USERPROFILE%\.claude\skills\godmode\`.
4. Restart Claude Code and type `/godmode`.

**Manual install (any OS)**
1. Copy the `skills/godmode` folder of this repo to `~/.claude/skills/godmode`, so that
   `~/.claude/skills/godmode/SKILL.md` exists.
2. Restart Claude Code.

### Option B: one project only (shared with your team through git)

From the root of the project:
```bash
/path/to/claude_godmode/install.sh --project     # → ./.claude/skills/godmode
git add .claude/skills/godmode && git commit -m "Add /godmode skill"
```
Anyone who opens the project in Claude Code gets `/godmode`.

### Option C: Claude Code plugin

Inside Claude Code, run:
```
/plugin marketplace add robenn044/claude_godmode
/plugin install godmode@claude-godmode
```
Restart Claude Code. Run it as `/godmode ...`, or as `/godmode:godmode ...` if another command
already uses the name.

> **Installing from a branch before it is merged into `main`:** for the one-liners, add
> `GODMODE_REF=<branch>` (bash), for example
> `curl -fsSL .../<branch>/install.sh | GODMODE_REF=<branch> bash`, or pass `-Ref <branch>` to
> `install.ps1`.

### Verify

```bash
python3 ~/.claude/skills/godmode/scripts/godmode_engine.py check
# claude CLI: /usr/local/bin/claude (2.x.x (Claude Code))
```
Then, in Claude Code:
```
/godmode 3 What is the capital of Australia?
```
The 3 agents should agree unanimously on Canberra.

### Update / uninstall

```bash
./install.sh              # re-run to update (overwrites the old copy)
./install.sh --uninstall  # remove the global install   (--project --uninstall for per-project)
```
To remove the plugin, run `/plugin uninstall godmode@claude-godmode`.

---

## Usage

```
/godmode X [options] <prompt>
```

| Option | Default | Meaning |
|---|---|---|
| `X` | required | number of agents, **1 to 10,000** |
| `--concurrency N` | 8 | agents running at the same time (max 256); raise it only if your rate limits allow |
| `--model M` | your default | model for every agent, for example `sonnet`, `opus` or `haiku` |
| `--budget USD` | none | hard spending stop; resume later with a higher budget |
| `--fresh-voters` | off | vote in fresh contexts instead of each agent's own session (cheaper) |
| `--native` | off | use in-session subagents instead of `claude -p` (max 50 agents) |
| `--yes` | off | skip the confirmation that is asked for X ≥ 500 |

If your prompt asks for a change (fix, implement, refactor), the orchestrator **applies the winning
solution** and runs your checks. Otherwise it only reports the winner.

### Cost and time

A run makes about **3 × X Claude calls**: X to solve, X in the qualifying vote, X in the final.
Runs with 10 or fewer unique answers skip the qualifying vote, and unanimous runs skip voting
entirely. Cost grows linearly with X:

- 10 agents is a normal "careful" run.
- 100 agents is a meaningful spend.
- 1,000 to 10,000 agents can cost hundreds to thousands of dollars and take hours.

For large runs, use `--budget`, a cheaper `--model`, and `--fresh-voters`. You can check progress at any time:
```bash
python3 ~/.claude/skills/godmode/scripts/godmode_engine.py status --run-dir .godmode/runs/<run>
```

### What a run leaves behind

```
.godmode/runs/20260927-152625-dotfiles-name/
├── manifest.json         # the orchestrator's plan: brief, strategies, lenses, criteria
├── agents/agent-00001/   # prompt.md, raw_solve.md, solution.md, meta.json (session, strategy, votes)
├── candidates.json       # unique solutions and who wrote them
├── votes/round1/*.json   # qualifying ballots and votes (with reasons)
├── votes/final/*.json    # final ballots and votes (with reasons)
├── report.json           # full tally, cost, failures
└── WINNER.md             # the winning solution and a vote table
```
`.godmode/` is added to `.gitignore` automatically.

---

## Using the engine directly (without the skill)

```bash
mkdir -p .godmode/runs/demo
cat > .godmode/runs/demo/manifest.json <<'JSON'
{
  "task": "Suggest a name for a dotfiles sync CLI",
  "agents": 20,
  "brief": "Short, typeable, no clashes with existing tools.",
  "strategies": [{"name": "Wordplay", "instructions": "Pun on home/dot/rc"},
                 {"name": "Metaphor", "instructions": "Real word evoking travel/home"}],
  "lenses": ["Memorability", "Uniqueness"],
  "criteria": ["Memorable", "Easy to type", "Evokes the purpose"],
  "solution_format": "Name on line 1, tagline on line 2."
}
JSON
python3 skills/godmode/scripts/godmode_engine.py run --run-dir .godmode/runs/demo --concurrency 8
```
Run `python3 skills/godmode/scripts/godmode_engine.py run --help` to see every option (`--ballot-size`,
`--finalists`, `--timeout`, `--retries`, `--max-turns`, `--dry-run`, and more).

## Tests

The tests use a fake `claude` binary, so they make no API calls:
```bash
python3 -m unittest discover -s tests -v
```

## Troubleshooting

- **`/godmode` doesn't appear.** Restart Claude Code, then check that
  `~/.claude/skills/godmode/SKILL.md` exists (or `.claude/skills/godmode/SKILL.md` in the project).
- **"claude CLI: NOT FOUND".** Put `claude` on your `PATH`, or set `GODMODE_CLAUDE_BIN=/path/to/claude`.
  Without it, the skill falls back to in-session subagents, which are limited to 50 agents.
- **Rate limit errors.** Lower `--concurrency`. Failed calls are retried with backoff, and re-running
  the command resumes the swarm.
- **Budget reached (exit code 3).** Re-run with a higher `--budget`. Finished work is kept.
- **Disk usage.** Each agent keeps its session under `~/.claude/projects/` so it can vote with its own
  context. Use `--fresh-voters` to avoid resuming sessions. The engine flag
  `--no-session-persistence` also stops sessions from being saved at all.

## License

MIT
