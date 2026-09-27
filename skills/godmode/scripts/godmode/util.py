"""Small shared helpers: atomic JSON/text IO, logging, text normalisation, hashing."""

import datetime as _dt
import hashlib
import json
import os
import re
import sys
import threading

_LOG_LOCK = threading.Lock()


def now_iso():
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, data):
    write_text(path, json.dumps(data, indent=2, ensure_ascii=False))


def write_text(path, text):
    """Write atomically so an interrupted run never leaves a half-written file."""
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    tmp = "%s.%d.%d.tmp" % (path, os.getpid(), threading.get_ident())
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def read_text(path, default=""):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return default


def normalise(text):
    """Case/whitespace/punctuation-insensitive form used for exact clustering."""
    t = (text or "").strip().lower()
    t = re.sub(r"[`*_\"'“”‘’]", "", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip(" .;:!")


def normalise_patch(patch):
    """Drop index lines and whitespace so byte-different but identical diffs match."""
    lines = []
    for line in (patch or "").splitlines():
        if line.startswith("index ") or line.startswith("@@"):
            continue
        s = line.rstrip()
        if s:
            lines.append(re.sub(r"\s+", " ", s))
    return "\n".join(lines)


def sha(text, n=16):
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:n]


def truncate(text, limit):
    text = text or ""
    if limit and len(text) > limit:
        return text[:limit] + "\n[... truncated by godmode: %d more characters ...]" % (len(text) - limit)
    return text


def tail(text, limit=3000):
    text = text or ""
    return text if len(text) <= limit else "[...]\n" + text[-limit:]


def agent_name(i):
    return "agent-%05d" % i


class Logger(object):
    def __init__(self, path=None, quiet=False):
        self.path = path
        self.quiet = quiet

    def __call__(self, msg):
        line = "[%s] %s" % (now_iso(), msg)
        with _LOG_LOCK:
            if self.path:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            if not self.quiet:
                sys.stdout.write(line + "\n")
                sys.stdout.flush()


class GodmodeError(Exception):
    """A user-facing error: printed without a traceback."""

    def __init__(self, msg, code=2):
        Exception.__init__(self, msg)
        self.code = code
