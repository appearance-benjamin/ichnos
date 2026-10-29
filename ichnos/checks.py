"""Human checks of the findings behind a manuscript's headline claims.

An editor's condition for accepting an AI-assisted review: a person checks every finding that the
Abstract, the principal findings, the implications and the conclusion rest on, against its passage or
the full text. Each check is kept (who, when, against what, the outcome, a note and any correction);
the latest one per finding counts. The Declaration of AI use is generated from these checks, so it
says what a person actually did, and the next revision must follow every correction.

Which parts are headline parts is set by `[paper] headline` in the protocol: section names (Abstract,
Conclusion) or paragraph headings within a section (Principal findings, Implications ...).
"""

from __future__ import annotations

import json
import re
import tomllib

from . import writer
from .store import Store, now

STATUSES = {
    "confirmed": "the source says what the finding says",
    "corrected": "the finding is wrong or incomplete; the correction says what the source says",
}
AGAINST = {"passage": "the passage shown", "full text": "the full text of the study"}
HEADLINE = ["Abstract", "Principal findings", "Implications", "Conclusion"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS finding_checks (
    check_id    INTEGER PRIMARY KEY,
    finding_id  INTEGER NOT NULL REFERENCES findings(finding_id),
    paper_id    INTEGER,                         -- the version whose headline claims were being checked
    checker     TEXT NOT NULL,                   -- human:<name>
    status      TEXT NOT NULL,                   -- confirmed, corrected, or unchecked (a check withdrawn)
    against     TEXT,                            -- passage or full text
    correction  TEXT,                            -- what the source says, when corrected
    note        TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS finding_checks_finding ON finding_checks(finding_id);
"""


def ensure(store: Store) -> None:
    store.db.executescript(SCHEMA)


def current_protocol(store: Store, corpus: str) -> dict:
    """The corpus's protocol as last registered: declarations and headline parts the author edits later."""
    row = store.db.execute("SELECT content FROM protocols WHERE corpus = ? ORDER BY first_used DESC, rowid DESC "
                           "LIMIT 1", (corpus,)).fetchone()
    return tomllib.loads(row["content"]) if row else {}


# a focused paper's headline parts follow its kind (the review headline does not fit a proposal)
AIM_HEADLINES = {"effectiveness": ["Abstract", "Main findings", "Conclusion"],
                 "proposal": ["Abstract", "Proposal", "Conclusion"],
                 "solutions": ["Abstract", "Recommendations", "Conclusion"],
                 "focus": ["Abstract", "Main findings", "Conclusion"]}


def headline_names(protocol: dict, paper_row=None) -> list[str]:
    if paper_row is not None:
        kind = json.loads(paper_row["method"] or "{}").get("aim_kind")
        if kind in AIM_HEADLINES:
            return list(AIM_HEADLINES[kind])
    return list((protocol.get("paper") or {}).get("headline") or HEADLINE)


def headline_sentences(content: dict, names: list[str]):
    """(part, sentence) for every sentence in a headline part: a whole section with that name, or the
    paragraphs under a heading that contains the name, up to the next heading."""
    wanted = [n.lower() for n in names]
    for p in content.get("abstract") or []:
        if "abstract" in wanted:
            for s in p.get("sentences") or []:
                yield "Abstract", s
    for sec in content.get("sections") or []:
        whole = sec.get("name", "").lower() in wanted
        current = None
        for p in sec.get("paragraphs") or []:
            if p.get("heading"):
                current = next((n for n in names if n.lower() in p["heading"].lower()), None)
            part = sec["name"] if whole else current
            if part:
                for s in p.get("sentences") or []:
                    yield part, s


def latest(store: Store, finding_ids) -> dict[int, dict]:
    ensure(store)
    ids = list(finding_ids)
    if not ids:
        return {}
    out = {}
    for r in store.db.execute(f"SELECT * FROM finding_checks WHERE finding_id IN ({','.join('?' * len(ids))}) "
                              "ORDER BY check_id", tuple(ids)):
        out[r["finding_id"]] = dict(r)
    return {k: v for k, v in out.items() if v["status"] != "unchecked"}


def latest_paper(store: Store, run_id: int):
    from .paper import ensure as ensure_papers
    ensure_papers(store)
    return store.db.execute("SELECT * FROM papers WHERE run_id = ? ORDER BY paper_id DESC LIMIT 1",
                            (run_id,)).fetchone()


def headline_findings(store: Store, paper_row, protocol: dict) -> list[dict]:
    """Every finding a headline sentence cites, in order of first use, with its passage, its study, the
    sentences that use it and its latest check."""
    content = json.loads(paper_row["content"])
    uses: dict[int, list] = {}
    for part, s in headline_sentences(content, headline_names(protocol, paper_row)):
        for c in s.get("cites") or []:
            if str(c).lstrip("-").isdigit():
                uses.setdefault(int(c), []).append({"part": part, "text": s.get("text", "")})
    return finding_items(store, paper_row, uses)


def sample_findings(store: Store, paper_row, protocol: dict, n: int, seed: int, skip=()) -> list[dict]:
    """A random sample of the findings the paper rests on (its aim's findings, else all), stratified by the findings'
    first matrix axis (e.g. the candidate rule) in proportion to its size, without the findings in `skip`; each
    labelled with the stratum it was drawn from."""
    import math
    import random
    from . import aim as aim_mod, tables
    method = json.loads(paper_row["method"] or "{}")
    p = aim_mod.with_aim(protocol, method.get("aim_id")) if method.get("aim_id") else protocol
    rows = aim_mod.aim_rows(store, paper_row["run_id"], p) if p.get("aim") else tables.finding_rows(
        store, paper_row["run_id"])
    axis = ((p.get("findings") or {}).get("matrix") or ["candidate_rule"])[0]
    focused = (p.get("focus") or {}).get("rules") or []
    strata: dict[str, list[int]] = {}
    for r in rows:
        if r["finding_id"] not in set(skip):
            vals = [v.strip() for v in str(json.loads(r["fields"] or "{}").get(axis) or "other").split(";")]
            key = next((v for v in vals if v in focused), vals[0])      # a focus paper's strata are its rules
            strata.setdefault(key, []).append(r["finding_id"])
    total, rnd, uses = sum(len(v) for v in strata.values()), random.Random(seed), {}
    for key, ids in sorted(strata.items()):
        for fid in rnd.sample(sorted(ids), min(len(ids), math.ceil(n * len(ids) / total)) if total else 0):
            uses[fid] = [{"part": "Random sample", "text": f"one of the {len(ids)} findings on "
                                                           f"{key.replace('_', ' ')} that the paper counts"}]
    return finding_items(store, paper_row, uses)


def finding_items(store: Store, paper_row, uses: dict[int, list]) -> list[dict]:
    """The findings in `uses` as items to check: passage, study, link, codes, the sentences that use them, the
    latest check."""
    if not uses:
        return []
    ids = list(uses)
    rows = {r["finding_id"]: r for r in store.db.execute(
        f"""SELECT f.*, w.title, w.year, w.authors, w.doi, w.oa_url, t.status AS ft_status, t.url AS ft_url
            FROM findings f JOIN works w ON w.work_id = f.work_id
            LEFT JOIN fulltexts t ON t.work_id = f.work_id
            WHERE f.finding_id IN ({','.join('?' * len(ids))})""", tuple(ids))}
    checked = latest(store, ids)
    labels = (json.loads(paper_row["method"] or "{}") or {}).get("cite_labels") or {}
    out = []
    for fid in ids:
        r = rows.get(fid)
        if r is None:
            continue
        link = f"https://doi.org/{r['doi']}" if r["doi"] else (r["ft_url"] or r["oa_url"] or "")
        out.append({"id": fid, "work": r["work_id"],
                    "cite": labels.get(str(r["work_id"])) or writer.cite_label(r["authors"], r["year"]),
                    "title": r["title"], "link": link, "fulltext": r["ft_status"] == "parsed",
                    "finding": r["finding"], "effect": r["effect"], "for_whom": r["for_whom"],
                    "fields": json.loads(r["fields"] or "{}"), "evidence": r["evidence"], "location": r["location"],
                    "source": r["source"] or "not found", "uses": uses[fid], "check": checked.get(fid)})
    return out


def record(store: Store, finding_id: int, checker: str, status: str, against: str | None = None,
           note: str | None = None, correction: str | None = None, paper_id: int | None = None) -> int:
    """Save a check; status "" withdraws the last one (kept in the history as `unchecked`)."""
    ensure(store)
    status = (status or "").strip() or "unchecked"
    if status not in STATUSES and status != "unchecked":
        raise ValueError(f"status must be one of {', '.join(STATUSES)}")
    if not checker.startswith("human:") or checker == "human:":
        raise ValueError("a check needs the name of the person who made it")
    if not store.db.execute("SELECT 1 FROM findings WHERE finding_id = ?", (finding_id,)).fetchone():
        raise ValueError(f"no finding {finding_id}")
    against = (against or "").strip() or None
    if status != "unchecked" and against not in AGAINST:
        raise ValueError("say what you checked it against: the passage or the full text")
    correction = (correction or "").strip() or None
    if status == "corrected" and not correction:
        raise ValueError("write what the source actually says in the correction field")
    with store.db:
        cur = store.db.execute(
            "INSERT INTO finding_checks (finding_id, paper_id, checker, status, against, correction, note, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (finding_id, paper_id, checker, status, against if status != "unchecked" else None,
             clean_text(correction) if status == "corrected" else None, clean_text(note), now()))
    return cur.lastrowid


def summary(items: list[dict]) -> dict:
    done = [i["check"] for i in items if i.get("check")]
    dates = sorted(c["created_at"][:10] for c in done)
    return {"headline": len(items), "checked": len(done),
            "confirmed": sum(c["status"] == "confirmed" for c in done),
            "corrected": sum(c["status"] == "corrected" for c in done),
            "full_text": sum(c["against"] == "full text" for c in done),
            "checkers": sorted({c["checker"].split(":", 1)[1] for c in done}),
            "first": dates[0] if dates else None, "last": dates[-1] if dates else None}


def insight_review(store: Store, corpus: str, paper_row=None) -> dict:
    """How many insights were logged and how many a person reviewed; with `paper_row`, only those logged
    before that manuscript was written, the set its Method counts."""
    sql, args = "SELECT insight_id, status FROM insights WHERE corpus = ?", [corpus]
    if paper_row is not None and paper_row["call_id"]:
        sql += " AND (call_id < ? OR (call_id IS NULL AND created_at < ?))"
        args += [paper_row["call_id"], paper_row["created_at"]]
    rows = store.db.execute(sql, args).fetchall()
    who = sorted({r[0].split(":", 1)[1] for r in store.db.execute(
        "SELECT DISTINCT e.author FROM insight_events e JOIN insights i ON i.insight_id = e.insight_id "
        "WHERE i.corpus = ? AND e.author LIKE 'human:%'", (corpus,))})
    return {"total": len(rows), "reviewed": sum(r["status"] != "noted" for r in rows), "reviewers": who}


def _names(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _people(handles: list[str], protocol: dict) -> list[str]:
    """Reviewer names as the manuscript should give them: `[paper.names]` maps a name typed in the app
    (e.g. sam) to the full name; otherwise the typed name, capitalised."""
    full = (protocol.get("paper") or {}).get("names") or {}
    return [full.get(h) or h[:1].upper() + h[1:] for h in handles]


def declaration(store: Store, paper_row, protocol: dict, totals: dict) -> str:
    """The Declaration of AI use, from what the store records: no claim a person did not make."""
    method = json.loads(paper_row["method"])
    items = headline_findings(store, paper_row, protocol)
    s = summary(items)
    ins = insight_review(store, paper_row["corpus"], paper_row)
    claims = totals.get("sentences", 0) - totals.get("frame", 0)
    focused = bool(method.get("aim"))
    parts = ["This manuscript was produced with Ichnos, an AI-assisted review pipeline. Language-model "
             "agents screened records, charted studies, extracted findings, drafted the text and reviewed it."]
    if totals and focused:      # for journal readers: what was checked and how, without the pipeline's counts
        parts.append("Every statement in the text cites the findings, method facts, tables or background sources it "
                     "rests on; the citations and every number were checked in code against those sources, and each "
                     "statement was audited against them by a separate agent.")
    elif totals:
        parts.append(f"Each of the {claims} claim sentences cites the findings, method facts, tables or background "
                     "sources it rests on" + ((" (the other sentence is a signpost that claims nothing)"
                                               if totals["frame"] == 1 else
                                               f" (the other {totals['frame']} sentences are signposts that claim "
                                               "nothing)") if totals.get("frame") else "")
                     + "; the citations and numbers were checked in code and each sentence was audited against what "
                       "it cites by a separate agent.")
    # records a person screened outside a blind calibration sample (that sample is reported on its own below)
    humans = store.db.execute("SELECT count(DISTINCT s.work_id) FROM screening s WHERE s.run_id = ? AND s.screener LIKE "
                              "'human:%' AND s.work_id NOT IN (SELECT work_id FROM samples WHERE run_id = s.run_id AND "
                              "purpose = 'calibration_screening')", (paper_row["run_id"],)).fetchone()[0]
    # who wrote the protocol is the protocol's to say ([paper] protocol_by); the default is the human author
    by = " ".join(str((protocol.get("paper") or {}).get("protocol_by") or "").split())
    if by:
        parts.append(f"The protocol was {by}" + (f"; the human author screened {humans} records." if humans else "."))
    else:
        parts.append("The human author set the protocol" + (f" and screened {humans} records." if humans else "."))
    heads = _names([n.lower() if n != "Abstract" else n for n in headline_names(protocol, paper_row)])
    if not s["headline"]:
        pass
    elif not s["checked"]:
        parts.append(f"No person has yet checked the {s['headline']} extracted "
                     f"{'finding' if s['headline'] == 1 else 'findings'} behind the headline claims "
                     f"({heads}) against their sources.")
    else:
        against = ("against the full texts" if s["full_text"] == s["checked"] else
                   "against the cited passages" if not s["full_text"] else
                   f"against the full texts ({s['full_text']}) or the cited passages ({s['checked'] - s['full_text']})")
        when = s["first"] if s["first"] == s["last"] else f"{s['first']} to {s['last']}"
        parts.append(f"{_names(_people(s['checkers'], protocol))} checked {s['checked']} of the {s['headline']} "
                     "findings behind the "
                     f"headline claims ({heads}) {against} ({when}): {s['confirmed']} confirmed and {s['corrected']} "
                     "corrected" + (", as listed in the appendix." if s["corrected"] else ".")
                     + (f" The other {s['headline'] - s['checked']} were not checked by a person."
                        if s["checked"] < s["headline"] else ""))
    from .checkpack import calibration, core_verdicts
    cal = calibration(store, paper_row["run_id"])
    if cal and cal["n"]:
        parts.append(f"{_names(_people(cal['people'], protocol))} screened a random sample of {cal['n']} records "
                     "blind to the agents' decisions and agreed with "
                     + _names([f"{a} on {v['agree']} of {v['n']}" for a, v in cal["agreement"].items()]) + ".")
    core = [v for v in core_verdicts(store, paper_row["run_id"]).values()]
    if core:
        n = {k: sum(v["verdict"] == k for v in core) for k in ("meets", "fails", "cannot_tell")}
        who = sorted({v["checker"].split(":", 1)[1] for v in core})
        parts.append(f"{_names(_people(who, protocol))} checked {len(core)} core "
                     f"{'study' if len(core) == 1 else 'studies'} against the inclusion criteria: {n['meets']} met "
                     f"them, {n['fails']} did not and {n['cannot_tell']} could not be told"
                     + (" (listed in the appendix)." if n["fails"] or n["cannot_tell"] else "."))
    if ins["total"] and not focused:
        parts.append(f"{_names(_people(ins['reviewers'], protocol)) if ins['reviewers'] else 'No person'} reviewed "
                     f"{ins['reviewed']} of the {ins['total']} insights logged during the review."
                     if ins["reviewed"] else f"None of the {ins['total']} insights logged during the review has "
                                             "been reviewed by a person.")
    statement = ((protocol.get("paper") or {}).get("declarations") or {}).get("accountability")
    if statement:
        parts.append(" ".join(statement.split()))
    parts.append(f"Models: {', '.join(method.get('models') or [])}.")
    return " ".join(parts)


def appendix(store: Store, paper_row, protocol: dict) -> list[str]:
    from .checkpack import core_verdicts
    core = [v for v in core_verdicts(store, paper_row["run_id"]).values() if v["verdict"] != "meets"]
    extra = []
    if core:
        labels = {c["id"]: c.get("label", c["id"]) for c in (protocol.get("paper") or {}).get("core") or []}
        cites = {w: lab for w, lab in ((json.loads(paper_row["method"] or "{}") or {}).get("cite_labels") or {}).items()}
        extra = ["## Appendix: core studies a person did not confirm", ""]
        for v in core:
            line = (f"- **{cites.get(str(v['work_id']), 'Record ' + str(v['work_id']))}**: "
                    f"{'fails: ' + labels.get(v['criterion'], v['criterion'] or '') if v['verdict'] == 'fails' else 'cannot tell'}"
                    f" (checked by {v['checker'].split(':', 1)[1]} on {v['created_at'][:10]})")
            extra.append(line + (f". Note: {v['note']}" if v["note"] else "."))
        extra.append("")
    items = headline_findings(store, paper_row, protocol)
    if not items:
        return extra
    out = ["## Appendix: human checks of the headline findings", ""]
    for i in items:
        c = i["check"]
        head = f"- **Finding {i['id']}** ({i['cite']}): {i['finding']}"
        if not c:
            out.append(head + " — *not checked by a person.*")
            continue
        who = c["checker"].split(":", 1)[1]
        line = f"{head} — **{c['status']}** by {who} on {c['created_at'][:10]} against {c['against']}."
        if c["correction"]:
            line += f" Correction: {c['correction']}"
        if c["note"]:
            line += f" Note: {c['note']}"
        out.append(line)
    return out + [""] + extra


def for_writer(store: Store, finding_ids) -> dict[int, dict]:
    """The latest check per finding, as the writer and the auditor see it."""
    return {fid: {"status": c["status"], "against": c["against"], "correction": c["correction"], "note": c["note"],
                  "by": c["checker"].split(":", 1)[1], "on": c["created_at"][:10]}
            for fid, c in latest(store, finding_ids).items()}


def page_data(store: Store, run_id: int, protocol: dict) -> dict | None:
    row = latest_paper(store, run_id)
    if row is None:
        return None
    items = headline_findings(store, row, protocol)
    return {"paper": row["paper_id"], "version": row["version"], "parts": headline_names(protocol),
            "items": items, "summary": summary(items), "statuses": STATUSES, "against": AGAINST,
            "insights": insight_review(store, row["corpus"])}


def clean_text(text: str | None, limit: int = 4000) -> str | None:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text[:limit] or None
