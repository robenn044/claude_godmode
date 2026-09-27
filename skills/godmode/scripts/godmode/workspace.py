"""Isolated per-agent workspaces for code tasks.

A snapshot commit of the user's *current* tree (including uncommitted and untracked, non-ignored
files) is built with a temporary GIT_INDEX_FILE, so the user's index, HEAD and working tree are
never touched. Every agent gets its own detached `git worktree` of that snapshot; only agents
that are currently running have a worktree on disk. Non-git projects are copied instead.
"""

import os
import shutil
import subprocess
import tempfile

from .claude import run_shell
from .util import GodmodeError, read_json, sha, write_json, write_text

GIT_ID = {"GIT_AUTHOR_NAME": "godmode", "GIT_AUTHOR_EMAIL": "godmode@localhost",
          "GIT_COMMITTER_NAME": "godmode", "GIT_COMMITTER_EMAIL": "godmode@localhost"}


def git(args, cwd, env_extra=None, check=True):
    env = dict(os.environ)
    env.update(env_extra or {})
    p = subprocess.run(["git"] + args, cwd=cwd, env=env, capture_output=True)
    out = p.stdout.decode("utf-8", "replace")
    if check and p.returncode != 0:
        raise GodmodeError("git %s failed: %s" % (" ".join(args[:3]), p.stderr.decode("utf-8", "replace").strip()[-500:]))
    return p.returncode, out


def repo_root(path):
    try:
        code, out = git(["rev-parse", "--show-toplevel"], path, check=False)
    except OSError:
        return None
    return os.path.abspath(out.strip()) if code == 0 and out.strip() else None


def ensure_excluded(project, pattern=".godmode/"):
    """Keep run files out of `git status` without editing the user's .gitignore."""
    root = repo_root(project)
    if not root:
        return
    code, common = git(["rev-parse", "--git-common-dir"], root, check=False)
    if code != 0:
        return
    common = common.strip()
    common = common if os.path.isabs(common) else os.path.join(root, common)
    rel = os.path.relpath(os.path.join(project, pattern), root).replace(os.sep, "/")
    path = os.path.join(common, "info", "exclude")
    try:
        existing = open(path, "r", encoding="utf-8").read() if os.path.exists(path) else ""
        if rel not in existing.split("\n") and "/" + rel not in existing.split("\n"):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(("" if existing.endswith("\n") or not existing else "\n") + "/" + rel + "\n")
    except OSError:
        pass


def _symlink(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    try:
        os.symlink(src, dst, target_is_directory=os.path.isdir(src))
        return True
    except (OSError, NotImplementedError):
        if os.name == "nt" and os.path.isdir(src):  # junctions need no admin rights
            return subprocess.run(["cmd", "/c", "mklink", "/J", dst, src], capture_output=True).returncode == 0
        return False


class Workspace(object):
    def __init__(self, project, run_dir, link_paths=(), root=None, log=print):
        self.project = os.path.abspath(project)
        self.run_dir = run_dir
        self.link_paths = [p.strip("/\\") for p in link_paths]
        self.log = log
        self.run_id = "%s-%s" % (os.path.basename(run_dir.rstrip("/\\")), sha(run_dir, 6))
        self.root = root or os.path.join(tempfile.gettempdir(), "godmode", self.run_id)
        self.repo = repo_root(self.project)
        self.rel = os.path.relpath(self.project, self.repo) if self.repo else "."
        self.state_path = os.path.join(run_dir, "workspace.json")
        self.state = read_json(self.state_path, {}) or {}

    # ---------------------------------------------------------------- snapshot
    def _excludes(self):
        ex = []
        base = "" if self.rel == "." else self.rel.replace(os.sep, "/") + "/"
        for p in [".godmode"] + self.link_paths:
            ex.append(":(exclude)%s%s" % (base, p))
        return ex

    def snapshot(self):
        if self.state.get("snapshot"):
            return self.state["snapshot"]
        if not self.repo:
            self.state["snapshot"] = "copy"
            write_json(self.state_path, self.state)
            return "copy"
        idx = os.path.join(self.run_dir, ".snapshot-index")
        if os.path.exists(idx):
            os.remove(idx)
        env = dict(GIT_ID, GIT_INDEX_FILE=idx)
        code, head = git(["rev-parse", "--verify", "-q", "HEAD"], self.repo, check=False)
        head = head.strip() if code == 0 else None
        if head:
            git(["read-tree", head], self.repo, env)
        git(["add", "-A", "--", "."] + self._excludes(), self.repo, env)
        _, tree = git(["write-tree"], self.repo, env)
        args = ["commit-tree", tree.strip(), "-m", "godmode snapshot of the working tree"]
        if head:
            args[2:2] = ["-p", head]
        _, commit = git(args, self.repo, env)
        commit = commit.strip()
        git(["update-ref", "refs/godmode/%s" % self.run_id, commit], self.repo)
        os.remove(idx)
        self.state.update({"snapshot": commit, "head": head, "repo": self.repo, "rel": self.rel, "root": self.root})
        write_json(self.state_path, self.state)
        return commit

    # --------------------------------------------------------------- lifecycle
    def create(self, name):
        """Returns (workspace_path, agent_cwd)."""
        snap = self.snapshot()
        path = os.path.join(self.root, name)
        if os.path.exists(path):
            self.remove(path)
        os.makedirs(self.root, exist_ok=True)
        if snap == "copy":
            ignore = shutil.ignore_patterns(".godmode", *self.link_paths)
            shutil.copytree(self.project, path, symlinks=True, ignore=ignore)
            git(["init", "-q"], path)
            git(["add", "-A"], path, GIT_ID)
            git(["commit", "-q", "--allow-empty", "--no-verify", "-m", "godmode baseline"], path, GIT_ID)
            cwd = path
        else:
            git(["worktree", "add", "--detach", "-q", path, snap], self.repo)
            cwd = os.path.normpath(os.path.join(path, self.rel))
        for lp in self.link_paths:
            src, dst = os.path.join(self.project, lp), os.path.join(cwd, lp)
            if os.path.exists(src) and not os.path.lexists(dst):
                if not _symlink(src, dst):
                    self.log("warning: could not link %s into %s" % (lp, name))
        return path, cwd

    def capture(self, path):
        """The agent's full change as a binary-safe patch against the snapshot."""
        base = "HEAD" if self.state.get("snapshot") == "copy" else self.state["snapshot"]
        rel = "." if self.state.get("snapshot") == "copy" else self.rel
        prefix = "" if rel in (".", "") else rel.replace(os.sep, "/") + "/"
        ex = [":(exclude)%s%s" % (prefix, lp.replace(os.sep, "/")) for lp in self.link_paths]
        git(["add", "-A", "--", "."] + ex, path, GIT_ID)
        _, diff = git(["diff", "--cached", "--binary", base], path)
        return diff

    def verify(self, cwd, command, timeout):
        code, out = run_shell(command, cwd, timeout)
        return {"passed": code == 0, "exit_code": code, "output_tail": out[-3000:]}

    def remove(self, path):
        for lp in self.link_paths:  # never follow links into the real project
            for base in (path, os.path.join(path, self.rel)):
                p = os.path.join(base, lp)
                if os.path.islink(p):
                    try:
                        os.unlink(p)
                    except OSError:
                        pass
        if self.repo and self.state.get("snapshot") not in (None, "copy"):
            code, _ = git(["worktree", "remove", "--force", "--force", path], self.repo, check=False)
            if code == 0:
                return
        shutil.rmtree(path, ignore_errors=True)
        if self.repo:
            git(["worktree", "prune"], self.repo, check=False)

    def baseline_verify(self, command, timeout):
        path, cwd = self.create("baseline-check")
        try:
            return self.verify(cwd, command, timeout)
        finally:
            self.remove(path)

    def apply(self, patch_text):
        """Apply a winning patch to the user's real project. Returns (ok, message)."""
        patch_path = os.path.join(self.run_dir, "winner.patch")
        write_text(patch_path, patch_text)
        if not patch_text.strip():
            return False, "the winning solution has no file changes"
        target = self.repo or self.project
        for extra in ([], ["--3way"]):
            code, _ = git(["apply", "--check"] + extra + [patch_path], target, check=False)
            if code == 0:
                code, out = git(["apply", "--whitespace=nowarn"] + extra + [patch_path], target, check=False)
                if code == 0:
                    return True, "applied %s%s" % (patch_path, " (3-way)" if extra else "")
        _, err = git(["apply", "--check", "-v", patch_path], target, check=False)
        return False, "patch does not apply cleanly (the tree changed since the snapshot?). Patch: %s" % patch_path

    def clean(self):
        if os.path.isdir(self.root):
            for name in os.listdir(self.root):
                self.remove(os.path.join(self.root, name))
            shutil.rmtree(self.root, ignore_errors=True)
        if self.repo:
            git(["worktree", "prune"], self.repo, check=False)
            git(["update-ref", "-d", "refs/godmode/%s" % self.run_id], self.repo, check=False)
