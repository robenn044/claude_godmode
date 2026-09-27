"""End-to-end tests for the godmode engine using tests/fake_claude.py (no API calls).

Run:  python3 -m unittest discover -s tests -v
"""
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(ROOT, "skills", "godmode", "scripts")
ENGINE = os.path.join(SCRIPTS, "godmode_engine.py")
FAKE = os.path.join(ROOT, "tests", "fake_claude.py")
sys.path.insert(0, SCRIPTS)

from godmode import voting  # noqa: E402
from godmode.claude import AdaptiveLimiter, run_shell  # noqa: E402

HAS_GIT = shutil.which("git") is not None


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="godmode-test-")
        self.env = dict(os.environ)
        for k in ("GODMODE_CHILD", "FAKE_SCENARIO", "FAKE_NO_SCHEMA", "FAKE_RATE_LIMIT_EVERY", "FAKE_LOG"):
            self.env.pop(k, None)
        self.env["FAKE_COUNTER"] = os.path.join(self.tmp, "counter")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_run(self, agents, name="run", **extra):
        run_dir = os.path.join(self.tmp, name)
        os.makedirs(run_dir, exist_ok=True)
        m = {"task": "What is the answer?", "agents": agents,
             "strategies": [{"name": "S%d" % k, "workflow": "do %d" % k} for k in range(4)],
             "lenses": ["fast", "careful"], "cwd": self.tmp}
        m.update(extra)
        with open(os.path.join(run_dir, "manifest.json"), "w") as f:
            json.dump(m, f)
        return run_dir

    def engine(self, *args, **env):
        e = dict(self.env, **env)
        cmd = [sys.executable, ENGINE] + list(args)
        if args and args[0] == "run":
            cmd += ([] if "--claude-bin" in args else ["--claude-bin", FAKE]) + ["--seed", "7", "--stagger", "0.2", "--quiet"]
        return subprocess.run(cmd, env=e, capture_output=True, text=True, timeout=600)

    def run_ok(self, run_dir, *flags, **env):
        p = self.engine("run", "--run-dir", run_dir, *flags, **env)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        return load(os.path.join(run_dir, "report.json"))


class TestConsensus(Base):
    def test_semantic_clusters_qualify_and_final(self):
        run = self.make_run(40)
        r = self.run_ok(run)
        self.assertIn("42", r["winner"]["solution"])
        self.assertEqual(r["winner"]["supporters"], 13)  # "42", "forty-two", "the answer is 42" merged
        self.assertEqual(r["winner"]["first_choice_votes"], 40)  # every agent voted
        rd = r["rounds"][0]
        self.assertTrue(rd["qualifying"])
        self.assertLessEqual(len(rd["finalists"]), 6)
        self.assertFalse(r["contest"]["contested"])
        self.assertEqual(len(r["rounds"]), 1)
        # qualifying ballots never contain the voter's own cluster
        clusters = load(os.path.join(run, "clusters", "r0.json"))["clusters"]
        own = {int(k.rsplit("-", 1)[1]): c["id"] for c in clusters for k in c["members"]}
        qdir = os.path.join(run, "votes", "q0")
        for name in os.listdir(qdir):
            v = load(os.path.join(qdir, name))
            self.assertNotIn(own[v["voter"]], v["ballot"])

    def test_final_rotations_balance_positions(self):
        run = self.make_run(24)
        self.run_ok(run)
        fdir = os.path.join(run, "votes", "f0")
        firsts = {}
        for name in os.listdir(fdir):
            b = load(os.path.join(fdir, name))["ballot"]
            firsts[b[0]] = firsts.get(b[0], 0) + 1
        self.assertEqual(len(set(firsts.values())), 1, firsts)  # each finalist leads equally often

    def test_shared_brief_is_a_single_identical_file(self):
        run = self.make_run(8)
        log = os.path.join(self.tmp, "calls.jsonl")
        self.run_ok(run, FAKE_LOG=log)
        with open(log) as f:
            calls = [json.loads(line) for line in f]
        solver = [c for c in calls if "--append-system-prompt-file" in c["flags"]]
        self.assertEqual(len(solver), 8)
        self.assertEqual(len({c["system_sha"] for c in solver}), 1)
        for c in solver:  # per-agent content comes last, shared content is in the system prompt file
            self.assertTrue(c["prompt_head"].startswith("<assignment>"))
        judges = [c for c in calls if "--system-prompt" in c["flags"]]
        self.assertTrue(all("--tools" in c["flags"] for c in judges))

    def test_single_agent_is_unanimous_without_votes(self):
        r = self.run_ok(self.make_run(1))
        self.assertTrue(r["rounds"][0]["unanimous"])
        self.assertEqual(r["usage"]["claude_calls"], 1)

    def test_resume_makes_no_new_calls(self):
        run = self.make_run(12)
        self.run_ok(run)
        calls_before = load(os.path.join(run, "report.json"))["usage"]["claude_calls"]
        r = self.run_ok(run)
        self.assertEqual(r["usage"]["claude_calls"], calls_before)

    def test_capability_fallback_without_json_schema(self):
        r = self.run_ok(self.make_run(9), FAKE_NO_SCHEMA="1")
        self.assertIn("42", r["winner"]["solution"])

    def test_runtime_unknown_option_is_dropped(self):
        r = self.run_ok(self.make_run(6), FAKE_NO_SCHEMA="2")
        self.assertIn("42", r["winner"]["solution"])

    def test_no_semantic_cluster_flag(self):
        r = self.run_ok(self.make_run(9), "--no-semantic-cluster")
        self.assertLess(r["winner"]["supporters"], 3)


class TestRefinement(Base):
    def test_contested_vote_triggers_refinement_and_revote(self):
        run = self.make_run(12, judge_angles=["A1 x", "A2 y", "A3 z"])
        r = self.run_ok(run, FAKE_SCENARIO="split")
        self.assertEqual(len(r["rounds"]), 2)
        self.assertTrue(r["rounds"][0]["contest"]["contested"])
        self.assertIn("BEST", r["winner"]["solution"])
        self.assertEqual(r["winner"]["round"], 1)
        self.assertEqual(len(os.listdir(os.path.join(run, "votes", "f1"))), 12)  # all agents vote again

    def test_no_refine_flag(self):
        run = self.make_run(12, judge_angles=["A1 x", "A2 y", "A3 z"])
        r = self.run_ok(run, "--no-refine", FAKE_SCENARIO="split")
        self.assertEqual(len(r["rounds"]), 1)
        self.assertTrue(r["contest"]["contested"])


@unittest.skipUnless(HAS_GIT, "git required")
class TestCodeMode(Base):
    def make_repo(self):
        repo = os.path.join(self.tmp, "proj")
        os.makedirs(repo)
        with open(os.path.join(repo, "calc.py"), "w") as f:
            f.write("def add(a, b):\n    return a * b\n")
        with open(os.path.join(repo, "test_calc.py"), "w") as f:
            f.write("import calc\nassert calc.add(2, 3) == 5\n")
        g = ["git", "-c", "user.name=t", "-c", "user.email=t@t"]
        subprocess.run(g + ["init", "-q"], cwd=repo, check=True)
        subprocess.run(g + ["add", "-A"], cwd=repo, check=True)
        subprocess.run(g + ["commit", "-qm", "init"], cwd=repo, check=True)
        with open(os.path.join(repo, "untracked.txt"), "w") as f:
            f.write("keep me\n")
        return repo

    def git(self, repo, *args):
        return subprocess.run(["git"] + list(args), cwd=repo, capture_output=True, text=True).stdout

    def test_worktrees_verify_filter_and_apply(self):
        repo = self.make_repo()
        head, status = self.git(repo, "rev-parse", "HEAD"), self.git(repo, "status", "--porcelain")
        wt_root = os.path.join(self.tmp, "wt")
        run = self.make_run(9, mode="code", cwd=repo, verify_command='"%s" test_calc.py' % sys.executable)
        p = self.engine("validate", "--run-dir", run, "--baseline", "--worktree-root", wt_root)
        self.assertEqual(p.returncode, 0, p.stderr)
        r = self.run_ok(run, "--worktree-root", wt_root, FAKE_SCENARIO="code")
        # 6 of 9 agents wrote a wrong fix, but verification + voting pick the correct minority
        self.assertTrue(r["winner"]["verify"]["passed"])
        self.assertEqual(r["winner"]["supporters"], 3)
        # the user's repo is untouched until apply: same HEAD, same status, no leftover worktrees
        self.assertEqual(self.git(repo, "rev-parse", "HEAD"), head)
        self.assertEqual(self.git(repo, "status", "--porcelain"), status)
        self.assertEqual(len(self.git(repo, "worktree", "list").strip().splitlines()), 1)
        self.assertEqual(os.listdir(wt_root) if os.path.isdir(wt_root) else [], [])
        p = self.engine("apply", "--run-dir", run)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        with open(os.path.join(repo, "calc.py")) as f:
            self.assertIn("a + b", f.read())
        self.assertEqual(subprocess.run([sys.executable, "test_calc.py"], cwd=repo).returncode, 0)
        p = self.engine("clean", "--run-dir", run, "--worktree-root", wt_root)
        self.assertEqual(p.returncode, 0)
        self.assertEqual(self.git(repo, "for-each-ref", "refs/godmode"), "")

    def test_all_failing_verification_triggers_refinement(self):
        repo = self.make_repo()
        run = self.make_run(3, mode="code", cwd=repo, verify_command='"%s" -c "import sys; sys.exit(1)"' % sys.executable)
        r = self.run_ok(run, "--worktree-root", os.path.join(self.tmp, "wt"), FAKE_SCENARIO="code")
        self.assertIn("no candidate passed verification", " ".join(r["rounds"][0]["contest"]["reasons"]))
        self.assertEqual(len(r["rounds"]), 2)


class TestControls(Base):
    def test_budget_stops_resumably(self):
        run = self.make_run(20)
        p = self.engine("run", "--run-dir", run, "--budget", "0.005", "--concurrency", "1")
        self.assertEqual(p.returncode, 3, p.stderr)
        self.assertIn("budget", p.stderr)
        r = self.run_ok(run)  # resume without a budget
        self.assertIn("42", r["winner"]["solution"])

    def test_pilot_then_full_run_and_brief_change_invalidates(self):
        run = self.make_run(10)
        p = self.engine("run", "--run-dir", run, "--pilot", "3")
        self.assertEqual(p.returncode, 0, p.stderr)
        pilot = json.loads(p.stdout)
        self.assertEqual(pilot["succeeded"], 3)
        self.assertAlmostEqual(pilot["projected_solve_cost_usd"], 0.01, places=3)
        m = load(os.path.join(run, "manifest.json"))
        m["brief"] = "changed after the pilot"
        with open(os.path.join(run, "manifest.json"), "w") as f:
            json.dump(m, f)
        log = os.path.join(self.tmp, "calls.jsonl")
        self.run_ok(run, FAKE_LOG=log)
        with open(log) as f:
            solver_calls = [json.loads(line) for line in f]
        self.assertEqual(len([c for c in solver_calls if "--append-system-prompt-file" in c["flags"]]), 10)

    def test_rate_limit_events_reduce_concurrency(self):
        r = self.run_ok(self.make_run(30), "--concurrency", "8", FAKE_RATE_LIMIT_EVERY="5")
        self.assertGreater(r["usage"]["rate_limit_events"], 0)

    def test_circuit_breaker_on_broken_setup(self):
        run = self.make_run(50)
        p = self.engine("run", "--run-dir", run, "--claude-bin", sys.executable)  # "claude" that is really python
        self.assertEqual(p.returncode, 4, p.stderr)
        self.assertIn("all failed", p.stderr)

    def test_rejects_invalid_manifest_verbosely(self):
        run = self.make_run(10001, mode="nope", strategies=[{"name": ""}])
        p = self.engine("validate", "--run-dir", run)
        self.assertEqual(p.returncode, 2)
        for s in ("between 1 and 10000", "mode must be", "strategies[0]"):
            self.assertIn(s, p.stderr)

    def test_dry_run_10000_agents(self):
        run = self.make_run(10000)
        p = self.engine("run", "--run-dir", run, "--dry-run")
        self.assertEqual(p.returncode, 0, p.stderr)
        plan = json.loads(p.stdout)
        self.assertEqual(plan["claude_calls_estimate"]["solve"], 10000)
        self.assertFalse(os.path.exists(os.path.join(run, "agents")))

    def test_refuses_nested_swarm(self):
        p = self.engine("run", "--run-dir", self.make_run(3), GODMODE_CHILD="1")
        self.assertEqual(p.returncode, 2)

    def test_preflight_never_fails(self):
        p = self.engine("preflight", "--claude-bin", "/nonexistent/claude")
        self.assertEqual(p.returncode, 0)
        self.assertIn("godmode engine", p.stdout)

    @unittest.skipIf(os.name == "nt", "POSIX process groups")
    def test_timeout_kills_process_tree(self):
        marker = os.path.join(self.tmp, "child_alive")
        cmd = "(sleep 3; touch %s) & sleep 30" % marker
        start = time.time()
        code, _ = run_shell(cmd, self.tmp, timeout=1)
        self.assertEqual(code, 124)
        self.assertLess(time.time() - start, 10)
        time.sleep(3.5)
        self.assertFalse(os.path.exists(marker))

    def test_scale_2000_agents(self):
        run = self.make_run(2000)
        start = time.time()
        r = self.run_ok(run, "--concurrency", "64")
        self.assertEqual(r["winner"]["first_choice_votes"], 2000)
        self.assertLess(time.time() - start, 300)


class TestVotingUnits(unittest.TestCase):
    def test_qualify_ballots_are_balanced(self):
        rng = random.Random(1)
        cands = ["C%d" % i for i in range(50)]
        own = {v: cands[v % 50] for v in range(1, 1001)}
        ballots = voting.qualify_ballots(cands, own, list(range(1, 1001)), 6, rng)
        counts = {}
        for v, b in ballots.items():
            self.assertEqual(len(b), 6)
            self.assertNotIn(own[v], b)
            self.assertEqual(len(set(b)), 6)
            for c in b:
                counts[c] = counts.get(c, 0) + 1
        self.assertLessEqual(max(counts.values()) - min(counts.values()), 12)

    def test_interpret_moves_vote_first_and_completes_ranking(self):
        iv = voting.interpret({"vote": "b", "ranking": ["A"], "reason": "x"}, ["C1", "C2", "C3"])
        self.assertEqual(iv["vote"], "C2")
        self.assertEqual(iv["ranking"], ["C2", "C1", "C3"])

    def test_contest_rules(self):
        t = {"a": {"first": 5, "borda_share": 0.6}, "b": {"first": 4, "borda_share": 0.5}, "c": {"first": 1, "borda_share": 0.1}}
        cl = {k: {"members": [k], "verify": None} for k in t}
        c = voting.contest(t, ["a", "b", "c"], cl, False)
        self.assertTrue(c["contested"])
        t["a"]["first"] = 9
        self.assertFalse(voting.contest(t, ["a", "b", "c"], cl, False)["contested"])

    def test_aimd_limiter(self):
        lim = AdaptiveLimiter(16)
        lim.on_rate_limit(pause=0)
        self.assertEqual(lim.limit, 8)
        for _ in range(8):
            lim.on_success()
        self.assertEqual(lim.limit, 9)


if __name__ == "__main__":
    unittest.main()
