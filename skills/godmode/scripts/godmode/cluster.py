"""Group equivalent solutions so they pool their support instead of splitting the vote.

1. Exact: identical normalised answer keys, or identical normalised patches, merge.
2. Semantic (optional): a cheap, tool-less, conservative LLM call partitions distinct answer keys
   in batches of <= BATCH: pass 1 alphabetical batches, pass 2 the largest groups together (they
   decide the vote), pass 3 random batches. With <= BATCH distinct keys a single call suffices.
The representative of a cluster is its best member: passed verification, then highest
self-reported confidence, then the length closest to the cluster median.
"""

import random

from .claude import CallSpec
from .prompts import CLUSTER_SCHEMA, CLUSTER_SYSTEM, cluster_prompt
from .util import normalise, normalise_patch, sha

BATCH = 150  # answer keys per clustering call: ~5-10k tokens, well inside one careful read
MAX_PASSES = 3


class _UF(object):
    def __init__(self, keys):
        self.p = {k: k for k in keys}

    def find(self, k):
        while self.p[k] != k:
            self.p[k] = self.p[self.p[k]]
            k = self.p[k]
        return k

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[max(ra, rb)] = min(ra, rb)
            return True
        return False


def _groups(uf, keys):
    out = {}
    for k in keys:
        out.setdefault(uf.find(k), []).append(k)
    return out


def pool_hash(pool):
    return sha("|".join("%s:%s:%s" % (k, pool[k].get("answer_key"), sha(pool[k].get("patch", ""), 8))
                        for k in sorted(pool)))


def build_clusters(pool, m, runner=None, semantic=True, log=print, label="cluster"):
    """pool: {solution_key: rec}. Returns (list of clusters, number of LLM clustering calls)."""
    keys = sorted(pool)
    uf = _UF(keys)
    by_key, by_patch = {}, {}
    for k in keys:
        rec = pool[k]
        nk = normalise(rec.get("answer_key") or rec.get("summary") or rec.get("solution", "")[:200])
        if nk:
            if nk in by_key:
                uf.union(by_key[nk], k)
            else:
                by_key[nk] = k
        np_ = normalise_patch(rec.get("patch", ""))
        if np_:
            if np_ in by_patch:
                uf.union(by_patch[np_], k)
            else:
                by_patch[np_] = k

    calls = 0
    if semantic and runner is not None:
        rng = random.Random(pool_hash(pool))
        for p in range(MAX_PASSES):
            groups = _groups(uf, keys)
            if len(groups) < 2:
                break
            reps = sorted(groups)
            if p == 0:
                # pass 1: alphabetical batches, so near-identical keys tend to share a batch
                reps.sort(key=lambda r: normalise(pool[r].get("answer_key", "")))
                batches = [reps[i:i + BATCH] for i in range(0, len(reps), BATCH)]
            elif p == 1:
                # pass 2: the largest groups all together - these are the ones that decide the vote
                reps.sort(key=lambda r: (-len(groups[r]), r))
                batches = [reps[:BATCH]]
            else:
                # pass 3: random batches, so leftovers meet different neighbours
                rng.shuffle(reps)
                batches = [reps[i:i + BATCH] for i in range(0, len(reps), BATCH)]
            if p == 1 and len(reps) <= BATCH:
                break  # pass 1 already compared everything in one batch
            for bi, batch in enumerate(batches):
                if len(batch) < 2:
                    continue
                items = []
                for n, r in enumerate(batch, 1):
                    rec = pool[r]
                    txt = (rec.get("answer_key") or "").strip().replace("\n", " ")[:300]
                    summ = (rec.get("summary") or "").strip().replace("\n", " ")[:200]
                    items.append((n, txt + (" -- " + summ if summ and summ != txt else "")))
                spec = CallSpec("%s-p%d-b%d" % (label, p + 1, bi + 1), cluster_prompt(m["task"], m["answer_key_spec"], items),
                                cwd=m["run_dir"], schema=CLUSTER_SCHEMA, system_prompt=CLUSTER_SYSTEM, tools="",
                                model=m.get("vote_model") or m.get("model"), fallback_model=m.get("fallback_model"),
                                effort="low", timeout=600, judge=True)
                try:
                    res = runner.run(spec, validate=lambda d: None if d and isinstance(d.get("groups"), list) else "no groups")
                except Exception as e:  # clustering is an optimisation: never fail the run on it
                    log("semantic clustering batch failed (%s); keeping exact clusters" % str(e)[:200])
                    continue
                calls += 1
                seen = set()
                for g in res.data["groups"]:
                    idx = [int(x) for x in g if isinstance(x, (int, float)) or str(x).isdigit()]
                    idx = [x for x in idx if 1 <= x <= len(batch) and x not in seen]
                    seen.update(idx)
                    for x in idx[1:]:
                        uf.union(batch[idx[0] - 1], batch[x - 1])
            if len(reps) <= BATCH:
                break

    clusters = []
    for root, members in sorted(_groups(uf, keys).items(), key=lambda kv: (-len(kv[1]), kv[0])):
        clusters.append(_make_cluster(members, pool))
    clusters.sort(key=lambda c: (-len(c["members"]), c["members"][0]))
    for i, c in enumerate(clusters, 1):
        c["id"] = "C%04d" % i
    return clusters, calls


def _make_cluster(members, pool):
    lengths = sorted(len(pool[k].get("solution", "")) for k in members)
    median = lengths[len(lengths) // 2]

    def rank(k):
        r = pool[k]
        v = r.get("verify")
        return (0 if v is None else (0 if v.get("passed") else 1), -int(r.get("confidence") or 0),
                abs(len(r.get("solution", "")) - median), k)

    members = sorted(members)
    rep = min(members, key=rank)
    r = pool[rep]
    confs = [int(pool[k].get("confidence") or 0) for k in members]
    strategies = {}
    for k in members:
        s = pool[k].get("strategy", "")
        strategies[s] = strategies.get(s, 0) + 1
    display = r.get("solution", "")
    if r.get("patch"):
        display += "\n\n<patch>\n%s\n</patch>" % r["patch"][:20000]
    return {
        "members": members, "rep": rep, "answer_key": r.get("answer_key", ""), "summary": r.get("summary", ""),
        "display": display, "verify": r.get("verify"), "confidence_mean": round(sum(confs) / float(len(confs)), 1),
        "strategies": strategies, "has_patch": bool(r.get("patch")),
        "any_passed": any((pool[k].get("verify") or {}).get("passed") for k in members),
    }
