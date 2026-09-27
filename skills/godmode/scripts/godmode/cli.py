"""Command line: preflight | validate | run | status | apply | report | clean."""

import argparse
import json
import os
import platform
import signal
import sys
import time

from . import __version__
from . import manifest as mf
from .claude import Capabilities, detect_session_model, find_claude
from .util import GodmodeError, read_json, write_text
from .workspace import Workspace, repo_root


def cmd_preflight(a):
    """Never fails: its output is injected into the skill before the orchestrator starts."""
    out = ["godmode engine %s | python %s | %s" % (__version__, platform.python_version(), platform.system())]
    out.append("engine: %s" % os.path.abspath(sys.argv[0]))
    try:
        c = find_claude(a.claude_bin)
        if c:
            caps = Capabilities(c)
            out.append("claude CLI: %s (%s)" % (c, caps.version or "version unknown"))
            missing = [k for k, v in caps.summary().items() if not v]
            out.append("CLI features: %s" % ("all present" if not missing else "missing " + ", ".join(missing)))
        else:
            out.append("claude CLI: NOT FOUND -> use native mode (references/native-mode.md)")
        sm = detect_session_model()
        out.append("session model: %s (agents default to it)" % (sm or "not detected; pass --model <your model id>"))
        eff = os.environ.get("CLAUDE_EFFORT")
        if eff:
            out.append("session effort: %s" % eff)
        cwd = os.getcwd()
        root = repo_root(cwd)
        out.append("cwd: %s | git repo: %s" % (cwd, root or "no"))
        out.append("default --concurrency: 8 (adapts down automatically on rate limits)")
    except Exception as e:  # noqa: BLE001
        out.append("preflight warning: %s" % e)
    print("\n".join(out))
    return 0


def cmd_validate(a):
    m = mf.load(a.run_dir)
    print("manifest OK: %d agents, mode %s, %d strategies, %d lenses, %d judge angles, verifier: %s" % (
        m["agents"], m["mode"], len(m["strategies"]), len(m["lenses"]), len(m["judge_angles"]),
        m.get("verify_command") or "none"))
    if m["mode"] == "code":
        ws = Workspace(m["cwd"], os.path.abspath(a.run_dir), m["link_paths"], root=a.worktree_root)
        print("project: %s (%s)" % (m["cwd"], "git repo" if ws.repo else "not git: copies will be used"))
        if a.baseline and m.get("verify_command"):
            r = ws.baseline_verify(m["verify_command"], m["verify_timeout"])
            print("baseline verify on the unmodified snapshot: exit %s (%s)" % (
                r["exit_code"], "passes already" if r["passed"] else "fails, as expected for a fix task"))
            print(r["output_tail"][-1500:])
            if r["exit_code"] in (126, 127) or "command not found" in r["output_tail"]:
                raise GodmodeError("verify_command cannot run in a worktree (missing tool or dependency?). "
                                   "Fix it or add link_paths (e.g. node_modules, .venv).")
    return 0


def _swarm(a):
    from .pipeline import Swarm
    if os.environ.get("GODMODE_CHILD"):
        raise GodmodeError("refusing to start a swarm from inside a godmode agent")
    return Swarm(a.run_dir, a)


def _pid_alive(pid):
    if pid <= 0 or pid == os.getpid():
        return False
    if os.name == "nt":
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
        ctypes.windll.kernel32.CloseHandle(h)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


class RunLock(object):
    """One engine per run directory: a second `run` on a live run would duplicate work and spend."""

    def __init__(self, run_dir, force=False):
        self.path = os.path.join(run_dir, "engine.lock")
        self.force = force

    def __enter__(self):
        info = read_json(self.path)
        if info and _pid_alive(int(info.get("pid", 0))) and not self.force:
            raise GodmodeError("another engine (pid %s, started %s) is already running this run. Use "
                               "`status --wait` to follow it, or pass --force if that process is gone."
                               % (info.get("pid"), info.get("started")), 5)
        write_text(self.path, json.dumps({"pid": os.getpid(), "started": time.strftime("%Y-%m-%d %H:%M:%S")}))
        return self

    def __exit__(self, *exc):
        info = read_json(self.path)
        if info and info.get("pid") == os.getpid():
            try:
                os.remove(self.path)
            except OSError:
                pass


def cmd_run(a):
    s = _swarm(a)
    if a.dry_run:
        print(json.dumps(s.dry_run(), indent=2))
        return 0
    with RunLock(s.run_dir, a.force):
        return _run_locked(s, a)


def _run_locked(s, a):
    def on_sigint(signum, frame):
        sys.stderr.write("\ngodmode: stopping agents (state is saved; re-run to resume)...\n")
        if s.runner:
            s.runner.kill_all()
    if hasattr(signal, "SIGINT"):
        signal.signal(signal.SIGINT, on_sigint)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, on_sigint)
    if a.pilot:
        print(json.dumps(s.pilot(a.pilot), indent=2))
        return 0
    s.run()
    print(os.path.join(s.run_dir, "WINNER.md"))
    return 0


def cmd_status(a):
    """Print progress. With --wait N, block up to N seconds until the run finishes or its engine
    stops, so the orchestrator can check in periodically without a sleep loop."""
    deadline = time.time() + max(0, a.wait or 0)
    while True:
        p = read_json(os.path.join(a.run_dir, "progress.json"))
        lock = read_json(os.path.join(a.run_dir, "engine.lock"))
        running = bool(lock and _pid_alive(int(lock.get("pid", 0))))
        done = bool(p and p.get("phase") == "done")
        if done or not running or time.time() >= deadline:
            break
        time.sleep(min(5.0, max(0.1, deadline - time.time())))
    if not p:
        print("no progress yet in %s (engine running: %s)" % (a.run_dir, running))
        return 0
    p["engine_running"] = running
    print(json.dumps(p, indent=2))
    if done:
        print("finished: %s" % os.path.join(a.run_dir, "WINNER.md"))
    elif not running:
        print("the engine is not running; re-run the same `run` command to resume")
    return 0


def cmd_apply(a):
    r = read_json(os.path.join(a.run_dir, "report.json"))
    if not r:
        raise GodmodeError("no report.json yet; the run has not finished")
    pf = r["winner"].get("patch_file")
    if not pf or not os.path.exists(pf):
        raise GodmodeError("the winning solution has no patch (reason-mode run, or no file changes)")
    m = mf.load(a.run_dir)
    ws = Workspace(m["cwd"], os.path.abspath(a.run_dir), m["link_paths"])
    with open(pf, "r", encoding="utf-8") as f:
        ok, msg = ws.apply(f.read())
    print(msg)
    return 0 if ok else 5


def cmd_report(a):
    from .report import render_winner
    r = read_json(os.path.join(a.run_dir, "report.json"))
    if not r:
        raise GodmodeError("no report.json yet")
    write_text(os.path.join(a.run_dir, "WINNER.md"), render_winner(r))
    print(os.path.join(a.run_dir, "WINNER.md"))
    return 0


def cmd_clean(a):
    m = mf.load(a.run_dir)
    Workspace(m["cwd"], os.path.abspath(a.run_dir), m["link_paths"], root=a.worktree_root).clean()
    print("removed worktrees and snapshot ref for %s" % a.run_dir)
    return 0


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def build_parser():
    ap = argparse.ArgumentParser(prog="godmode_engine", description="GODMODE swarm engine %s" % __version__)
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("preflight", help="environment report (never fails)")
    p.add_argument("--claude-bin")

    for name, hlp in (("validate", "check manifest.json (add --baseline to test verify_command)"),
                      ("status", "show progress"), ("apply", "apply the winning patch to the real project"),
                      ("report", "re-render WINNER.md"), ("clean", "remove worktrees and the snapshot ref")):
        p = sub.add_parser(name, help=hlp)
        p.add_argument("--run-dir", required=True)
        p.add_argument("--worktree-root", default=None)
        if name == "validate":
            p.add_argument("--baseline", action="store_true")
        if name == "status":
            p.add_argument("--wait", type=float, default=0, help="block up to N seconds until the run finishes")

    r = sub.add_parser("run", help="run or resume the swarm described by <run-dir>/manifest.json")
    r.add_argument("--run-dir", required=True)
    r.add_argument("--concurrency", type=int, default=_env_int("GODMODE_CONCURRENCY", 8),
                   help="max parallel claude processes; adapts down on rate limits (default 8, max 256)")
    r.add_argument("--model", default=os.environ.get("GODMODE_MODEL"), help="model for solver agents")
    r.add_argument("--vote-model", default=None, help="model for votes/clustering (default: same as --model)")
    r.add_argument("--effort", default=None, choices=mf.EFFORTS, help="override worker effort")
    r.add_argument("--budget", type=float, default=None, help="stop (resumably) when total spend reaches this USD")
    r.add_argument("--agent-budget", type=float, default=None, help="max USD per single claude call")
    r.add_argument("--finalists", type=int, default=6, help="candidates in the final vote (default 6)")
    r.add_argument("--ballot-size", type=int, default=6, help="candidates per qualifying ballot (default 6)")
    r.add_argument("--max-candidate-chars", type=int, default=12000, help="truncate candidates on ballots")
    r.add_argument("--max-refine", type=int, default=1, help="max adaptive refinement rounds (default 1)")
    r.add_argument("--no-refine", action="store_true")
    r.add_argument("--refine-fraction", type=float, default=0.1, help="share of agents that refine (min 4)")
    r.add_argument("--contest-share", type=float, default=0.5, help="refine if winner share is below this")
    r.add_argument("--contest-margin", type=float, default=0.15, help="refine if top-2 margin is below this")
    r.add_argument("--vote-confidence", type=int, default=70,
                   help="a voter below this confidence (0-100) re-examines its ballot at higher effort (default 70)")
    r.add_argument("--max-wait-hours", type=float, default=6.0,
                   help="max total time to wait for plan usage-limit resets instead of failing (default 6)")
    r.add_argument("--force", action="store_true", help="take over the run lock of a dead engine")
    r.add_argument("--no-semantic-cluster", action="store_true")
    r.add_argument("--timeout", type=int, default=3600, help="seconds per solver call (default 3600)")
    r.add_argument("--vote-timeout", type=int, default=900)
    r.add_argument("--max-turns", type=int, default=None)
    r.add_argument("--retries", type=int, default=2)
    r.add_argument("--stagger", type=float, default=8.0,
                   help="seconds the rest of a phase waits for the primer call's first token (prompt cache)")
    r.add_argument("--bypass", action="store_true", help="workers use bypassPermissions instead of acceptEdits+allowlist")
    r.add_argument("--worktree-root", default=None)
    r.add_argument("--pilot", type=int, default=0, help="only solve the first N agents and report projected cost")
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--seed", type=int, default=None)
    r.add_argument("--claude-bin", default=None)
    r.add_argument("--quiet", action="store_true")
    return ap


def main(argv=None):
    a = build_parser().parse_args(argv)
    if a.cmd == "run":
        a.concurrency = max(1, min(a.concurrency, 256))
        a.ballot_size = max(2, min(a.ballot_size, 26))
        a.finalists = max(2, min(a.finalists, 26))
    handlers = {"preflight": cmd_preflight, "validate": cmd_validate, "run": cmd_run, "status": cmd_status,
                "apply": cmd_apply, "report": cmd_report, "clean": cmd_clean}
    if a.cmd not in handlers:
        build_parser().print_help()
        return 0
    try:
        return handlers[a.cmd](a)
    except GodmodeError as e:
        sys.stderr.write("godmode: %s\n" % e)
        return e.code
    except KeyboardInterrupt:
        sys.stderr.write("godmode: interrupted; re-run the same command to resume\n")
        return 130
