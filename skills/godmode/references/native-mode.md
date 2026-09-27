# Native mode (fallback without the engine)

Use this mode only when preflight reports that the `claude` CLI or Python is missing, or when the user passes `--native`. Native mode runs inside your own session with the Agent tool. It is limited to **X ≤ 50**, because every result lands in your context and the session caps concurrent subagents (about 20 by default). For a larger X, tell the user to install Python 3.8+ and the Claude Code CLI, then use the engine.

## Steps

1. **Recon, crux and strategies.** Do these exactly as in SKILL.md steps 3 and 4. Write the brief once.

2. **Solve.** Spawn X `general-purpose` agents in parallel, at most 10 per message, until all X are done.
   - Build each prompt as the shared brief first, then `<assignment>` (agent number, strategy workflow, lens) at the end.
   - Code tasks: pass `isolation: worktree` if your Agent tool supports it. If it does not, instruct agents to propose a unified diff and not edit files.
   - Every agent must end its reply with:
     ```
     <answer_key>one line</answer_key>
     <solution>...</solution>
     <confidence>0-100</confidence>
     ```

3. **Verify** (code tasks). For each candidate diff, apply it in a scratch worktree (`git worktree add`), run the verifier, record pass or fail, then remove the worktree.

4. **Cluster.** Merge candidates whose answer keys are substantively identical.
   - The representative is the best member: verified first, then the highest confidence.
   - If at least 2 clusters passed verification, keep only the passing ones.

5. **Vote.**
   - **Voters.** Every one of the X agents votes. Spawn fresh voter agents with no memory of their own solution, one per agent, each assigned that agent's judge angle.
   - **Ballot.** Every voter gets the finalists (at most 6 clusters), anonymised as A, B, C… and ordered by rotation `(voter-1) mod F`. It lists each candidate's verification result but not its supporter count.
   - **Qualifying round.** If there are more than 6 clusters, run one first: each voter ranks 6 *other* clusters, and the top 6 by average rank advance.
   - **Voter reply format:**
     ```
     <ranking>B,A,C</ranking>
     <vote>B</vote>
     <reason>…</reason>
     ```

6. **Decide.** The winner is the candidate with the most first-choice votes. Break ties by average rank, then verification, then supporter count.
   - If the winner has under 50% of the votes, leads by under 15%, or failed verification, run **one** refinement round.
   - In that round, max(4, 10% of X) agents each get the leader plus up to 3 other finalists along with the critiques, and produce an improved solution.
   - Cluster again, then everyone votes again.

7. **Report.** Report exactly as in SKILL.md step 8.
