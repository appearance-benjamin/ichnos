"""Pilot report, rebuilt entirely from the evidence store so it can be regenerated at any time."""

from __future__ import annotations

import csv
import json
import math
import tomllib
from collections import Counter
from pathlib import Path

from . import agreement, fulltext, insights, mapping, query, signals
from .review import latest_protocol, write_review
from .store import Store, normalize_doi, work_link

DISAGREEMENT_COLUMNS = ["screener_a", "screener_b", "work_id", "title", "link",
                        "a_decision", "a_reason", "a_note", "b_decision", "b_reason", "b_note"]


def refresh(store: Store, run_id: int, root: Path | None = None, out: Path | None = None) -> Path:
    """Rebuild a run's report and review page, and the corpus's insight log."""
    from .config import ROOT
    root = root or ROOT
    run = store.db.execute("SELECT corpus FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    out = out or root / "output" / f"corpus_{run['corpus']}" / f"run_{run_id:03d}" / "report.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    write_report(store, run_id, out)
    insights.write_log(store, run["corpus"], root / "notes" / f"insights_corpus_{run['corpus']}.md")
    from . import paper, writer
    d = writer.latest(store, run_id)
    if d:
        writer.write_markdown(store, d["draft_id"], root)
    for p in paper.overview(store, run_id):
        paper.write_files(store, p["id"], root)
    return out


def write_report(store: Store, run_id: int, path: Path) -> None:
    path.write_text(build_report(store, run_id), encoding="utf-8")
    rows = disagreement_rows(store, run_id)
    target = path.parent / "disagreements.csv"
    if rows:
        with target.open("w", encoding="utf-8-sig", newline="") as fh:
            writer = csv.writer(fh, delimiter=";")
            writer.writerow(DISAGREEMENT_COLUMNS)
            writer.writerows(rows)
    elif target.exists():
        target.unlink()
    write_review(store, run_id, path.parent / "review.html")


def build_report(store: Store, run_id: int) -> str:
    db = store.db
    run = db.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    proto = db.execute("SELECT * FROM protocols WHERE sha256 = ?", (run["protocol_sha256"],)).fetchone()
    protocol = tomllib.loads(proto["content"])
    tree = query.parse(protocol["query"])
    sources = db.execute("SELECT * FROM run_sources WHERE run_id = ? ORDER BY source", (run_id,)).fetchall()
    found = {}
    for r in db.execute("SELECT work_id, source FROM retrievals WHERE run_id = ? ORDER BY source", (run_id,)):
        found.setdefault(r["work_id"], []).append(r["source"])
    works = [dict(w, found_by="+".join(found[w["work_id"]])) for w in db.execute(
        f"SELECT * FROM works WHERE work_id IN ({','.join('?' * len(found))}) ORDER BY work_id",
        tuple(found))] if found else []
    sample_ids = {r["work_id"] for r in db.execute(
        "SELECT work_id FROM samples WHERE run_id = ? AND purpose = 'pilot_screening'", (run_id,))}
    calls = db.execute("""SELECT sum(kind = 'http') AS n, sum(kind = 'http' AND status = 200) AS ok,
                                 sum(kind = 'llm') AS llm FROM calls WHERE run_id = ?""", (run_id,)).fetchone()

    texts = {w["work_id"]: f"{w['title'] or ''} {w['abstract'] or ''}" for w in works}
    local = {wid: query.matches(tree, t) for wid, t in texts.items()}
    n = len(works)
    out = []
    add = out.append

    add(f"# Corpus {run['corpus']} pilot: run {run_id}\n")
    add(f"{protocol.get('title', '')}  ")
    add(f"Started {run['started_at']} UTC, status **{run['status']}**. "
        f"Protocol {protocol.get('version', '?')} (`{proto['path']}`, sha256 `{proto['sha256'][:12]}`). "
        f"{calls['n'] or 0} HTTP calls logged, {calls['ok'] or 0} succeeded"
        + (f"; {calls['llm']} model calls logged" if calls["llm"] else "") + ".\n")

    add("## Search strings as sent\n")
    for s in sources:
        add(f"**{s['source']}** (titles and abstracts)\n\n```\n{s['query']}\n```\n")
        if s["notes"]:
            add("> " + s["notes"].replace("\n", "  \n> ") + "\n")

    only = Counter(w["found_by"] for w in works if "+" not in w["found_by"])
    add("## Hits\n")
    add("| Source | Reported total | Fetched | Only this source | Status |")
    add("| --- | ---: | ---: | ---: | --- |")
    for s in sources:
        status = s["status"] + (f": {s['error'][:160]}" if s["error"] else "")
        add(f"| {s['source']} | {_num(s['reported_total'])} | {s['fetched']} | {only.get(s['source'], 0)} | "
            f"{_cell(status)} |")
    add("")
    overlap = Counter(w["found_by"] for w in works)
    add(f"**{n} unique records** after merging on DOI, PubMed ID, source IDs, and title plus year.\n")
    add("| Found by | Records |")
    add("| --- | ---: |")
    for key, count in overlap.most_common():
        add(f"| {key} | {count} |")
    add("")
    earlier_runs = [r["run_id"] for r in db.execute(
        "SELECT run_id FROM runs WHERE corpus = ? AND run_id < ? ORDER BY run_id", (run["corpus"], run_id))]
    if earlier_runs:
        seen_before = {r["work_id"] for r in db.execute(
            f"SELECT DISTINCT work_id FROM retrievals WHERE run_id IN ({','.join('?' * len(earlier_runs))})",
            tuple(earlier_runs))}
        fresh = [w for w in works if w["work_id"] not in seen_before]
        add(f"**{len(fresh)} {'record is' if len(fresh) == 1 else 'records are'} new**: "
            f"no earlier run of corpus {run['corpus']} "
            f"(runs {', '.join(map(str, earlier_runs))}) retrieved them.\n")
        if fresh:
            add("| Found by | New records |")
            add("| --- | ---: |")
            for key, count in Counter(w["found_by"] for w in fresh).most_common():
                add(f"| {key} | {count} |")
            add("")

    add("## Record quality\n")
    with_abstract = sum(1 for w in works if w["abstract"])
    with_doi = sum(1 for w in works if w["doi"])
    with_oa = sum(1 for w in works if w["oa_url"])
    add(f"- Abstract available: {with_abstract} of {n} ({_pct(with_abstract, n)}). "
        "Records without one can only be screened on title, or after fetching the full text.")
    add(f"- DOI: {with_doi} of {n} ({_pct(with_doi, n)}); open-access link: {with_oa} of {n} ({_pct(with_oa, n)}).")
    misses = [w for w in works if not local[w["work_id"]]]
    add(f"- Local re-check: {len(misses)} of {n} records were returned by a source although their stored "
        "title and abstract do not satisfy the string. Usually a missing abstract or a different tokenizer; "
        "if many come from one source, that source's matching is looser than the protocol says.")
    if misses:
        add("")
        add("  | work_id | Found by | Abstract? | Title |")
        add("  | ---: | --- | --- | --- |")
        for w in misses[:15]:
            add(f"  | {w['work_id']} | {w['found_by']} | {'yes' if w['abstract'] else 'no'} | {_cell(w['title'], 110)} |")
        if len(misses) > 15:
            add(f"  | | | | ... and {len(misses) - 15} more |")
    add("")

    add("## Term diagnostics\n")
    add("For each concept block of the string: how many of the merged records contain each term, and how many "
        "contain no other term of that block (the records you would lose by dropping the term). "
        "Counted on title and abstract, so terms in text a source did not return are missed.\n")
    matched = [wid for wid, ok in local.items() if ok]
    for i, block in enumerate(query.blocks(tree), 1):
        block_terms = query.terms(block)
        hits = {t: {wid for wid in matched if t.regex().search(texts[wid])} for t in block_terms}
        add(f"**Block {i}**\n")
        add("| Term | Records | Only this term |")
        add("| --- | ---: | ---: |")
        for t in block_terms:
            others = set().union(*(hits[o] for o in block_terms if o != t)) if len(block_terms) > 1 else set()
            add(f"| `{t.label}` | {len(hits[t])} | {len(hits[t] - others)} |")
        zero = [t.label for t in block_terms if not hits[t]]
        if zero:
            add(f"\nNo records contain: {', '.join(f'`{z}`' for z in zero)}.")
        add("")

    add("## Publication years\n")
    years = Counter(_bin(w["year"]) for w in works)
    add("| Years | Records |")
    add("| --- | ---: |")
    for key in sorted(years, key=lambda k: (k == "unknown", k)):
        add(f"| {key} | {years[key]} |")
    add("")

    add("## Most frequent venues\n")
    venues = Counter(w["venue"] for w in works if w["venue"])
    add("| Venue | Records |")
    add("| --- | ---: |")
    for venue, count in venues.most_common(12):
        add(f"| {_cell(venue, 90)} | {count} |")
    add(f"\n{sum(1 for w in works if not w['venue'])} records have no venue.\n")

    seeds = [normalize_doi(d) for d in (protocol.get("seeds") or {}).get("dois", []) if d]
    add("## Seed recall\n")
    if seeds:
        found = {w["doi"] for w in works if w["doi"]}
        missed = [d for d in seeds if d not in found]
        add(f"{len(seeds) - len(missed)} of {len(seeds)} seed papers found.")
        for d in missed:
            add(f"- missed: https://doi.org/{d}")
    else:
        add("No seed DOIs in the protocol yet. Add papers you know belong in this corpus under `[seeds]`; "
            "the next run lists the ones the string misses.")
    add("")

    add("## Screening\n")
    on_earlier = {r["work_id"] for r in db.execute(
        """SELECT s.work_id FROM samples s JOIN runs r ON r.run_id = s.run_id
           WHERE r.corpus = ? AND s.purpose = 'pilot_screening' AND s.run_id < ?""", (run["corpus"], run_id))}
    pool_ids = {w["work_id"] for w in works}
    unscreened = pool_ids - on_earlier
    if sample_ids == pool_ids:
        sheet_note = "the whole pool, so precision below is exact for this string"
    elif sample_ids == unscreened:
        sheet_note = (f"every record not already on an earlier sheet of this corpus. The other "
                      f"{len(pool_ids & on_earlier)} keep the decisions made on those sheets, so precision "
                      "below covers only the records added in this run")
    else:
        sheet_note = f"a seeded random draw of {len(sample_ids)} from the {len(unscreened)} not on an earlier sheet"
    add(f"The sheet holds {len(sample_ids)} records: {sheet_note}.\n")
    screening = db.execute("SELECT * FROM screening WHERE run_id = ? AND stage != 'full_text'", (run_id,)).fetchall()
    if not screening:
        add("No decisions imported yet.\n")
    for screener in sorted({s["screener"] for s in screening}):
        rows = [s for s in screening if s["screener"] == screener]
        add(f"**{screener}**: {len(rows)} of {len(sample_ids)} screened.\n")
        add("| Found by | Include | Exclude | Unsure | Precision (95% CI) |")
        add("| --- | ---: | ---: | ---: | --- |")
        by_source = {w["work_id"]: w["found_by"] for w in works}
        groups = {"all": rows}
        for key in sorted({by_source.get(r["work_id"]) for r in rows} - {None}):
            groups[key] = [r for r in rows if by_source.get(r["work_id"]) == key]
        for key, group in groups.items():
            c = Counter(r["decision"] for r in group)
            add(f"| {key} | {c['include']} | {c['exclude']} | {c['unsure']} | "
                f"{_precision(c['include'], c['include'] + c['exclude'])} |")
        reasons = Counter(r["reason"] for r in rows if r["decision"] == "exclude")
        if reasons:
            add("\nExclusion reasons: " + ", ".join(f"{k} {v}" for k, v in reasons.most_common()) + ".")
        add("\nPrecision counts include against include plus exclude; unsure records are left out "
            "until the full-text stage settles them.\n")

    add("## Agreement between screeners\n")
    by_screener, pairs = agreement.complete_pairs(screening, sample_ids)
    if not pairs:
        progress = ", ".join(f"{s} {len(r)} of {len(sample_ids)}" for s, r in sorted(by_screener.items()))
        add("Shown once at least two screeners have decided every record on the sheet, so a partial import "
            f"never reveals another screener's decisions. Progress: {progress or 'no decisions yet'}.\n")
    for p in pairs:
        table = p.table()
        add(f"**{p.a}** (rows) against **{p.b}** (columns), {p.n} records\n")
        add("| | include | unsure | exclude |")
        add("| --- | ---: | ---: | ---: |")
        for x in agreement.LEVELS:
            add(f"| {x} | " + " | ".join(str(table[(x, y)]) for y in agreement.LEVELS) + " |")
        both_excluded = [w for w, (x, y) in zip(p.work_ids, p.labels) if x == y == "exclude"]
        other_reason = sum(1 for w in both_excluded
                           if by_screener[p.a][w]["reason"] != by_screener[p.b][w]["reason"])
        add("")
        add(f"- Agreement {p.observed:.0%}. Cohen's κ {_kappa(p.kappa())} on the three decisions, "
            f"{_kappa(p.kappa(collapse_unsure=True))} on keep (include or unsure) against exclude.")
        add(f"- {p.b} excluded {len(p.dropped_by(p.b))} records that {p.a} kept; "
            f"{p.a} excluded {len(p.dropped_by(p.a))} that {p.b} kept. These matter most: an excluded record "
            "never reaches full-text screening.")
        add(f"- Both excluded {len(both_excluded)}, giving different reasons for {other_reason}.")
        add("- Every disagreement, with both screeners' reasons and notes: `disagreements.csv` next to this report.\n")

    latest = latest_protocol(store, run["corpus"]) or protocol
    _signals_section(add, store, sample_ids, latest, screening)
    _metrics_section(add, store, run_id, latest)
    _fulltext_section(add, store, sample_ids)
    _extractor_section(add, store, run_id, latest)
    _map_section(add, store, run_id, latest)
    _findings_section(add, store, run_id, latest)
    _insights_section(add, store, run["corpus"], run_id)
    _papers_section(add, store, run_id)

    rules = protocol.get("screening", {})
    add("## How to screen this sheet\n")
    add("The easiest way is `review.html` in this folder: open it in your browser, decide each record there, "
        "and export your decisions. The CSV sheet below still works too.\n")
    add("Open `screening_sheet.csv` in Excel. For each row, fill `decision` with "
        f"{' / '.join(rules.get('decisions', []))}; for exclude, also fill `exclude_reason` with one of "
        f"{', '.join(f'`{r}`' for r in rules.get('exclude_reasons', []))}. "
        "Save as CSV (either CSV UTF-8 or plain CSV works), then run:\n")
    add(f"```\npython -m psyscientist import-screening output/corpus_{run['corpus']}/run_{run_id:03d}/screening_sheet.csv\n```\n")
    add("Rule from the protocol:\n")
    add("> " + " ".join(rules.get("rule", "").split()) + "\n")
    return "\n".join(out)


def _signals_section(add, store: Store, sample_ids: set[int], protocol: dict, screening) -> None:
    if not sample_ids:
        return
    lex = signals.lexicons(protocol)
    rows = store.db.execute(f"SELECT work_id, title, abstract FROM works WHERE work_id IN "
                            f"({','.join('?' * len(sample_ids))})", tuple(sample_ids)).fetchall()
    info = {r["work_id"]: signals.summary(r["title"], r["abstract"], lex) for r in rows}
    levels = Counter(i["level"] for i in info.values())
    add("## Trajectory signal\n")
    add("A word-list hint on every record of the sheet, the same in any domain: does the title or abstract "
        "name a way of modelling change, and does it speak of subgroups or moderators? It never decides; "
        "on the review page the words are highlighted.\n")
    add("| Signal | Records | Means |")
    add("| --- | ---: | --- |")
    for lvl in signals.LEVELS:
        add(f"| {lvl} | {levels.get(lvl, 0)} | {signals.LEVEL_HELP[lvl]} |")
    xs = sum(1 for i in info.values() if i["cross_sectional"])
    add(f"\n{xs} records use cross-sectional wording.")
    models = {}
    for s in screening:
        if not s["screener"].startswith("human:"):
            models.setdefault(s["work_id"], []).append(s["decision"])
    if models:
        misses = [w for w, i in info.items() if i["level"] == "strong" and models.get(w)
                  and all(d == "exclude" for d in models[w])]
        add(f"{len(misses)} records with a strong signal were excluded by every agent: check them under "
            "*Possible misses* on the review page.")
    add("")


def _metrics_section(add, store: Store, run_id: int, protocol: dict) -> None:
    rows = store.db.execute("SELECT * FROM screening_scores WHERE run_id = ? AND metric NOT LIKE 'eligibility:%'",
                            (run_id,)).fetchall()
    if not rows:
        return
    order = [m["id"] for m in (protocol.get("screening") or {}).get("metrics", [])]
    add("## Metric scores\n")
    add("Per metric screener: how often each metric was met (2), unclear (1) or not met (0), and how many of "
        "the quotes behind the scores occur word for word in the record. An unfound quote is a claim without "
        "evidence.\n")
    for screener in sorted({r["screener"] for r in rows}):
        mine = [r for r in rows if r["screener"] == screener]
        add(f"**{screener}**\n")
        add("| Metric | 2 met | 1 unclear | 0 not met | Quotes found verbatim |")
        add("| --- | ---: | ---: | ---: | --- |")
        for metric in order + sorted({r["metric"] for r in mine} - set(order)):
            m = [r for r in mine if r["metric"] == metric]
            if not m:
                continue
            c = Counter(r["score"] for r in m)
            checked = [r for r in m if r["grounded"] is not None]
            ok = sum(r["grounded"] for r in checked)
            add(f"| {metric} | {c[2]} | {c[1]} | {c[0]} | {ok} of {len(checked)} ({_pct(ok, len(checked))}) |")
        add("")


def _fulltext_section(add, store: Store, sample_ids: set[int]) -> None:
    rows = fulltext.summary_rows(store, sorted(sample_ids))
    if not rows:
        return
    add("## Full texts\n")
    add("Open-access copies only (Europe PMC XML, then Unpaywall and OpenAlex PDFs); files stay in "
        "`fulltext/`, the store keeps source, licence, version and SHA-256.\n")
    add("| Status | Source | Parser | Records |")
    add("| --- | --- | --- | ---: |")
    for (status, source, parser), n in Counter((r["status"], r["source"] or "", r["parser"] or "")
                                               for r in rows).most_common():
        add(f"| {status} | {source} | {parser} | {n} |")
    licences = Counter(r["license"] or "not stated" for r in rows if r["status"] == "parsed")
    if licences:
        add("\nLicences of the parsed copies: " + ", ".join(f"{k} {v}" for k, v in licences.most_common()) + ".")
    failed = [r for r in rows if r["status"] == "failed"]
    if failed:
        add(f"\n{len(failed)} copies were found but could not be fetched or read, usually because the "
            "publisher served a web page instead of the PDF. Their errors are in the `fulltexts` table.")
    add("")


def _extractor_section(add, store: Store, run_id: int, protocol: dict) -> None:
    """When a run was extracted more than once (e.g. abstracts only, then with full texts), compare them."""
    rows = store.db.execute("SELECT work_id, extractor, dimension, canonical FROM study_features WHERE run_id = ?",
                            (run_id,)).fetchall()
    extractors = sorted({r["extractor"] for r in rows})
    if len(extractors) < 2:
        return
    dims = [d["name"] for d in (protocol.get("map") or {}).get("dimensions", [])]
    by = {(r["extractor"], r["work_id"], r["dimension"]): r["canonical"] for r in rows}
    common = sorted(set.intersection(*({r["work_id"] for r in rows if r["extractor"] == e} for e in extractors)))
    has_ft = {r["work_id"] for r in fulltext.summary_rows(store, common) if r["status"] == "parsed"}
    add("## Extractions compared\n")
    add(f"{len(common)} studies were extracted by each of {', '.join(extractors)}. Share of dimensions left "
        f"not_reported, for the {len(has_ft)} studies with a parsed full text and the {len(common) - len(has_ft)} "
        "without:\n")
    add("| Dimension | " + " | ".join(f"{e} (full text / none)" for e in extractors) + " |")
    add("| --- |" + " --- |" * len(extractors))
    for d in dims:
        cells = []
        for e in extractors:
            def share(ids):
                vals = [by.get((e, w, d)) for w in ids]
                return f"{sum(v == 'not_reported' for v in vals)}/{len(vals)}" if ids else "-"
            cells.append(f"{share(sorted(has_ft))} / {share([w for w in common if w not in has_ft])}")
        add(f"| {d} | " + " | ".join(cells) + " |")
    add("\nThe evidence map below uses the most recent extraction.\n")


def _map_section(add, store: Store, run_id: int, protocol: dict) -> None:
    m = mapping.build(store, run_id, protocol)
    if m is None:
        return
    add("## Evidence map\n")
    add(f"{len(m.labels)} kept studies, extracted by {mapping.latest_extractor(store, run_id)} from titles and "
        f"abstracts. Tree: {' → '.join(m.tree_dims)}. The review page shows the same map interactively.\n")

    def walk(node, depth):
        for label, child in sorted(node.items()):
            if label == "__studies__":
                continue
            n = _count(child)
            add(f"{'  ' * depth}- **{m.tree_dims[depth]}**: {label} ({n})")
            walk(child, depth + 1)
    walk(m.tree, 0)
    add("")
    if m.across:
        add(f"**Matches across {m.across}** (same {', '.join(m.match_on)}): {len(m.matches)}\n")
        for x in m.matches:
            on = ", ".join(f"{k} {v}" for k, v in x["on"].items())
            within = " (within one study)" if x.get("n_studies") == 1 else ""
            add(f"- {on}{within}: " + "; ".join(f"{a} #{', #'.join(map(str, ids))}" for a, ids in x["studies"].items()))
        add(f"\n**Gaps**: {len(m.gaps)} comparable groups lack studies in some {m.across} categories.\n")
        for g in m.gaps[:25]:
            on = ", ".join(f"{k} {v}" for k, v in g["on"].items())
            has = ", ".join(f"{a} ({n})" for a, n in g["present"].items())
            add(f"- {on}: has {has}; none for {', '.join(g['missing'])}")
        if len(m.gaps) > 25:
            add(f"- ... and {len(m.gaps) - 25} more on the review page")
        for dim, mx in m.matrix.items():
            add(f"\n**{m.across} × {dim}** (studies; 0 is a coarse gap)\n")
            add("| | " + " | ".join(mx["cols"]) + " |")
            add("| --- |" + " ---: |" * len(mx["cols"]))
            for r in mx["rows"]:
                add(f"| {r} | " + " | ".join(str(mx["cells"][r].get(c, 0)) for c in mx["cols"]) + " |")
        add("")
    if m.claims:
        add("**Claims**\n")
        for c in m.claims:
            links = "; ".join(f"{rel}: #{', #'.join(str(x['work_id']) for x in ls)}" for rel, ls in c["links"].items())
            add(f"- `{c['id']}` {c['claim']} — {links or 'no study linked yet'}")
        add("")
    add("**Vocabulary** (merge synonyms under `[map.aliases]`):\n")
    for dim, counts in m.vocabulary.items():
        add(f"- {dim}: " + ", ".join(f"{v} ({n})" for v, n in counts.most_common()))
    add("")


def latest_findings(store: Store, run_id: int) -> list:
    row = store.db.execute("SELECT extractor FROM findings WHERE run_id = ? ORDER BY created_at DESC LIMIT 1",
                           (run_id,)).fetchone()
    if row is None:
        return []
    return store.db.execute("SELECT * FROM findings WHERE run_id = ? AND extractor = ? AND status != 'superseded' "
                            "ORDER BY work_id, finding_id", (run_id, row["extractor"])).fetchall()


def _findings_section(add, store: Store, run_id: int, protocol: dict) -> None:
    rows = latest_findings(store, run_id)
    if not rows:
        return
    spec = protocol.get("findings") or {}
    cat_fields = [f["name"] for f in spec.get("fields") or [] if f.get("categories")]
    axes = [a for a in spec.get("matrix") or cat_fields[:2] if a in cat_fields][:2]
    fields = {r["finding_id"]: json.loads(r["fields"] or "{}") for r in rows}
    studies = sorted({r["work_id"] for r in rows})
    add("## Findings\n")
    add(f"{len(rows)} findings from {len(studies)} studies, extracted by {rows[0]['extractor']}. Each row is "
        "one result with the passage that states it; these rows are what a writer may cite.\n")
    src = Counter(r["source"] or "quote not found" for r in rows)
    add("Where the quotes were found: " + ", ".join(f"{k} {v}" for k, v in src.most_common()) + ".\n")
    if len(axes) == 2:
        a, b = axes
        cells = Counter((fields[r["finding_id"]].get(a, "?"), fields[r["finding_id"]].get(b, "?")) for r in rows)
        rows_a = sorted({k[0] for k in cells})
        cols_b = sorted({k[1] for k in cells})
        add(f"**{a} × {b}** (findings)\n")
        add("| | " + " | ".join(cols_b) + " |")
        add("| --- |" + " ---: |" * len(cols_b))
        for x in rows_a:
            add(f"| {x} | " + " | ".join(str(cells.get((x, y), 0)) for y in cols_b) + " |")
        add("")
    add("| # | Study | " + " | ".join(cat_fields) + " | For whom | Finding | From |")
    add("| ---: | ---: |" + " --- |" * len(cat_fields) + " --- | --- | --- |")
    for r in rows[:80]:
        f = fields[r["finding_id"]]
        add(f"| {r['finding_id']} | #{r['work_id']} | " + " | ".join(_cell(str(f.get(c, "")), 30) for c in cat_fields)
            + f" | {_cell(r['for_whom'], 60)} | {_cell(r['finding'], 180)} | {r['source'] or 'not found'} |")
    if len(rows) > 80:
        add(f"| | | {len(rows) - 80} more on the review page |")
    add("")


def _insights_section(add, store: Store, corpus: str, run_id: int) -> None:
    items = insights.rows(store, corpus)
    if not items:
        return
    add("## Insights\n")
    add(f"Things noticed along the way, by agents and by you: {len(items)} for corpus {corpus}. The full log, "
        f"with evidence and history, is `notes/insights_corpus_{corpus}.md`.\n")
    add("| # | Kind | Status | Statement | Rests on | From |")
    add("| ---: | --- | --- | --- | --- | --- |")
    for it in items:
        sup = json.loads(it["support"] or "{}")
        rests = ", ".join([f"#{w}" for w in sup.get("works", [])][:8]
                          + [f"finding {f}" for f in sup.get("findings", [])][:6])
        if sup.get("unverified_numbers"):
            rests += f" ⚠ numbers not in cited findings: {', '.join(sup['unverified_numbers'][:6])}"
        add(f"| {it['insight_id']} | {it['kind']} | {it['status']} | {_cell(it['statement'], 200)} | {rests} | "
            f"{it['author']}, {it['stage']}{', run ' + str(it['run_id']) if it['run_id'] else ''} |")
    add("")


def _papers_section(add, store: Store, run_id: int) -> None:
    from . import paper
    versions = paper.overview(store, run_id)
    if not versions:
        return
    add("## Manuscripts\n")
    for v in versions:
        t = v["verification"]
        add(f"**Paper {v['id']}, version {v['version']}**: {v['title']} (`{v['file']}`). {t.get('verified', 0)} of "
            f"{t.get('sentences', 0)} sentences verified, {t.get('flagged', 0)} flagged, {t.get('words', 0)} words.\n")
        peers = [r for r in v["reviews"] if r["kind"] == "peer"]
        if peers:
            add("| Reviewer | " + " | ".join(paper.RUBRIC) + " | Overall | Decision |")
            add("| --- |" + " ---: |" * (len(paper.RUBRIC) + 1) + " --- |")
            for r in peers:
                c = r["content"]
                add(f"| {r['reviewer']} | " + " | ".join(str(c.get(k, "")) for k in paper.RUBRIC)
                    + f" | {c.get('overall', '')} | {c.get('decision', '')} |")
            add("")
        for r in v["reviews"]:
            if r["kind"] == "audit":
                add("Evidence audit (" + r["reviewer"] + "): " + ", ".join(f"{k} {n}" for k, n in r["content"]["counts"].items()) + ".")
            if r["kind"] == "meta":
                add(f"Editor, round {r['round']} ({r['reviewer']}): **{r['decision']}**. "
                    f"{_cell(r['content'].get('summary'), 600)}")
        add("")


def _count(node) -> int:
    return len(node.get("__studies__", [])) + sum(_count(v) for k, v in node.items() if k != "__studies__")


def disagreement_rows(store: Store, run_id: int) -> list[list]:
    db = store.db
    sample_ids = {r["work_id"] for r in db.execute(
        "SELECT work_id FROM samples WHERE run_id = ? AND purpose = 'pilot_screening'", (run_id,))}
    screening = db.execute("SELECT * FROM screening WHERE run_id = ? AND stage != 'full_text'", (run_id,)).fetchall()
    by, pairs = agreement.complete_pairs(screening, sample_ids)
    rows = []
    for p in pairs:
        for w, (x, y) in zip(p.work_ids, p.labels):
            if x == y:
                continue
            work = db.execute("SELECT * FROM works WHERE work_id = ?", (w,)).fetchone()
            a, b = by[p.a][w], by[p.b][w]
            rows.append([p.a, p.b, w, " ".join((work["title"] or "").split()), work_link(work),
                         x, a["reason"] or "", a["note"] or "", y, b["reason"] or "", b["note"] or ""])
    return rows


def _kappa(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def _precision(k: int, n: int) -> str:
    if n == 0:
        return "n/a"
    p = k / n
    z = 1.96
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return f"{p:.0%} ({max(0, centre - half):.0%} to {min(1, centre + half):.0%})"


def _bin(year: int | None) -> str:
    if not year:
        return "unknown"
    start = year - year % 5
    return f"{start}-{start + 4}"


def _pct(k: int, n: int) -> str:
    return f"{k / n:.0%}" if n else "n/a"


def _num(value) -> str:
    return f"{value:,}" if isinstance(value, int) else "n/a"


def _cell(text: str | None, limit: int = 200) -> str:
    text = " ".join((text or "").split()).replace("|", "\\|")
    return text if len(text) <= limit else text[: limit - 1] + "…"
