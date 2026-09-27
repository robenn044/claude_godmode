"""report.json (machine-readable) and WINNER.md (what the orchestrator shows the user)."""

import os

from .prompts import LABELS
from .util import now_iso, read_json


def _failures(swarm):
    solved = 0
    for i in range(1, swarm.n + 1):
        rec = read_json(os.path.join(swarm.run_dir, "agents", "agent-%05d" % i, "result.json"))
        if rec and rec.get("solve_hash") == swarm.hash:
            solved += 1
    return {"solve": swarm.n - solved}


def build_report(swarm, rounds):
    last = rounds[-1]
    by_id = {c["id"]: c for c in last["clusters"]}
    ranking, t, c = last["ranking"], last["tally"], last["contest"]
    w = by_id[ranking[0]]
    total = c["total_votes"] or swarm.n
    stats = swarm._stats()
    expected = swarm.state.get("model")
    used = stats.get("models") or {}
    # modelUsage may list a helper model next to the main one; only a missing main model is a mismatch
    mismatch = bool(expected and used and not any(expected in k or k in expected for k in used))
    cache_in = stats["cache_read_input_tokens"] + stats["cache_creation_input_tokens"] + stats["input_tokens"]
    rep_dir = "refine/r%d" % int(w["rep"].split("-")[0][1:]) if w["rep"].startswith("r") else "agents"
    rep_agent = w["rep"].rsplit("-", 2)[-2] + "-" + w["rep"].rsplit("-", 1)[-1]
    patch_path = os.path.join(swarm.run_dir, rep_dir, rep_agent, "patch.diff")

    def table(r):
        bid = {x["id"]: x for x in r["clusters"]}
        rows = []
        for cid in r["ranking"]:
            cl, s = bid[cid], r["tally"].get(cid, {})
            rows.append({"candidate": cid, "first_choice_votes": s.get("first", 0), "borda_share": s.get("borda_share"),
                         "supporters": len(cl["members"]), "answer_key": cl["answer_key"], "summary": cl["summary"],
                         "verified": None if cl.get("verify") is None else cl["verify"]["passed"],
                         "strategies": cl["strategies"], "representative": cl["rep"]})
        return rows

    dissent = []
    for v in last["votes"]:
        if v["vote"] != ranking[0] and v.get("reason"):
            legend = ", ".join("%s=%s" % (LABELS[i], c) for i, c in enumerate(v["ballot"]))
            dissent.append({"voter": v["voter"], "voted_for": v["vote"], "reason": v["reason"], "ballot": legend})
    return {
        "task": swarm.m["task"], "agents": swarm.n, "mode": swarm.m["mode"], "run_dir": swarm.run_dir,
        "winner": {
            "candidate": w["id"], "first_choice_votes": t[w["id"]]["first"], "total_votes": total,
            "vote_share": c["winner_share"] if c["total_votes"] else 1.0,
            "votes_text": "%d/%d first-choice votes%s" % (t[w["id"]]["first"], total,
                                                          ", unanimous" if last["unanimous"] else ""),
            "supporters": len(w["members"]), "representative": w["rep"], "answer_key": w["answer_key"],
            "summary": w["summary"], "solution": w["display"], "verify": w.get("verify"),
            "patch_file": patch_path if w.get("has_patch") else None, "round": last["round"],
        },
        "contest": c,
        "models": {"requested": expected, "source": swarm.state.get("model_source"),
                   "session_model": swarm.state.get("session_model"), "vote_model": swarm.state.get("vote_model"),
                   "used_by_calls": used, "mismatch": mismatch},
        "rounds": [{"round": r["round"], "solutions": r["solutions"], "clusters": len(r["clusters"]),
                    "eligible": r["eligible"], "qualifying": (r["qualify"] or [])[:30], "finalists": r["finalists"],
                    "final_tally": table(r), "contest": r["contest"], "unanimous": r["unanimous"],
                    "refiners": len(r.get("refiners") or [])} for r in rounds],
        "dissent": dissent[:20],
        "failures": _failures(swarm),
        "usage": {
            "claude_calls": stats["calls"], "failed_calls": stats["failed_calls"],
            "cost_usd": round(stats["cost_usd"], 4), "input_tokens": stats["input_tokens"],
            "output_tokens": stats["output_tokens"], "cache_read_input_tokens": stats["cache_read_input_tokens"],
            "cache_creation_input_tokens": stats["cache_creation_input_tokens"],
            "cache_hit_ratio": round(stats["cache_read_input_tokens"] / float(cache_in), 3) if cache_in else None,
            "rate_limit_events": stats["rate_limit_events"],
            "usage_limit_waits": stats.get("usage_limit_waits", 0),
        },
        "finished_at": now_iso(),
    }


def render_winner(r):
    w, c, u = r["winner"], r["contest"], r["usage"]
    md = r.get("models") or {}
    cf = c.get("confidence") or {}
    status = "unanimous" if r["rounds"][-1]["unanimous"] else (
        "CONTESTED (%s)" % "; ".join(c["reasons"]) if c["contested"] else "clear majority")
    v = w.get("verify")
    lines = [
        "# GODMODE result", "",
        "**Task:** %s" % r["task"].strip().splitlines()[0][:300], "",
        "- **Winner:** %s with **%s** (%.0f%%). Status: %s" % (w["candidate"], w["votes_text"], 100 * w["vote_share"], status),
        "- **Independently reached by:** %d agent(s)" % w["supporters"],
        "- **Verification:** %s" % ("not run" if v is None else ("PASSED" if v["passed"] else "FAILED (exit %s)" % v["exit_code"])),
        "- **Rounds:** %d%s" % (len(r["rounds"]), " (adaptive refinement ran)" if len(r["rounds"]) > 1 else ""),
        "- **Agents:** %d, solve failures: %d | Claude calls: %d | cost: $%.2f | cache hit: %s" % (
            r["agents"], r["failures"]["solve"], u["claude_calls"], u["cost_usd"],
            "n/a" if u["cache_hit_ratio"] is None else "%.0f%%" % (100 * u["cache_hit_ratio"])),
    ]
    lines.append("- **Model:** %s (%s)%s" % (
        md.get("requested") or "CLI default", md.get("source") or "?",
        "; calls used: " + ", ".join("%s x%d" % kv for kv in sorted((md.get("used_by_calls") or {}).items()))
        if md.get("used_by_calls") else ""))
    if md.get("mismatch"):
        lines.append("- **WARNING:** some calls ran on a different model than requested (see report.json models)")
    if cf.get("mean_confidence") is not None:
        lines.append("- **Voter confidence:** mean %s%%, winner's voters %s%%, re-checked votes: %d/%d" % (
            cf["mean_confidence"], cf.get("winner_voters_mean_confidence"), cf.get("rechecked_votes", 0),
            cf.get("votes", 0)))
    if w.get("patch_file"):
        lines.append("- **Patch:** `%s` (apply with the engine's `apply` command)" % w["patch_file"])
    for rd in r["rounds"]:
        lines += ["", "## Round %d vote (%d solutions -> %d clusters)" % (rd["round"], rd["solutions"], rd["clusters"]), "",
                  "| Rank | Candidate | Votes | Borda | Supporters | Verified | Answer |", "|---:|---|---:|---:|---:|---|---|"]
        for i, row in enumerate(rd["final_tally"][:12], 1):
            ver = "-" if row["verified"] is None else ("yes" if row["verified"] else "no")
            lines.append("| %d | %s | %d | %s | %d | %s | %s |" % (
                i, row["candidate"], row["first_choice_votes"], row["borda_share"], row["supporters"], ver,
                (row["answer_key"] or row["summary"] or "").replace("|", "/").replace("\n", " ")[:140]))
    if r.get("dissent"):
        lines += ["", "## Strongest dissent", ""]
        for d in r["dissent"][:5]:
            lines.append("- voted %s: %s _(ballot letters: %s)_" % (
                d["voted_for"], d["reason"].replace("\n", " ")[:300], d.get("ballot", "")))
    lines += ["", "## Winning solution", "", w["solution"].strip(), ""]
    if v is not None:
        lines += ["## Verification output (tail)", "", "```", v.get("output_tail", "")[-2000:], "```", ""]
    return "\n".join(lines)
