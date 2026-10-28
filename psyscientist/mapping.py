"""The evidence map: a tree of what the kept studies did, and what it lets you match.

Everything is driven by the protocol's [map]:

    tree         = dimensions in the order the tree branches
    match_across = the dimension whose values you want to compare (e.g. setting, population, culture)
    match_on     = the dimensions two studies must share to be comparable (e.g. construct, measure, design)
    aliases      = [map.aliases.<dimension>] "raw label" = "label", to merge synonyms after a first extraction

From the extracted features it builds:
- the tree, with the studies at each leaf;
- matches: groups of studies that agree on every match_on dimension but differ on match_across;
- gaps: for each such group, the match_across categories with no study at all;
- a matrix of match_across against each match_on dimension, where empty cells are coarse gaps;
- claim links: per protocol claim, the studies proposed to support, challenge or qualify it;
- the vocabulary: every label per dimension with its count, for calibrating aliases.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field

from .agents import NOT_REPORTED, split_values
from .store import Store


@dataclass
class EvidenceMap:
    dims: list[str]
    tree_dims: list[str]
    across: str | None
    match_on: list[str]
    labels: dict[int, dict[str, list[str]]]                 # work_id -> dimension -> labels
    titles: dict[int, str]
    tree: dict = field(default_factory=dict)
    matches: list[dict] = field(default_factory=list)
    gaps: list[dict] = field(default_factory=list)
    matrix: dict[str, dict] = field(default_factory=dict)
    vocabulary: dict[str, Counter] = field(default_factory=dict)
    claims: list[dict] = field(default_factory=list)


def latest_extractor(store: Store, run_id: int) -> str | None:
    row = store.db.execute("SELECT extractor FROM study_features WHERE run_id = ? ORDER BY created_at DESC LIMIT 1",
                           (run_id,)).fetchone()
    return row["extractor"] if row else None


def build(store: Store, run_id: int, protocol: dict, extractor: str | None = None) -> EvidenceMap | None:
    spec = protocol.get("map") or {}
    dims = [d["name"] for d in spec.get("dimensions") or []]
    extractor = extractor or latest_extractor(store, run_id)
    if not dims or not extractor:
        return None
    aliases = {dim: {k.lower(): v.lower() for k, v in table.items()}
               for dim, table in (spec.get("aliases") or {}).items()}
    labels: dict[int, dict[str, list[str]]] = defaultdict(dict)
    for r in store.db.execute("SELECT * FROM study_features WHERE run_id = ? AND extractor = ?", (run_id, extractor)):
        values = split_values(r["canonical"]) or [NOT_REPORTED]
        table = aliases.get(r["dimension"], {})
        labels[r["work_id"]][r["dimension"]] = sorted({table.get(v, v) for v in values})
    if not labels:
        return None
    titles = {r["work_id"]: r["title"] or "" for r in store.db.execute(
        f"SELECT work_id, title FROM works WHERE work_id IN ({','.join('?' * len(labels))})", tuple(labels))}

    tree_dims = [d for d in spec.get("tree") or dims[:4] if d in dims]
    across = spec.get("match_across") if spec.get("match_across") in dims else None
    match_on = [d for d in spec.get("match_on") or [] if d in dims and d != across]
    m = EvidenceMap(dims, tree_dims, across, match_on, dict(labels), titles)
    m.vocabulary = {d: Counter(v for f in labels.values() for v in f.get(d, [NOT_REPORTED])) for d in dims}
    m.tree = _tree(labels, tree_dims)
    if across and match_on:
        categories = _categories(spec, across)
        m.matches, m.gaps = _matches_and_gaps(labels, across, match_on, categories)
        m.matrix = {d: _matrix(labels, across, d) for d in match_on}
    m.claims = _claims(store, run_id, extractor, protocol)
    return m


def _tree(labels, tree_dims) -> dict:
    root: dict = {}
    for w, feats in labels.items():
        branches = [[root]]
        for d in tree_dims:
            nxt = []
            for node in branches[-1]:
                for v in feats.get(d, [NOT_REPORTED]):
                    nxt.append(node.setdefault(v, {}))
            branches.append(nxt)
        for leaf in branches[-1]:
            leaf.setdefault("__studies__", []).append(w)
    return root


def _categories(spec, across) -> list[str]:
    for d in spec.get("dimensions") or []:
        if d["name"] == across and d.get("categories"):
            return [c.lower() for c in d["categories"] if c.lower() not in ("other", NOT_REPORTED)]
    return []


def _matches_and_gaps(labels, across, match_on, categories):
    groups: dict[tuple, dict[str, set]] = defaultdict(lambda: defaultdict(set))
    for w, feats in labels.items():
        keys = [feats.get(d, [NOT_REPORTED]) for d in match_on]
        if any(NOT_REPORTED in k for k in keys):
            continue
        for combo in _product(keys):
            for a in feats.get(across, [NOT_REPORTED]):
                groups[combo][a].add(w)
    matches, gaps = [], []
    observed = sorted({a for g in groups.values() for a in g} - {NOT_REPORTED})
    universe = categories or observed
    for combo, by_across in sorted(groups.items()):
        present = {a for a in by_across if a != NOT_REPORTED}
        key = dict(zip(match_on, combo))
        if len(present) >= 2:
            n = len(set().union(*(by_across[a] for a in present)))
            matches.append({"on": key, "studies": {a: sorted(by_across[a]) for a in sorted(present)},
                            "n_studies": n})       # 1: one study compares the settings itself
        missing = [c for c in universe if c not in present]
        if present and missing:
            gaps.append({"on": key, "present": {a: len(by_across[a]) for a in sorted(present)}, "missing": missing})
    return matches, gaps


def _matrix(labels, across, dim) -> dict:
    cells: dict[str, Counter] = defaultdict(Counter)
    for feats in labels.values():
        for a in feats.get(across, [NOT_REPORTED]):
            for v in feats.get(dim, [NOT_REPORTED]):
                cells[a][v] += 1
    cols = sorted({v for row in cells.values() for v in row})
    return {"rows": sorted(cells), "cols": cols, "cells": {a: dict(c) for a, c in cells.items()}}


def _claims(store, run_id, extractor, protocol) -> list[dict]:
    out = []
    links = store.db.execute("SELECT * FROM claim_links WHERE run_id = ? AND extractor = ?",
                             (run_id, extractor)).fetchall()
    for c in protocol.get("claims") or []:
        rel = defaultdict(list)
        for link in links:
            if link["claim"] == c["id"]:
                rel[link["relation"]].append({"work_id": link["work_id"], "reason": link["reason"],
                                              "evidence": link["evidence"], "grounded": link["grounded"]})
        out.append({"id": c["id"], "claim": " ".join(c["claim"].split()), "source": c.get("source", ""),
                    "locator": c.get("locator", ""), "links": dict(rel)})
    return out


def _product(lists):
    out = [()]
    for values in lists:
        out = [prev + (v,) for prev in out for v in values]
    return out
