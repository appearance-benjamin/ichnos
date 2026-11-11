"""Tables and facts about the retained records, generated from the evidence store.

Like the Method section they are true by construction: the writer cites them through
method facts, it does not write them.

- eligibility:      each retained record against the protocol's inclusion criteria
                    (`[[paper.core]]`), judged from the charting with the title-and-abstract
                    scores as a check. Records meeting every criterion are the core.
- overlaps:         reports that may share a sample: a data set named in their texts, or
                    two or more shared authors.
- evidence_map:     setting by construct, naming each study and its design.
- without_findings: why each retained record yielded no finding.
- abbreviations:    acronyms as the records' own texts define them.
- apply_fixes:      corrections to bibliographic records (`[[metadata.fix]]`).
"""

from __future__ import annotations

import json
import re
from collections import Counter

from . import writer
from .fulltext import METHOD_HEADS, sections_of, select_text
from .store import Store, now, title_key

NOT_REPORTED = {"", "not_reported", "not reported", "none", "n/a", "-"}
UNKNOWN = NOT_REPORTED - {"none"}      # "none" says the thing is absent, which a criterion can judge
MARKS = {"yes": "✓", "no": "✗", "screen": "✗ˢ", "unclear": "?"}
SMALL = {"of", "and", "in", "the", "for", "on", "to", "at", "&", "de", "la", "du"}
DATA_WORDS = {"study", "survey", "panel", "cohort", "project", "programme", "program", "trial", "register",
              "registry", "database", "consortium", "sample", "initiative"}
# Words that make a named "programme" an organisation that many reports mention ("National Resident Matching
# Program"), not a sample they share.
ORGANISATION_WORDS = {"matching", "board", "boards", "association", "council", "commission", "society", "college",
                      "colleges", "department", "ministry", "agency", "office", "foundation", "federation",
                      "committee", "organization", "organisation"}


def _latest(store: Store, table: str, run_id: int) -> str | None:
    row = store.db.execute(f"SELECT extractor FROM {table} WHERE run_id = ? ORDER BY created_at DESC LIMIT 1",
                           (run_id,)).fetchone()
    return row[0] if row else None


def charting(store: Store, run_id: int) -> dict[int, dict]:
    """The latest charting per study: {work_id: {dimension: {values, text, grounded}}}."""
    ex = _latest(store, "study_features", run_id)
    out: dict[int, dict] = {}
    if ex:
        for r in store.db.execute("SELECT work_id, dimension, value, canonical, grounded FROM study_features "
                                  "WHERE run_id = ? AND extractor = ?", (run_id, ex)):
            values = [v.strip() for v in (r["canonical"] or "").split(";") if v.strip()]
            out.setdefault(r["work_id"], {})[r["dimension"]] = {"values": values, "text": r["value"],
                                                                "grounded": r["grounded"]}
    return out


def finding_rows(store: Store, run_id: int):
    from .agents import removed_ids
    ex = _latest(store, "findings", run_id)
    if not ex:
        return []
    out = removed_ids(store, run_id)
    return [r for r in store.db.execute("SELECT * FROM findings WHERE run_id = ? AND extractor = ? AND "
                                        "status != 'superseded'", (run_id, ex)) if r["work_id"] not in out]


def cite_map(store: Store, run_id: int) -> dict[int, str]:
    """The run's author-year labels: every retained record and every study with findings, with a, b, ...
    where two would share a label, so the text, the tables and the references agree."""
    from .agents import kept_ids
    ids = set(kept_ids(store, run_id)) | {r["work_id"] for r in finding_rows(store, run_id)}
    if not ids:
        return {}
    rows = store.db.execute(f"SELECT work_id, authors, year, title FROM works WHERE work_id IN "
                            f"({','.join('?' * len(ids))})", tuple(ids)).fetchall()
    return writer.cite_labels(rows)


def works(store: Store, ids, run_id: int | None = None) -> dict[int, dict]:
    """Bibliographic rows with `cite`, the author-year label; with `run_id`, the run's disambiguated label."""
    ids = list(ids)
    if not ids:
        return {}
    labels = cite_map(store, run_id) if run_id is not None else {}
    rows = store.db.execute(f"SELECT * FROM works WHERE work_id IN ({','.join('?' * len(ids))})", tuple(ids))
    return {r["work_id"]: dict(r, cite=labels.get(r["work_id"]) or writer.cite_label(r["authors"], r["year"], r["title"]))
            for r in rows}


def kind_of(work: dict) -> str:
    kind = (work.get("type") or "").split(";")[0].strip()
    kind = {"JournalArticle": "article", "journal-article": "article", "Journal Article": "article", "Study": "article",
            "ClinicalTrial": "article", "Review": "review", "LettersAndComments": "letter or comment",
            "Conference": "conference paper", "proceedings-article": "conference paper",
            "CaseReport": "case report", "BookSection": "book chapter", "MetaAnalysis": "meta-analysis"}.get(kind, kind)
    return kind.replace("-", " ").lower() or "not stated"


def _scorer(store: Store, run_id: int) -> str | None:
    row = store.db.execute("SELECT screener FROM screening_scores WHERE run_id = ? ORDER BY screener LIMIT 1",
                           (run_id,)).fetchone()
    return row[0] if row else None


def _scores(store: Store, run_id: int) -> dict[int, dict[str, int]]:
    scorer = _scorer(store, run_id)
    out: dict[int, dict[str, int]] = {}
    for r in store.db.execute("SELECT work_id, metric, score FROM screening_scores WHERE run_id = ? AND screener = ? "
                              "AND metric NOT LIKE 'eligibility:%'", (run_id, scorer)):
        out.setdefault(r["work_id"], {})[r["metric"]] = r["score"]
    return out


# Eligibility at charting: the core

def criteria(protocol: dict) -> list[dict]:
    return list((protocol.get("paper") or {}).get("core") or [])


def judge(criterion: dict, chart: dict | None, scores: dict, kind: str = "") -> str:
    """yes, no, screen (the charting meets it but the title-and-abstract score was 0) or unclear.

    Without a charted value the title-and-abstract score decides (2 yes, 1 unclear, 0 no). A charted
    value whose quote was not confirmed in the source counts as unclear. Record types listed in
    `fail_types` (e.g. data-set records, which report no results) do not meet the criterion."""
    if kind and kind in {t.replace("-", " ") for t in criterion.get("fail_types") or []}:
        return "no"
    score = scores.get(criterion.get("metric")) if criterion.get("metric") else None
    if criterion.get("dimension"):
        d = (chart or {}).get(criterion["dimension"])
        values = [v for v in (d or {}).get("values", []) if v.lower() not in UNKNOWN]
        if values:
            want = criterion.get("values", "any")
            present = [v for v in values if v.lower() != "none"]
            if not (present and (want == "any" or any(v in want for v in present))):
                return "no"
            if d.get("grounded") == 0:
                return "no" if score == 0 else "unclear"
            return "screen" if score == 0 else "yes"
    return {2: "yes", 0: "no"}.get(score, "unclear")


POTENTIAL = "potential core: full text needed"
ABSTRACT_ONLY = "conference abstract: meets every criterion as an abstract"
FULL_MIN = 2500     # characters: a parsed text shorter than this is a first page or an abstract, not the study


def conference_abstracts(meta: dict[int, dict], protocol: dict) -> set[int]:
    """Records that are conference abstracts and count as abstracts, whatever text was found for them: typed so in
    their bibliographic record, or listed by DOI in [paper] conference_abstracts."""
    listed = {str(d).lower() for d in (protocol.get("paper") or {}).get("conference_abstracts") or []}
    return {w for w, m in meta.items() if "abstract" in kind_of(m) or (m.get("doi") or "").lower() in listed}


def no_results(store: Store, run_id: int, full: set[int]) -> set[int]:
    """Studies read in full whose latest reading for findings used the full text and found none: they report no
    results of their own (an editorial, a commentary, a conceptual overview)."""
    last = {}
    for r in store.db.execute("SELECT work_id, read_from, findings FROM findings_reads WHERE run_id = ? ORDER BY "
                              "created_at, read_id", (run_id,)):
        last[r["work_id"]] = r
    return {w for w, r in last.items() if w in full and r["read_from"] == "full text" and r["findings"] == 0}


def read_in_full(store: Store, run_id: int) -> set[int]:
    """The studies charted with their full text: it is parsed, at least FULL_MIN characters long, and the latest
    charting is not older than it (a PDF added later counts once the study is charted again)."""
    chart = {r[0]: r[1] for r in store.db.execute(
        "SELECT work_id, max(created_at) FROM study_features WHERE run_id = ? GROUP BY work_id", (run_id,))}
    return {r["work_id"] for r in store.db.execute("SELECT work_id, updated_at, chars FROM fulltexts WHERE status = "
                                                   "'parsed'")
            if (r["chars"] or 0) >= FULL_MIN and chart.get(r["work_id"], "") >= r["updated_at"]}


def eligibility(store: Store, run_id: int, protocol: dict) -> list[dict]:
    from .agents import kept_ids
    crit = criteria(protocol)
    kept = kept_ids(store, run_id)
    chart, scores = charting(store, run_id), _scores(store, run_id)
    n_find = Counter(r["work_id"] for r in finding_rows(store, run_id))
    meta = works(store, kept, run_id)
    from .checkpack import core_verdicts
    checked = core_verdicts(store, run_id)
    needs_ft = bool((protocol.get("paper") or {}).get("core_needs_fulltext"))
    abstracts = conference_abstracts(meta, protocol)
    read = read_in_full(store, run_id) - abstracts
    full = read if needs_ft else set()
    empty = no_results(store, run_id, read)
    out = []
    for w in kept:
        kind = kind_of(meta[w])
        # `fail_types` concern records that report no results of their own: one that yielded findings is judged
        # like any other record.
        cells = {c["id"]: judge(c, chart.get(w), scores.get(w, {}), "" if n_find.get(w) else kind) for c in crit}
        if w in empty:
            # `no_results_fails`: read in full, it yielded no findings of its own
            cells.update({c["id"]: "no" for c in crit if c.get("no_results_fails")})
        ft = store.get_fulltext(w)
        if len((meta[w].get("abstract") or "").strip()) <= 50 and not (ft and ft["status"] == "parsed"):
            # only a title to go on: a title shows neither that a criterion is met nor that it is not; only a
            # record type checked in the bibliographic metadata (fail_types) still counts
            typed = {c["id"] for c in crit if kind in {x.replace("-", " ") for x in c.get("fail_types") or []}}
            cells = {k: (v if k in typed else "unclear") for k, v in cells.items()}
        failed = next((c for c in crit if cells[c["id"]] in ("no", "screen")), None)
        unclear = next((c for c in crit if cells[c["id"]] == "unclear"), None)
        person = checked.get(w)
        if person and person["verdict"] == "fails" and not failed:
            # a person checked the study against the criteria and found that it fails one: their call wins
            failed = next((c for c in crit if c["id"] == person["criterion"]), crit[0])
            cells[failed["id"]] = "no"
        # [paper] core_needs_fulltext: a study is core only when it was charted from its full text; one that meets
        # every criterion on its abstract is a potential core study, whose full text is needed first
        # a conference abstract counts as an abstract: never core, and no fuller text to get, so not potential either
        potential = not (failed or unclear) and needs_ft and w not in full and w not in abstracts
        abstract_only = not (failed or unclear) and needs_ft and w in abstracts
        if failed:
            group = failed.get("fails", f"fails {failed['id']}")
        elif unclear:
            group = f"cannot tell: {unclear.get('label', unclear['id'])}"
        elif potential:
            group = POTENTIAL
        elif abstract_only:
            group = ABSTRACT_ONLY
        else:
            group = "core"
        out.append({"work_id": w, "cite": meta[w]["cite"], "cells": cells,
                    "core": not (failed or unclear or potential or abstract_only), "potential": potential,
                    "abstract_only": abstract_only,
                    "group": group, "findings": n_find.get(w, 0), "charted": w in chart, "type": kind})
    return out


def eligibility_table(rows: list[dict], protocol: dict, label: str = "Table 2") -> str:
    crit = criteria(protocol)
    if not rows or not crit:
        return ""
    order = {g: i for i, g in enumerate(["core", POTENTIAL, ABSTRACT_ONLY] + [c.get("fails", "") for c in crit])}
    rows = sorted(rows, key=lambda r: (order.get(r["group"], 99), r["group"], r["cite"]))
    head = ["Set", "Source"] + [c.get("short", c["id"]).capitalize() for c in crit] + ["Findings"]
    typed = sorted({t for c in crit for t in c.get("fail_types") or []})
    lines = [f"**{label}.** Retained records against the inclusion criteria, judged from the charting: "
             "✓ met; ✗ not met; ✗ˢ the charting meets it but the title-and-abstract scoring ruled it out; ? cannot "
             "tell. Where a record or value was not charted or not reported, the title-and-abstract score decides "
             "(2 ✓, 1 ?, 0 ✗); a charted value whose quote was not confirmed in the source counts as ?"
             + (f"; records of these types that yielded no finding ({', '.join(t.replace('-', ' ') for t in typed)}) "
                "report no results of their own and do not meet the criteria marked for them" if typed else "")
             + ". The core meets every criterion"
             + (", read in its full text; a record that meets every criterion on its abstract only is a potential core "
                "study, counted once its full text is read, and a conference abstract counts as an abstract"
                if (protocol.get("paper") or {}).get("core_needs_fulltext") else "")
             + ("; a study read in full that yields no findings of its own does not meet the criteria marked for it"
                if any(c.get("no_results_fails") for c in crit) else "")
             + "; the other records are a separately labelled set, grouped by the "
               "first criterion they clearly do not meet, otherwise by the first they cannot be judged on. Criteria: "
             + "; ".join(f"{c.get('short', c['id'])}: {c.get('label', c['id'])}" for c in crit) + ".", "",
             "| " + " | ".join(head) + " |", "| " + " | ".join(["---"] * (len(head) - 1)) + " | ---: |"]
    for r in rows:
        lines.append("| " + " | ".join([r["group"], r["cite"]] + [MARKS[r["cells"][c["id"]]] for c in crit]
                                       + [str(r["findings"])]) + " |")
    return "\n".join(lines)


# Overlapping reports

def abbreviations(text: str) -> dict[str, str]:
    """Acronyms as a text defines them: 'Health and Retirement Study (HRS)' -> {'HRS': 'Health and ...'}.

    The name must spell the acronym from the starts of its words, in order: each main word gives a
    prefix ('MIDlife in the United States' -> MIDUS), small words may be skipped."""
    found = {}
    for m in re.finditer(r"\(([A-Z][A-Za-z0-9-]{1,9})\)", text):
        acr = m.group(1)
        letters = "".join(c for c in acr.upper() if c.isalpha())
        if len(letters) < 2 or acr in found or sum(c.isupper() for c in acr) < 2:
            continue
        words = re.findall(r"[^\W\d_][\w'’-]*", text[max(0, m.start() - 160):m.start()])[-12:]
        for k in range(1, len(words) + 1):
            cand = words[-k:]
            # one word spells an acronym only through its hyphenated parts ('Self-Efficacy (SE)'), never
            # through a plain prefix ('usage (US)')
            parts = [p for w in cand for p in (w.split("-") if len(cand) == 1 else [w]) if p]
            if len(parts) < 2:
                continue
            if cand[0].lower() not in SMALL and _spells(letters, [w.upper() for w in parts]):
                found[acr] = " ".join(cand)
                break
    return found


def _spells(letters: str, words: list[str]) -> bool:
    """Can `letters` be made of a non-empty prefix of each main word, in order (small words optional)?"""
    if not words:
        return not letters
    word, rest = words[0], words[1:]
    if word.lower() in SMALL and _spells(letters, rest):
        return True
    for n in range(min(len(word), len(letters)), 0, -1):
        if letters[:n] == word[:n] and _spells(letters[n:], rest):
            return True
    return False


def _text_of(store: Store, work: dict, chart: dict | None) -> str:
    parts = [work.get("title") or "", work.get("abstract") or ""]
    parts += [d["text"] or "" for d in (chart or {}).values()]
    ft = store.get_fulltext(work["work_id"])
    if ft and ft["status"] == "parsed":
        parts.append(select_text(sections_of(ft), (METHOD_HEADS,), 8000))
    return "\n".join(parts)


def _people(authors_json) -> set[str]:
    """Authors as family name plus first initial: common surnames alone (Chen, Wang, Li) would link strangers."""
    try:
        names = json.loads(authors_json or "[]")
    except ValueError:
        names = []
    out = set()
    for n in names:
        if n.strip():
            family, given = writer.name_parts(n)
            out.add(f"{family.lower()}|{given.strip()[:1].lower()}")
    return out


def overlaps(store: Store, run_id: int, protocol: dict | None = None) -> dict:
    """Groups of retained reports that may share a sample: a data set named in their texts, the same abstract,
    a pair listed under [[overlaps]] in the protocol (thesis and article, preprint and article, ...) and, unless
    `[paper] overlap_by_authors = false`, two or more shared authors."""
    protocol = protocol or {}
    from .agents import kept_ids
    kept = kept_ids(store, run_id)
    meta, chart = works(store, kept, run_id), charting(store, run_id)
    texts = {w: _text_of(store, meta[w], chart.get(w)) for w in kept}
    defined: dict[str, str] = {}
    meanings: dict[str, dict[str, str]] = {}
    for t in texts.values():
        for acr, name in abbreviations(t).items():
            defined.setdefault(acr, name)
            meanings.setdefault(acr, {}).setdefault(re.sub(r"s$", "", " ".join(name.lower().replace("-", " ").split())),
                                                     name)
    # A data set has a proper name ("Health and Retirement Study", "McMaster Modular Assessment Program"); a generic
    # phrase that happens to be abbreviated ("program of assessment (PoA)") links unrelated reports, and so does an
    # organisation's name ("National Resident Matching Program (NRMP)").
    datasets = {a: n for a, n in defined.items() if DATA_WORDS & {x.lower() for x in n.split()}
                and not ORGANISATION_WORDS & {x.lower() for x in n.split()}
                and any(x[:1].isupper() for x in n.split() if x.lower() not in DATA_WORDS)}
    named: dict[str, set[int]] = {}
    for w, t in texts.items():
        for acr, name in datasets.items():
            if re.search(rf"\b{re.escape(acr)}\b", t) or name.lower() in t.lower():
                named.setdefault(acr, set()).add(w)
    links: list[tuple[set[int], str]] = [(ws, f"{datasets[a]} ({a})") for a, ws in named.items() if len(ws) > 1]
    if (protocol.get("paper") or {}).get("overlap_by_authors", True):
        people = {w: _people(meta[w]["authors"]) for w in kept}
        for i, a in enumerate(kept):
            for b in kept[i + 1:]:
                if len(people[a] & people[b]) >= 2:
                    links.append(({a, b}, "shared authors"))
    for o in protocol.get("overlaps") or []:
        dois = {d.lower() for d in o.get("dois") or []}
        keys = [title_key(t) or "" for t in o.get("titles") or []]
        ws = {w for w in kept if (meta[w].get("doi") or "").lower() in dois
              or any(k and (title_key(meta[w].get("title") or "") or "").startswith(k) for k in keys)}
        if len(ws) > 1:
            links.append((ws, o.get("reason", "the same study (named in the protocol)")))
    same = {}
    for w in kept:                                       # the same abstract under two records: one report twice
        key = (title_key(meta[w].get("abstract") or "") or "")[:300]
        if len(key) >= 150:
            same.setdefault(key, set()).add(w)
    links += [(ws, "same abstract") for ws in same.values() if len(ws) > 1]
    titled = {}
    for w in kept:                                       # the same title under two records the merge missed
        key = title_key(meta[w].get("title") or "") or ""
        if len(key) >= 30:
            titled.setdefault(key, set()).add(w)
    links += [(ws, "same title") for ws in titled.values() if len(ws) > 1]
    parent = {w: w for w in kept}

    def root(w):
        while parent[w] != w:
            parent[w] = parent[parent[w]]
            w = parent[w]
        return w
    for ws, _ in links:
        ws = sorted(ws)
        for w in ws[1:]:
            parent[root(w)] = root(ws[0])
    groups: dict[int, dict] = {}
    for ws, basis in links:
        g = groups.setdefault(root(next(iter(ws))), {"works": set(), "basis": []})
        g["works"] |= ws
        if basis not in g["basis"]:
            g["basis"].append(basis)
    return {"groups": sorted(groups.values(), key=lambda g: -len(g["works"])), "root": {w: root(w) for w in kept},
            "datasets": {a: datasets[a] for a in named if len(named[a]) > 1}, "meta": meta,
            # an acronym the records define in two ways (CBD: case-based discussion, Competence by Design) gets both
            "defined": {a: (defined[a] if len(meanings[a]) == 1 else
                            " or ".join(sorted(meanings[a].values(), key=str.lower)) + ", depending on the study")
                        for a in defined}}


def independent(ov: dict, ids) -> int:
    """How many of these reports remain when each group of possibly overlapping reports counts once."""
    return len({ov["root"][w] for w in ids if w in ov["root"]})


def overlap_table(ov: dict, contributing: set[int], label: str = "Table 3", by_authors: bool = True) -> str:
    if not ov["groups"]:
        return ""
    lines = [f"**{label}.** Retained reports that may share a sample, with the basis for each group: a data set "
             "named in their title, abstract, charting or methods, the same title or abstract, or a pair named in the "
             "protocol (thesis and article, preprint and article)" + (" or shared authors" if by_authors else
                                                                     "; shared authors alone do not group reports")
             + ". Reports in bold contributed findings.", "",
             "| Group | Reports | Basis |", "| ---: | --- | --- |"]
    for i, g in enumerate(ov["groups"], 1):
        names = sorted(ov["meta"][w]["cite"] for w in g["works"])
        bold = {ov["meta"][w]["cite"] for w in g["works"] if w in contributing}
        lines.append(f"| {i} | " + "; ".join(f"**{n}**" if n in bold else n for n in names)
                     + f" | {'; '.join(g['basis'])} |")
    return "\n".join(lines)


# Evidence map

def evidence_map(store: Store, run_id: int, protocol: dict, core: set[int], label: str = "Table 4",
                 core_label: str = "Table 2") -> str:
    rows = finding_rows(store, run_id)
    contributing = sorted({r["work_id"] for r in rows})
    if not contributing:
        return ""
    chart, meta = charting(store, run_id), works(store, contributing, run_id)
    dims = {d["name"]: d for d in (protocol.get("map") or {}).get("dimensions", [])}
    across = (protocol.get("map") or {}).get("match_across", "setting")
    tree = (protocol.get("map") or {}).get("tree") or []
    by = next((d for d in tree if d != across), "construct")
    design = "design" if "design" in dims else None
    cells: dict[tuple[str, str], list[str]] = {}
    for w in contributing:
        c = chart.get(w, {})
        rows_v = [v for v in c.get(across, {}).get("values", []) if v.lower() not in NOT_REPORTED] or ["not reported"]
        cols_v = [v for v in c.get(by, {}).get("values", []) if v.lower() not in NOT_REPORTED] or ["not reported"]
        des = ", ".join(v.replace("_", " ") for v in c.get(design, {}).get("values", [])
                        if v.lower() not in NOT_REPORTED) if design else ""
        entry = meta[w]["cite"] + ("*" if w in core else "") + (f" ({des})" if des else "")
        for a in rows_v:
            for b in cols_v:
                cells.setdefault((a, b), []).append(entry)

    def ordered(values, dim):
        cats = (dims.get(dim) or {}).get("categories") or []
        return sorted(values, key=lambda v: (cats.index(v) if v in cats else len(cats), v))
    row_keys = ordered({a for a, _ in cells}, across)
    col_keys = ordered({b for _, b in cells}, by)
    lines = [f"**{label}.** Evidence map of the {len(contributing)} studies that contributed findings: "
             f"{across} by {by} as charted, with each study's design; * marks a core study ({core_label}). A study charted "
             "with several values appears in several cells.", "",
             f"| {across.capitalize()} | " + " | ".join(b.replace("_", " ") for b in col_keys) + " |",
             "| --- | " + " | ".join(["---"] * len(col_keys)) + " |"]
    for a in row_keys:
        lines.append(f"| {a.replace('_', ' ')} | " + " | ".join("; ".join(sorted(cells.get((a, b), []))) or "–"
                                                                for b in col_keys) + " |")
    return "\n".join(lines)


# Records without findings

def in_scope(store: Store, run_id: int, protocol: dict, kept=None) -> set[int] | None:
    """The retained studies findings are extracted from, if the protocol limits them with [[findings.scope]]:
    a study is in scope when its charting has any listed value on any listed dimension, or it is a core study.
    None when there is no scope (every retained study)."""
    from .agents import kept_ids
    rules = (protocol.get("findings") or {}).get("scope") or []
    if not rules:
        return None
    kept = kept if kept is not None else kept_ids(store, run_id)
    chart = charting(store, run_id)
    core = {e["work_id"] for e in eligibility(store, run_id, protocol) if e["core"]} if criteria(protocol) else set()
    out = set()
    for w in kept:
        c = chart.get(w, {})
        if w in core or any(set(c.get(r["dimension"], {}).get("values", [])) & set(r["values"]) for r in rules):
            out.add(w)
    return out


def brief_ids(store: Store, run_id: int, protocol: dict, works=None) -> set[int]:
    """The studies that get a brief findings extraction ([findings.brief]): their charting has a value of a `when`
    rule and none of an `unless` rule, and they are not core studies."""
    b = (protocol.get("findings") or {}).get("brief") or {}
    if not b:
        return set()
    from .agents import kept_ids
    works = works if works is not None else kept_ids(store, run_id)
    chart = charting(store, run_id)
    core = {e["work_id"] for e in eligibility(store, run_id, protocol) if e["core"]} if criteria(protocol) else set()

    def hit(c, rules):
        return any(set(c.get(r["dimension"], {}).get("values", [])) & set(r["values"]) for r in rules)
    return {w for w in works if w not in core and hit(chart.get(w, {}), b.get("when") or [])
            and not hit(chart.get(w, {}), b.get("unless") or [])}


def without_findings(store: Store, run_id: int, elig: list[dict], scope: set[int] | None = None) -> list[dict]:
    rows = finding_rows(store, run_id)
    last = max((r["created_at"] for r in rows), default=None)
    meta = works(store, [e["work_id"] for e in elig], run_id)
    out = []
    for e in elig:
        if e["findings"]:
            continue
        w = meta[e["work_id"]]
        kind = kind_of(w)
        ft = store.get_fulltext(w["work_id"])
        has_ft = bool(ft and ft["status"] == "parsed")
        has_abs = len((w.get("abstract") or "").strip()) > 50
        text = "abstract and full text" if has_abs and has_ft else "full text" if has_ft else \
            "abstract" if has_abs else "title only"
        human = store.db.execute("SELECT max(created_at) FROM screening WHERE run_id = ? AND work_id = ? AND "
                                 "screener LIKE 'human:%' AND decision != 'exclude'", (run_id, w["work_id"])).fetchone()[0]
        if not e["charted"] and human and last and human > last:
            why = f"retained by the author on {human[:10]}, after charting was done; not charted"
        elif not e["charted"]:
            why = "not charted"
        elif text == "title only":
            why = "no abstract and no open-access full text: nothing to chart beyond the title"
        elif scope is not None and w["work_id"] not in scope:
            why = "charted; outside the findings scope set in the protocol"
        elif kind in ("review", "editorial"):
            why = f"not an original study ({kind}); reports no finding of its own"
        elif kind == "dataset":
            why = "a data-set record: describes the data and reports no results"
        elif kind == "conference abstract":
            why = "conference abstract: no result on the review question"
        else:
            why = f"charted from the {text}: no result on the review question"
        out.append({"work_id": w["work_id"], "cite": e["cite"], "type": kind, "text": text, "why": why,
                    "group": e["group"]})
    return out


def without_table(rows: list[dict], label: str = "Table 5", elig_label: str = "Table 2") -> str:
    if not rows:
        return ""
    lines = [f"**{label}.** Retained records that yielded no finding, with the text available to the "
             f"agents, why no finding was extracted and where the record falls in {elig_label}.", "",
             f"| Source | Type | Text available | Why no finding | Set ({elig_label}) |", "| --- | --- | --- | --- | --- |"]
    lines += [f"| {r['cite']} | {r['type']} | {r['text']} | {r['why']} | {r['group']} |"
              for r in sorted(rows, key=lambda r: (r["why"], r["cite"]))]
    return "\n".join(lines)


# Corrections to bibliographic records

FIX_KEYS = ("doi", "openalex_id", "s2_id", "pmid")
FIX_FIELDS = ("authors", "title", "year", "venue", "type", "language")
FIX_BIBLIO = ("volume", "issue", "pages")     # corrections to what Crossref gave (python -m ichnos biblio)


def apply_fixes(store: Store, protocol: dict) -> int:
    """Apply `[[metadata.fix]]` entries: each names a record by DOI or source id and the fields to set."""
    n = 0
    for fix in (protocol.get("metadata") or {}).get("fix") or []:
        key = next((k for k in FIX_KEYS if fix.get(k)), None)
        if not key:
            raise ValueError(f"metadata.fix needs one of {FIX_KEYS}: {fix}")
        values = {f: (json.dumps(fix[f], ensure_ascii=False) if f == "authors" else fix[f])
                  for f in FIX_FIELDS if f in fix}
        value = str(fix[key]).lower() if key == "doi" else str(fix[key])
        where = "lower(doi) = ?" if key == "doi" else f"{key} = ?"
        bib = {f: str(fix[f]) for f in FIX_BIBLIO if f in fix}
        if bib and store.db.execute("SELECT 1 FROM sqlite_master WHERE name = 'biblio'").fetchone():
            with store.db:
                cur = store.db.execute(f"UPDATE biblio SET {', '.join(f'{f} = ?' for f in bib)} WHERE work_id IN "
                                       f"(SELECT work_id FROM works WHERE {where}) AND ("
                                       + " OR ".join(f"{f} IS NOT ?" for f in bib) + ")",
                                       (*bib.values(), value, *bib.values()))
            n += cur.rowcount
        if not values:
            continue
        with store.db:
            cur = store.db.execute(f"UPDATE works SET {', '.join(f'{f} = ?' for f in values)}, updated_at = ? "
                                   f"WHERE {where} AND (" + " OR ".join(f"{f} IS NOT ?" for f in values) + ")",
                                   (*values.values(), now(), value, *values.values()))
        n += cur.rowcount
    return n


def proper_nouns(store: Store, run_id: int) -> set[str]:
    """Proper nouns for sentence-casing reference titles: from all abstracts, charted values and findings."""
    texts = [r[0] for r in store.db.execute("SELECT abstract FROM works")]
    texts += [r[0] for r in store.db.execute("SELECT value FROM study_features WHERE run_id = ?", (run_id,))]
    texts += [f"{r['finding']} {r['evidence']} {r['for_whom'] or ''}" for r in finding_rows(store, run_id)]
    return writer.case_lexicon(texts)


# Tables for the paper itself (the record-level tables above go to the supplement)

def count_specs(protocol: dict) -> list[dict]:
    """What to count studies by: `[paper] count_tables` (pairs of findings fields, or {rows, cols, where, title},
    `where` keeping only findings with those field values), else the findings matrix."""
    specs = (protocol.get("paper") or {}).get("count_tables")
    if not specs:
        m = (protocol.get("findings") or {}).get("matrix")
        specs = [m] if m else []
    return [dict(s) if isinstance(s, dict) else {"rows": s[0], "cols": s[1]} for s in specs]


def table_labels(protocol: dict) -> dict[str, str]:
    """Each table's number by its role, so the Method, captions and the writer agree."""
    out, n = {"flow": "Figure 1"}, 1
    paper = protocol.get("paper") or {}
    if criteria(protocol):
        out["core"], n = f"Table {n}", n + 1
    for i, _ in enumerate(paper.get("focus") or []):
        out[f"focus{i}"], n = f"Table {n}", n + 1
    if paper.get("summary"):
        out["summary"], n = f"Table {n}", n + 1
    if paper.get("outcome_table"):
        out["outcomes"], n = f"Table {n}", n + 1
    for i, _ in enumerate(count_specs(protocol)):
        out[f"counts{i}"], n = f"Table {n}", n + 1
    out["overlaps"], n = f"Table {n}", n + 1
    for role in ("programmes", "fidelity", "evidence_strength"):
        if paper.get(role):
            out[role], n = f"Table {n}", n + 1
    if not paper.get("outcome_table"):          # the outcome table replaces the evidence map as counts
        out["map"] = f"Table {n}"
    out.update(sources="Table S1", eligibility="Table S2", map_list="Table S3", without="Table S4")
    if paper.get("summary"):
        out["subset_list"] = "Table S5"
    k = 6 if paper.get("summary") else 5
    out["prisma"] = f"Table S{k}"
    for role in paper.get("supplement") or []:  # reviews, exclusions, criteria, recall, repository, findings_list
        k += 1
        out[role] = f"Table S{k}"
    if protocol.get("aim"):
        # A focused paper: Figure 1 and the aim's three tables in the main text; the scoping tables in the supplement.
        # the general paper keeps the comparative evidence, study by study, in the main text
        comp = next((f"focus{i}" for i, s in enumerate(paper.get("focus") or []) if s.get("group_by")), None) \
            if protocol["aim"].get("kind") == "effectiveness" else None
        moved = [r for r, lab in out.items() if lab.startswith("Table ") and not lab.startswith("Table S") and r != comp]
        for role in moved:
            k += 1
            out[role] = f"Table S{k}"
        if protocol["aim"].get("kind") == "focus":           # a hypothesis test: the evidence statements first
            out["statements"] = "Table 1"
            policy = protocol["aim"].get("layout") in ("policy", "essay")   # the other tests in the supplement
            for role in ("sensitivity", "strata"):
                if (protocol.get("focus") or {}).get(role):
                    if policy:
                        k += 1
                        out[role] = f"Table S{k}"
                    else:
                        out[role] = "Table 3" if "sensitivity" in out else "Table 2"
            if policy:
                out["model"] = "Table 2"                     # the writer's recommendations
            k += 1
            out["studies"] = f"Table S{k}"
            if paper.get("guideline") == "PRISMA 2020":     # a systematic review reports each study's risk of bias
                out["bias"] = f"Table S{k + 1}"
            return out
        if protocol["aim"].get("kind") == "strong":          # the strongest studies and their effects, then the actions
            out.update(effects="Table 1", model="Table 2", appraisals=f"Table S{k + 1}")
            return out
        if protocol["aim"].get("kind") == "proposal":        # a redesign: the design choices first
            out.update(solutions="Table 1", outcomes_aim="Table 2", comparator="Table 3", patterns="Table 4",
                       model="Table 5")                      # the proposal's own model table
            k += 1
            out["studies"] = f"Table S{k}"
        elif protocol["aim"].get("kind") == "effectiveness":   # the general paper: the answer by outcome first
            out.update(outcomes_aim="Table 1", solutions="Table 2", comparator="Table 3", patterns="Table 4")
            # the comparative evidence study by study, then the writer's own table of recommendations
            if comp:
                out[comp] = "Table 5"
            out["model"] = "Table 6" if comp else "Table 5"
            k += 1
            out["studies"] = f"Table S{k}"
        else:
            out.update(solutions="Table 1", comparator="Table 2", patterns="Table 3", studies="Table 4")
        k += 1
        out["comparator_list"] = f"Table S{k}"
    return out


def label_id(label: str) -> str:
    """'Figure 1' -> F1, 'Table 3' -> T3, 'Table S2' -> S2."""
    num = label.split()[-1]
    return ("F" + num) if label.startswith("Figure") else (num if num.startswith("S") else "T" + num)


def core_table(store: Store, run_id: int, protocol: dict, elig: list[dict], label: str = "Table 1",
               ov: dict | None = None) -> str:
    core = [e for e in elig if e["core"]]
    if not core:
        return ""
    chart, meta = charting(store, run_id), works(store, [e["work_id"] for e in core], run_id)
    dims = {d["name"]: d for d in (protocol.get("map") or {}).get("dimensions", [])}
    cats = [d for d in (protocol.get("map") or {}).get("tree") or [] if dims.get(d, {}).get("categories")]
    free = [d for d in ("measure", "population", "culture", "country") if d in dims]
    n_find = Counter(r["work_id"] for r in finding_rows(store, run_id))

    def cell(w, d):
        c = chart.get(w, {}).get(d) or {}
        if dims[d].get("categories"):
            return ", ".join(v.replace("_", " ") for v in c.get("values", [])) or "not reported"
        return _said(c.get("text")).replace("|", "/")
    lines = [f"**{label}.** Characteristics of the {len(core)} core studies (meeting every inclusion criterion): "
             + ", ".join(d.replace("_", " ") for d in cats + free) + " as charted, the publication type, the text "
             "they were charted from and the number of findings."
             + (independent_note(ov, [e["work_id"] for e in core]) if ov else ""), "",
             "| Source | " + " | ".join(d.replace("_", " ").capitalize() for d in cats + free)
             + " | Type | Charted from | Findings |", "| --- | " + " | ".join("---" for _ in cats + free) + " | --- | --- | ---: |"]
    for e in sorted(core, key=lambda e: e["cite"]):
        w = e["work_id"]
        ft = store.get_fulltext(w)
        lines.append(f"| {e['cite']} | " + " | ".join(cell(w, d) for d in cats + free) + f" | {kind_of(meta[w])} | "
                     + ("full text" if ft and ft["status"] == "parsed" else "abstract") + f" | {n_find.get(w, 0)} |")
    return "\n".join(lines)


def _full_text(store: Store, ids) -> set[int]:
    """The studies among these charted from a parsed full text."""
    ids = list(ids)
    if not ids:
        return set()
    return {r[0] for r in store.db.execute(f"SELECT work_id FROM fulltexts WHERE status = 'parsed' AND work_id IN "
                                           f"({','.join('?' * len(ids))})", tuple(ids))}


def _codes_of(value) -> list[str]:
    """A finding's code on a field as a list: "positive_association; no_association" is two codes."""
    return [v.strip() for v in str(value or "").split(";") if v.strip()]


def _matches(fields: dict, where: dict) -> bool:
    """Does a finding have, on every field of `where`, at least one of the listed codes?"""
    return all(set(_codes_of(fields.get(k))) & {str(x) for x in v} for k, v in where.items())


def count_table(store: Store, run_id: int, protocol: dict, rows_field: str, cols_field: str,
                label: str = "Table 2", where: dict | None = None, title: str | None = None,
                note: str | None = None, only: set[int] | None = None, ov: dict | None = None,
                text: bool = False, overlaps_label: str | None = None) -> str:
    """How many studies reported findings with each pair of field values (a study counts once per cell). With `ov`, a
    Samples column counts reports that may share a sample once; with `text`, each cell gives in brackets how many of
    its studies were charted from a full text, a sensitivity count restricted to full-text records."""
    fields = {f["name"]: f for f in (protocol.get("findings") or {}).get("fields", [])}
    cells, row_tot = {}, {}
    for r in finding_rows(store, run_id):
        if only is not None and r["work_id"] not in only:
            continue
        f = json.loads(r["fields"] or "{}")
        if where and not _matches(f, where):
            continue
        for a in _codes_of(f.get(rows_field)):          # a finding coded "a; b" counts in both cells
            for b in _codes_of(f.get(cols_field)):
                cells.setdefault((a, b), set()).add(r["work_id"])
                row_tot.setdefault(a, set()).add(r["work_id"])
    if not cells:
        return ""

    def order(field, values):
        cats = (fields.get(field) or {}).get("categories") or []
        return sorted(values, key=lambda v: (cats.index(v) if v in cats else len(cats), v))
    rows, cols = order(rows_field, {a for a, _ in cells}), order(cols_field, {b for _, b in cells})
    coded = ("; only findings coded " + "; ".join(f"{k.replace('_', ' ')} {', '.join(x.replace('_', ' ') for x in v)}"
                                                 for k, v in where.items())) if where else ""
    lines = [f"**{label}.** {(title + ': n') if title else 'N'}umber of {'core ' if only is not None else ''}"
             f"studies with findings by "
             f"{rows_field.replace('_', ' ')} and {cols_field.replace('_', ' ')}{coded}, as coded on each finding by "
             f"the findings agent (a study with several "
             "findings in a cell counts once; a study can appear in several cells, and a finding coded with two values "
             "counts under both). The evidence map counts studies by "
             "their study-level charting, done by another agent, so the two tables can differ where the agents coded "
             "a study differently." + (f" {' '.join(note.split())}" if note else "")
             + (" In brackets, how many of the studies in a cell were charted from a full text: the same count "
                "restricted to full-text records, so that a cell resting only on abstracts can be told apart."
                if text else "")
             + (" The Samples column counts reports that may share a sample"
                + (f" ({overlaps_label})" if overlaps_label else "") + " once." if ov else ""), "",
             f"| {rows_field.replace('_', ' ').capitalize()} | " + " | ".join(c.replace("_", " ") for c in cols)
             + " | Studies |" + (" Samples |" if ov else ""),
             "| --- | " + " | ".join("---:" for _ in cols) + " | ---: |" + (" ---: |" if ov else "")]
    full = _full_text(store, set().union(*row_tot.values())) if text else set()

    def cell(ids):
        return f"{len(ids)} ({len(ids & full)})" if text and ids else str(len(ids))
    for a in rows:
        lines.append(f"| {a.replace('_', ' ')} | " + " | ".join(cell(cells.get((a, b), set())) for b in cols)
                     + f" | {cell(row_tot[a])} |" + (f" {independent(ov, row_tot[a])} |" if ov else ""))
    return "\n".join(lines)


def evidence_counts(store: Store, run_id: int, protocol: dict, core: set[int], label: str = "Table 4",
                    list_label: str = "Table S3", ov: dict | None = None) -> str:
    """The evidence map as counts: studies with findings per cell, core studies in brackets; with `ov`, each row's
    studies and the samples they come from (reports that may share a sample counted once)."""
    rows = finding_rows(store, run_id)
    contributing = sorted({r["work_id"] for r in rows})
    if not contributing:
        return ""
    chart = charting(store, run_id)
    dims = {d["name"]: d for d in (protocol.get("map") or {}).get("dimensions", [])}
    across = (protocol.get("map") or {}).get("match_across", "setting")
    by = next((d for d in (protocol.get("map") or {}).get("tree") or [] if d != across), "construct")
    cells = {}
    for w in contributing:
        c = chart.get(w, {})
        for a in [v for v in c.get(across, {}).get("values", []) if v.lower() not in UNKNOWN] or ["not reported"]:
            for b in [v for v in c.get(by, {}).get("values", []) if v.lower() not in UNKNOWN] or ["not reported"]:
                cells.setdefault((a, b), set()).add(w)

    def order(dim, values):
        cats = (dims.get(dim) or {}).get("categories") or []
        return sorted(values, key=lambda v: (cats.index(v) if v in cats else len(cats), v))
    rs, cs = order(across, {a for a, _ in cells}), order(by, {b for _, b in cells})
    lines = [f"**{label}.** Evidence map as counts: studies with findings by {across.replace('_', ' ')} and "
             f"{by.replace('_', ' ')} as charted for the study as a whole (not as coded on each finding), with the "
             f"number of core studies in brackets. The studies in each cell are listed in {list_label}."
             + (" A study charted with several values counts in several cells but once in the Studies column; the "
                "Samples column counts reports that may share a sample once." if ov else ""), "",
             f"| {across.replace('_', ' ').capitalize()} | " + " | ".join(b.replace("_", " ") for b in cs) + " |"
             + (" Studies | Samples |" if ov else ""),
             "| --- | " + " | ".join("---:" for _ in cs) + " |" + (" ---: | ---: |" if ov else "")]
    for a in rs:
        row = set().union(*(cells.get((a, b), set()) for b in cs))
        lines.append(f"| {a.replace('_', ' ')} | " + " | ".join(
            (f"{len(cells[(a, b)])} ({len(cells[(a, b)] & core)})" if (a, b) in cells else "0") for b in cs) + " |"
            + (f" {len(row)} ({len(row & core)}) | {independent(ov, row)} |" if ov else ""))
    return "\n".join(lines)


# Tables on a subset of studies: a focus table (both agents' codings) and a summary of characteristics

def _codes(store: Store, run_id: int) -> tuple[dict, dict]:
    """Per study: the charted values by dimension, and the values coded on its findings by field."""
    chart = charting(store, run_id)
    coded: dict[int, dict[str, Counter]] = {}
    for r in finding_rows(store, run_id):
        for k, v in json.loads(r["fields"] or "{}").items():
            for x in _codes_of(v) or [str(v)]:
                coded.setdefault(r["work_id"], {}).setdefault(k, Counter())[x] += 1
    return chart, coded


def _hit(w, rules, chart, coded) -> bool:
    """Does the study have a listed value on a rule's dimension, as charted or as coded on a finding?"""
    for r in rules:
        have = set(chart.get(w, {}).get(r["dimension"], {}).get("values", [])) | set(coded.get(w, {}).get(r["dimension"], {}))
        if have & set(r["values"]):
            return True
    return False


def subset_ids(store: Store, run_id: int, spec: dict, chart=None, coded=None, only: set[int] | None = None) -> list[int]:
    """Studies with findings that meet every rule in `all` (or `when`) and, if given, one rule in `any`."""
    if chart is None:
        chart, coded = _codes(store, run_id)
    out = []
    for w in sorted(coded):
        if only is not None and w not in only:
            continue
        if all(_hit(w, [r], chart, coded) for r in spec.get("all") or spec.get("when") or []) and \
                (not spec.get("any") or _hit(w, spec["any"], chart, coded)):
            out.append(w)
    return out


def _said(text) -> str:
    """Charted text, or "not reported" for the placeholders agents write when a record says nothing."""
    t = " ".join(str(text or "").split())
    return "not reported" if t.lower().replace("_", " ") in {"", "not reported", "none", "n/a"} else t


def _clip(text: str, n: int = 140) -> str:
    text = " ".join(str(text or "").split()).replace("|", "/")
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


def focus_table(store: Store, run_id: int, protocol: dict, spec: dict, label: str,
                only: set[int] | None = None, ov: dict | None = None) -> str:
    """The studies a rule picks out, under either agent's coding, with both codings side by side."""
    chart, coded = _codes(store, run_id)
    ids = subset_ids(store, run_id, spec, chart, coded, only)
    if not ids:
        return ""
    meta = works(store, ids, run_id)
    show = spec.get("show") or [r["dimension"] for r in spec.get("any") or []]
    # Each shown dimension is filtered by its own values in the rule (none: every value); a dimension that only the
    # charting has, or only the findings have, gets one column.
    wanted = {r["dimension"]: set(r["values"]) for r in spec.get("any") or []}
    charted_dims = {d["name"] for d in (protocol.get("map") or {}).get("dimensions") or []}
    finding_dims = {f["name"] for f in (protocol.get("findings") or {}).get("fields") or []}
    cols = [(d, kind) for d in show for kind in ("charted", "findings")
            if (kind == "charted" and (d in charted_dims or not charted_dims))
            or (kind == "findings" and (d in finding_dims or not finding_dims))]
    head = {"charted": "as charted", "findings": "on findings"}
    lines = [f"**{label}.** {spec.get('title', 'Studies')}: the {len(ids)} studies with findings whose charting or "
             "findings show " + "; or ".join(f"{r['dimension'].replace('_', ' ')} "
                                             f"{', '.join(v.replace('_', ' ') for v in r['values'])}"
                                             for r in spec.get("any") or []) + ", with "
             + " and ".join(d.replace("_", " ") for d in show) + " as charted for the study (charting agent) and as "
             "coded on its findings (findings agent, with the number of findings), so that disagreements between the "
             "two codings are visible." + (independent_note(ov, ids) if ov else ""), "",
             "| Source | Design | Population | " + " | ".join(f"{d.replace('_', ' ').capitalize()} {head[k]}"
                                                          for d, k in cols) + " | Findings | Charted from |",
             "| --- | --- | --- | " + " | ".join("---" for _ in cols) + " | ---: | --- |"]
    for w in sorted(ids, key=lambda w: meta[w]["cite"]):
        c = chart.get(w, {})
        ft = store.get_fulltext(w)
        cells = []
        for d, kind in cols:
            if kind == "charted":
                cells.append(", ".join(v.replace("_", " ") for v in c.get(d, {}).get("values", [])) or "not charted")
            else:
                on = coded.get(w, {}).get(d, Counter())
                keep = wanted.get(d)
                cells.append("; ".join(f"{v.replace('_', ' ')} {n}" for v, n in on.most_common()
                                       if not keep or v in keep) or "none listed")
        has_abs = len((meta[w].get("abstract") or "").strip()) > 50
        lines.append(f"| {meta[w]['cite']} | {', '.join(v.replace('_', ' ') for v in c.get('design', {}).get('values', [])) or 'not reported'} | "
                     f"{_clip(_said((c.get('population') or {}).get('text')))} | " + " | ".join(cells)
                     + f" | {sum(coded.get(w, {}).get('outcome', Counter()).values()) or sum(next(iter(coded.get(w, {}).values()), Counter()).values())}"
                     f" | {'full text' if ft and ft['status'] == 'parsed' else 'abstract' if has_abs else 'title only'} |")
    return "\n".join(lines)


def _grouped(values: list[str], text: str, groups: dict | None) -> list[str]:
    """A study's group on a dimension: the first of the protocol's named groups whose regular expressions match the
    charted values or text. Without a match, the values themselves (`_rest = "values"`) or "other or unnamed"; with
    no groups at all, the values themselves."""
    known = [v for v in values if v.lower() not in UNKNOWN]
    if not groups:   # a study with several values counts once, under their combination
        return [" + ".join(sorted(v.replace("_", " ") for v in known))] if known else ["not reported"]
    hay = " ; ".join(values + [text or ""]).lower()
    for name, pats in groups.items():
        if name != "_rest" and any(re.search(p, hay) for p in pats):
            return [name]
    if not known:
        return ["not reported"]
    known = [_proper(v) for v in known]
    return [" + ".join(sorted(v.replace("_", " ") for v in known))] if groups.get("_rest") == "values"         else ["other or unnamed"]


def _proper(v: str) -> str:
    """A name charted in lowercase (a country, an institution), capitalised for a table."""
    if v != v.lower():
        return v
    return " ".join(w if w in SMALL else w[:1].upper() + w[1:] for w in v.replace("_", " ").split())


def summary_table(store: Store, run_id: int, protocol: dict, spec: dict, label: str, list_label: str,
                  only: set[int] | None = None) -> str:
    """How many studies of a subset fall in each group of each dimension (a study can count in several)."""
    chart, coded = _codes(store, run_id)
    ids = subset_ids(store, run_id, spec, chart, coded, only)
    if not ids:
        return ""
    meta = works(store, ids, run_id)
    groups = spec.get("groups") or {}
    lines = [f"**{label}.** Characteristics of the {len(ids)} {'core ' if only is not None else ''}"
             f"{spec.get('title', 'studies')}: number of studies "
             "by " + ", ".join(d.replace("_", " ") for d in spec.get("rows") or []) + ", the publication type and "
             "the text they were charted from. Each study counts once per characteristic"
             + ("; groups are defined in the protocol by the names the records use (a study counts in the first group "
                "it matches, and a city counts under its country)" if groups else "") + f". Each study is listed in {list_label}.", "",
             "| Characteristic | Group | Studies |", "| --- | --- | ---: |"]
    for d in spec.get("rows") or []:
        n = Counter()
        for w in ids:
            c = chart.get(w, {}).get(d) or {}
            for g in set(_grouped(c.get("values", []), c.get("text", ""), groups.get(d))):
                n[g] += 1
        top = n.most_common()
        shown = top[:int(spec.get("top", 8))]
        rest = sum(k for _, k in top[len(shown):])
        for g, k in shown:
            lines.append(f"| {d.replace('_', ' ').capitalize()} | {g.replace('_', ' ')} | {k} |")
        if rest:
            lines.append(f"| {d.replace('_', ' ').capitalize()} | {len(top) - len(shown)} other groups | {rest} |")
    kinds = Counter(kind_of(meta[w]) for w in ids)
    for k, v in kinds.most_common():
        lines.append(f"| Publication type | {k} | {v} |")
    texts = Counter()
    for w in ids:
        ft = store.get_fulltext(w)
        texts["full text" if ft and ft["status"] == "parsed" else
              "abstract" if len((meta[w].get("abstract") or "").strip()) > 50 else "title only"] += 1
    for k, v in texts.most_common():
        lines.append(f"| Charted from | {k} | {v} |")
    return "\n".join(lines)


def subset_list(store: Store, run_id: int, protocol: dict, spec: dict, label: str) -> str:
    """Every study of a subset with its charted characteristics, for the supplement."""
    chart, coded = _codes(store, run_id)
    ids = subset_ids(store, run_id, spec, chart, coded)
    if not ids:
        return ""
    meta = works(store, ids, run_id)
    dims = spec.get("rows") or []
    lines = [f"**{label}.** The {len(ids)} {spec.get('title', 'studies')}, with "
             + ", ".join(d.replace("_", " ") for d in dims) + " as charted, the publication type, the text they were "
             "charted from and the number of findings.", "",
             "| Source | " + " | ".join(d.replace("_", " ").capitalize() for d in dims) + " | Type | Charted from | Findings |",
             "| --- | " + " | ".join("---" for _ in dims) + " | --- | --- | ---: |"]
    for w in sorted(ids, key=lambda w: meta[w]["cite"]):
        c = chart.get(w, {})
        cells = []
        for d in dims:
            v = c.get(d) or {}
            vals = [x.replace("_", " ") for x in v.get("values", []) if x.lower() not in UNKNOWN]
            cells.append(_clip(_said(v.get("text")) if d == "population" else ", ".join(vals) or "not reported", 120))
        ft = store.get_fulltext(w)
        text = "full text" if ft and ft["status"] == "parsed" else \
            "abstract" if len((meta[w].get("abstract") or "").strip()) > 50 else "title only"
        n = sum(next(iter(coded.get(w, {}).values()), Counter()).values())
        lines.append(f"| {meta[w]['cite']} | " + " | ".join(cells) + f" | {kind_of(meta[w])} | {text} | {n} |")
    return "\n".join(lines)


# PRISMA-ScR (Tricco et al., 2018): where each item is reported, generated with the tables

PRISMA_SCR = [
    ("Title", "1", "Title", "Title."),
    ("Abstract", "2", "Structured summary", "Abstract."),
    ("Introduction", "3", "Rationale", "Introduction."),
    ("Introduction", "4", "Objectives", "Introduction: the last part, and Method: Protocol and question."),
    ("Methods", "5", "Protocol and registration", None),
    ("Methods", "6", "Eligibility criteria", "Method: Eligibility criteria."),
    ("Methods", "7", "Information sources", "Method: Information sources and search (sources, dates, citation "
                                             "searching)."),
    ("Methods", "8", "Search", "Method: Information sources and search (the full string, and the string for citation "
                               "searching where one was used)."),
    ("Methods", "9", "Selection of sources of evidence", "Method: Selection of sources of evidence."),
    ("Methods", "10", "Data charting process", "Method: Data charting."),
    ("Methods", "11", "Data items", "Method: Data charting (the charted dimensions)."),
    ("Methods", "12", "Critical appraisal of individual sources (optional)", "Not done; study-level descriptors "
                                                                              "(design, population, text charted from, "
                                                                              "publication type) are reported instead."),
    ("Methods", "13", "Synthesis of results", "Method: Synthesis of results; Synthesis and use of AI."),
    ("Results", "14", "Selection of sources of evidence", "{flow}; Results."),
    ("Results", "15", "Characteristics of sources of evidence", "{characteristics}."),
    ("Results", "16", "Critical appraisal within sources (optional)", "Not done (item 12)."),
    ("Results", "17", "Results of individual sources of evidence", "{individual}; every finding with its verbatim "
                                                                     "passage is in the evidence store."),
    ("Results", "18", "Synthesis of results", "Results; {counts}."),
    ("Discussion", "19", "Summary of evidence", "Discussion: the first section."),
    ("Discussion", "20", "Limitations", "Discussion: limitations of the evidence and of this AI-assisted review."),
    ("Discussion", "21", "Conclusions", "Conclusion."),
    ("Funding", "22", "Funding", None),
]


# PRISMA 2020 (Page et al., 2021): the checklist for a systematic review, used when `[paper] guideline` names it. Each
# item has a default place; `[paper.prisma_where]` gives a paper's own (by item number) where its sections differ.
PRISMA_2020 = [
    ("Title", "1", "Title", "Title (identifies the report as a systematic review)."),
    ("Abstract", "2", "Abstract", "Abstract (structured, following PRISMA 2020 for Abstracts)."),
    ("Introduction", "3", "Rationale", "Introduction."),
    ("Introduction", "4", "Objectives", "Introduction: the last part; Abstract: Objective."),
    ("Methods", "5", "Eligibility criteria", "Method; Supplementary method: Eligibility criteria; {studies}."),
    ("Methods", "6", "Information sources", "Method; Supplementary method: Information sources and search (each "
                                            "source and the date it was last searched)."),
    ("Methods", "7", "Search strategy", "Supplementary method: Information sources and search (every string in full)."),
    ("Methods", "8", "Selection process", "Method; Supplementary method: Selection of sources of evidence (agents, "
                                          "independence, agreement, how disagreements were resolved)."),
    ("Methods", "9", "Data collection process", "Method; Supplementary method: Data charting; Synthesis and use of AI."),
    ("Methods", "10a", "Data items: outcomes", "Method; Supplementary method: Synthesis of results (the outcomes and "
                                               "how each finding's direction was coded)."),
    ("Methods", "10b", "Data items: other variables", "Supplementary method: Data charting (the charted dimensions)."),
    ("Methods", "11", "Study risk of bias assessment", None),
    ("Methods", "12", "Effect measures", None),
    ("Methods", "13a", "Synthesis methods: eligibility for each synthesis", "Method; {studies}."),
    ("Methods", "13b", "Synthesis methods: preparing data", "Method (one direction per study and context)."),
    ("Methods", "13c", "Synthesis methods: tabulation and display", "Method; {counts}."),
    ("Methods", "13d", "Synthesis methods: methods and rationale", "Method."),
    ("Methods", "13e", "Synthesis methods: exploring heterogeneity", "Method; {strata}."),
    ("Methods", "13f", "Synthesis methods: sensitivity analyses", "Method; {sensitivity}."),
    ("Methods", "14", "Reporting bias assessment", "Method."),
    ("Methods", "15", "Certainty assessment", "Method."),
    ("Results", "16a", "Study selection", "Results; {flow}."),
    ("Results", "16b", "Studies excluded that seemed to meet the criteria", "{flow}; {without} (retained records "
                                                                             "without findings, with the reason)."),
    ("Results", "17", "Study characteristics", "Results; {studies}."),
    ("Results", "18", "Risk of bias in studies", "Results; {bias}."),
    ("Results", "19", "Results of individual studies", "{studies} (direction per study and context); every finding "
                                                       "with its verbatim passage is in the evidence store."),
    ("Results", "20a", "Results of syntheses: characteristics and risk of bias", "Results; {counts}."),
    ("Results", "20b", "Results of syntheses: statistical syntheses", "Results; {counts}."),
    ("Results", "20c", "Results of syntheses: heterogeneity", "Results; {strata}."),
    ("Results", "20d", "Results of syntheses: sensitivity analyses", "Results; {sensitivity}."),
    ("Results", "21", "Reporting biases", "Results; {strata} (abstract-only against full-text studies)."),
    ("Results", "22", "Certainty of evidence", "Results; {counts} (the strength verdict of each statement)."),
    ("Discussion", "23a", "Interpretation", "Discussion."),
    ("Discussion", "23b", "Limitations of the evidence", "Discussion: Limitations."),
    ("Discussion", "23c", "Limitations of the review processes", "Discussion: Limitations."),
    ("Discussion", "23d", "Implications", "Discussion: practical implications and research that would decide it."),
    ("Other information", "24a", "Registration", None),
    ("Other information", "24b", "Protocol", "Declarations: Registration and protocol."),
    ("Other information", "24c", "Amendments", "Supplementary method: Protocol deviations (each amendment with its "
                                               "date, who decided and why)."),
    ("Other information", "25", "Support", None),
    ("Other information", "26", "Competing interests", None),
    ("Other information", "27", "Availability of data, code and other materials", "Declarations: Data and materials."),
]


def prisma_table(protocol: dict, labels: dict, label: str) -> str:
    """The completed checklist: each item with where this manuscript reports it, or that it was not done."""
    p = protocol.get("paper") or {}
    stated = p.get("declarations") or {}
    if p.get("guideline") == "PRISMA 2020":
        return _prisma_2020(protocol, labels, label)
    has = lambda role: role in labels  # noqa: E731
    fill = {"flow": labels["flow"],
            "characteristics": "; ".join(labels[r] for r in ("core", "summary", "sources", "subset_list") if has(r)),
            "individual": "; ".join(labels[r] for r in ("sources", "map_list", "subset_list") if has(r)),
            "counts": "; ".join(v for k, v in labels.items() if k.startswith(("counts", "focus")) or k == "map")}
    lines = [f"**{label}.** PRISMA-ScR checklist (Tricco et al., 2018): where this manuscript reports each item.", "",
             "| Section | Item | Topic | Where reported |", "| --- | ---: | --- | --- |"]
    for section, n, topic, where in PRISMA_SCR:
        if n == "5":
            where = ("Method: Protocol and question (protocol version and hash); registered: " + p["registration"]
                     if p.get("registration") else "Method: Protocol and question (protocol version and hash); "
                                                   "the protocol was not registered.")
        elif n == "22":
            where = "Declarations: Funding" + ("." if stated.get("funding") else " (not yet stated by the author).")
        lines.append(f"| {section} | {n} | {topic} | {where.format(**fill)} |")
    return "\n".join(lines)


def _prisma_2020(protocol: dict, labels: dict, label: str) -> str:
    p = protocol.get("paper") or {}
    stated = p.get("declarations") or {}
    own = {str(k): v for k, v in (p.get("prisma_where") or {}).items()}
    pick = lambda *roles: "; ".join(labels[r] for r in roles if r in labels) or "–"  # noqa: E731
    fill = {"flow": labels["flow"], "studies": pick("studies", "core", "subset_list"), "bias": pick("bias"),
            "counts": pick("statements", *[k for k in labels if k.startswith("focus")]),
            "strata": pick("strata"), "sensitivity": pick("sensitivity"), "without": pick("without")}

    def declared(key, head):
        return f"Declarations: {head}" + ("." if stated.get(key) else " (to be stated by the author).")
    fixed = {"11": "Method; not done with a formal tool.",
             "12": "Method: the direction of each study's findings (vote counting by direction of effect).",
             "24a": ("Registered: " + p["registration"]) if p.get("registration") else
                    "Not registered (Declarations: Registration and protocol).",
             "25": declared("funding", "Funding"), "26": declared("competing_interests", "Competing interests")}
    lines = [f"**{label}.** PRISMA 2020 checklist (Page et al., 2021): where this manuscript reports each item.", "",
             "| Section | Item | Topic | Where reported |", "| --- | ---: | --- | --- |"]
    for section, n, topic, where in PRISMA_2020:
        text = own.get(n) or fixed.get(n) or where or "–"
        lines.append(f"| {section} | {n} | {topic} | {' '.join(str(text).split()).format(**fill)} |")
    return "\n".join(lines)


# Tables added after the second editorial decision: the paper's tables count the core studies with findings (R8),
# comparisons in kinds (R13), one table of outcomes (R26), fidelity (R22), programmes (R10), evidence strength (R27).

def core_set(store: Store, run_id: int, protocol: dict) -> set[int]:
    return {e["work_id"] for e in eligibility(store, run_id, protocol) if e["core"]} if criteria(protocol) else set()


def paper_set(store: Store, run_id: int, protocol: dict) -> set[int] | None:
    """The studies the paper's tables count: the core studies with findings when `[paper] core_only`, else all."""
    if not (protocol.get("paper") or {}).get("core_only"):
        return None
    return core_set(store, run_id, protocol) & {r["work_id"] for r in finding_rows(store, run_id)}


def independent_note(ov: dict, ids) -> str:
    ids = set(ids)
    n = independent(ov, ids)
    return (f" Counting reports that may share a sample once, the {len(ids)} studies are {n} independent studies."
            if n != len(ids) else f" None of the {len(ids)} studies shares a sample with another (as far as detected).")


def _text_source(store: Store, meta: dict, w: int) -> str:
    ft = store.get_fulltext(w)
    return "full text" if ft and ft["status"] == "parsed" else \
        "abstract" if len((meta[w].get("abstract") or "").strip()) > 50 else "title only"


def _cat(protocol: dict, field: str, findings: bool = True) -> list[str]:
    spec = (protocol.get("findings") or {}).get("fields") if findings else (protocol.get("map") or {}).get("dimensions")
    return next((d.get("categories") or [] for d in spec or [] if d["name"] == field), [])


def _label(v: str) -> str:
    return str(v).replace("_", " ")


def outcome_table(store: Store, run_id: int, protocol: dict, spec: dict, label: str, only: set[int] | None,
                  ov: dict) -> str:
    """Studies by outcome, with their settings and the direction of their findings, from the codes on each finding:
    one table in place of separate outcome-by-direction, setting-by-outcome and charted evidence-map tables."""
    rows_f, by_f, dir_f = spec.get("rows", "outcome"), spec.get("by", "setting"), spec.get("direction", "direction")
    data: dict[str, dict] = {}
    studies = set()
    for r in finding_rows(store, run_id):
        if only is not None and r["work_id"] not in only:
            continue
        f = json.loads(r["fields"] or "{}")
        o = f.get(rows_f)
        if not o:
            continue
        d = data.setdefault(o, {"all": set(), "by": {}, "dir": {}})
        d["all"].add(r["work_id"])
        d["by"].setdefault(f.get(by_f) or "not coded", set()).add(r["work_id"])
        d["dir"].setdefault(f.get(dir_f) or "not coded", set()).add(r["work_id"])
        studies.add(r["work_id"])
    if not data:
        return ""
    cats = _cat(protocol, rows_f)
    outs = sorted(data, key=lambda v: (cats.index(v) if v in cats else len(cats), v))
    by_cats = [c for c in _cat(protocol, by_f) if any(c in d["by"] for d in data.values())]
    dir_cats = [c for c in _cat(protocol, dir_f) if any(c in d["dir"] for d in data.values())]
    who = "core studies with findings" if only is not None else "studies with findings"
    lines = [f"**{label}.** Outcomes of the {len(studies)} {who}: for each outcome the number of studies with at least "
             f"one finding on it, the number of independent studies, the {_label(by_f)} and the direction of those "
             f"findings, all as coded on each finding by the findings agent. A study counts once in a cell but can "
             f"appear in several cells: a study with findings in two {_label(by_f)}s counts in both columns, and one "
             f"with favourable and unfavourable findings in both direction columns; the Studies column counts each study "
             f"once. Directions were coded by an agent and not checked by a person; the counts describe where findings "
             f"point, they are not vote counts of effectiveness." + independent_note(ov, studies), "",
             f"| {_label(rows_f).capitalize()} | Studies | Independent | "
             + " | ".join(_label(c) for c in by_cats) + " | " + " | ".join(_label(c) for c in dir_cats) + " |",
             "| --- | ---: | ---: | " + " | ".join("---:" for _ in by_cats + dir_cats) + " |"]
    for o in outs:
        d = data[o]
        lines.append(f"| {_label(o)} | {len(d['all'])} | {independent(ov, d['all'])} | "
                     + " | ".join(str(len(d["by"].get(c, ()))) for c in by_cats) + " | "
                     + " | ".join(str(len(d["dir"].get(c, ()))) for c in dir_cats) + " |")
    return "\n".join(lines)


PARTIAL = {"added_to_traditional": "added to traditional high-stakes exams",
           "built_on_pa_principles": "built on its principles under another name"}
N_OF_ONE = re.compile(r"\b(n\s*=\s*1\b|single (trainee|student|resident|learner|participant)|auto-?ethnograph)", re.I)


def comparison_table(store: Store, run_id: int, protocol: dict, spec: dict, label: str, only: set[int] | None,
                     ov: dict) -> str:
    """The comparative evidence in kinds of comparison (`group_by`, a charted dimension), each study marked for what
    limits it: a case report or a single participant, reports that may share a sample, a partial implementation."""
    chart, coded = _codes(store, run_id)
    ids = [w for w in subset_ids(store, run_id, spec, chart, coded) if only is None or w in only]
    if not ids:
        return ""
    meta = works(store, ids, run_id)
    dim = spec["group_by"]
    groups = spec.get("groups") or {}
    kinds = {w: [v for v in chart.get(w, {}).get(dim, {}).get("values", []) if v in groups] for w in ids}
    roots = {w: ov["root"].get(w, w) for w in ids}
    partial = set(spec.get("partial") or [])
    lines_by = {}
    for w in ids:
        c = chart.get(w, {})
        marks = []
        design = [v for v in c.get("design", {}).get("values", []) if v.lower() not in UNKNOWN]
        if "case_report" in design:
            marks.append("case report")
        if N_OF_ONE.search(f"{(c.get('population') or {}).get('text') or ''} {meta[w].get('abstract') or ''}"):
            marks.append("single participant")
        mates = sorted(meta[x]["cite"] for x in ids if x != w and roots[x] == roots[w])
        if mates:
            marks.append("may share a sample with " + "; ".join(mates))
        fid = set(c.get("fidelity", {}).get("values", [])) | set(c.get("pa_status", {}).get("values", []))
        if fid & partial:
            marks.append("partial implementation (" + ", ".join(PARTIAL.get(v, _label(v)) for v in sorted(fid & partial))
                         + ")")
        found = coded.get(w, {})
        outs = "; ".join(f"{_label(o)}" for o, _ in found.get("outcome", Counter()).most_common())
        dirs = "; ".join(f"{_label(d)} {n}" for d, n in found.get("direction", Counter()).most_common())
        row = (f"| {meta[w]['cite']} | {', '.join(_label(v) for v in design) or 'not reported'} | "
               f"{_clip(_said((c.get('population') or {}).get('text')), 110)} | {outs or 'none listed'} | "
               f"{dirs or 'none listed'} | {'; '.join(marks) or '–'} | {_text_source(store, meta, w)} |")
        for k in kinds[w] or ["none"]:
            lines_by.setdefault(k, []).append((meta[w]["cite"], row))
    order = list(groups) + (["none"] if "none" in lines_by else [])
    head = "| Source | Design | Population | Outcomes of its findings | Direction of its findings (findings) | " \
           "Marks | Charted from |"
    effect = [g for g in spec.get("effect_groups") or []]
    eff = {w for w in ids if set(kinds[w]) & set(effect)}
    rand = {w for w in eff if "randomised" in chart.get(w, {}).get("comparison", {}).get("values", [])}
    title = spec.get("title", "Comparative evidence")
    out = [f"**{label}.** {title}: the {len(ids)} core studies with findings whose charting or findings show a "
           f"comparison, grouped by the kind of comparison as charted (a study with two kinds appears in both groups). "
           f"Marks name what limits a study: a case report or a single participant, reports that may share a sample, "
           f"and partial implementations (an assessment added to traditional high-stakes exams, or a programme built on "
           f"the principles under another name). {len(eff)} studies compare programmatic with other assessment "
           f"({', '.join(groups[g].lower() for g in effect)}); counting reports that may share a sample once, they are "
           f"{independent(ov, eff)} independent comparisons, {len(rand)} of them randomised." + (
               f" {len(lines_by.get('none', []))} studies were charted with no comparison although a finding was "
               f"coded as comparative or before-after; they are listed last and are not comparisons." if "none" in lines_by
               else ""), ""]
    for g in order:
        rows = sorted(lines_by.get(g, []))
        if not rows:
            continue
        out += [f"*{groups.get(g, 'Charted with no comparison')}* ({len(rows)} studies)", "", head,
                "| --- | --- | --- | --- | --- | --- | --- |"] + [r for _, r in rows] + [""]
    return "\n".join(out).rstrip()


def fidelity_table(store: Store, run_id: int, protocol: dict, spec: dict, label: str, only: set[int] | None,
                   ov: dict) -> str:
    """Findings on the chosen outcomes by how fully the programme implements programmatic assessment (`dimension`, a
    charted list of the consensus principles)."""
    dim, outs = spec.get("dimension", "fidelity"), spec.get("outcomes") or []
    chart = charting(store, run_id)
    ids = only if only is not None else {r["work_id"] for r in finding_rows(store, run_id)}
    if not any(chart.get(w, {}).get(dim) for w in ids):
        return ""
    data: dict[int, dict[str, Counter]] = {}
    for r in finding_rows(store, run_id):
        if r["work_id"] in ids:
            f = json.loads(r["fields"] or "{}")
            if f.get("outcome") in outs:
                data.setdefault(r["work_id"], {}).setdefault(f["outcome"], Counter())[f.get("direction") or "?"] += 1

    def config(w):
        v = set(chart.get(w, {}).get(dim, {}).get("values", []))
        if not v:
            return "not charted"
        if "added_to_traditional" in v:
            return "added to traditional high-stakes assessment"
        if "replaces_traditional" in v and v & {"proportional_stakes", "committee_decision"}:
            return "replaces traditional assessment; decisions on aggregated data"
        if v - {"none_described", "not_reported", "replaces_traditional"}:
            return "some principles described; replacement not stated"
        return "no principle described"
    configs = ["replaces traditional assessment; decisions on aggregated data", "some principles described; "
               "replacement not stated", "added to traditional high-stakes assessment", "no principle described",
               "not charted"]
    principles = [c for c in _cat(protocol, dim, findings=False) if c not in ("none_described",)]

    def cell(ws, o):
        hit = [w for w in ws if o in data.get(w, {})]
        if not hit:
            return "0"
        d = Counter()
        for w in hit:
            for k in data[w][o]:
                d[k] += 1
        return f"{len(hit)} (" + ", ".join(f"{_label(k)} {n}" for k, n in d.most_common()) + ")"
    lines = [f"**{label}.** How fully the programmes implement programmatic assessment, and their findings on "
             + ", ".join(_label(o) for o in outs) + f": core studies with findings by the configuration charted from "
             f"their texts (rows above the line; each study in one row) and by each principle described (rows below; a "
             f"study in every row it describes). Cells give the number of studies with findings on the outcome and, in "
             f"brackets, the number of those studies with findings in each direction (a study can count in several). "
             f"A principle not described is not necessarily absent: abstracts rarely describe a programme fully.",
             "", "| Configuration or principle | Studies | Independent | " + " | ".join(_label(o).capitalize() for o in outs)
             + " |", "| --- | ---: | ---: | " + " | ".join("---" for _ in outs) + " |"]
    by_config = {c: {w for w in ids if config(w) == c} for c in configs}
    for c in configs:
        ws = by_config[c]
        if ws:
            lines.append(f"| {c} | {len(ws)} | {independent(ov, ws)} | " + " | ".join(cell(ws, o) for o in outs) + " |")
    lines.append("| *Principles described:* | | | " + " | ".join("" for _ in outs) + " |")
    for p in principles:
        ws = {w for w in ids if p in chart.get(w, {}).get(dim, {}).get("values", [])}
        if ws:
            lines.append(f"| {_label(p)} | {len(ws)} | {independent(ov, ws)} | " + " | ".join(cell(ws, o) for o in outs)
                         + " |")
    return "\n".join(lines)


def programme_table(store: Store, run_id: int, protocol: dict, spec: dict, label: str, only: set[int] | None,
                    ov: dict, core: set[int]) -> str:
    """The core studies by the institution whose programme they report (`dimension`, charted), with their settings:
    how much of the evidence comes from how few programmes."""
    dim = spec.get("dimension", "institution")
    chart = charting(store, run_id)
    ids = sorted(core)
    if not any(chart.get(w, {}).get(dim) for w in ids):
        return ""
    meta = works(store, ids, run_id)
    with_f = {r["work_id"] for r in finding_rows(store, run_id)}
    inst: dict[str, set[int]] = {}
    for w in ids:
        for n in institutions(chart, w, dim, spec.get("aliases")) or ["not named"]:
            inst.setdefault(n, set()).add(w)
    named = {k: v for k, v in inst.items() if k != "not named"}
    progs = {(i, s) for i, ws in named.items() for w in ws
             for s in chart.get(w, {}).get("setting", {}).get("values", []) or ["not reported"]}
    multi = {k: v for k, v in named.items() if len(v) > 1}
    lines = [f"**{label}.** Programme-level map: the {len(ids)} core studies by the institution whose programme they "
             f"report, as charted from their texts (an institution named only in the authors' affiliations is not "
             f"counted). {len(ids) - len(inst.get('not named', ()))} core studies name {len(named)} institutions, "
             f"{len(progs)} institution-and-setting combinations; {len(inst.get('not named', ()))} name none. "
             f"Institutions with more than one core study are listed; the {len(named) - len(multi)} others have one "
             f"each and are listed in Table S1's population column.", "",
             "| Institution | Core studies | With findings | Independent | Settings | Reports |",
             "| --- | ---: | ---: | ---: | --- | --- |"]
    for k, ws in sorted(multi.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        sets = Counter(s for w in ws for s in chart.get(w, {}).get("setting", {}).get("values", []))
        lines.append(f"| {_proper(k)} | {len(ws)} | {len(ws & with_f)} | {independent(ov, ws)} | "
                     + ", ".join(f"{_label(s)} {n}" for s, n in sets.most_common()) + " | "
                     + "; ".join(sorted(meta[w]["cite"] for w in ws)) + " |")
    lines.append(f"| {len(named) - len(multi)} institutions with one core study each | {len(named) - len(multi)} | "
                 f"{sum(1 for v in named.values() if len(v) == 1 and v & with_f)} | – | – | – |")
    if inst.get("not named"):
        ws = inst["not named"]
        lines.append(f"| No institution named | {len(ws)} | {len(ws & with_f)} | {independent(ov, ws)} | – | – |")
    return "\n".join(lines)


def institutions(chart: dict, w: int, dim: str = "institution", aliases: dict | None = None) -> list[str]:
    """A study's charted institutions, with the protocol's aliases (an abbreviation the record used, a hospital
    under its university) mapped to one name."""
    names = [v for v in chart.get(w, {}).get(dim, {}).get("values", []) if v.lower() not in UNKNOWN]
    return sorted({(aliases or {}).get(n, n) for n in names})


def strength_table(store: Store, run_id: int, protocol: dict, label: str, only: set[int] | None, ov: dict,
                   inst_dim: str = "institution") -> str:
    """For each outcome, what the evidence behind a statement about it rests on: studies, independent studies,
    institutions, designs and kinds of evidence, and the text the studies were charted from."""
    chart = charting(store, run_id)
    data: dict[str, dict] = {}
    for r in finding_rows(store, run_id):
        if only is not None and r["work_id"] not in only:
            continue
        f = json.loads(r["fields"] or "{}")
        o = f.get("outcome")
        if o:
            d = data.setdefault(o, {"all": set(), "ev": {}})
            d["all"].add(r["work_id"])
            d["ev"].setdefault(f.get("evidence_type") or "?", set()).add(r["work_id"])
    if not data:
        return ""
    meta = works(store, {w for d in data.values() for w in d["all"]}, run_id)
    cats = _cat(protocol, "outcome")
    lines = [f"**{label}.** Strength of the evidence behind statements about each outcome: core studies with findings "
             "on it, independent studies (reports that may share a sample counted once), institutions named, studies "
             "with comparative or before-after findings (and how many were randomised), studies whose findings on it "
             "are perceptions only, and the text the studies were charted from. Kinds of evidence and outcomes were "
             "coded on each finding by an agent.", "",
             "| Outcome | Studies | Independent | Institutions | Comparative or before-after (randomised) | "
             "Perception only | Full text | Abstract only | Conference abstracts |",
             "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for o in sorted(data, key=lambda v: (cats.index(v) if v in cats else len(cats), v)):
        d = data[o]
        ws = d["all"]
        comp = d["ev"].get("comparative", set()) | d["ev"].get("pre_post", set())
        rand = {w for w in comp if "randomised" in chart.get(w, {}).get("comparison", {}).get("values", [])}
        perc = {w for w in ws if all(w not in v for k, v in d["ev"].items() if k != "perception")}
        aliases = ((protocol.get("paper") or {}).get("programmes") or {}).get("aliases")
        insts = {v for w in ws for v in institutions(chart, w, inst_dim, aliases)}
        src = Counter(_text_source(store, meta, w) for w in ws)
        conf = sum(1 for w in ws if "conference" in kind_of(meta[w]) or "proceedings" in kind_of(meta[w])
                   or re.match(r"^\d{2,4}\b", meta[w].get("title") or ""))
        lines.append(f"| {_label(o)} | {len(ws)} | {independent(ov, ws)} | {len(insts)} | {len(comp)} ({len(rand)}) | "
                     f"{len(perc)} | {src.get('full text', 0)} | {src.get('abstract', 0)} | {conf} |")
    return "\n".join(lines)


def reviews_table(store: Store, run_id: int, protocol: dict, label: str) -> str:
    """The records excluded as reviews and the core studies whose references (and citations) were searched."""
    from .mining import seeds
    who = seeds(store, run_id, protocol)
    if not who["reviews"]:
        return ""
    meta = works(store, who["reviews"])
    scorer = _scorer(store, run_id)
    by_scorer = {r[0] for r in store.db.execute("SELECT work_id FROM screening WHERE run_id = ? AND screener = ? AND "
                                                "decision = 'exclude' AND reason = 'review'", (run_id, scorer))}
    known = {}
    for b in (protocol.get("recall") or {}).get("sources") or []:
        ref = next((x["reference"] for x in protocol.get("background") or [] if x["id"] == b), "")
        m = re.search(r"10\.\d{4,9}/\S+", ref)
        if m:
            known[m.group(0).rstrip(".").lower()] = b
    lines = [f"**{label}.** The {len(who['reviews'])} records that at least one screening agent excluded as reviews "
             f"and that were not retained ({len(by_scorer & set(who['reviews']))} of them by the criteria-scoring "
             "agent). Citation searching searched the reference lists of those excluded when it ran, and the references "
             "and citations of the studies that were core then (Method). Background marks a review also used as a "
             "background source.", "",
             "| Record | Year | Title | Type | Background |", "| --- | ---: | --- | --- | --- |"]
    for w in sorted(who["reviews"], key=lambda w: (meta[w]["cite"], w)):
        m = meta[w]
        lines.append(f"| {m['cite']} | {m['year'] or 'n.d.'} | {_clip(m['title'], 120)} | {kind_of(m)} | "
                     f"{known.get((m.get('doi') or '').lower(), '–')} |")
    return "\n".join(lines)


def exclusions_table(store: Store, run_id: int, label: str) -> str:
    """Records excluded after the eligibility stage by a decision recorded in the protocol, with who, when and why."""
    from .agents import kept_ids, protocol_exclusions
    passed = set(kept_ids(store, run_id, "title_abstract"))
    dropped = {w: e for w, e in protocol_exclusions(store, run_id).items() if w in passed}
    if not dropped:
        return ""
    meta = works(store, dropped)
    lines = [f"**{label}.** The {len(dropped)} records excluded after the eligibility stage by a decision recorded in "
             "the protocol, with the date, who decided and why. The author has not yet confirmed them.", "",
             "| Record | Title | Date | Decided by | Reason |", "| --- | --- | --- | --- | --- |"]
    for w, e in sorted(dropped.items(), key=lambda kv: meta[kv[0]]["cite"]):
        lines.append(f"| {meta[w]['cite']} | {_clip(meta[w]['title'], 100)} | {e.get('date', 'undated')} | "
                     f"{e.get('who', 'the reviewer')} | {' '.join(str(e.get('reason', '')).split())} |")
    return "\n".join(lines)


REPOSITORIES = {"10.5281/": "Zenodo", "10.6084/": "figshare", "10.17605/": "OSF", "10.17632/": "Mendeley Data",
                "10.7910/": "Harvard Dataverse", "10.3886/": "ICPSR", "10.13130/": "UNIMI Dataverse",
                "10.31234/": "PsyArXiv"}


def repository_records(store: Store, run_id: int, protocol: dict) -> tuple[list[tuple], Counter]:
    """The records listed under `[repository] records` that reached the retained set (or were excluded from it by a
    recorded decision), as table rows, with counts: all, retained, excluded, data deposits, published version found,
    retained with findings."""
    spec = protocol.get("repository") or {}
    recs = {str(r["doi"]).lower(): r for r in spec.get("records") or [] if r.get("doi")}
    if not recs:
        return [], Counter()
    from .agents import kept_ids, protocol_exclusions, sheet_ids
    sheet = sheet_ids(store, run_id)
    kept, dropped = set(kept_ids(store, run_id)), set(protocol_exclusions(store, run_id))
    meta = works(store, sheet, run_id)
    by_doi = {(m.get("doi") or "").lower(): w for w, m in meta.items() if m.get("doi")}
    nf = Counter(r["work_id"] for r in finding_rows(store, run_id))
    rows, n = [], Counter()
    for doi, r in recs.items():
        w = by_doi.get(doi)
        if w is None or (w not in kept and w not in dropped):
            continue
        pub = str(r.get("published") or "").lower()
        p = by_doi.get(pub)
        published = ("none found" if not pub else
                     f"{meta[p]['cite']} ({'retained' if p in kept else 'not retained'})" if p is not None else
                     f"doi:{pub} (not retrieved by the search)")
        kind = r.get("kind") or kind_of(meta[w])
        status = "excluded" if w in dropped else "retained"
        n["all"] += 1
        n[status] += 1
        n["deposit"] += kind == "data deposit"
        n["found"] += bool(pub)
        n["findings"] += status == "retained" and nf.get(w, 0) > 0
        rows.append((meta[w]["cite"], next((v for k, v in REPOSITORIES.items() if doi.startswith(k)), "repository"),
                     kind, nf.get(w, 0) if status == "retained" else "–", published, status,
                     " ".join(str(r.get("decision") or "").split()), " ".join(str(r.get("note") or "").split())))
    return sorted(rows), n


def repository_table(store: Store, run_id: int, protocol: dict, label: str) -> str:
    """Records hosted by a repository rather than a journal (`[repository] records` in the protocol), each with the
    check for a published version and the decision taken, so that they can be told apart from journal reports."""
    spec = protocol.get("repository") or {}
    rows, n = repository_records(store, run_id, protocol)
    if not rows:
        return ""
    lines = [f"**{label}.** The {n['all']} records hosted by a repository ({', '.join(sorted(set(REPOSITORIES.values())))}) "
             f"rather than by a journal, among them {n['deposit']} data deposits, with the check for a published "
             f"version: on {spec.get('date', 'an undated day')} each title (for a data deposit also without words such as "
             "'Dataset for') was searched in OpenAlex and Crossref, and the review's own records were searched for "
             f"shared authors and title words. A published version was found for {n['found']}; {n['excluded']} records "
             f"were excluded as copies of a retained report, and {n['retained']} remain, {n['findings']} of them with "
             "findings. Data deposits are not core studies (they fail the empirical criterion as data-set records). "
             f"Decisions by {spec.get('who', 'the reviewer')}, not yet confirmed by the author.", "",
             "| Record | Repository | Kind | Findings | Published version | Status | Decision | Note |",
             "| --- | --- | --- | ---: | --- | --- | --- | --- |"]
    lines += ["| " + " | ".join(str(x).replace("|", "/") for x in r) + " |" for r in rows]
    return "\n".join(lines)


def findings_list_table(store: Store, run_id: int, protocol: dict, spec: dict, label: str) -> str:
    """Each finding with the given field values (`[paper.findings_list] where`), with the study's population and
    design as charted, the effect as the findings agent recorded it and the text the study was charted from."""
    where = spec.get("where") or {}
    chart = charting(store, run_id)
    picked = [r for r in finding_rows(store, run_id) if _matches(json.loads(r["fields"] or "{}"), where)]
    show = list(spec.get("show") or [])
    if not picked:
        return ""
    meta = works(store, {r["work_id"] for r in picked}, run_id)
    full = _full_text(store, meta)
    order = {c: i for i, c in enumerate(_cat(protocol, "outcome"))}
    picked.sort(key=lambda r: (min((order.get(c, 99) for c in _codes_of(json.loads(r["fields"] or "{}").get("outcome"))),
                                   default=99), meta[r["work_id"]]["cite"], r["finding_id"]))
    what = "; ".join(f"{_label(k)} {', '.join(_label(x) for x in v)}" for k, v in where.items())
    lines = [f"**{label}.** {spec.get('title', 'Findings')}: the {len(picked)} findings from "
             f"{len({r['work_id'] for r in picked})} studies coded {what}, each with the study's design and population "
             "as charted (the population gives the sample size where the record states it), the finding and the "
             "effect as the findings agent recorded them from the passage (statistic, size and whether it was adjusted, "
             "as far as the record reports them; 'not reported' where it gives none), and the text the study was "
             "charted from. Codes are the findings agent's, not checked by a person."
             + "".join(f" {' '.join(str(x).split())}" for x in [spec.get("note")] if x), "",
             "| Source | Design | Population | Outcome | Direction | " + "".join(f"{_label(k).capitalize()} | " for k in show)
             + "Finding | Effect as reported | Charted from |",
             "| --- | --- | --- | --- | --- | " + "--- | " * len(show) + "--- | --- | --- |"]
    for r in picked:
        w, f, c = r["work_id"], json.loads(r["fields"] or "{}"), chart.get(r["work_id"], {})
        lines.append(f"| {meta[w]['cite']} | "
                     f"{', '.join(_label(v) for v in c.get('design', {}).get('values', [])) or 'not reported'} | "
                     f"{_clip(_said((c.get('population') or {}).get('text')), 110)} | {_label(f.get('outcome', ''))} | "
                     f"{_label(f.get('direction', ''))} | " + "".join(f"{_label(f.get(k, '')) or 'not coded'} | " for k in show)
                     + f"{_clip(r['finding'], 220)} | "
                     f"{_clip(r['effect'] or 'not reported', 160)} | "
                     f"{'full text' if w in full else 'abstract' if len((meta[w].get('abstract') or '').strip()) > 50 else 'title only'} |")
    return "\n".join(lines)


def criteria_text(protocol: dict, label: str) -> str:
    """The inclusion rule and the criteria of both stages, verbatim from the protocol."""
    s, e = protocol.get("screening") or {}, protocol.get("eligibility") or {}
    if not s.get("rule"):
        return ""
    lines = [f"**{label}.** The eligibility criteria verbatim from the protocol: the inclusion rule and the criteria "
             "the agents scored at title and abstract (2 met, 1 cannot tell, 0 not met; the first criterion scored 0 "
             "excluded the record, all met included it, anything else was kept for the next stage), and the rule and "
             "criteria of the eligibility stage.", "", "| Stage | Item | Text |", "| --- | --- | --- |",
             f"| Title and abstract | Inclusion rule | {' '.join(s['rule'].split())} |"]
    lines += [f"| Title and abstract | Criterion: {m['id']} | {' '.join(m['question'].split())} |"
              for m in s.get("metrics") or []]
    if e.get("rule"):
        lines.append(f"| Eligibility | Rule | {' '.join(e['rule'].split())} |")
        lines += [f"| Eligibility | Criterion: {m['id']} | {' '.join(m['question'].split())} |"
                  for m in e.get("metrics") or []]
    return "\n".join(lines)


def recall_table(store: Store, run_id: int, protocol: dict, label: str) -> str:
    """The recall test: per known source, how many of its references the search found, and the relevant ones it
    missed."""
    from . import recall
    try:
        s = recall.summary(store, run_id, protocol)
    except ValueError:
        return ""
    if not s["references"]:
        return ""
    cite = {b["id"]: b["cite"] for b in protocol.get("background") or []}
    lines = [f"**{label}.** Recall test: the references of the consensus statements and prior reviews, retrieved on "
             f"{s['date']} ({s['references']} distinct records), by whether the search found them. Relevant found: the "
             "review's own title-and-abstract screening kept it; relevant missed: a reference the search did not find "
             "that at least one of the two screening agents included on the same criteria; unsure: an agent could not "
             "tell (mostly records without an abstract).", "",
             "| Source | References | Found by the search | Relevant found | Retained | Core | Relevant missed |",
             "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"]
    for b, d in s["per_source"].items():
        lines.append(f"| {cite.get(b, b)} | {d['references']} | {d['found']} | {d['relevant_found']} | {d['retained']} | "
                     f"{d['core']} | {d['relevant_missed']} |")
    lines.append(f"| All (distinct) | {s['references']} | {sum(v for k, v in s['found'].items() if k != 'not_found')} | "
                 f"{s['relevant_found']} | {s['relevant_found_retained']} | {s['relevant_found_core']} | "
                 f"{s['relevant_missed']} |")
    if s["missed_ids"] or s["unsure_ids"]:
        meta = works(store, s["missed_ids"] + s["unsure_ids"])
        tree = __import__("ichnos.query", fromlist=["parse"]).parse(" ".join(protocol["query"].split()))
        from .query import matches
        lines += ["", "Relevant or unsure references the search did not find:", "",
                  "| Record | Year | Title | Agents | Search phrase in title or abstract |", "| --- | ---: | --- | --- | --- |"]
        for w in s["missed_ids"] + s["unsure_ids"]:
            m = meta[w]
            lines.append(f"| {m['cite']} | {m['year'] or 'n.d.'} | {_clip(m['title'], 110)} | "
                         f"{'include' if w in s['missed_ids'] else 'unsure'} | "
                         f"{'yes' if matches(tree, (m['title'] or '') + ' ' + (m['abstract'] or '')) else 'no'} |")
    return "\n".join(lines)
