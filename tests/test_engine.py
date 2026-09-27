"""End-to-end tests for godmode_engine.py using tests/fake_claude.py (no API calls).

Run:  python3 -m unittest discover -s tests -v
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENGINE = os.path.join(ROOT, "skills", "godmode", "scripts", "godmode_engine.py")
FAKE = os.path.join(ROOT, "tests", "fake_claude.py")


def load(path):
    with open(path) as f:
        return json.load(f)


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="godmode-test-")
        self.env = dict(os.environ, FAKE_CLAUDE_SESSIONS=os.path.join(self.tmp, "sessions"))
        self.env.pop("GODMODE_CHILD", None)

    def make_run(self, agents, **extra):
        run_dir = os.path.join(self.tmp, "run-%d" % agents)
        os.makedirs(run_dir, exist_ok=True)
        manifest = {
            "task": "Say something useful.",
            "agents": agents,
            "brief": "Test briefing.",
            "strategies": [{"name": "S%d" % k, "instructions": "do %d" % k} for k in range(4)],
            "lenses": ["fast", "careful"],
            "criteria": ["Correctness"],
            "cwd": self.tmp,
        }
        manifest.update(extra)
        with open(os.path.join(run_dir, "manifest.json"), "w") as f:
            json.dump(manifest, f)
        return run_dir

    def run_engine(self, run_dir, *flags):
        cmd = [sys.executable, ENGINE, "run", "--run-dir", run_dir, "--claude-bin", FAKE,
               "--concurrency", "8", "--quiet", "--seed", "7"] + list(flags)
        return subprocess.run(cmd, env=self.env, capture_output=True, text=True)

    def report(self, run_dir):
        return load(os.path.join(run_dir, "report.json"))

    def test_large_swarm_with_qualifying_round(self):
        run_dir = self.make_run(40)
        p = self.run_engine(run_dir)
        self.assertEqual(p.returncode, 0, p.stderr)
        r = self.report(run_dir)
        self.assertEqual(r["solutions"], 40)
        self.assertEqual(r["unique_candidates"], 28)  # 13 identical "BEST" answers merged
        self.assertIsNotNone(r["round1"])
        self.assertIn("BEST", r["winner"]["text"])
        self.assertEqual(len(r["winner"]["authors"]), 13)
        self.assertEqual(r["winner"]["votes"], 40)  # every agent voted, all for BEST
        self.assertEqual(len(os.listdir(os.path.join(run_dir, "votes", "round1"))), 40)
        self.assertEqual(len(os.listdir(os.path.join(run_dir, "votes", "final"))), 40)
        self.assertTrue(os.path.exists(os.path.join(run_dir, "WINNER.md")))
        # every round-1 ballot excludes the voter's own candidate
        cands = {c["id"]: c for c in load(os.path.join(run_dir, "candidates.json"))}
        own = {a: cid for cid, c in cands.items() for a in c["authors"]}
        for name in os.listdir(os.path.join(run_dir, "votes", "round1")):
            v = load(os.path.join(run_dir, "votes", "round1", name))
            self.assertNotIn(own[v["voter"]], v["ballot"])
            self.assertTrue(v["resumed"])

    def test_small_swarm_goes_straight_to_final(self):
        run_dir = self.make_run(5)
        p = self.run_engine(run_dir)
        self.assertEqual(p.returncode, 0, p.stderr)
        r = self.report(run_dir)
        self.assertIsNone(r["round1"])
        self.assertIn("BEST", r["winner"]["text"])
        self.assertEqual(r["claude_calls"], 10)  # 5 solve + 5 final votes

    def test_single_agent_is_unanimous(self):
        run_dir = self.make_run(1)
        p = self.run_engine(run_dir)
        self.assertEqual(p.returncode, 0, p.stderr)
        r = self.report(run_dir)
        self.assertTrue(r["unanimous"])
        self.assertEqual(r["claude_calls"], 1)

    def test_resume_makes_no_new_calls(self):
        run_dir = self.make_run(12)
        self.assertEqual(self.run_engine(run_dir).returncode, 0)
        os.remove(os.path.join(run_dir, "report.json"))
        p = self.run_engine(run_dir)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.report(run_dir)["claude_calls"], 0)

    def test_fresh_voters(self):
        run_dir = self.make_run(6)
        p = self.run_engine(run_dir, "--fresh-voters")
        self.assertEqual(p.returncode, 0, p.stderr)
        v = load(os.path.join(run_dir, "votes", "final", "agent-00001.json"))
        self.assertFalse(v["resumed"])

    def test_rejects_too_many_agents(self):
        run_dir = self.make_run(10001)
        p = self.run_engine(run_dir)
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("between 1 and 10000", p.stderr)

    def test_dry_run_makes_no_calls(self):
        run_dir = self.make_run(10000)
        p = self.run_engine(run_dir, "--dry-run")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('"max_claude_calls": 30000', p.stdout)
        self.assertFalse(os.path.exists(os.path.join(run_dir, "agents", "agent-00001", "solution.md")))


if __name__ == "__main__":
    unittest.main()
