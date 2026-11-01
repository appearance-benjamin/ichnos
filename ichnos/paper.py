"""Full manuscripts with independent review: write, audit, review, revise, decide.

The blueprint's stages 6 to 8 for a whole paper. Like AI Scientist-v2's
pipeline the result is a complete manuscript scored by a reviewer ensemble; unlike
it, the paper is written evidence first and checked sentence by sentence.

Task kinds (each an agent step in the usual task folder):

- paper:   the manuscript (title, abstract, introduction, results, discussion,
           conclusion). The Method section is not written by an agent: it is
           generated from the evidence store, so its numbers are true by
           construction. Every other sentence cites finding rows, method facts
           (M1, M2, ...) or background sources from the protocol (B1, ...), and
           `verify` checks citations and numbers in code.
- audit:   a different agent sees each sentence with exactly the passages it
           cites and judges whether it represents them faithfully: the check code
           cannot do.
- peer:    an independent reviewer with a persona (methodologist, field expert,
           theorist, critical) sees only the submitted manuscript and scores it
           on a psychology-journal rubric.
- meta:    the handling editor combines the reviews and the audit into a decision
           and a list of required changes; in round 2 it judges the revision.
- revise:  the writer revises the paper and answers every required change.
"""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from collections import Counter
from pathlib import Path

from . import agreement, biblio, checks, document, insights, numgate, tables, writer
from .store import Store, now

KINDS = ("paper", "audit", "peer", "meta", "revise")
SECTIONS = ("Introduction", "Results", "Discussion", "Conclusion")
PROPOSAL_SECTIONS = ("Introduction", "Method", "Evidence", "Proposal", "Evaluation", "Discussion", "Conclusion")
# a focus aim with layout "policy": the same evidence written as a short policy review, its lessons first
POLICY_SECTIONS = ("Key points", "Introduction", "Lessons", "Recommendations", "Limits", "Method", "Conclusion")
# layout "essay": the same evidence as one argument (a diagnosis, the argument, what follows, what would refute it, a
# landing); each section may carry a `title` that states its claim, and the Method is printed as an appendix
ESSAY_SECTIONS = ("Diagnosis", "Argument", "What follows", "What would show this wrong", "Conclusion", "Method")
# the layouts whose writer's table is a table of recommendations, printed as Table 2
ACTION_LAYOUTS = ("policy", "essay")
MODEL_CELLS = ("component", "change", "why", "performance", "wellbeing", "strength", "test")
MODEL_OPTIONAL = {"principle": "Relation to the consensus principles", "adopt": "Adopt now or in the trial",
                  "resources": "Resources"}
VERDICTS = ("faithful", "overstated", "understated", "unsupported", "misattributed")
DECISIONS = ("accept", "minor_revision", "major_revision", "reject")
RUBRIC = {
    "originality": "Originality: does the review ask and answer something new? (1-4)",
    "rigor": "Methodological rigour: search, selection, charting and synthesis adequate and reproducible? (1-4)",
    "evidence": "Evidence-claim alignment: are claims proportionate to the evidence, its designs and limits? (1-4)",
    "contribution": "Theoretical contribution: does it advance understanding beyond summary? (1-4)",
    "clarity": "Clarity and structure (1-4)",
    "transparency": "Reporting, ethics and disclosure, including the use of AI (1-4)",
}
PERSONAS = {
    "methodologist": "an expert in review methodology (scoping and systematic reviews, PRISMA-ScR, search strategies, "
                     "screening reliability, evidence synthesis). Judge whether the methods are adequate and "
                     "transparently reported and whether the conclusions follow from the evidence.",
    "domain": "a senior researcher in the paper's field ({field}). Judge whether the paper gets this literature "
              "right, misses key studies, constructs or theories, and whether its claims would convince experts.",
    "theory": "a theorist who judges conceptual contribution: is there a clear argument, are theories and "
              "philosophical sources used well, does the paper add more than a summary?",
    "critical": "a demanding reviewer known for high standards. Look hard for overclaiming, weak evidence presented "
                "as strong, missing limitations and errors. If in doubt, recommend rejection.",
    "practitioner": "a programme director who chairs an assessment committee and must decide how to run and adjust "
                    "this kind of programme. Judge whether the paper gives a clear, usable answer: what does it do, "
                    "for whom, what should I keep or change, and how sure can I be? Penalise overclaiming, and "
                    "penalise just as much a paper so hedged that it leaves you nothing to act on.",
    "policy": "an adviser on family policy in a ministry or an international organisation, who must decide which "
              "measures to recommend. Judge whether the paper gives clear, usable recommendations, whether each rests "
              "on evidence strong enough for what it asks, and whether a reader outside the field can follow it. "
              "Penalise overclaiming, and penalise just as much a paper so hedged that it leaves nothing to act on.",
    "editor": "the editor of a journal that publishes argued essays at the meeting of science and policy, for "
              "readers outside the field. Judge whether the paper makes one clear argument a reader can carry away, "
              "whether its concepts are well chosen and used consistently, whether it meets the strongest objection at "
              "full strength, whether its examples are vivid and representative of the evidence, and whether every "
              "claim is earned by the evidence it cites. Penalise a paper that reads like an inventory of studies as "
              "much as one that claims more than its evidence.",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
    paper_id     INTEGER PRIMARY KEY,
    corpus       TEXT NOT NULL,
    run_id       INTEGER REFERENCES runs(run_id),
    version      INTEGER NOT NULL,
    parent_id    INTEGER REFERENCES papers(paper_id),
    author       TEXT NOT NULL,                  -- the writing agent
    title        TEXT,
    content      TEXT NOT NULL,                  -- JSON: title, abstract, keywords, sections
    method       TEXT NOT NULL,                  -- JSON: the method facts and section used
    verification TEXT,                           -- JSON: per-sentence checks
    response     TEXT,                           -- JSON: response to reviewers (revisions)
    call_id      INTEGER REFERENCES calls(call_id),
    created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS reviews (
    review_id   INTEGER PRIMARY KEY,
    paper_id    INTEGER NOT NULL REFERENCES papers(paper_id),
    kind        TEXT NOT NULL CHECK (kind IN ('audit', 'peer', 'meta')),
    reviewer    TEXT NOT NULL,                   -- agent and persona
    round       INTEGER NOT NULL DEFAULT 1,
    content     TEXT NOT NULL,                   -- JSON
    overall     REAL,
    decision    TEXT,
    call_id     INTEGER REFERENCES calls(call_id),
    created_at  TEXT NOT NULL
);
"""


def ensure(store: Store) -> None:
    store.db.executescript(SCHEMA)


SOURCE_NAMES = {"openalex": "OpenAlex", "semantic_scholar": "Semantic Scholar", "pubmed": "PubMed",
                "citation_search": "citation searching", "eric": "ERIC", "crossref": "Crossref",
                "openalex_translated": "OpenAlex (the term in other languages)",
                "europepmc": "Europe PMC", "psycinfo": "APA PsycINFO", "scopus": "Scopus", "wos": "Web of Science"}


FULLTEXT_NAMES = {"europepmc": "Europe PMC", "unpaywall": "Unpaywall", "openalex": "OpenAlex", "core": "CORE",
                  "crossref": "Crossref", "wayback": "Internet Archive", "library": "the author's library"}
FULLTEXT_ROUTES = {"core": "CORE", "crossref": "openly licensed publisher links registered in Crossref",
                   "wayback": "archived copies of failed links in the Internet Archive"}


def source_name(source: str) -> str:
    return SOURCE_NAMES.get(source, source.replace("_", " "))


# the fields each source's translation of the string searches (see query.DIALECTS and the source modules)
SOURCE_FIELDS = {"openalex": "title and abstract, exact words", "openalex_translated": "title and abstract, exact words",
                 "semantic_scholar": "title and abstract",
                 "pubmed": "title and abstract [tiab]", "europepmc": "title and abstract (TITLE_ABS)",
                 "eric": "title and description (abstract)"}


def dim_label(protocol: dict, name: str) -> str:
    """How the paper names a charted dimension: its `label` in the protocol, else its name."""
    d = next((d for d in (protocol.get("map") or {}).get("dimensions") or [] if d["name"] == name), {})
    return d.get("label") or name.replace("_", " ")


# Method facts: what the pipeline did, from the store, so the Method section is true by construction.

def method_facts(store: Store, run_id: int, protocol: dict) -> list[dict]:
    from .agents import kept_ids, screened_ids, sheet_ids
    db = store.db
    run = db.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    corpus = run["corpus"]
    L = tables.table_labels(protocol)
    facts = []

    def add(group, text):
        facts.append({"id": f"M{len(facts) + 1}", "group": group, "text": " ".join(text.split())})

    by = " ".join(str((protocol.get("paper") or {}).get("protocol_by") or "").split())
    add("Protocol and question",
        f"The review followed a written protocol (version {protocol.get('version', '?')})"
        + (f", {by}" if by else "") + "; the protocol, every amendment and the date and reason for each are kept with "
        "the evidence. Review question: " + " ".join((protocol.get("findings") or {}).get("question", "").split()))
    every = db.execute("SELECT * FROM run_sources WHERE run_id = ? ORDER BY source", (run_id,)).fetchall()
    srcs = [s for s in every if s["source"] != "citation_search" and "@" not in s["source"]]    # updates come apart
    updates = [s for s in every if "@" in s["source"]]
    mined = next((s for s in every if s["source"] == "citation_search"), None)
    def when(src):
        m = re.match(r"searched (\d{4}-\d{2}-\d{2})", src["notes"] or "")
        return f", searched {m.group(1)}" if m and m.group(1) != run["started_at"][:10] else ""
    parts = [f"{source_name(s['source'])} ({s['reported_total']} records reported, {s['fetched']} retrieved"
             + (f"; {SOURCE_FIELDS[s['source']]}" if s["source"] in SOURCE_FIELDS else "") + when(s) + ")"
             if s["status"] != "failed" else f"{source_name(s['source'])} (failed, no records retrieved)"
             for s in srcs]
    search = "Information sources and search"
    add(search, f"Titles and abstracts were searched on {run['started_at'][:10]} (dates are UTC) in {', '.join(parts)}, "
                "with one boolean string translated for each source; no limits on language, publication type or date "
                "were set in any source.")
    add(search, "Search string: `" + " ".join(protocol.get("query", "").split()) + "`.")
    pool = db.execute("SELECT count(DISTINCT work_id) FROM retrievals WHERE run_id = ? AND source != "
                      "'citation_search' AND source NOT LIKE '%@%'", (run_id,)).fetchone()[0]
    sheet = set(sheet_ids(store, run_id))
    late = set()
    if mined:
        m = json.loads(mined["notes"] or "{}")
        late = {r[0] for r in db.execute("SELECT DISTINCT work_id FROM retrievals WHERE run_id = ? AND source = "
                                         "'citation_search'", (run_id,))} - {r[0] for r in db.execute(
            "SELECT DISTINCT work_id FROM retrievals WHERE run_id = ? AND source != 'citation_search' AND source NOT "
            "LIKE '%@%'", (run_id,))}
    from .update import added
    upd_day = added(store, run_id)
    upd = set(upd_day)
    sheet_n = len(sheet - late - upd)
    add(search, f"After merging duplicates by DOI, source identifiers, PubMed ID and title with year, {pool} unique "
                "records remained" + (f"; a seeded random sample of {sheet_n} of them was drawn for screening, and "
                                      f"the other {pool - sheet_n} were not screened." if sheet_n < pool else "."))
    if mined:
        _citation_facts(store, run_id, protocol, m, late, add)
    from .update import supplement_of, supplements
    plain = [s for s in updates if not supplement_of(s["source"])]
    if plain:
        days = sorted({s["source"].split("@", 1)[1] for s in plain})
        n = sum(1 for d in upd_day.values() if "+" not in d)
        add(search, f"The search was updated on {', '.join(days)}, with the same string in the same sources "
                    f"({sum(s['fetched'] for s in plain)} records retrieved); the {n} records new to the review "
                    "were screened and judged like the others.")
    for sid, supp in supplements(protocol).items():
        mine = [s for s in updates if supplement_of(s["source"]) == sid]
        if not mine:
            continue
        day = mine[0]["source"].split("@", 1)[1].split("+", 1)[0]
        n = sum(1 for d in upd_day.values() if d.endswith("+" + sid))
        add(search, f"On {day} a supplementary search was run in {', '.join(source_name(s['source'].split('@')[0]) for s in mine)}"
                    + (f" ({' '.join(str(supp['why']).split())})" if supp.get("why") else "")
                    + f", with its own string ({sum(s['fetched'] for s in mine)} records retrieved); the {n} records new "
                    "to the review were screened and judged like the others. Supplementary string: `"
                    + " ".join(str(supp.get("query", "")).split()) + "`.")
    for r in db.execute("SELECT run_id, started_at FROM runs WHERE corpus = ? AND run_id > ? ORDER BY run_id",
                        (corpus, run_id)).fetchall():
        names = [source_name(s["source"]) for s in db.execute(
            "SELECT source FROM run_sources WHERE run_id = ? ORDER BY source", (r["run_id"],))]
        add(search, f"A later search on {r['started_at'][:10]} in {', '.join(names)} found "
                    f"{len(sheet_ids(store, r['run_id']))} further records not retrieved before; they were screened "
                    "separately and contributed no findings to this review.")
    rules = protocol.get("screening") or {}
    elig = "Eligibility criteria"
    if rules.get("summary") and L.get("criteria"):
        add(elig, " ".join(rules["summary"].split()) + f" The inclusion rule and every criterion are given verbatim in "
                  f"{L['criteria']}.")
        if rules.get("metrics"):
            add(elig, f"Agent screeners scored each record on {len(rules['metrics'])} criteria ("
                      + ", ".join(m.get("label") or m["id"] for m in rules["metrics"]) + "; 2 met, 1 cannot tell, 0 not "
                      "met). The first criterion scored 0 excluded the record; all criteria met included it; anything "
                      "else was unsure and kept for full assessment.")
    else:
        add(elig, "Inclusion rule: " + " ".join(rules.get("rule", "").split()))
    if rules.get("metrics") and not (rules.get("summary") and L.get("criteria")):
        add(elig, "Agent screeners scored each record on these criteria (2 met, 1 cannot tell, 0 not met): "
                  + "; ".join(f"{m['id']}: {' '.join(m['question'].split())}" for m in rules["metrics"])
                  + " The first criterion scored 0 excluded the record; all criteria met included it; anything else "
                    "was unsure and kept for full assessment.")
    select = "Selection of sources of evidence"
    screen = db.execute("SELECT * FROM screening WHERE run_id = ? AND stage != 'full_text'", (run_id,)).fetchall()
    elig_rows = db.execute("SELECT * FROM screening WHERE run_id = ? AND stage = 'full_text'", (run_id,)).fetchall()
    models = sorted({s["screener"] for s in screen if s["screener"].startswith("model:")})
    humans = sorted({s["screener"] for s in screen if s["screener"].startswith("human:")})
    sample = set(screened_ids(store, run_id))
    unscreened = set(sheet_ids(store, run_id)) - sample
    _, pairs = agreement.complete_pairs([s for s in screen if s["screener"] in models], sample)
    if models:
        txt = (f"Titles and abstracts of {'all ' if not unscreened else ''}{len(sample)} records were screened "
               f"independently by {len(models)} language-model agents against the protocol's inclusion rule")
        k = pairs[0].kappa() if pairs else None
        if k is not None:
            txt += f"; they agreed on {round(100 * pairs[0].observed)}% of records (Cohen's kappa {k:.2f})"
        scorers = [m for m in models if db.execute(
            "SELECT 1 FROM screening_scores WHERE run_id = ? AND screener = ? AND metric NOT LIKE 'eligibility:%' "
            "LIMIT 1", (run_id, m)).fetchone()]
        n_metrics = len(rules.get("metrics") or [])
        if scorers:
            who = ("Each agent" if len(scorers) == len(models) and len(models) > 1 else
                   "One agent" if len(scorers) == 1 else f"{len(scorers)} agents")
            txt += (f". {who} scored the {n_metrics} criteria with quotes, and "
                    + ("its" if len(scorers) == 1 else "each") + " decision followed from the scores by a fixed rule")
        if unscreened:      # in the same fact, so that the facts after it keep their numbers
            txt += (f". {len(unscreened)} further records on the screening sheet were left unscreened by a decision "
                    "recorded in the protocol (see the deviations); they are not counted as screened, and none was "
                    "retained")
        add(select, txt + ".")
        # records first screened after the first manuscript of this run (the sheet was completed after review)
        ensure(store)
        first = db.execute("SELECT min(created_at) FROM papers WHERE run_id = ?", (run_id,)).fetchone()[0]
        if first:
            late = {s["work_id"] for s in screen} - {s["work_id"] for s in screen if s["created_at"] < first}
            if late:
                final = set(kept_ids(store, run_id))
                add(select, f"{len(late)} of the {len(sample)} records were first screened after the first manuscript "
                            f"was written; {len(late & final)} of them are among the {len(final)} records retained for "
                            "charting.")
    from .checkpack import calibration
    cal = calibration(store, run_id)
    if cal and cal["n"]:
        names = ", ".join(cal["people"])
        add(select, f"A person ({names}) then screened a random sample of {cal['n']} records on title and abstract, "
                    "drawn by the pipeline and stratified by the agents' decisions ("
                    + ", ".join(f"{k} by the agents {v}" for k, v in sorted(cal["strata"].items()))
                    + ") and shown without them; the person's decision overrode the agents' for these records, and "
                    f"{cal['kept']} were kept. Agreement with each agent on keep or exclude: "
                    + "; ".join(f"{a} {v['agree']} of {v['n']} ({round(100 * v['agree'] / v['n'])}%"
                                + (f", Cohen's kappa {v['kappa']:.2f}" if v["kappa"] is not None else "") + ")"
                                for a, v in cal["agreement"].items())
                    + ". Because the sample was stratified, these figures describe the strata, not the screening "
                      "as a whole.")
        humans = [h for h in humans if not cal or h.split(":", 1)[1] not in cal["people"]]
    if humans:
        hrows = [s for s in screen if s["screener"] in humans]
        n_h = len({s["work_id"] for s in hrows})
        n_inc = sum(1 for s in hrows if s["decision"] != "exclude")
        add(select, f"The human author then screened {n_h} records, not a random sample: those the agents kept, "
                    "disagreed on, or excluded despite wording that suggested trajectory analyses, with the agents' "
                    f"decisions visible. The author retained {n_inc} of them, and the author's decision overrode the "
                    "agents'. Because this sample was neither random nor blind, it does not estimate the agents' "
                    "accuracy.")
        human = {s["work_id"]: s["decision"] != "exclude" for s in hrows}
        unseen = sample - set(human)
        add(select, f"For the {len(unseen)} records the author did not screen, the agents' decisions governed: a "
                    "record was retained when either agent included it or was unsure, so disagreements between the "
                    f"agents were resolved towards inclusion; {len(unseen & set(kept_ids(store, run_id)))} records "
                    "were retained this way.")
        for m in models:
            agent_keep = {s["work_id"]: s["decision"] != "exclude" for s in screen if s["screener"] == m}
            pairs_h = [(human[w], agent_keep[w]) for w in human if w in agent_keep]
            if pairs_h:
                agree = sum(a == b for a, b in pairs_h)
                k = agreement.cohen_kappa([("keep" if a else "exclude", "keep" if b else "exclude") for a, b in pairs_h])
                both_keep = sum(a and b for a, b in pairs_h)
                add(select, f"On these {len(pairs_h)} records the author and the agent {m.split(':')[1]} agreed on keep "
                            f"or exclude for {agree} ({round(100 * agree / len(pairs_h))}%"
                            + (f", Cohen's kappa {k:.2f}" if k is not None else "")
                            + f"); both kept {both_keep}, the author alone kept "
                              f"{sum(a and not b for a, b in pairs_h)} and the agent alone kept "
                              f"{sum(b and not a for a, b in pairs_h)}.")
    kept = kept_ids(store, run_id)
    passed = set(kept_ids(store, run_id, "title_abstract"))
    excluded = sample - passed
    by_reason = Counter()
    metric_screener = next((m for m in models if db.execute(
        "SELECT 1 FROM screening_scores WHERE run_id = ? AND screener = ? LIMIT 1", (run_id, m)).fetchone()), None)
    for s in screen:
        if s["work_id"] in excluded and s["screener"] == (metric_screener or (models[0] if models else None)):
            by_reason[s["reason"] or "other"] += 1
    reasons = ", ".join(f"{k.replace('_', ' ')} {n}" for k, n in by_reason.most_common())
    if elig_rows:
        add(select, f"At title and abstract {len(passed)} of {len(sample)} records were retained and {len(excluded)} "
                    "were excluded" + (f" (reasons recorded by the criteria-scoring agent: {reasons})" if reasons else "")
                    + ".")
        _eligibility_stage(store, run_id, protocol, elig_rows, passed, kept, add)
    else:
        add(select, f"In total {len(kept)} of {len(sample)} records were retained for charting and {len(excluded)} "
                    "were excluded" + (f" (reasons recorded by the criteria-scoring agent: {reasons})" if reasons else "")
                    + ".")
    _deviations(store, run_id, protocol, screen, metric_screener, kept, add)
    chart = "Data charting"
    _abstract_facts(store, run_id, protocol, kept, add)
    got = Counter(r["source"] for r in db.execute(
        f"SELECT source FROM fulltexts WHERE status = 'parsed' AND work_id IN ({','.join('?' * len(kept))})",
        tuple(kept))) if kept else Counter()
    later = [FULLTEXT_ROUTES[k] for k in FULLTEXT_ROUTES if got.get(k)]
    add(chart, f"Open-access full texts were sought for all retained records through Europe PMC, Unpaywall and "
               f"OpenAlex" + (", then " + (" and ".join(later) if len(later) < 3 else
                                           ", ".join(later[:-1]) + " and " + later[-1]) if later else "")
               + f"; {sum(got.values())} were obtained "
               "and parsed" + (" (" + ", ".join(f"{FULLTEXT_NAMES.get(k, k)} {v}" for k, v in got.most_common()) + ")"
                               if len(got) > 1 else "")
               + ", and the remaining records were charted from titles and abstracts.")
    dims = [d["name"] for d in (protocol.get("map") or {}).get("dimensions", [])]
    fex = tables._latest(store, "study_features", run_id)
    feat = db.execute("SELECT count(*), sum(grounded) FROM study_features WHERE run_id = ? AND extractor = ? "
                      "AND grounded IS NOT NULL", (run_id, fex)).fetchone()
    n_charted = len({r[0] for r in db.execute("SELECT DISTINCT work_id FROM study_features WHERE run_id = ? AND "
                                             "extractor = ?", (run_id, fex))} & set(kept))
    later = _later_dims(store, run_id, protocol, fex, kept)
    first = [d for d in dims if not any(d in names.split(",") for names in later)]
    add(chart, f"For {n_charted} retained studies an agent charted {', '.join(dim_label(protocol, d) for d in first)}; "
               f"every charted value carried a verbatim quote unless it was not reported, and {feat[1] or 0} of "
               f"{feat[0]} quotes were confirmed in the source text.")
    for names, n in later.items():
        add(chart, f"A separate charting pass after the second review round added "
                   + " and ".join(dim_label(protocol, d) for d in names.split(",")) + f" for the core studies, leaving "
                   f"their other charted values unchanged ({n} retained records now have "
                   + ("it" if "," not in names else "them") + "; records charted later have them from the start).")
    unconfirmed = db.execute("SELECT work_id, dimension FROM study_features WHERE run_id = ? AND extractor = ? AND "
                             "grounded = 0", (run_id, fex)).fetchall()
    if unconfirmed:
        meta = tables.works(store, {r["work_id"] for r in unconfirmed}, run_id)
        add(chart, "Unconfirmed charting quotes: " + "; ".join(f"{meta[r['work_id']]['cite']}, {r['dimension']}"
                                                              for r in unconfirmed)
                   + ". The values were kept in the store, marked as unconfirmed, and count as 'cannot tell' where "
                     f"{L['eligibility']} rests on them.")
    frows = tables.finding_rows(store, run_id)
    if frows:
        src = Counter(r["source"] or "unconfirmed" for r in frows)
        with_f = {r["work_id"] for r in frows}
        add(chart, f"An agent extracted {len(frows)} findings from {len(with_f)} studies, each with the verbatim "
                   f"passage that states it; {sum(1 for r in frows if r['grounded'])} passages were confirmed in the "
                   f"source ({src.get('fulltext', 0)} from full texts, {src.get('abstract', 0)} from abstracts). The "
                   f"other {len(set(kept) - with_f)} retained records yielded no finding; {L['without']} gives the reason "
                   "for each.")
        parsed = {w for w in kept if ft_status(store, w) == "parsed"}
        from_parsed = [r for r in frows if r["work_id"] in parsed]
        if parsed:
            s2 = Counter(r["source"] or "unconfirmed" for r in from_parsed)
            add(chart, f"The findings agent was given both the abstract and the full text for the {len(parsed)} "
                       f"studies with a parsed full text; the {len({r['work_id'] for r in from_parsed})} of them "
                       f"that contributed findings yielded {len(from_parsed)} findings, {s2.get('fulltext', 0)} "
                       f"quoting the full text and {s2.get('abstract', 0)} the abstract. A finding's source label "
                       f"({L['sources']}'s 'Charted from' describes the study, not each finding) says which text its "
                       "passage was found in.")
    _eligibility_facts(store, run_id, protocol, add)
    _repository_facts(store, run_id, protocol, add)
    _synthesis_facts(store, run_id, protocol, add)
    from_abstracts = [b for b in protocol.get("background") or [] if "from the review's abstract" in (b.get("checked") or "")
                      or "written from the abstract" in (b.get("checked") or "")]
    if from_abstracts:
        add("Synthesis of results", f"The summaries of {len(from_abstracts)} background sources, among them the prior "
                                    "reviews, were written from their abstracts, not from their full texts; the other "
                                    "background sources are cited for standard summaries not checked against their texts. "
                                    "Each is cited only for its summary.")
    synth = "Synthesis and use of AI"
    stages = _models_by_stage(store, run_id)
    if stages:
        add(synth, "Models by stage: " + "; ".join(f"{k}: {', '.join(v)}" for k, v in stages.items())
                   + ". Each agent ran as a separate session with the model's default settings and saw only its task "
                     "folder (the protocol-generated instructions and its batch of records); every instruction text "
                     "and answer is stored with the evidence. The audit and the reviewers are agents of the same model "
                     "family as the writer, not independent human verification.")
    n_ins = db.execute("SELECT count(*), sum(status != 'noted'), sum(stage = 'synthesize') FROM insights "
                       "WHERE corpus = ?", (corpus,)).fetchone()
    add(synth, f"Patterns, contradictions and gaps the agents noticed while screening, charting and extracting were "
               f"logged as insights ({n_ins[0]} in total, {n_ins[1] or 0} reviewed by the author)"
               + (", and a synthesis agent proposed cross-study insights from all findings." if n_ins[2] else "."))
    add(synth, "This manuscript was drafted by a language-model agent that could cite only the extracted findings, "
               "these method facts and listed background sources; each sentence's citations and numbers were checked "
               "in code, a separate agent audited each sentence against the passages it cites, and independent agent "
               "reviewers assessed the manuscript before revision.")
    return facts


def _citation_facts(store, run_id, protocol, m, late, add):
    """Citation searching, each pass reconciled: seeds, lookups, matches, merges, and the fate of the added records."""
    from .agents import kept_ids, screened_ids, sheet_ids
    search = "Information sources and search"
    passes = m.get("passes") or [m]
    last = passes[-1]
    from .mining import seeds
    scorer = tables._scorer(store, run_id)
    reviews = set(seeds(store, run_id, protocol)["reviews"])
    passed_ta = set(kept_ids(store, run_id, "title_abstract"))
    by_scorer = {r[0] for r in store.db.execute("SELECT work_id FROM screening WHERE run_id = ? AND screener = ? AND "
                                                "decision = 'exclude' AND reason = 'review' AND stage != 'full_text'",
                                                (run_id, scorer))}
    n_scorer = len(reviews & by_scorer - passed_ta)
    earlier = [p for p in passes[:-1] if p.get("date")]
    via = sorted({p.get("via") or "Semantic Scholar" for p in passes}, key=[p.get("via") or "Semantic Scholar"
                                                                           for p in passes].index)
    add(search, f"Citation searching used {' and then '.join(via)}"
        + (f" in {len(passes)} passes ({', '.join(p.get('date', '?') for p in passes)}); each later pass started from "
           "more core studies, so the last pass, from every current core study, is reported in full" if len(passes) > 1
           else f" on {last.get('date', '?')}") + ".")
    looked = last.get("lookups")
    add(search, f"The last pass searched the reference lists of the {last.get('seeds_reviews', 0)} records that at least "
                f"one screening agent called a review and that were not retained ({n_scorer} of them excluded at title "
                f"and abstract as reviews by the criteria-scoring agent, the count given below; the others were called a "
                f"review by the second agent only, or were excluded at a later stage) and of the "
                f"{last.get('seeds_core', 0)} studies that were core at the time, and the works citing those studies"
        + (f"; of {looked} lookups, by the "
           + ("OpenAlex identifier" if last.get("via") == "OpenAlex" else "Semantic Scholar identifier")
           + ", the DOI or the PubMed ID in turn"
           + (f" (and a title match for {last['by_title']} records without any)" if last.get("by_title") else "")
           + f", {last.get('failed', 0)} failed under every identifier" if looked is not None else
           f"; {last.get('failed', 0)} lookups failed")
        + (f", and {last.get('no_identifier', 0)} records had no identifier and no title match" if last.get("no_identifier")
           else "") + ".")
    merged = last.get("matched", 0) - last.get("matched_works", last.get("matched", 0))
    add(search, f"It returned {last.get('references', 0)} references and {last.get('citations', 0)} citations, "
                f"{last.get('unique', 0)} distinct works; {last.get('matched', 0)} matched "
                + ("a broader string with synonyms, given in the protocol," if last.get("broadened") else "the search string")
                + " in their title or abstract"
                + (f", which are {last['matched_works']} records after merging duplicates" if merged else "")
                + f"; {last.get('already', 0)} of them were already among the records and {last.get('new', 0)} were new.")
    if last.get("broadened"):
        add(search, "String for citation searching: `" + last.get("query", m.get("query", "")) + "`.")
    sheet = set(sheet_ids(store, run_id))
    added = late & sheet
    screened = added & set(screened_ids(store, run_id))
    passed, kept = set(kept_ids(store, run_id, "title_abstract")), set(kept_ids(store, run_id))
    core = {e["work_id"] for e in tables.eligibility(store, run_id, protocol) if e["core"]} if tables.criteria(protocol) \
        else set()
    add(search, f"Across all passes, {len(added)} records that the database search had not found were added; "
                + ("they were screened like every other record" if screened == added else
                   f"{len(screened)} of them were screened like every other record and the other "
                   f"{len(added - screened)} were left unscreened by a decision recorded in the protocol")
                + f"; {len(added & passed)} of them were kept at title and abstract, "
                f"{len(added & kept)} were retained after the eligibility stage, and {len(added & core)} are core studies.")
    _recall_facts(store, run_id, protocol, add)


def _recall_facts(store, run_id, protocol, add):
    from . import recall
    if not (protocol.get("recall") or {}).get("sources"):
        return
    try:
        s = recall.summary(store, run_id, protocol)
    except ValueError:
        return
    if not s["references"]:
        return
    L = tables.table_labels(protocol)
    f = s["found"]
    judged = s["missed_judged"]
    add("Information sources and search",
        f"A recall test, added after the second review round, retrieved on {s['date']} from OpenAlex the reference "
        f"lists of the two consensus statements and the four prior reviews named in the Introduction: "
        f"{s['references']} distinct records, of which the search had found {f.get('database', 0) + f.get('citation_search', 0)} "
        f"({f.get('database', 0)} by the database search, {f.get('citation_search', 0)} only by citation searching) and "
        f"{f.get('not_found', 0)} not. Relevance was judged on the review's own title-and-abstract criteria: "
        f"{s['relevant_found']} of the found records had been kept by the review's screening, and the two screening "
        f"models scored the {judged} records not found on the same criteria, at least one including "
        f"{s['relevant_missed']} and at least one unsure about {s['unsure_missed']} more."
        + (f" Recall was thus {round(100 * s['recall'])}% ({s['relevant_found']} of "
           f"{s['relevant_found'] + s['relevant_missed']}), or {round(100 * s['recall_if_unsure_relevant'])}% if the "
           f"unsure records were relevant; {s['relevant_missed_with_label']} of the {s['relevant_missed']} relevant "
           f"records missed used one of the search phrases in their title or abstract"
           + (f", and {s['relevant_missed_with_broad']} would have matched the broader string used for citation "
              "searching" if s.get("relevant_missed_with_broad") is not None else "") + "."
           if s["recall"] is not None and judged else "")
        + (f" {L['recall']} gives the counts by source and lists the records missed." if L.get("recall") else ""))
    tried = recall.trials(store, run_id)
    if tried:
        own = next((t for t in tried if t["label"] == "the review's search string"), None)
        broad = next((t for t in tried if t["label"].startswith("the broader string")), None)
        langs = [t for t in tried if t["label"].startswith("non-English")]
        add("Information sources and search",
            f"As trials on {tried[0]['created_at'][:10]}, in OpenAlex titles and abstracts"
            + (f", the broader string returned {broad['total']} records against {own['total']} for the review's string; "
               "it was counted, not screened" if broad and own else "")
            + ("; the term in other languages (" + ", ".join(t["label"].replace("non-English phrase ", "") + f" {t['total']}"
                                                           for t in langs)
               + f") found {sum(t['fetched'] or 0 for t in langs)} records, {sum(t['in_pool'] or 0 for t in langs)} of "
               "them already among the records; the others were added and screened like every other record"
               if langs else "") + ".")


def _abstract_facts(store, run_id, protocol, kept, add):
    from . import abstracts
    s = abstracts.summary(store, run_id)
    if not s["tried"]:
        return
    by = ", ".join(f"{abstracts.NAMES.get(k, source_name(k))} {v}"
                   for k, v in sorted(s["by_source"].items(), key=lambda kv: -kv[1]))
    reach = {w for w in s["ids"] if w in set(kept)}
    order = [abstracts.NAMES[k] for k in abstracts.SOURCES if k != "core" or "core" in s["by_source"]]
    add("Data charting",
        f"Abstracts were looked up on {s['date']} for the {s['tried']} retained records that had only a title, by DOI "
        f"or PubMed ID in {', '.join(order[:-1])} and {order[-1]} in turn; {s['found']} were found ({by or 'none'}), "
        "and the others are mostly commentaries, editorials, book "
        f"chapters and conference records that no source indexes with an abstract."
        + (f" The {len(reach)} records that gained an abstract were judged again at the eligibility stage by both "
           "agents, charted again and, where in the findings scope, their findings extracted." if reach else ""))


def _later_dims(store, run_id, protocol, extractor, kept) -> dict[str, int]:
    """Dimensions charted in a separate pass (a task with `dims`): {"fidelity,institution": retained studies charted}."""
    names = [d["name"] for d in (protocol.get("map") or {}).get("dimensions") or []]
    charted = Counter()
    kept = set(kept)
    for r in store.db.execute("SELECT dimension, work_id FROM study_features WHERE run_id = ? AND extractor = ?",
                              (run_id, extractor)):
        if r["work_id"] in kept:
            charted[r["dimension"]] += 1
    full = max(charted.values(), default=0)
    late = [d for d in names if 0 < charted.get(d, 0) < full and d in LATER_DIMENSIONS]
    groups: dict[int, list[str]] = {}
    for d in late:
        groups.setdefault(charted[d], []).append(d)
    return {",".join(v): k for k, v in groups.items()}


LATER_DIMENSIONS = ("fidelity", "institution", "comparison_kind")


def _repository_facts(store, run_id, protocol, add):
    """Records hosted by a repository rather than a journal ([repository] in the protocol): how a published version was
    sought and what was decided, next to the overlapping reports."""
    rows, n = tables.repository_records(store, run_id, protocol)
    if not rows:
        return
    spec = protocol.get("repository") or {}
    L = tables.table_labels(protocol)
    add("Eligibility at charting and overlapping reports",
        f"{n['all']} records were hosted by a repository rather than a journal, {n['deposit']} of them data deposits. "
        f"To find a published version, each title was searched in OpenAlex and Crossref on {spec.get('date', 'an undated day')} "
        f"(for a data deposit also without words such as 'Dataset for'), and the review's own records were searched for "
        f"shared authors and title words, by {spec.get('who', 'the reviewer')}: a published version was found for "
        f"{n['found']} of them. "
        f"{n['excluded']} were excluded as copies of a retained report (a data deposit or a mis-described record of a "
        f"retained article) and {n['retained']} were kept, {n['findings']} of them with findings; each record, the check "
        f"and the decision are listed in {L.get('repository', 'the supplement')}.")


def _synthesis_facts(store, run_id, protocol, add):
    """How the findings were grouped, counted and coded: the synthesis method, from the protocol and the store."""
    f = protocol.get("findings") or {}
    fields = {d["name"]: d for d in f.get("fields") or []}
    if not fields:
        return
    L = tables.table_labels(protocol)
    group = "Synthesis of results"
    only = (protocol.get("paper") or {}).get("core_only")
    kinds = ", ".join(c.replace("_", " ") for c in (fields.get("evidence_type") or {}).get("categories") or [])
    specs = tables.count_specs(protocol)
    add(group, ("Findings were grouped by the outcome they concern and, within each outcome, by the kind of evidence "
                f"coded on each finding ({kinds}). " if kinds else
                "The unit of synthesis is the study: the findings agent coded each finding on the protocol's fields ("
                + ", ".join(k.replace("_", " ") for k in fields) + "), and the tables count the studies with at least one "
                "finding in a cell, each study once per cell"
                + (" (" + "; ".join(f"{s['rows'].replace('_', ' ')} by {s['cols'].replace('_', ' ')}"
                                    + (" for findings coded " + "; ".join(
                                        f"{k.replace('_', ' ')} {', '.join(x.replace('_', ' ') for x in v)}"
                                        for k, v in s["where"].items()) if s.get("where") else "")
                                    for s in specs) + ")" if specs else "") + ". ")
               + ("The tables in the paper count the core studies with findings, each study once per cell; records "
                  "outside the core are reported in the supplement. " if only else "")
               + ("For the evidence map, counts describe where the findings point; the pre-specified tests that count "
                  "studies by direction are described with the aim." if (protocol.get("aim") or {}).get("kind") == "focus"
                  else "Counts are a descriptive map of where the findings point, not a test of effectiveness or a vote "
                       "count."))
    d = fields.get("direction") or {}
    defined = [k for k in ("direction", "evidence_type") if (fields.get(k) or {}).get("definitions")]
    for k in defined:
        add(group, f"The {'direction' if k == 'direction' else 'kind-of-evidence'} codes were defined in operational "
                   f"terms {fields[k].get('defined_when', 'after the second review round')}, and are therefore post hoc: "
                   + " ".join(" ".join(str(x).split()).replace("_", " ") for x in fields[k]["definitions"]))
    if defined:
        has = store.db.execute("SELECT 1 FROM sqlite_master WHERE name = 'finding_recodes'").fetchone()
        rows = tables.finding_rows(store, run_id)
        ids = {r["finding_id"] for r in rows}
        changed = {}
        if has:
            for r in store.db.execute("SELECT finding_id, field FROM finding_recodes"):
                if r["finding_id"] in ids and r["field"] in defined:
                    changed.setdefault(r["field"], set()).add(r["finding_id"])
        n_calls = store.db.execute("SELECT count(*) FROM calls WHERE run_id = ? AND json_extract(meta, '$.task') = "
                                   "'recode'", (run_id,)).fetchone()[0]
        if n_calls:
            add(group, f"A recoding agent re-applied these definitions to the findings, keeping each finding's text and "
                       "passage; it changed " + " and ".join(
                           f"the {'direction' if k == 'direction' else 'kind-of-evidence'} code of "
                           f"{len(changed.get(k, ()))} of the {len(rows)} findings" for k in defined)
                       + ", and every change is kept with the old code.")
    if d and "direction" not in defined:
        add(group, "The findings agent coded each finding's direction as instructed: " + " ".join(d["question"].split()))
    focus = (protocol.get("aim") or {}).get("kind") == "focus"
    add(group, "Reports that may share a sample are grouped "
               + (f"({L['overlaps']}) " if L.get("overlaps") else "")
               + ("for the record; as pre-specified, the tests count each report as one study (a record entered "
                  "twice counts once), so reports of the same sample count separately. " if focus else
                  "and counted once wherever a table or the text gives independent studies. ")
               + "For each study the tables give the text it was charted from (full text, "
               "abstract or title), so that statements resting on abstracts can be told apart; the drafting agent was "
               "instructed to name each cited study's design and to present contradictory findings as contradictions.")


def _eligibility_stage(store, run_id, protocol, rows, passed, kept, add):
    group = "Selection of sources of evidence"
    e = protocol.get("eligibility") or {}
    agents = sorted({r["screener"].split(":", 1)[1] for r in rows if r["screener"].startswith("model:")})
    humans = sorted({r["screener"] for r in rows if r["screener"].startswith("human:")})
    from .agents import protocol_exclusions
    dropped = {w: e for w, e in protocol_exclusions(store, run_id).items() if w in passed}
    out = passed - set(kept) - set(dropped)
    given: dict[int, list[str]] = {}
    for r in rows:
        if r["work_id"] in out and r["decision"] == "exclude":
            given.setdefault(r["work_id"], []).append((r["reason"] or "other").replace("_", " "))
    reasons = Counter(Counter(v).most_common(1)[0][0] for v in given.values())   # one reason per record
    add(group, "At a second, eligibility stage the records retained at title and abstract were judged against "
               "stricter criteria" + (f" by {len(agents)} language-model agent{'s' if len(agents) > 1 else ''}" if agents
                                      else "") + (" and the author" if humans else "")
               + ", from titles, abstracts and full texts where available: "
               + (" ".join(str(e["summary"]).split()) if e.get("summary") else " ".join(str(e.get("rule", "")).split())))
    models = sorted({r["screener"] for r in rows if r["screener"].startswith("model:")})
    add(group, f"Of the {len(passed)} records judged there, {len(out)} were excluded"
               + (f" (main reason given: {', '.join(f'{k} {n}' for k, n in reasons.most_common())})" if reasons else "")
               + f" and {len(passed) - len(out)} were kept; "
               + ("a record was kept when either agent kept it, so disagreements were resolved towards inclusion, "
                  "and records an agent could not judge were kept." if len(models) > 1 else
                  "records the agent could not judge were kept."))
    if len(models) > 1:
        both = {}
        for r in rows:
            if r["screener"] in models:
                both.setdefault(r["work_id"], {})[r["screener"]] = r["decision"] != "exclude"
        common = [v for v in both.values() if len(v) == len(models)]
        if common:
            pairs = [("keep" if v[models[0]] else "exclude", "keep" if v[models[1]] else "exclude") for v in common]
            agree = sum(a == b for a, b in pairs)
            k = agreement.cohen_kappa(pairs)
            add(group, f"The two eligibility agents both judged {len(common)} records and agreed on keep or exclude for "
                       f"{agree} ({round(100 * agree / len(common))}%" + (f", Cohen's kappa {k:.2f}" if k is not None else "")
                       + ").")
    L = tables.table_labels(protocol)
    if dropped and L.get("exclusions"):
        dates = sorted({e.get("date", "undated") for e in dropped.values()})
        on = ", ".join(dates[:-1]) + " and " + dates[-1] if len(dates) > 1 else dates[0]
        add(group, f"After the eligibility stage, {len(dropped)} record{'s were' if len(dropped) > 1 else ' was'} "
                   f"excluded by decisions recorded in the protocol on {on}, each with its reason "
                   f"({L['exclusions']}); {len(kept)} records were retained for charting.")
    elif dropped:
        cites = tables.works(store, dropped, run_id)
        add(group, f"After the eligibility stage, {len(dropped)} record{'s were' if len(dropped) > 1 else ' was'} "
                   "excluded by a decision recorded in the protocol: "
                   + "; ".join(f"'{cites[w]['title']}' ({cites[w]['year'] or 'n.d.'}), excluded on "
                               f"{e.get('date', 'an undated day')} by {e.get('who', 'the reviewer')}, reason: "
                               f"{e.get('reason', 'outside the review')}" for w, e in dropped.items())
                   + f". {len(kept)} records were retained for charting.")
    add(group, f"Without the eligibility stage, {len(passed) - len(dropped)} records would have been retained for "
               f"charting instead of {len(kept)}.")


def _models_by_stage(store, run_id) -> dict[str, list[str]]:
    """Which model did each stage, from the stored decisions and the logged agent calls."""
    db = store.db
    name = lambda a: a.split(":", 1)[1].split(":")[0] if ":" in a else a  # noqa: E731
    out = {}
    screeners = sorted({name(r[0]) for r in db.execute(
        "SELECT DISTINCT screener FROM screening WHERE run_id = ? AND stage != 'full_text' AND screener LIKE 'model:%'",
        (run_id,))})
    if screeners:
        out["title-and-abstract screening"] = screeners
    elig = sorted({name(r[0]) for r in db.execute(
        "SELECT DISTINCT screener FROM screening WHERE run_id = ? AND stage = 'full_text' AND screener LIKE 'model:%'",
        (run_id,))})
    if elig:
        out["eligibility"] = elig
    for label, table in (("charting", "study_features"), ("findings extraction", "findings")):
        ms = sorted({name(r[0]) for r in db.execute(f"SELECT DISTINCT extractor FROM {table} WHERE run_id = ?",
                                                     (run_id,))})
        if ms:
            out[label] = ms
    for label, task in (("recall test", "recall"), ("recoding of findings", "recode"), ("drafting", "paper"),
                        ("revision", "revise"), ("sentence audit", "audit"), ("peer review", "peer"),
                        ("editorial decisions", "meta")):
        ms = sorted({name(r[0]) for r in db.execute(
            "SELECT DISTINCT json_extract(meta, '$.agent') FROM calls WHERE run_id = ? AND kind = 'llm' "
            "AND json_extract(meta, '$.task') = ?", (run_id, task)) if r[0]})
        if ms:
            out[label] = ms
    return out


def ft_status(store: Store, work_id: int) -> str | None:
    row = store.get_fulltext(work_id)
    return row["status"] if row else None


def _deviations(store, run_id, protocol, screen, scorer, kept, add):
    """Records the human kept although the criteria-scoring agent excluded them, and protocol-listed deviations."""
    group = "Protocol deviations"
    L = tables.table_labels(protocol)
    human = {}
    for s in screen:
        if s["screener"].startswith("human:"):
            human[s["work_id"]] = s
    agent = {s["work_id"]: s for s in screen if s["screener"] == scorer}
    dev = [w for w in kept if w in human and human[w]["decision"] != "exclude"
           and agent.get(w) is not None and agent[w]["decision"] == "exclude"]
    if dev:
        reasons = Counter((agent[w]["reason"] or "other").replace("_", " ") for w in dev)
        days = sorted({human[w]["created_at"][:10] for w in dev})
        noted = sum(1 for w in dev if (human[w]["note"] or "").strip())
        charted = tables.charting(store, run_id)
        last = max((r["created_at"] for r in tables.finding_rows(store, run_id)), default="")
        late = [w for w in dev if w not in charted and last and human[w]["created_at"] > last]
        add(group, f"The inclusion rule was not applied strictly. The author retained {len(dev)} records that the "
                   "criteria-scoring agent had excluded under it ("
                   + ", ".join(f"{k} {n}" for k, n in reasons.most_common()) + f"), deciding while screening on "
                   f"{' and '.join(days)} with the agents' decisions visible; "
                   + ("no reason was recorded for these decisions. " if not noted else
                      f"a reason was recorded for {noted} of them. ")
                   + (f"{len(late)} of them were retained after charting was done and were not charted. "
                      if late else "")
                   + f"They are reported as a separately labelled set ({L['eligibility']}), and the review question is answered "
                     "first from the core.")
    for d in protocol.get("deviations") or []:
        add(group, f"Deviation recorded in the protocol ({d.get('date', 'undated')}, {d.get('who', 'author')}): "
                   f"{' '.join(d.get('what', '').split())} Reason: {' '.join(d.get('why', 'not stated').split())}")


def _eligibility_facts(store, run_id, protocol, add):
    crit = tables.criteria(protocol)
    if not crit:
        return
    elig = tables.eligibility(store, run_id, protocol)
    group = "Eligibility at charting and overlapping reports"
    L = tables.table_labels(protocol)
    core = [e for e in elig if e["core"]]
    core_f = [e for e in core if e["findings"]]
    add(group, f"Each retained record was judged against the inclusion criteria from its charting, with the "
               f"title-and-abstract scores as a check ({L['eligibility']}; the core studies are described in "
               f"{L.get('core', L['eligibility'])}). {len(core)} of the {len(elig)} retained records meet "
               "every criterion and form the core"
               + (f" (listed in {L['core']})" if core and "core" in L else "")
               + f"; {len(core_f)} of them contributed {sum(e['findings'] for e in core)} findings.")
    if (protocol.get("paper") or {}).get("core_needs_fulltext"):
        pot, abst = [e for e in elig if e.get("potential")], [e for e in elig if e.get("abstract_only")]
        add(group, "A study counts as core only when it was charted from its full text (a parsed text of at least "
                   f"{tables.FULL_MIN} characters, charted after it was obtained); conference abstracts count as "
                   "abstracts"
                   + ("; a study read in full that yielded no findings of its own does not count as an original "
                      "empirical study" if any(c.get("no_results_fails") for c in crit) else "")
                   + f". {len(pot)} retained studies meet every criterion on their abstract only and are set apart as "
                     f"potential core studies until their full text is read"
                   + (f"; {len(abst)} conference abstracts meet every criterion as abstracts" if abst else "")
                   + (". None of their findings enter the synthesis." if (protocol.get("paper") or {}).get("core_only")
                      else "."))
    rest = Counter(e["group"] for e in elig if not e["core"])
    if rest:
        add(group, f"The other {len(elig) - len(core)} retained records form a separately labelled set, grouped by "
                   "the first criterion they do not meet: " + "; ".join(f"{g} {n}" for g, n in rest.most_common())
                   + ".")
    # the text each set was charted from, so absence claims can be weighed by what was read
    meta = tables.works(store, [e["work_id"] for e in elig])

    def source_of(w):
        ft = store.get_fulltext(w)
        return "full text" if ft and ft["status"] == "parsed" else             "abstract" if len((meta[w].get("abstract") or "").strip()) > 50 else "title only"

    def split(es):
        n = Counter(source_of(e["work_id"]) for e in es)
        return ", ".join(f"{n[k]} from {k if k != 'title only' else 'the title only'}" for k in
                         ("full text", "abstract", "title only") if n[k])
    with_f = [e for e in elig if e["findings"]]
    add(group, f"By the text they were charted from, the {len(elig)} retained records divide into {split(elig)}; "
               + (f"the {len(core)} core studies into {split(core)}; " if core else "")
               + f"and the {len(with_f)} studies with findings into {split(with_f)}. A record with only a title "
               "cannot meet a criterion that needs its content, so none of them is a core study.")
    contrib = [e for e in elig if e["findings"]]
    if contrib:
        add(group, f"Of the {len(contrib)} studies that contributed findings, "
                   + "; ".join(f"{sum(1 for e in contrib if e['cells'][c['id']] == 'yes')} meet the "
                               f"{c.get('short', c['id'])} criterion" for c in crit)
                   + f"; {sum(1 for e in contrib if e['core'])} meet all of them.")
    ov = tables.overlaps(store, run_id, protocol)
    if ov["groups"]:
        n_in = sum(len(g["works"]) for g in ov["groups"])
        ids = lambda rows: {e["work_id"] for e in rows}
        add(group, f"{n_in} retained reports fall into {len(ov['groups'])} groups that may share a sample, because "
                   + ("they name the same data set, have the same title or abstract, or are named as one study in the protocol "
                      + ("or share two or more authors" if (protocol.get("paper") or {}).get("overlap_by_authors", True)
                         else "(shared authors are not counted)"))
                   + f" ({L['overlaps']}). Counting each group once, the "
                   f"{len(elig)} retained reports reduce to {tables.independent(ov, ids(elig))}, the {len(contrib)} "
                   f"that contributed findings to {tables.independent(ov, ids(contrib))} and the {len(core)} core "
                   f"reports to {tables.independent(ov, ids(core))}.")
    used = " ".join(f"{r['finding']} {r['evidence']} {r['for_whom'] or ''}" for r in tables.finding_rows(store, run_id))
    used += " " + tables_markdown(store, run_id, protocol)
    abbr = {a: n for a, n in ov["defined"].items() if re.search(rf"\b{re.escape(a)}\b", used)}
    # abbreviations are listed under the tables, not in the Method
    scope = tables.in_scope(store, run_id, protocol)
    if scope is not None:
        rules = (protocol.get("findings") or {}).get("scope") or []
        add(group, f"Findings were extracted from the {len(scope)} retained studies within the protocol's findings "
                   "scope (core studies, and "
                   + "; or ".join(r.get("label") or f"studies charted with {dim_label(protocol, r['dimension'])} "
                                  f"{', '.join(v.replace('_', ' ') for v in r['values'])}" for r in rules)
                   + f"); the other {len(elig) - len(scope)} retained studies were charted only.")
        brief = (protocol.get("findings") or {}).get("brief") or {}
        short = tables.brief_ids(store, run_id, protocol, sorted(scope))
        if short:
            rule = lambda rs: "; or ".join(f"{r['dimension']} {', '.join(v.replace('_', ' ') for v in r['values'])}"  # noqa: E731
                                          for r in rs)
            add(group, f"{len(short)} of them, not core studies, whose charting showed {rule(brief.get('when') or [])} "
                       f"but no {rule(brief.get('unless') or [])}, were given a brief extraction of at most "
                       f"{int(brief.get('max_per_study', 3))} findings each, on "
                       + " ".join(str(brief.get("focus", "the question")).split())
                       + f"; the others, of at most {int((protocol.get('findings') or {}).get('max_per_study', 6))}.")
    none = tables.without_findings(store, run_id, elig, scope)
    if none:
        why = Counter("title only" if r["text"] == "title only" and "after charting" not in r["why"] else
                      "outside scope" if "findings scope" in r["why"] else
                      "retained after charting" if "after charting" in r["why"] else
                      "no own results" if r["type"] in ("review", "editorial", "dataset") else "no result on the question"
                      for r in none)
        n_core = sum(1 for r in none if r["group"] == "core")
        parts = [(why.get("title only", 0), "had neither an abstract nor an open-access full text",
                  "had neither an abstract nor an open-access full text"),
                 (why.get("outside scope", 0), "were charted only, outside the findings scope",
                  "was charted only, outside the findings scope"),
                 (why.get("retained after charting", 0), "were retained after charting was done and not charted",
                  "was retained after charting was done and not charted"),
                 (why.get("no own results", 0), "were reviews, editorials or data-set records without results of "
                                                 "their own", "was a review, editorial or data-set record without "
                                                              "results of its own"),
                 (why.get("no result on the question", 0), "reported no result on the review question in the text "
                                                           "available", "reported no result on the review question "
                                                                        "in the text available")]
        parts = [f"{n} {one if n == 1 else many}" for n, many, one in parts if n]
        add(group, f"Of the {len(none)} retained records without findings ({L['without']}), "
                   + (", ".join(parts[:-1]) + " and " + parts[-1] if len(parts) > 1 else parts[0] if parts else "")
                   + f"; {n_core} of them {'meets' if n_core == 1 else 'meet'} every inclusion criterion.")


def table_label(tid: str) -> str | None:
    """F1 -> Figure 1, T7 -> Table 7, S5 -> Table S5 (the inverse of tables.label_id)."""
    m = re.fullmatch(r"([FTS])(\d+)", str(tid))
    if not m:
        return None
    return {"F": "Figure ", "T": "Table ", "S": "Table S"}[m.group(1)] + m.group(2)


def table_items(store: Store, run_id: int, protocol: dict) -> list[dict]:
    """Figure 1 and the tables as citable items {id, label, place, text}: in the paper (the core studies' characteristics,
    counts, overlapping reports, the evidence map as counts) and in the supplement (the record-level tables)."""
    L = tables.table_labels(protocol)
    elig = tables.eligibility(store, run_id, protocol) if tables.criteria(protocol) else []
    core = {e["work_id"] for e in elig if e["core"]}
    contributing = {r["work_id"] for r in tables.finding_rows(store, run_id)}
    ov = tables.overlaps(store, run_id, protocol)
    pp = protocol.get("paper") or {}
    only = core & contributing if pp.get("core_only") else None
    note = (lambda text: text + "\n\n" + tables.independent_note(ov, only).strip() if text and only else text)  # noqa: E731
    parts = [("flow", "main", flow(store, run_id, elig, L))]
    if (protocol.get("aim") or {}).get("kind") == "focus":
        # a hypothesis-testing paper: the evidence statements and the test in the main text, the studies in the supplement
        from . import rulemap
        parts.append(("statements", "main", rulemap.statements_table(store, run_id, protocol, L["statements"])))
        if "sensitivity" in L:
            parts.append(("sensitivity", "main", rulemap.sensitivity_table(store, run_id, protocol, L["sensitivity"])))
        if "strata" in L:
            parts.append(("strata", "main", rulemap.strata_table(store, run_id, protocol, L["strata"])))
        parts.append(("studies", "supplement", rulemap.studies_list(store, run_id, protocol, L["studies"])))
    elif (protocol.get("aim") or {}).get("kind") == "strong":
        # the strongest studies with their effects in the main text, every candidate's appraisal in the supplement
        from . import strong
        parts += [("effects", "main", strong.effects_table(store, run_id, protocol, L["effects"])),
                  ("appraisals", "supplement", strong.appraisal_table(store, run_id, protocol, L["appraisals"]))]
    elif protocol.get("aim"):
        from . import aim
        if "outcomes_aim" in L:
            parts.append(("outcomes_aim", "main", aim.outcomes_table(store, run_id, protocol, L["outcomes_aim"])))
        parts += [("solutions", "main", aim.solutions_table(store, run_id, protocol, L["solutions"])),
                  ("comparator", "main", aim.comparator_table(store, protocol, L["comparator"])),
                  ("patterns", "main", aim.patterns_table(store, run_id, protocol, L["patterns"])),
                  ("studies", "main", aim.studies_table(store, run_id, protocol, L["studies"])),
                  ("comparator_list", "supplement", aim.comparator_list(store, protocol, L["comparator_list"]))]
    if "core" in L:
        parts.append(("core", "main", tables.core_table(store, run_id, protocol, elig, L["core"], ov)))
    for i, spec in enumerate(pp.get("focus") or []):
        parts.append((f"focus{i}", "main", tables.comparison_table(store, run_id, protocol, spec, L[f"focus{i}"], only, ov)
                      if spec.get("group_by") else tables.focus_table(store, run_id, protocol, spec, L[f"focus{i}"], only, ov)))
    if pp.get("summary"):
        parts.append(("summary", "main", note(tables.summary_table(store, run_id, protocol, pp["summary"], L["summary"],
                                                                   L["subset_list"], only))))
    if pp.get("outcome_table"):
        parts.append(("outcomes", "main", tables.outcome_table(store, run_id, protocol, pp["outcome_table"],
                                                               L["outcomes"], only, ov)))
    for i, spec in enumerate(tables.count_specs(protocol)):
        parts.append((f"counts{i}", "main", note(tables.count_table(store, run_id, protocol, spec["rows"], spec["cols"],
                                                                    L[f"counts{i}"], spec.get("where"), spec.get("title"),
                                                                    spec.get("note"), only, ov, bool(spec.get("text")),
                                                                    L["overlaps"]))))
    parts.append(("overlaps", "main", tables.overlap_table(ov, contributing, L["overlaps"],
                                                           pp.get("overlap_by_authors", True))))
    if pp.get("programmes"):
        parts.append(("programmes", "main", tables.programme_table(store, run_id, protocol, pp["programmes"],
                                                                   L["programmes"], only, ov, core)))
    if pp.get("fidelity"):
        parts.append(("fidelity", "main", tables.fidelity_table(store, run_id, protocol, pp["fidelity"], L["fidelity"],
                                                                only, ov)))
    if pp.get("evidence_strength"):
        parts.append(("evidence_strength", "main", tables.strength_table(store, run_id, protocol, L["evidence_strength"],
                                                                         only, ov)))
    if "map" in L:
        parts.append(("map", "main", tables.evidence_counts(store, run_id, protocol, core, L["map"], L["map_list"], ov)))
    parts += [("sources", "supplement", sources_table(store, run_id, protocol, L["sources"])),
              ("eligibility", "supplement", tables.eligibility_table(elig, protocol, L["eligibility"])),
              ("map_list", "supplement", tables.evidence_map(store, run_id, protocol, core, L["map_list"],
                                                            L.get("core", L["eligibility"]))),
              ("without", "supplement", tables.without_table(
                  tables.without_findings(store, run_id, elig, tables.in_scope(store, run_id, protocol)),
                  L["without"], L["eligibility"]))]
    if pp.get("summary"):
        parts.append(("subset_list", "supplement", tables.subset_list(store, run_id, protocol, pp["summary"],
                                                                      L["subset_list"])))
    parts.append(("prisma", "supplement", tables.prisma_table(protocol, L, L["prisma"])))
    extra = {"reviews": lambda: tables.reviews_table(store, run_id, protocol, L["reviews"]),
             "exclusions": lambda: tables.exclusions_table(store, run_id, L["exclusions"]),
             "criteria": lambda: tables.criteria_text(protocol, L["criteria"]),
             "recall": lambda: tables.recall_table(store, run_id, protocol, L["recall"]),
             "repository": lambda: tables.repository_table(store, run_id, protocol, L["repository"]),
             "findings_list": lambda: tables.findings_list_table(store, run_id, protocol, pp.get("findings_list") or {},
                                                                 L["findings_list"])}
    for role in pp.get("supplement") or []:
        if role in extra:
            parts.append((role, "supplement", extra[role]()))
    # where a table goes follows its label (Table S... is in the supplement), so a focused paper can move tables
    items = [{"id": tables.label_id(L[role]), "label": L[role], "role": role,
              "place": "supplement" if L[role].startswith("Table S") else "main", "text": t}
             for role, place, t in parts if t]
    for wrong, right in (pp.get("spell") or {}).items():     # one spelling in every table ('Turkey' -> 'Türkiye')
        for i in items:
            i["text"] = re.sub(rf"\b{re.escape(wrong)}\b", right, i["text"])
    main = [i for i in items if i["place"] == "main"]
    text = "\n\n".join(i["text"] for i in main)
    # A term counts when it stands alone: 'DASS' is not used where only 'DASS-21' is. `[paper.abbreviations]` gives the
    # full name where the records spell it out badly, or "" to leave a term out.
    fixed = pp.get("abbreviations") or {}
    abbr = {a: fixed.get(a, n) for a, n in ov["defined"].items()
            if re.search(rf"(?<![\w-]){re.escape(a)}(?![\w-])", text)}
    abbr = {a: n for a, n in abbr.items() if n}
    if abbr and main:
        main[-1]["text"] += "\n\n*Abbreviations:* " + "; ".join(f"{a}, {n}" for a, n in sorted(abbr.items())) + "."
    return items


def tables_markdown(store: Store, run_id: int, protocol: dict) -> str:
    """Figure 1 and Tables 1-5, inserted after the Method section."""
    return "\n\n".join(i["text"] for i in table_items(store, run_id, protocol))


def study_details(store: Store, run_id: int, protocol: dict) -> dict[int, dict]:
    """What the writer may say about each study besides its findings: type, its charted map dimensions and
    population, the text it was charted from, and where it falls in the eligibility table."""
    chart = tables.charting(store, run_id)
    elig = {e["work_id"]: e for e in tables.eligibility(store, run_id, protocol)} if tables.criteria(protocol) else {}
    dims = [d["name"] for d in ((protocol.get("map") or {}).get("dimensions") or []) if d["name"] != "population"]
    dims = dims or ["setting", "construct", "design"]
    out = {}
    for w, meta in tables.works(store, {r["work_id"] for r in tables.finding_rows(store, run_id)}, run_id).items():
        c = chart.get(w, {})
        value = lambda d: ", ".join(v.replace("_", " ") for v in c.get(d, {}).get("values", [])) or "not charted"  # noqa: E731
        has_abstract = len((meta.get("abstract") or "").strip()) > 50
        out[w] = {"type": tables.kind_of(meta), **{d: value(d) for d in dims},
                  "population": (c.get("population") or {}).get("text") or "not charted",
                  "charted_from": "full text" if ft_status(store, w) == "parsed" else
                  "abstract" if has_abstract else "title only",
                  "set": elig[w]["group"] if w in elig else None}
    return out


def findings_digest(data: dict, protocol: dict) -> str:
    """The writer's findings as a list to read: one line each, grouped by the first three findings fields, core
    studies first. batch_01.json stays the source; this is a view of it."""
    fields = [d["name"] for d in (protocol.get("findings") or {}).get("fields") or []]
    by, rest = fields[:3], fields[3:]
    studies = data.get("studies") or {}
    groups: dict[tuple, list] = {}
    for f in data.get("findings") or []:
        groups.setdefault(tuple(str((f.get("fields") or {}).get(k, "")) for k in by), []).append(f)
    lines = [f"# Findings by {', '.join(by)}", "",
             "A view of `findings` in batch_01.json, which remains the source: one line per finding, with its id "
             "(what a sentence cites), the study, its set and the text it was charted from, the finding, the effect "
             "as reported and, in brackets, for whom. Core studies come first in each group.", ""]
    for key in sorted(groups, key=lambda k: (-len(groups[k]), k)):
        fs = groups[key]
        lines += [f"## {' · '.join(f'{k}: {v}' for k, v in zip(by, key))} "
                  f"({len(fs)} findings from {len({f['work_id'] for f in fs})} studies)", ""]
        for f in sorted(fs, key=lambda f: (studies.get(str(f["work_id"]), {}).get("set") != "core", f["work_id"], f["id"])):
            s = studies.get(str(f["work_id"]), {})
            more = "; ".join(f"{k}: {(f.get('fields') or {}).get(k, '')}" for k in rest)
            lines.append(f"- F{f['id']} · {s.get('cite', f['work_id'])} ({s.get('set') or 'no set'}; "
                         f"{s.get('charted_from', '')}) · {f['finding']}"
                         + (f" — {f['effect']}" if f.get("effect") else "")
                         + (f" [{f['for_whom']}]" if f.get("for_whom") else "")
                         + (f" · {more}" if more else "")
                         + (f" · checked by a person: {f['human_check'].get('status')}" if f.get("human_check") else ""))
        lines.append("")
    return "\n".join(lines)


def table_dimensions(protocol: dict | None) -> list[str]:
    """Table 1's charted columns: the protocol's categorical map dimensions in tree order (at most four)."""
    m = (protocol or {}).get("map") or {}
    dims = {d["name"]: d for d in m.get("dimensions") or []}
    order = list(m.get("tree") or []) + [d for d in dims if d not in (m.get("tree") or [])]
    cols = [d for d in order if d in dims and dims[d].get("categories")][:4]
    return cols or ["setting", "construct", "design"]


def sources_table(store: Store, run_id: int, protocol: dict | None = None, label: str = "Table 1") -> str:
    """Table 1: every retained record with how it was done and what it contributed, from the store."""
    cols = table_dimensions(protocol)
    from .agents import kept_ids
    db = store.db
    kept = kept_ids(store, run_id)
    if not kept:
        return ""
    feat_ex = db.execute("SELECT extractor FROM study_features WHERE run_id = ? ORDER BY created_at DESC LIMIT 1",
                         (run_id,)).fetchone()
    feats = {}
    if feat_ex:
        for r in db.execute("SELECT work_id, dimension, canonical FROM study_features WHERE run_id = ? AND extractor = ?",
                            (run_id, feat_ex[0])):
            feats.setdefault(r["work_id"], {})[r["dimension"]] = r["canonical"].replace("_", " ")
    fcount = Counter(r[0] for r in db.execute(
        "SELECT work_id FROM findings WHERE run_id = ? AND status != 'superseded' AND extractor = (SELECT extractor "
        "FROM findings WHERE run_id = ? ORDER BY created_at DESC LIMIT 1)", (run_id, run_id)))
    ft = {r["work_id"]: r["status"] for r in db.execute(
        f"SELECT work_id, status FROM fulltexts WHERE work_id IN ({','.join('?' * len(kept))})", tuple(kept))}
    values = {}
    if feat_ex:
        for r in db.execute("SELECT work_id, dimension, value FROM study_features WHERE run_id = ? AND extractor = ? "
                            "AND dimension = 'population'", (run_id, feat_ex[0])):
            values[r["work_id"]] = r["value"]
    rows = []
    labels = tables.cite_map(store, run_id)
    for w in db.execute(f"SELECT * FROM works WHERE work_id IN ({','.join('?' * len(kept))})", tuple(kept)):
        f = feats.get(w["work_id"], {})
        blank = "not charted" if not f else "not reported"
        has_abs = len((w["abstract"] or "").strip()) > 50
        text = "not charted" if not f else "full text" if ft.get(w["work_id"]) == "parsed" else             "abstract" if has_abs else "title only"
        pop = values.get(w["work_id"], blank)
        rows.append((labels.get(w["work_id"]) or writer.cite_label(w["authors"], w["year"], w["title"]),
                     tables.kind_of(dict(w)),
                     *[f.get(c, blank) for c in cols],
                     _short("not reported" if pop.lower() in tables.NOT_REPORTED else pop, 1000), text,
                     fcount.get(w["work_id"], 0)))
    rows.sort()
    names = [c.replace("_", " ") for c in cols]
    lines = [f"**{label}.** All retained records: publication type (from the bibliographic source), "
             + ", ".join(names) + " and population as charted by the extraction agent, the text it worked from, and the "
             "number of findings extracted.", "",
             "| Source | Type | " + " | ".join(n.capitalize() for n in names) + " | Population | Charted from | Findings |",
             "| --- | --- | " + " | ".join("---" for _ in cols) + " | --- | --- | ---: |"]
    lines += ["| " + " | ".join(str(x) for x in r) + " |" for r in rows]
    return "\n".join(lines)


def _short(text: str, n: int) -> str:
    text = " ".join(str(text).split()).replace("|", "/")
    return text if len(text) <= n else text[: n - 1] + "…"


def flow(store: Store, run_id: int, elig: list[dict] | None = None, labels: dict | None = None) -> str:
    """Figure 1: the flow of records in the PRISMA-ScR stages, with the reasons for exclusion at each, from the store."""
    from .agents import kept_ids, protocol_exclusions, screened_ids, sheet_ids
    db = store.db
    labels = labels or {}
    from .update import added
    srcs = db.execute("SELECT source, fetched FROM run_sources WHERE run_id = ? AND source != 'citation_search' "
                      "AND source NOT LIKE '%@%' ORDER BY source", (run_id,)).fetchall()
    updates = db.execute("SELECT source, fetched FROM run_sources WHERE run_id = ? AND source LIKE '%@%'",
                         (run_id,)).fetchall()
    total = sum(s["fetched"] for s in srcs)
    pool = db.execute("SELECT count(DISTINCT work_id) FROM retrievals WHERE run_id = ? AND source != "
                      "'citation_search' AND source NOT LIKE '%@%'", (run_id,)).fetchone()[0]
    mined = {r[0] for r in db.execute("SELECT DISTINCT work_id FROM retrievals WHERE run_id = ? AND source = "
                                      "'citation_search'", (run_id,))}
    late = mined - {r[0] for r in db.execute("SELECT DISTINCT work_id FROM retrievals WHERE run_id = ? AND "
                                              "source != 'citation_search' AND source NOT LIKE '%@%'", (run_id,))}
    upd = set(added(store, run_id)) - late
    on_sheet = sheet_ids(store, run_id)
    sheet = screened_ids(store, run_id)         # a record left on the sheet by a recorded decision is not screened
    kept = kept_ids(store, run_id)
    with_f = {r["work_id"] for r in tables.finding_rows(store, run_id)}
    passed = kept_ids(store, run_id, "title_abstract")
    dropped = set(protocol_exclusions(store, run_id)) & set(passed)
    words = lambda c: ", ".join(f"{k.replace('_', ' ')} {n}" for k, n in c.most_common())  # noqa: E731
    # reasons at title and abstract: the criteria-scoring agent's, for the records excluded
    excluded = set(sheet) - set(passed)
    scorer = tables._scorer(store, run_id)
    ta = Counter(r["reason"] or "other" for r in db.execute(
        "SELECT work_id, reason FROM screening WHERE run_id = ? AND screener = ? AND stage != 'full_text'",
        (run_id, scorer)) if r["work_id"] in excluded)
    # reasons at the eligibility stage: the reason most often given for each excluded record
    out = set(passed) - set(kept) - dropped
    given: dict[int, list[str]] = {}
    for r in db.execute("SELECT work_id, decision, reason FROM screening WHERE run_id = ? AND stage = 'full_text'",
                        (run_id,)):
        if r["work_id"] in out and r["decision"] == "exclude":
            given.setdefault(r["work_id"], []).append(r["reason"] or "other")
    el = Counter(Counter(v).most_common(1)[0][0] for v in given.values())
    kept_set = set(kept)
    meta = tables.works(store, kept)
    text = Counter("full text" if ft_status(store, w) == "parsed" else
                   "abstract" if len((meta[w].get("abstract") or "").strip()) > 50 else "title only" for w in kept)
    rows = [("Identification", f"{total} records from databases ({', '.join(f'{source_name(s[0])} {s[1]}' for s in srcs)})",
             f"{total - pool} duplicates removed"),
            ("Identification", f"{pool} unique records" + (f"; {len(late)} more from citation searching" if mined else ""),
             "–")]
    from .update import supplement_of
    upd_day = added(store, run_id)
    plain = [s for s in updates if not supplement_of(s["source"])]
    if plain:
        days = sorted({s["source"].split("@", 1)[1] for s in plain})
        n = sum(1 for w, d in upd_day.items() if "+" not in d and w not in late)
        rows.append(("Identification (update searches)", f"{sum(s['fetched'] for s in plain)} records from the same "
                     f"search repeated on {', '.join(days)}", f"all but {n} already in the review"))
    for sid in sorted({supplement_of(s["source"]) for s in updates} - {None}):
        mine = [s for s in updates if supplement_of(s["source"]) == sid]
        day = mine[0]["source"].split("@", 1)[1].split("+", 1)[0]
        n = sum(1 for w, d in upd_day.items() if d.endswith("+" + sid) and w not in late)
        rows.append(("Identification (supplementary search)", f"{sum(s['fetched'] for s in mine)} records from a "
                     f"supplementary search on {day} ({sid})", f"all but {n} already in the review"))
    if len(on_sheet) < pool + len(late) + len(upd):
        rows.append(("Screening", f"{pool + len(late) + len(upd) - len(on_sheet)} records outside the random screening "
                     "sample", "not screened"))
    if len(sheet) < len(on_sheet):
        left = set(on_sheet) - set(sheet)
        rows.append(("Screening", f"{len(left)} records" + (" from citation searching" if left <= late else "")
                     + " left unscreened by a decision recorded in the protocol", "not screened"))
    rows.append(("Screening (title and abstract)", f"{len(sheet)} records screened",
                 f"{len(sheet) - len(passed)} excluded ({words(ta)})" if ta else f"{len(sheet) - len(passed)} excluded"))
    if len(passed) - len(dropped) != len(kept) or dropped:
        rows.append(("Eligibility", f"{len(passed)} records assessed against stricter criteria",
                     f"{len(out)} excluded ({words(el)})" if el else f"{len(out)} excluded"))
    if dropped:
        rows.append(("Eligibility", f"{len(passed) - len(out)} records kept",
                     f"{len(dropped)} excluded afterwards by decisions recorded in the protocol"
                     + (f" ({labels['exclusions']})" if labels.get("exclusions") else "")))
    rows.append(("Included", f"{len(kept)} records retained for charting ("
                 + ", ".join(f"{n} from {k}" for k, n in text.most_common()) + ")", "–"))
    if elig:
        core = [e for e in elig if e["core"]]
        potential = [e for e in elig if e.get("potential")]
        rows.append(("Included", f"{len(core)} core studies, meeting every inclusion criterion"
                     + (" in their full text" if potential else "")
                     + f" ({labels.get('eligibility', 'Table S2')}); {sum(1 for e in core if e['findings'])} of them "
                     "contributed findings", f"{len(elig) - len(core)} other retained records, reported as a separate set"
                     + (f", among them {len(potential)} potential core studies that meet every criterion on their "
                        "abstract and wait for their full text" if potential else "")))
    rows.append(("Included", f"{len(with_f & kept_set)} records contributed at least one finding",
                 f"{len(kept_set - with_f)} yielded none ({labels.get('without', 'Table S4')})"))
    lines = ["**Figure 1.** PRISMA-ScR flow diagram: the flow of records through the stages, with "
             "the number excluded and the reasons "
             "at each stage (at title and abstract, the reason recorded by the criteria-scoring agent; at the "
             "eligibility stage, the reason the agents gave most often for each record).", "",
             "| Stage | Records | Excluded or set aside |", "| --- | --- | --- |"]
    lines += [f"| {a} | {b} | {c} |" for a, b, c in rows]
    return "\n".join(lines)


def method_section(facts: list[dict], protocol: dict) -> str:
    p = protocol.get("paper") or {}
    out = [f"This {p.get('kind', 'review')} followed the stages of Arksey and O'Malley (2005). It is reported "
           f"following PRISMA-ScR (Tricco et al., 2018); the completed checklist is "
           f"{tables.table_labels(protocol)['prisma']}"
           + (f"; the protocol was registered ({p['registration']})." if p.get("registration") else
              ", and the protocol was not registered.")]
    groups = []
    for f in facts:
        if not groups or groups[-1][0] != f["group"]:
            groups.append((f["group"], []))
        groups[-1][1].append(f["text"])
    out += [f"### {head}\n\n" + " ".join(texts) for head, texts in groups]
    return "\n\n".join(out)


# Inputs for the agents

def background(protocol: dict) -> list[dict]:
    return [{"id": b["id"], "cite": b["cite"], "claim": " ".join(b["claim"].split()), "checked": b.get("checked", "")}
            for b in protocol.get("background") or []]


def paper_input(store: Store, run_id: int, protocol: dict) -> dict:
    tables.apply_fixes(store, protocol)
    data = writer.build_input(store, run_id, protocol)
    # Notes agents made while screening or charting are mostly about records and methods; the writer gets the
    # insights from the findings stage on, and any a person has reviewed.
    stage = dict(store.db.execute("SELECT insight_id, stage FROM insights"))
    data["insights"] = [i for i in data["insights"]
                        if i["status"] != "noted" or stage.get(i["id"]) not in ("screen", "eligibility", "extract")]
    facts = method_facts(store, run_id, protocol)
    items = table_items(store, run_id, protocol)
    for w, d in study_details(store, run_id, protocol).items():
        data["studies"].setdefault(str(w), {}).update(d)
    human = checks.for_writer(store, [f["id"] for f in data["findings"]])
    for f in data["findings"]:
        if f["id"] in human:
            f["human_check"] = human[f["id"]]
    data.update({"paper": protocol.get("paper") or {}, "title": protocol.get("title", ""),
                 "method_facts": facts, "method_section": method_section(facts, protocol),
                 "tables": items,
                 "background": background(protocol)})
    if protocol.get("aim"):
        # A focused paper: only the findings that bear on the aim, and the facts that say how they were chosen.
        # (The aim may be one of several: write_task passes the protocol with the chosen aim as its [aim].)
        from . import aim
        if protocol["aim"].get("kind") == "strong":     # the estimates read from the strongest studies
            from . import strong
            data["findings"] = strong.writer_findings(store, run_id, protocol)
            keep = {f["id"] for f in data["findings"]}
        else:
            keep = {r["finding_id"] for r in aim.aim_rows(store, run_id, protocol)}
            data["findings"] = [f for f in data["findings"] if f["id"] in keep]
        works = {str(f["work_id"]) for f in data["findings"]}
        data["studies"] = {w: s for w, s in data["studies"].items() if w in works}
        data["insights"] = [i for i in data["insights"] if not i.get("findings") or set(i["findings"]) & keep]
        data["method_facts"] = facts + aim.facts(store, run_id, protocol)
        data["aim"] = protocol["aim"]
    return data


def _aim_parts(protocol: dict) -> list[str]:
    a = protocol["aim"]
    words = a.get("words", "4,000 to 5,500")
    if a.get("kind") == "proposal":
        return [
            f"This paper proposes: {' '.join(str(a.get('question', '')).split())} Its readers: "
            f"{a.get('audience', 'people who must decide')}. It is an evidence-based design proposal, not a review: it "
            "states what is wrong with the current design, proposes a restructured model, grounds every component in the "
            "evidence and theory given, predicts what each component does to performance and to well-being, and says "
            "how to test the whole. The findings in batch_01.json are all the findings of the core studies; the tables "
            "give the design choices (T1), the outcomes (T2), the comparator review of assessment stakes (T3) and the "
            "patterns across studies (T4).",
            "",
            "Parts to write: `title` (names the proposed model and what it is for); `abstract` (200-250 words"
            + ("; five paragraphs with `heading` Background, Objective, Methods, Results and Conclusions; in Results "
               "give the evidence the redesign answers and the model's components; in Conclusions what to do and how to "
               "test it" if (protocol.get("paper") or {}).get("structured_abstract") else "") + "); `keywords` (4-6); "
            f"`sections` named Introduction, Method, Evidence, Proposal, Evaluation, Discussion and Conclusion, {words} "
            "words in all:",
            "- Introduction (400-600 words): what programmatic assessment promises, what the evidence shows it does "
            "and does not do, and the aim: a restructured design that raises performance and protects well-being.",
            "- Method (300-500 words): the evidence base (method facts, including what no person checked, A4) and how "
            "the proposal was derived: each component from a problem in the evidence, a mechanism from theory "
            "(background sources) and, where it exists, comparative evidence (T3).",
            "- Evidence (1,200-1,800 words), blocks with a `heading` per problem the redesign must solve, the problem "
            "in the block's first sentence with its numbers: performance is not shown to rise; low-stakes data points "
            "are felt as high-stakes; stress and well-being are divided and barely measured; workload is a cost; what "
            "learners value (narrative feedback, mentoring); what lowering stakes did elsewhere (T3). Every sentence "
            "cites findings or tables.",
            "- Proposal (1,600-2,300 words): the restructured model, 5 to 8 components, one block with a `heading` "
            "each: what changes from current practice, the mechanism (theory), the evidence and how strong it is, the "
            "expected effect on performance and on well-being as a directional, testable prediction, and the trade-off "
            "or risk. Address the tension between low stakes and effort explicitly. Then summarise the model in "
            "`model_table` (below), which the paper refers to as Table 5.",
            "- Evaluation (500-800 words): how a university could test the model: the design (e.g. a stepped-wedge or "
            "cluster comparison across cohorts or programmes), primary outcomes for performance and for well-being, "
            "process measures (perceived stakes, feedback use, workload), a time frame, and the results that would "
            "count as success or failure. Name measures only from the background sources.",
            "- Discussion (700-1,000 words): risks and what could fail, how the model relates to the consensus "
            "principles and earlier reviews, the limits of the evidence it rests on (in one place), and what the author "
            "would need to run the evaluation.",
            "- Conclusion (150-250 words): the proposal in plain words.",
            "",
            "`model_table`: {\"caption\": \"...\", \"rows\": [{\"component\": \"short name\", \"change\": \"what changes\", "
            "\"why\": \"mechanism and evidence in one sentence\", \"performance\": \"expected effect\", \"wellbeing\": "
            "\"expected effect\", \"strength\": \"how strong the evidence is\", \"test\": \"how to test it\", \"cites\": [finding "
            "ids], \"tables\": [\"T1\"], \"background\": [\"B9\"]}]}: 4 to 9 rows, one per component; each row is checked "
            "like a sentence (every number in its cells must be in what it cites, and it must cite something). Optional "
            "cells, shown as columns when used: \"principle\" (how the component relates to the consensus principles: "
            "restated, sharpened, new or a departure), \"adopt\" (adopt now, or only within the evaluation) and "
            "\"resources\" (a rough estimate); a row whose cells give the authors' own estimates sets \"plan\": true.",
            "",
            "Plan sentences: in Proposal and Evaluation, a sentence that states the authors' own design parameters "
            "(numbers of programmes, cohorts, steps or participants, a margin, an assumed effect size, a time frame) sets "
            "`plan` to true; its numbers are not checked against sources, so it must contain no number about the evidence, "
            "and it should cite what motivates the choice. Keep plan sentences to the parameters themselves.",
            "",
            "Tone: a confident, concrete proposal (we propose, programmes should) that never claims more than the "
            "evidence: each component carries its strength, and its predicted effects are what the evaluation would "
            "test.",
        ]
    if a.get("kind") == "focus" and a.get("layout") == "policy":
        return _policy_parts(protocol)
    if a.get("kind") in ("focus", "strong") and a.get("layout") == "essay":
        return _essay_parts(protocol)
    if a.get("kind") == "focus":
        rules = ", ".join(str(r).replace("_", " ") for r in (protocol.get("focus") or {}).get("rules") or [])
        sens = bool((protocol.get("focus") or {}).get("sensitivity"))
        return [
            f"This paper tests one question: {' '.join(str(a.get('question', '')).split())} Its readers: "
            f"{a.get('audience', 'researchers')}. The findings in batch_01.json are the verified findings of the "
            f"focused evidence on {rules} (method facts A0-A5); Table 1 (T1) gives, per rule and context, the studies' "
            "directions with a verdict (strong, moderate, inconsistent, limited) and the test of ancestral against "
            "modern contexts, both fixed in the protocol before the verified codes were counted (A3). Call them "
            "pre-specified, never pre-registered: the protocol was not publicly registered, and descriptive tables of "
            "the unverified codes had been seen."
            + (" Table 2 (T2) repeats each test under other choices of selection, counting and measurement; these "
               "sensitivity analyses were added after peer review and are reported as such (its caption says which "
               "came when)." if sens else "")
            + " Answer first: the paper's result is the pattern in T1"
            + (", and how far it survives T2." if sens else "."),
            "",
            "Parts to write: `title` (states the main result, not 'a review'); `abstract` (200-250 words"
            + ("; five paragraphs with `heading` Background, Objective, Methods, Results and Conclusions" if
               (protocol.get("paper") or {}).get("structured_abstract") else "") + "; the Results paragraph gives each "
            "rule's test with its numbers and p value; the Conclusions paragraph states what the pattern means in plain "
            "words); `keywords` (4-6); `sections` named Introduction, Method, Results, Discussion and Conclusion, "
            f"{a.get('words', '4,500 to 6,000')} words in all:",
            "- Introduction (500-800 words): the puzzle (fertility fell wherever prosperity, schooling and life "
            "expectancy rose, and economic and cultural accounts leave open why some changes move fertility and others "
            "do not), the evolutionary-mismatch idea (decision rules that read ancestral cues, fed modern inputs), the "
            "rules tested here and the cue each reads, and the prediction: the same cue should move fertility one way "
            "in ancestral-like settings and differently in modern ones. Say what each possible result would mean, "
            "including the results that would count against mismatch, and name the rival adaptive account (the best "
            "number of children simply differs between environments), which predicts a changed cue-fertility link too. "
            "Cite only the background sources and findings.",
            "- Method (400-600 words): how the evidence was found, selected, coded, verified and counted, citing the "
            "method facts (A0-A5 and the generated ones); state how the rules and test were fixed (A3) and what no "
            "person checked (A5)" + ("; say that the sensitivity analyses (T2) came after peer review" if sens else "")
            + ". The full method is in the supplement.",
            "- Results (2,000-2,800 words): first the overview (T1: how many studies per rule, and the three tests in "
            "one paragraph" + ("; then which results hold under every analysis in T2 and which depend on one choice"
                               if sens else "") + "); then one block with a `heading` per rule, the test result in "
            "its first sentence: the "
            "verdict per context with its numbers, the strongest studies by name (natural experiments, long panels, "
            "historical genealogies), what the direction means (more of the cue: more education or skills, higher "
            "status or wealth, more kin nearby or more help), for whom and under which conditions (subgroups, sexes, "
            "kin types), and the studies that go against the pattern. Then a block on the social constructs measured "
            "alongside (gender equity and division of labour, family policy): what the studies report, without "
            "claiming moderation the data do not test.",
            "- Discussion (1,000-1,500 words): begin with 'Main findings': numbered statements, one per verdict that is "
            "strong or moderate and one per test, each saying what the evidence shows and how strong it is. Then what "
            "the pattern means for the mismatch account against the adaptive account (which results separate them, "
            "for example evidence on whether fewer children now costs descendants, and which do not), rival "
            "explanations the data cannot exclude (economic and cultural accounts; "
            "publication and framing biases), 'Limitations' in one place (vote counts, not pooled effects; studies "
            "treated as independent; most ancestral evidence observational; codes by language models, verified "
            "against passages but not by a person; studies read from abstracts), and the research that would decide it."
            + (f" Before the Limitations, a block headed '{a['implications'].get('heading', 'Practical implications')}' "
               f"({a['implications'].get('words', '400-700')} words): "
               + " ".join(str(a["implications"].get("for", "")).split())
               + " Each implication follows from a stated result and names the evidence it rests on (the design, the "
               "number of studies, the verdict or test); an implication resting on causal designs (natural experiments, "
               "reforms) may say what a policy or institution would change; one resting on associations says what "
               "should be tried and evaluated, or monitored, not what will work. Name who would act (governments, "
               "employers, universities) and what they would do, concretely." if isinstance(a.get("implications"), dict)
               else ""),
            "- Conclusion (150-250 words): the result in plain words"
            + (", and its main practical implication." if isinstance(a.get("implications"), dict) else "."),
            "",
            "Tone: strong where the evidence is strong. A strong verdict or a significant test is stated as a finding "
            "(\"in historical populations, higher status went with more children: 18 of 24 studies\"), not as a "
            "possibility; a moderate verdict as consistent evidence; inconsistent and limited cells as exactly that. "
            "Never claim more than T1 and the cited findings show, and never less: no hedging a significant result "
            "into a hypothesis. Uncertainty belongs in the strength clause and the Limitations, not in every sentence.",
        ]
    if a.get("kind") == "effectiveness":
        return [
            f"This paper answers one question: {' '.join(str(a.get('question', '')).split())} Its readers: "
            f"{a.get('audience', 'people who must decide')}. The findings in batch_01.json are all the findings of the "
            "core studies. Answer the question first and plainly; organise by what readers want to know, not by study.",
            "",
            "Parts to write: `title` (states the main answer, not 'a scoping review'); `abstract` (200-250 words"
            + ("; five paragraphs with `heading` Background, Objective, Methods, Results and Conclusions" if
               (protocol.get("paper") or {}).get("structured_abstract") else "") + "; the Results paragraph gives the "
            "four or five main findings with their numbers; the Conclusions paragraph answers the question in plain "
            "words); `keywords` (4-6); `sections` named Introduction, Method, Results, Discussion and Conclusion, "
            f"{words} words in all:",
            "- Introduction (400-600 words): what programmatic assessment promises, why it matters to the readers, what "
            "earlier reviews found, and the question.",
            "- Method (400-600 words): how the evidence was found, selected, coded and checked, citing method facts; "
            "say what no person checked (A4) in one or two sentences, and that the full method is in the supplement.",
            "- Results (2,200-2,900 words), one block with a `heading` per question, the answer in the block's first "
            "sentence: does it change performance and competence; learning behaviour and motivation; stress and "
            "well-being; how learners and teachers experience it (above all the stakes); the quality of its decisions; "
            "its costs and workload; which design choices matter (T2); what the review of assessment stakes adds (T3); "
            "what the patterns across studies show (T4). Use T1 throughout"
            + ("; the comparative evidence study by study is in T5" if tables.table_labels(protocol).get("model") ==
               "Table 6" else "") + ".",
            "- Discussion (1,000-1,400 words): begin with a block headed 'Main findings': five to seven numbered "
            "statements, each one or two sentences that say what the evidence shows and how strong it is. Then 'What "
            "this means for programmes' (concrete), 'Limitations' (at most 350 words, in one place) and 'What research "
            "is needed'. The recommendations may also be set out, graded, in `model_table` (shown after the Discussion "
            f"as {tables.table_labels(protocol).get('model', 'Table 5')}): {{\"caption\": \"...\", \"rows\": "
            "[{\"component\": \"the recommendation\", \"change\": \"what it means in practice\", \"why\": \"the reason in "
            "one sentence\", \"performance\": \"the effect to expect\", \"wellbeing\": \"the effect to expect\", "
            "\"strength\": \"the evidence and its strength, with the number of independent studies\", \"test\": \"how a "
            "programme could check it\", \"adopt\": \"a firm recommendation, or a design hypothesis\", \"cites\": [finding "
            "ids], \"tables\": [\"T1\"], \"background\": [\"B9\"]}]}: 3 to 9 rows; each row is checked like a sentence "
            "(every number in its cells must be in what it cites, and it must cite something).",
            "- Conclusion (150-250 words): the answer in plain words.",
            "",
            "Tone: state what the evidence shows, and calibrate each claim with its strength in a clause (tested in "
            "comparisons; consistently reported across N studies; a single study). A finding reported consistently "
            "across many studies is a finding about what learners and teachers report: say so, do not turn it into a "
            "hypothesis. Uncertainty belongs in the strength clause and the Limitations, not in every sentence. Where "
            "the evidence contradicts the approach's promise, say that plainly too.",
        ]
    return [
        f"This is a focused paper with one aim: {' '.join(str(a.get('question', '')).split())} Its readers: "
        f"{a.get('audience', 'people who must decide')}. The findings in batch_01.json are already only those that "
        f"connect a {str(a.get('by', 'design_choice')).replace('_', ' ')} to the aim's outcomes (fields "
        f"`{a.get('by')}`, `{a.get('link')}`: tested, associated or reported, and `{a.get('domain')}`). Answer the aim; "
        "do not survey the field, and do not describe studies for their own sake.",
        "",
        "Parts to write: `title` (says the answer's subject, not 'a scoping review'); `abstract` (150-250 words"
        + ("; five paragraphs with `heading` Background, Objective, Methods, Results and Conclusions" if
           (protocol.get("paper") or {}).get("structured_abstract") else "") + "); `keywords` (4-6); `sections` "
        f"named Introduction, Method, Results, Discussion and Conclusion, {words} words in all:",
        "- Introduction (400-600 words): why these choices matter to the readers, what is known (background sources), "
        "the aim as a question, and why a comparator from another literature helps.",
        "- Method (400-600 words): a concise account of how the evidence was found, selected, coded and checked, "
        "citing method facts (`method` ids); it must cover the selection for the aim (A1), the comparator (A2), the "
        "patterns (A3) and what was and was not checked by a person (A4). Say that the full method and the scoping "
        "tables are in the supplement. Do not repeat search strings or criteria verbatim.",
        "- Results (1,800-2,500 words): first a short overview citing T1; then one paragraph block per design choice "
        "with a `heading`, the best-supported first: what it does to learning and to well-being, how strong the "
        "evidence is (tested, associated or reported; number of studies), for whom, and where findings conflict; then "
        "a block on the comparator evidence (T2) and one on the patterns across studies (T3), each saying plainly what "
        "held, what failed and what could not be judged.",
        "- Discussion (1,000-1,400 words): begin with a block headed 'Recommendations': each recommendation one "
        "paragraph, ranked by how well it is supported, saying what to do, the evidence strength, the main "
        "uncertainty and what would settle it. Then what the comparator adds, the limitations (the evidence's and "
        "this AI-directed review's, including what no person checked), and implications for research.",
        "- Conclusion (150-250 words): the answer to the aim in plain words.",
    ]


def _policy_parts(protocol: dict) -> list[str]:
    a = protocol["aim"]
    return [
        f"This paper is a short policy review: {' '.join(str(a.get('question', '')).split())} Its readers: "
        f"{a.get('audience', 'people who must decide')}. The findings in batch_01.json are the verified findings of "
        "the focused evidence (method facts A0-A5); Table 1 (T1) gives, per rule and context, the studies' directions "
        "with a verdict and the test of ancestral-like against modern contexts. The other tables are in the "
        "supplement; cite them (S ids) only where a lesson needs a number they alone give. It is not a data analysis: "
        "the evolutionary account is the lens through which the evidence is read, and the paper's value is what "
        "readers and institutions can do with it. Cut the literature to what the lens needs.",
        "",
        "Lead with the majority. State each result as what most studies show, in its own direction (\"more schooling "
        "went with fewer or later births in most modern studies\"), and give the exceptions after it, never the "
        "exceptions first. The test between contexts comes after the majority, in one sentence.",
        "",
        "Parts to write: `title` (states the practical message, not 'a review'); `abstract` (150-200 words, one "
        "paragraph, plain words: the problem, the lens, the three lessons with their main numbers, what to do); "
        "`keywords` (4-6); `sections` named Key points, Introduction, Lessons, Recommendations, Limits, Method and "
        f"Conclusion, {a.get('words', '3,500 to 4,500')} words in all:",
        "- Key points (4-5 paragraphs, each one sentence a non-specialist can grasp, with its number of studies): "
        "the lessons and what follows from them.",
        "- Introduction (400-600 words): why low fertility matters to societies, why familiar policies often move it "
        "little, and the lens: decision rules that evolved to read cues of readiness, status and help, now fed modern "
        "inputs; what the lens lets one see that a list of correlates does not. Cite only the background sources; "
        "no survey of the literature.",
        "- Lessons (1,500-2,000 words): one block with a `heading` per rule (readiness, status, kin help), the "
        "heading a lesson in plain words. Each block: the majority result first with its numbers (T1), the two or "
        "three strongest studies by name and design (natural experiments, reforms, long panels, historical "
        "genealogies), what the lens explains about the result, for whom it holds (sexes, kin types, timing against "
        "number of children), and the exceptions. Figure 1 and Table 1 are printed after this section.",
        "- Recommendations (500-800 words): one paragraph per recommendation, ranked by the strength of its evidence: "
        "what to do, who acts (governments, employers, universities, families), why (the lesson and the rule it works "
        "with) and what to expect. Then set them out in `model_table`, printed after this section as Table 2: "
        "{\"caption\": \"...\", \"rows\": [{\"component\": \"the action\", \"change\": \"who acts and how\", \"why\": "
        "\"the lesson and the rule in one sentence\", \"performance\": \"the expected effect on births (timing or "
        "number)\", \"wellbeing\": \"other effects to watch (equity, careers, well-being)\", \"strength\": \"the "
        "evidence with its number of studies and designs\", \"test\": \"how to evaluate it\", \"adopt\": \"act now, or "
        "pilot and evaluate\", \"cites\": [finding ids], \"tables\": [\"T1\"], \"background\": [\"B9\"]}]}: 3 to 7 rows; "
        "each row is checked like a sentence (every number in its cells must be in what it cites, and it must cite "
        "something). A recommendation resting on causal designs may say what a policy would change; one resting on "
        "associations says what to pilot and evaluate.",
        "- Limits (300-450 words): what the evidence does not show, in one place: mostly associations; vote counts of "
        "studies, not pooled effects; studies treated as independent; ancestral evidence mostly observational; "
        "economic and cultural accounts predict much of the same; codes by language models, checked against passages "
        "and in part by a person (A5); studies read from abstracts.",
        "- Method (200-300 words): how the evidence was found, selected, coded, verified and counted, citing the "
        "method facts; the full method and tables are in the supplement.",
        "- Conclusion (100-200 words): the message in plain words.",
        "",
        "Tone: plain and direct, for readers outside the field; strong where the evidence is strong, with the "
        "strength in a clause, not in hedges in every sentence. No jargon without a gloss in the same sentence.",
    ]


def _essay_parts(protocol: dict) -> list[str]:
    a = protocol["aim"]
    strong = a.get("kind") == "strong"      # the strongest studies read for their effects, not counts of directions
    return [
        f"This paper is an argued essay built on evidence: {' '.join(str(a.get('question', '')).split())} Its "
        f"readers: {a.get('audience', 'people who must decide')}. "
        + ("The findings in batch_01.json are the estimates read from the strongest studies, selected by design under "
           "a rule fixed before any effect was read (method facts A0-A5): each states what changed, by how much and "
           "for whom, with the passage it rests on. Table 1 (T1) lists those studies by rule, with what changed, the "
           "design and the main estimates; " + tables.table_labels(protocol).get("appraisals", "a supplementary table")
           + " in the supplement gives every candidate's appraisal. The "
           "paper rests on these studies alone: no counts of directions across studies and no comparison of "
           "ancestral-like with modern contexts. Effects are reported in the studies' own units (years of delay, "
           "children per woman, percentage points), never converted or pooled, and the paper says where they differ "
           "in kind (timing against number of children, intentions against births)."
           if strong else
           "The findings in batch_01.json are the verified findings of the focused evidence (method facts A0-A5); "
           "Table 1 (T1) gives, per rule and context, the studies' directions with a verdict and the test of "
           "ancestral-like against modern contexts. The other tables are in the supplement; cite them (S ids) only "
           "where a claim needs a number they alone give."),
        "",
        "It must read as one argument, not as a data analysis or an inventory of studies. The evolutionary account is "
        "the lens through which the evidence is read; the paper's value is what readers and institutions can see and "
        "do because of it. Its model is an essay that opens on a concrete case, names the few concepts that carry its "
        "thesis, meets the strongest objection at full strength and turns it, tells a handful of studies as stories "
        "of what happened to whom, and ends where it began, with the case now understood.",
        "",
        "Parts to write: `title` (a claim a reader remembers, optionally with a subtitle after a colon; not 'a "
        "review'); `abstract` (150-220 words, one paragraph: the thesis first, then the lens, the three results with "
        "their main numbers, what follows, and what would refute it); `keywords` (4-6); `sections` named Diagnosis, "
        "Argument, What follows, What would show this wrong, Conclusion and Method, in that order, "
        f"{a.get('words', '4,000 to 5,500')} words in all. Give every section except Method a `title` that states "
        "its claim in a few words (\"Diagnosis: ...\"); the Method is printed as an appendix.",
        "- Diagnosis (500-800 words): open with one concrete case from the findings, told in a few plain sentences "
        "(a reform, a population, what changed and what followed); then why it matters now (the background on low "
        "fertility and on the small effects of familiar policies); then the thesis in two or three sentences and the "
        "one to three concepts that carry it, each defined in one sentence when first used (coin a short name where "
        "the literature has none, and use it the same way to the end); close with the route of the argument.",
        "- Argument (1,800-2,400 words): one block with a `heading` per rule (readiness, status, kin help), the "
        "heading a claim. Each block: what the rule reads and what modern life feeds it, in the essay's concepts; "
        + ("what the strongest studies show, with their effects in their own units (T1 and the estimates): how large, "
           "whether births came later, fewer or both, and how far each study can see (its horizon); where the studies "
           "agree and where they differ, and why (design, setting, who is affected); "
           if strong else "the majority result once, with its numbers (T1); ")
        + "two or three studies told as what happened to whom (place, "
        "time, design, what changed); for whom it holds; the strongest objection at full strength, including the "
        "rival readings in the background sources (the economics of child quality, the second demographic "
        "transition, gender equity, the diffusion of ideas), and the answer, or a plain admission where the lens "
        "adds nothing; the exceptions; and a short closing line that says what the block has established.",
        "- What follows (600-900 words): the recommendations, ranked by the strength of their evidence, one "
        "paragraph each: what to do, who acts (governments, employers, universities, families), why (the concept and "
        "the result it rests on) and what to expect. Then `model_table`, printed after this section as Table 2: "
        "{\"caption\": \"...\", \"rows\": [{\"component\": \"the action\", \"change\": \"who acts and how\", \"why\": "
        "\"the result and the rule in one sentence\", \"performance\": \"the expected effect on births (timing or "
        "number)\", \"wellbeing\": \"other effects to watch (equity, careers, well-being)\", \"strength\": \"the "
        "evidence with its number of studies and designs\", \"test\": \"how to evaluate it\", \"adopt\": \"act now, or "
        "pilot and evaluate\", \"cites\": [finding ids], \"tables\": [\"T1\"], \"background\": [\"B9\"]}]}: 3 to 7 rows; "
        "each row is checked like a sentence (every number in its cells must be in what it cites, and it must cite "
        "something). A recommendation resting on causal designs may say what a policy would change; one resting on "
        "associations says what to pilot and evaluate.",
        "- What would show this wrong (350-550 words): the three or four claims the argument rests on, one paragraph "
        "each: where the evidence stands, what result would refute the claim, and the study that would settle it. "
        "Then, in one paragraph, what the evidence cannot show: "
        + ("a few dozen studies chosen by design, mostly reforms in modern societies, so the evolutionary reading is a "
           "lens and not a test; effects in different units, not pooled; reforms change more than one thing; published "
           "studies lean towards clear effects; designs appraised and effects read by language models, the passages "
           "checked in code but not by a person (A5)."
           if strong else
           "mostly associations; counts of studies, not pooled effects; ancestral evidence mostly observational; rival "
           "accounts predict much the same; codes by language models, checked against passages and in part by a "
           "person (A5)."),
        "- Conclusion (200-350 words): return to the opening case, now read through the argument, and end on one "
        "short sentence that carries the thesis.",
        "- Method (250-400 words, printed as an appendix with Figure 1 and Table 1 after it): how the evidence was "
        + ("found, how the candidates were appraised and the studies selected by the rule fixed before any effect was "
           "read, and how the effects were read and checked, citing the method facts; "
           if strong else "found, selected, coded, verified and counted, citing the method facts; ")
        + "that the review was AI-assisted, how, and what that limits; the full method and tables are in the "
        "supplement.",
        "",
        "Style, which the reviewers will judge:",
        ("- Be precise: every effect with its unit, its population and its horizon; say whether a result is about "
         "the timing of births or their number, and never let a delay stand for a loss of births or the reverse."
         if strong else
         "- Lead with the majority: state each result as what most studies show, in its own direction, and give the "
         "exceptions after it, never first."),
        "- Give a claim's numbers once, where the claim is made; do not repeat them in the following sentences. No "
        "p-values, odds ratios, confidence intervals or test statistics in the text: say in words what a test shows "
        "and cite the table. Not every sentence needs a number.",
        ("- Tell the studies well: the essay may name any study of Table 1 but tells about 15 to 25 of them with "
         "enough of their story (who, where, when, what changed) to be remembered; the others are cited where they "
         "agree or differ. Do not list studies in strings."
         if strong else
         "- Tell few studies, well: in all about 15 to 25 studies by name, each with enough of its story (who, where, "
         "when, what changed) to be remembered. Do not list studies in strings."),
        "- Short declarative sentences, one claim each, varied in length; paragraphs that build; no throat-clearing "
        "('it is important to note'), no talk about the paper or the review outside Diagnosis and Method.",
        "- Strong where the evidence is strong, with the strength in a clause, not in a hedge in every sentence; "
        "plain about what the evidence cannot show, in its place.",
        "- Signposts and a block's closing line may be framing sentences (`frame`: true) if they add no fact and no "
        "number; every other sentence cites what it rests on as usual. No invented scenes, no personal anecdotes and "
        "no quotation that is not a finding's verbatim passage.",
    ]


def paper_instructions(protocol: dict, revision: bool = False) -> str:
    p = protocol.get("paper") or {}
    lines = [
        f"# {'Revision' if revision else 'Manuscript'} task: {protocol.get('title', '')}",
        "",
        (f"Write a complete, focused paper (an evidence synthesis that answers one aim) for "
         f"{p.get('target', 'a peer-reviewed journal')}. Cite Figure 1 and "
         + (("Table 1" if protocol["aim"].get("layout") in ACTION_LAYOUTS else
             "Tables 1-3" if (protocol.get("focus") or {}).get("strata") else
             "Tables 1-2" if (protocol.get("focus") or {}).get("sensitivity") else "Table 1")
            if protocol["aim"].get("kind") == "focus" else "Tables 1-4")
         + " in the text; the supplementary tables (S...) are there for the record, and you need not read them."
         if protocol.get("aim") else
         f"Write a complete {p.get('kind', 'review')} manuscript for {p.get('target', 'a peer-reviewed journal')}.")
        if not revision else
        "Revise the manuscript in `paper` so that it answers every required change in `meta_review` and the "
        "concerns in `reviews` and `audit_flags`, and write a response to the reviewers.",
        "",
        "batch_01.json holds everything you may use: `findings` (each with its verbatim passage), `insights` "
        "(`noted` means not yet reviewed by a person), `background` (sources you may cite, each only for its "
        "`claim`), `method_facts` (what the review did" + ("; you write a short Method from them, and the full "
        "generated method in `method_section` goes to the supplement" if protocol.get("aim") else "; the Method section "
        "in `method_section` is generated from them and inserted between Introduction and Results") + "), "
        "`tables` (Figure 1 and the tables, ids F1, T1, ... "
        "in the paper and S1, ... in the supplement, generated from the store; you cannot change them, but you can cite "
        "them) and "
        "`studies` (how to cite each study, and its type, charted dimensions and population, the "
        "text it was charted from and its `set` in the eligibility table).",
        "",
        "findings.md lists the same findings one per line, grouped by their fields with core studies first: read it "
        "to survey the evidence, and check a finding's verbatim passage in batch_01.json before you rest a claim on "
        "it.",
        "",
        *(_aim_parts(protocol) if protocol.get("aim") else [
            "Parts to write: `title`; `abstract` (150-250 words" + ("; five paragraphs with `heading` Background, "
            "Objective, Methods, Results and Conclusions, the objective a full sentence" if p.get("structured_abstract")
            else ": background, objective, method, results, conclusions") + "); "
            "`keywords` (4-6); `sections` named Introduction (600-900 words: why the question matters, what is known, "
            "the gap, the objective), Results (1,200-1,800 words), Discussion (900-1,300 words: interpretation, "
            "relation to theory and background sources, limitations of the evidence and of this AI-assisted review, "
            "implications, future research) and Conclusion (150-250 words)."]),
        "",
        "Rules, all checked by the pipeline:",
        "- Every part is a list of paragraphs, each a list of sentences. Each sentence that states anything lists "
        "what it rests on: `cites` (finding ids), `background` (ids like B2), `method` (ids like M3) and/or "
        "`tables` (ids like T2). Results sentences must cite findings or tables. A study's details in `studies` "
        "(type, setting, design, population, charting source) may be stated when the sentence cites one of its "
        "findings or the table that shows them.",
        "- Every number in a sentence must appear in an item that sentence cites. No other numbers.",
        "- `insights` (ids) may organise the argument, but are not evidence: the sentence still cites what supports it.",
        "- A finding with `human_check` was checked by a person against its source. If its status is `corrected`, the "
        "`correction` is what the source says: state that, never the statement or passage where they differ.",
        "- Signposts that claim nothing set `frame` to true, cite nothing and contain no digits. Keep them few.",
        "- From a background source claim only what its `claim` says. No other literature, no outside knowledge, no "
        "author names or years in the text (the pipeline adds citations).",
        "- State evidence strength (number of studies, designs, abstract or full text). Present contradictions as "
        "contradictions.",
        *[f"- {' '.join(str(r).split())}" for r in p.get("rules") or []],
        "- The " + ("Method appendix" if (protocol.get("aim") or {}).get("layout") == "essay" else "Discussion")
        + " must say plainly that the review was AI-assisted, how, and what that limits.",
        "",
        insights.instructions(),
        "",
        "Output: one JSON object:",
        "",
        "```json",
        json.dumps({"title": "...", "abstract": [{"sentences": [{"text": "...", "cites": [1], "method": ["M2"]}]}],
                    "keywords": ["..."], "sections": [{"name": "Introduction", "paragraphs": [
                        {"heading": "optional", "sentences": [{"text": "...", "background": ["B2"]}]}]}],
                    **({"response": [{"change": "R1", "action": "done", "response": "...", "where": "Results"}]}
                       if revision else {}),
                    "insights": []}),
        "```",
    ]
    if revision:
        lines += ["", "`response` answers every required change in `meta_review` by its id: `action` is done, "
                  "partly or declined, with a reason; `where` says where the paper changed. A change that only the "
                  "human author can make (verifying claims, signing declarations, depositing materials) is "
                  "declined with that reason and what the author must do; never claim it was done.",
                  "", "Answer the latest `meta_review` (the last in the list); earlier ones are context."]
        notes = [" ".join(str(n).split()) for n in p.get("revision_notes") or []]
        if notes:
            lines += ["", "Facts the coordinating agent established after the last decision, from the records' texts "
                      "(context for what you may and may not say; cite the findings, tables and method facts as usual):",
                      *[f"- {n}" for n in notes]]
    return "\n".join(lines) + "\n"


# Checking a manuscript

def all_sentences(paper: dict):
    for p_i, p in enumerate(paper.get("abstract") or []):
        for s_i, s in enumerate(p.get("sentences") or []):
            yield "Abstract", p_i, s_i, s
    for sec in paper.get("sections") or []:
        for p_i, p in enumerate(sec.get("paragraphs") or []):
            for s_i, s in enumerate(p.get("sentences") or []):
                yield sec.get("name", "?"), p_i, s_i, s
    for i, row in enumerate((paper.get("model_table") or {}).get("rows") or []):
        # a row of a proposal's model table is checked as one sentence: its cells, and what it cites
        yield "Model table", i, 0, {"text": " | ".join(str(row.get(c) or "") for c in MODEL_CELLS + tuple(MODEL_OPTIONAL)
                                                   if row.get(c)),
                                    "plan": bool(row.get("plan")),
                                    **{k: row.get(k) or [] for k in ("cites", "tables", "background", "method")}}


def check_shape(paper, data) -> list[str]:
    if not isinstance(paper, dict) or not paper.get("title"):
        return ["the manuscript needs a title"]
    errs = []
    names = [s.get("name") for s in paper.get("sections") or []]
    proposal = ((data or {}).get("aim") or {}).get("kind") == "proposal"
    general = ((data or {}).get("aim") or {}).get("kind") == "effectiveness"
    layout = ((data or {}).get("aim") or {}).get("layout")
    policy = layout in ACTION_LAYOUTS
    rows = (paper.get("model_table") or {}).get("rows") or []
    if proposal or policy or (general and rows):     # a general paper's table of recommendations is optional
        if not (4 if proposal else 3) <= len(rows) <= 9:
            errs.append(f"the {'model' if proposal else 'recommendations'} table needs "
                        f"{4 if proposal else 3} to 9 rows, not {len(rows)}")
        for i, row in enumerate(rows, 1):
            missing = [c for c in MODEL_CELLS if not str(row.get(c) or "").strip()]
            if missing:
                errs.append(f"model table row {i}: needs {missing}")
            if not (row.get("cites") or row.get("tables") or row.get("background")):
                errs.append(f"model table row {i}: cites nothing")
    for need in (PROPOSAL_SECTIONS if proposal else ESSAY_SECTIONS if layout == "essay" else
                 POLICY_SECTIONS if policy else
                 SECTIONS + (("Method",) if (data or {}).get("aim") else ())):
        if need not in names:
            errs.append(f"section {need} is missing")
    if not paper.get("abstract"):
        errs.append("the abstract is missing")
    for sec, p_i, s_i, s in all_sentences(paper):
        if not isinstance(s, dict) or not (s.get("text") or "").strip():
            errs.append(f"{sec} paragraph {p_i + 1} sentence {s_i + 1}: needs text")
    return errs


def verify(paper: dict, data: dict, store: Store) -> dict:
    findings = {f["id"]: f for f in data["findings"]}
    bg = {b["id"]: b for b in data["background"]}
    facts = {f["id"]: f for f in data["method_facts"]}
    tabs = {t["id"]: t for t in data.get("tables") or []}
    given = {i["id"] for i in data["insights"]}
    dropped = {r["insight_id"] for r in store.db.execute("SELECT insight_id FROM insights WHERE status = 'dropped'")}
    results = []
    totals = {"sentences": 0, "verified": 0, "frame": 0, "flagged": 0, "words": 0}
    for sec, p_i, s_i, s in all_sentences(paper):
        text = s["text"].strip()
        cites = [int(c) for c in s.get("cites") or [] if str(c).lstrip("-").isdigit()]
        b_ids = [str(b) for b in s.get("background") or []]
        m_ids = [str(m) for m in s.get("method") or []]
        t_ids = [str(t) for t in s.get("tables") or []]
        ins = [int(i) for i in s.get("insights") or [] if str(i).lstrip("-").isdigit()]
        problems = []
        nums = insights.numbers(insights.strip_references(text))
        if s.get("frame"):
            if cites or b_ids or m_ids or t_ids:
                problems.append("a framing sentence cites sources; mark it as a claim instead")
            if nums:
                problems.append(f"a framing sentence contains numbers: {', '.join(nums)}")
        else:
            if not (cites or b_ids or m_ids or t_ids):
                problems.append("states something but cites nothing")
            if sec in ("Results", "Evidence", "Lessons", "Argument") and not (cites or t_ids):
                problems.append(f"a {sec} sentence without a finding or table")
            for bad, kind in (([c for c in cites if c not in findings], "findings"),
                              ([b for b in b_ids if b not in bg], "background sources"),
                              ([m for m in m_ids if m not in facts], "method facts"),
                              ([t for t in t_ids if t not in tabs], "tables")):
                if bad:
                    problems.append(f"cites {kind} that were not given: {bad}")
            items = [numgate.finding_text(findings[c]["finding"], findings[c].get("effect"), findings[c].get("for_whom"),
                                          findings[c].get("evidence"),
                                          (findings[c].get("human_check") or {}).get("correction"))
                     for c in cites if c in findings]
            items += [" ".join(str(v) for v in data["studies"].get(str(findings[c]["work_id"]), {}).values())
                      for c in cites if c in findings]
            items += [tabs[t]["text"] for t in t_ids if t in tabs]
            items += [f"{bg[b]['claim']} {bg[b]['cite']}" for b in b_ids if b in bg]
            items += [facts[m]["text"] for m in m_ids if m in facts]
            # a proposal's own design parameters (clusters, steps, margins) are its plan, not claims about evidence
            if not (s.get("plan") and sec in ("Proposal", "Evaluation", "Model table")):
                problems += numgate.describe(numgate.gate(insights.strip_references(text), items))
        bad_ins = [i for i in ins if i not in given or i in dropped]
        if bad_ins:
            problems.append(f"uses insights that were not given or are dropped: {bad_ins}")
        totals["sentences"] += 1
        totals["words"] += len(text.split())
        totals["frame"] += bool(s.get("frame"))
        totals["flagged"] += bool(problems)
        totals["verified"] += not problems and not s.get("frame")
        results.append({"section": sec, "p": p_i, "s": s_i, "problems": problems})
    return {"totals": totals, "sentences": results}


# Rendering

def render(store: Store, paper_id: int, full: bool = True) -> str:
    """The manuscript as Markdown. full=False is the submission reviewers see (no verification appendix). A focused
    (aim) paper has its own Method and leaves the full method and the scoping tables to its supplement
    (render_supplement); full=True appends them."""
    ensure(store)
    row = store.db.execute("SELECT * FROM papers WHERE paper_id = ?", (paper_id,)).fetchone()
    paper, method, ver = json.loads(row["content"]), json.loads(row["method"]), json.loads(row["verification"] or "{}")
    protocol_bg = {b["id"]: b for b in method.get("background_full", [])}
    flags = {(r["section"], r["p"], r["s"]): r["problems"] for r in ver.get("sentences", [])}
    ids = sorted({c for _, _, _, s in all_sentences(paper) for c in s.get("cites") or []})   # model table rows too
    rows = {r["finding_id"]: r for r in store.db.execute(
        f"""SELECT f.finding_id, f.work_id, f.evidence, f.source, w.title, w.year, w.authors, w.venue, w.doi
            FROM findings f JOIN works w ON w.work_id = f.work_id
            WHERE f.finding_id IN ({','.join('?' * len(ids))})""", tuple(ids))} if ids else {}

    stored = {int(w): lab for w, lab in (method.get("cite_labels") or {}).items()}
    relabel: dict[str, str] = {}
    if method.get("aim"):
        # A focused paper labels the studies it uses among themselves (Baartman 2022b without a 2022a otherwise); the
        # supplement keeps the labels that tell all retained records apart.
        used = {int(w) for w in method.get("studies") or {}} | {r["work_id"] for r in rows.values()}
        used |= {int(w) for w, lab in stored.items() if lab in {b.get("cite") for b in protocol_bg.values()}}
        if used:
            wrows = store.db.execute(f"SELECT work_id, authors, year, title FROM works WHERE work_id IN "
                                     f"({','.join('?' * len(used))})", tuple(used)).fetchall()
            fresh = writer.cite_labels(wrows)
            relabel = {stored[w]: fresh[w] for w in used if w in stored and w in fresh and stored[w] != fresh[w]}
            stored.update({w: fresh[w] for w in used if w in fresh})

    def label_of(work_id, authors, year):
        return stored.get(work_id) or writer.cite_label(authors, year)

    def relabelled(text: str) -> str:
        for old in sorted(relabel, key=len, reverse=True):
            text = text.replace(old, relabel[old])
        return text

    # a background source that is also a retained record is cited by the record's label (it is listed once)
    by_doi = {w["doi"].lower(): label_of(w["work_id"], w["authors"], w["year"])
              for w in tables.works(store, set(method.get("retained") or [])).values() if w["doi"]}
    bg_label = {}
    for b, entry in protocol_bg.items():
        m = re.search(r"10\.\d{4,9}/\S+", entry.get("reference", ""))
        if m and m.group(0).rstrip(".").lower() in by_doi:
            bg_label[b] = by_doi[m.group(0).rstrip(".").lower()]
    bg_cite = {b: e["cite"] for b, e in protocol_bg.items()}
    if method.get("aim"):
        # letters among what this paper cites: a background source and a study by the same authors in the same year
        # are a and b by title; a background source alone needs no letter
        def base(label):
            return re.sub(r"(\d{4})-?[a-z]$", r"\1", label)

        def bg_title(ref):
            t = re.search(r"\(\d{4}[a-z]?\)\.\s*(.+?)[.?]\s", ref)
            return t.group(1) if t else ref
        cited_bg = {b for _, _, _, x in all_sentences(paper) for b in x.get("background") or []}
        cited_works = {r["work_id"]: r for r in rows.values()}
        # a background source that is also a retained record carries that record's letter (2013b); alone among what
        # this paper cites, it needs none
        for b in sorted(cited_bg & set(bg_label)):
            same = [w for w in cited_works if base(label_of(w, cited_works[w]["authors"], cited_works[w]["year"]))
                    == base(bg_label[b])]
            same += [c for c in cited_bg - {b} if base(bg_label.get(c, bg_cite.get(c, ""))) == base(bg_label[b])]
            if not same:
                bg_label[b] = base(bg_label[b])
        for b in sorted(cited_bg):
            if b not in protocol_bg or b in bg_label:
                continue
            group = [("w", w, (cited_works[w]["title"] or "")) for w in cited_works
                     if base(label_of(w, cited_works[w]["authors"], cited_works[w]["year"])) == base(bg_cite[b])]
            group += [("b", c, bg_title(protocol_bg[c]["reference"])) for c in sorted(cited_bg)
                      if c in protocol_bg and c not in bg_label and base(bg_cite[c]) == base(bg_cite[b])]
            if len(group) == 1:
                bg_cite[b] = base(bg_cite[b])
                continue
            for i, (kind, key, _) in enumerate(sorted(group, key=lambda g: " ".join(g[2].casefold().split()))):
                new = base(bg_cite[b] if kind == "b" else stored.get(key, "")) + "abcdefghijklmnopqrstuvwxyz"[i % 26]
                if kind == "b":
                    bg_cite[key] = new
                elif stored.get(key) and stored[key] != new:
                    relabel[stored[key]] = new
                    stored[key] = new

    essay = method.get("aim_layout") == "essay"

    def sentence(sec, p_i, s_i, s, seen=None):
        """seen: the labels the sentence before printed, which an essay does not print again in the next one (the
        sentence still cites them in the record, and the check and audit still use them). Only the sentence before:
        a label kept back further would leave a study's own sentence bare after a sentence that sums up several."""
        text = s["text"].strip()
        labels = []
        for c in s.get("cites") or []:
            r = rows.get(c)
            if r:
                lab = label_of(r["work_id"], r["authors"], r["year"])
                if lab not in labels:
                    labels.append(lab)
        for b in s.get("background") or []:
            if b in protocol_bg and bg_label.get(b, bg_cite[b]) not in labels:
                labels.append(bg_label.get(b, bg_cite[b]))
        for t in s.get("tables") or []:
            lab = table_label(t)
            if lab and lab not in text and lab not in labels:
                labels.insert(0, lab)
        if essay and sec == "Abstract":     # an essay's abstract, like a journal's, prints no citations
            labels = []
        if seen is not None:
            shown, labels = [lab for lab in labels if lab not in seen], labels
            seen.clear()
            seen.update(labels)
            labels = shown
        if labels:
            end = text[-1] if text[-1:] in ".!?" else ""
            text = f"{text[:-1] if end else text} ({'; '.join(labels)}){end}"
        if full and flags.get((sec, p_i, s_i)):
            text += " **[⚠]**"
        return text

    protocol = checks.current_protocol(store, row["corpus"])
    stated = (protocol.get("paper") or {}).get("declarations") or {}
    authors = "; ".join(author_line(a) for a in (protocol.get("paper") or {}).get("authors") or method.get("authors")
                        or ["(author)"])
    out = [f"# {paper['title']}", "", f"**Authors:** {authors}", ""]
    ai_use = "**Declaration of AI use:** " + checks.declaration(store, row, protocol, ver.get("totals", {}))
    if not essay:                       # an essay opens on its argument; the declaration goes with the others
        out += [ai_use, ""]
    out += ["## Abstract", ""]
    for p_i, p in enumerate(paper.get("abstract") or []):
        out += [(f"**{p['heading'].strip().rstrip('.:')}.** " if p.get("heading") else "")
                + " ".join(sentence("Abstract", p_i, s_i, s) for s_i, s in enumerate(p["sentences"])), ""]
    if paper.get("keywords"):
        out += [f"**Keywords:** {', '.join(paper['keywords'])}", ""]
    n = 0
    for sec in paper.get("sections") or []:
        title = " ".join(str(sec.get("title") or "").split()) or sec["name"]
        if essay and sec["name"] == "Method":
            out += [f"## Appendix: {title if sec.get('title') else 'The evidence review'}", ""]
        else:
            n += 1
            out += [f"## {n}. {title}", ""]
        for p_i, p in enumerate(sec.get("paragraphs") or []):
            if p.get("heading"):
                out += [f"### {p['heading']}", ""]
            seen = set() if essay else None
            out += [" ".join(sentence(sec["name"], p_i, s_i, s, seen) for s_i, s in enumerate(p["sentences"])), ""]
        if method.get("aim") and sec["name"] == ("Lessons" if method.get("aim_layout") == "policy" else "Method"):
            main = [t for t in method.get("tables") or [] if t.get("place") == "main"]
            main.sort(key=lambda t: (t["id"][:1] != "F", int(re.sub(r"\D", "", t["id"]) or 0)))   # Figure 1, Table 1, 2 ...
            out += [relabelled(t["text"]) + "\n" for t in main]
        kind = method.get("aim_layout") or method.get("aim_kind") or "proposal"
        if sec["name"] == {"effectiveness": "Discussion", "policy": "Recommendations",
                           "essay": "What follows"}.get(kind, "Proposal") \
                and (paper.get("model_table") or {}).get("rows"):
            from . import aim as aim_mod
            label = tables.table_labels(aim_mod.with_aim(protocol, method.get("aim_id"))).get("model", "Table 5")
            out += [_model_table(paper["model_table"], rows, protocol_bg, bg_cite, bg_label, label_of, label, kind), ""]
        if sec["name"] == "Introduction" and not method.get("aim"):
            n += 1
            out += [f"## {n}. Method", "", method["section"], ""]
            placed = [t for t in method.get("tables") or [] if "place" in t]
            if placed:
                out += [t["text"] + "\n" for t in placed if t["place"] != "supplement"]
            elif method.get("table"):
                out += [method["table"], ""]
    out += ["## Declarations", ""] + ([ai_use, ""] if essay else [])
    for key, head, default in DECLARATIONS:
        out += [f"**{head}.** " + (" ".join(str(stated[key]).split()) if stated.get(key) else default), ""]
    out += ["## References", ""]
    refs = []
    separate = bool((protocol.get("paper") or {}).get("included_list"))
    retained = set(method.get("retained") or [])
    # a focused paper lists the studies its main table of studies shows (a table in the supplement does not count)
    in_main = any(t.get("role") == "studies" and t.get("place") == "main" for t in method.get("tables") or [])
    # a focused paper's retained records are listed in its supplement, not among the references
    cited = sorted({r["work_id"] for r in rows.values()} | (set() if separate or method.get("aim") else retained)
                   | ({int(w) for w in method.get("studies") or {}} if method.get("aim") and in_main else set()))
    proper = tables.proper_nouns(store, row["run_id"])

    def entry(w):
        lab = label_of(w["work_id"], w["authors"], w["year"])
        suffix = lab[len(w["cite"]):].lstrip("-") if lab.startswith(w["cite"]) else ""
        suffix = ("-" + suffix if suffix and not w["year"] else suffix)
        return lab, writer.reference(w, biblio.get(store, w["work_id"]), proper, suffix)
    for w in tables.works(store, cited).values():
        refs.append(entry(w))
    used_bg = {b for _, _, _, s in all_sentences(paper) for b in s.get("background") or []}
    listed = " ".join(r[1].lower() for r in refs)
    for b in sorted(used_bg):
        if b in protocol_bg:
            doi = re.search(r"10\.\d{4,9}/\S+", protocol_bg[b]["reference"])
            if doi and doi.group(0).rstrip(".").lower() in listed:
                continue                                # already listed as a retained record
            refs.append((bg_cite[b], _with_suffix(protocol_bg[b]["reference"], bg_cite[b])))
    listed = " ".join(r[1].lower() for r in refs)
    body = "\n".join(out)
    for author, reference in METHOD_REFERENCES:        # cited by the generated Method section, in every corpus
        doi = re.search(r"10\.\d{4,9}/\S+", reference).group(0).lower()
        # a focused paper's full method is in its supplement: listed here only when the text itself cites it
        if method.get("aim") and author.split(",")[0] not in body:
            continue
        if doi not in listed:
            refs.append((author, reference))
    out += [f"- {text}" for _, text in sorted(refs, key=lambda r: r[1].casefold())]
    if method.get("aim"):
        if full:
            out += ["", _supplement_body(store, row, method, entry)]
        separate, retained = False, set()
    if separate and retained:
        core = set((method.get("core") or []))
        inc = [entry(w) + (w["work_id"],) for w in tables.works(store, retained).values()]
        out += ["", "## Records retained for charting", "",
                f"The {len(inc)} records retained after the eligibility stage, to which the supplementary tables refer by "
                "label" + ("; * marks the core studies, which meet every inclusion criterion." if core else ".")
                + " Records cited in the text are also listed under References.", ""]
        out += [f"- {text}{' *' if w in core else ''}" for _, text, w in sorted(inc, key=lambda r: r[1].casefold())]
    supplement = [t for t in method.get("tables") or [] if t.get("place") == "supplement"]
    if supplement and not method.get("aim"):     # a focused paper keeps its supplement apart (render_supplement)
        out += ["", "## Supplementary tables", ""] + [t["text"] + "\n" for t in supplement]
    if row["response"] and full:
        out += ["", "## Response to reviewers", ""]
        for r in json.loads(row["response"]):
            out.append(f"- **{r.get('change')}** ({r.get('action')}, {r.get('where', '')}): {r.get('response')}")
    if full:
        out += [""] + checks.appendix(store, row, protocol)
        t = ver.get("totals", {})
        out += ["", "## Appendix: verification", "",
                f"{t.get('verified', 0)} of {t.get('sentences', 0)} sentences verified, {t.get('frame', 0)} framing, "
                f"{t.get('flagged', 0)} flagged. Checked in code: every claim cites findings, method facts, "
                "background sources or tables that exist; every number occurs in what its sentence cites; Results "
                "sentences cite findings or tables. Background sources were not checked against their texts; see the protocol.", ""]
        for r in ver.get("sentences", []):
            if r["problems"]:
                s = _find(paper, r)
                out.append(f"- {r['section']}: “{s['text'].strip()}” — {'; '.join(r['problems'])}")
    return "\n".join(out) + "\n"


MODEL_HEADS = {
    "proposal": ("Component", "What changes", "Why", "Expected effect on performance", "Expected effect on well-being",
                 "Strength of evidence", "How to test"),
    "effectiveness": ("Recommendation", "What it means in practice", "Why", "Effect on performance",
                      "Effect on well-being", "Strength of evidence", "How to test"),
    "policy": ("Action", "Who acts and how", "Why", "Expected effect on births", "Other effects to watch",
               "Strength of evidence", "How to evaluate")}
MODEL_HEADS["essay"] = MODEL_HEADS["policy"]
MODEL_OPTIONAL_HEADS = {"effectiveness": {"adopt": "A firm recommendation or a design hypothesis"},
                        "policy": {"adopt": "Act now, or pilot and evaluate"},
                        "essay": {"adopt": "Act now, or pilot and evaluate"}}


def _model_table(table: dict, rows: dict, protocol_bg: dict, bg_cite: dict, bg_label: dict, label_of,
                 label: str = "Table 5", kind: str = "proposal") -> str:
    """The writer's own table: a proposal's model (its components), or a general paper's recommendations; each row's
    sources as author-year labels."""
    def esc(x):
        return " ".join(str(x or "").split()).replace("|", "/")
    caption = re.sub(r"^\s*Table \d+[.:]?\s*", "", esc(table.get("caption")))     # the label is added here
    extra = [k for k in MODEL_OPTIONAL if any((r.get(k) or "").strip() for r in table.get("rows") or [])]
    heads = {**MODEL_OPTIONAL, **MODEL_OPTIONAL_HEADS.get(kind, {})}
    lines = [f"**{label}.** {caption}", "",
             "| " + " | ".join(MODEL_HEADS.get(kind, MODEL_HEADS["proposal"])) + " | "
             + "".join(f"{heads[k]} | " for k in extra) + "Sources |",
             "| " + " | ".join("---" for _ in range(8 + len(extra))) + " |"]
    for r in table.get("rows") or []:
        labels = []
        for c in r.get("cites") or []:
            f = rows.get(c)
            if f:
                lab = label_of(f["work_id"], f["authors"], f["year"])
                if lab not in labels:
                    labels.append(lab)
        for b in r.get("background") or []:
            if b in protocol_bg and bg_label.get(b, bg_cite.get(b)) not in labels:
                labels.append(bg_label.get(b, bg_cite.get(b)))
        labels += [table_label(t) for t in r.get("tables") or [] if table_label(t)]
        lines.append("| " + " | ".join(esc(r.get(c)) for c in MODEL_CELLS + tuple(extra)) + f" | {'; '.join(labels)} |")
    return "\n".join(lines)


# A focused paper prints the supplement on its focused studies; the tables over every retained record of the map go
# to a supplementary data file, which the printed supplement names.
DATA_ROLES = ("sources", "eligibility", "map_list", "without", "core")


def _supplement_body(store, row, method, entry, part: str = "all") -> str:
    """part: "all" (every table and record), "print" (without the data tables, listing only the focused studies) or
    "data" (the data tables and every retained record)."""
    supp = [t for t in method.get("tables") or [] if t.get("place") == "supplement"]
    supp.sort(key=lambda t: int(re.sub(r"\D", "", t["id"]) or 0))
    data = [t for t in supp if t.get("role") in DATA_ROLES]
    if part == "print":
        supp = [t for t in supp if t not in data]
    elif part == "data":
        supp = data
    out = [] if part == "data" else ["## Supplement", "", "### Supplementary method", "", method["section"], ""]
    cites = [ref for author, ref in METHOD_REFERENCES if author.split(",")[0] in method["section"]]
    if part != "data" and cites:
        out += ["References of the supplementary method:", ""] + [f"- {r}" for r in cites] + [""]
    out += ["### Supplementary tables", ""] + [t["text"] + "\n" for t in supp]
    if part == "print" and data:
        ids = [t["id"] for t in data]
        named = ", ".join(ids[:-1]) + " and " + ids[-1] if len(ids) > 1 else ids[0]
        out += [f"Table{'s' if len(ids) > 1 else ''} {named} and the list of every record retained for charting "
                "cover the whole evidence map rather than the focused studies; they are in the supplementary data "
                "file.", ""]
    retained = set(method.get("retained") or [])
    focused = {int(w) for w in method.get("studies") or {}}
    if part == "print" and focused:
        inc = [entry(w) + (w["work_id"],) for w in tables.works(store, focused).values()]
        out += ["", "### Studies of the focused evidence", "",
                f"The {len(inc)} studies of the focused evidence (Table 1), to which the focused tables refer by "
                "label.", ""]
        out += [f"- {text}" for _, text, w in sorted(inc, key=lambda r: r[1].casefold())]
    elif part != "print" and retained:
        core = set(method.get("core") or [])
        inc = [entry(w) + (w["work_id"],) for w in tables.works(store, retained).values()]
        out += ["", "### Records retained for charting", "",
                f"The {len(inc)} records retained after the eligibility stage, to which the supplementary tables refer "
                "by label" + ("; * marks the core studies." if core else "."), ""]
        out += [f"- {text}{' *' if w in core else ''}" for _, text, w in sorted(inc, key=lambda r: r[1].casefold())]
    return "\n".join(out)


def render_supplement(store: Store, paper_id: int, part: str = "print") -> str:
    """A focused paper's supplement on its own: the full generated method, the tables on the focused studies and
    those studies (part "print"), or the supplementary data: the tables over the whole map and every retained record
    (part "data")."""
    row = store.db.execute("SELECT * FROM papers WHERE paper_id = ?", (paper_id,)).fetchone()
    method = json.loads(row["method"])
    stored = {int(w): lab for w, lab in (method.get("cite_labels") or {}).items()}
    proper = tables.proper_nouns(store, row["run_id"])

    def entry(w):
        lab = stored.get(w["work_id"]) or writer.cite_label(w["authors"], w["year"])
        suffix = lab[len(w["cite"]):].lstrip("-") if lab.startswith(w["cite"]) else ""
        suffix = ("-" + suffix if suffix and not w["year"] else suffix)
        return lab, writer.reference(w, biblio.get(store, w["work_id"]), proper, suffix)
    title = json.loads(row["content"]).get("title", "")
    head = "Supplementary data to" if part == "data" else "Supplement to"
    return f"# {head}: {title}\n\n" + _supplement_body(store, row, method, entry, part) + "\n"


def _with_suffix(reference: str, cite: str) -> str:
    """A background reference whose in-text label has a letter (2026c) gets the letter after its year too."""
    m = re.search(r"(\d{4})([a-z])$", cite.strip())
    if not m or f"({m.group(1)}{m.group(2)})" in reference:
        return reference
    return reference.replace(f"({m.group(1)})", f"({m.group(1)}{m.group(2)})", 1)


def author_line(author) -> str:
    """An author from `[paper] authors`: a name, or {name, affiliation}."""
    if isinstance(author, dict):
        return author.get("name", "?") + (f" ({author['affiliation']})" if author.get("affiliation") else "")
    return str(author)


METHOD_REFERENCES = [
    ("Arksey, H., & O'Malley, L.", "Arksey, H., & O'Malley, L. (2005). Scoping studies: Towards a methodological "
                                   "framework. *International Journal of Social Research Methodology*, *8*(1), 19–32. "
                                   "https://doi.org/10.1080/1364557032000119616"),
    ("Tricco, A. C., Lillie, E., Zarin, W., et al.", "Tricco, A. C., Lillie, E., Zarin, W., et al. (2018). PRISMA "
                                                     "extension for scoping reviews (PRISMA-ScR): Checklist and "
                                                     "explanation. *Annals of Internal Medicine*, *169*(7), 467–473. "
                                                     "https://doi.org/10.7326/M18-0850"),
]

DECLARATIONS = [
    ("data", "Data and materials", "The protocol, every prompt and agent answer, the evidence store and the extraction "
                                   "tables are available from the author; each claim can be traced to the passage it "
                                   "rests on."),
    ("funding", "Funding", "[To be stated by the author.]"),
    ("ethics", "Ethics", "[To be stated by the author.]"),
    ("competing_interests", "Competing interests", "[To be stated by the author.]"),
    ("credit", "Author contributions (CRediT)", "[To be stated by the author.]"),
]


def _find(paper, r):
    if r["section"] == "Abstract":
        return paper["abstract"][r["p"]]["sentences"][r["s"]]
    sec = next(s for s in paper["sections"] if s["name"] == r["section"])
    return sec["paragraphs"][r["p"]]["sentences"][r["s"]]


def write_files(store: Store, paper_id: int, root: Path) -> tuple[Path, Path]:
    row = store.db.execute("SELECT * FROM papers WHERE paper_id = ?", (paper_id,)).fetchone()
    base = root / "output" / f"corpus_{row['corpus']}" / f"run_{row['run_id']:03d}" / "papers"
    base.mkdir(parents=True, exist_ok=True)
    full = base / f"paper_{paper_id:02d}_v{row['version']}.md"
    sub = base / f"paper_{paper_id:02d}_v{row['version']}_submission.md"
    text = render(store, paper_id, True)
    full.write_text(text, encoding="utf-8")
    sub.write_text(render(store, paper_id, False), encoding="utf-8")
    full.with_suffix(".html").write_text(document.to_html(text, row["title"] or "Manuscript"), encoding="utf-8")
    if json.loads(row["method"]).get("aim"):
        supp = base / f"paper_{paper_id:02d}_v{row['version']}_supplement.md"
        supp.write_text(render_supplement(store, paper_id), encoding="utf-8")
        data = render_supplement(store, paper_id, "data")
        path = base / f"paper_{paper_id:02d}_v{row['version']}_data.md"
        path.write_text(data, encoding="utf-8")
        path.with_suffix(".html").write_text(document.to_html(data, "Supplementary data"), encoding="utf-8")
    return full, sub


def write_pdf(store: Store, paper_id: int, root: Path, full: bool = False) -> Path:
    """The manuscript as a journal-style PDF: the submission (or, with full, the version with the response to
    reviewers and the appendices)."""
    row = store.db.execute("SELECT * FROM papers WHERE paper_id = ?", (paper_id,)).fetchone()
    base = root / "output" / f"corpus_{row['corpus']}" / f"run_{row['run_id']:03d}" / "papers"
    base.mkdir(parents=True, exist_ok=True)
    out = base / f"paper_{paper_id:02d}_v{row['version']}{'' if full else '_submission'}.pdf"
    return document.to_pdf(render(store, paper_id, full), row["title"] or "Manuscript", out)


def write_supplement_pdf(store: Store, paper_id: int, root: Path) -> Path:
    row = store.db.execute("SELECT * FROM papers WHERE paper_id = ?", (paper_id,)).fetchone()
    base = root / "output" / f"corpus_{row['corpus']}" / f"run_{row['run_id']:03d}" / "papers"
    base.mkdir(parents=True, exist_ok=True)
    out = base / f"paper_{paper_id:02d}_v{row['version']}_supplement.pdf"
    return document.to_pdf(render_supplement(store, paper_id), "Supplement", out)


# Task folders

BASE_NOTE = """
## The previous version

`previous_version` in batch_01.json is this paper as it was finished on an earlier evidence base (Markdown, with its
tables). The evidence has changed since: studies were added, set apart or read again in full, and findings re-extracted.
Write the paper anew from the current evidence in this batch. Keep from the previous version what still holds: its
structure, its argument where the current evidence still carries it, how it states the strength of each claim, and, for a
proposal, its design and evaluation plan. Change whatever the current evidence changes. The paper stands on its own:
it does not mention or compare itself with the previous version. Every number, citation and table reference must come
from the current evidence and tables in this batch, never from the previous version.
"""


def write_task(store: Store, run_id: int, kind: str, agent: str, protocol: dict, sha: str, root: Path,
               options: dict) -> Path:
    from .agents import _new_folder
    ensure(store)
    corpus = store.db.execute("SELECT corpus FROM runs WHERE run_id = ?", (run_id,)).fetchone()["corpus"]
    meta = {"kind": kind, "agent": agent, "run_id": run_id, "corpus": corpus, "protocol_sha256": sha, "batches": 1}
    from . import aim as aim_mod
    if kind == "paper":
        protocol = aim_mod.with_aim(protocol, options.get("aim"))
    elif kind in ("revise", "peer", "meta"):       # reviewers judge the paper for the journal its aim names
        parent = options.get("paper") or _latest_id(store, run_id)
        prow = store.db.execute("SELECT method FROM papers WHERE paper_id = ?", (parent,)).fetchone() if parent else None
        protocol = aim_mod.with_aim(protocol, json.loads(prow["method"]).get("aim_id") if prow else None)
    if kind == "paper":
        data = paper_input(store, run_id, protocol)
        text = paper_instructions(protocol)
        meta["records"] = len(data["studies"])
        if options.get("base"):
            # a paper written anew on a changed evidence base, from the finished version before it
            data["previous_version"] = render(store, int(options["base"]), full=False)
            meta["base"] = int(options["base"])
            text += BASE_NOTE
    else:
        pid = options.get("paper") or _latest_id(store, run_id)
        if pid is None:
            raise ValueError("no manuscript yet: run a paper task first")
        row = store.db.execute("SELECT * FROM papers WHERE paper_id = ?", (pid,)).fetchone()
        meta["paper_id"] = pid
        paper = json.loads(row["content"])
        if kind == "audit":
            items = audit_items(store, row)
            used = {c["table"] for i in items for c in i["cited"] if "table" in c}
            data = {"tables": {t["id"]: t["text"] for t in json.loads(row["method"]).get("tables", [])
                               if t["id"] in used}, "sentences": items}
            text = audit_instructions()
            meta["records"] = len(data["sentences"])
        elif kind == "peer":
            persona = options.get("persona") or "critical"
            meta["persona"] = persona
            data = {"manuscript": render(store, pid, full=False)}
            text = peer_instructions(protocol, persona)
            meta["records"] = 1
        elif kind == "meta":
            rnd = int(options.get("round") or row["version"])
            meta["round"] = rnd
            data = {"manuscript": render(store, pid, full=False), "reviews": _reviews(store, pid, "peer"),
                    "audit": _audit_summary(store, pid)}
            if rnd >= 2 and row["parent_id"]:
                data["previous_meta_review"] = _reviews(store, row["parent_id"], "meta")
                data["response_to_reviewers"] = json.loads(row["response"] or "[]")
            text = meta_instructions(rnd)
            meta["records"] = 1
        else:   # revise
            data = paper_input(store, run_id, protocol)
            data.update({"paper": paper, "meta_review": _reviews(store, pid, "meta"),
                         "reviews": _reviews(store, pid, "peer"), "audit_flags": _audit_flags(store, pid)})
            if not data["meta_review"]:
                raise ValueError("no meta-review for this paper yet")
            text = paper_instructions(protocol, revision=True)
            meta["records"] = len(data["studies"])
    out = _new_folder(root, corpus, run_id, kind, agent + (f"-{options.get('persona')}" if kind == "peer" else ""))
    (out / "instructions.md").write_text(text, encoding="utf-8")
    (out / "batch_01.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    if kind in ("paper", "revise"):
        (out / "findings.md").write_text(findings_digest(data, protocol), encoding="utf-8")
    (out / "task.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    return out


def _latest_id(store, run_id):
    ensure(store)
    row = store.db.execute("SELECT paper_id FROM papers WHERE run_id = ? ORDER BY paper_id DESC LIMIT 1",
                           (run_id,)).fetchone()
    return row["paper_id"] if row else None


def _reviews(store, pid, kind):
    return [dict(json.loads(r["content"]), reviewer=r["reviewer"], round=r["round"]) for r in store.db.execute(
        "SELECT * FROM reviews WHERE paper_id = ? AND kind = ? ORDER BY review_id", (pid, kind))]


def _audit_summary(store, pid):
    a = _reviews(store, pid, "audit")
    if not a:
        return None
    verdicts = Counter(v["verdict"] for v in a[-1]["verdicts"])
    return {"verdicts": dict(verdicts), "flagged": _audit_flags(store, pid)}


def _audit_flags(store, pid):
    a = _reviews(store, pid, "audit")
    return [v for v in a[-1]["verdicts"] if v["verdict"] != "faithful"] if a else []


# Audit: each sentence with exactly what it cites

def audit_items(store: Store, row) -> list[dict]:
    paper, method = json.loads(row["content"]), json.loads(row["method"])
    bg = {b["id"]: b for b in method.get("background_full", [])}
    facts = {f["id"]: f["text"] for f in method.get("facts", [])}
    studies = method.get("studies") or {}
    tabs = {t["id"] for t in method.get("tables", [])}
    items = []
    for n, (sec, p_i, s_i, s) in enumerate(all_sentences(paper)):
        if s.get("frame"):
            continue
        cited = []
        for c in s.get("cites") or []:
            f = store.db.execute("SELECT finding, evidence, source, work_id FROM findings WHERE finding_id = ?",
                                 (c,)).fetchone()
            if f:
                item = {"finding": c, "statement": f["finding"], "passage": f["evidence"],
                        "passage_from": f["source"] or "unconfirmed"}
                if studies.get(str(f["work_id"])):
                    item["study"] = studies[str(f["work_id"])]
                human = checks.for_writer(store, [c]).get(c)
                if human:
                    item["human_check"] = human
                cited.append(item)
        cited += [{"background": b, "claim": " ".join(bg[b]["claim"].split())} for b in s.get("background") or [] if b in bg]
        cited += [{"method": m, "fact": facts[m]} for m in s.get("method") or [] if m in facts]
        cited += [{"table": t} for t in s.get("tables") or [] if t in tabs]
        items.append({"id": f"S{n + 1}", "section": sec, "p": p_i, "s": s_i, "text": s["text"], "cited": cited,
                      **({"plan": True} if s.get("plan") else {})})     # the authors' own design parameters
    return items


def audit_instructions() -> str:
    return "\n".join([
        "# Evidence audit",
        "",
        "batch_01.json lists every claim sentence of a manuscript with exactly what it cites: findings (the extractor's "
        "statement, the verbatim passage, which text the passage was found in, and the study's charted details: "
        "type, charted dimensions, population, charting source and its set in the eligibility table), background "
        "claims, "
        "method facts and tables (`{\"table\": \"T2\"}`: the table's full text is under `tables` at the top of "
        "the file). You see nothing else, on purpose.",
        "",
        "For each sentence decide whether it represents what it cites:",
        "- `faithful`: says no more and no less than the cited material supports;",
        "- `overstated`: claims more certainty, generality, causality or strength than the material supports;",
        "- `understated`: weaker than the material, in a way that misleads;",
        "- `unsupported`: the cited material does not support the claim, or part of it;",
        "- `misattributed`: the claim may be true but is attributed to the wrong source or population.",
        "",
        "Judge the passages, not the extractor's statements, when they differ. A finding with `human_check` marked "
        "`corrected` was checked by a person: its `correction` says what the source says, so judge against it. Be "
        "strict but fair: wording that paraphrases accurately is faithful.",
        "",
        "Output: one JSON object: {\"verdicts\": [{\"id\": \"S1\", \"verdict\": \"faithful\", \"reason\": \"at most "
        "30 words\", \"fix\": \"optional suggested rewording\"}]} with every sentence id exactly once.",
    ]) + "\n"


def peer_instructions(protocol: dict, persona: str) -> str:
    who = PERSONAS.get(persona, persona).format(field=protocol.get("title", "this field"))
    target = (protocol.get("paper") or {}).get("target", "a peer-reviewed journal")
    return "\n".join([
        f"# Peer review for {target}",
        "",
        f"You are {who}",
        "",
        "batch_01.json holds the submitted manuscript (`manuscript`, Markdown). Review it as you would for the "
        "journal. You see only the manuscript; judge it on its own terms, and do not look anything up.",
        "",
        "A review paper should tell its readers what the evidence shows. Ask for claims to be calibrated (their "
        "strength stated: tested in comparisons, consistently reported across many studies, a single study), not "
        "removed: a finding reported consistently across many studies is a finding about what those studies report, "
        "not a hypothesis. Count it as a weakness when a paper hedges so much that it says nothing.",
        "",
        "Scores (integers): " + "; ".join(f"`{k}`: {v}" for k, v in RUBRIC.items())
        + "; `overall` 1-10 (10 = top 5% of papers in this journal); `confidence` 1-5.",
        "",
        f"`decision`: one of {', '.join(DECISIONS)}.",
        "",
        "Also give `summary` (3-5 sentences), `strengths` and `weaknesses` (lists), `questions` for the authors, and "
        "`requested_changes`: [{\"change\": \"...\", \"location\": \"section\", \"severity\": \"major|minor\"}].",
        "",
        "Output: one JSON object with exactly these fields, each score a top-level field (no nested `scores` "
        "object): {\"" + "\": 2, \"".join(RUBRIC) + "\": 2, \"overall\": 5, \"confidence\": 3, \"decision\": \"...\", "
        "\"summary\": \"...\", \"strengths\": [], \"weaknesses\": [], \"questions\": [], \"requested_changes\": []}.",
    ]) + "\n"


def meta_instructions(rnd: int) -> str:
    if rnd == 1:
        task = ("You are the handling editor. batch_01.json holds the submitted manuscript, the independent "
                "reviews and the evidence audit (how many sentences an auditor found faithful, overstated, "
                "unsupported... and which). Weigh them, resolve disagreements between reviewers, and decide.")
        extra = ("`required_changes`: the changes the authors must make, numbered R1, R2, ... "
                 "[{\"id\": \"R1\", \"change\": \"...\", \"from\": [\"reviewer or audit\"], \"severity\": \"major|minor\"}]; "
                 "every audit flag that matters belongs here.")
    else:
        task = (f"You are the handling editor, round {rnd}. batch_01.json holds the revised manuscript, the authors' "
                "response to your required changes, your previous meta-review and the audit of the revision"
                + (", and new reviews if any" if rnd > 2 else "") + ". Judge whether each required change was made, "
                "and decide. Judge the revision against your previous required changes only; do not raise new "
                "conditions unless the revision introduced a new problem.")
        extra = ("`assessment`: [{\"id\": \"R1\", \"addressed\": \"yes|partly|no\", \"comment\": \"...\"}] for every "
                 "earlier required change; `required_changes` for anything still needed (may be empty), each with "
                 "`needs`: \"pipeline\" (a text or generated-table revision can fix it) or \"author\" (only the "
                 "human author can).")
    return "\n".join([
        f"# Editorial decision, round {rnd}",
        "",
        task,
        "",
        "Weigh usefulness as well as accuracy: does the paper give its readers a clear answer, with each claim's "
        "strength stated? Required changes should make claims more accurate, not fewer; do not require that findings "
        "reported consistently across many studies be recast as hypotheses, and treat a paper that hedges every claim "
        "as a weakness. Keep the required changes to what matters most (at most ten).",
        "",
        "What only the human author can do (a person checking findings or a sample of the screening or coding, "
        "retrieving full texts through a library, depositing materials, signing declarations) is not a reason to "
        "reject or to require a revision when the manuscript says plainly what no person has checked: list such items "
        "with `needs`: \"author\" as recommendations, and decide on the content and on the checks the pipeline "
        "itself performed.",
        "",
        "Output: one JSON object with `summary` (4-8 sentences), `decision` (one of " + ", ".join(DECISIONS) + "), "
        "`scores` (your consensus on: " + ", ".join(RUBRIC) + ", overall), " + extra,
    ]) + "\n"


# Import

def import_task(folder: Path, store: Store, meta: dict, protocol: dict, instructions: str) -> dict:
    from .agents import _strip_fences, grounded
    ensure(store)
    kind, agent, run_id = meta["kind"], meta["agent"], meta["run_id"]
    batch = folder / "batch_01.json"
    result = folder / "batch_01_result.json"
    if not result.exists():
        return {"stored": 0, "problems": ["batch_01_result.json is missing"]}
    data = json.loads(batch.read_text(encoding="utf-8"))
    raw = result.read_text(encoding="utf-8")
    try:
        answer = json.loads(_strip_fences(raw))
    except ValueError as err:
        return {"stored": 0, "problems": [f"not valid JSON ({err})"]}
    errs = _check(kind, answer, data)
    found = answer.get("insights") or [] if isinstance(answer, dict) else []
    if kind in ("paper", "revise"):
        errs += insights.check(found, {int(w) for w in data["studies"]}, {f["id"] for f in data["findings"]})
    if errs:
        return {"stored": 0, "problems": errs}
    call_id = store.log_call(run_id, "llm", agent, f"{instructions}\n\n[{kind} batch, sha256 "
                             f"{hashlib.sha256(batch.read_bytes()).hexdigest()}]", 200, 0, raw.encode("utf-8"),
                             {"agent": agent, "task": kind, **{k: meta[k] for k in ("paper_id", "persona", "round")
                                                               if k in meta}})
    counts = {"stored": 1, "evidence": 0, "grounded": 0, "insights": 0, "problems": []}
    corpus = meta["corpus"]
    if kind in ("paper", "revise"):
        verification = verify(answer, data, store)
        from .agents import kept_ids
        method = {"facts": data["method_facts"], "section": data["method_section"],
                  "table": data.get("sources_table") or "\n\n".join(t["text"] for t in data.get("tables") or []),
                  "tables": data.get("tables") or [], "studies": data.get("studies") or {},
                  "retained": kept_ids(store, run_id) if data.get("tables") else [],
                  "core": [e["work_id"] for e in tables.eligibility(store, run_id, protocol) if e["core"]]
                  if data.get("tables") and tables.criteria(protocol) else [],
                  "background_full": protocol.get("background") or [],
                  "authors": (protocol.get("paper") or {}).get("authors"),
                  "cite_labels": {str(w): lab for w, lab in tables.cite_map(store, run_id).items()},
                  "models": sorted({r["agent"] for r in _agents_used(store, run_id)} | {agent}),
                  "aim": bool(data.get("aim")), "aim_id": (data.get("aim") or {}).get("id"),
                  "aim_kind": (data.get("aim") or {}).get("kind"), "aim_layout": (data.get("aim") or {}).get("layout")}
        parent = meta.get("paper_id") if kind == "revise" else None
        version = 1 if parent is None else store.db.execute(
            "SELECT version FROM papers WHERE paper_id = ?", (parent,)).fetchone()["version"] + 1
        content = {k: answer[k] for k in ("title", "abstract", "keywords", "sections", "model_table") if k in answer}
        with store.db:
            cur = store.db.execute(
                "INSERT INTO papers (corpus, run_id, version, parent_id, author, title, content, method, verification, "
                "response, call_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (corpus, run_id, version, parent, agent, answer["title"], json.dumps(content, ensure_ascii=False),
                 json.dumps(method, ensure_ascii=False), json.dumps(verification, ensure_ascii=False),
                 json.dumps(answer.get("response"), ensure_ascii=False) if kind == "revise" else None,
                 call_id, now()))
        counts.update(paper=cur.lastrowid, verification=verification["totals"])
        texts = {}
        for f in data["findings"]:
            texts[int(f["work_id"])] = texts.get(int(f["work_id"]), "") + " " + f["evidence"] + " " + f["finding"]
        n, checked, ok = insights.store_from_agent(store, corpus, run_id, agent, "paper", found, texts, call_id, grounded)
        counts.update(insights=n, evidence=checked, grounded=ok)
    else:
        pid = meta["paper_id"]
        reviewer = agent + (f" ({meta['persona']})" if meta.get("persona") else "")
        overall = (answer.get("scores") or {}).get("overall") if kind == "meta" else answer.get("overall")
        with store.db:
            store.db.execute(
                "INSERT INTO reviews (paper_id, kind, reviewer, round, content, overall, decision, call_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (pid, kind, reviewer, int(meta.get("round") or 1), json.dumps(answer, ensure_ascii=False), overall,
                 answer.get("decision"), call_id, now()))
    return counts


def _agents_used(store, run_id):
    return store.db.execute("SELECT DISTINCT json_extract(meta, '$.agent') AS agent FROM calls "
                            "WHERE run_id = ? AND kind = 'llm' AND json_extract(meta, '$.agent') IS NOT NULL",
                            (run_id,)).fetchall()


def _check(kind, answer, data) -> list[str]:
    if not isinstance(answer, dict):
        return ["the answer must be a JSON object"]
    if kind in ("paper", "revise"):
        errs = check_shape(answer, data)
        if kind == "revise":
            ids = {c["id"] for m in data.get("meta_review") or [] for c in m.get("required_changes") or []}
            answered = {r.get("change") for r in answer.get("response") or []}
            if ids - answered:
                errs.append(f"the response does not answer {sorted(ids - answered)}")
        return errs
    if kind == "audit":
        expected = {s["id"] for s in data["sentences"]}
        got = [v.get("id") for v in answer.get("verdicts") or []]
        errs = []
        if set(got) != expected or len(got) != len(set(got)):
            errs.append(f"verdicts must cover every sentence id once (missing {sorted(expected - set(got))[:8]})")
        errs += [f"{v.get('id')}: verdict {v.get('verdict')!r}" for v in answer.get("verdicts") or []
                 if v.get("verdict") not in VERDICTS]
        return errs
    if kind == "peer":
        errs = []
        for k in RUBRIC:
            if answer.get(k) not in (1, 2, 3, 4):
                errs.append(f"score {k} must be 1-4")
        if not isinstance(answer.get("overall"), int) or not 1 <= answer["overall"] <= 10:
            errs.append("overall must be 1-10")
        if answer.get("decision") not in DECISIONS:
            errs.append(f"decision must be one of {DECISIONS}")
        for k in ("summary", "strengths", "weaknesses", "requested_changes"):
            if not answer.get(k):
                errs.append(f"`{k}` is missing")
        return errs
    if kind == "meta":
        errs = []
        if answer.get("decision") not in DECISIONS:
            errs.append(f"decision must be one of {DECISIONS}")
        if not answer.get("summary"):
            errs.append("`summary` is missing")
        if "required_changes" not in answer:
            errs.append("`required_changes` is missing (may be an empty list)")
        return errs
    return [f"unknown kind {kind}"]


def overview(store: Store, run_id: int) -> list[dict]:
    """Every manuscript version of a run with its checks and reviews, for the report and the app."""
    ensure(store)
    out = []
    for p in store.db.execute("SELECT * FROM papers WHERE run_id = ? ORDER BY paper_id", (run_id,)):
        reviews = []
        for r in store.db.execute("SELECT * FROM reviews WHERE paper_id = ? ORDER BY review_id", (p["paper_id"],)):
            c = json.loads(r["content"])
            if r["kind"] == "audit":
                c = {"verdicts": c.get("verdicts", []), "counts": dict(Counter(v["verdict"] for v in c.get("verdicts", [])))}
            reviews.append({"kind": r["kind"], "reviewer": r["reviewer"], "round": r["round"], "overall": r["overall"],
                            "decision": r["decision"], "content": c})
        out.append({"id": p["paper_id"], "version": p["version"], "parent": p["parent_id"], "title": p["title"],
                    "author": p["author"], "created": p["created_at"][:10],
                    "verification": json.loads(p["verification"] or "{}").get("totals", {}),
                    "response": json.loads(p["response"] or "null"), "reviews": reviews,
                    "file": f"output/corpus_{p['corpus']}/run_{run_id:03d}/papers/paper_{p['paper_id']:02d}_v{p['version']}.md"})
    return out
