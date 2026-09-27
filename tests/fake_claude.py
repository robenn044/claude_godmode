#!/usr/bin/env python3
"""Stand-in for the `claude` CLI used by the test suite (no API calls).

Solve: agents whose number is divisible by 3 all answer "The BEST answer";
everyone else writes a unique answer. Vote: pick the candidate containing
"BEST" when present, otherwise the first candidate.
"""
import json, os, re, sys

args = sys.argv[1:]
if "--version" in args:
    print("0.0.0 (fake claude)")
    sys.exit(0)
prompt = sys.stdin.read()
store = os.environ.get("FAKE_CLAUDE_SESSIONS", "/tmp/fake_claude_sessions")
os.makedirs(store, exist_ok=True)
sid = None
if "--resume" in args:
    sid = args[args.index("--resume") + 1]
    if not os.path.exists(os.path.join(store, sid)):
        print(json.dumps({"type": "result", "is_error": True, "result": "No conversation found"}))
        sys.exit(1)
elif "--session-id" in args:
    sid = args[args.index("--session-id") + 1]
    open(os.path.join(store, sid), "w").close()

if "GODMODE VOTING" in prompt:
    cands = re.findall(r'<candidate id="([A-Z]+)">\n(.*?)\n</candidate>', prompt, re.S)
    pick = next((c for c, t in cands if "BEST" in t), cands[0][0])
    out = "<vote>%s</vote>\n<reason>fake</reason>" % pick
else:
    num = int(re.search(r"GODMODE agent #(\d+)", prompt).group(1))
    if num % 3 == 0:
        out = "<summary>best</summary>\n<solution>\nThe BEST answer\n</solution>"
    else:
        out = "<summary>agent %d</summary>\n<solution>\nanswer from agent %d\n</solution>" % (num, num)
print(json.dumps({"type": "result", "is_error": False, "result": out, "session_id": sid, "total_cost_usd": 0.001}))
