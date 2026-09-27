#!/usr/bin/env python3
"""Stand-in for the `claude` CLI used by the test suite (no API calls).

Roles are recognised from the prompt:
- solver:   "You are agent #N" (or "<refinement_round>")
- judge:    "<candidates>" ballots   -> votes for the candidate containing the best marker
- cluster:  "<items>"                -> groups keys by a synonym table

Behaviour knobs (env):
  FAKE_SCENARIO   consensus (default) | split | code
  FAKE_NO_SCHEMA  1 -> --json-schema unsupported (help omits it); 2 -> advertised but rejected at runtime
  FAKE_RATE_LIMIT_EVERY  N -> every Nth call emits an api_retry rate-limit event
  FAKE_LOG        path -> append one JSON line per call (argv flags, cwd, prompt head)
"""
import hashlib
import json
import os
import re
import sys
import time

FLAGS = ["--print", "--output-format", "--verbose", "--json-schema", "--append-system-prompt", "--effort",
         "--permission-mode", "--permission-prompts", "--allowedTools", "--disallowedTools", "--tools", "--model",
         "--fallback-model", "--no-session-persistence", "--strict-mcp-config", "--system-prompt", "--add-dir",
         "--exclude-dynamic-system-prompt-sections", "--max-budget-usd", "--max-turns", "--disable-slash-commands"]
args = sys.argv[1:]
no_schema = os.environ.get("FAKE_NO_SCHEMA") in ("1", "2")
if "--help" in args:  # FAKE_NO_SCHEMA=2 advertises the flag but rejects it at runtime
    hide = os.environ.get("FAKE_NO_SCHEMA") == "1"
    print("Usage: claude [options]\n" + "\n".join(f for f in FLAGS if not (hide and f == "--json-schema")))
    sys.exit(0)
if "--version" in args:
    print("9.9.9 (fake claude)")
    sys.exit(0)
if no_schema and "--json-schema" in args:
    sys.stderr.write("error: unknown option '--json-schema'\n")
    sys.exit(1)

prompt = sys.stdin.read()
system = ""
if "--append-system-prompt-file" in args:
    system = open(args[args.index("--append-system-prompt-file") + 1], encoding="utf-8").read()
scenario = os.environ.get("FAKE_SCENARIO", "consensus")
log = os.environ.get("FAKE_LOG")
counter_file = os.environ.get("FAKE_COUNTER")
call_no = 0
if counter_file:
    try:
        with open(counter_file, "a+") as f:
            f.write("x")
            f.seek(0)
            call_no = len(f.read())
    except OSError:
        pass
if log:
    with open(log, "a") as f:
        f.write(json.dumps({"flags": [a for a in args if a.startswith("--")], "cwd": os.getcwd(),
                            "prompt_head": prompt[:120], "system_sha": hashlib.sha256(system.encode()).hexdigest()}) + "\n")


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


emit({"type": "system", "subtype": "init"})
every = int(os.environ.get("FAKE_RATE_LIMIT_EVERY", "0") or 0)
if every and call_no and call_no % every == 0:
    emit({"type": "system", "subtype": "api_retry", "error_status": 429, "error": "rate_limit"})
emit({"type": "stream_event", "event": {"type": "message_start"}})

SYN = {"forty-two": "42", "42.0": "42", "the answer is 42": "42"}


def solver(num, refine):
    if scenario == "code":
        # agents divisible by 3 write the correct fix; others write a wrong one
        good = num % 3 == 0 or refine
        with open("calc.py", "w") as f:
            f.write("def add(a, b):\n    return a + b\n" if good else "def add(a, b):\n    return a - b\n")
        key = "fix add to use +" if good else "change add to use -"
        return {"summary": key, "answer_key": key, "solution": "Changed calc.add. " + key, "confidence": 80}
    if scenario == "split":
        # three camps of similar size, nobody convinces a majority; refiners converge on BEST
        if refine:
            return {"summary": "refined", "answer_key": "BEST refined", "solution": "BEST refined answer", "confidence": 90}
        camp = ["alpha", "beta", "gamma"][num % 3]
        return {"summary": camp, "answer_key": camp, "solution": "answer %s" % camp, "confidence": 50}
    # consensus: every 3rd agent says 42 in varied wording, the rest are unique wrong answers
    if num % 3 == 0:
        key = ["42", "forty-two", "the answer is 42"][(num // 3) % 3]
        return {"summary": "best", "answer_key": key, "solution": "BEST: the answer is 42", "confidence": 90}
    return {"summary": "agent %d" % num, "answer_key": "guess %d" % num, "solution": "answer from agent %d" % num,
            "confidence": 30}


def judge():
    cands = re.findall(r'<candidate id="([A-Z])">\n(.*?)\n</candidate>', prompt, re.S)
    ids = [c for c, _ in cands]

    def score(t):
        if "PASSED" in t:
            return 3
        if "BEST" in t:
            return 2
        if scenario == "split":
            return 0
        return 1 if "a + b" in t else 0
    if scenario == "split" and not any("BEST" in t for _, t in cands):
        # split electorate: vote by the judging angle so no camp wins a majority
        m = re.search(r"Judging angle: (\w+)", prompt)
        pref = {"A1": "alpha", "A2": "beta", "A3": "gamma"}.get(m.group(1) if m else "", "alpha")
        pick = next((c for c, t in cands if pref in t), ids[0])
    else:
        pick = max(cands, key=lambda ct: score(ct[1]))[0]
    ranking = [pick] + [c for c in ids if c != pick]
    return {"assessments": [{"id": c, "verdict": "correct" if c == pick else "flawed", "note": "fake note %s" % c}
                            for c in ids], "ranking": ranking, "vote": pick, "reason": "fake judge"}


def cluster():
    items = re.findall(r"^(\d+)\. (.*?)(?: -- .*)?$", prompt.split("<items>")[1].split("</items>")[0], re.M)
    groups = {}
    for n, text in items:
        k = SYN.get(text.strip().lower(), text.strip().lower())
        groups.setdefault(k, []).append(int(n))
    return {"groups": list(groups.values())}


if "<items>" in prompt:
    out = cluster()
elif "<candidates>" in prompt:
    out = judge()
else:
    m = re.search(r"You are agent #(\d+)", prompt)
    out = solver(int(m.group(1)) if m else 1, "<refinement_round>" in prompt)
    if "GODMODE SWARM PROTOCOL" not in system and "GODMODE SWARM PROTOCOL" not in prompt:
        out["summary"] += " (NO BRIEF)"
if os.environ.get("FAKE_SLEEP"):
    time.sleep(float(os.environ["FAKE_SLEEP"]))

result = {"type": "result", "subtype": "success", "is_error": False, "num_turns": 1, "total_cost_usd": 0.001,
          "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 100,
                    "cache_creation_input_tokens": 20}}
if no_schema:
    result["result"] = "```json\n%s\n```" % json.dumps(out)
else:
    result["result"] = json.dumps(out)
    result["structured_output"] = out
emit(result)
