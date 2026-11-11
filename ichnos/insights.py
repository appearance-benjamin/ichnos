"""Insights that emerge along the way, captured with their evidence and their history.

Working through many studies turns up things nobody asked for: a pattern that
recurs, an anomaly, two findings that contradict each other, a method problem,
an empty corner of the field, a hypothesis. They are worth keeping even when
they do not answer the protocol's question.

Sources:
- every agent task may return `insights` next to its answers (stage screen, extract or findings);
- the synthesis task reads all findings of a run together and proposes cross-study insights;
- you add your own: `python -m ichnos insight add ...`.

Each insight cites the studies (and, from synthesis, the finding rows) behind
it, with quotes that are checked against the records. Its status moves through
noted -> supported / contradicted / tested -> promoted (into the paper) or
dropped; every change is kept in insight_events, so the path from first hunch
to final argument stays visible. `write_log` renders that history as Markdown.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from .store import Store, now

KINDS = ("pattern", "anomaly", "contradiction", "gap", "method", "hypothesis", "question")
STATUSES = ("noted", "supported", "contradicted", "tested", "promoted", "dropped")
STATUS_HELP = {
    "noted": "seen once, not yet checked",
    "supported": "holds up in further studies or data",
    "contradicted": "further evidence goes against it",
    "tested": "checked deliberately; see the note for the result",
    "promoted": "taken into the argument of the paper",
    "dropped": "not pursued",
}

INSTRUCTIONS = """Insights (optional). While working through the batch, you may notice something the task
did not ask for but a researcher in this field would want to know: a pattern that recurs across
studies, an anomaly, a contradiction between studies, a methodological problem, a gap, a new
hypothesis or an open question. Add each to `insights` with `kind` (one of {kinds}), a one-sentence
`statement`, a short `rationale`, the `works` (work_ids) it rests on, and `quotes`: exact words
from those records ([{{"work_id": 1, "text": "..."}}]). Only what the records show; an empty list
is fine and usual."""


def instructions() -> str:
    return INSTRUCTIONS.format(kinds=", ".join(KINDS))


def check(items, known_works: set[int], known_findings: set[int] | None = None) -> list[str]:
    errs = []
    if items is None:
        return errs
    if not isinstance(items, list):
        return ["`insights` must be a list"]
    for i, it in enumerate(items, 1):
        if not isinstance(it, dict) or not (it.get("statement") or "").strip():
            errs.append(f"insight {i}: needs a statement")
            continue
        if it.get("kind") not in KINDS:
            errs.append(f"insight {i}: kind {it.get('kind')!r} not in {KINDS}")
        # A model may write an id as an object or a list; such an id is wrong, not a crash.
        bad = [w for w in _ids(it.get("works")) if not _known(w, known_works)]
        if bad:
            errs.append(f"insight {i}: works {bad} are not in this task")
        if known_findings is not None:
            badf = [f for f in _ids(it.get("findings")) if not _known(f, known_findings)]
            if badf:
                errs.append(f"insight {i}: finding ids {badf} do not exist in this run")
    return errs


def add(store: Store, corpus: str, author: str, stage: str, kind: str, statement: str, rationale: str | None = None,
        works: list[int] | None = None, findings: list[int] | None = None, quotes: list[dict] | None = None,
        run_id: int | None = None, call_id: int | None = None) -> int:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    support = {"works": sorted({int(w) for w in works or []}), "findings": sorted({int(f) for f in findings or []}),
               "quotes": quotes or []}
    stamp = now()
    with store.db:
        cur = store.db.execute(
            """INSERT INTO insights (corpus, run_id, author, stage, kind, statement, rationale, support, status,
                                     call_id, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'noted', ?, ?, ?)""",
            (corpus, run_id, author, stage, kind, " ".join(statement.split()), rationale,
             json.dumps(support, ensure_ascii=False), call_id, stamp, stamp))
        store.db.execute("INSERT INTO insight_events (insight_id, at, author, status, note) VALUES (?, ?, ?, ?, ?)",
                         (cur.lastrowid, stamp, author, "noted", f"captured at stage {stage}"))
    return cur.lastrowid


def set_status(store: Store, insight_id: int, status: str, author: str, note: str | None = None) -> None:
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    if store.db.execute("SELECT 1 FROM insights WHERE insight_id = ?", (insight_id,)).fetchone() is None:
        raise ValueError(f"no insight {insight_id}")
    stamp = now()
    with store.db:
        store.db.execute("UPDATE insights SET status = ?, updated_at = ? WHERE insight_id = ?",
                         (status, stamp, insight_id))
        store.db.execute("INSERT INTO insight_events (insight_id, at, author, status, note) VALUES (?, ?, ?, ?, ?)",
                         (insight_id, stamp, author, status, note))


def store_from_agent(store: Store, corpus: str, run_id: int, author: str, stage: str, items, texts: dict[int, str],
                     call_id: int, grounded) -> tuple[int, int, int]:
    """Store an agent's insights; returns (stored, quotes checked, quotes found)."""
    stored = checked = found = 0
    for it in items or []:
        quotes = []
        for q in it.get("quotes") or []:
            w = _int(q.get("work_id"))
            g = grounded(q.get("text"), texts.get(w, "")) if w in texts else False
            checked += g is not None
            found += bool(g)
            quotes.append({"work_id": w, "text": q.get("text"), "grounded": g})
        add(store, corpus, author, stage, it["kind"], it["statement"], it.get("rationale"),
            [_int(w) for w in it.get("works") or []], [_int(f) for f in it.get("findings") or []], quotes,
            run_id, call_id)
        stored += 1
    return stored, checked, found


NUMBER = re.compile(r"(?<![\w.])(\d+(?:[.,]\d+)?)(%?)")


def numbers(text: str) -> list[str]:
    """Numbers worth checking: decimals, percentages and integers of 10 or more (not '3 studies')."""
    out = []
    for m in NUMBER.finditer(text or ""):
        value, pct = m.group(1).replace(",", "."), m.group(2)
        if "." in value or pct or float(value) >= 10:
            out.append(value + pct)
    return sorted(set(out))


def verify_numbers(store: Store, insight_id: int) -> list[str]:
    """Flag numbers in an insight that none of its cited findings (or their studies' titles) contain.

    A number the cited evidence does not show may still be true, but the trail from claim to source is
    broken, and that is what this pipeline must never hide. The result is stored in support["unverified_numbers"].
    """
    from . import numgate
    it = store.db.execute("SELECT * FROM insights WHERE insight_id = ?", (insight_id,)).fetchone()
    support = json.loads(it["support"] or "{}")
    ids = support.get("findings") or []
    works = support.get("works") or []
    if ids:
        rows_ = store.db.execute(f"SELECT f.finding, f.effect, f.for_whom, f.evidence, w.title, w.year "
                                 f"FROM findings f JOIN works w ON w.work_id = f.work_id "
                                 f"WHERE f.finding_id IN ({','.join('?' * len(ids))})", tuple(ids)).fetchall()
    elif works:       # an insight that names studies but no findings is checked against those studies' findings
        rows_ = store.db.execute(f"SELECT f.finding, f.effect, f.for_whom, f.evidence, w.title, w.year "
                                 f"FROM findings f JOIN works w ON w.work_id = f.work_id "
                                 f"WHERE f.status != 'superseded' AND f.work_id IN ({','.join('?' * len(works))})",
                                 tuple(works)).fetchall()
    else:
        rows_ = []
    items = [numgate.finding_text(r["finding"], r["effect"], r["for_whom"], r["evidence"]) + f" {r['title']} {r['year']}"
             for r in rows_]
    haystack = " ".join(items)
    have = set(numbers(haystack)) | {n.rstrip("%") for n in numbers(haystack)}
    # Study and finding ids used as citations, and corpus counts ("19 studies"), are not data to check.
    run_ids = store.db.execute("SELECT count(DISTINCT work_id), count(*) FROM findings WHERE run_id = ?",
                               (it["run_id"],)).fetchone() if it["run_id"] else (None, None)
    work_ids = {str(r[0]) for r in store.db.execute("SELECT DISTINCT work_id FROM findings WHERE run_id = ?",
                                                     (it["run_id"],))} if it["run_id"] else set()
    have |= work_ids | {str(i) for i in ids} | {str(w) for w in support.get("works", [])} \
        | {str(x) for x in run_ids if x is not None}
    text = strip_references(f"{it['statement']} {it['rationale'] or ''}")
    missing = [n for n in numbers(text) if n not in have and n.rstrip("%") not in have and not _same_number(n, have)]
    support["unverified_numbers"] = missing
    flags = numgate.gate(text, items, exempt=have - set(numbers(haystack)))
    flags.pop("missing", None)
    support["number_flags"] = numgate.describe(flags)
    with store.db:
        store.db.execute("UPDATE insights SET support = ? WHERE insight_id = ?",
                         (json.dumps(support, ensure_ascii=False), insight_id))
    return missing


REFERENCE = re.compile(
    r"\b(?:insights?|questions?|contradictions?|patterns?|gaps?|hypothes[ie]s|methods?|anomal(?:y|ies)|findings?|"
    r"stud(?:y|ies)|rows?)\s+(?:ids?\s+)?#?\d+(?:\s*(?:,|and|&|or)\s*#?\d+)*", re.I)


def strip_references(text: str) -> str:
    """Remove 'existing insight 18', 'contradictions 33 and 41', 'study 51': ids, not data."""
    return REFERENCE.sub(" ", text or "")


def _same_number(n: str, have: set[str]) -> bool:
    """0.38 and .38, 5.40 and 5.4 count as the same number."""
    try:
        x = float(n.rstrip("%"))
    except ValueError:
        return False
    for h in have:
        try:
            if abs(float(h.rstrip("%")) - x) < 1e-9:
                return True
        except ValueError:
            continue
    return False


def rows(store: Store, corpus: str):
    return store.db.execute("SELECT * FROM insights WHERE corpus = ? ORDER BY insight_id", (corpus,)).fetchall()


def events(store: Store, insight_id: int):
    return store.db.execute("SELECT * FROM insight_events WHERE insight_id = ? ORDER BY event_id",
                            (insight_id,)).fetchall()


def write_log(store: Store, corpus: str, path: Path) -> Path:
    """The insight log: everything noticed so far, by status, each with its evidence and history."""
    items = rows(store, corpus)
    out = [f"# Insight log: corpus {corpus}", "",
           "Generated from the evidence store; do not edit by hand. Change a status with "
           "`python -m ichnos insight status <id> <status> --note \"...\"`.", ""]
    if not items:
        out.append("No insights captured yet.")
    titles = {}
    works = {w for it in items for w in json.loads(it["support"] or "{}").get("works", [])}
    if works:
        titles = {r["work_id"]: r["title"] or "" for r in store.db.execute(
            f"SELECT work_id, title FROM works WHERE work_id IN ({','.join('?' * len(works))})", tuple(works))}
    for status in STATUSES:
        group = [it for it in items if it["status"] == status]
        if not group:
            continue
        out += [f"## {status.capitalize()} ({len(group)}): {STATUS_HELP[status]}", ""]
        for it in group:
            sup = json.loads(it["support"] or "{}")
            out.append(f"### #{it['insight_id']} {it['kind']}: {it['statement']}")
            out.append("")
            out.append(f"*{it['author']}, stage {it['stage']}, {it['created_at'][:16].replace('T', ' ')} UTC"
                       + (f", run {it['run_id']}" if it["run_id"] else "") + "*")
            out.append("")
            if it["rationale"]:
                out.append(it["rationale"])
                out.append("")
            for w in sup.get("works", []):
                out.append(f"- study #{w}: {titles.get(w, '')[:110]}")
            if sup.get("findings"):
                out.append(f"- finding rows: {', '.join('#' + str(f) for f in sup['findings'])}")
            for q in sup.get("quotes", []):
                mark = "" if q.get("grounded") else " (quote not found in the record)"
                out.append(f"- #{q.get('work_id')}: “{q.get('text')}”{mark}")
            if sup.get("unverified_numbers"):
                out.append(f"- ⚠ numbers not found in the cited findings: {', '.join(sup['unverified_numbers'])}")
            history = events(store, it["insight_id"])
            if len(history) > 1:
                out.append("- history: " + "; ".join(f"{e['at'][:10]} {e['status']} ({e['author']})"
                                                      + (f": {e['note']}" if e["note"] else "") for e in history))
            out.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return path


def _int(x):
    try:
        return int(x)
    except (TypeError, ValueError):
        return x


def _ids(value) -> list:
    """The ids of an insight's `works` or `findings`: a list as given, a single value as a list of one."""
    if value is None or value == "":
        return []
    return value if isinstance(value, list) else [value]


def _known(x, known: set[int]) -> bool:
    v = _int(x)
    return isinstance(v, int) and not isinstance(v, bool) and v in known
