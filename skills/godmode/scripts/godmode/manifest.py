"""manifest.json: the orchestrator's plan for a swarm. See references/manifest.md."""

import json
import os

from . import MAX_AGENTS
from .util import GodmodeError, read_json, sha

EFFORTS = ("low", "medium", "high", "xhigh", "max")
MODES = ("code", "reason")

DEFAULTS = {
    "version": 2,
    "mode": "reason",
    "brief": "",
    "dead_ends": [],
    "strategies": [],
    "lenses": ["Balanced: weigh every criterion evenly."],
    "judge_angles": [
        "Correctness tracer: check each claim/step and whether the result is actually right.",
        "Edge-case hunter: look for inputs, cases or conditions where the solution breaks.",
        "Spec compliance: does it do exactly what the task asks - nothing missing, nothing extra?",
        "Simplicity auditor: prefer the simplest solution that is fully correct and maintainable.",
    ],
    "criteria": ["Correctness", "Completeness", "Robustness", "Clarity and simplicity"],
    "answer_key_spec": "The final answer or the core approach in one line (<= 200 chars), phrased canonically "
                       "so that two agents with the same answer write the same key.",
    "solution_format": "A complete, self-contained solution: the answer or the change first, then the reasoning "
                       "or evidence that it is correct.",
    "max_words": 1500,
    "verify_command": None,
    "verify_timeout": 900,
    "link_paths": [],
    "worker_tools": None,
    "worker_subagents": False,
    "worker_mcp": False,
    "voter_tools": "",
    "worker_effort": "high",
    "voter_effort": "medium",
    "model": None,
    "vote_model": None,
    "fallback_model": None,
}


def load(run_dir):
    path = os.path.join(run_dir, "manifest.json")
    raw = read_json(path)
    if raw is None:
        if not os.path.exists(path):
            raise GodmodeError("%s not found. Write the manifest first (see references/manifest.md)." % path)
        try:
            with open(path, "r", encoding="utf-8") as f:
                json.load(f)
        except ValueError as e:
            raise GodmodeError("manifest.json is not valid JSON: %s" % e)
    m = dict(DEFAULTS)
    m.update(raw)
    errors = validate(m)
    if errors:
        raise GodmodeError("manifest.json has %d problem(s):\n  - %s" % (len(errors), "\n  - ".join(errors)))
    normalise(m, run_dir)
    return m


def validate(m):
    e = []
    try:
        n = int(m.get("agents"))
        if not 1 <= n <= MAX_AGENTS:
            e.append("agents must be between 1 and %d (got %s)" % (MAX_AGENTS, n))
    except (TypeError, ValueError):
        e.append("agents must be an integer between 1 and %d" % MAX_AGENTS)
    if not str(m.get("task") or "").strip():
        e.append("task is required (the user's prompt, verbatim)")
    if m.get("mode") not in MODES:
        e.append("mode must be one of %s" % (MODES,))
    strategies = m.get("strategies") or []
    if not isinstance(strategies, list):
        e.append("strategies must be a list of {name, workflow}")
    else:
        for i, s in enumerate(strategies):
            if not isinstance(s, dict) or not s.get("name") or not (s.get("workflow") or s.get("instructions")):
                e.append("strategies[%d] needs a non-empty name and workflow" % i)
    for key in ("lenses", "judge_angles", "criteria", "dead_ends", "link_paths"):
        v = m.get(key)
        if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            e.append("%s must be a list of strings" % key)
    for key in ("lenses", "judge_angles", "criteria"):
        if isinstance(m.get(key), list) and not m[key]:
            e.append("%s must not be empty" % key)
    for key in ("worker_effort", "voter_effort"):
        if m.get(key) not in EFFORTS:
            e.append("%s must be one of %s" % (key, EFFORTS))
    if m.get("mode") == "code" and m.get("verify_command") is not None and not str(m["verify_command"]).strip():
        e.append("verify_command must be a non-empty shell command or null")
    for p in m.get("link_paths") or []:
        if os.path.isabs(p) or ".." in p.replace("\\", "/").split("/"):
            e.append("link_paths entries must be relative paths inside the project: %r" % p)
    try:
        if int(m.get("max_words")) < 50:
            e.append("max_words must be >= 50")
    except (TypeError, ValueError):
        e.append("max_words must be an integer")
    cwd = m.get("cwd")
    if cwd and not os.path.isdir(os.path.expanduser(cwd)):
        e.append("cwd does not exist: %s" % cwd)
    return e


def normalise(m, run_dir):
    m["agents"] = int(m["agents"])
    m["max_words"] = int(m["max_words"])
    m["cwd"] = os.path.abspath(os.path.expanduser(m.get("cwd") or os.getcwd()))
    if not m["strategies"]:
        m["strategies"] = [{"name": "Independent", "workflow": "Solve the task however you judge best."}]
    for s in m["strategies"]:
        s["workflow"] = s.get("workflow") or s.get("instructions")
    m["run_dir"] = os.path.abspath(run_dir)


def solve_hash(m):
    """Anything that changes what a solver sees. Solutions made under another hash are redone."""
    keys = ("task", "brief", "dead_ends", "strategies", "lenses", "criteria", "answer_key_spec",
            "solution_format", "max_words", "mode", "verify_command", "agents")
    return sha(json.dumps({k: m.get(k) for k in keys}, sort_keys=True))
