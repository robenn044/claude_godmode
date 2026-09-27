"""Runs headless `claude -p` processes robustly.

- Finds the CLI (Claude Desktop exposes the running binary as CLAUDE_CODE_EXECPATH).
- Detects which flags the installed version supports, and drops a flag at runtime if the
  CLI rejects it ("unknown option"), so one engine works across Claude Code versions.
- Streams `--output-format stream-json` to (a) release the rest of a phase as soon as the
  primer call's first token arrives, so siblings hit the prompt cache the primer wrote
  (the API only serves a cache entry after the first response begins), and (b) notice
  API retries caused by rate limits / overload and back off.
- Adaptive (AIMD) concurrency, a global dollar budget, and process-tree kill on timeout
  or Ctrl-C.
"""

import json
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import threading
import time

from .util import GodmodeError

# Env of the *parent* Claude Code session that must not leak into children.
STRIP_ENV = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID")
# Tools auto-approved for workers ("agents may do everything"). acceptEdits + an explicit
# allowlist grants full autonomy and, unlike bypassPermissions, also works when running as root.
FULL_ACCESS_TOOLS = "Bash Edit Write Read Grep Glob NotebookEdit WebSearch WebFetch TodoWrite"


def find_claude(explicit=None):
    candidates = [explicit, os.environ.get("GODMODE_CLAUDE_BIN"), os.environ.get("CLAUDE_CODE_EXECPATH"),
                  shutil.which("claude")]
    for p in ("~/.claude/local/claude", "~/.local/bin/claude", "~/.npm-global/bin/claude",
              "/opt/homebrew/bin/claude", "/usr/local/bin/claude"):
        candidates.append(os.path.expanduser(p))
    for c in candidates:
        if c and os.path.isfile(c) and (os.access(c, os.X_OK) or c.endswith(".py")):
            return os.path.abspath(c)
    return None


def claude_argv(claude):
    """A .py path (e.g. a test double) is run with this interpreter, which also works on Windows."""
    return [sys.executable, claude] if claude.endswith(".py") else [claude]


def child_env():
    env = dict(os.environ)
    for k in STRIP_ENV:
        env.pop(k, None)
    env["GODMODE_CHILD"] = "1"
    return env


def popen_kwargs():
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200)}
    return {"start_new_session": True}


def kill_tree(proc):
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, timeout=30)
        else:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        try:
            proc.kill()
        except OSError:
            pass


def run_shell(command, cwd, timeout, env=None):
    """Run a shell command with a process-tree kill on timeout. Returns (exit_code, output)."""
    proc = subprocess.Popen(command, shell=True, cwd=cwd, env=env or child_env(), stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, **popen_kwargs())
    try:
        out, _ = proc.communicate(timeout=timeout)
        return proc.returncode, out.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        out = b""
        try:
            out, _ = proc.communicate(timeout=10)
        except (subprocess.TimeoutExpired, ValueError):
            pass
        return 124, out.decode("utf-8", "replace") + "\n[godmode: timed out after %ss]" % timeout


class Capabilities(object):
    """Which CLI flags the installed claude supports."""

    HIDDEN = {"--append-system-prompt": ["--append-system-prompt-file"]}

    def __init__(self, claude):
        self.claude = claude
        self.version = ""
        self.flags = set()
        self.disabled = set()
        self.lock = threading.Lock()
        try:
            env = child_env()
            h = subprocess.run(claude_argv(claude) + ["--help"], capture_output=True, text=True, timeout=60, env=env)
            self.flags = set(re.findall(r"(--[a-z][a-z0-9-]+)", h.stdout + h.stderr))
            v = subprocess.run(claude_argv(claude) + ["--version"], capture_output=True, text=True, timeout=60, env=env)
            self.version = v.stdout.strip()
        except (OSError, subprocess.SubprocessError):
            pass
        for flag, extra in self.HIDDEN.items():
            if flag in self.flags:
                self.flags.update(extra)

    def has(self, flag):
        return flag in self.flags and flag not in self.disabled

    def disable(self, flag):
        with self.lock:
            self.disabled.add(flag)

    def summary(self):
        keys = ["--json-schema", "--append-system-prompt-file", "--exclude-dynamic-system-prompt-sections",
                "--effort", "--permission-prompts", "--no-session-persistence", "--max-budget-usd",
                "--fallback-model", "--strict-mcp-config", "--tools"]
        return {k: self.has(k) for k in keys}


class AdaptiveLimiter(object):
    """AIMD concurrency: halve on rate-limit signals, +1 after a streak of successes."""

    def __init__(self, maximum):
        self.maximum = max(1, maximum)
        self.limit = self.maximum
        self.active = 0
        self.streak = 0
        self.pause_until = 0.0
        self.cond = threading.Condition()

    def acquire(self, stop):
        with self.cond:
            while self.active >= self.limit or time.time() < self.pause_until:
                if stop.is_set():
                    raise Stopped()
                self.cond.wait(0.5)
            self.active += 1

    def release(self):
        with self.cond:
            self.active -= 1
            self.cond.notify_all()

    def on_rate_limit(self, pause=10.0):
        with self.cond:
            self.limit = max(1, self.limit // 2)
            self.streak = 0
            self.pause_until = max(self.pause_until, time.time() + pause)

    def on_success(self):
        with self.cond:
            self.streak += 1
            if self.limit < self.maximum and self.streak >= self.limit:
                self.limit += 1
                self.streak = 0
                self.cond.notify_all()


class PrimerGate(object):
    """First call of a phase runs alone until its first token (cache written), then the rest go."""

    def __init__(self, stagger):
        self.stagger = stagger
        self.event = threading.Event()
        self.claimed = False
        self.lock = threading.Lock()
        if stagger <= 0:
            self.event.set()

    def enter(self):
        with self.lock:
            if self.claimed or self.event.is_set():
                return False
            self.claimed = True
            return True

    def wait(self):
        self.event.wait(self.stagger)

    def open(self):
        self.event.set()


class Stopped(Exception):
    pass


class BudgetExceeded(Exception):
    pass


class CallError(Exception):
    def __init__(self, msg, retryable=True, rate_limited=False):
        Exception.__init__(self, msg)
        self.retryable = retryable
        self.rate_limited = rate_limited


class _UnknownOption(Exception):
    def __init__(self, flag):
        Exception.__init__(self, flag)
        self.flag = flag


class CallSpec(object):
    def __init__(self, label, prompt, cwd, schema=None, system_prompt=None, append_file=None, tools=None,
                 allowed_tools=None, disallowed_tools=None, full_access=False, model=None, fallback_model=None,
                 effort=None, add_dirs=(), strict_mcp=True, exclude_dynamic=True, max_turns=None,
                 max_budget=None, timeout=1800, judge=False):
        self.__dict__.update(locals())
        del self.__dict__["self"]


class CallResult(object):
    def __init__(self, data, text, cost, usage, num_turns, duration):
        self.data, self.text, self.cost, self.usage = data, text, cost, usage
        self.num_turns, self.duration = num_turns, duration


def parse_output(text, schema):
    """Recover a dict from free text when structured output is unavailable."""
    if not text:
        return None
    t = text.strip()
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", t, re.S)
    for chunk in ([m.group(1)] if m else []) + [t, t[t.find("{"): t.rfind("}") + 1] if "{" in t else ""]:
        if not chunk:
            continue
        try:
            d = json.loads(chunk)
            if isinstance(d, dict):
                return d
        except ValueError:
            pass
    if schema:
        out = {}
        for key, spec in schema.get("properties", {}).items():
            vals = re.findall(r"<%s>\s*(.*?)\s*</%s>" % (key, key), t, re.S | re.I)
            if vals:
                v = vals[-1].strip()
                if spec.get("type") == "integer":
                    n = re.search(r"-?\d+", v)
                    v = int(n.group(0)) if n else 0
                elif spec.get("type") == "array":
                    v = [x.strip() for x in re.split(r"[,\s>]+", v) if x.strip()]
                out[key] = v
        if out:
            return out
    return None


class ClaudeRunner(object):
    def __init__(self, claude, concurrency=8, retries=2, budget=None, log=print, bypass=False):
        if not claude:
            raise GodmodeError("Could not find the `claude` CLI. Install Claude Code or pass --claude-bin.")
        self.claude = claude
        self.caps = Capabilities(claude)
        self.limiter = AdaptiveLimiter(concurrency)
        self.retries = retries
        self.budget = budget
        self.log = log
        self.bypass = bypass
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.procs = set()
        self.stats = {"calls": 0, "failed_calls": 0, "cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0,
                      "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "rate_limit_events": 0}

    # ------------------------------------------------------------------ public
    def add_prior_cost(self, cost):
        with self.lock:
            self.stats["cost_usd"] += cost or 0.0

    def kill_all(self):
        self.stop.set()
        with self.lock:
            procs = list(self.procs)
        for p in procs:
            kill_tree(p)

    def run(self, spec, gate=None, validate=None):
        """Run one call with retries. `validate(data)` returns an error string or None."""
        is_primer = gate.enter() if gate else False
        if gate and not is_primer:
            gate.wait()
        on_first = gate.open if is_primer else None
        try:
            return self._run_with_retries(spec, on_first, validate)
        finally:
            if is_primer:
                gate.open()

    # --------------------------------------------------------------- internals
    def _run_with_retries(self, spec, on_first, validate):
        attempt, downgrades, last = 0, 0, "unknown error"
        while attempt <= self.retries:
            if self.stop.is_set():
                raise Stopped()
            if self.budget is not None and self.stats["cost_usd"] >= self.budget:
                raise BudgetExceeded()
            self.limiter.acquire(self.stop)
            try:
                res = self._run_once(spec, on_first)
                err = validate(res.data) if validate else None
                if err:
                    raise CallError("invalid output: %s" % err)
                self.limiter.on_success()
                return res
            except _UnknownOption as e:
                downgrades += 1
                self.caps.disable(e.flag)
                self.log("CLI rejected %s; retrying without it" % e.flag)
                if downgrades > 8:
                    raise CallError("too many unsupported flags", retryable=False)
                continue
            except CallError as e:
                last = str(e)
                with self.lock:
                    self.stats["failed_calls"] += 1
                if e.rate_limited:
                    self.limiter.on_rate_limit(pause=15 * (attempt + 1))
                if not e.retryable:
                    raise
            finally:
                self.limiter.release()
            attempt += 1
            if attempt <= self.retries:
                self.log("%s attempt %d failed: %s" % (spec.label, attempt, last[:300]))
                time.sleep(min(90, 2 ** attempt * (5 if "rate" in last.lower() else 1)) + random.random())
        raise CallError(last)

    def _build_cmd(self, spec, prompt):
        c = self.caps
        cmd = claude_argv(self.claude) + ["-p", "--output-format", "stream-json", "--verbose"]
        if c.has("--no-session-persistence"):
            cmd.append("--no-session-persistence")
        if spec.model:
            cmd += ["--model", spec.model]
        if spec.fallback_model and c.has("--fallback-model"):
            cmd += ["--fallback-model", spec.fallback_model]
        if spec.effort and c.has("--effort"):
            cmd += ["--effort", spec.effort]
        if spec.schema is not None and c.has("--json-schema"):
            cmd += ["--json-schema", json.dumps(spec.schema, separators=(",", ":"))]
        elif spec.schema is not None:
            prompt += ("\n\nReturn ONLY a JSON object (no prose) matching this JSON Schema:\n"
                       + json.dumps(spec.schema))
        if spec.system_prompt:
            cmd += ["--system-prompt", spec.system_prompt]
        if spec.append_file:
            if c.has("--append-system-prompt-file"):
                cmd += ["--append-system-prompt-file", spec.append_file]
            else:  # still a byte-identical prefix, now at the start of the user message
                with open(spec.append_file, "r", encoding="utf-8") as f:
                    prompt = f.read() + "\n\n" + prompt
        if spec.tools is not None and c.has("--tools"):
            cmd += ["--tools", spec.tools]
        if spec.full_access:
            if self.bypass and c.has("--permission-mode"):
                cmd += ["--permission-mode", "bypassPermissions"]
            else:
                if c.has("--permission-mode"):
                    cmd += ["--permission-mode", "acceptEdits"]
                cmd += ["--allowedTools", spec.allowed_tools or FULL_ACCESS_TOOLS]
        elif spec.allowed_tools:
            cmd += ["--allowedTools", spec.allowed_tools]
        if c.has("--permission-prompts"):
            cmd += ["--permission-prompts", "none"]
        if spec.disallowed_tools:
            cmd += ["--disallowedTools", spec.disallowed_tools]
        if spec.strict_mcp and c.has("--strict-mcp-config"):
            cmd.append("--strict-mcp-config")
        if spec.exclude_dynamic and not spec.system_prompt and c.has("--exclude-dynamic-system-prompt-sections"):
            cmd.append("--exclude-dynamic-system-prompt-sections")
        if spec.judge and c.has("--disable-slash-commands"):
            cmd.append("--disable-slash-commands")
        for d in spec.add_dirs or ():
            cmd += ["--add-dir", d]
        if spec.max_turns:
            cmd += ["--max-turns", str(spec.max_turns)]
        if spec.max_budget and c.has("--max-budget-usd"):
            cmd += ["--max-budget-usd", "%.4f" % spec.max_budget]
        return cmd, prompt

    def _run_once(self, spec, on_first):
        cmd, prompt = self._build_cmd(spec, spec.prompt)
        started = time.time()
        proc = subprocess.Popen(cmd, cwd=spec.cwd, env=child_env(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, **popen_kwargs())
        with self.lock:
            self.procs.add(proc)
        box = {"result": None, "first": False, "tail": [], "retry_rl": False}
        err_chunks = []

        def read_stdout():
            for raw in iter(proc.stdout.readline, b""):
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    box["tail"] = (box["tail"] + [line])[-20:]
                    continue
                t = ev.get("type")
                if t == "result":
                    box["result"] = ev
                elif t in ("stream_event", "assistant") and not box["first"]:
                    box["first"] = True
                    if on_first:
                        on_first()
                elif t == "system" and ev.get("subtype") == "api_retry":
                    blob = json.dumps(ev).lower()
                    if any(k in blob for k in ("429", "529", "rate_limit", "overloaded")) and not box["retry_rl"]:
                        box["retry_rl"] = True
                        with self.lock:
                            self.stats["rate_limit_events"] += 1
                        self.limiter.on_rate_limit()

        def read_stderr():
            for raw in iter(proc.stderr.readline, b""):
                err_chunks.append(raw.decode("utf-8", "replace"))

        threads = [threading.Thread(target=read_stdout, daemon=True), threading.Thread(target=read_stderr, daemon=True)]
        for th in threads:
            th.start()
        try:
            try:
                proc.stdin.write(prompt.encode("utf-8"))
                proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass
            try:
                proc.wait(timeout=spec.timeout)
            except subprocess.TimeoutExpired:
                kill_tree(proc)
                raise CallError("timeout after %ss" % spec.timeout)
            for th in threads:
                th.join(10)
        finally:
            with self.lock:
                self.procs.discard(proc)
            if proc.poll() is None:
                kill_tree(proc)
        if self.stop.is_set():
            raise Stopped()
        stderr = "".join(err_chunks)
        m = re.search(r"unknown option '(--[a-z0-9-]+)'", stderr)
        if m:
            raise _UnknownOption(m.group(1))
        if "cannot be used with root" in stderr and self.bypass:
            self.bypass = False
            raise CallError("bypassPermissions refused (root); falling back to acceptEdits + allowlist")
        res = box["result"]
        if res is None:
            raise CallError("no result (exit %s): %s" % (proc.returncode, (stderr.strip() or "\n".join(box["tail"]))[-600:]))
        cost = float(res.get("total_cost_usd") or 0.0)
        usage = res.get("usage") or {}
        with self.lock:
            s = self.stats
            s["calls"] += 1
            s["cost_usd"] += cost
            for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
                s[k] += int(usage.get(k) or 0)
        if res.get("is_error") or res.get("subtype") not in (None, "success"):
            status = res.get("api_error_status")
            rl = status in (429, 529, "429", "529")
            budget = res.get("subtype") == "error_max_budget_usd"
            raise CallError("%s %s %s" % (res.get("subtype"), status or "", "; ".join(res.get("errors") or [])[:400]),
                            retryable=not budget, rate_limited=rl)
        data = res.get("structured_output")
        text = str(res.get("result") or "")
        if not isinstance(data, dict):
            data = parse_output(text, spec.schema) if spec.schema is not None else None
        return CallResult(data, text, cost, usage, res.get("num_turns"), time.time() - started)
