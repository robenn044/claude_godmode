#!/usr/bin/env python3
"""
GODMODE engine: runs a swarm of independent Claude Code agents on one task,
then has every agent vote, and reports the solution with the most votes.

The main Claude Code session (the orchestrator, driven by SKILL.md) writes a
manifest.json into a run directory and then calls:

    python3 godmode_engine.py run    --run-dir <dir> [options]
    python3 godmode_engine.py status --run-dir <dir>
    python3 godmode_engine.py check

Pipeline
  1. SOLVE   - N agents, each a separate `claude -p` process with its own
               session/context, its own strategy + lens, read-only tools.
  2. DEDUPE  - identical answers are merged into one candidate (supporters).
  3. ROUND 1 - (only when there are more candidates than finalist slots)
               every agent resumes its own session and votes on a balanced,
               shuffled, anonymised ballot of other agents' candidates.
  4. FINAL   - every agent votes on the finalists. Most votes wins.

Everything is written to disk, so an interrupted run can be resumed by
running the same command again. Standard library only (Python 3.8+).
"""

import argparse
import concurrent.futures
import datetime as _dt
import hashlib
import json
import os
import random
import re
import shutil
import string
import subprocess
import sys
import threading
import time
import uuid

MAX_AGENTS = 10000
DEFAULT_DISALLOWED = "Edit,Write,MultiEdit,NotebookEdit,Agent,Task"
DEFAULT_TOOLS = "Read,Grep,Glob,WebSearch,WebFetch"
# Env vars of the *parent* Claude Code session that must not leak into the
# child processes (they would make every child share the parent's session).
STRIP_ENV = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID")

LABELS = list(string.ascii_uppercase) + [a + b for a in string.ascii_uppercase for b in string.ascii_uppercase]


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #

def now_iso():
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def write_text(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def extract_tag(text, tag):
    """Return the content of the LAST <tag>...</tag> block, or None."""
    matches = re.findall(r"<%s>\s*(.*?)\s*</%s>" % (tag, tag), text or "", re.S | re.I)
    return matches[-1].strip() if matches else None


def normalise(text):
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def truncate(text, limit):
    if limit and len(text) > limit:
        return text[:limit] + "\n\n[... truncated by godmode: %d more characters ...]" % (len(text) - limit)
    return text


def agent_name(i):
    return "agent-%05d" % i


def find_claude(explicit=None):
    if explicit:
        return explicit
    env = os.environ.get("GODMODE_CLAUDE_BIN")
    if env:
        return env
    found = shutil.which("claude")
    if found:
        return found
    for cand in ("~/.claude/local/claude", "~/.local/bin/claude", "~/.npm-global/bin/claude"):
        p = os.path.expanduser(cand)
        if os.path.exists(p):
            return p
    return None


# --------------------------------------------------------------------------- #
# prompt templates
# --------------------------------------------------------------------------- #

WORKER_TEMPLATE = """You are GODMODE agent #{num} of {total}: one independent solver in a large parallel swarm.
You cannot see the other agents. Think for yourself and produce the best solution you can.
Later, every agent (you included) will vote on the solutions, and the one with the most votes wins.

## TASK
{task}

## BRIEFING FROM THE ORCHESTRATOR
{brief}

## YOUR ASSIGNMENT
- Strategy: {strategy_name}
  {strategy_instructions}
- Lens: {lens}
- Variation seed: {seed} (use it to pick among equally good options, so the swarm stays diverse)

## RULES
- Read-only: inspect whatever you need, but do NOT create, edit or delete files and do not run
  commands that change state. The orchestrator applies the winning solution afterwards.
- Your solution must be self-contained: voters will see only what is inside <solution>.
- Required solution format: {solution_format}
- Keep the solution under about {max_words} words unless the task genuinely needs more.
- Judging criteria the voters will use:
{criteria}

## OUTPUT (mandatory, exactly these tags, nothing important outside them)
<summary>one sentence describing your solution</summary>
<solution>
...your complete solution...
</solution>
"""

VOTE_TEMPLATE = """GODMODE VOTING - {round_title}

The swarm has finished solving. {intro}

The task was:
{task}

Judging criteria:
{criteria}

Candidates are anonymised and shuffled. Judge strictly on merit: correctness first, then the other
criteria. Do not reward length, confidence or style for its own sake. If a candidate is truncated,
judge what is shown. {own_note}

{ballot}

Reply with exactly:
<vote>LETTER</vote>
<reason>one or two sentences</reason>
"""


def format_criteria(criteria):
    if not criteria:
        criteria = ["Correctness", "Completeness", "Clarity", "Simplicity / maintainability"]
    return "\n".join("  %d. %s" % (i + 1, c) for i, c in enumerate(criteria))


# --------------------------------------------------------------------------- #
# engine
# --------------------------------------------------------------------------- #

class Budget(Exception):
    pass


class Engine(object):
    def __init__(self, args):
        self.args = args
        self.run_dir = os.path.abspath(args.run_dir)
        self.manifest = read_json(os.path.join(self.run_dir, "manifest.json"))
        if not self.manifest:
            die("manifest.json missing or invalid in %s" % self.run_dir)
        self.validate_manifest()
        self.n = int(self.manifest["agents"])
        self.lock = threading.Lock()
        self.cost = 0.0
        self.calls = 0
        self.failures = 0
        self.log_f = open(os.path.join(self.run_dir, "engine.log"), "a", encoding="utf-8")
        self.state_path = os.path.join(self.run_dir, "state.json")
        self.state = read_json(self.state_path, {}) or {}
        if "seed" not in self.state:
            self.state["seed"] = args.seed if args.seed is not None else random.randrange(1 << 30)
            self.state["created_at"] = now_iso()
        self.cost = float(self.state.get("cost_usd", 0.0))
        self.rng = random.Random(self.state["seed"])
        self.claude = find_claude(args.claude_bin)
        if not self.claude and not args.dry_run:
            die("Could not find the `claude` CLI. Install Claude Code or pass --claude-bin.")
        self.cwd = os.path.abspath(os.path.expanduser(self.manifest.get("cwd") or os.getcwd()))
        self.progress = {}
        for sub in ("agents", "votes/round1", "votes/final"):
            os.makedirs(os.path.join(self.run_dir, sub), exist_ok=True)

    # ---------------------------------------------------------------- setup
    def validate_manifest(self):
        m = self.manifest
        try:
            n = int(m.get("agents", 0))
        except (TypeError, ValueError):
            n = 0
        if not 1 <= n <= MAX_AGENTS:
            die("manifest.agents must be an integer between 1 and %d" % MAX_AGENTS)
        if not str(m.get("task", "")).strip():
            die("manifest.task is required")
        strategies = m.get("strategies") or []
        if not strategies:
            m["strategies"] = [{"name": "Independent", "instructions": "Solve the task however you judge best."}]
        for s in m["strategies"]:
            s.setdefault("name", "Strategy")
            s.setdefault("instructions", "")
        m.setdefault("lenses", ["Balanced: weigh every criterion evenly."])
        m.setdefault("brief", "(no extra briefing)")
        m.setdefault("criteria", [])
        m.setdefault("solution_format", "Clear, complete prose/code that fully answers the task.")
        m.setdefault("max_words", 1500)

    def log(self, msg):
        line = "[%s] %s" % (now_iso(), msg)
        with self.lock:
            self.log_f.write(line + "\n")
            self.log_f.flush()
        if not self.args.quiet:
            print(line, flush=True)

    def save_state(self):
        with self.lock:
            self.state["cost_usd"] = round(self.cost, 6)
            write_json(self.state_path, self.state)

    def set_progress(self, phase, done, total, failed=0, note=""):
        with self.lock:
            self.progress = {
                "phase": phase, "done": done, "total": total, "failed": failed,
                "cost_usd": round(self.cost, 4), "calls": self.calls, "note": note,
                "updated_at": now_iso(), "pid": os.getpid(),
            }
            write_json(os.path.join(self.run_dir, "progress.json"), self.progress)

    # ------------------------------------------------------------ claude call
    def agent_dir(self, i):
        d = os.path.join(self.run_dir, "agents", agent_name(i))
        os.makedirs(d, exist_ok=True)
        return d

    def session_for(self, i):
        meta_path = os.path.join(self.agent_dir(i), "meta.json")
        meta = read_json(meta_path, {}) or {}
        if "session_id" not in meta:
            meta["session_id"] = str(uuid.uuid4())
            write_json(meta_path, meta)
        return meta["session_id"]

    def call_claude(self, prompt, session_id, resume):
        a = self.args
        if a.max_budget_usd and self.cost >= a.max_budget_usd:
            raise Budget()
        cmd = [self.claude, "-p", "--output-format", "json"]
        cmd += ["--resume", session_id] if resume else ["--session-id", session_id]
        if a.model or self.manifest.get("model"):
            cmd += ["--model", a.model or self.manifest.get("model")]
        tools = self.manifest.get("worker_tools", DEFAULT_TOOLS)
        if tools:
            cmd += ["--allowedTools", tools]
        cmd += ["--disallowedTools", self.manifest.get("disallowed_tools", DEFAULT_DISALLOWED)]
        if a.max_turns:
            cmd += ["--max-turns", str(a.max_turns)]
        if a.no_session_persistence:
            cmd += ["--no-session-persistence"]
        env = dict(os.environ)
        for k in STRIP_ENV:
            env.pop(k, None)
        env["GODMODE_CHILD"] = "1"
        proc = subprocess.run(
            cmd, input=prompt, cwd=self.cwd, env=env, capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=a.timeout,
        )
        out = proc.stdout.strip()
        data = None
        try:
            data = json.loads(out)
        except ValueError:
            for line in reversed(out.splitlines()):
                try:
                    data = json.loads(line)
                    break
                except ValueError:
                    continue
        if isinstance(data, list):  # some versions emit a list of messages
            data = next((d for d in reversed(data) if isinstance(d, dict) and d.get("type") == "result"), None)
        cost = float((data or {}).get("total_cost_usd") or (data or {}).get("cost_usd") or 0.0)
        with self.lock:
            self.cost += cost
            self.calls += 1
        if proc.returncode != 0 or data is None or data.get("is_error"):
            err = (proc.stderr or "").strip()[-800:] or out[-800:]
            raise RuntimeError("claude exited %s: %s" % (proc.returncode, err))
        return str(data.get("result") or ""), data

    def call_with_retry(self, prompt, session_id, resume, label):
        last = None
        for attempt in range(self.args.retries + 1):
            try:
                return self.call_claude(prompt, session_id, resume)
            except Budget:
                raise
            except subprocess.TimeoutExpired:
                last = "timeout after %ss" % self.args.timeout
            except Exception as e:  # noqa: BLE001
                last = str(e)
            self.log("%s attempt %d failed: %s" % (label, attempt + 1, last[:300]))
            time.sleep(min(60, 2 ** (attempt + 1)) + random.random())
        raise RuntimeError(last)

    def run_pool(self, phase, items, fn):
        """Run fn(item) over items with bounded concurrency. fn returns True if a call was made."""
        total = len(items)
        done = [0]
        failed = [0]
        self.set_progress(phase, 0, total)
        budget_hit = [False]

        def wrapped(item):
            try:
                fn(item)
            except Budget:
                budget_hit[0] = True
                return
            except Exception as e:  # noqa: BLE001
                with self.lock:
                    failed[0] += 1
                    self.failures += 1
                self.log("%s item %s FAILED: %s" % (phase, item, str(e)[:300]))
            with self.lock:
                done[0] += 1
                d, f = done[0], failed[0]
            if d == total or d % max(1, total // 50) == 0:
                self.set_progress(phase, d, total, f)
                self.save_state()
                self.log("%s progress %d/%d (failed %d, cost $%.2f)" % (phase, d, total, f, self.cost))

        with concurrent.futures.ThreadPoolExecutor(max_workers=self.args.concurrency) as ex:
            list(ex.map(wrapped, items))
        self.set_progress(phase, done[0], total, failed[0])
        self.save_state()
        if budget_hit[0]:
            self.set_progress(phase, done[0], total, failed[0], "budget exceeded")
            die("Budget of $%.2f reached during %s. Re-run with a higher --max-budget-usd to resume."
                % (self.args.max_budget_usd, phase), code=3)
        return failed[0]

    # ----------------------------------------------------------------- solve
    def assignment(self, i):
        m = self.manifest
        strategies, lenses = m["strategies"], m["lenses"]
        s = strategies[(i - 1) % len(strategies)]
        lens = lenses[((i - 1) // len(strategies)) % len(lenses)]
        return s, lens

    def worker_prompt(self, i):
        m = self.manifest
        s, lens = self.assignment(i)
        return WORKER_TEMPLATE.format(
            num=i, total=self.n, task=m["task"].strip(), brief=m["brief"].strip(),
            strategy_name=s["name"], strategy_instructions=s["instructions"], lens=lens,
            seed=hashlib.sha1(("%s-%d" % (self.state["seed"], i)).encode()).hexdigest()[:8],
            solution_format=m["solution_format"], max_words=m["max_words"],
            criteria=format_criteria(m["criteria"]),
        )

    def solve_one(self, i):
        d = self.agent_dir(i)
        if os.path.exists(os.path.join(d, "solution.md")):
            return
        prompt = self.worker_prompt(i)
        write_text(os.path.join(d, "prompt.md"), prompt)
        result, data = self.call_with_retry(prompt, self.session_for(i), False, agent_name(i) + " solve")
        solution = extract_tag(result, "solution") or result.strip()
        if not solution.strip():
            raise RuntimeError("empty solution")
        summary = extract_tag(result, "summary") or solution.strip().splitlines()[0][:200]
        meta = read_json(os.path.join(d, "meta.json"), {}) or {}
        s, lens = self.assignment(i)
        meta.update({"strategy": s["name"], "lens": lens, "summary": summary,
                     "solve_cost_usd": data.get("total_cost_usd"), "solved_at": now_iso()})
        write_json(os.path.join(d, "meta.json"), meta)
        write_text(os.path.join(d, "raw_solve.md"), result)
        write_text(os.path.join(d, "solution.md"), solution)

    # ---------------------------------------------------------------- dedupe
    def build_candidates(self):
        cands, by_hash, by_id, author_of = [], {}, {}, {}
        for i in range(1, self.n + 1):
            p = os.path.join(self.run_dir, "agents", agent_name(i), "solution.md")
            if not os.path.exists(p):
                continue
            with open(p, "r", encoding="utf-8") as f:
                text = f.read()
            h = hashlib.sha256(normalise(text).encode()).hexdigest()
            if h not in by_hash:
                meta = read_json(os.path.join(self.run_dir, "agents", agent_name(i), "meta.json"), {}) or {}
                cid = "C%05d" % (len(cands) + 1)
                by_hash[h] = cid
                by_id[cid] = {"id": cid, "authors": [], "text": text, "summary": meta.get("summary", ""),
                              "strategy": meta.get("strategy", ""), "lens": meta.get("lens", "")}
                cands.append(by_id[cid])
            cid = by_hash[h]
            by_id[cid]["authors"].append(i)
            author_of[i] = cid
        write_json(os.path.join(self.run_dir, "candidates.json"),
                   [{k: v for k, v in c.items() if k != "text"} for c in cands])
        return cands, author_of

    # ---------------------------------------------------------------- voting
    def ballots_round1(self, cands, author_of):
        ids = [c["id"] for c in cands]
        perm = ids[:]
        self.rng.shuffle(perm)
        pos = 0
        ballots = {}
        for voter in range(1, self.n + 1):
            own = author_of.get(voter)
            size = min(self.args.ballot_size, len(ids) - (1 if own else 0))
            ballot, guard = [], 0
            while len(ballot) < size and guard < 10 * len(ids):
                c = perm[pos % len(perm)]
                pos += 1
                guard += 1
                if c != own and c not in ballot:
                    ballot.append(c)
            ballots[voter] = ballot
        return ballots

    def vote_one(self, round_key, voter, ballot, cands_by_id, own_included):
        path = os.path.join(self.run_dir, "votes", round_key, agent_name(voter) + ".json")
        if os.path.exists(path):
            return
        if len(ballot) == 1:
            write_json(path, {"voter": voter, "ballot": ballot, "vote": ballot[0], "forced": True})
            return
        order = ballot[:]
        random.Random("%s-%s-%d" % (self.state["seed"], round_key, voter)).shuffle(order)
        labels = dict(zip(LABELS, order))
        limit = self.args.max_candidate_chars
        blocks = []
        for lab, cid in labels.items():
            blocks.append('<candidate id="%s">\n%s\n</candidate>' % (lab, truncate(cands_by_id[cid]["text"], limit)))
        if round_key == "final":
            title = "FINAL ROUND"
            intro = "These are the %d finalists. The candidate with the most votes becomes the swarm's answer." % len(order)
            own_note = ("Your own solution may be among them. Vote for it only if you honestly believe it is the best; "
                        "switching to a better candidate is encouraged.") if own_included else ""
        else:
            title = "QUALIFYING ROUND"
            intro = ("You are one of %d voters. Each voter sees a different random ballot of other agents' solutions "
                     "(yours is excluded). The best-scoring candidates go to the final." % self.n)
            own_note = ""
        prompt = VOTE_TEMPLATE.format(round_title=title, intro=intro, task=self.manifest["task"].strip(),
                                      criteria=format_criteria(self.manifest["criteria"]),
                                      own_note=own_note, ballot="\n\n".join(blocks))
        meta_path = os.path.join(self.agent_dir(voter), "meta.json")
        can_resume = os.path.exists(os.path.join(self.agent_dir(voter), "solution.md")) and not self.args.fresh_voters
        choice, reason, result = None, "", ""
        for attempt in range(2):
            try:
                if can_resume:
                    result, _ = self.call_with_retry(prompt, self.session_for(voter), True, agent_name(voter) + " vote")
                else:
                    result, _ = self.call_with_retry(prompt, str(uuid.uuid4()), False, agent_name(voter) + " vote(fresh)")
            except Budget:
                raise
            except Exception:  # noqa: BLE001
                if can_resume:  # session lost? fall back to a fresh voter
                    can_resume = False
                    continue
                raise
            letter = (extract_tag(result, "vote") or "").strip().strip("[]()\"'.").upper()
            if letter not in labels:
                m = re.search(r"\b([A-Z]{1,2})\b", letter)
                letter = m.group(1) if m else letter
            if letter in labels:
                choice = labels[letter]
                reason = extract_tag(result, "reason") or ""
                break
            prompt += "\n\nYour previous reply had no valid <vote>. Reply again with <vote>LETTER</vote> using one of: %s" % ", ".join(labels)
        if choice is None:
            raise RuntimeError("no valid vote parsed")
        write_json(path, {"voter": voter, "ballot": order, "vote": choice, "reason": reason[:1000],
                          "resumed": can_resume, "voted_at": now_iso()})
        meta = read_json(meta_path, {}) or {}
        meta["vote_" + round_key] = choice
        write_json(meta_path, meta)

    def tally(self, round_key):
        votes, appearances = {}, {}
        vdir = os.path.join(self.run_dir, "votes", round_key)
        for name in os.listdir(vdir):
            if not name.endswith(".json"):
                continue
            v = read_json(os.path.join(vdir, name))
            if not v:
                continue
            for cid in v.get("ballot", []):
                appearances[cid] = appearances.get(cid, 0) + 1
            votes[v["vote"]] = votes.get(v["vote"], 0) + 1
        return votes, appearances

    # ------------------------------------------------------------------- run
    def run(self):
        a = self.args
        n = self.n
        self.log("GODMODE run: %d agents, concurrency %d, run dir %s" % (n, a.concurrency, self.run_dir))
        if a.dry_run:
            return self.dry_run()

        # 1. SOLVE --------------------------------------------------------
        self.state["phase"] = "solve"
        self.save_state()
        self.run_pool("solve", list(range(1, n + 1)), self.solve_one)
        cands, author_of = self.build_candidates()
        if not cands:
            die("No agent produced a solution. See %s/engine.log" % self.run_dir, code=4)
        by_id = {c["id"]: c for c in cands}
        self.log("SOLVE done: %d solutions, %d unique candidates" % (len(author_of), len(cands)))

        round1 = None
        if len(cands) == 1:
            finalists = [cands[0]["id"]]
        elif len(cands) <= a.finalists:
            finalists = [c["id"] for c in cands]
        else:
            # 2. QUALIFYING ROUND ----------------------------------------
            self.state["phase"] = "round1"
            self.save_state()
            ballots = self.ballots_round1(cands, author_of)
            self.run_pool("round1", list(ballots.keys()),
                          lambda v: self.vote_one("round1", v, ballots[v], by_id, False))
            votes, apps = self.tally("round1")
            scored = []
            for c in cands:
                sup = len(c["authors"])
                vts, ap = votes.get(c["id"], 0), apps.get(c["id"], 0)
                # authors count as implicit votes on a ballot where they saw their own answer
                score = (vts + sup) / float(ap + sup)
                scored.append((score, vts, sup, c["id"]))
            scored.sort(key=lambda t: (-t[0], -t[1], -t[2], t[3]))
            finalists = [t[3] for t in scored[:a.finalists]]
            round1 = [{"candidate": t[3], "score": round(t[0], 4), "votes": t[1], "supporters": t[2],
                       "appearances": apps.get(t[3], 0)} for t in scored]
            self.log("ROUND 1 done. Finalists: %s" % ", ".join(finalists))

        # 3. FINAL ---------------------------------------------------------
        if len(finalists) == 1:
            final_votes = {finalists[0]: n}
            self.log("All agents converged on a single answer - unanimous.")
            unanimous = True
        else:
            unanimous = False
            self.state["phase"] = "final"
            self.save_state()
            self.run_pool("final", list(range(1, n + 1)),
                          lambda v: self.vote_one("final", v, finalists, by_id, v in author_of))
            final_votes, _ = self.tally("final")

        r1_rank = {r["candidate"]: idx for idx, r in enumerate(round1 or [])}
        ranking = sorted(finalists, key=lambda cid: (-final_votes.get(cid, 0), r1_rank.get(cid, 0),
                                                     -len(by_id[cid]["authors"]), cid))
        total_votes = sum(final_votes.values()) or 1
        winner = by_id[ranking[0]]
        top = final_votes.get(ranking[0], 0)
        tied = [cid for cid in ranking if final_votes.get(cid, 0) == top]
        self.state["phase"] = "done"
        self.save_state()
        self.set_progress("done", n, n, self.failures)
        report = {
            "task": self.manifest["task"],
            "agents": n,
            "solutions": len(author_of),
            "unique_candidates": len(cands),
            "unanimous": unanimous,
            "tie_broken": len(tied) > 1,
            "winner": {
                "candidate": winner["id"], "votes": top, "vote_share": round(top / float(total_votes), 4),
                "authors": winner["authors"], "summary": winner["summary"], "strategy": winner["strategy"],
                "lens": winner["lens"], "text": winner["text"],
            },
            "final_tally": [{"candidate": cid, "votes": final_votes.get(cid, 0),
                             "authors": by_id[cid]["authors"][:20], "supporters": len(by_id[cid]["authors"]),
                             "summary": by_id[cid]["summary"], "strategy": by_id[cid]["strategy"]}
                            for cid in ranking],
            "round1": round1[:50] if round1 else None,
            "claude_calls": self.calls,
            "failures": self.failures,
            "cost_usd": round(self.cost, 4),
            "finished_at": now_iso(),
        }
        write_json(os.path.join(self.run_dir, "report.json"), report)
        write_text(os.path.join(self.run_dir, "WINNER.md"), render_winner(report))
        self.log("WINNER %s with %d/%d votes. Report: %s" % (winner["id"], top, total_votes,
                                                           os.path.join(self.run_dir, "WINNER.md")))
        print(os.path.join(self.run_dir, "WINNER.md"))

    def dry_run(self):
        n = self.n
        s = self.manifest["strategies"]
        plan = {
            "agents": n, "strategies": len(s), "lenses": len(self.manifest["lenses"]),
            "max_claude_calls": n * 3, "concurrency": self.args.concurrency,
            "claude_bin": self.claude, "cwd": self.cwd,
        }
        write_text(os.path.join(self.run_dir, "sample_prompt_agent1.md"), self.worker_prompt(1))
        print(json.dumps(plan, indent=2))
        print("Sample worker prompt written to %s" % os.path.join(self.run_dir, "sample_prompt_agent1.md"))


def render_winner(r):
    w = r["winner"]
    lines = [
        "# GODMODE result",
        "",
        "**Task:** %s" % r["task"].strip().splitlines()[0][:300],
        "",
        "- Agents: %d (solutions: %d, unique candidates: %d)" % (r["agents"], r["solutions"], r["unique_candidates"]),
        "- Winner: **%s** with **%d votes** (%.1f%% of final votes)%s" % (
            w["candidate"], w["votes"], 100 * w["vote_share"],
            " - unanimous" if r["unanimous"] else (" - tie broken by qualifying score" if r["tie_broken"] else "")),
        "- Written by %d agent(s), strategy: %s" % (len(w["authors"]), w["strategy"] or "n/a"),
        "- Claude calls: %d, failures: %d, cost: $%.2f" % (r["claude_calls"], r["failures"], r["cost_usd"]),
        "",
        "## Final vote tally",
        "",
        "| Rank | Candidate | Votes | Written by | Strategy | Summary |",
        "|---:|---|---:|---:|---|---|",
    ]
    for idx, t in enumerate(r["final_tally"][:15], 1):
        lines.append("| %d | %s | %d | %d | %s | %s |" % (
            idx, t["candidate"], t["votes"], t["supporters"], (t["strategy"] or "").replace("|", "/"),
            (t["summary"] or "").replace("|", "/").replace("\n", " ")[:160]))
    lines += ["", "## Winning solution", "", w["text"].strip(), ""]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def die(msg, code=2):
    sys.stderr.write("godmode: %s\n" % msg)
    sys.exit(code)


def cmd_status(args):
    run_dir = os.path.abspath(args.run_dir)
    p = read_json(os.path.join(run_dir, "progress.json"))
    if not p:
        print("No progress yet in %s" % run_dir)
        return
    print(json.dumps(p, indent=2))
    if p.get("phase") == "done":
        print("Finished. Read %s" % os.path.join(run_dir, "WINNER.md"))


def cmd_check(args):
    c = find_claude(args.claude_bin)
    if not c:
        print("claude CLI: NOT FOUND")
        sys.exit(1)
    try:
        v = subprocess.run([c, "--version"], capture_output=True, text=True, timeout=30).stdout.strip()
    except Exception as e:  # noqa: BLE001
        print("claude CLI found at %s but failed to run: %s" % (c, e))
        sys.exit(1)
    print("claude CLI: %s (%s)" % (c, v))
    print("python: %s" % sys.version.split()[0])


def main(argv=None):
    ap = argparse.ArgumentParser(prog="godmode_engine", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")

    r = sub.add_parser("run", help="run (or resume) a swarm described by <run-dir>/manifest.json")
    r.add_argument("--run-dir", required=True)
    r.add_argument("--concurrency", type=int, default=int(os.environ.get("GODMODE_CONCURRENCY", 8)),
                   help="parallel claude processes (default 8)")
    r.add_argument("--model", default=os.environ.get("GODMODE_MODEL"), help="model for every agent")
    r.add_argument("--ballot-size", type=int, default=8, help="candidates per qualifying ballot (default 8)")
    r.add_argument("--finalists", type=int, default=10, help="candidates in the final (default 10)")
    r.add_argument("--max-candidate-chars", type=int, default=8000, help="truncate candidates on ballots")
    r.add_argument("--timeout", type=int, default=900, help="seconds per claude call (default 900)")
    r.add_argument("--retries", type=int, default=2)
    r.add_argument("--max-turns", type=int, default=None)
    r.add_argument("--max-budget-usd", type=float, default=None, help="stop when spend reaches this")
    r.add_argument("--fresh-voters", action="store_true",
                   help="vote in fresh contexts instead of resuming each agent's own session (cheaper)")
    r.add_argument("--no-session-persistence", action="store_true",
                   help="do not save child sessions to disk (implies --fresh-voters)")
    r.add_argument("--seed", type=int, default=None)
    r.add_argument("--claude-bin", default=None)
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--quiet", action="store_true")

    s = sub.add_parser("status", help="show progress of a run")
    s.add_argument("--run-dir", required=True)

    c = sub.add_parser("check", help="verify the claude CLI is available")
    c.add_argument("--claude-bin", default=None)

    args = ap.parse_args(argv)
    if args.cmd == "run":
        if args.no_session_persistence:
            args.fresh_voters = True
        args.concurrency = max(1, min(args.concurrency, 256))
        args.ballot_size = max(2, args.ballot_size)
        args.finalists = max(2, args.finalists)
        if os.environ.get("GODMODE_CHILD"):
            die("refusing to start a swarm from inside a godmode agent")
        Engine(args).run()
    elif args.cmd == "status":
        cmd_status(args)
    elif args.cmd == "check":
        cmd_check(args)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
