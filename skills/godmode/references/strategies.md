# Strategy and judge-angle library

Diversity of *approach* is what makes a swarm beat a single agent. Research backs this:
- PlanSearch: searching over different plans beat repeated sampling at the same budget.
- DIV-SE: combining approach and persona beat raising the temperature.

Treat the list below as raw material. Tailor each strategy to the task, and write it as a concrete workflow of 3–6 steps.

## Contents
- How to build strategies
- Strategy archetypes (code, reasoning, research and design)
- Lenses
- Judge angles

## How to build strategies

1. List 8–20 **observations** about the problem: facts, symmetries, constraints, suspicious spots, analogies.
2. Combine them into candidate **plans**. Keep the plans whose *first step* is different from every other plan's.
3. Keep 4–24. Use more when the problem is open-ended and fewer when it is narrow.
4. Always include at least:
   - one **contrarian** strategy: "assume the obvious framing is wrong"
   - one **empirical** strategy: build it, run it, measure it
   - one **first-principles** strategy
5. Use the known dead ends as negative constraints. Never assign one as a strategy.

## Strategy archetypes

### Code: debugging and fixing
- **Reproduce and bisect.** Get a deterministic reproduction, then bisect over inputs, commits or code paths until one line is left.
- **Instrument the truth.** Add logging or tracing, capture the real execution, and compare it with the expected execution.
- **Invariant-first.** State the invariants, find where they break, and restore them structurally.
- **Test-first.** Write the smallest failing test that pins the bug down, then make it pass with the smallest correct change.
- **Minimal diff.** Make the smallest change that is fully correct. Refactoring is forbidden.
- **Root-cause redesign.** If the design itself causes the bug, replace the flawed mechanism while keeping the API.
- **Upstream/prior art.** Look for how this codebase, its dependencies or well-known projects already solved the same problem, and follow that.
- **Adversarial.** Write the nastiest inputs and interleavings first, then build a fix that survives them.

### Code: building
- **Spec-to-tests.** Turn every requirement into a test, then implement.
- **Walking skeleton.** Get the thinnest end-to-end path working first, then deepen it.
- **Reuse-first.** Build as much as possible from existing utilities in the codebase.
- **Performance-first.** Choose data structures for the worst case, then write the code and measure it.

### Reasoning and math
- **Work backwards** from the goal, or from the form any answer must take.
- **Small cases → pattern → proof.** Solve n = 1, 2, 3, … by hand or in code, guess the rule, then prove it.
- **Invariants and extremal principle.**
- **Bound both sides.** Prove a lower bound and an upper bound, and look for where they meet.
- **Computational check.** Write a program in the scratch directory that brute-forces or verifies the answer.
- **Reformulate.** Translate the problem into another domain (graphs, algebra, probability), solve it there, and translate back.
- **Counterexample hunt.** Try to break the popular answer. Keep whichever answer survives.

### Research, analysis and design
- **Primary sources only.** Read specs, source code and papers, and use no summaries.
- **Compare three options** against explicit criteria and pick one.
- **Pre-mortem.** Assume the chosen design failed in production, find out why, and design against that failure.
- **Steelman the alternative,** then decide.
- **Constraint-first.** List the hard constraints, eliminate options that break them, and optimise within what is left.

## Lenses (secondary priorities)

- Correctness above all
- Minimal change
- Robustness and edge cases
- Performance
- Readability and maintainability
- Security
- Backward compatibility
- User experience

## Judge angles (how each agent votes)

Each voter checks every candidate from its own angle but votes on overall merit. Using different angles gives more independent votes than many identical judges. Research found that 9 identical judges behave like about 2 independent votes.

- **Correctness tracer.** Re-derive or trace every step: is the result actually right?
- **Edge-case hunter.** Find the inputs, states or conditions that break it.
- **Spec compliance.** Does it do exactly what was asked, with nothing missing and nothing extra?
- **Regression hunter.** What else could this change break?
- **Evidence auditor.** Are the claims backed by verification output or a proof, or only asserted?
- **Simplicity auditor.** Is it the simplest solution that is fully correct?
- **Security reviewer.** Does it add injection, secrets exposure, unsafe defaults or races?
- **Performance reviewer.** Complexity, hot paths, memory.
