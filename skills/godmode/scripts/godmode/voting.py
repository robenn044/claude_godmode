"""Ballots, tallies and the decision rule.

- Qualifying ballots: balanced incomplete design over the eligible clusters (each appears about
  N*K/C times), never containing the voter's own cluster, shuffled per voter.
- Final ballots: all finalists in one of F cyclic rotations (a Latin square), so every finalist
  sits in every position equally often (position-bias control) and each rotation's prompt prefix
  is shared by ~N/F voters (prompt caching).
- Decision: most first-choice votes wins. Ties: Borda share, then verification, then supporters.
"""

import math

from .prompts import LABELS


def qualify_ballots(eligible, own_of, voters, k, rng):
    perm = list(eligible)
    rng.shuffle(perm)
    ballots, pos, n = {}, 0, len(perm)
    for v in voters:
        own = own_of.get(v)
        size = min(k, n - (1 if own in eligible else 0))
        ballot, guard = [], 0
        while len(ballot) < size and guard < 4 * n + 4:
            c = perm[pos % n]
            pos += 1
            guard += 1
            if c != own and c not in ballot:
                ballot.append(c)
        rng.shuffle(ballot)
        ballots[v] = ballot
    return ballots


def rotations(finalists):
    f = list(finalists)
    return [f[i:] + f[:i] for i in range(len(f))]


def interpret(data, order):
    """Map a judge's structured output (letters) back to cluster ids."""
    labels = dict(zip(LABELS, order))
    vote = labels.get(str(data.get("vote", "")).strip().upper()[:1])
    ranking = []
    for x in data.get("ranking") or []:
        c = labels.get(str(x).strip().upper()[:1])
        if c and c not in ranking:
            ranking.append(c)
    if vote is None and ranking:
        vote = ranking[0]
    if vote is None:
        return None
    if vote in ranking:
        ranking.remove(vote)
    ranking.insert(0, vote)
    ranking += [c for c in order if c not in ranking]
    verdicts, notes = {}, {}
    for a in data.get("assessments") or []:
        c = labels.get(str(a.get("id", "")).strip().upper()[:1]) if isinstance(a, dict) else None
        if c:
            verdicts[c] = a.get("verdict")
            if a.get("note"):
                notes[c] = str(a["note"])[:500]
    return {"vote": vote, "ranking": ranking, "verdicts": verdicts, "notes": notes,
            "reason": str(data.get("reason", ""))[:1000]}


def validate_vote(n):
    labels = set(LABELS[:n])

    def check(data):
        if not isinstance(data, dict):
            return "no structured output"
        v = str(data.get("vote", "")).strip().upper()[:1]
        if v not in labels:
            return "vote %r is not one of %s" % (data.get("vote"), sorted(labels))
        return None
    return check


def tally(votes, candidates):
    """votes: list of interpreted vote records (with 'ballot'). Returns {cid: stats}."""
    t = {c: {"first": 0, "borda": 0.0, "appearances": 0} for c in candidates}
    for v in votes:
        ballot = v["ballot"]
        n = len(ballot)
        for c in ballot:
            if c in t:
                t[c]["appearances"] += 1
        if v["vote"] in t:
            t[v["vote"]]["first"] += 1
        for pos, c in enumerate(v["ranking"]):
            if c in t and n > 1:
                t[c]["borda"] += (n - 1 - pos) / float(n - 1)
    for c, s in t.items():
        s["borda_share"] = round(s["borda"] / s["appearances"], 4) if s["appearances"] else 0.0
    return t


def qualify_score(stats, supporters):
    # Authors count as implicit first-place rankings of their own cluster (self-consistency prior).
    return (stats["borda"] + supporters) / float(stats["appearances"] + supporters)


def order_final(t, clusters_by_id):
    def key(c):
        s = t[c]
        cl = clusters_by_id[c]
        v = cl.get("verify")
        return (-s["first"], -s["borda_share"], 0 if (v is None or v.get("passed")) else 1, -len(cl["members"]), c)
    return sorted(t, key=key)


def contest(t, ranking, clusters_by_id, has_verifier, share_threshold=0.5, margin_threshold=0.15):
    total = sum(s["first"] for s in t.values())
    first = t[ranking[0]]["first"] if ranking else 0
    second = t[ranking[1]]["first"] if len(ranking) > 1 else 0
    share = first / float(total) if total else 0.0
    margin = (first - second) / float(total) if total else 0.0
    probs = [s["first"] / float(total) for s in t.values() if total and s["first"]]
    entropy = -sum(p * math.log(p, 2) for p in probs) if probs else 0.0
    reasons = []
    if total >= 3 and share < share_threshold:
        reasons.append("winner has only %.0f%% of first-choice votes" % (100 * share))
    if total >= 3 and len(ranking) > 1 and margin < margin_threshold:
        reasons.append("lead over the runner-up is only %.0f%% of votes" % (100 * margin))
    if has_verifier:
        if not any((clusters_by_id[c].get("verify") or {}).get("passed") for c in ranking):
            reasons.append("no candidate passed verification")
        elif not (clusters_by_id[ranking[0]].get("verify") or {}).get("passed"):
            reasons.append("the winner failed verification")
    return {"total_votes": total, "winner_share": round(share, 4), "margin": round(margin, 4),
            "entropy_bits": round(entropy, 3), "contested": bool(reasons), "reasons": reasons}
