"""The review page: one self-contained HTML file per run, opened straight from the folder.

It shows every record on the run's sheet with its signals highlighted, each
agent's decision and metric scores (with the quotes behind them), the
extracted features, and the evidence map. You record your own decisions in the
page; they stay in your browser until you export them as a CSV that
`import-screening` reads. Blind mode hides every agent judgement.
"""

from __future__ import annotations

import json
import tomllib
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from . import agreement, checks, insights, mapping, signals, writer
from .store import Store, work_link


def latest_protocol(store: Store, corpus: str) -> dict:
    row = store.db.execute("SELECT content FROM protocols WHERE corpus = ? ORDER BY first_used DESC, rowid DESC "
                           "LIMIT 1", (corpus,)).fetchone()
    return tomllib.loads(row["content"]) if row else {}


def build_data(store: Store, run_id: int, reviewer: str | None = None, root: Path | None = None) -> dict:
    """Everything the page shows for one run. With `reviewer`, "your call" is that person's saved decisions."""
    db = store.db
    run = db.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    run_protocol = tomllib.loads(db.execute("SELECT content FROM protocols WHERE sha256 = ?",
                                            (run["protocol_sha256"],)).fetchone()["content"])
    protocol = latest_protocol(store, run["corpus"]) or run_protocol
    lex = signals.lexicons(protocol)
    lex_names = list(lex)

    sample = [r[0] for r in db.execute(
        "SELECT work_id FROM samples WHERE run_id = ? AND purpose = 'pilot_screening' ORDER BY work_id", (run_id,))]
    found = {}
    for r in db.execute("SELECT work_id, source FROM retrievals WHERE run_id = ? ORDER BY source", (run_id,)):
        found.setdefault(r["work_id"], []).append(r["source"])
    screening = db.execute("SELECT * FROM screening WHERE run_id = ? AND stage != 'full_text'", (run_id,)).fetchall()
    scores = db.execute("SELECT * FROM screening_scores WHERE run_id = ? AND metric NOT LIKE 'eligibility:%'",
                        (run_id,)).fetchall()
    extractor = mapping.latest_extractor(store, run_id)
    features = db.execute("SELECT * FROM study_features WHERE run_id = ? AND extractor = ?",
                          (run_id, extractor)).fetchall() if extractor else []

    calls: dict[int, dict] = {}
    human: dict[int, dict] = {}
    for s in screening:
        entry = {"d": s["decision"], "r": s["reason"], "n": s["note"]}
        if s["screener"].startswith("human:"):
            if reviewer is None or s["screener"] == f"human:{reviewer}":
                human[s["work_id"]] = entry
        else:
            calls.setdefault(s["work_id"], {})[s["screener"]] = entry
    for s in scores:
        call = calls.get(s["work_id"], {}).get(s["screener"])
        if call is not None:
            call.setdefault("s", {})[s["metric"]] = [s["score"], s["evidence"], s["grounded"]]
    fulltexts = {r["work_id"]: {"source": r["source"], "parser": r["parser"], "url": r["url"],
                                "license": r["license"]}
                 for r in db.execute("SELECT * FROM fulltexts WHERE status = 'parsed'")}
    results: dict[int, list] = {}
    for f in _latest_findings(store, run_id):
        results.setdefault(f["work_id"], []).append({
            "id": f["finding_id"], "finding": f["finding"], "effect": f["effect"], "for_whom": f["for_whom"],
            "fields": json.loads(f["fields"] or "{}"), "evidence": f["evidence"], "location": f["location"],
            "source": f["source"], "grounded": f["grounded"]})
    feats: dict[int, dict] = {}
    for f in features:
        feats.setdefault(f["work_id"], {})[f["dimension"]] = [f["value"], f["canonical"], f["evidence"], f["grounded"]]

    records = []
    for w in db.execute(f"SELECT * FROM works WHERE work_id IN ({','.join('?' * len(sample))})",
                        tuple(sample)) if sample else []:
        sig = signals.summary(w["title"], w["abstract"], lex)
        records.append({
            "id": w["work_id"], "title": w["title"] or "", "abstract": w["abstract"] or "", "year": w["year"],
            "venue": w["venue"] or "", "link": work_link(w), "found_by": found.get(w["work_id"], []),
            "level": sig["level"], "xs": sig["cross_sectional"],
            "hl": {"title": _spans(sig["title"], lex_names), "abstract": _spans(sig["abstract"], lex_names)},
            "calls": calls.get(w["work_id"], {}), "features": feats.get(w["work_id"], {}),
            "ft": fulltexts.get(w["work_id"]), "findings": results.get(w["work_id"], []),
        })
    records.sort(key=lambda r: r["id"])

    model_rows = [s for s in screening if not s["screener"].startswith("human:")]
    _, pairs = agreement.complete_pairs(model_rows, set(sample))
    emap = mapping.build(store, run_id, protocol, extractor)
    rules = run_protocol.get("screening", {})
    interp = interpret_data(store, run_id, root)
    home = overview(store, run_id, protocol, root)
    if interp and interp.get("gates"):
        theory = [g for g in interp["gates"] if g["source"] == "tournament"]
        found = Counter(h["novelty"] for h in interp["hypotheses"] if h["credited"])
        parts = [", ".join(f"{k.replace('_', ' ')} {found[k]}" for k in ("novel", "near", "contested", "prior",
                                                                          "not_searchable", "unjudged", "not_searched")
                           if found.get(k)),
                 f"{sum(len(c.get('cards') or []) for c in interp['cards'])} test cards"]
        judged2 = bool(interp.get("derive_judges"))
        home["stages"].append({
            "stage": "Interpret", "value": sum(g["credited"] for g in theory),
            "of": sum(g["submitted"] for g in theory),
            "label": "hypotheses past gates 1 and 2" if judged2 else "credited (gate 2 not judged yet)",
            "detail": "; ".join(p for p in parts if p), "tab": "interpret"})
    return {
        "run": run_id, "corpus": run["corpus"], "title": protocol.get("title", ""),
        "version": protocol.get("version", ""), "generated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "rule": " ".join(rules.get("rule", "").split()),
        "decisions": rules.get("decisions", ["include", "exclude", "unsure"]),
        "reasons": rules.get("exclude_reasons", []),
        "metrics": [{"id": m["id"], "q": " ".join(m["question"].split())}
                    for m in (protocol.get("screening") or {}).get("metrics", [])],
        "lexicons": lex_names, "levels": signals.LEVEL_HELP,
        "screeners": sorted({s["screener"] for s in model_rows}),
        "human": human, "records": records,
        "agreement": [{"a": p.a, "b": p.b, "n": p.n, "observed": round(p.observed, 3),
                       "kappa": _round(p.kappa()), "kappa_keep": _round(p.kappa(collapse_unsure=True))} for p in pairs],
        "map": _map_data(emap, extractor),
        "finding_fields": [f["name"] for f in (protocol.get("findings") or {}).get("fields", [])],
        "insights": [{"id": i["insight_id"], "kind": i["kind"], "status": i["status"], "statement": i["statement"],
                      "rationale": i["rationale"], "author": i["author"], "stage": i["stage"], "run": i["run_id"],
                      "created": i["created_at"][:10], **json.loads(i["support"] or "{}")}
                     for i in insights.rows(store, run["corpus"])],
        "statuses": insights.STATUS_HELP,
        "draft": _draft_data(store, run_id),
        "papers": _paper_data(store, run_id),
        "check": checks.page_data(store, run_id, protocol),
        "interpret": interp,
        "overview": home,
        "directions": _directions(store, run_id),
        "library": _library_data(store, run_id, protocol),
        "project": _project_info(store, run["corpus"], root),
    }


def _project_info(store: Store, corpus: str, root: Path | None) -> dict:
    """The project this run belongs to, as protocol/projects.toml names it (or by its protocol's title)."""
    from . import projects
    from .config import ROOT
    try:
        return projects.info(store, corpus, root or ROOT)
    except (OSError, ValueError):            # a broken projects file must not stop the page
        return {"name": "", "about": "", "owner": "", "archived": False}


def _library_data(store: Store, run_id: int, protocol: dict) -> dict:
    """The retained records without a full text, the ones that matter most first, for the Full texts page."""
    from . import library
    from .config import ROOT
    from .agents import kept_ids
    from .config import load_settings
    rows = library.todo(store, run_id, protocol, load_settings().library_proxy)
    kept = kept_ids(store, run_id)
    added = sum(1 for w in kept if (lambda f: f and f["status"] == "parsed" and f["source"] == "library")(
        store.get_fulltext(w)))
    groups = {g: library.group_label(g, rows) for g in library.GROUPS}
    from . import abstracts, query
    lacking = set(abstracts.missing(store, run_id, kept))
    try:
        tree = query.parse(protocol["query"])
        databases = [{"platform": d, "name": query.DIALECTS[d].name, "string": query.render(tree, d)}
                     for d in query.LIBRARY_DIALECTS]
    except (KeyError, query.QueryError):
        databases = []
    return {"inbox": str((ROOT / library.INBOX).resolve()), "groups": groups, "added": added,
            "no_abstract": len(lacking), "databases": databases,
            "rows": [{**{k: r[k] for k in ("work_id", "group", "cite", "title", "year", "type", "findings", "links",
                                           "status")}, "abstract": r["work_id"] not in lacking} for r in rows]}


def interpret_data(store: Store, run_id: int, root: Path | None = None) -> dict | None:
    """The lens tournament for the Interpretations page: every hypothesis with its fate at the three gates, the
    literature verdicts with the hits behind them, and the cartographer's test cards. None without a lens file."""
    from . import lenses, tables
    root = root or lenses.ROOT
    corpus = store.db.execute("SELECT corpus FROM runs WHERE run_id = ?", (run_id,)).fetchone()["corpus"]
    path = root / "protocol" / f"lenses_corpus_{corpus.lower()}.toml"
    if not path.exists():
        return None
    try:
        _, _, cfg = lenses.load(corpus, root)
        tour = cfg["interpret"].get("tournament", "pilot")
        cells = {c["cell"]: f"{c['setting']} × {c['contrast']}" for c in lenses.grid(cfg)}
    except Exception as e:              # a typo in the lens file: say so, do not pretend there is none
        return {"file": path.relative_to(root).as_posix(), "lenses": [], "error": f"{type(e).__name__}: {e}"}
    report = root / "notes" / f"interpretation_corpus_{corpus}.md"
    base = {"tournament": tour, "file": path.relative_to(root).as_posix(),
            "report": report.relative_to(root).as_posix() if report.exists() else None, "grid": cells,
            "lenses": [{k: lens.get(k) for k in ("id", "name", "role", "heuristic", "core")}
                       for lens in cfg.get("lenses") or []]}
    if not lenses.outputs(store, run_id, tour, "interpret"):
        return base | {"empty": True}
    try:
        r = lenses.evaluate(store, run_id, root)
    except Exception as e:              # a half-imported task must not take the whole app down: show it instead
        return base | {"error": f"{type(e).__name__}: {e}"}
    order = {lens["id"]: i for i, lens in enumerate(cfg.get("lenses") or [])}
    gates = []
    for key, v in r["hypotheses"].items():
        source, lens = key.split(":", 1)
        if source == "no_card":
            continue
        d = v.get("derivation_passed") or {}            # gate 2 counted over what passed gate 1: rows add up
        lit = v.get("literature") or {}                 # gate 3 counted over what was credited
        gates.append({"source": source, "lens": lens, "submitted": v["submitted"], "judged": v["judged"],
                      "untestable": v["judged"] - v["testable"], "restates": v["restates"],
                      "in_baseline": v["in_baseline"], "gate1": v["credited"],
                      "common_sense": d.get("common_sense", 0), "from_core": d.get("follows_from_core", 0),
                      "with_auxiliary": d.get("needs_auxiliary", 0), "contradicts": d.get("contradicts_core", 0),
                      "disputed": d.get("disputed", 0), "unjudged2": d.get("unjudged", 0), "credited": v["credited_core"],
                      "searched": source == "tournament",
                      "novel": lit.get("novel", 0), "near": lit.get("near", 0), "contested": lit.get("contested", 0),
                      "prior": lit.get("prior", 0),
                      "other": sum(n for k, n in lit.items() if k not in ("novel", "near", "contested", "prior")),
                      "other_kinds": {k: n for k, n in lit.items() if k not in ("novel", "near", "contested", "prior")}})
    gates.sort(key=lambda g: (g["source"] != "tournament", order.get(g["lens"], len(order)), g["lens"]))
    log = sorted(r.get("hypothesis_log") or [], key=lambda h: (order.get(h["lens"], len(order)), h["replicate"] or 0,
                                                                h["output_id"], str(h["id"])))
    fids = {e.get("finding") for c in r["cards"] for card in c.get("cards") or [] for e in card.get("existing_evidence") or []}
    fids |= {f for h in log for f in (h.get("derived_from") or [])} | {h.get("restates") for h in log}
    fids = sorted(f for f in fids if isinstance(f, int))
    findings = {}
    rows = store.db.execute(f"SELECT finding_id, work_id, finding FROM findings WHERE finding_id IN "
                            f"({','.join('?' * len(fids))})", tuple(fids)).fetchall() if fids else []
    works = tables.works(store, sorted({x["work_id"] for x in rows})) if rows else {}
    for x in rows:
        w = works.get(x["work_id"]) or {}
        findings[str(x["finding_id"])] = {"work": x["work_id"], "cite": w.get("cite"), "finding": x["finding"]}
    baseline = r["hypotheses"].get("no_card:no_card")
    pc = r.get("positive_control") or {}
    return base | {
        "gates": gates, "hypotheses": log, "baseline": baseline["submitted"] if baseline else None,
        "judges": r.get("judges") or [], "derive_judges": r.get("derive_judges") or [],
        "judge_agreement": r.get("judge_agreement") or {}, "novelty": r.get("novelty"),
        "cards": r.get("cards") or [], "findings": findings,
        "positive_control": {"passed": pc.get("passed"), "lenses": (pc.get("config") or {}).get("lenses") or [],
                             "variable": (pc.get("config") or {}).get("variable")} if pc.get("passed") is not None else None}


def overview(store: Store, run_id: int, protocol: dict, root: Path | None = None) -> dict:
    """Where the review stands, stage by stage, and what to do next: for the app's Home page."""
    from . import paper, referee, tables
    from .config import ROOT
    from .agents import kept_ids, sheet_ids
    from .checkpack import calibration, core_verdicts
    db = store.db
    sources = db.execute("SELECT source, status, fetched FROM run_sources WHERE run_id = ? ORDER BY source",
                         (run_id,)).fetchall()
    sheet = sheet_ids(store, run_id)
    screened = {r[0] for r in db.execute("SELECT DISTINCT work_id FROM screening WHERE run_id = ? AND stage = "
                                         "'title_abstract'", (run_id,))}
    passed = kept_ids(store, run_id, "title_abstract")
    kept = kept_ids(store, run_id)
    chart = tables.charting(store, run_id)
    rows = tables.finding_rows(store, run_id)
    elig = tables.eligibility(store, run_id, protocol) if tables.criteria(protocol) else []
    meta = tables.works(store, kept)
    parsed = title_only = 0
    for w in kept:
        ft = store.get_fulltext(w)
        if ft and ft["status"] == "parsed":
            parsed += 1
        elif len((meta[w].get("abstract") or "").strip()) <= 50:
            title_only += 1
    paper.ensure(store)
    latest = db.execute("SELECT paper_id, parent_id, version, title, created_at FROM papers WHERE run_id = ? ORDER BY "
                        "paper_id DESC LIMIT 1", (run_id,)).fetchone()
    decision = None
    if latest:
        meta = lambda pid: db.execute("SELECT decision, overall, round FROM reviews WHERE paper_id = ? AND kind = "  # noqa: E731
                                      "'meta' ORDER BY review_id DESC LIMIT 1", (pid,)).fetchone()
        m, polished = meta(latest["paper_id"]), False
        if m is None and latest["parent_id"]:       # a final polish answers the decision on the version before it
            m = meta(latest["parent_id"])
            m = m if m and m["decision"] in ("accept", "minor_revision") else None
            polished = m is not None
        a = db.execute("SELECT content FROM reviews WHERE paper_id = ? AND kind = 'audit' ORDER BY review_id DESC LIMIT 1",
                       (latest["paper_id"],)).fetchone()
        faithful = None
        if a:
            verdicts = [v.get("verdict") for v in json.loads(a["content"]).get("verdicts") or []]
            faithful = {"faithful": verdicts.count("faithful"), "of": len(verdicts)}
        overall = m["overall"] if m else None
        overall = int(overall) if isinstance(overall, float) and overall.is_integer() else overall
        decision = {"decision": m["decision"] if m else None, "overall": overall,
                    "round": m["round"] if m else None, "audit": faithful, "polished": polished}
    chk = checks.page_data(store, run_id, protocol)
    cal, core_chk = calibration(store, run_id), core_verdicts(store, run_id)
    noted = sum(1 for i in insights.rows(store, db.execute("SELECT corpus FROM runs WHERE run_id = ?",
                                                             (run_id,)).fetchone()["corpus"]) if i["status"] == "noted")
    stages = [
        {"stage": "Search", "value": len({r[0] for r in db.execute(
            "SELECT DISTINCT work_id FROM retrievals WHERE run_id = ?", (run_id,))}), "label": "unique records",
         "detail": ", ".join(f"{paper.source_name(r['source'])} {'failed' if r['status'] == 'failed' else r['fetched']}"
                             for r in sources), "tab": "records"},
        {"stage": "Screen", "value": len(screened & set(sheet)), "of": len(sheet), "label": "screened",
         "detail": f"{len(passed)} passed title and abstract", "tab": "records"},
        {"stage": "Select", "value": len(kept), "label": "retained",
         "detail": (f"{sum(e['core'] for e in elig)} core studies" if elig else ""), "tab": "records"},
        {"stage": "Chart", "value": len(set(chart) & set(kept)), "of": len(kept), "label": "charted",
         "detail": f"{parsed} with an open-access full text, {title_only} with a title only", "tab": "map"},
        {"stage": "Extract", "value": len(rows), "label": "findings",
         "detail": f"from {len({r['work_id'] for r in rows})} studies", "tab": "findings"},
        {"stage": "Write", "value": latest["version"] if latest else 0, "label": "manuscript versions" if latest else
         "no manuscript yet", "detail": (latest["title"] if latest else ""), "tab": "paper"},
        {"stage": "Check", "value": (chk or {}).get("summary", {}).get("checked", 0),
         "of": (chk or {}).get("summary", {}).get("headline", 0), "label": "headline findings checked by a person",
         "detail": (f"blind sample screened: {cal['n']}" if cal and cal["n"] else "no blind screening sample yet")
                   + (f"; core studies checked: {len(core_chk)}" if core_chk else ""), "tab": "check"},
    ]
    nxt = []
    if len(screened & set(sheet)) < len(sheet):
        nxt.append({"text": f"{len(sheet) - len(screened & set(sheet))} records on the sheet are not screened yet.",
                    "tab": "agents"})
    # an update search reports its own failures (update --report); this step is about the run's search
    failed = [paper.source_name(r["source"]) for r in sources if r["status"] == "failed" and "@" not in r["source"]]
    # a source counts as searched when this run or another run of the corpus searched it
    searched = {r[0] for r in db.execute(
        "SELECT rs.source FROM run_sources rs JOIN runs r ON r.run_id = rs.run_id WHERE r.corpus = "
        "(SELECT corpus FROM runs WHERE run_id = ?) AND rs.status IN ('ok', 'truncated')", (run_id,))}
    wanted = list(protocol.get("sources") or []) + [e["source"] for e in protocol.get("imports") or [] if e.get("source")]
    missing = [paper.source_name(x) for x in wanted if x not in searched and paper.source_name(x) not in failed]
    if failed or missing:
        both = lambda xs: " and ".join([", ".join(xs[:-1]), xs[-1]] if len(xs) > 1 else xs)
        nxt.append({"text": (f"The {both(failed)} search failed" if failed else "")
                            + ("; " if failed and missing else "")
                            + (f"{both(missing)} {'are' if len(missing) > 1 else 'is'} in the protocol but not "
                               "searched yet" if missing else "")
                            + ". Run it now: new records go on the sheet to be screened (for OpenAlex, the key in .env "
                              "must be set first).", "tab": "records", "job": "sources", "action": "Run the search"})
    from . import directions as directions_mod
    proposed = [d for d in directions_mod.rows(store, run_id) if d["status"] == "proposed"]
    if decision and decision["decision"] in ("accept", "minor_revision"):
        nxt.append({"text": f"The paper is finished: version {latest['version']}, “{latest['title']}” (editor: "
                            f"{decision['decision'].replace('_', ' ')}). Read it.", "tab": "paper"})
    if proposed:
        nxt.append({"text": f"{len(proposed)} directions to explore next are proposed from the "
                            f"{'papers' if len(proposed[0].get('papers') or []) > 1 else 'paper'}. Choose the ones "
                            "worth pursuing (Home, “What to explore next”).", "tab": "home"})
    forward = referee.claims_summary(store, run_id, root or ROOT)
    for p in (forward or {}).get("papers") or []:
        against = [c for c in p["claims"] if c["status"] in ("contradicted", "mixed")]
        if against:
            nxt.append({"text": f"Later studies contradict {len(against)} of the {len(p['claims'])} claims of "
                                f"{p['name']}. Read them under “Forward check of the papers” and decide whether the "
                                "paper needs an update.", "tab": "home"})
    if title_only:
        nxt.append({"text": f"{title_only} retained records have only a title, and others only an abstract. Download "
                            "the full texts through your library, core studies first.", "tab": "fulltext",
                    "optional": True})
    if chk and chk["summary"]["headline"] > chk["summary"]["checked"]:
        nxt.append({"text": f"{chk['summary']['headline'] - chk['summary']['checked']} findings behind the headline "
                            "claims are not checked by a person. Check them yourself, or send a check pack.",
                    "tab": "check", "optional": True})
    if not (cal and cal["n"]) and screened:
        nxt.append({"text": "No person has screened a blind sample, so the agents' accuracy is unknown. A check pack "
                            "includes one.", "tab": "check", "optional": True})
    if decision and decision["decision"] and not decision["polished"]:
        nxt.append({"text": f"Editor's decision on version {latest['version']}: "
                            f"{decision['decision'].replace('_', ' ')}"
                            + (f" (overall {decision['overall']}/10)" if decision["overall"] else "")
                            + ". The required changes are on the Manuscripts page.", "tab": "paper",
                    "optional": decision["decision"] in ("accept", "minor_revision")})
    if noted:
        nxt.append({"text": f"{noted} insights logged by the agents wait for a person's review.", "tab": "insights",
                    "optional": True})
    return {"stages": stages, "next": nxt, "paper": dict(latest) if latest else None, "decision": decision,
            "forward": forward}


def _directions(store: Store, run_id: int) -> list[dict]:
    from . import directions
    return directions.rows(store, run_id)


def _paper_data(store: Store, run_id: int) -> list[dict]:
    from . import paper
    out = []
    for v in paper.overview(store, run_id):
        out.append(dict(v, text=paper.render(store, v["id"], full=True)))
    return out


def _draft_data(store: Store, run_id: int) -> dict | None:
    d = writer.latest(store, run_id)
    if d is None:
        return None
    content, ver = json.loads(d["content"]), json.loads(d["verification"] or "{}")
    ids = sorted({c for p in content["paragraphs"] for s in p["sentences"] for c in s.get("cites") or []})
    cited = {}
    for r in store.db.execute(
            f"""SELECT f.finding_id, f.work_id, f.evidence, f.source, w.authors, w.year FROM findings f
                JOIN works w ON w.work_id = f.work_id WHERE f.finding_id IN ({','.join('?' * len(ids))})""",
            tuple(ids)) if ids else []:
        cited[str(r["finding_id"])] = {"work": r["work_id"], "cite": writer.cite_label(r["authors"], r["year"]),
                                       "evidence": r["evidence"], "source": r["source"]}
    return {"id": d["draft_id"], "title": content.get("title"), "author": d["author"], "created": d["created_at"][:10],
            "paragraphs": content["paragraphs"], "checks": ver.get("sentences", []), "totals": ver.get("totals", {}),
            "cited": cited, "file": f"output/corpus_{d['corpus']}/run_{run_id:03d}/draft_{d['draft_id']:02d}.md"}


def _latest_findings(store: Store, run_id: int) -> list:
    row = store.db.execute("SELECT extractor FROM findings WHERE run_id = ? ORDER BY created_at DESC LIMIT 1",
                           (run_id,)).fetchone()
    if row is None:
        return []
    return store.db.execute("SELECT * FROM findings WHERE run_id = ? AND extractor = ? AND status != 'superseded' "
                            "ORDER BY finding_id", (run_id, row["extractor"])).fetchall()


def write_review(store: Store, run_id: int, path: Path, root: Path | None = None) -> None:
    path.write_text(render_page(build_data(store, run_id, root=root)), encoding="utf-8")


def render_page(data: dict, live: dict | None = None) -> str:
    """The page as a file (live=None: decisions stay in the browser) or served by the app (saved directly)."""
    # "<" only occurs inside JSON strings: escaping it keeps any "</script>" or "<!--" in the data inert
    as_json = lambda x: json.dumps(x, ensure_ascii=False).replace("<", "\\u003c")  # noqa: E731
    return PAGE.replace("__LIVE__", as_json(live)).replace("__DATA__", as_json(data))


def _spans(hits, names) -> list[list[int]]:
    """Non-overlapping [start, end, lexicon index], longest first where hits overlap."""
    out, last = [], -1
    for h in sorted(hits, key=lambda h: (h.start, -(h.end - h.start))):
        if h.start >= last:
            out.append([h.start, h.end, names.index(h.lexicon)])
            last = h.end
    return out


def _round(x):
    return None if x is None else round(x, 3)


def _map_data(m, extractor):
    if m is None:
        return None

    def tree(node):
        return [{"label": k, "studies": v.get("__studies__", []), "children": tree(v)}
                for k, v in sorted(node.items()) if k != "__studies__"]

    return {"extractor": extractor, "tree_dims": m.tree_dims, "across": m.across, "match_on": m.match_on,
            "tree": tree(m.tree), "matches": m.matches, "gaps": m.gaps, "matrix": m.matrix,
            "vocabulary": {d: c.most_common() for d, c in m.vocabulary.items()},
            "claims": m.claims, "titles": {str(k): v for k, v in m.titles.items()}}


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ichnos</title>
<link rel="manifest" href="/manifest.webmanifest">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 256 256' width='256' height='256'%3E  %3Cdefs%3E    %3ClinearGradient id='bg' x1='0' y1='0' x2='1' y2='1'%3E      %3Cstop offset='0' stop-color='%234361ee'/%3E      %3Cstop offset='1' stop-color='%235a2fc2'/%3E    %3C/linearGradient%3E  %3C/defs%3E  %3Crect x='8' y='8' width='240' height='240' rx='56' fill='url(%23bg)'/%3E  %3Cg fill='none' stroke='%23ffffff' stroke-width='22' stroke-linecap='round' stroke-linejoin='round'%3E    %3Cpath d='M128 50 V206'/%3E    %3Cpath d='M66 70 V104 C66 146 94 166 128 166 C162 166 190 146 190 104 V70'/%3E    %3Cpath d='M98 206 H158'/%3E  %3C/g%3E  %3Ccircle cx='196' cy='196' r='42' fill='%2322a05a' stroke='%23ffffff' stroke-width='10'/%3E  %3Cpath d='M176 197 L190 211 L217 182' fill='none' stroke='%23ffffff' stroke-width='12' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E">
<style>
:root {
  --bg: #f6f6f3; --panel: #ffffff; --ink: #1c1c1e; --muted: #6c6c72; --line: #e2e2dd; --chip: #efefea;
  --accent: #3451d1; --inc: #2b8a3e; --uns: #c27c0e; --exc: #c0392b; --focus: #3451d1;
  --h0: rgba(52, 81, 209, .16); --h1: rgba(140, 60, 200, .16); --h2: rgba(16, 152, 140, .18);
  --h3: rgba(214, 51, 108, .15); --h4: rgba(120, 120, 120, .18); --h5: rgba(70, 140, 60, .18);
  --traj: rgba(240, 140, 0, .30); --het: rgba(214, 51, 108, .20); --long: rgba(16, 152, 140, .14);
  --xs: rgba(192, 57, 43, .20);
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #131315; --panel: #1c1c1f; --ink: #ececef; --muted: #9b9ba3; --line: #2f2f34; --chip: #29292e;
    --accent: #8ea2ff; --inc: #63d07a; --uns: #f2b84b; --exc: #ff7b6e; --focus: #8ea2ff;
    --h0: rgba(142, 162, 255, .22); --h1: rgba(190, 130, 255, .22); --h2: rgba(60, 210, 190, .20);
    --h3: rgba(255, 110, 160, .20); --h4: rgba(200, 200, 200, .16); --h5: rgba(130, 210, 110, .20);
    --traj: rgba(255, 170, 40, .32); --het: rgba(255, 110, 160, .24); --long: rgba(60, 210, 190, .16);
    --xs: rgba(255, 123, 110, .25);
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink);
  font: 15px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
header.top { position: sticky; top: 0; z-index: 5; background: var(--panel); border-bottom: 1px solid var(--line); }
[hidden] { display: none !important; }
.bar { max-width: 1100px; margin: 0 auto; padding: 12px 16px; display: flex; flex-wrap: wrap; gap: 10px 16px;
  align-items: center; }
.bar h1 { font-size: 17px; margin: 0; font-weight: 650; }
.bar .meta { color: var(--muted); font-size: 13px; }
.tabs { display: flex; flex-wrap: wrap; gap: 4px; }
.tabs button { white-space: nowrap; }
.tabs button, .btn { border: 1px solid var(--line); background: var(--panel); color: var(--ink); border-radius: 8px;
  padding: 6px 12px; font: inherit; font-size: 14px; cursor: pointer; }
.tabs button.on { background: var(--ink); color: var(--panel); border-color: var(--ink); }
.btn.primary { background: var(--accent); color: #fff; border-color: var(--accent); }
.spacer { flex: 1; }
.progress { height: 4px; background: var(--line); }
.progress span { display: block; height: 100%; background: var(--accent); width: 0; transition: width .2s; }
main { max-width: 1100px; margin: 0 auto; padding: 16px; }
.filters { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-bottom: 12px; }
.filters select, .filters input { font: inherit; font-size: 14px; padding: 6px 8px; border-radius: 8px;
  border: 1px solid var(--line); background: var(--panel); color: var(--ink); }
.filters input[type=search] { flex: 1; min-width: 160px; }
.legend { display: flex; flex-wrap: wrap; gap: 6px; margin: 0 0 14px; font-size: 12.5px; color: var(--muted); }
.legend span { padding: 1px 7px; border-radius: 5px; }
.summary { color: var(--muted); font-size: 13.5px; margin: 0 0 10px; }
.card { background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 14px 16px;
  margin-bottom: 12px; }
.card.focus { outline: 2px solid var(--focus); outline-offset: 1px; }
.card header { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; font-size: 12.5px; color: var(--muted); }
.chip { background: var(--chip); border-radius: 999px; padding: 1px 8px; }
.lvl { border-radius: 999px; padding: 1px 8px; font-weight: 600; }
.lvl-strong { background: var(--traj); color: var(--ink); }
.lvl-moderate { background: var(--het); color: var(--ink); }
.lvl-weak { background: var(--chip); }
.lvl-none { background: transparent; border: 1px dashed var(--line); }
.warn { color: var(--exc); font-weight: 600; }
.card h3 { font-size: 16px; margin: 8px 0 6px; line-height: 1.35; }
.card h3 a { color: inherit; text-decoration: none; }
.card h3 a:hover { text-decoration: underline; }
.abs { margin: 0; color: var(--ink); font-size: 14px; max-height: 9.2em; overflow: hidden; position: relative; }
.abs.open { max-height: none; }
.more { background: none; border: 0; color: var(--accent); padding: 2px 0; cursor: pointer; font: inherit;
  font-size: 13px; }
mark { color: inherit; border-radius: 3px; padding: 0 1px; }
mark.trajectory { background: var(--traj); font-weight: 600; }
mark.heterogeneity { background: var(--het); }
mark.longitudinal { background: var(--long); }
mark.cross_sectional { background: var(--xs); text-decoration: line-through; }
.agents { margin-top: 10px; display: grid; gap: 6px; }
.agent { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; font-size: 13px; }
.agent .who { color: var(--muted); min-width: 150px; }
.dec { font-weight: 650; border-radius: 6px; padding: 0 7px; }
.dec-include { color: var(--inc); border: 1px solid var(--inc); }
.dec-unsure { color: var(--uns); border: 1px solid var(--uns); }
.dec-exclude { color: var(--exc); border: 1px solid var(--exc); }
.pill { font-size: 12px; border-radius: 6px; padding: 0 6px; border: 1px solid var(--line); }
.pill.s2 { background: rgba(43, 138, 62, .14); }
.pill.s1 { background: rgba(194, 124, 14, .16); }
.pill.s0 { background: rgba(192, 57, 43, .16); }
details.ev { font-size: 13px; color: var(--muted); }
details.ev summary { cursor: pointer; }
details.ev li { margin: 2px 0; }
.ungrounded { color: var(--exc); }
.feats { margin-top: 8px; font-size: 13px; display: grid; grid-template-columns: max-content 1fr; gap: 2px 10px; }
.feats dt { color: var(--muted); }
.feats dd { margin: 0; }
.mine { margin-top: 12px; display: flex; flex-wrap: wrap; gap: 8px; align-items: center; padding-top: 10px;
  border-top: 1px dashed var(--line); }
.mine .label { font-size: 13px; color: var(--muted); }
.mine button { font: inherit; font-size: 13.5px; padding: 4px 12px; border-radius: 8px; cursor: pointer;
  border: 1px solid var(--line); background: var(--panel); color: var(--ink); }
.mine button.on.include { background: var(--inc); color: #fff; border-color: var(--inc); }
.mine button.on.unsure { background: var(--uns); color: #fff; border-color: var(--uns); }
.mine button.on.exclude { background: var(--exc); color: #fff; border-color: var(--exc); }
.mine select, .mine input { font: inherit; font-size: 13.5px; padding: 4px 8px; border-radius: 8px;
  border: 1px solid var(--line); background: var(--panel); color: var(--ink); }
.mine input { flex: 1; min-width: 140px; }
.empty { color: var(--muted); padding: 30px 0; text-align: center; }
section.block { background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 14px 16px;
  margin-bottom: 14px; }
section.block h2 { font-size: 16px; margin: 0 0 8px; }
section.block p.hint { color: var(--muted); font-size: 13.5px; margin: 0 0 10px; }
.tree details { margin-left: 14px; }
.tree summary { cursor: pointer; padding: 1px 0; }
.tree .n { color: var(--muted); font-size: 12.5px; }
.tree .dim { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .03em; margin-right: 4px; }
.tree ul { margin: 2px 0 6px 28px; padding: 0; font-size: 13.5px; }
table { border-collapse: collapse; font-size: 13.5px; width: 100%; }
th, td { border-bottom: 1px solid var(--line); padding: 5px 8px; text-align: left; vertical-align: top; }
td.gap { background: rgba(192, 57, 43, .10); color: var(--muted); text-align: center; }
td.num { text-align: center; }
.scroll { overflow-x: auto; }
.how li { margin: 4px 0; }
kbd { border: 1px solid var(--line); border-bottom-width: 2px; border-radius: 4px; padding: 0 5px; font-size: 12px; }
.bar.live { padding-top: 0; }
.bar.live select, .bar.live input { font: inherit; font-size: 13.5px; padding: 3px 6px; border-radius: 7px;
  border: 1px solid var(--line); background: var(--panel); color: var(--ink); }
#toast { position: fixed; bottom: 18px; left: 50%; transform: translateX(-50%); background: var(--ink); color: var(--panel);
  padding: 8px 14px; border-radius: 9px; font-size: 14px; z-index: 20; max-width: 90vw; }
#toast.err { background: var(--exc); color: #fff; }
.pending { outline: 2px solid var(--uns); }
form.add { display: grid; gap: 8px; }
form.add textarea, form.add input, form.add select, .ins-edit select, .ins-edit input {
  font: inherit; font-size: 14px; padding: 6px 8px; border-radius: 8px; border: 1px solid var(--line);
  background: var(--panel); color: var(--ink); }
.ins-edit { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; margin: 6px 0 2px; }
.ins-edit input { flex: 1; min-width: 160px; }
.ins { padding: 10px 0; border-bottom: 1px solid var(--line); }
.ins:last-child { border-bottom: 0; }
.state { font-weight: 650; }
.state-written { color: var(--muted); }
.state-answered { color: var(--uns); }
.state-imported { color: var(--inc); }
.saved { color: var(--inc); font-weight: 600; }
.ck { background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 14px 16px; margin-bottom: 12px; }
.ck.st-confirmed { border-left: 5px solid var(--inc); }
.ck.st-corrected { border-left: 5px solid var(--uns); }
.ck header { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; font-size: 12.5px; color: var(--muted); }
.ck .uses { margin: 8px 0; padding: 0 0 0 18px; font-size: 13.5px; }
.ck .uses li { margin: 2px 0; }
.ck .says { margin: 8px 0 4px; }
.ck blockquote { margin: 6px 0; padding: 6px 12px; border-left: 3px solid var(--accent); background: var(--chip);
  border-radius: 0 8px 8px 0; font-size: 14.5px; }
.ck .edit { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-top: 10px; padding-top: 10px;
  border-top: 1px dashed var(--line); }
.ck .edit label.opt { display: inline-flex; gap: 4px; align-items: center; font-size: 14px; cursor: pointer; }
.ck .edit select, .ck .edit input, .ck .edit textarea { font: inherit; font-size: 14px; padding: 5px 8px;
  border-radius: 8px; border: 1px solid var(--line); background: var(--panel); color: var(--ink); }
.ck .edit input[data-note] { flex: 1; min-width: 180px; }
.ck .edit textarea { width: 100%; }
p.draft { font-size: 15.5px; line-height: 1.65; max-width: 72ch; }
.s-ok { text-decoration: underline; text-decoration-color: rgba(43, 138, 62, .45); text-underline-offset: 3px; }
.s-flag { background: rgba(194, 124, 14, .18); border-radius: 3px; }
.s-frame { color: var(--muted); }
.cite { color: var(--accent); cursor: help; }
section.paper { max-width: 80ch; font-size: 15.5px; line-height: 1.65; }
section.paper h2 { font-size: 22px; } section.paper h3 { font-size: 18px; margin-top: 22px; } section.paper h4 { font-size: 16px; }
@media (max-width: 640px) { .agent .who { min-width: 0; width: 100%; } .bar h1 { font-size: 15px; } }
/* App shell: sidebar by workflow stage, page header, content */
.shell { display: grid; grid-template-columns: 252px minmax(0, 1fr); min-height: 100vh; }
aside.side { position: sticky; top: 0; height: 100vh; overflow-y: auto; background: var(--panel);
  border-right: 1px solid var(--line); display: flex; flex-direction: column; z-index: 30; }
.brand { display: flex; align-items: center; gap: 10px; padding: 16px 16px 12px; font-weight: 700; font-size: 16px;
  letter-spacing: -.01em; }
.brand svg { flex: none; border-radius: 7px; }
.project { padding: 2px 16px 14px; border-bottom: 1px solid var(--line); }
.lab { display: block; font-size: 11px; text-transform: uppercase; letter-spacing: .07em; color: var(--muted);
  margin: 0 0 4px; font-weight: 600; }
.project select, .side-foot input { width: 100%; font: inherit; font-size: 13.5px; padding: 6px 8px; border-radius: 8px;
  border: 1px solid var(--line); background: var(--bg); color: var(--ink); }
.proj-name { font-weight: 650; font-size: 15px; line-height: 1.3; }
pre.qstring { white-space: pre-wrap; word-break: break-word; font-size: 12.5px; background: var(--chip); padding: 8px 10px;
  border-radius: 8px; margin: 4px 0 0; }
.project .back { display: inline-block; margin: 0 0 10px; font-size: 13px; color: var(--accent); text-decoration: none; }
.project .back:hover { text-decoration: underline; }
.project .owner { display: inline-block; margin-top: 6px; font-size: 11.5px; padding: 1px 8px; border-radius: 10px;
  background: var(--chip); color: var(--muted); }
.project select { margin-top: 10px; }
.project .meta { display: block; margin-top: 6px; font-size: 12px; color: var(--muted); line-height: 1.4; }
nav.side-nav { padding: 6px 8px 10px; display: flex; flex-direction: column; gap: 1px; }
nav.side-nav .group { font-size: 11px; text-transform: uppercase; letter-spacing: .07em; color: var(--muted);
  padding: 14px 10px 4px; font-weight: 600; }
nav.side-nav button { display: flex; align-items: center; gap: 10px; width: 100%; text-align: left; border: 0;
  background: none; color: var(--ink); font: inherit; font-size: 14px; padding: 7px 10px; border-radius: 8px;
  cursor: pointer; }
nav.side-nav button:hover { background: var(--chip); }
nav.side-nav button.on { background: var(--h0); color: var(--accent); font-weight: 600; }
nav.side-nav button svg { flex: none; width: 17px; height: 17px; stroke: currentColor; fill: none; stroke-width: 1.8;
  stroke-linecap: round; stroke-linejoin: round; opacity: .9; }
nav.side-nav .badge { margin-left: auto; font-size: 11.5px; font-weight: 600; background: var(--chip); color: var(--muted);
  border-radius: 999px; padding: 0 7px; min-width: 22px; text-align: center; }
nav.side-nav .badge.todo { background: var(--uns); color: #fff; }
nav.side-nav kbd { margin-left: auto; opacity: 0; transition: opacity .15s; }
body.keys nav.side-nav kbd { opacity: .7; }
.side-foot { margin-top: auto; padding: 12px 16px 16px; border-top: 1px solid var(--line); font-size: 13px; display: grid; gap: 6px; }
.side-foot .meta { color: var(--muted); font-size: 12px; }
.content { min-width: 0; display: flex; flex-direction: column; }
header.top .bar { max-width: none; padding: 12px 22px; }
header.top h1 { font-size: 19px; margin: 0; font-weight: 700; letter-spacing: -.01em; }
.actions { display: flex; flex-wrap: wrap; gap: 6px; }
main { width: 100%; max-width: 1180px; margin: 0; padding: 18px 22px 60px; }
.menu-btn { display: none; }
.scrim { display: none; }
@media (max-width: 900px) {
  .shell { grid-template-columns: minmax(0, 1fr); }
  aside.side { position: fixed; inset: 0 auto 0 0; width: 272px; transform: translateX(-102%); transition: transform .2s;
    box-shadow: 0 0 30px rgba(0, 0, 0, .25); }
  body.nav-open aside.side { transform: none; }
  body.nav-open .scrim { display: block; position: fixed; inset: 0; background: rgba(0, 0, 0, .35); z-index: 25; }
  .menu-btn { display: inline-flex; }
  header.top .bar { padding: 10px 16px; }
  main { padding: 14px 16px 50px; }
}
/* Home */
.stages { display: grid; grid-template-columns: repeat(auto-fill, minmax(190px, 1fr)); gap: 10px; margin: 0 0 18px; }
.stage { background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 12px 14px; cursor: pointer;
  text-align: left; font: inherit; color: var(--ink); }
.stage:hover { border-color: var(--accent); }
.stage .st { font-size: 11px; text-transform: uppercase; letter-spacing: .07em; color: var(--muted); font-weight: 600; }
.stage .big { font-size: 26px; font-weight: 700; letter-spacing: -.02em; margin: 2px 0 0; }
.stage .big small { font-size: 14px; color: var(--muted); font-weight: 500; }
.stage .what { font-size: 13px; color: var(--muted); }
.stage .det { font-size: 12.5px; color: var(--muted); margin-top: 6px; line-height: 1.4; display: -webkit-box;
  -webkit-line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }
a.btn { text-decoration: none; display: inline-flex; align-items: center; }
.stage .meter { height: 4px; background: var(--line); border-radius: 3px; margin-top: 8px; overflow: hidden; }
.stage .meter i { display: block; height: 100%; background: var(--accent); }
ol.next { margin: 0; padding: 0; list-style: none; counter-reset: n; }
ol.next li { counter-increment: n; display: flex; gap: 12px; align-items: flex-start; padding: 10px 0;
  border-bottom: 1px solid var(--line); font-size: 14.5px; }
ol.next li:last-child { border-bottom: 0; }
ol.next li::before { content: counter(n); flex: none; width: 24px; height: 24px; border-radius: 50%; background: var(--h0);
  color: var(--accent); font-weight: 700; font-size: 13px; display: grid; place-items: center; }
ol.next li .go { margin-left: auto; flex: none; }
details.optional { margin-top: 10px; } details.optional summary { cursor: pointer; color: var(--muted); }
.dir { border: 1px solid var(--line); border-radius: 10px; padding: 12px 14px; margin: 10px 0; }
table.fw td:first-child { width: 62%; } table.fw td { vertical-align: top; }
table.fw .hint { color: var(--muted); font-size: 12.5px; margin-top: 3px; }
.fwst { border-radius: 999px; padding: 1px 8px; background: var(--chip); white-space: nowrap; }
.fwst.supported, .fwst.contradicted, .fwst.mixed { background: none; border: 1px solid; font-weight: 600; }
.fwst.supported { color: var(--inc); } .fwst.contradicted { color: var(--exc); } .fwst.mixed { color: var(--uns); }
.dir.chosen { border-left: 4px solid var(--inc); } .dir.set_aside { opacity: .6; }
.dir .dhead { display: flex; flex-wrap: wrap; gap: 8px; align-items: baseline; margin-bottom: 6px; }
.dir p { margin: 4px 0; } .dir .dact { margin-top: 8px; display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.decision { display: inline-block; font-weight: 700; padding: 1px 9px; border-radius: 999px; background: var(--chip); }
.decision.reject { color: var(--exc); } .decision.accept, .decision.minor_revision { color: var(--inc); }
.decision.major_revision { color: var(--uns); }
/* Interpretations */
.lens-chip { display: inline-flex; align-items: center; gap: 5px; border: 1px solid var(--lc, var(--line));
  border-radius: 999px; padding: 0 9px; font-weight: 600; font-size: 12.5px; color: var(--ink); white-space: nowrap; }
.lens-chip::before { content: ""; width: 8px; height: 8px; border-radius: 50%; background: var(--lc, var(--muted)); }
.role { font-size: 11.5px; color: var(--muted); text-transform: uppercase; letter-spacing: .05em; }
.gates td { white-space: nowrap; }
.gates th { font-size: 12.5px; line-height: 1.3; }
.gates th[title] { cursor: help; text-decoration: underline dotted; text-underline-offset: 3px; }
.gates th.gh { text-align: center; text-decoration: none; cursor: default; color: var(--muted); font-weight: 600;
  border-bottom: 2px solid var(--line); }
.gates .lcol { position: sticky; left: 0; z-index: 1; background: var(--panel); white-space: nowrap; }
button.lens-btn { background: none; border: 0; padding: 0; font: inherit; color: inherit; cursor: pointer; }
button.lens-btn:focus-visible, .gates th[tabindex]:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; border-radius: 6px; }
.gates tr.grp th { background: var(--bg); font-size: 11.5px; text-transform: uppercase; letter-spacing: .06em;
  color: var(--muted); text-decoration: none; cursor: default; white-space: normal; }
.hyp a, details.tc a { color: var(--accent); }
.gates tr:hover td { background: var(--chip); }
.gates tr.placebo td { color: var(--muted); }
.gates td.hi { font-weight: 700; }
.funnel { display: flex; height: 12px; width: 160px; border-radius: 4px; overflow: hidden; background: var(--line); }
.funnel i { display: block; height: 100%; }
.funnel i + i { box-shadow: inset 1px 0 var(--panel); }
.f-pend { background: repeating-linear-gradient(45deg, rgba(120, 120, 120, .45) 0 3px, transparent 3px 6px); }
.f-out { background: rgba(120, 120, 120, .45); } .f-cs { background: rgba(192, 57, 43, .55); }
.f-disp { background: rgba(194, 124, 14, .55); } .f-aux { background: rgba(16, 152, 140, .55); }
.f-core { background: rgba(43, 138, 62, .85); }
.key { display: inline-flex; align-items: center; gap: 5px; margin-right: 12px; font-size: 12.5px; color: var(--muted); }
.key i { width: 12px; height: 10px; border-radius: 2px; display: inline-block; }
.verdict { display: inline-block; border: 1px solid currentColor; border-radius: 6px; padding: 0 7px; font-weight: 650;
  font-size: 12.5px; white-space: nowrap; }
button.verdict { background: none; font: inherit; font-size: 12.5px; font-weight: 650; cursor: pointer; }
button.verdict.on { background: var(--chip); outline: 2px solid currentColor; outline-offset: 1px; }
button.verdict.on::before { content: "✓ "; }
button.verdict:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; }
.v-novel { color: #237033; } .v-near { color: #8a5600; } .v-prior { color: var(--exc); }
.v-contested { color: #7d3c98; } .v-not_searchable, .v-unjudged, .v-not_searched, .v-out { color: var(--muted); }
.hyp .verdict.v-out { white-space: normal; }
@media (prefers-color-scheme: dark) { .v-novel { color: var(--inc); } .v-near { color: var(--uns); } .v-contested { color: #c58af0; } }
.hyp { background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 12px 16px; margin-bottom: 10px; }
.hyp.out { border-style: dashed; }
.hyp.out .stmt { font-weight: 500; color: var(--muted); }
.hyp header { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; font-size: 12.5px; color: var(--muted); }
.hyp .stmt { margin: 6px 0 4px; font-weight: 600; font-size: 15px; line-height: 1.45; }
.hyp .sub { font-size: 13.5px; color: var(--muted); margin: 2px 0; }
.hyp .sub b { color: var(--ink); font-weight: 600; }
ul.hits { margin: 6px 0 0; padding-left: 18px; font-size: 13.5px; }
ul.hits li { margin: 4px 0; }
ul.hits .q { color: var(--muted); font-style: italic; }
details.tc { background: var(--panel); border: 1px solid var(--line); border-radius: 12px; padding: 12px 16px; margin-bottom: 12px; }
details.tc > summary { cursor: pointer; font-weight: 650; font-size: 15.5px; line-height: 1.4; }
details.tc > summary .chip { font-weight: 700; margin-right: 4px; }
.positions { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 10px; margin: 12px 0; }
.pos { border: 1px solid var(--line); border-top: 4px solid var(--lc, var(--accent)); border-radius: 10px; padding: 10px 12px;
  font-size: 14px; }
.pos .from { display: block; margin-top: 6px; font-size: 12.5px; color: var(--muted); }
.pos .pred { margin: 6px 0 0; }
.core-h { margin: 6px 0 0; }
dl.test { display: grid; grid-template-columns: max-content minmax(0, 1fr); gap: 5px 14px; font-size: 14px; margin: 6px 0 10px; }
dl.test dt { color: var(--muted); font-weight: 600; }
dl.test dd { margin: 0; }
.tc h4 { margin: 16px 0 4px; font-size: 14px; }
.tc ul { margin: 4px 0; padding-left: 20px; font-size: 14px; }
.tc li { margin: 4px 0; }
.lens-core { padding: 10px 0; border-bottom: 1px solid var(--line); }
.lens-core:last-child { border-bottom: 0; }
.lens-core ul { margin: 4px 0 0; padding-left: 20px; font-size: 14px; }
@media (max-width: 640px) { dl.test { grid-template-columns: minmax(0, 1fr); } dl.test dt { margin-top: 6px; }
  .gates .fcol { display: none; } }
</style>
</head>
<body>
<div class="shell">
<aside class="side" id="side">
  <div class="brand"><svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 256 256" width="28" height="28" aria-hidden="true"> <defs>  <linearGradient id="bg" x1="0" y1="0" x2="1" y2="1">   <stop offset="0" stop-color="#4361ee"/>   <stop offset="1" stop-color="#5a2fc2"/>  </linearGradient> </defs> <rect x="8" y="8" width="240" height="240" rx="56" fill="url(#bg)"/> <g fill="none" stroke="#ffffff" stroke-width="22" stroke-linecap="round" stroke-linejoin="round">  <path d="M128 50 V206"/>  <path d="M66 70 V104 C66 146 94 166 128 166 C162 166 190 146 190 104 V70"/>  <path d="M98 206 H158"/> </g> <circle cx="196" cy="196" r="42" fill="#22a05a" stroke="#ffffff" stroke-width="10"/> <path d="M176 197 L190 211 L217 182" fill="none" stroke="#ffffff" stroke-width="12" stroke-linecap="round" stroke-linejoin="round"/></svg><span>Ichnos</span></div>
  <div class="project">
    <a class="back" id="allproj" href="/" hidden>&larr; All projects</a>
    <span class="lab">Project</span>
    <div class="proj-name" id="head"></div>
    <span class="owner" id="owner" hidden></span>
    <span class="meta" id="meta"></span>
    <select id="runsel" hidden aria-label="Switch to another project or run"></select>
  </div>
  <nav class="side-nav" id="tabs" aria-label="Pages">
    <div class="group">Overview</div>
    <button data-tab="home"><svg viewBox="0 0 24 24"><path d="M3 11l9-7 9 7v9a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1z"/></svg>Home<kbd>1</kbd></button>
    <div class="group">Screen and select</div>
    <button data-tab="records"><svg viewBox="0 0 24 24"><path d="M4 6h16M4 12h16M4 18h10"/></svg>Records<span class="badge" data-badge="records"></span></button>
    <button data-tab="fulltext"><svg viewBox="0 0 24 24"><path d="M6 3h8l4 4v14H6z"/><path d="M12 10v7M9 14l3 3 3-3"/></svg>Full texts<span class="badge" data-badge="fulltext"></span></button>
    <div class="group">Evidence</div>
    <button data-tab="map"><svg viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg>Evidence map<kbd>3</kbd></button>
    <button data-tab="findings"><svg viewBox="0 0 24 24"><path d="M9 3h6l4 4v14H5V3z"/><path d="M9 12h6M9 16h6"/></svg>Findings<span class="badge" data-badge="findings"></span></button>
    <button data-tab="insights"><svg viewBox="0 0 24 24"><path d="M9 18h6M10 21h4M12 3a6 6 0 0 0-4 10.5c.7.7 1 1.5 1 2.5h6c0-1 .3-1.8 1-2.5A6 6 0 0 0 12 3z"/></svg>Insights<span class="badge" data-badge="insights"></span></button>
    <div class="group">Interpret</div>
    <button data-tab="interpret"><svg viewBox="0 0 24 24"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/></svg>Interpretations<span class="badge" data-badge="interpret"></span></button>
    <div class="group">Write</div>
    <button data-tab="draft"><svg viewBox="0 0 24 24"><path d="M4 20h4L19 9l-4-4L4 16z"/></svg>Draft<kbd>6</kbd></button>
    <button data-tab="paper"><svg viewBox="0 0 24 24"><path d="M6 3h9l3 3v15H6z"/><path d="M9 9h6M9 13h6M9 17h4"/></svg>Manuscripts<span class="badge" data-badge="paper"></span></button>
    <div class="group">Verify</div>
    <button data-tab="check"><svg viewBox="0 0 24 24"><path d="M12 3l8 3v6c0 4.5-3.4 8.2-8 9-4.6-.8-8-4.5-8-9V6z"/><path d="M8.5 12l2.5 2.5 4.5-5"/></svg>Check findings<span class="badge" data-badge="check"></span></button>
    <div class="group" id="agents-group" hidden>Pipeline</div>
    <button data-tab="agents" id="agents-tab" hidden><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M12 2v3M12 19v3M2 12h3M19 12h3M4.9 4.9l2.1 2.1M17 17l2.1 2.1M4.9 19.1L7 17M17 7l2.1-2.1"/></svg>Agent tasks<kbd>9</kbd></button>
    <div class="group">Help</div>
    <button data-tab="how"><svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9"/><path d="M9.5 9.5a2.5 2.5 0 1 1 3.5 2.3c-.7.3-1 .9-1 1.6V14M12 17.5v.01"/></svg>How it works</button>
  </nav>
  <div class="side-foot">
    <div id="livebar" hidden>
      <label class="lab" for="reviewer">Your name</label>
      <input id="reviewer" placeholder="e.g. sam">
      <span class="meta" id="saved">Changes are saved as you make them.</span>
    </div>
    <div id="staticbar" hidden><span class="warn">Read-only copy.</span>
      <span class="meta">To save decisions and checks, open the Ichnos app.</span></div>
    <span class="meta" id="jobs"></span>
  </div>
</aside>
<div class="scrim" id="scrim"></div>
<div class="content">
<header class="top">
  <div class="bar">
    <button class="btn menu-btn" id="menu" aria-label="Show the pages">&#9776;</button>
    <h1 id="pagetitle"></h1>
    <span class="meta" id="pagehint"></span>
    <span class="spacer"></span>
    <label class="meta" id="blindbox"><input type="checkbox" id="blind"> Blind mode</label>
    <button class="btn primary" id="export">Export my decisions</button>
    <span class="actions" id="actions" hidden>
      <button class="btn" data-job="report" title="Rebuild the report, the review data and the insight log">Rebuild report</button>
      <button class="btn" data-job="fulltext" title="Look for open-access full texts of the kept studies and parse them">Find full texts</button>
      <button class="btn" data-job="pdfs" title="Match and parse the PDFs you saved in fulltext/inbox (downloaded through your library)">Add downloaded PDFs</button>
    </span>
  </div>
  <div class="progress"><span id="prog"></span></div>
</header>
<div id="toast" role="status" hidden></div>
<main>
  <div id="home" hidden></div>
  <div id="fulltext" hidden></div>
  <div id="records">
    <div class="filters">
      <select id="filter" aria-label="Show">
        <option value="needs">Needs you</option>
        <option value="disagree">Agents disagree</option>
        <option value="kept">Kept by an agent</option>
        <option value="miss">Possible misses: excluded, strong trajectory signal</option>
        <option value="excluded">Excluded by every agent</option>
        <option value="undecided">Not decided by you</option>
        <option value="mine">Decided by you</option>
        <option value="all">All records</option>
      </select>
      <select id="level" aria-label="Trajectory signal">
        <option value="any">Any trajectory signal</option>
        <option value="strong">Strong</option>
        <option value="moderate">Moderate</option>
        <option value="weak">Weak</option>
        <option value="none">None</option>
      </select>
      <input type="search" id="q" placeholder="Search title and abstract">
    </div>
    <div class="legend" id="legend"></div>
    <p class="summary" id="summary"></p>
    <div id="list"></div>
  </div>
  <div id="map" hidden></div>
  <div id="findings" hidden></div>
  <div id="insights" hidden></div>
  <div id="interpret" hidden></div>
  <div id="draft" hidden></div>
  <div id="paper" hidden></div>
  <div id="check" hidden></div>
  <div id="agents" hidden></div>
  <div id="how" hidden></div>
</main>
</div>
</div>
<script id="data" type="application/json">__DATA__</script>
<script id="live" type="application/json">__LIVE__</script>
<script>
"use strict";
const D = JSON.parse(document.getElementById("data").textContent);
const KEY = "psyscientist:" + D.corpus + ":run" + D.run;
const load = (k, f) => { try { const v = localStorage.getItem(k); return v ? JSON.parse(v) : f; } catch (e) { return f; } };
const save = (k, v) => { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} };
const esc = s => String(s == null ? "" : s).replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
// LIVE: served by the app (python -m ichnos app); changes go straight into the evidence store.
const LIVE = JSON.parse(document.getElementById("live").textContent);
const mine = LIVE ? {} : load(KEY + ":decisions", {});
for (const [id, h] of Object.entries(D.human)) if (!mine[id]) mine[id] = {d: h.d, r: h.r || "", n: h.n || ""};
function toast(msg, err) {
  const t = document.getElementById("toast");
  t.textContent = msg; t.className = err ? "err" : ""; t.hidden = false;
  clearTimeout(toast.timer); toast.timer = setTimeout(() => { t.hidden = true; }, err ? 6000 : 2200);
}
async function api(path, body) {
  const res = await fetch(path, {method: "POST", headers: {"Content-Type": "application/json", "X-Token": LIVE.token},
                                 body: JSON.stringify(body || {})});
  const out = await res.json().catch(() => ({error: "no answer from the app"}));
  if (!res.ok || out.error) throw new Error(out.error || res.statusText);
  return out;
}
const reviewer = () => (document.getElementById("reviewer").value || "").trim().toLowerCase().replace(/[^a-z0-9_.-]+/g, "_");
async function persist(id) {
  if (!LIVE) { save(KEY + ":decisions", mine); return; }
  const m = mine[id] || {};
  const card = document.querySelector('.card[data-id="' + id + '"]');
  if (!reviewer()) { toast("Enter your name at the top first, so the decision is attributed to you.", true); return; }
  if (m.d === "exclude" && !m.r) { if (card) card.querySelector("[data-reason]").classList.add("pending"); toast("Choose a reason for the exclusion; it is saved then."); return; }
  try {
    await api("/api/decision", {run: D.run, work_id: +id, reviewer: reviewer(), decision: m.d || "", reason: m.r || null, note: m.n || null});
    if (card) card.querySelector("[data-reason]").classList.remove("pending");
    toast(m.d ? "Saved: " + m.d + (m.r ? " (" + m.r + ")" : "") : "Decision removed");
  } catch (e) { toast("Not saved: " + (e.message.includes("fetch") ? "the app is not running. Double-click Ichnos.bat and reload this page." : e.message), true); }
}
const st = Object.assign({tab: "home", filter: "needs", level: "any", q: "", blind: false, focus: 0}, load(KEY + ":view", {}));
const byId = Object.fromEntries(D.records.map(r => [r.id, r]));
const LEX_CLASS = name => ["trajectory", "heterogeneity", "longitudinal", "cross_sectional"].includes(name) ? name : null;
const queryLex = D.lexicons.filter(n => n.startsWith("query:"));
const lexStyle = i => {
  const name = D.lexicons[i];
  const cls = LEX_CLASS(name);
  if (cls) return {cls, style: ""};
  const k = queryLex.indexOf(name);
  return {cls: "q", style: "background: var(--h" + (k % 6) + ")"};
};

function hl(text, spans) {
  let out = "", at = 0;
  for (const [s, e, i] of spans) {
    out += esc(text.slice(at, s));
    const {cls, style} = lexStyle(i);
    out += '<mark class="' + cls + '" style="' + style + '" title="' + esc(D.lexicons[i].replace("query:", "search: ")) + '">' + esc(text.slice(s, e)) + "</mark>";
    at = e;
  }
  return out + esc(text.slice(at));
}

const short = s => s.replace(/^model:/, "");
const agentCalls = r => D.screeners.map(s => r.calls[s]).filter(Boolean);
function consensus(r) {
  const c = agentCalls(r).map(x => x.d);
  if (!c.length) return "none";
  const keep = c.map(d => d !== "exclude");
  if (keep.every(Boolean)) return new Set(c).size === 1 ? "agree-keep" : "keep-mixed";
  if (!keep.some(Boolean)) return "agree-exclude";
  return "disagree";
}
function matches(r) {
  const m = mine[r.id];
  if (st.level !== "any" && r.level !== st.level) return false;
  if (st.q) { const q = st.q.toLowerCase(); if (!(r.title + " " + r.abstract).toLowerCase().includes(q)) return false; }
  const c = consensus(r);
  switch (st.filter) {
    case "needs":
      if (m && m.d) return false;
      if (st.blind || c === "none") return true;
      return c !== "agree-exclude" || r.level === "strong";
    case "disagree": return c === "disagree" || c === "keep-mixed";
    case "kept": return c === "agree-keep" || c === "keep-mixed" || c === "disagree";
    case "miss": return c === "agree-exclude" && r.level === "strong";
    case "excluded": return c === "agree-exclude";
    case "undecided": return !(m && m.d);
    case "mine": return !!(m && m.d);
    default: return true;
  }
}

function agentsPanel(r) {
  const rows = D.screeners.filter(s => r.calls[s]).map(s => {
    const c = r.calls[s];
    let pills = "", ev = "";
    if (c.s) {
      for (const m of D.metrics) {
        const v = c.s[m.id];
        if (!v) continue;
        const [score, quote, grounded] = v;
        pills += '<span class="pill s' + score + '" title="' + esc(m.q) + '">' + esc(m.id) + " " + score + (grounded === 0 ? " ⚠" : "") + "</span>";
        ev += "<li><b>" + esc(m.id) + " " + score + "</b>" + (quote ? ": “" + esc(quote) + "”" : "") +
              (grounded === 0 ? ' <span class="ungrounded">quote not found in the record</span>' : "") + "</li>";
      }
    }
    return '<div class="agent"><span class="who">' + esc(short(s)) + '</span><span class="dec dec-' + c.d + '">' + c.d + "</span>" +
      (c.r ? '<span class="chip">' + esc(c.r) + "</span>" : "") + pills +
      (c.n ? '<span class="meta">' + esc(c.n) + "</span>" : "") + "</div>" +
      (ev ? '<details class="ev"><summary>Evidence behind ' + esc(short(s)) + "'s scores</summary><ul>" + ev + "</ul></details>" : "");
  });
  return rows.length ? '<div class="agents">' + rows.join("") + "</div>" : "";
}

function featuresPanel(r) {
  const f = r.features;
  const dims = Object.keys(f);
  if (!dims.length) return "";
  return '<dl class="feats">' + dims.map(d => {
    const [value, canonical, quote, grounded] = f[d];
    return "<dt>" + esc(d) + "</dt><dd" + (quote ? ' title="“' + esc(quote) + '”"' : "") + ">" + esc(value) +
      (canonical && canonical !== value.toLowerCase() && value !== "not_reported" ? ' <span class="meta">(' + esc(canonical) + ")</span>" : "") +
      (grounded === 0 ? ' <span class="ungrounded">⚠</span>' : "") + "</dd>";
  }).join("") + "</dl>";
}

function mineControls(r) {
  const m = mine[r.id] || {};
  const btn = d => '<button data-act="' + d + '" class="' + d + (m.d === d ? " on" : "") + '">' + d + "</button>";
  const reasons = '<select data-reason ' + (m.d === "exclude" ? "" : "disabled") + '><option value="">reason…</option>' +
    D.reasons.map(x => "<option" + (m.r === x ? " selected" : "") + ">" + esc(x) + "</option>").join("") + "</select>";
  return '<div class="mine"><span class="label">Your call</span>' + D.decisions.map(btn).join("") + reasons +
    '<input data-note placeholder="note" value="' + esc(m.n || "") + '"></div>';
}

function card(r, idx) {
  const src = r.found_by.map(s => '<span class="chip">' + esc(s) + "</span>").join("");
  const title = r.link ? '<a href="' + esc(r.link) + '" target="_blank" rel="noopener">' + hl(r.title, r.hl.title) + "</a>" : hl(r.title, r.hl.title);
  const abs = r.abstract ? '<p class="abs">' + hl(r.abstract, r.hl.abstract) + '</p><button class="more" data-more>Show all</button>' : '<p class="abs"><em>No abstract in the databases searched.</em> The publisher\'s page (the link above) often shows one: publishers do not always pass abstracts to the open databases. A library export (Full texts page) or the downloaded PDF fills it in.</p>';
  return '<article class="card' + (st.focus === idx ? " focus" : "") + '" data-id="' + r.id + '" data-idx="' + idx + '">' +
    "<header><span>#" + r.id + "</span><span>" + (r.year || "") + "</span><span>" + esc(r.venue) + "</span>" + src +
    '<span class="lvl lvl-' + r.level + '" title="' + esc(D.levels[r.level]) + '">trajectory: ' + r.level + "</span>" +
    (r.ft ? '<a class="chip" href="' + esc(r.ft.url) + '" target="_blank" rel="noopener" title="' + esc("licence: " + (r.ft.license || "not stated")) + '">full text: ' + esc(r.ft.source + ", " + r.ft.parser) + "</a>" : "") +
    (r.xs ? '<span class="warn">cross-sectional wording</span>' : "") + "</header>" +
    "<h3>" + title + "</h3>" + abs + (st.blind ? "" : agentsPanel(r)) + featuresPanel(r) + findingsPanel(r) + mineControls(r) + "</article>";
}

let shown = [];
function renderList() {
  shown = D.records.filter(matches);
  if (st.focus >= shown.length) st.focus = Math.max(0, shown.length - 1);
  const list = document.getElementById("list");
  list.innerHTML = shown.length ? shown.map(card).join("") : '<p class="empty">Nothing here with these filters.</p>';
  const decided = D.records.filter(r => mine[r.id] && mine[r.id].d).length;
  document.getElementById("summary").textContent = shown.length + " of " + D.records.length + " records shown · " +
    decided + " decided by you" + agreementText();
  document.getElementById("prog").style.width = (100 * decided / Math.max(1, D.records.length)) + "%";
}
function agreementText() {
  if (st.blind || !D.agreement.length) return "";
  return " · " + D.agreement.map(a => short(a.a) + " vs " + short(a.b) + ": " + Math.round(100 * a.observed) + "% agree, κ " +
    (a.kappa == null ? "n/a" : a.kappa.toFixed(2))).join("; ");
}
function rerenderCard(id) {
  const el = document.querySelector('.card[data-id="' + id + '"]');
  if (el) el.outerHTML = card(byId[id], +el.dataset.idx);
  const decided = D.records.filter(r => mine[r.id] && mine[r.id].d).length;
  document.getElementById("prog").style.width = (100 * decided / Math.max(1, D.records.length)) + "%";
}

function setDecision(id, d) {
  const m = mine[id] || (mine[id] = {d: "", r: "", n: ""});
  m.d = m.d === d ? "" : d;
  if (m.d !== "exclude") m.r = "";
  rerenderCard(id);
  persist(id);
}

document.getElementById("list").addEventListener("click", e => {
  const c = e.target.closest(".card");
  if (!c) return;
  st.focus = +c.dataset.idx;
  if (e.target.dataset.act) setDecision(c.dataset.id, e.target.dataset.act);
  else if (e.target.hasAttribute("data-more")) {
    const a = c.querySelector(".abs");
    a.classList.toggle("open");
    e.target.textContent = a.classList.contains("open") ? "Show less" : "Show all";
  }
});
document.getElementById("list").addEventListener("change", e => {
  const c = e.target.closest(".card");
  if (c && e.target.hasAttribute("data-reason")) {
    (mine[c.dataset.id] = mine[c.dataset.id] || {d: "exclude", n: ""}).r = e.target.value;
    persist(c.dataset.id);
  } else if (c && e.target.hasAttribute("data-note") && LIVE && (mine[c.dataset.id] || {}).d) {
    persist(c.dataset.id);   // notes are saved when you leave the field
  }
});
document.getElementById("list").addEventListener("input", e => {
  const c = e.target.closest(".card");
  if (c && e.target.hasAttribute("data-note")) {
    (mine[c.dataset.id] = mine[c.dataset.id] || {d: "", r: ""}).n = e.target.value;
    if (!LIVE) save(KEY + ":decisions", mine);
  }
});
document.addEventListener("keydown", e => {
  if (st.tab !== "records" || /INPUT|SELECT|TEXTAREA/.test(document.activeElement.tagName) || e.ctrlKey || e.metaKey) return;
  const move = d => {
    st.focus = Math.min(Math.max(0, st.focus + d), shown.length - 1);
    document.querySelectorAll(".card.focus").forEach(x => x.classList.remove("focus"));
    const el = document.querySelector('.card[data-idx="' + st.focus + '"]');
    if (el) { el.classList.add("focus"); el.scrollIntoView({block: "center", behavior: "smooth"}); }
  };
  const map = {i: "include", u: "unsure", e: "exclude"};
  if (e.key === "j") move(1);
  else if (e.key === "k") move(-1);
  else if (map[e.key] && shown[st.focus]) setDecision(String(shown[st.focus].id), map[e.key]);
});

for (const [id, key] of [["filter", "filter"], ["level", "level"]]) {
  const el = document.getElementById(id);
  el.value = st[key];
  el.addEventListener("change", () => { st[key] = el.value; st.focus = 0; save(KEY + ":view", st); renderList(); });
}
const q = document.getElementById("q");
q.addEventListener("input", () => { st.q = q.value; st.focus = 0; renderList(); });
const blind = document.getElementById("blind");
blind.checked = !!st.blind;
blind.addEventListener("change", () => { st.blind = blind.checked; save(KEY + ":view", st); renderList(); renderMap(); });

const PAGES = {
  home: ["Home", "Where the review stands, and what to do next"],
  records: ["Records", "Screen on title and abstract; the agents' calls are shown unless blind mode is on"],
  fulltext: ["Full texts", "Download what open access could not find, through your library"],
  map: ["Evidence map", "What the retained studies cover, as charted"],
  findings: ["Findings", "Every extracted finding with its verbatim passage"],
  insights: ["Insights", "Patterns, contradictions and gaps the agents noticed, for you to review"],
  draft: ["Draft", "A results section drafted from the findings"],
  paper: ["Manuscripts", "Versions, reviews, audits and decisions"],
  check: ["Check findings", "A person checks what the headline claims rest on"],
  agents: ["Agent tasks", "Prepare, answer and import the agents' work"],
  how: ["How it works", "What the page shows and how decisions are saved"],
  interpret: ["Interpretations", "Lenses read the same findings; two gates decide which hypotheses count, the literature says which are known"],
};
const ORDER = Object.keys(PAGES);
function go(t) {
  if (!(t in PAGES)) return;
  st.tab = t; save(KEY + ":view", st);
  if (location.hash.slice(1) !== t) history.pushState(null, "", "#" + t);
  showTab(); window.scrollTo(0, 0);
}
document.getElementById("tabs").addEventListener("click", e => {
  const b = e.target.closest("button[data-tab]");
  if (b) go(b.dataset.tab);
});
window.addEventListener("popstate", () => { const t = location.hash.slice(1); if (t in PAGES) { st.tab = t; showTab(); } });
document.getElementById("menu").addEventListener("click", () => document.body.classList.toggle("nav-open"));
document.getElementById("scrim").addEventListener("click", () => document.body.classList.remove("nav-open"));
document.addEventListener("keydown", e => {
  if (e.key === "Alt") document.body.classList.add("keys");
  if (e.altKey && /^[1-9]$/.test(e.key)) {
    const visible = ORDER.filter(t => !(t === "agents" && !LIVE));
    const t = visible[+e.key - 1];
    if (t) { e.preventDefault(); go(t); }
  }
});
document.addEventListener("keyup", e => { if (e.key === "Alt") document.body.classList.remove("keys"); });
function showTab() {
  if (st.tab === "agents" && !LIVE) st.tab = "home";
  if (!(st.tab in PAGES)) st.tab = "home";
  for (const t of ORDER) document.getElementById(t).hidden = st.tab !== t;
  document.querySelectorAll("#tabs button").forEach(b => b.classList.toggle("on", b.dataset.tab === st.tab));
  document.getElementById("pagetitle").textContent = PAGES[st.tab][0];
  document.getElementById("pagehint").textContent = PAGES[st.tab][1];
  document.title = PAGES[st.tab][0] + " · " + ((D.project && D.project.name) || "Ichnos");
  const screening = st.tab === "records" || st.tab === "map";
  document.getElementById("blindbox").hidden = !screening;
  document.getElementById("export").hidden = !!LIVE || st.tab !== "records";
  document.getElementById("actions").hidden = !LIVE || !(st.tab === "records" || st.tab === "fulltext");
  document.body.classList.remove("nav-open");
}
function badges() {
  const set = (k, n, todo) => { const b = document.querySelector('[data-badge="' + k + '"]'); if (b) { b.textContent = n ? String(n) : ""; b.hidden = !n; b.classList.toggle("todo", !!todo); } };
  const O = D.overview || {stages: []};
  const by = Object.fromEntries((O.stages || []).map(x => [x.stage, x]));
  set("records", D.records.length);
  const L = D.library || {rows: []};
  set("fulltext", L.rows.filter(r => r.group <= 2).length, true);
  set("findings", by.Extract ? by.Extract.value : 0);
  set("insights", D.insights.filter(i => i.status === "noted").length, true);
  set("paper", D.papers.length);
  const C = D.check && D.check.summary;
  set("check", C ? C.headline - C.checked : 0, true);
  const I = D.interpret;
  set("interpret", I && I.cards ? I.cards.reduce((n, c) => n + (c.cards || []).length, 0) : 0);
}
function openOutside(url) {
  // Through the app, the link opens in the computer's own browser (with the library's sign-in). When the app does
  // not answer (its window was closed or it stopped), open it here instead and say so.
  if (LIVE) api("/api/open-url", {url}).catch(() => {
    window.open(url, "_blank", "noopener");
    toast("The app is not answering, so the link opened here. Reopen Ichnos (its shortcut) before you save anything.", true);
  });
  else window.open(url, "_blank", "noopener");
}
function renderFulltext() {
  const el = document.getElementById("fulltext");
  const L = D.library;
  if (!L) { el.innerHTML = '<section class="block"><p class="hint">No list for this run.</p></section>'; return; }
  const st3 = load(KEY + ":fulltext", {group: "priority", q: ""});
  const counts = {};
  L.rows.forEach(r => { counts[r.group] = (counts[r.group] || 0) + 1; });
  const steps = '<ol class="next">' +
    "<li><span>Open a study with <b>DOI</b> or <b>Google Scholar</b>. Links open in your normal browser, where your library login works (on campus or the university VPN, or the publisher's <i>Access through your institution</i>).</span></li>" +
    "<li><span>Leave the PDF where your browser saved it (your Downloads folder), or save it in the inbox folder. In Downloads it is matched by the DOI or title on its first pages; in the inbox a file named with the record number (<b>Copy file name</b> puts it on your clipboard) is matched at once.</span>" +
    (LIVE ? '<button class="btn go" id="ftinbox">Open the inbox folder</button>' : "") + "</li>" +
    "<li><span>When you have a batch, press <b>Add downloaded PDFs</b>. Each file is matched and parsed; inbox files move to inbox/done, and Downloads stays as it is (matching files are copied). The list here shrinks. Then ask Claude to chart and extract the new full texts.</span>" +
    (LIVE ? '<button class="btn primary go" id="ftadd">Add downloaded PDFs</button>' : "") + "</li></ol>";
  const noAbs = L.rows.filter(r => !r.abstract).length;
  const opts = '<option value="priority">Download first: yours, core and headline studies</option>' +
    Object.entries(L.groups).filter(([g]) => counts[g]).map(([g, label]) => '<option value="' + g + '">' + esc(label) + " (" + counts[g] + ")</option>").join("") +
    (noAbs ? '<option value="title">Title only: the agents had no abstract (' + noAbs + ")</option>" : "") +
    '<option value="all">All (' + L.rows.length + ")</option>";
  const dbs = L.databases || [];
  const exportBox = (L.no_abstract && dbs.length) ? '<section class="block"><h2>Missing abstracts</h2><p class="hint">' + L.no_abstract +
    " retained records have no abstract, so the agents judged and charted them from the title alone. Most come from publishers that do not pass abstracts to the open databases: that is why the link shows one and this page does not. A library database export brings them all at once, and PDFs (above) do the same record by record.</p>" +
    '<ol class="next"><li><span>Run the search string below in the database, through the library (it is the protocol\'s own string).</span></li>' +
    "<li><span>Export all results as RIS (or CSV) <b>including abstracts</b>, and note how many results the database reported.</span></li>" +
    "<li><span>Add the file here. Records new to this review go on the sheet to be screened; records already here get the export's abstract.</span></li></ol>" +
    (LIVE ? '<div class="filters"><select id="xdb">' + dbs.map(d => '<option value="' + esc(d.platform) + '">' + esc(d.name) + "</option>").join("") + "</select>" +
      '<input type="number" id="xhits" min="0" placeholder="Results reported"><input type="file" id="xfile" accept=".ris,.txt,.csv,.nbib">' +
      '<button class="btn primary" id="xadd">Add the export</button></div>' : "") +
    '<p class="hint">Search string:</p><pre class="qstring" id="xq"></pre></section>' : "";
  el.innerHTML = '<section class="block"><h2>Get the full texts</h2><p class="hint">' + L.rows.length +
    " retained records have no full text yet; " + L.added + " came from your library so far. The PDFs stay on this computer. Inbox: <code>" + esc(L.inbox) + "</code></p>" + steps + "</section>" + exportBox +
    '<div class="filters"><select id="ftgroup">' + opts + '</select><input type="search" id="ftq" placeholder="Search title or author"></div><div id="ftlist"></div>';
  const sel = document.getElementById("ftgroup"), q = document.getElementById("ftq");
  sel.value = st3.group; q.value = st3.q;
  const list = () => {
    const words = q.value.toLowerCase().trim();
    const rows = L.rows.filter(r => (sel.value === "all" || (sel.value === "priority" ? r.group <= 2 : sel.value === "title" ? !r.abstract : String(r.group) === sel.value)) &&
      (!words || (r.title + " " + r.cite).toLowerCase().includes(words)));
    document.getElementById("ftlist").innerHTML = rows.length ? '<section class="block scroll"><table><thead><tr><th>No.</th><th>Study</th><th>Title</th><th>Agents had</th><th>Findings</th><th></th></tr></thead><tbody>' +
      rows.map(r => "<tr><td><b>" + r.work_id + "</b></td><td>" + esc(r.cite) + '<div class="hint" style="font-size:12px">' + esc(L.groups[r.group] ? L.groups[r.group].split(" (")[0] : "") + "</div></td><td>" + esc(r.title) + "</td><td>" + (r.abstract ? "abstract" : '<span class="warn">title only</span>') + "</td><td class=\"num\">" + r.findings + "</td><td style=\"white-space:nowrap\">" +
        Object.entries(r.links).map(([k, v]) => '<button class="btn" data-url="' + esc(v) + '">' + esc(k) + "</button> ").join("") +
        '<button class="btn" data-copy="' + r.work_id + '.pdf">Copy file name</button></td></tr>').join("") + "</tbody></table></section>"
      : '<p class="empty">Nothing to download here.</p>';
    document.querySelectorAll("#ftlist [data-url]").forEach(b => b.addEventListener("click", () => openOutside(b.dataset.url)));
    document.querySelectorAll("#ftlist [data-copy]").forEach(b => b.addEventListener("click", () => {
      navigator.clipboard.writeText(b.dataset.copy).then(() => toast("Copied " + b.dataset.copy + ": paste it as the file name when you save the PDF."),
        () => toast("Name the file " + b.dataset.copy, true));
    }));
  };
  sel.addEventListener("change", () => { st3.group = sel.value; save(KEY + ":fulltext", st3); list(); });
  q.addEventListener("input", () => { st3.q = q.value; save(KEY + ":fulltext", st3); list(); });
  list();
  const xdb = document.getElementById("xdb"), xq = document.getElementById("xq");
  const showString = () => { if (xq) { const d = dbs.find(x => x.platform === (xdb ? xdb.value : dbs[0].platform)) || dbs[0]; xq.textContent = d.string; } };
  if (xdb) xdb.addEventListener("change", showString);
  showString();
  const xadd = document.getElementById("xadd");
  if (xadd) xadd.addEventListener("click", () => {
    const f = document.getElementById("xfile").files[0];
    if (!f) { toast("Choose the exported file first.", true); return; }
    const reader = new FileReader();
    reader.onload = () => api("/api/export", {run: D.run, platform: xdb.value, filename: f.name, content: reader.result,
                                              hits: document.getElementById("xhits").value})
      .then(r => { toast("Registered as " + r.source + "; importing it now."); startJob({action: "sources"}); })
      .catch(e => toast("Could not add the export: " + e.message, true));
    reader.readAsText(f);
  });
  if (LIVE) {
    document.getElementById("ftinbox").addEventListener("click", () => api("/api/open-inbox", {}).catch(e => toast("Could not open the folder: " + e.message, true)));
    document.getElementById("ftadd").addEventListener("click", () => startJob({action: "pdfs"}));
  }
}
function renderHome() {
  const el = document.getElementById("home");
  const O = D.overview;
  if (!O) { el.innerHTML = '<section class="block"><p class="hint">No overview for this run yet.</p></section>'; return; }
  const fmt = n => Number(n).toLocaleString("en");
  const cards = O.stages.map(x => {
    const pct = x.of ? Math.round(100 * x.value / x.of) : null;
    return '<button class="stage" data-go="' + esc(x.tab) + '"><div class="st">' + esc(x.stage) + '</div>' +
      '<div class="big">' + fmt(x.value) + (x.of != null ? " <small>of " + fmt(x.of) + "</small>" : "") + "</div>" +
      '<div class="what">' + esc(x.label) + "</div>" + (x.detail ? '<div class="det">' + esc(x.detail) + "</div>" : "") +
      (pct != null ? '<div class="meter"><i style="width:' + pct + '%"></i></div>' : "") + "</button>";
  }).join("");
  const item = n => "<li><span>" + esc(n.text) + "</span>" +
      (n.job && LIVE ? '<button class="btn primary go" data-job="' + esc(n.job) + '">' + esc(n.action || "Do it now") + "</button>"
                     : (n.tab === "home" ? "" : '<button class="btn go" data-go="' + esc(n.tab) + '">Open ' + esc(PAGES[n.tab] ? PAGES[n.tab][0] : n.tab) + "</button>")) + "</li>";
  const must = O.next.filter(n => !n.optional), may = O.next.filter(n => n.optional);
  const next = (must.length ? '<ol class="next">' + must.map(item).join("") + "</ol>"
                : '<p class="hint">Nothing needs you right now. The pipeline writes and reviews the paper; you read it and choose what to explore next.</p>') +
    (may.length ? '<details class="optional"><summary>Optional: checks that would raise confidence (' + may.length + ")</summary>" +
                  '<ol class="next">' + may.map(item).join("") + "</ol></details>" : "");
  const DIRS = D.directions || [];
  const dirCard = d => '<div class="dir ' + esc(d.status) + '"><div class="dhead"><b>' + esc(d.rank + ". " + d.title) + "</b>" +
      '<span class="chip">' + esc(String(d.kind).replace(/_/g, " ")) + "</span>" +
      '<span class="hint">value ' + esc(d.value) + " · feasibility " + esc(d.feasibility) + " · novelty " + esc(d.novelty) + "</span></div>" +
      "<p><b>Question.</b> " + esc(d.question) + "</p><p><b>Why it matters.</b> " + esc(d.why) + "</p>" +
      "<p><b>What we know.</b> " + esc(d.evidence) + "</p><p><b>What we don't.</b> " + esc(d.unknown) + "</p>" +
      "<p><b>Next step.</b> " + esc(d.next_step) + "</p>" +
      (LIVE ? '<div class="dact">' + (d.status === "chosen" ? '<span class="decision accept">chosen</span> ' : "") +
              (d.status === "set_aside" ? '<span class="hint">set aside</span> ' : "") +
              '<button class="btn primary" data-dir="' + d.direction_id + '" data-st="chosen">Explore this</button>' +
              '<button class="btn" data-dir="' + d.direction_id + '" data-st="set_aside">Set aside</button></div>'
            : '<p class="hint">Status: ' + esc(String(d.status).replace(/_/g, " ")) + "</p>") + "</div>";
  const dirs = DIRS.length ? '<section class="block"><h2>What to explore next</h2><p class="hint">Proposed by the pipeline from the finished paper' +
    ((DIRS[0].papers || []).length > 1 ? "s, read together" : "") + ', ranked. Choose what is worth pursuing; Claude drafts the next run from what you choose.</p>' +
    DIRS.map(dirCard).join("") + "</section>" : "";
  let ms = "";
  if (O.paper) {
    const d = O.decision || {};
    ms = '<section class="block"><h2>Latest manuscript</h2><p><b>Version ' + O.paper.version + ":</b> " + esc(O.paper.title) + "</p><p>" +
      (d.decision ? 'Editor, round ' + esc(d.round) + ': <span class="decision ' + esc(d.decision) + '">' + esc(d.decision.replace(/_/g, " ")) + "</span>" +
        (d.overall != null ? " · overall " + esc(Math.round(d.overall * 10) / 10) + "/10" : "") : "No editorial decision yet.") +
      (d.audit ? " · sentence audit: " + d.audit.faithful + " of " + d.audit.of + " faithful" : "") + "</p>" +
      '<button class="btn" data-go="paper">Read it and its reviews</button></section>';
  }
  const FW = O.forward;
  const fwRow = c => "<tr><td>" + esc(c.claim) + '<div class="hint">' + esc(c.where || "") + "</div></td><td>" +
      (c.for + c.against ? esc(c.studies_for) + " for · " + esc(c.studies_against) + " against" : "–") + "</td>" +
      '<td><span class="fwst ' + esc(c.status.replace(/ /g, "_")) + '">' + esc(c.status) + "</span>" +
      (c.why && c.status !== "not tested yet" ? '<div class="hint">' + esc(c.why) + "</div>" : "") + "</td></tr>";
  const fwTable = cs => '<table class="fw"><thead><tr><th>Claim</th><th>Later studies</th><th>Status</th></tr></thead><tbody>' +
      cs.map(fwRow).join("") + "</tbody></table>";
  const fw = FW ? '<section class="block"><h2>Forward check of the papers</h2><p class="hint">' +
    (FW.frozen_at ? "The papers’ claims were frozen on " + esc(FW.frozen_at.slice(0, 10)) + " (sha256 " +
       esc((FW.frozen_sha256 || "").slice(0, 12)) + "…), before any later study was seen. Studies the review finds later, " +
       "first published from " + esc(FW.test_from) + " on, are judged blind against them." +
       (FW.intact === false ? " <b>The stored claims no longer match that hash.</b>" : "")
     : "The papers’ claims are being written down; they are frozen before any later study is seen.") + "</p><p>" +
    (FW.test_studies ? esc(FW.test_studies) + " later " + (FW.test_studies === 1 ? "study" : "studies") + " so far; " +
       esc(FW.judged) + " of " + esc(FW.test_findings) + " findings judged."
     : "No later study yet: the first come with the next search update.") + "</p>" +
    FW.papers.map(p => {
      const tested = p.claims.filter(c => c.status !== "not tested yet"), rest = p.claims.filter(c => c.status === "not tested yet");
      return "<h3>" + esc(p.name) + "</h3>" + (tested.length ? fwTable(tested) : "") +
        (rest.length ? '<details class="optional"><summary>' +
                       (tested.length ? rest.length + " claims not tested yet" : "Its " + rest.length + " frozen claims") +
                       "</summary>" + fwTable(rest) + "</details>" : "");
    }).join("") + "</section>" : "";
  const share = LIVE ? '<section class="block"><h2>Someone else checks</h2><p class="hint">A check pack is one file anyone can open in a browser to check the headline findings, the core studies and a blind screening sample. Their returned file is imported on the Check findings page.</p>' +
    '<a class="btn primary" href="/checkpack/' + D.run + '?sample=60">Download a check pack</a> <button class="btn" data-go="check">Import a returned file</button></section>' : "";
  el.innerHTML = '<section class="block"><h2>' + esc(D.title) + "</h2><p class=\"hint\">Corpus " + esc(D.corpus) + " · run " + D.run +
    " · protocol " + esc(D.version) + " · data built " + esc(D.generated) + "</p></section>" +
    '<div class="stages">' + cards + "</div>" +
    '<section class="block"><h2>What needs you</h2>' + next + "</section>" + ms + fw + dirs + share;
  el.querySelectorAll("[data-go]").forEach(b => b.addEventListener("click", () => go(b.dataset.go)));
  if (LIVE) el.querySelectorAll("[data-job]").forEach(b => b.addEventListener("click", () => startJob({action: b.dataset.job})));
  if (LIVE) el.querySelectorAll("[data-dir]").forEach(b => b.addEventListener("click", () =>
    api("/api/direction", {id: Number(b.dataset.dir), status: b.dataset.st, reviewer: LIVE.reviewer || ""})
      .then(() => { const d = (D.directions || []).find(x => x.direction_id === Number(b.dataset.dir)); if (d) d.status = b.dataset.st;
                    toast(b.dataset.st === "chosen" ? "Chosen. Tell Claude to start it, or it is picked up in the next session." : "Set aside."); renderHome(); })
      .catch(e => toast("Could not save: " + e.message, true))));
}

document.getElementById("export").addEventListener("click", () => {
  const rows = D.records.filter(r => mine[r.id] && mine[r.id].d);
  const open = rows.filter(r => mine[r.id].d === "exclude" && !mine[r.id].r);
  if (open.length && !confirm(open.length + " exclusions have no reason; import-screening will reject them. Export anyway?")) return;
  const cell = v => /[;"\n]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v;
  const lines = ["work_id;decision;exclude_reason;note"].concat(rows.map(r => {
    const m = mine[r.id];
    return [r.id, m.d, m.d === "exclude" ? (m.r || "") : "", (m.n || "").replace(/\r?\n/g, " ")].map(x => cell(String(x))).join(";");
  }));
  const blob = new Blob(["﻿" + lines.join("\r\n") + "\r\n"], {type: "text/csv;charset=utf-8"});
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "screening_decisions_corpus_" + D.corpus + "_run_" + String(D.run).padStart(3, "0") + ".csv";
  document.body.appendChild(a); a.click(); a.remove();
});

function renderMap() {
  const el = document.getElementById("map");
  const M = D.map;
  if (!M) { el.innerHTML = '<section class="block"><h2>No evidence map yet</h2><p class="hint">The map appears once an extraction agent has recorded the methods, scales and settings of the kept studies.</p></section>'; return; }
  const title = id => "#" + id + " " + esc((M.titles[id] || "").slice(0, 120));
  const count = n => { let k = n.studies.length; for (const c of n.children) k += count(c); return k; };
  const node = (n, depth) => {
    const dim = M.tree_dims[depth] || "";
    const kids = n.children.map(c => node(c, depth + 1)).join("");
    const leaf = n.studies.length ? "<ul>" + n.studies.map(s => "<li>" + title(s) + "</li>").join("") + "</ul>" : "";
    return "<details" + (depth < 1 ? " open" : "") + '><summary><span class="dim">' + esc(dim) + "</span>" + esc(n.label) +
      ' <span class="n">(' + count(n) + ")</span></summary>" + kids + leaf + "</details>";
  };
  const key = on => Object.entries(on).map(([k, v]) => '<span class="chip">' + esc(k) + ": " + esc(v) + "</span>").join(" ");
  let html = '<section class="block"><h2>Tree</h2><p class="hint">Kept studies by ' + M.tree_dims.map(esc).join(" → ") +
    ". Extracted by " + esc(short(M.extractor)) + " from titles and abstracts.</p><div class=\"tree\">" + M.tree.map(n => node(n, 0)).join("") + "</div></section>";
  if (M.across) {
    html += '<section class="block"><h2>Matches across ' + esc(M.across) + '</h2><p class="hint">Studies that agree on ' +
      M.match_on.map(esc).join(", ") + " but differ in " + esc(M.across) + ": their findings can be compared directly.</p>" +
      (M.matches.length ? M.matches.map(m => "<p>" + key(m.on) + (m.n_studies === 1 ? ' <span class="meta">within one study</span>' : "") + "</p><ul>" + Object.entries(m.studies).map(([a, ids]) =>
        "<li><b>" + esc(a) + "</b>: " + ids.map(title).join("; ") + "</li>").join("") + "</ul>").join("") : '<p class="hint">No such pair yet.</p>') + "</section>";
    html += '<section class="block"><h2>Gaps</h2><p class="hint">For each comparable group, the ' + esc(M.across) +
      " categories with no study at all.</p>" + (M.gaps.length ? "<table><tr><th>Group</th><th>Has</th><th>Missing</th></tr>" +
      M.gaps.map(g => "<tr><td>" + key(g.on) + "</td><td>" + Object.entries(g.present).map(([a, n]) => esc(a) + " (" + n + ")").join(", ") +
      "</td><td>" + g.missing.map(x => '<span class="chip">' + esc(x) + "</span>").join(" ") + "</td></tr>").join("") + "</table>" : '<p class="hint">None.</p>') + "</section>";
    for (const [dim, mx] of Object.entries(M.matrix)) {
      html += '<section class="block"><h2>' + esc(M.across) + " × " + esc(dim) + '</h2><p class="hint">Number of studies; shaded cells are empty.</p><div class="scroll"><table><tr><th></th>' +
        mx.cols.map(c => "<th>" + esc(c) + "</th>").join("") + "</tr>" + mx.rows.map(r => "<tr><th>" + esc(r) + "</th>" +
        mx.cols.map(c => { const n = (mx.cells[r] || {})[c] || 0; return n ? '<td class="num">' + n + "</td>" : '<td class="gap">0</td>'; }).join("") + "</tr>").join("") + "</table></div></section>";
    }
  }
  if (M.claims.length) {
    html += '<section class="block"><h2>Claims</h2>' + M.claims.map(c => "<p><b>" + esc(c.id) + "</b> " + esc(c.claim) +
      ' <span class="meta">' + esc(c.source + " " + c.locator) + "</span></p>" + (Object.keys(c.links).length ? "<ul>" +
      Object.entries(c.links).map(([rel, ls]) => "<li><b>" + esc(rel) + "</b>: " + ls.map(l => title(l.work_id) +
      (l.reason ? " — " + esc(l.reason) : "")).join("; ") + "</li>").join("") + "</ul>" : '<p class="hint">No study linked yet.</p>')).join("") + "</section>";
  }
  html += '<section class="block"><h2>Vocabulary</h2><p class="hint">Every label the extractor used. Merge synonyms under [map.aliases] in the protocol, then rebuild the report.</p>' +
    Object.entries(M.vocabulary).map(([d, vs]) => "<p><b>" + esc(d) + "</b> " + vs.map(([v, n]) => '<span class="chip">' + esc(v) + " " + n + "</span>").join(" ") + "</p>").join("") + "</section>";
  el.innerHTML = html;
}

function findingsPanel(r) {
  if (!r.findings.length) return "";
  return '<details class="ev"><summary>Findings (' + r.findings.length + ")</summary><ul>" + r.findings.map(f =>
    "<li><b>" + esc(f.finding) + "</b>" + (f.for_whom ? " <span class=\"chip\">" + esc(f.for_whom) + "</span>" : "") +
    (f.effect ? " <span class=\"meta\">" + esc(f.effect) + "</span>" : "") +
    "<br><span class=\"meta\">“" + esc(f.evidence) + "” — " + esc(f.location || "") + "</span>" +
    (f.grounded === 0 ? ' <span class="ungrounded">quote not found in the record</span>' : "") + "</li>").join("") + "</ul></details>";
}

function renderFindings() {
  const el = document.getElementById("findings");
  const rows = D.records.flatMap(r => r.findings.map(f => Object.assign({work: r.id, title: r.title}, f)));
  if (!rows.length) { el.innerHTML = '<section class="block"><h2>No findings yet</h2><p class="hint">Run a findings task: one row per result of each kept study, with the passage that states it.</p></section>'; return; }
  const fields = D.finding_fields;
  el.innerHTML = '<section class="block"><h2>Findings</h2><p class="hint">' + rows.length + " findings from " +
    new Set(rows.map(r => r.work)).size + ' studies. Each row is one result with the passage that states it; type to filter.</p>' +
    '<div class="filters"><input type="search" id="fq" placeholder="Filter findings"></div><div class="scroll"><table id="ft"><tr><th>#</th><th>Study</th>' +
    fields.map(f => "<th>" + esc(f) + "</th>").join("") + "<th>For whom</th><th>Finding</th><th>From</th></tr>" +
    rows.map(r => '<tr data-text="' + esc((r.finding + " " + (r.for_whom || "") + " " + Object.values(r.fields).join(" ") + " " + r.title).toLowerCase()) + '"><td>' + r.id + "</td><td title=\"" + esc(r.title) + "\">#" + r.work + "</td>" +
      fields.map(f => "<td>" + esc(r.fields[f] || "") + "</td>").join("") + "<td>" + esc(r.for_whom || "") + "</td><td>" + esc(r.finding) +
      (r.effect ? ' <span class="meta">' + esc(r.effect) + "</span>" : "") + "</td><td>" + esc(r.source || "not found") + "</td></tr>").join("") +
    "</table></div></section>";
  document.getElementById("fq").addEventListener("input", e => {
    const q = e.target.value.toLowerCase();
    el.querySelectorAll("#ft tr[data-text]").forEach(tr => { tr.hidden = q && !tr.dataset.text.includes(q); });
  });
}

function renderInsights() {
  const el = document.getElementById("insights");
  const kinds = ["pattern", "anomaly", "contradiction", "gap", "method", "hypothesis", "question"];
  const form = LIVE ? '<section class="block"><h2>Add an insight</h2><p class="hint">Something you noticed: a pattern, an anomaly, a gap, a hypothesis. It starts as <i>noted</i>.</p>' +
    '<form class="add" id="addins"><select name="kind">' + kinds.map(k => "<option>" + k + "</option>").join("") + "</select>" +
    '<textarea name="statement" rows="2" placeholder="One sentence: what you noticed" required></textarea>' +
    '<textarea name="rationale" rows="2" placeholder="Why you think so (optional)"></textarea>' +
    '<input name="works" placeholder="Study numbers it rests on, e.g. 20, 46 (optional)">' +
    '<button class="btn primary" type="submit">Add insight</button></form></section>' : "";
  if (!D.insights.length) {
    el.innerHTML = form + '<section class="block"><h2>No insights yet</h2><p class="hint">Agents add insights while they work and the synthesis task proposes cross-study ones' + (LIVE ? "; add your own above." : ".") + "</p></section>";
  } else {
    const titles = Object.fromEntries(D.records.map(r => [r.id, r.title]));
    const editor = i => LIVE ? '<div class="ins-edit" data-ins="' + i.id + '"><label class="meta">Status <select data-status>' +
      Object.keys(D.statuses).map(s => "<option" + (s === i.status ? " selected" : "") + ">" + s + "</option>").join("") +
      '</select></label><input data-note placeholder="Note (optional): why, or what you checked"><span class="meta" data-ok></span></div>' : "";
    const reviewed = D.insights.filter(i => i.status !== "noted").length;
    el.innerHTML = form + (LIVE ? '<p class="summary">You have reviewed ' + reviewed + " of " + D.insights.length +
      " insights. Picking a status saves it at once; a note is saved when you leave its field.</p>" : "") +
      Object.keys(D.statuses).filter(s => D.insights.some(i => i.status === s)).map(s =>
      '<section class="block"><h2>' + esc(s) + " (" + D.insights.filter(i => i.status === s).length + ') <span class="meta">' + esc(D.statuses[s]) + "</span></h2>" +
      D.insights.filter(i => i.status === s).map(i =>
        '<div class="ins"><span class="chip">#' + i.id + " " + esc(i.kind) + "</span> <b>" + esc(i.statement) + "</b><br>" +
        (i.rationale ? esc(i.rationale) + "<br>" : "") +
        '<span class="meta">' + esc(i.author) + ", " + esc(i.stage) + ", " + esc(i.created) +
        ((i.works || []).length ? " · studies " + i.works.map(w => '<span title="' + esc(titles[w] || "") + '">#' + w + "</span>").join(", ") : "") +
        ((i.findings || []).length ? " · findings " + i.findings.map(f => "#" + f).join(", ") : "") + "</span>" +
        ((i.quotes || []).length ? '<br><span class="meta">' + i.quotes.map(q => "#" + q.work_id + ": “" + esc(q.text) + "”" +
          (q.grounded === false ? ' <span class="ungrounded">not found</span>' : "")).join("; ") + "</span>" : "") +
        ((i.unverified_numbers || []).length ? '<br><span class="ungrounded">⚠ numbers not in the cited findings: ' + esc(i.unverified_numbers.join(", ")) + "</span>" : "") +
        editor(i) + "</div>").join("") +
      "</section>").join("") + '<p class="hint">' + (LIVE ? "Every status change is kept with its date, your name and note. " : "Change a status with <code>python -m ichnos insight status &lt;id&gt; supported</code>, or open the app. ") +
      "The full history is in notes/insights_corpus_" + esc(D.corpus) + ".md.</p>";
  }
  if (!LIVE) return;
  const saveInsight = async box => {
    const ok = box.querySelector("[data-ok]");
    if (!reviewer()) { toast("Enter your name at the top first.", true); return; }
    const status = box.querySelector("[data-status]").value, note = box.querySelector("[data-note]").value;
    ok.textContent = "saving…";
    try {
      await api("/api/insight/status", {id: +box.dataset.ins, status, note, reviewer: reviewer()});
      D.insights.find(i => i.id === +box.dataset.ins).status = status;
      ok.textContent = "✓ saved as " + status; ok.className = "meta saved";
      const n = D.insights.filter(i => i.status !== "noted").length;
      const sum = el.querySelector(".summary");
      if (sum) sum.textContent = "You have reviewed " + n + " of " + D.insights.length +
        " insights. Picking a status saves it at once; a note is saved when you leave its field.";
    } catch (e) {
      ok.textContent = "✗ not saved"; ok.className = "ungrounded";
      toast("Not saved: " + (e.message.includes("fetch") ? "the app is not running. Double-click Ichnos.bat and reload this page." : e.message), true);
    }
  };
  el.querySelectorAll(".ins-edit").forEach(box => {
    box.querySelector("[data-status]").addEventListener("change", () => saveInsight(box));
    box.querySelector("[data-note]").addEventListener("change", () => saveInsight(box));
  });
  const f = document.getElementById("addins");
  if (f) f.addEventListener("submit", async ev => {
    ev.preventDefault();
    if (!reviewer()) { toast("Enter your name at the top first.", true); return; }
    const works = (f.works.value.match(/\d+/g) || []).map(Number);
    try {
      const out = await api("/api/insight/add", {corpus: D.corpus, run: D.run, kind: f.kind.value, statement: f.statement.value,
                                                 rationale: f.rationale.value, works, reviewer: reviewer()});
      D.insights.push(out.insight);
      toast("Insight #" + out.insight.id + " noted");
      renderInsights();
    } catch (e) { toast("Not saved: " + e.message, true); }
  });
}

function renderDraft() {
  const el = document.getElementById("draft");
  const R = D.draft;
  if (!R) { el.innerHTML = '<section class="block"><h2>No draft yet</h2><p class="hint">The writer drafts a section from the findings and the insights you have not dropped; every sentence must cite the finding rows it rests on, and the pipeline checks each one.</p></section>'; return; }
  const checks = Object.fromEntries(R.checks.map(c => [c.p + ":" + c.s, c.problems]));
  const t = R.totals;
  const unreviewed = D.insights.filter(i => i.status === "noted").length;
  let html = '<section class="block"><h2>' + esc(R.title || "Draft") + '</h2><p class="hint">Draft ' + R.id + " by " + esc(R.author) + ", " + esc(R.created) +
    ". " + t.verified + " of " + t.sentences + " sentences verified, " + t.frame + " framing, " + t.flagged + " flagged. " +
    "Hover a citation to see the passage behind it. Green underline: checked; orange: flagged; grey: framing." +
    (unreviewed ? " <b>" + unreviewed + " of the insights the writer could use are not yet reviewed by you.</b>" : "") +
    ' The text with references is in <code>' + esc(R.file) + "</code>.</p>";
  R.paragraphs.forEach((p, pi) => {
    if (p.heading) html += "<h3>" + esc(p.heading) + "</h3>";
    html += '<p class="draft">' + p.sentences.map((s, si) => {
      const probs = checks[pi + ":" + si] || [];
      const cls = probs.length ? "s-flag" : (s.frame ? "s-frame" : "s-ok");
      const works = [];
      for (const c of s.cites || []) { const f = R.cited[String(c)]; if (f && !works.some(w => w.work === f.work)) works.push(Object.assign({id: c}, f)); }
      const cites = works.length ? " (" + works.map(w => '<span class="cite" title="' + esc("F" + w.id + " · " + (w.source || "") + " · “" + w.evidence + "”") + '">' + esc(w.cite) + "</span>").join("; ") + ")" : "";
      return '<span class="' + cls + '"' + (probs.length ? ' title="' + esc(probs.join("; ")) + '"' : "") + ">" + esc(s.text.trim()) + "</span>" + cites;
    }).join(" ") + "</p>";
  });
  const flagged = R.checks.filter(c => c.problems.length);
  html += flagged.length ? "<h3>Flagged sentences</h3><ul>" + flagged.map(c => "<li>“" + esc(R.paragraphs[c.p].sentences[c.s].text) + "” — " + esc(c.problems.join("; ")) + "</li>").join("") + "</ul>"
    : '<p class="hint">No sentence was flagged by the checks.</p>';
  el.innerHTML = html + "</section>";
}

// A small Markdown renderer for the manuscript: headings, paragraphs, lists, bold, italics.
function md(text) {
  const inline = s => esc(s).split(/(`[^`]+`)/).map(part => part.startsWith("`") && part.endsWith("`") && part.length > 1
    ? "<code>" + part.slice(1, -1) + "</code>"
    : part.replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/(^|[\s(])\*([^*\s][^*]*?)\*(?=[\s).,;:]|$)/g, "$1<i>$2</i>")).join("");
  const out = []; let list = false;
  for (const line of text.split("\n")) {
    const h = line.match(/^(#{1,3}) (.*)/);
    if (list && !line.startsWith("- ")) { out.push("</ul>"); list = false; }
    if (h) out.push("<h" + (h[1].length + 1) + ">" + inline(h[2]) + "</h" + (h[1].length + 1) + ">");
    else if (line.startsWith("- ")) { if (!list) { out.push("<ul>"); list = true; } out.push("<li>" + inline(line.slice(2)) + "</li>"); }
    else if (line.trim()) out.push("<p>" + inline(line).replace(/\[⚠\]/g, '<span class="ungrounded">[⚠]</span>') + "</p>");
  }
  if (list) out.push("</ul>");
  return out.join("");
}

function renderPaper() {
  const el = document.getElementById("paper");
  const P = D.papers || [];
  if (!P.length) { el.innerHTML = '<section class="block"><h2>No manuscript yet</h2><p class="hint">Prepare a <i>paper</i> task in the Agents tab: a full manuscript written from the findings, then an evidence audit, independent reviewers, an editor\'s decision and a revision.</p></section>'; return; }
  const pick = st.paper && P.find(p => p.id === st.paper) ? st.paper : P[P.length - 1].id;
  const v = P.find(p => p.id === pick);
  const t = v.verification;
  const peers = v.reviews.filter(r => r.kind === "peer"), metas = v.reviews.filter(r => r.kind === "meta"), audits = v.reviews.filter(r => r.kind === "audit");
  const rubric = ["originality", "rigor", "evidence", "contribution", "clarity", "transparency"];
  let side = '<section class="block"><details><summary><b>Reviews, audit and editor decisions for version ' + v.version + "</b> (click to open)</summary>";
  if (audits.length) {
    const a = audits[audits.length - 1];
    side += "<p><b>Evidence audit</b> (" + esc(a.reviewer) + "): " + Object.entries(a.content.counts).map(([k, n]) => esc(k) + " " + n).join(", ") + "</p>";
    const bad = a.content.verdicts.filter(x => x.verdict !== "faithful");
    if (bad.length) side += "<details class=\"ev\"><summary>Sentences the auditor flagged (" + bad.length + ")</summary><ul>" + bad.map(x => "<li><b>" + esc(x.verdict) + "</b> " + esc(x.id) + ": " + esc(x.reason || "") + (x.fix ? " <i>Fix: " + esc(x.fix) + "</i>" : "") + "</li>").join("") + "</ul></details>";
  }
  if (peers.length) side += '<div class="scroll"><table><tr><th>Reviewer</th>' + rubric.map(k => "<th>" + k + "</th>").join("") + "<th>Overall</th><th>Decision</th></tr>" +
    peers.map(r => "<tr><td>" + esc(r.reviewer) + "</td>" + rubric.map(k => '<td class="num">' + esc(r.content[k]) + "</td>").join("") + '<td class="num"><b>' + esc(r.content.overall) + "</b></td><td>" + esc(r.content.decision) + "</td></tr>").join("") + "</table></div>" +
    peers.map(r => "<details class=\"ev\"><summary>" + esc(r.reviewer) + ": " + esc(r.content.decision) + "</summary><p>" + esc(r.content.summary) + "</p><p><b>Strengths</b></p><ul>" + (r.content.strengths || []).map(s => "<li>" + esc(s) + "</li>").join("") + "</ul><p><b>Weaknesses</b></p><ul>" + (r.content.weaknesses || []).map(s => "<li>" + esc(s) + "</li>").join("") + "</ul><p><b>Requested changes</b></p><ul>" + (r.content.requested_changes || []).map(c => "<li>[" + esc(c.severity) + "] " + esc(c.change) + " <span class=\"meta\">" + esc(c.location || "") + "</span></li>").join("") + "</ul></details>").join("");
  for (const m of metas) side += "<p><b>Editor, round " + m.round + ": " + esc(m.decision) + "</b><br>" + esc(m.content.summary) + "</p>" +
    ((m.content.required_changes || []).length ? "<ul>" + m.content.required_changes.map(c => "<li><b>" + esc(c.id) + "</b> [" + esc(c.severity) + (c.needs === "author" ? ", needs you" : "") + "] " + esc(c.change) + "</li>").join("") + "</ul>" : "") +
    ((m.content.assessment || []).length ? "<ul>" + m.content.assessment.map(a => "<li><b>" + esc(a.id) + "</b> addressed: " + esc(a.addressed) + " — " + esc(a.comment) + "</li>").join("") + "</ul>" : "");
  if (!v.reviews.length) side += '<p class="hint">Not reviewed yet.</p>';
  side += "</details></section>";
  const doc = LIVE ? "/paper/" + v.id : "papers/" + v.file.split("/").pop().replace(/\.md$/, ".html");
  el.innerHTML = '<section class="block"><h2>Manuscript</h2><p class="hint"><select id="papersel">' + P.map(p => '<option value="' + p.id + '"' + (p.id === pick ? " selected" : "") + ">Paper " + p.id + ", version " + p.version + " (" + esc(p.created) + ")</option>").join("") + "</select> " +
    t.verified + " of " + t.sentences + " sentences verified, " + t.flagged + " flagged, " + t.words + " words. " +
    '<a class="btn primary" href="' + esc(doc) + '" target="_blank" rel="noopener">Open as a document ↗</a> (to read, print or save as PDF). File: <code>' + esc(v.file) + "</code></p></section>" +
    '<section class="block paper">' + md(v.text) + "</section>" + side;
  document.getElementById("papersel").addEventListener("change", e => { st.paper = +e.target.value; renderPaper(); });
}

function sharePanel() {
  if (!LIVE) return "";
  return '<section class="block"><h2>Someone else checks</h2><p class="hint">' +
    "Download a check pack: one HTML file that anyone can open in a browser, with nothing to install. " +
    "It holds the findings behind the headline claims, the core studies and a blind random sample of records to screen. " +
    "The person sends back the file the pack saves; import it here. Their checks then count in the Declaration, with their name.</p>" +
    '<div class="filters"><label class="meta">Records in the blind sample <input id="cksample" type="number" min="0" max="500" value="60" style="width:5em"></label>' +
    '<a class="btn primary" id="ckpack" href="/checkpack/' + D.run + '?sample=60">Download a check pack</a>' +
    '<label class="btn">Import a returned checks file<input id="ckimport" type="file" accept=".json,application/json" hidden></label></div></section>';
}
function wireShare() {
  const s = document.getElementById("cksample"), a = document.getElementById("ckpack"), f = document.getElementById("ckimport");
  if (!s) return;
  s.addEventListener("input", () => { a.href = "/checkpack/" + D.run + "?sample=" + (+s.value || 0); });
  f.addEventListener("change", async e => {
    const file = e.target.files[0]; if (!file) return;
    try {
      const out = await api("/api/checks/import", {content: await file.text()});
      const c = out.counts;
      toast("Imported for " + out.checker + ": " + c.findings + " finding checks, " + c.core + " core studies, " + c.screening + " screening decisions. Reloading.");
      setTimeout(() => location.reload(), 1500);
    } catch (err) { toast("Not imported: " + err.message, true); }
    e.target.value = "";
  });
}
function renderCheck() {
  const el = document.getElementById("check");
  const C = D.check;
  if (!C || !C.items.length) {
    el.innerHTML = sharePanel() + '<section class="block"><h2>Nothing to check yet</h2><p class="hint">Once a manuscript exists, this page lists every finding its headline claims rest on, so you can check each against its source.</p></section>';
    wireShare();
    return;
  }
  const parts = C.parts.join(", ");
  const sum = () => {
    const done = C.items.filter(i => i.check), conf = done.filter(i => i.check.status === "confirmed").length;
    return (LIVE ? "You have checked " : "Checked: ") + "<b>" + done.length + " of " + C.items.length + "</b> findings (" + conf + " confirmed, " +
      (done.length - conf) + " corrected). Insights reviewed: <b>" + C.insights.reviewed + " of " + C.insights.total + "</b> (Insights tab).";
  };
  const st2 = load(KEY + ":check", {show: "todo"});
  let html = '<section class="block"><h2>Check the findings behind the headline claims</h2><p class="hint">' +
    "Paper " + C.paper + ", version " + C.version + ": the " + esc(parts) + " rest on these " + C.items.length + " findings. " +
    "For each, read the passage (or open the study) and decide whether the finding says what the source says. " +
    "<b>Confirmed</b>: it does. <b>Corrected</b>: it does not, or only partly; write what the source actually says. " +
    "Say whether you checked against the passage shown or the full text. " +
    "The manuscript's Declaration of AI use is written from these checks, and the next revision must follow every correction.</p>" +
    '<p class="summary" id="cksum">' + sum() + "</p>" +
    '<div class="filters"><select id="ckshow"><option value="todo">Not checked yet</option><option value="done">Checked</option>' +
    '<option value="corrected">Corrected</option><option value="all">All</option></select></div></section><div id="cklist"></div>';
  el.innerHTML = sharePanel() + html;
  wireShare();
  const show = document.getElementById("ckshow");
  show.value = st2.show;
  const card = i => {
    const c = i.check;
    const last = c ? "Last check: <b>" + esc(c.status) + "</b> by " + esc(c.checker.replace(/^human:/, "")) + " on " + esc(c.created_at.slice(0, 10)) +
      " against " + esc(c.against) + (c.correction ? " — correction: " + esc(c.correction) : "") + (c.note ? " — note: " + esc(c.note) : "") : "Not checked yet.";
    const opt = (v, label) => '<label class="opt"><input type="radio" name="st' + i.id + '" value="' + v + '"' + ((c ? c.status : "") === v ? " checked" : "") + "> " + label + "</label>";
    const edit = LIVE ? '<div class="edit">' + opt("", "Not checked") + opt("confirmed", "Confirmed") + opt("corrected", "Corrected") +
      '<select data-against>' + Object.entries(C.against).map(([k, v]) => '<option value="' + esc(k) + '"' + ((c && c.against) === k ? " selected" : "") + ">checked against " + esc(v) + "</option>").join("") + "</select>" +
      '<input data-note placeholder="Note (optional): what you checked, page or table" value="' + esc(c && c.note || "") + '">' +
      '<textarea data-correction rows="2" placeholder="What the source actually says (needed for Corrected)"' + (c && c.status === "corrected" ? "" : " hidden") + ">" + esc(c && c.correction || "") + "</textarea>" +
      '<span class="meta" data-ok></span></div>' : "";
    return '<div class="ck' + (c ? " st-" + esc(c.status) : "") + '" data-f="' + i.id + '"><header><span class="chip">Finding #' + i.id + "</span> <b>" + esc(i.cite) + "</b> " +
      '<span title="' + esc(i.title) + '">' + esc(i.title.length > 90 ? i.title.slice(0, 89) + "…" : i.title) + "</span>" +
      (i.link ? ' <a href="' + esc(i.link) + '" target="_blank" rel="noopener">open the study ↗</a>' : "") +
      ' <span class="chip">passage found in the ' + esc(i.source) + "</span>" + (i.fulltext ? ' <span class="chip">full text parsed</span>' : "") + "</header>" +
      '<ul class="uses">' + i.uses.map(u => "<li><b>" + esc(u.part) + ":</b> " + esc(u.text) + "</li>").join("") + "</ul>" +
      '<p class="says"><b>The finding says:</b> ' + esc(i.finding) + (i.effect ? ' <span class="meta">' + esc(i.effect) + "</span>" : "") +
      (i.for_whom ? ' <span class="chip">' + esc(i.for_whom) + "</span>" : "") + "</p>" +
      "<blockquote>“" + esc(i.evidence) + "”</blockquote>" + (i.location ? '<span class="meta">Where in the study: ' + esc(i.location) + "</span>" : "") +
      '<p class="meta" data-last>' + last + "</p>" + edit + "</div>";
  };
  const list = () => {
    const pick = {todo: i => !i.check, done: i => !!i.check, corrected: i => i.check && i.check.status === "corrected", all: () => true}[show.value];
    const rows = C.items.filter(pick);
    document.getElementById("cklist").innerHTML = rows.length ? rows.map(card).join("") : '<p class="empty">Nothing here.</p>';
    if (LIVE) document.querySelectorAll("#cklist .ck").forEach(wire);
  };
  const wire = box => {
    const id = +box.dataset.f, item = C.items.find(i => i.id === id);
    const ok = box.querySelector("[data-ok]"), corr = box.querySelector("[data-correction]");
    const saveCheck = async () => {
      if (!reviewer()) { toast("Enter your name at the top first, so the check is attributed to you.", true); return; }
      const status = (box.querySelector("input[type=radio]:checked") || {}).value || "";
      corr.hidden = status !== "corrected";
      if (status === "corrected" && !corr.value.trim()) { corr.focus(); ok.textContent = "write the correction; it is saved when you leave the field"; ok.className = "meta"; return; }
      ok.textContent = "saving…"; ok.className = "meta";
      try {
        const out = await api("/api/check", {finding_id: id, paper: C.paper, status, against: box.querySelector("[data-against]").value,
                                             note: box.querySelector("[data-note]").value, correction: corr.value, reviewer: reviewer()});
        item.check = out.check;
        box.className = "ck" + (out.check ? " st-" + out.check.status : "");
        box.querySelector("[data-last]").innerHTML = out.check ? "Last check: <b>" + esc(out.check.status) + "</b> by " + esc(reviewer()) + " just now against " + esc(out.check.against) : "Not checked yet.";
        ok.textContent = "✓ saved"; ok.className = "meta saved";
        document.getElementById("cksum").innerHTML = sum();
      } catch (e) {
        ok.textContent = "✗ not saved"; ok.className = "ungrounded";
        toast("Not saved: " + (e.message.includes("fetch") ? "the app is not running. Double-click Ichnos.bat and reload this page." : e.message), true);
      }
    };
    box.querySelectorAll("input[type=radio]").forEach(r => r.addEventListener("change", saveCheck));
    box.querySelector("[data-against]").addEventListener("change", () => { if (item.check) saveCheck(); });
    box.querySelector("[data-note]").addEventListener("change", () => { if (item.check) saveCheck(); });
    corr.addEventListener("change", saveCheck);
  };
  show.addEventListener("change", () => { st2.show = show.value; save(KEY + ":check", st2); list(); });
  list();
}

// Interpretations: the lens tournament. Read-only: the gates, the literature verdicts and the test cards.
const arr = x => Array.isArray(x) ? x : x == null || x === "" ? [] : [x];    // agents' answers: never trust the shape
const DERIVATION = {follows_from_core: "from the core", needs_auxiliary: "with an auxiliary", common_sense: "common sense",
                    contradicts_core: "contradicts its core", disputed: "judges disagree"};
const VERDICT = {
  novel: "No judge saw a prior or neighbouring test among the hits of these logged searches. Not proof that none exists.",
  near: "Every judge says a hit tests a close neighbour: the same mechanism with another outcome or population. A claimed direct test whose quote the pipeline could not find also counts here, not as prior.",
  contested: "The judges split: only some say a hit states or tests it (with a quote found in the hit), or only some say a hit tests a close neighbour.",
  prior: "Every judge says a hit already states or tests it, each with a quote the pipeline found in the hit's title or abstract.",
  not_searchable: "No hit came back with an abstract the judges could read.",
  unjudged: "Some hits with an abstract are not labelled by every judge yet (for example after searching again), so a prior could hide among them.",
  not_searched: "Credited, but not searched yet: no search strings, or the search has not run."};
const VERDICT_ORDER = ["novel", "near", "contested", "prior", "not_searchable", "unjudged", "not_searched"];
const GATE1 = {untestable: "not testable", restates: "restates a finding", in_baseline: "already in the no-lens baseline"};
function renderInterpret() {
  const el = document.getElementById("interpret");
  const I = D.interpret;
  const intro = '<p class="hint">Several agents read the same findings through different theories (lenses), each twice and blind to the others. ' +
    "A placebo lens with no theoretical content shows what an agent says anyway. Each hypothesis then passes three gates, labelled blind by the judges. " +
    "A hypothesis counts as judged once every judge has labelled it, and credit needs every judge.</p>";
  if (!I) { el.innerHTML = '<section class="block"><h2>No lenses for this corpus</h2>' + intro + '<p class="hint">Write a lens file, <code>protocol/lenses_corpus_' + esc(String(D.corpus).toLowerCase()) + ".toml</code>, then prepare <i>interpret</i> tasks (README, section Lenses).</p></section>"; return; }
  if (I.error) { el.innerHTML = '<section class="block"><h2>The tournament could not be shown</h2><p class="warn">' + esc(I.error) + '</p><p class="hint">Lens file: <code>' + esc(I.file) + '</code>. Run <code>python -m ichnos lenses ' + D.run + "</code> to see the full error.</p></section>"; return; }
  if (I.empty) { el.innerHTML = '<section class="block"><h2>No interpretations yet</h2>' + intro + '<p class="hint">The lens file <code>' + esc(I.file) + "</code> exists, but tournament <b>" + esc(I.tournament) + "</b> has no imported <i>interpret</i> answers for this run.</p></section>"; return; }

  const lensList = arr(I.lenses), order = lensList.map(l => l.id);
  const PALETTE = ["#3451d1", "#c0392b", "#10988c", "#8c3cc8", "#c27c0e"];
  const role = id => (lensList.find(l => l.id === id) || {}).role || "";
  const color = id => role(id) === "placebo" ? "#8a8a90" : PALETTE[Math.max(0, order.indexOf(id)) % PALETTE.length];
  const name = id => String(id).charAt(0).toUpperCase() + String(id).slice(1);
  const chip = id => '<span class="lens-chip" style="--lc:' + color(id) + '">' + esc(name(id)) + "</span>";
  const vname = v => esc(String(v).replace(/_/g, " "));
  const verdict = v => v ? '<span class="verdict v-' + esc(v) + '" title="' + esc(VERDICT[v] || "") + '">' + vname(v) + "</span>" : "";
  const findings = I.findings || {};
  const fcite = id => { const f = findings[String(id)]; return "#" + esc(id) + (f && f.cite ? " " + esc(f.cite) : ""); };
  const ftext = id => { const f = findings[String(id)]; return esc(f ? f.finding : "not among the current findings"); };
  const pct = x => x == null ? "n/a" : Math.round(100 * x) + "%";
  const kappa = a => a.kappa != null ? ", κ " + a.kappa.toFixed(2) : a.agree === 1 ? ", κ undefined: every label the same" : "";
  const sameModel = js => js.length > 1 && new Set(js.map(j => short(j).replace(/-j\d+$/, ""))).size === 1;
  const judgeText = js => js.map(j => esc(short(j))).join(", ") + (sameModel(js) ? " (runs of one model, not independent raters)" : "");
  const hypotheses = arr(I.hypotheses), gatesAll = arr(I.gates), sets = arr(I.cards);
  const fs = Object.assign({lens: "all", show: "credited", verdict: "any", q: ""}, load(KEY + ":interp", {}));
  fs.q = "";

  // 1. The gates
  const theory = gatesAll.filter(g => g.source === "tournament"), control = gatesAll.filter(g => g.source !== "tournament");
  const G2 = [["Common sense", "common_sense", "Both judges: it follows from common sense, not from the lens's core"],
    ["From core", "from_core", "Both judges: it follows from the core alone"],
    ["With auxiliary", "with_auxiliary", "Both judges: it follows from the core plus a named auxiliary assumption (or one says from the core, the other with an auxiliary)"]]
    .concat(gatesAll.some(g => g.contradicts) ? [["Contradicts core", "contradicts", "Both judges: it contradicts the lens's own core"]] : [])
    .concat([["Disputed", "disputed", "The judges give labels that do not agree on whether it follows: no credit"]])
    .concat(gatesAll.some(g => g.unjudged2) ? [["Pending", "unjudged2", "Passed gate 1, not yet labelled by every gate-2 judge"]] : []);
  const G3 = [["Novel", "novel", VERDICT.novel], ["Near", "near", VERDICT.near], ["Contested", "contested", VERDICT.contested],
    ["Prior", "prior", VERDICT.prior], ["Other", "other", "Not searchable, unjudged, or not searched yet"]];
  const breakdown = g => [[g.from_core, "from the core"], [g.with_auxiliary, "with an auxiliary"], [g.disputed + (g.contradicts || 0), "disputed or contradicting the core"],
    [g.common_sense, "common sense"], [g.unjudged2 || 0, "gate 2 pending"], [g.submitted - g.gate1, "out at gate 1 or not judged"]].filter(x => x[0]);
  const bar = g => {
    const n = Math.max(1, g.submitted), cls = {"from the core": "f-core", "with an auxiliary": "f-aux", "disputed or contradicting the core": "f-disp",
      "common sense": "f-cs", "gate 2 pending": "f-pend", "out at gate 1 or not judged": "f-out"};
    const parts = breakdown(g);
    return '<div class="funnel" role="img" aria-label="' + esc(g.submitted + " submitted: " + parts.map(([k, t]) => k + " " + t).join(", ")) + '">' +
      parts.map(([k, t]) => '<i class="' + cls[t] + '" style="width:' + (100 * k / n) + '%" title="' + esc(t + ": " + k) + '"></i>').join("") + "</div>";
  };
  const out1 = g => [[g.untestable, "not testable"], [g.restates, "restate a finding"], [g.in_baseline, "in the no-lens baseline"]].filter(x => x[0]).map(x => x[0] + " " + x[1]).join(", ");
  const row = (g, pick) => {
    const other = Object.entries(g.other_kinds || {}).map(([k, n]) => n + " " + String(k).replace(/_/g, " ")).join(", ");
    return '<tr class="' + (role(g.lens) === "placebo" ? "placebo" : "") + '"><td class="lcol">' +
      (pick ? '<button type="button" class="lens-btn" data-lens="' + esc(g.lens) + '" title="Show this lens\'s hypotheses">' + chip(g.lens) + "</button>" : chip(g.lens)) +
      ' <span class="role">' + esc(role(g.lens)) + '</span></td><td class="fcol">' + bar(g) + '</td><td class="num">' + g.submitted + "</td>" +
      '<td class="num"' + (out1(g) ? ' title="' + esc("out: " + out1(g)) + '"' : "") + ">" + g.gate1 + "</td>" +
      G2.map(([, k]) => '<td class="num">' + (g[k] || 0) + "</td>").join("") + '<td class="num hi">' + g.credited + "</td>" +
      G3.map(([, k]) => '<td class="num"' + (k === "other" && other ? ' title="' + esc(other) + '"' : "") + ">" + (g.searched ? (g[k] || 0) : "–") + "</td>").join("") + "</tr>";
  };
  const span = 5 + G2.length + G3.length;
  const ja = I.judge_agreement || {};
  const agree = Object.entries(ja).filter(([, a]) => a && a.agree != null).map(([k, a]) => esc(k) + " " + pct(a.agree) + kappa(a) + " (n = " + a.n + ")").join("; ");
  const pc = I.positive_control;
  const credited = hypotheses.filter(h => h.credited);
  let html = '<section class="block"><h2>Lens tournament ' + esc(I.tournament) + "</h2>" + intro +
    '<ol class="how"><li><b>Gate 1</b>: is the hypothesis testable, not a mere restatement of a finding, and not already proposed by an agent with no lens?</li>' +
    "<li><b>Gate 2</b>: does it follow from the lens's core, only with a named auxiliary assumption, or just from common sense? The judges see the cores with the lens names masked.</li>" +
    "<li><b>Gate 3, the literature</b>: an agent writes search strings, the pipeline searches OpenAlex and Semantic Scholar and logs every call, and the judges label each hit. " +
    "The verdict does not remove credit; it says which credited hypotheses are already known.</li></ol>" +
    '<p class="hint">Lenses: ' + lensList.map(l => chip(l.id) + ' <span class="role">' + esc(l.role) + "</span>").join(" &nbsp; ") +
    (pc ? " · positive control (" + arr(pc.lenses).map(name).map(esc).join(" vs ") + " diverge on " + esc(pc.variable || "?") + "): " + (pc.passed ? "passed" : '<span class="warn">not passed</span>') : "") +
    (I.report ? " · full report: <code>" + esc(I.report) + "</code>" : "") + "</p></section>";

  html += '<section class="block"><h2>The gates</h2><p class="hint">Each row adds up: the gate-2 columns count what passed gate 1, the gate-3 columns what was credited. ' +
    "Pick a lens to see its hypotheses below; hover or focus a column name for what it counts.</p>" +
    '<div class="scroll"><table class="gates"><tr><th rowspan="2" class="lcol">Lens</th><th rowspan="2" class="fcol">Where its hypotheses went</th>' +
    '<th rowspan="2" class="num" title="Hypotheses proposed: each lens over its two blind runs; the control in one run" tabindex="0">Submitted</th>' +
    '<th class="gh">Gate 1</th><th class="gh" colspan="' + G2.length + '">Gate 2, of those past gate 1</th><th class="gh">Credited</th>' +
    '<th class="gh" colspan="' + G3.length + '">Gate 3: the literature, of the credited</th></tr><tr>' +
    '<th class="num" title="Testable, not a restatement of a finding, not already in the no-lens baseline" tabindex="0">Passed</th>' +
    G2.concat([["Gates 1+2", "credited", "Passed gate 1, and follows from the core (alone or with an auxiliary)"]]).concat(G3)
      .map(([h, , t]) => '<th class="num" title="' + esc(t) + '" tabindex="0">' + esc(h) + "</th>").join("") + "</tr>" +
    '<tr class="grp"><th colspan="' + span + '">Each lens alone, two blind runs</th></tr>' + theory.map(g => row(g, true)).join("") +
    (control.length ? '<tr class="grp"><th colspan="' + span + '">Control: one agent playing every lens at once (not searched in the literature)</th></tr>' + control.map(g => row(g, false)).join("") : "") +
    "</table></div>" +
    '<p class="hint"><span class="key"><i class="f-core"></i>from the core</span><span class="key"><i class="f-aux"></i>with an auxiliary</span>' +
    '<span class="key"><i class="f-disp"></i>disputed or contradicting the core</span><span class="key"><i class="f-cs"></i>common sense</span>' +
    (gatesAll.some(g => g.unjudged2) ? '<span class="key"><i class="f-pend"></i>gate 2 pending</span>' : "") +
    '<span class="key"><i class="f-out"></i>out at gate 1 or not judged</span></p>' +
    (I.baseline != null ? '<p class="hint">The no-lens baseline proposed ' + I.baseline + " hypotheses; they are the comparison set for gate 1, not judged themselves.</p>" : "") +
    (agree ? '<p class="hint">Judges: ' + judgeText(arr(I.judges)) +
      (arr(I.derive_judges).join() !== arr(I.judges).join() && arr(I.derive_judges).length ? "; gate 2: " + judgeText(arr(I.derive_judges)) : "") +
      ". Agreement between them: " + agree + ".</p>" : "") + "</section>";

  // 2. The hypotheses and the literature
  const N = I.novelty;
  const vcount = {};
  credited.forEach(h => { if (h.novelty) vcount[h.novelty] = (vcount[h.novelty] || 0) + 1; });
  html += '<section class="block" id="ihyp"><h2>Hypotheses and the literature</h2>';
  if (N) {
    const pa = N.judge_agreement_on_prior;
    html += '<p class="hint">' + credited.length + " hypotheses are credited after gates 1 and 2; " + N.searched + " of them were searched" +
      " (" + N.hits + " hits, " + N.readable + " with an abstract), each hit labelled blind by " + judgeText(arr(N.judges)) + ". Only titles and abstracts were read." +
      (pa && pa.agree != null ? " The judges agreed whether a hit states or tests the hypothesis for " + pct(pa.agree) + " of " + pa.n + " hits; both said it does for " + (pa.both_prior || 0) + ", at least one for " + (pa.either_prior || 0) + "." : "") + "</p>" +
      '<p class="hint">Recall controls: ' + N.controls + " findings of the review's own core studies were searched the same way (" + (N.control_hits || 0) + " further hits). " +
      "The search found the source study for " + N.controls_found_by_search + ", and every judge labelled that study as prior for " + N.controls_judged_prior + ". " +
      "A described study is easier to find than a prior test of a prediction, so this checks the search more than the verdicts.</p>" +
      (N.plain_rewritten ? '<p class="hint">For ' + N.plain_rewritten + " hypotheses the plain search had found nothing, so a fresh agent rewrote only their plain query as 3-5 key terms (<i>novelty_plain</i>) and it ran again on OpenAlex; their boolean strings are unchanged.</p>" : "") +
      (N.without_plain ? '<p class="hint"><span class="warn">Search gap:</span> the plain-text search found nothing new for ' + N.without_plain + " of the " + N.searched +
        " searched hypotheses, so their verdicts rest on the boolean OpenAlex searches alone. Rate limits refused some; a plain query matches only titles and abstracts " +
        "that contain all its words, which long queries rarely do; and works the boolean searches found already are not counted twice. <i>novelty_plain</i> rewrites the plain queries that found nothing (3-5 key terms); run <code>python -m ichnos lenses " + D.run + " --search</code> after it, then <i>novelty_judge</i> for each judge (each gets only the hits it has not labelled).</p>" : "") +
      (N.unlabelled ? '<p class="hint"><span class="warn">' + N.unlabelled + " hits with an abstract are not labelled by every judge yet</span>; their hypotheses stay unjudged until <i>novelty_judge</i> runs again.</p>" : "");
  } else {
    html += '<p class="hint">The literature check has not run yet: prepare <i>novelty_queries</i>, run <code>python -m ichnos lenses ' + D.run + " --search</code>, then <i>novelty_judge</i>.</p>";
  }
  if (vcount.not_searched) html += '<p class="hint">' + vcount.not_searched + " credited hypotheses are not searched yet: prepare <i>novelty_queries</i> (it takes only hypotheses without search strings), run <code>python -m ichnos lenses " + D.run + " --search</code>, then <i>novelty_judge</i> for each judge.</p>";
  if (credited.length) html += '<p class="hint">Verdicts (pick one to filter): ' + VERDICT_ORDER.filter(v => vcount[v])
    .map(v => '<button type="button" class="verdict v-' + v + (fs.verdict === v ? " on" : "") + '" aria-pressed="' + (fs.verdict === v) + '" data-verdict="' + v + '" title="' + esc(VERDICT[v]) + '">' + vname(v) + " " + vcount[v] + "</button>").join(" ") + "</p>";
  const isPending = h => h.gate1 === "unjudged" || (h.gate1 === "passed" && !h.derivation);
  const shows = [["credited", "Credited after gates 1 and 2"], ["out", "Out at gate 1 or 2"]]
    .concat(hypotheses.some(isPending) ? [["pending", "Not judged yet"]] : []).concat([["all", "All hypotheses"]]);
  html += '<div class="filters"><select id="ilens" aria-label="Lens"><option value="all">All lenses</option>' +
    order.map(id => '<option value="' + esc(id) + '">' + esc(name(id)) + "</option>").join("") + "</select>" +
    '<select id="ishow" aria-label="Which hypotheses">' + shows.map(([v, t]) => '<option value="' + v + '">' + t + "</option>").join("") + "</select>" +
    '<select id="iverdict" aria-label="Literature verdict"><option value="any">Any verdict</option>' +
    VERDICT_ORDER.map(v => '<option value="' + v + '">' + vname(v) + "</option>").join("") + "</select>" +
    '<input type="search" id="iq" placeholder="Search: words, H3, output 10"></div><p class="summary" id="isum"></p><div id="ilist"></div></section>';

  // 3. The test cards
  const grid = I.grid || {};
  for (const set of sets) {
    const ids = String(set.lenses || "").split("+").filter(Boolean);
    html += '<section class="block"><h2>Test cards: ' + ids.map(name).map(esc).join(" vs ") + '</h2><p class="hint">The cartographer is told to turn the disagreements between lenses into studies that would tell them apart, never to decide which lens is right. ' +
      "The pipeline refuses any card with a verdict field (winner, verdict, best, …) and any card whose possible results do not favour at least two different lenses. " +
      "References like “H1 (output 1)” point to the hypotheses above (search for “output 1”).</p></section>";
    arr(set.cards).forEach((c, i) => {
      const t = c.test || {}, m = t.measures || {};
      const fields = [["Design", t.design], ["Data", t.data], ["Population", t.population], ["Predictor", m.predictor], ["Moderator", m.moderator],
                      ["Outcome", m.outcome], ["Contrast", t.contrast], ["Timing", t.timing], ["Analysis", t.analysis]].filter(f => f[1]);
      const list = (title, xs, fn) => arr(xs).length ? "<h4>" + title + "</h4><ul>" + arr(xs).map(fn).join("") + "</ul>" : "";
      html += '<details class="tc"' + (i === 0 ? " open" : "") + '><summary><span class="chip">' + esc(c.id) + "</span> " + esc(c.question) + "</summary>" +
        (arr(c.cells).length ? '<p class="hint">Grid cells: ' + arr(c.cells).map(x => '<span class="chip">' + esc(x) + "</span>" + (grid[x] ? " " + esc(grid[x]) : "")).join("; ") + "</p>" : "") +
        '<div class="positions">' + arr(c.positions).map(p => '<div class="pos" style="--lc:' + color(p.lens) + '">' + chip(p.lens) +
          ' <b>predicts</b><p class="pred">' + esc(p.prediction) + '</p><span class="from">From ' + esc(p.from || "") + "</span></div>").join("") + "</div>" +
        (fields.length ? '<h4>The test</h4><dl class="test">' + fields.map(([k, v]) => "<dt>" + k + "</dt><dd>" + esc(v) + "</dd>").join("") + "</dl>" : "") +
        list("Possible results", c.patterns, p => "<li>" + esc(p.result) + " → favours " + arr(p.favours).map(chip).join(" ") + "</li>") +
        list("Existing evidence, not decisive", c.existing_evidence, e => "<li>Finding <b>" + fcite(e.finding) + "</b>: " + esc(e.why_not_decisive) +
          '<details class="ev"><summary>What the finding says</summary>' + ftext(e.finding) + "</details></li>") +
        list("Artifact threats", c.artifact_threats, x => "<li>" + esc(x) + "</li>") +
        list('Facts to verify <span class="warn">(not checked by the pipeline)</span>', c.facts_to_verify, x => "<li>" + esc(x) + "</li>") + "</details>";
    });
  }
  if (!sets.length) html += '<section class="block"><h2>No test cards yet</h2><p class="hint">Prepare a <i>cartograph</i> task: it turns the disagreements between two lenses (and the adversary) into at most five test cards.</p></section>';

  // 4. The lens cores, for rewriting
  html += '<section class="block"><details><summary><b>The lens cores</b> <span class="meta">(what each agent and judge was given; edit them in <code>' + esc(I.file) + "</code>)</span></summary>" +
    lensList.map(l => '<div class="lens-core">' + chip(l.id) + ' <span class="role">' + esc(l.role) + "</span>" +
      (l.name && String(l.name).toLowerCase() !== String(l.id).toLowerCase() ? ' <span class="meta">shown to the agents as “' + esc(l.name) + "”</span>" : "") +
      (l.heuristic ? '<p class="hint core-h"><i>' + esc(l.heuristic) + "</i></p>" : "") +
      "<ul>" + arr(l.core).map(x => "<li>" + esc(x) + "</li>").join("") + "</ul></div>").join("") + "</details></section>";
  el.innerHTML = html;

  const safe = u => /^https:\/\//.test(String(u || "")) ? u : null;
  const hits = (kind, xs, n) => {
    xs = arr(xs);
    return xs.length ? '<ul class="hits">' + xs.map(x => "<li>" + '<span class="verdict v-' + kind + '">' + kind + "</span> " +
      (safe(x.link) ? '<a href="' + esc(x.link) + '" target="_blank" rel="noopener">' + esc(x.title || "untitled") + "</a>" : esc(x.title || "untitled")) +
      (x.year ? " (" + esc(x.year) + ")" : "") +
      (kind === "contested" && arr(x.labels).length ? ' <span class="meta">judges: ' + arr(x.labels).map(l => esc(String(l || "no label").replace(/_/g, " "))).join(" / ") + "</span>" : "") +
      (x.quote ? ' <span class="q">“' + esc(x.quote) + "”</span>" : "") + "</li>").join("") +
      (n > xs.length ? '<li class="meta">and ' + (n - xs.length) + " more " + kind + " hits</li>" : "") + "</ul>" : "";
  };
  const fate = h => {
    if (h.gate1 === "unjudged") return '<span class="verdict v-out">gate 1 not judged yet</span>';
    if (h.gate1 !== "passed") return '<span class="verdict v-out">out at gate 1: ' + esc(h.gate1 === "restates" && h.restates != null ? "restates finding #" + h.restates : GATE1[h.gate1] || h.gate1) + "</span>";
    if (!h.derivation) return '<span class="verdict v-out">gate 2 not judged yet</span>';
    if (!h.credited) return '<span class="verdict v-out">out at gate 2: ' + esc(DERIVATION[h.derivation] || h.derivation) + "</span>";
    return '<span class="chip">' + esc(DERIVATION[h.derivation] || h.derivation) + "</span>";
  };
  const card = h => {
    const L = h.literature;
    let lit = "";
    if (h.credited && !L) lit = '<p class="sub">Not searched in the literature yet.</p>';
    else if (h.credited) {
      const counts = L.counts || {};
      const plain = L.plain_searched ? "" : " The plain-text search found nothing new here (refused, no title and abstract with all its words, or only works the boolean searches had found), so only the boolean OpenAlex searches count.";
      if (h.novelty === "novel") lit = '<p class="sub">No judge saw a prior or neighbouring test among ' + L.hits + " hits (" + L.readable + " with an abstract)." + plain + "</p>";
      else if (h.novelty === "not_searchable") lit = '<p class="sub">' + (L.hits ? "None of the " + L.hits + " hits had an abstract the judges could read." : "The searches returned no hits.") + plain + "</p>";
      else lit = hits("prior", L.prior, counts.prior) + hits("contested", L.contested, counts.contested) + hits("near", L.near, counts.near) +
        (L.unlabelled ? '<p class="sub"><span class="warn">' + L.unlabelled + " hits with an abstract are not labelled by every judge yet.</span></p>" : "") +
        (plain ? '<p class="sub">' + plain.trim() + "</p>" : "");
    }
    const why = arr(h.derivation_by_judge).length ? '<details class="ev"><summary>Why the judges said so (gate 2)</summary><ul>' +
      arr(h.derivation_by_judge).map(j => "<li><b>" + esc(short(j.judge || "")) + ": " + esc(DERIVATION[j.label] || j.label) + "</b>" +
        (j.auxiliary ? " — auxiliary: " + esc(j.auxiliary) : "") + (j.reason ? " — " + esc(j.reason) : "") + "</li>").join("") + "</ul></details>" : "";
    const from = arr(h.derived_from);
    return '<div class="hyp' + (h.credited ? "" : " out") + '"><header>' + chip(h.lens) + "<span>" + esc(h.id) + " · output " + esc(h.output_id) + " · replicate " + esc(h.replicate) + "</span>" +
      fate(h) + (h.credited ? verdict(h.novelty) : "") + "</header>" +
      '<p class="stmt">' + esc(h.statement) + "</p>" +
      (h.condition ? '<p class="sub"><b>When:</b> ' + esc(h.condition) + (h.sign ? " · <b>sign</b> " + esc(h.sign) : "") + "</p>" : "") +
      (h.forbids ? '<p class="sub"><b>Forbids:</b> ' + esc(h.forbids) + "</p>" : "") +
      (from.length ? '<details class="ev"><summary>Drawn from findings ' + from.map(fcite).join(", ") + "</summary><ul>" +
        from.map(id => "<li><b>" + fcite(id) + "</b>: " + ftext(id) + "</li>").join("") + "</ul></details>" : "") +
      lit + why + "</div>";
  };
  const draw = () => {
    const q = fs.q.toLowerCase().trim();
    const rows = hypotheses.filter(h => (fs.lens === "all" || h.lens === fs.lens) &&
      (fs.show === "all" || (fs.show === "credited" ? h.credited : fs.show === "pending" ? isPending(h) : !h.credited && !isPending(h))) &&
      (fs.verdict === "any" || (h.credited && h.novelty === fs.verdict)) &&
      (!q || [h.id, "output " + h.output_id, h.statement, h.condition, h.forbids].join(" ").toLowerCase().includes(q)));
    document.getElementById("ilist").innerHTML = rows.length ? rows.map(card).join("") : '<p class="empty">No hypothesis matches these filters.</p>';
    document.getElementById("isum").textContent = rows.length + " of " + hypotheses.length + " hypotheses shown · " + credited.length + " credited after gates 1 and 2";
    el.querySelectorAll("[data-verdict]").forEach(b => { const on = b.dataset.verdict === fs.verdict; b.classList.toggle("on", on); b.setAttribute("aria-pressed", String(on)); });
    save(KEY + ":interp", {lens: fs.lens, show: fs.show, verdict: fs.verdict});
  };
  const sel = {ilens: "lens", ishow: "show", iverdict: "verdict"};
  const setSel = (id, v) => { document.getElementById(id).value = v; };
  for (const [id, k] of Object.entries(sel)) {
    const s = document.getElementById(id);
    if (![...s.options].some(o => o.value === fs[k])) fs[k] = s.options[0].value;
    s.value = fs[k];
    s.addEventListener("change", () => {
      fs[k] = s.value;
      if (k === "verdict" && s.value !== "any") { fs.show = "credited"; setSel("ishow", "credited"); }
      if (k === "show" && s.value !== "credited" && s.value !== "all") { fs.verdict = "any"; setSel("iverdict", "any"); }
      draw();
    });
  }
  if (fs.verdict !== "any" && fs.show !== "credited" && fs.show !== "all") { fs.verdict = "any"; setSel("iverdict", "any"); }
  document.getElementById("iq").addEventListener("input", e => { fs.q = e.target.value; draw(); });
  el.querySelectorAll("[data-verdict]").forEach(b => b.addEventListener("click", () => {
    fs.verdict = fs.verdict === b.dataset.verdict ? "any" : b.dataset.verdict;
    if (fs.verdict !== "any") { fs.show = "credited"; setSel("ishow", "credited"); }
    setSel("iverdict", fs.verdict); draw();
  }));
  el.querySelectorAll("button.lens-btn").forEach(b => b.addEventListener("click", () => {
    fs.lens = b.dataset.lens; setSel("ilens", fs.lens); draw();
    document.getElementById("ihyp").scrollIntoView({behavior: "smooth", block: "start"});
  }));
  draw();
}

function renderAgents() {
  if (!LIVE) return;
  const el = document.getElementById("agents");
  const tasks = LIVE.tasks || [];
  const kinds = {screen: "Screen the sheet on the protocol's metrics", extract: "Map how the kept studies were done",
                 findings: "Extract the findings of the kept studies", synthesize: "Propose cross-study insights from all findings",
                 write: "Draft a section from the findings (every sentence checked)",
                 paper: "Write a full manuscript (method section generated from the store)",
                 audit: "Audit the latest manuscript: does each sentence match what it cites?",
                 peer: "Independent peer review of the latest manuscript (persona: critical)",
                 meta: "Editor's decision on the latest manuscript",
                 revise: "Revise the latest manuscript and answer the editor"};
  el.innerHTML = '<section class="block"><h2>Agent tasks</h2><p class="hint">The pipeline writes each task from your protocol, an agent answers it, and the pipeline checks every answer and quote before storing it. ' +
    "For now the agent is Claude in Claude Code: after preparing a task, ask it to run the task in the folder shown, then come back and import the answers here.</p>" +
    (tasks.length ? "<table><tr><th>Task</th><th>Agent</th><th>Records</th><th>State</th><th></th></tr>" + tasks.map(t =>
      "<tr><td>" + esc(t.kind) + '<br><span class="meta">' + esc(t.folder) + "</span></td><td>" + esc(t.agent) + "</td><td>" + t.records + "</td>" +
      '<td class="state state-' + esc(t.state.split(" ")[0]) + '">' + esc(t.state) + "</td><td>" +
      (t.state === "answered" ? '<button class="btn primary" data-import="' + esc(t.folder) + '">Import answers</button>' : "") + "</td></tr>").join("") + "</table>"
      : '<p class="hint">No agent tasks for this run yet.</p>') + "</section>" +
    '<section class="block"><h2>Prepare a task</h2><form class="add" id="prep"><select name="kind">' +
    Object.entries(kinds).map(([k, v]) => '<option value="' + k + '">' + esc(v) + "</option>").join("") + "</select>" +
    '<input name="agent" value="model:claude-opus-5-5" placeholder="Agent name, e.g. model:claude-opus-5-5">' +
    '<button class="btn primary" type="submit">Prepare task</button></form></section>';
  el.querySelectorAll("[data-import]").forEach(b => b.addEventListener("click", () => startJob({action: "import", folder: b.dataset.import})));
  document.getElementById("prep").addEventListener("submit", ev => {
    ev.preventDefault();
    const f = ev.target;
    startJob({action: "prepare", kind: f.kind.value, agent: f.agent.value.trim()});
  });
}

// Long steps run on the server as jobs; the page watches them and reloads when one finishes.
async function startJob(body) {
  try { await api("/api/job", Object.assign({run: D.run}, body)); toast("Started: " + body.action); pollJobs(); }
  catch (e) { toast("Could not start: " + e.message, true); }
}
async function pollJobs() {
  try {
    const res = await fetch("/api/jobs"); const jobs = await res.json();
    const running = jobs.filter(j => j.status === "running");
    const last = jobs[jobs.length - 1];
    document.getElementById("jobs").textContent = running.length ? "Working: " + running.map(j => j.label).join(", ") + " … " + (running[0].log.slice(-1)[0] || "")
      : last ? "Last: " + last.label + " " + last.status + (last.error ? " (" + last.error + ")" : "") : "";
    if (running.length) { setTimeout(pollJobs, 1500); return; }
    if (last && last.status === "done" && !pollJobs.reloaded && last.finished > LIVE.loaded) {
      pollJobs.reloaded = true; toast(last.label + " done; refreshing"); setTimeout(() => location.reload(), 800);
    } else if (last && last.status === "failed" && last.finished > LIVE.loaded) toast(last.label + " failed: " + last.error, true);
  } catch (e) { document.getElementById("jobs").textContent = "The app is not answering; is its window still open?"; }
}

function setupLive() {
  if (!LIVE) { document.getElementById("staticbar").hidden = false; return; }
  // the window reports itself, so the desktop app knows when its last window has closed
  const PAGE_ID = (crypto.randomUUID ? crypto.randomUUID() : String(Math.random()).slice(2));
  const ping = () => api("/api/ping", {page: PAGE_ID}).catch(() => {});
  ping(); setInterval(ping, 15000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) ping(); });
  addEventListener("pagehide", () => navigator.sendBeacon("/api/bye?token=" + encodeURIComponent(LIVE.token) + "&page=" + PAGE_ID));
  document.getElementById("livebar").hidden = false;
  document.getElementById("agents-tab").hidden = false;
  document.getElementById("agents-group").hidden = false;
  document.getElementById("export").hidden = true;
  showTab();
  const sel = document.getElementById("runsel");
  document.getElementById("allproj").hidden = false;
  const P = LIVE.projects || [];
  const mineP = P.find(x => x.runs.some(r => r.id === D.run));
  if (P.length) {
    sel.hidden = false;
    sel.innerHTML = '<optgroup label="Switch project">' + P.map(x => '<option value="' + x.run + '"' + (x === mineP && x.run === D.run ? " selected" : "") + ">" + esc(x.name) + "</option>").join("") + "</optgroup>" +
      (mineP && mineP.runs.length > 1 ? '<optgroup label="Runs of this project">' + mineP.runs.map(r => '<option value="' + r.id + '"' + (r.id === D.run && mineP.run !== D.run ? " selected" : "") + ">Run " + r.id + (r.main ? " (main)" : "") + " · " + esc(r.date) + " · " + Number(r.records).toLocaleString("en") + " records</option>").join("") + "</optgroup>" : "");
    sel.addEventListener("change", () => { location.href = "/run/" + sel.value + (Number(sel.value) === D.run ? location.hash : "#home"); });
  }
  const name = document.getElementById("reviewer");
  name.value = load("psyscientist:reviewer", LIVE.reviewer || "");
  name.addEventListener("change", () => { save("psyscientist:reviewer", name.value.trim()); location.reload(); });
  if (!name.value) toast("Enter your name at the top: your decisions and insight changes are saved under it.");
  else if (LIVE.reviewer !== reviewer()) { location.replace("/run/" + D.run + "?reviewer=" + encodeURIComponent(reviewer())); return; }
  document.querySelectorAll("[data-job]").forEach(b => b.addEventListener("click", () => startJob({action: b.dataset.job})));
  renderAgents();
  pollJobs();
}

function renderHow() {
  document.getElementById("how").innerHTML = '<section class="block how"><h2>How this page works</h2><ul>' +
    (LIVE ? "<li><b>Your call</b> is saved in the evidence store as soon as you make it, under your name at the top. Exclusions are saved once you pick a reason; notes when you leave the field.</li>"
          : "<li><b>Your call</b> is stored in this browser only. <b>Export my decisions</b> saves a CSV; import it with <code>python -m ichnos import-screening &lt;file&gt;</code>. Easier: start the app (double-click Ichnos.bat), which saves directly.</li>") +
    "<li><b>Needs you</b> lists records you have not decided where the agents keep the record, disagree, or exclude it although the trajectory signal is strong.</li>" +
    "<li><b>Trajectory signal</b> comes from word lists, not from an agent: " + Object.entries(D.levels).map(([k, v]) => "<i>" + k + "</i> " + esc(v)).join("; ") + ". It is a hint, never a decision.</li>" +
    "<li><b>Agent scores</b>: 2 met, 1 cannot tell, 0 not met, each backed by a quote; ⚠ marks a quote that is not in the record. The decision follows a fixed rule: the first metric scored 0 excludes, all 2 includes, anything else is unsure.</li>" +
    "<li><b>Blind mode</b> hides every agent judgement, for screening that must stay independent (for example a validation set).</li>" +
    "<li>Keys: <kbd>j</kbd>/<kbd>k</kbd> next/previous, <kbd>i</kbd> include, <kbd>u</kbd> unsure, <kbd>e</kbd> exclude.</li></ul>" +
    "<p class=\"hint\">Inclusion rule: " + esc(D.rule) + "</p>" +
    (D.metrics.length ? "<p class=\"hint\">Metrics: " + D.metrics.map(m => "<b>" + esc(m.id) + "</b> " + esc(m.q)).join(" · ") + "</p>" : "") + "</section>";
}

const PJ = D.project || {};
document.getElementById("head").textContent = PJ.name || D.title || ("Corpus " + D.corpus);
if (PJ.owner) { const o = document.getElementById("owner"); o.textContent = PJ.owner; o.hidden = false; }
document.getElementById("meta").textContent = "Corpus " + D.corpus + " · run " + D.run + " · protocol " + D.version + " · data built " + D.generated;
// pages with nothing in them for this project are left out of the menu
const EMPTY = {interpret: !D.interpret, draft: !D.draft};
document.querySelectorAll("#tabs button[data-tab]").forEach(b => { if (EMPTY[b.dataset.tab]) b.hidden = true; });
document.querySelectorAll("#tabs .group").forEach(g => {
  let n = g.nextElementSibling, any = false;
  while (n && !n.classList.contains("group")) { if (n.tagName === "BUTTON" && !n.hidden) any = true; n = n.nextElementSibling; }
  if (!any) g.hidden = true;
});
if (location.hash.slice(1) in PAGES && !EMPTY[location.hash.slice(1)]) st.tab = location.hash.slice(1);
else if (EMPTY[st.tab]) st.tab = "home";
else history.replaceState(null, "", "#" + st.tab);
document.getElementById("legend").innerHTML = "Highlights: " + ["trajectory", "heterogeneity", "longitudinal", "cross_sectional"]
  .filter(n => D.lexicons.includes(n)).map(n => '<span class="' + n + '" style="background: var(--' + ({trajectory: "traj", heterogeneity: "het", longitudinal: "long", cross_sectional: "xs"}[n]) + ')">' + n.replace("_", "-") + "</span>").join("") +
  queryLex.map((n, k) => '<span style="background: var(--h' + (k % 6) + ')">search: ' + esc(n.slice(6)) + "</span>").join("");
// one page failing to draw must not stop the others, or the live wiring below
for (const draw of [showTab, renderHome, badges, renderList, renderMap, renderFindings, renderInsights, renderDraft, renderPaper,
                    renderCheck, renderInterpret, renderHow, renderFulltext]) {
  try { draw(); } catch (e) { console.error(e); toast("Part of the page could not be drawn (" + draw.name + "): " + e.message, true); }
}
setupLive();
</script>
</body>
</html>
"""
