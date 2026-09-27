# Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Preflight: `claude CLI: NOT FOUND` | The CLI isn't on PATH. Claude Desktop normally sets `CLAUDE_CODE_EXECPATH`. | Set `GODMODE_CLAUDE_BIN=/path/to/claude`, pass `--claude-bin`, or install the CLI (`npm i -g @anthropic-ai/claude-code`). |
| Exit 4: "the first N solve calls all failed" | Auth, model name, or a flag the CLI rejects | Read the error it prints. Run `claude -p "hi"` by hand in the same shell. Check `--model`. |
| Many `rate_limit_events`, or the concurrency number drops | Plan or org rate limits | Nothing is required: the engine adapts, halving on each limit and recovering slowly. To start lower, use `--concurrency 4`. Long runs resume after an interruption. |
| Exit 3: budget reached | `--budget` was hit | Re-run the same command with a higher `--budget`. Finished work is kept. |
| Baseline verify: `command not found` or `ModuleNotFoundError` | The worktree lacks dependencies | Add them to `link_paths` (for example `.venv`, `node_modules`), or change `verify_command` to use the absolute interpreter path. |
| `apply` fails with "does not apply cleanly" | The project changed after the snapshot | Re-run the swarm, or apply `RUN/winner.patch` by hand with `git apply --3way`. |
| Leftover worktrees or disk use | A run was interrupted | `python3 ENGINE clean --run-dir RUN`. Worktrees live under the system temp directory (`godmode/<run-id>/`). |
| Workers run as root and "bypass" is refused | The CLI blocks `bypassPermissions` for root | This is expected. The default already uses `acceptEdits` plus a full tool allowlist. Only pass `--bypass` when not running as root. |
| Windows: symlinks fail | Creating symlinks needs Developer Mode or admin rights | The engine falls back to directory junctions. You can also enable Developer Mode. |
| A result is "CONTESTED" even after refinement | The task is genuinely ambiguous, or no candidate passes | Read the dissent in `WINNER.md`. Tighten the brief or `answer_key_spec`, add a verifier, or raise `--max-refine`. |
| Semantic clustering merged answers that differ | The answer keys were too vague | Make `answer_key_spec` more specific, or pass `--no-semantic-cluster`. |

## Useful files in a run directory

| File | Contents |
|---|---|
| `progress.json` | The current phase, counts, cost and concurrency. `status` prints it. |
| `engine.log` | Every failure and retry. |
| `agents/agent-NNNNN/` | `prompt.md`, `result.json`, `patch.diff`. |
| `refine/rN/agent-NNNNN/` | Solutions from refinement round N. |
| `clusters/rN.json` | Clusters and their members for round N. |
| `votes/qN/`, `votes/fN/` | Every ballot with its ranking, verdicts and reason. |
| `report.json`, `WINNER.md` | The final result. |
