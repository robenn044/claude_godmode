"""The swarm pipeline. Every phase persists its results, so re-running resumes.

SOLVE (X agents) -> VERIFY -> CLUSTER -> QUALIFY (if clusters > F) -> FINAL (all X vote)
  -> contested? -> REFINE (a subset of the X agents) -> CLUSTER -> QUALIFY -> FINAL (all X vote again)
"""

import concurrent.futures
import math
import os
import random
import threading
import time

from . import manifest as mf
from .claude import (BudgetExceeded, CallSpec, ClaudeRunner, PrimerGate, Stopped, detect_session_model,
                     find_claude)
from .cluster import build_clusters, pool_hash
from .prompts import (JUDGE_SYSTEM, LABELS, WORKER_SCHEMA, assignment, ballot_block, recheck_prompt,
                      refine_prompt, shared_brief, verification_text, vote_prompt, vote_schema, worker_prompt)
from .report import build_report, render_winner
from .util import GodmodeError, Logger, agent_name, now_iso, read_json, read_text, write_json, write_text
from .voting import (contest, interpret, order_final, qualify_ballots, qualify_score, rotations, tally,
                     validate_vote)
from .workspace import Workspace, ensure_excluded

REFINE_SUBSET = 4  # candidates shown to each refiner (Recursive Self-Aggregation uses K=4)


def _validate_worker(data):
    if not isinstance(data, dict):
        return "no structured output"
    if not str(data.get("solution") or "").strip() and not str(data.get("answer_key") or "").strip():
        return "empty solution"
    return None


class Swarm(object):
    def __init__(self, run_dir, opts):
        self.run_dir = os.path.abspath(run_dir)
        self.opts = opts
        self.m = mf.load(self.run_dir)
        self.n = self.m["agents"]
        self.log = Logger(os.path.join(self.run_dir, "engine.log"), quiet=getattr(opts, "quiet", False))
        self.state_path = os.path.join(self.run_dir, "state.json")
        self.state = read_json(self.state_path, {}) or {}
        if "seed" not in self.state:
            self.state["seed"] = opts.seed if opts.seed is not None else random.randrange(1 << 30)
            self.state["created_at"] = now_iso()
        self.seed = self.state["seed"]
        self.code = self.m["mode"] == "code"
        self.has_verifier = self.code and bool(self.m.get("verify_command"))
        self.hash = mf.solve_hash(self.m)
        self.F = max(2, opts.finalists)
        self.runner = None
        self.ws = None
        self.brief_path = os.path.join(self.run_dir, "shared_brief.md")
        self.io_lock = threading.RLock()
        self._resolve_models()

    def _resolve_models(self):
        """Every agent runs on the same model as the session that launched the swarm, unless told
        otherwise: --model, then manifest.model, then the model recorded for this run, then the
        launching Claude Code / Claude Desktop session's model."""
        o, m, st = self.opts, self.m, self.state
        detected = detect_session_model()
        model = o.model or m.get("model") or st.get("model") or detected
        source = ("--model" if o.model else "manifest" if m.get("model") else "run state" if st.get("model")
                  else "session" if detected else "claude default")
        vote_model = o.vote_model or m.get("vote_model") or model
        st.update({"model": model, "model_source": source, "vote_model": vote_model,
                   "session_model": detected or st.get("session_model")})
        m["model"], m["vote_model"] = model, vote_model  # in memory only; the clusterer reads these

    # ------------------------------------------------------------------ setup
    def _setup(self):
        if self.runner is None:
            self.runner = ClaudeRunner(find_claude(self.opts.claude_bin), concurrency=self.opts.concurrency,
                                       retries=self.opts.retries, budget=self.opts.budget, log=self.log,
                                       bypass=self.opts.bypass, max_wait_hours=self.opts.max_wait_hours)
            self.runner.stats.update(self.state.get("stats") or {})
        brief = shared_brief(self.m)
        if read_text(self.brief_path) != brief:  # keep bytes stable across resumes (prompt cache)
            write_text(self.brief_path, brief)
        if self.code and self.ws is None:
            ensure_excluded(self.m["cwd"])
            self.ws = Workspace(self.m["cwd"], self.run_dir, self.m["link_paths"], root=self.opts.worktree_root,
                                log=self.log)
            snap = self.ws.snapshot()
            self.log("workspace snapshot: %s (worktrees in %s)" % (snap, self.ws.root))

    def _stats(self):
        if not self.runner:
            return {}
        with self.runner.lock:
            s = dict(self.runner.stats)
            s["models"] = dict(s.get("models") or {})
        return s

    def _save(self, phase=None):
        with self.io_lock:
            if phase:
                self.state["phase"] = phase
            if self.runner:
                self.state["stats"] = self._stats()
            write_json(self.state_path, self.state)

    def _progress(self, phase, done, total, failed, note=""):
        s = self._stats()
        lim = self.runner.limiter if self.runner else None
        if lim and lim.pause_until > time.time() + 60 and not note:
            note = "waiting for usage-limit reset until %s" % time.strftime("%H:%M", time.localtime(lim.pause_until))
        with self.io_lock:
            write_json(os.path.join(self.run_dir, "progress.json"), {
                "phase": phase, "done": done, "total": total, "failed": failed, "note": note,
                "cost_usd": round(s.get("cost_usd", 0.0), 4), "calls": s.get("calls", 0),
                "concurrency_now": lim.limit if lim else None, "model": self.state.get("model"),
                "rate_limit_events": s.get("rate_limit_events", 0), "updated_at": now_iso(), "pid": os.getpid()})

    def _pool(self, phase, items, fn, gate_for=None):
        failed_items = []
        n_failed = self._pool_pass(phase, items, fn, gate_for, failed_items)
        if failed_items:  # one retry sweep for agents that failed (timeouts, transient errors)
            self.log("%s: retrying %d failed item(s) once more" % (phase, len(failed_items)))
            retry_failed = []
            n_failed = self._pool_pass(phase + " (retry)", sorted(failed_items), fn, gate_for, retry_failed,
                                       breaker=False)
        return n_failed

    def _pool_pass(self, phase, items, fn, gate_for, failed_items, breaker=True):
        total, done, failed = len(items), [0], [0]
        flags = {"budget": False, "stopped": False, "breaker": None}
        gates, lock = {}, threading.Lock()
        self._progress(phase, 0, total, 0)

        def wrapped(item):
            key = gate_for(item) if gate_for else 0
            with lock:  # the first item of each prompt-prefix group becomes its cache primer
                gate = gates.setdefault(key, PrimerGate(self.opts.stagger))
            try:
                fn(item, gate)
            except BudgetExceeded:
                flags["budget"] = True
                return
            except Stopped:
                flags["stopped"] = True
                return
            except Exception as e:  # noqa: BLE001 - one agent failing must not sink the swarm
                with lock:
                    failed[0] += 1
                    failed_items.append(item)
                    # Circuit breaker: if the first calls all fail, the setup is broken; stop burning money.
                    if breaker and failed[0] >= min(6, total) and done[0] == 0 and not flags["breaker"]:
                        flags["breaker"] = str(e)[:600]
                        self.runner.kill_all()
                self.log("%s %s FAILED: %s" % (phase, item, str(e)[:400]))
                return
            with lock:
                done[0] += 1
                report = done[0] + failed[0] == total or done[0] % max(1, total // 40) == 0
            if report:
                self._progress(phase, done[0] + failed[0], total, failed[0])
                self._save()
                self.log("%s %d/%d (failed %d, $%.2f, concurrency %d)" % (
                    phase, done[0] + failed[0], total, failed[0], self.runner.stats["cost_usd"], self.runner.limiter.limit))

        with concurrent.futures.ThreadPoolExecutor(max_workers=self.runner.limiter.maximum) as ex:
            for f in concurrent.futures.as_completed([ex.submit(wrapped, it) for it in items]):
                f.result()
        self._progress(phase, done[0] + failed[0], total, failed[0])
        self._save()
        if flags["breaker"]:
            raise GodmodeError("the first %d %s calls all failed, so the run was stopped. Last error: %s"
                               % (failed[0], phase, flags["breaker"]), 4)
        if flags["stopped"]:
            raise GodmodeError("interrupted during %s; re-run the same command to resume" % phase, 130)
        if flags["budget"]:
            self._progress(phase, done[0], total, failed[0], "budget reached")
            raise GodmodeError("budget of $%.2f reached during %s. Re-run with a higher --budget to resume."
                               % (self.opts.budget, phase), 3)
        return failed[0]

    # ------------------------------------------------------------------ solve
    def _solver_spec(self, label, prompt, cwd):
        m, o = self.m, self.opts
        return CallSpec(
            label, prompt, cwd, schema=WORKER_SCHEMA, append_file=self.brief_path, tools=m.get("worker_tools"),
            full_access=True, disallowed_tools=None if m.get("worker_subagents") else "Agent",
            model=m.get("model"), fallback_model=m.get("fallback_model"),
            effort=o.effort or m["worker_effort"], add_dirs=() if self.code else (m["cwd"],),
            strict_mcp=not m.get("worker_mcp"), max_turns=o.max_turns, max_budget=o.agent_budget, timeout=o.timeout)

    def _solve(self, key, directory, prompt, strategy, lens, round_no, agent, gate):
        res_path = os.path.join(directory, "result.json")
        rec = read_json(res_path)
        if rec and rec.get("solve_hash") == self.hash:
            return
        write_text(os.path.join(directory, "prompt.md"), prompt)
        ws_path = None
        if self.code:
            ws_path, cwd = self.ws.create(key)
        else:
            cwd = os.path.join(directory, "scratch")
            os.makedirs(cwd, exist_ok=True)
        try:
            res = self.runner.run(self._solver_spec(key, prompt, cwd), gate, validate=_validate_worker)
            patch, verify = "", None
            if self.code:
                patch = self.ws.capture(ws_path)
                if self.has_verifier:
                    verify = self.ws.verify(cwd, self.m["verify_command"], self.m["verify_timeout"])
        finally:
            if ws_path:
                self.ws.remove(ws_path)
        d = res.data
        rec = {"key": key, "agent": agent, "round": round_no, "solve_hash": self.hash, "strategy": strategy,
               "lens": lens, "summary": str(d.get("summary", "")), "answer_key": str(d.get("answer_key", "")),
               "solution": str(d.get("solution", "")), "confidence": _int(d.get("confidence")),
               "checks_done": str(d.get("checks_done", "")), "patch": patch, "verify": verify,
               "cost_usd": res.cost, "num_turns": res.num_turns, "duration_s": round(res.duration, 1),
               "models": res.models,
               "finished_at": now_iso()}
        if patch:
            write_text(os.path.join(directory, "patch.diff"), patch)
        write_json(res_path, rec)

    def solve_agent(self, i, gate):
        s, lens, _ = assignment(self.m, i, self.seed)
        self._solve(agent_name(i), os.path.join(self.run_dir, "agents", agent_name(i)),
                    worker_prompt(self.m, i, self.seed), s["name"], lens, 0, i, gate)

    def load_solutions(self, agents):
        pool = {}
        for i in agents:
            rec = read_json(os.path.join(self.run_dir, "agents", agent_name(i), "result.json"))
            if rec and rec.get("solve_hash") == self.hash:
                pool[rec["key"]] = rec
        return pool

    # --------------------------------------------------------------- clusters
    def clusters_for(self, round_no, pool):
        path = os.path.join(self.run_dir, "clusters", "r%d.json" % round_no)
        h = pool_hash(pool)
        cached = read_json(path)
        if cached and cached.get("pool_hash") == h and cached.get("semantic") == (not self.opts.no_semantic_cluster):
            clusters = cached["clusters"]
        else:
            self._progress("cluster r%d" % round_no, 0, len(pool), 0)
            clusters, calls = build_clusters(pool, self.m, self.runner, semantic=not self.opts.no_semantic_cluster,
                                             log=self.log, label="cluster-r%d" % round_no)
            write_json(path, {"pool_hash": h, "semantic": not self.opts.no_semantic_cluster, "calls": calls,
                              "clusters": clusters})
            self._save()
        self.log("round %d: %d solutions -> %d clusters" % (round_no, len(pool), len(clusters)))
        return clusters

    # ------------------------------------------------------------------ votes
    def _vote(self, stage, voter, order, block, gate, what):
        path = os.path.join(self.run_dir, "votes", stage, agent_name(voter) + ".json")
        if os.path.exists(path):
            return
        angles = self.m["judge_angles"]
        angle = angles[(voter - 1) % len(angles)]
        if len(order) == 1:
            write_json(path, {"voter": voter, "ballot": order, "vote": order[0], "ranking": order, "verdicts": {},
                              "notes": {}, "reason": "only candidate on the ballot", "angle": angle, "forced": True})
            return
        m, o = self.m, self.opts
        vt = m.get("voter_tools") or ""
        spec = CallSpec("%s-%s" % (stage, agent_name(voter)), vote_prompt(block, angle, what), self.run_dir,
                        schema=vote_schema(len(order)), system_prompt=JUDGE_SYSTEM, tools=vt,
                        allowed_tools=vt or None, model=m.get("vote_model"),
                        fallback_model=m.get("fallback_model"), effort=m["voter_effort"],
                        add_dirs=(m["cwd"],) if vt else (), timeout=o.vote_timeout, judge=True)
        res = self.runner.run(spec, gate, validate=validate_vote(len(order)))
        rec = interpret(res.data, order)
        cost, models = res.cost, list(res.models)
        if rec["confidence"] is not None and rec["confidence"] < o.vote_confidence:
            # The voter is unsure: make it look again, harder, before its vote counts.
            first = dict(rec)
            spec.prompt = recheck_prompt(spec.prompt, first)
            spec.effort = "xhigh" if m["voter_effort"] in ("low", "medium", "high") else "max"
            try:
                res2 = self.runner.run(spec, None, validate=validate_vote(len(order)))
                rec = interpret(res2.data, order)
                rec["first_vote"] = {"vote": first["vote"], "confidence": first["confidence"]}
                rec["rechecked"] = True
                cost += res2.cost
                models += res2.models
            except (BudgetExceeded, Stopped):
                raise
            except Exception as e:  # noqa: BLE001 - keep the first, recorded-as-unsure vote
                self.log("recheck of %s failed (%s); keeping its first vote" % (spec.label, str(e)[:200]))
        rec.update({"voter": voter, "ballot": order, "angle": angle, "cost_usd": cost, "models": sorted(set(models))})
        write_json(path, rec)

    def _load_votes(self, stage):
        d = os.path.join(self.run_dir, "votes", stage)
        out = []
        if os.path.isdir(d):
            for name in sorted(os.listdir(d)):
                if name.endswith(".json"):
                    v = read_json(os.path.join(d, name))
                    if v:
                        out.append(v)
        return out

    def own_clusters(self, clusters, round_no):
        owner = {}
        for c in clusters:
            for k in c["members"]:
                agent = int(k.rsplit("-", 1)[1])
                if k.startswith("r%d-" % round_no) or agent not in owner:
                    owner[agent] = c["id"]
        return owner

    def qualify(self, round_no, clusters, eligible):
        stage = "q%d" % round_no
        by_id = {c["id"]: c for c in clusters}
        rng = random.Random("%s-%s" % (self.seed, stage))
        ballots = qualify_ballots([c["id"] for c in eligible], self.own_clusters(clusters, round_no),
                                  list(range(1, self.n + 1)), self.opts.ballot_size, rng)
        limit = self.opts.max_candidate_chars

        def one(v, gate):
            self._vote(stage, v, ballots[v], ballot_block(self.m, ballots[v], by_id, limit), gate,
                       "QUALIFYING ROUND: the best candidates advance to the final vote")
        self._pool("qualify r%d" % round_no, [v for v in ballots if ballots[v]], one)
        votes = self._load_votes(stage)
        t = tally(votes, [c["id"] for c in eligible])

        def key(c):
            s = t[c["id"]]
            passed = (c.get("verify") or {}).get("passed")
            return (0 if (not self.has_verifier or passed) else 1, -qualify_score(s, len(c["members"])),
                    -s["borda_share"], -len(c["members"]), c["id"])
        ranked = sorted(eligible, key=key)
        info = [{"candidate": c["id"], "score": round(qualify_score(t[c["id"]], len(c["members"])), 4),
                 "first": t[c["id"]]["first"], "borda_share": t[c["id"]]["borda_share"],
                 "appearances": t[c["id"]]["appearances"], "supporters": len(c["members"])} for c in ranked]
        return [c["id"] for c in ranked[:self.F]], info, len(votes)

    def final(self, round_no, clusters, finalists):
        stage = "f%d" % round_no
        by_id = {c["id"]: c for c in clusters}
        rots = rotations(finalists)
        limit = self.opts.max_candidate_chars
        blocks = [ballot_block(self.m, r, by_id, limit) for r in rots]

        def one(v, gate):
            r = (v - 1) % len(rots)
            self._vote(stage, v, rots[r], blocks[r], gate, "FINAL VOTE: the candidate with the most votes wins")
        self._pool("final r%d" % round_no, list(range(1, self.n + 1)), one, gate_for=lambda v: (v - 1) % len(rots))
        votes = self._load_votes(stage)
        t = tally(votes, finalists)
        ranking = order_final(t, by_id)
        return ranking, t, votes

    # ----------------------------------------------------------------- refine
    def refine(self, round_no, clusters, ranking, votes):
        """round_no = the new round. Refined solutions are keyed r<round>-agent-xxxxx."""
        by_id = {c["id"]: c for c in clusters}
        rng = random.Random("%s-refine-%d" % (self.seed, round_no))
        count = self.n if self.n < 4 else min(self.n, max(4, int(math.ceil(self.n * self.opts.refine_fraction))))
        owner = self.own_clusters(clusters, round_no - 1)
        dissent = [a for a in range(1, self.n + 1) if owner.get(a) != ranking[0]]
        rest = [a for a in range(1, self.n + 1) if owner.get(a) == ranking[0]]
        rng.shuffle(dissent)
        rng.shuffle(rest)
        refiners = sorted((dissent + rest)[:count])
        critiques = {c: [] for c in ranking}
        for v in votes:
            for c, verdict in (v.get("verdicts") or {}).items():
                if c in critiques and verdict in ("flawed", "wrong") and v.get("notes", {}).get(c):
                    critiques[c].append("%s: %s" % (verdict, v["notes"][c]))
        for c in critiques:
            critiques[c] = critiques[c][:5]
        limit = self.opts.max_candidate_chars
        picks = {}
        for a in refiners:  # the leader plus a random subset of the other finalists (RSA-style)
            pick = [ranking[0]] + rng.sample(ranking[1:], min(REFINE_SUBSET - 1, len(ranking) - 1))
            rng.shuffle(pick)
            picks[a] = pick

        def one(a, gate):
            pick = picks[a]
            items = [{"label": LABELS[i], "text": _trunc(by_id[c]["display"], limit),
                      "verification": verification_text(by_id[c]), "critiques": critiques.get(c) or ["(none recorded)"]}
                     for i, c in enumerate(pick)]
            s, lens, _ = assignment(self.m, a, self.seed)
            key = "r%d-%s" % (round_no, agent_name(a))
            self._solve(key, os.path.join(self.run_dir, "refine", "r%d" % round_no, agent_name(a)),
                        refine_prompt(self.m, a, self.seed, items), s["name"], lens, round_no, a, gate)
        self._pool("refine r%d" % round_no, refiners, one)
        pool = {}
        for a in refiners:
            rec = read_json(os.path.join(self.run_dir, "refine", "r%d" % round_no, agent_name(a), "result.json"))
            if rec and rec.get("solve_hash") == self.hash:
                pool[rec["key"]] = rec
        return pool, refiners

    # -------------------------------------------------------------------- run
    def pilot(self, count):
        self._setup()
        agents = list(range(1, min(count, self.n) + 1))
        self._save("pilot")
        self._pool("pilot", agents, self.solve_agent)
        pool = self.load_solutions(agents)
        costs = [r["cost_usd"] for r in pool.values()]
        durs = [r["duration_s"] for r in pool.values()]
        avg = sum(costs) / len(costs) if costs else 0.0
        out = {
            "pilot_agents": len(agents), "succeeded": len(pool), "avg_solve_cost_usd": round(avg, 4),
            "avg_solve_seconds": round(sum(durs) / len(durs), 1) if durs else None,
            "projected_solve_cost_usd": round(avg * self.n, 2),
            "projected_total_cost_usd_range": [round(avg * self.n * 1.15, 2), round(avg * self.n * 1.6, 2)],
            "note": "Voting and clustering typically add 15-60% on top of solving; refinement (if triggered) "
                    "adds about refine_fraction x solve cost plus one more vote.",
            "samples": [{"agent": r["agent"], "strategy": r["strategy"], "answer_key": r["answer_key"],
                         "confidence": r["confidence"], "verify_passed": (r.get("verify") or {}).get("passed")}
                        for r in sorted(pool.values(), key=lambda r: r["agent"])],
        }
        write_json(os.path.join(self.run_dir, "pilot.json"), out)
        return out

    def run(self):
        self._setup()
        o = self.opts
        self.log("GODMODE: %d agents | mode %s | verifier %s | concurrency %d | CLI %s" % (
            self.n, self.m["mode"], "yes" if self.has_verifier else "no", o.concurrency, self.runner.caps.version))
        self._save("solve")
        self._pool("solve", list(range(1, self.n + 1)), self.solve_agent)
        pool = self.load_solutions(range(1, self.n + 1))
        if not pool:
            raise GodmodeError("no agent produced a solution; see %s/engine.log" % self.run_dir, 4)

        rounds, round_no = [], 0
        while True:
            clusters = self.clusters_for(round_no, pool)
            by_id = {c["id"]: c for c in clusters}
            passing = [c for c in clusters if (c.get("verify") or {}).get("passed")]
            eligible = passing if (self.has_verifier and len(passing) >= 2) else clusters
            qual, qual_votes = None, 0
            if len(eligible) > self.F:
                self._save("qualify r%d" % round_no)
                finalists, qual, qual_votes = self.qualify(round_no, clusters, eligible)
            else:
                finalists = [c["id"] for c in sorted(eligible, key=lambda c: (-len(c["members"]), c["id"]))]
            if len(finalists) == 1:
                ranking, t, votes = finalists, {finalists[0]: {"first": self.n, "borda": float(self.n),
                                                               "appearances": self.n, "borda_share": 1.0}}, []
                unanimous = True
            else:
                self._save("final r%d" % round_no)
                ranking, t, votes = self.final(round_no, clusters, finalists)
                unanimous = False
            c = contest(t, ranking, by_id, self.has_verifier, o.contest_share, o.contest_margin, votes=votes)
            if unanimous:
                c["reasons"] = [r for r in c["reasons"] if "verification" in r]
                c["contested"] = bool(c["reasons"])
            rounds.append({"round": round_no, "solutions": len(pool), "clusters": clusters, "eligible": len(eligible),
                           "qualify": qual, "qualify_votes": qual_votes, "finalists": finalists, "ranking": ranking,
                           "tally": t, "votes": votes, "contest": c, "unanimous": unanimous})
            self.log("round %d result: %s leads with %s/%s first-choice votes%s" % (
                round_no, ranking[0], t[ranking[0]]["first"], c["total_votes"] or self.n,
                "; CONTESTED: " + "; ".join(c["reasons"]) if c["contested"] else ""))
            if not c["contested"] or o.no_refine or round_no >= o.max_refine or self.n < 2:
                break
            round_no += 1
            self._save("refine r%d" % round_no)
            refined, refiners = self.refine(round_no, clusters, ranking, votes)
            rounds[-1]["refiners"] = refiners
            if not refined:
                self.log("refinement produced no solutions; keeping round %d result" % (round_no - 1))
                break
            keep = set()
            for cid in finalists:
                keep.update(by_id[cid]["members"])
            pool = dict({k: v for k, v in pool.items() if k in keep}, **refined)

        self._save("done")
        report = build_report(self, rounds)
        write_json(os.path.join(self.run_dir, "report.json"), report)
        write_text(os.path.join(self.run_dir, "WINNER.md"), render_winner(report))
        self._progress("done", self.n, self.n, report["failures"]["solve"])
        self.log("WINNER %s (%s). Report: %s" % (report["winner"]["candidate"], report["winner"]["votes_text"],
                                                 os.path.join(self.run_dir, "WINNER.md")))
        return report

    def dry_run(self):
        brief = shared_brief(self.m)
        write_text(self.brief_path, brief)
        write_text(os.path.join(self.run_dir, "sample_prompt_agent1.md"), worker_prompt(self.m, 1, self.seed))
        self._save()
        n, F = self.n, self.F
        return {
            "agents": n, "mode": self.m["mode"], "verifier": self.has_verifier, "strategies": len(self.m["strategies"]),
            "lenses": len(self.m["lenses"]), "judge_angles": len(self.m["judge_angles"]),
            "claude_calls_estimate": {"solve": n, "cluster": "~%d" % max(1, n // 150), "qualify": "0-%d" % n,
                                      "final": "%d (0 if unanimous)" % n,
                                      "refine_if_contested": "~%d solve + %d vote" % (
                                          min(n, max(4, int(math.ceil(n * self.opts.refine_fraction)))), n)},
            "finalists": F, "concurrency": self.opts.concurrency, "shared_brief_chars": len(brief),
            "claude_bin": find_claude(self.opts.claude_bin), "cwd": self.m["cwd"],
        }


def _int(v):
    try:
        return max(0, min(100, int(v)))
    except (TypeError, ValueError):
        return 0


def _trunc(t, limit):
    from .util import truncate
    return truncate(t, limit)
