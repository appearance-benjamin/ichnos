"""Command line: python -m ichnos <command> ..."""

from __future__ import annotations

import argparse
import dataclasses
import json
import subprocess
import sys
from pathlib import Path

from . import discovery, insights
from .agents import KINDS, import_task, kept_ids, recheck_quotes, sheet_ids, task_state, write_tasks
from .fulltext import grobid_url, retrieve
from .config import ROOT, load_protocol, load_settings
from .imports import add_export, write_strings
from .pilot import import_screening, run_pilot
from .query import LIBRARY_DIALECTS
from .app import serve
from .report import refresh
from .runner import DEFAULT_ENDPOINT, RunnerError, RunSettings, run_task
from .store import Store


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="ichnos")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("pilot", help="search all sources for a corpus and draw the screening sheet")
    p.add_argument("corpus", help="corpus letter, e.g. A (reads protocol/corpus_a.toml)")
    p.add_argument("--csv-sep", default=";", help="sheet separator; ';' matches German Excel (default)")
    p.add_argument("--full-sheet", action="store_true",
                   help="put the whole pool on the sheet, including records already on an earlier sheet")

    p = sub.add_parser("import-screening", help="read decisions back from a filled screening sheet")
    p.add_argument("sheet", type=Path)
    p.add_argument("--run", type=int, help="run number (default: taken from the sheet's folder)")
    p.add_argument("--screener", default="human:reviewer", help="who screened, e.g. human:reviewer or human:coder2")

    p = sub.add_parser("report", help="rebuild a run's report from the evidence store")
    p.add_argument("run", type=int)
    p.add_argument("--out", type=Path, help="default: the run's output folder")

    p = sub.add_parser("strings", help="print the corpus search string for PsycINFO, Scopus, Web of Science "
                                       "and the API sources")
    p.add_argument("corpus")

    p = sub.add_parser("add-export", help="register a library database export (RIS, CSV, WoS text) "
                                          "for the next pilot run")
    p.add_argument("corpus")
    p.add_argument("files", nargs="+", type=Path, help="one or more export files of the same search")
    p.add_argument("--source", required=True, help="short name for this search, e.g. psycinfo, scopus, wos")
    p.add_argument("--platform", choices=LIBRARY_DIALECTS,
                   help="where you ran it; alone it means you pasted the string from `strings` unchanged")
    p.add_argument("--query-file", type=Path, help="text file with the search exactly as you ran it")
    p.add_argument("--searched", help="date of the search, YYYY-MM-DD (default: today)")
    p.add_argument("--hits", type=int, help="number of results the database reported")

    p = sub.add_parser("agent-tasks", help="write a screening or extraction task for an agent, from the protocol")
    p.add_argument("kind", choices=KINDS, help="screen: score the run's sheet on the protocol's metrics; "
                                               "extract: map the kept studies on the protocol's dimensions; "
                                               "findings: one row per finding of each kept study; "
                                               "synthesize: cross-study insights from all findings")
    p.add_argument("run", type=int)
    p.add_argument("--agent", required=True, help="who does it, e.g. model:claude-sonnet-5-5")
    p.add_argument("--batch-size", type=int, default=25)
    p.add_argument("--works", help="only these work_ids, comma-separated (default: the sheet or the kept studies)")
    p.add_argument("--paper", type=int, help="audit, peer, meta, revise: which manuscript (default: the latest)")
    p.add_argument("--papers", help="directions: several manuscripts read together, comma-separated (e.g. 16,19)")
    p.add_argument("--base", type=int, help="paper: write anew on the current evidence from this finished manuscript")
    p.add_argument("--persona", help="peer: methodologist, domain, theory or critical")
    p.add_argument("--lens", help="interpret: the lens id from protocol/lenses_corpus_X.toml")
    p.add_argument("--replicate", type=int, help="interpret: which run of the lens (1, 2, ...)")
    p.add_argument("--mode", help="lens_control: all_cards (one agent plays every lens) or no_card (no lens)")
    p.add_argument("--round", type=int, help="meta: 1 (first decision), 2 or more (after a revision; default: the version number)")
    p.add_argument("--no-fulltext", action="store_true",
                   help="eligibility, extract, findings: give agents titles and abstracts only")
    p.add_argument("--aim", help="paper: the id of the aim to write for ([aim] or one of [[aims]] in the protocol)")
    p.add_argument("--dims", help="extract: chart only these map dimensions, comma-separated (e.g. one added to the "
                                  "protocol later); the studies' other charted values stay as they are")
    p.add_argument("--second-coder", action="store_true",
                   help="extract with --dims: an independent second coding, stored apart from the charting and "
                        "compared with it (python -m ichnos patterns <run>)")

    p = sub.add_parser("fulltext", help="find, fetch and parse open-access full texts for a run's kept studies")
    p.add_argument("run", type=int)
    p.add_argument("--all", action="store_true", help="every record on the sheet, not only the kept ones")
    p.add_argument("--refresh", action="store_true", help="fetch again even if a full text is already parsed")

    p = sub.add_parser("texts", help="every text the kept studies can have, in one pass you can run again: "
                                     "identifiers by title, missing abstracts, then open full texts (with CORE and "
                                     "the Internet Archive as fallbacks)")
    p.add_argument("run", type=int)
    p.add_argument("--works", help="only these work_ids, comma-separated (default: every kept record)")
    p.add_argument("--refresh", action="store_true", help="look up and fetch again what is already settled")

    p = sub.add_parser("evidence-map", help="the evidence map as tables, one section per value of the findings' "
                                            "first matrix axis (e.g. each candidate rule); no conclusions drawn")
    p.add_argument("run", type=int)
    p.add_argument("--focus", action="store_true", help="only the protocol's [focus] rules and their precise studies "
                                                        "(written as evidence_map_focus.html)")
    p.add_argument("--aim", help="with --focus: the focus of this aim ([[aims]] with its own focus table; written as "
                                 "evidence_map_focus_<aim>.html)")

    p = sub.add_parser("agent-run", help="answer a task's batches with a local model, through an OpenAI-compatible "
                                         "server (Ollama, LM Studio, llama.cpp, vLLM)")
    p.add_argument("folder", type=Path, help="the task folder written by agent-tasks")
    p.add_argument("--model", help="the model's name on the server, e.g. qwen3:14b (default: LLM_MODEL in .env)")
    p.add_argument("--endpoint", help=f"the server's /v1 address (default: LLM_ENDPOINT in .env, else "
                                      f"{DEFAULT_ENDPOINT}, which is Ollama's)")
    p.add_argument("--parallel", type=int, default=1,
                   help="batches asked at once (vLLM serves many; Ollama on one small GPU: 1)")
    p.add_argument("--limit", type=int, help="answer only this many of the open batches, e.g. for a trial")
    p.add_argument("--retries", type=int, default=2, help="how often a failed answer is asked again, with its problems")
    p.add_argument("--max-tokens", type=int, help="the longest answer (default: the server's limit)")
    p.add_argument("--temperature", type=float, default=0.0)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--timeout", type=int, default=900, help="seconds to wait for one answer")
    p.add_argument("--no-json-mode", action="store_true",
                   help="do not ask the server for JSON output (for servers that refuse response_format)")

    p = sub.add_parser("agent-import", help="check an agent's answers and store them")
    p.add_argument("folder", type=Path, help="the task folder written by agent-tasks")
    p.add_argument("--recheck-quotes", action="store_true",
                   help="only recompute whether stored quotes occur in their records (no new import)")

    p = sub.add_parser("insight", help="capture an insight, or change one's status")
    isub = p.add_subparsers(dest="action", required=True)
    q = isub.add_parser("add", help="record an insight of your own")
    q.add_argument("corpus")
    q.add_argument("statement", help="one sentence, in quotes")
    q.add_argument("--kind", required=True, choices=insights.KINDS)
    q.add_argument("--rationale")
    q.add_argument("--works", help="work_ids it rests on, comma-separated")
    q.add_argument("--findings", help="finding ids it rests on, comma-separated")
    q.add_argument("--run", type=int)
    q.add_argument("--author", default="human:reviewer")
    q = isub.add_parser("status", help="move an insight along: " + ", ".join(insights.STATUSES))
    q.add_argument("id", type=int)
    q.add_argument("status", choices=insights.STATUSES)
    q.add_argument("--note", help="why, or what the test showed")
    q.add_argument("--author", default="human:reviewer")

    p = sub.add_parser("lenses", help="evaluate a lens tournament and write its report")
    p.add_argument("run", type=int)
    p.add_argument("--search", action="store_true", help="first run the novelty searches for imported queries")

    p = sub.add_parser("evolve", help="lens evolution: write the next tasks of the held-out evidence game, or its "
                                       "report (protocol/evolution_corpus_X.toml)")
    p.add_argument("run", type=int, nargs="?", help="the run; a pooled game's --plan and --report need none")
    p.add_argument("--game", help="the game id, where a corpus has several (or a pooled game's id)")
    p.add_argument("--plan", action="store_true", help="print the expected probes, power and agent calls; writes nothing")
    p.add_argument("--dates", action="store_true", help="fetch Crossref publication dates for a test split after the "
                                                        "forecaster's training cutoff")
    p.add_argument("--start", action="store_true", help="begin a new game (after its --plan is approved)")
    p.add_argument("--import-all", action="store_true", help="first import every answered task of the game")
    p.add_argument("--report", action="store_true", help="count and write the report instead of writing tasks")

    p = sub.add_parser("referee", help="the referee: a temporal backtest of the lenses' propositions against the studies "
                                        "published later (protocol/referee_corpus_X.toml)")
    p.add_argument("run", type=int)
    p.add_argument("--plan", action="store_true", help="print the split, the test set, the power and the agent tasks; "
                                                       "writes nothing")
    p.add_argument("--dates", action="store_true", help="fetch Crossref publication dates for the split")
    p.add_argument("--start", action="store_true", help="begin the round (after its --plan is approved)")
    p.add_argument("--import-all", action="store_true", help="first import every answered task of the round")
    p.add_argument("--report", action="store_true", help="count and write the report instead of writing tasks")

    p = sub.add_parser("library", help="list the retained records without a full text, to download through your library")
    p.add_argument("run", type=int)

    p = sub.add_parser("add-pdfs", help="match and parse the PDFs saved in fulltext/inbox or in your downloads "
                                        "folder (every run, or the one given)")
    p.add_argument("run", type=int, nargs="?")

    p = sub.add_parser("wrong-pdf", help="set aside stored PDFs that are not the record's full text (another article "
                                         "on the same pages, a page of abstracts): the records go back on the download "
                                         "list and the files are not matched to them again")
    p.add_argument("works", help="record numbers, comma-separated")
    p.add_argument("--reason", required=True, help="what the PDF is instead")

    p = sub.add_parser("pdf", help="write a manuscript as a journal-style PDF (needs Chrome or Edge)")
    p.add_argument("paper", type=int, help="paper id, as in the app's Paper tab")
    p.add_argument("--full", action="store_true", help="include the response to reviewers and the appendices")

    p = sub.add_parser("desktop", help="open Ichnos in a window of its own (no console); it stops when "
                                        "the window closes")
    p.add_argument("--reviewer", help="the name decisions are saved under (default: the one entered in the app)")

    p = sub.add_parser("shortcuts", help="put Ichnos with its icon on the desktop and in the Start menu")
    p.add_argument("--no-desktop", action="store_true", help="only the Start menu")

    p = sub.add_parser("check-pack", help="write a check pack: one HTML file in which a person checks the headline "
                                           "findings, the core studies and a blind screening sample")
    p.add_argument("run", type=int)
    p.add_argument("--paper", type=int, help="the manuscript whose headline findings to check (default: the latest)")
    p.add_argument("--sample", type=int, default=60, help="records in the blind screening sample (0: none)")
    p.add_argument("--findings-sample", type=int, default=0,
                   help="findings: also a random sample of this many of the paper's other findings, by rule")
    p.add_argument("--seed", type=int, help="seed for the sample (default: from the date)")
    p.add_argument("--parts", default="findings,core,screening", help="which parts, comma-separated (also: coding)")
    p.add_argument("--coding-works", help="coding: the work_ids to code blind, comma-separated")
    p.add_argument("--coding-dims", help="coding: the map dimensions to code (default: those with a second coding)")
    p.add_argument("--coding-note", default="", help="coding: a line for the checker on why these studies")
    p.add_argument("--out", type=Path, help="folder for the HTML file (default: the run's folder)")

    p = sub.add_parser("import-checks", help="read back a file saved from a check pack (without a file: the newest "
                                             "checks_*.json in your Downloads folder)")
    p.add_argument("file", type=Path, nargs="?")
    p.add_argument("--as", dest="checker", help="the checker's name, if the file has none or a different one")
    p.add_argument("--dry-run", action="store_true", help="show what would be stored, store nothing")

    p = sub.add_parser("mine", help="citation searching: references of excluded reviews and core studies, and "
                                     "citations of core studies, from Semantic Scholar; matches join the sheet")
    p.add_argument("run", type=int)
    p.add_argument("--via", choices=("semantic_scholar", "openalex"), default="semantic_scholar",
                   help="where the links come from; OpenAlex is much faster than Semantic Scholar without a key")

    p = sub.add_parser("update-search", help="search every source of the protocol again with the run's string (a search "
                                              "update, stored as <source>@<date>); records new to the run go on its sheet")
    p.add_argument("run", type=int)
    p.add_argument("--supplement", help="instead, the protocol's supplementary search of this id ([[supplements]]), "
                                        "with its own string, stored as <source>@<date>+<id>")
    p.add_argument("--source", action="append", help="only this source (repeatable), e.g. to retry one that failed")

    p = sub.add_parser("update", help="take the records the update searches added through the pipeline (screening, "
                                       "eligibility, charting, findings), then the forward check of the papers")
    p.add_argument("run", type=int)
    p.add_argument("--report", action="store_true", help="write what the updates found to notes/<date>-update-corpus-X.md")

    p = sub.add_parser("add-sources", help="search the protocol's sources a run has not searched yet (one added "
                                            "to the protocol later); the new records join the run's sheet")
    p.add_argument("run", type=int)

    p = sub.add_parser("recall", help="recall test: fetch the reference lists of the protocol's [recall] sources and "
                                       "count how many of the studies they cite the search found")
    p.add_argument("run", type=int)
    p.add_argument("--report", action="store_true", help="only count (after the agents judged the missed references)")
    p.add_argument("--translations", action="store_true", help="add the records that the protocol's non-English "
                                                                "[recall] trial_terms find in OpenAlex to the run's sheet")

    p = sub.add_parser("abstracts", help="look up missing abstracts of the retained records by DOI or PubMed ID "
                                          "(PubMed, Europe PMC, Crossref, Semantic Scholar, OpenAlex, CORE); "
                                          "records without an identifier are first matched in OpenAlex by title")
    p.add_argument("run", type=int)
    p.add_argument("--works", help="only these work_ids, comma-separated (default: every retained record)")
    p.add_argument("--refresh", action="store_true", help="look up records already looked up again")

    p = sub.add_parser("biblio", help="look up volume, issue and pages of the retained records in Crossref")
    p.add_argument("run", type=int)
    p.add_argument("--refresh", action="store_true", help="look up records already looked up again")

    p = sub.add_parser("patterns", help="check a run's pre-registered patterns ([[patterns]]) and count them on its "
                                         "coding; with --comparator, also its predictions on the comparator's run")
    p.add_argument("run", type=int)
    p.add_argument("--comparator", type=int, help="the run of the comparator corpus ([comparator] corpus)")
    p.add_argument("--check", action="store_true", help="only check that the patterns can be counted as written")
    p.add_argument("--second", action="store_true",
                   help="also count them on the second coder's coding (a robustness check, not pre-registered)")

    p = sub.add_parser("insights", help="write the insight log (notes/insights_corpus_X.md)")
    p.add_argument("corpus")

    p = sub.add_parser("discover", help="find candidate directions in every project's findings, charting and insights, "
                                         "by code: contradictions, gaps, bridges between projects, recurring "
                                         "insights and rising topics (notes/discoveries.md, the app's Discovery page)")
    p.add_argument("--kind", choices=discovery.KINDS, help="only print this kind")
    p.add_argument("--corpus", help="only print candidates that touch this corpus")
    p.add_argument("--top", type=int, default=5, help="candidates printed per kind (default 5)")
    p.add_argument("--no-sweep", action="store_true", help="print the stored candidates without looking again")
    p.add_argument("--task", choices=discovery.TASK_KINDS,
                   help="instead, write an agent task for the chosen discoveries: discovery_score (statement, value, "
                        "feasibility, artefact risk), novelty_queries (search strings) or novelty_judge (label the hits)")
    p.add_argument("--agent", default=discovery.DEFAULT_AGENT, help="with --task: who does it")
    p.add_argument("--ids", help="with --task discovery_score: these discoveries (default: every chosen one not scored "
                                 "by this agent yet), comma-separated")
    p.add_argument("--search", action="store_true", help="instead, run the novelty searches of the imported search "
                                                         "strings (OpenAlex and Semantic Scholar)")

    p = sub.add_parser("discovery", help="choose a discovery to explore, set it aside, or open it again")
    p.add_argument("id", type=int)
    p.add_argument("status", choices=discovery.STATUSES)
    p.add_argument("--note", help="why, or what to do with it")
    p.add_argument("--author", default="human:reviewer")

    p = sub.add_parser("app", help="open the app in your browser (no terminal needed after this)")
    p.add_argument("--port", type=int, default=8766)
    p.add_argument("--no-browser", action="store_true", help="do not open the browser automatically")
    p.add_argument("--db", type=Path, help="use another evidence store, e.g. a copy for practice or testing")

    args = parser.parse_args(argv)
    settings = load_settings()

    if args.command in ("report", "agent-tasks"):
        _register_protocol(Store(settings.db_path), args.run)

    if args.command == "strings":
        path, text = write_strings(args.corpus.upper())
        print(text)
        print(f"Saved: {path}")
    elif args.command == "add-export":
        try:
            add_export(args.corpus.upper(), args.files, args.source, args.platform, args.query_file,
                       args.searched, args.hits)
        except (OSError, ValueError) as err:
            parser.exit(1, f"add-export: {err}\n")
    elif args.command == "pilot":
        run_pilot(args.corpus.upper(), settings, csv_sep=args.csv_sep, full_sheet=args.full_sheet)
    elif args.command == "import-screening":
        import_screening(args.sheet, settings, args.run, args.screener)
    elif args.command == "report":
        store = Store(settings.db_path)
        run = store.db.execute("SELECT corpus FROM runs WHERE run_id = ?", (args.run,)).fetchone()
        if run is None:
            parser.error(f"no run {args.run}")
        out = _refresh(store, args.run, args.out)
        print(f"Report: {out}\nReview page: {out.parent / 'review.html'}")
    elif args.command == "agent-tasks":
        store = Store(settings.db_path)
        try:
            works = [int(w) for w in args.works.split(",") if w.strip()] if args.works else None
            options = {k: v for k, v in (("paper", args.paper), ("persona", args.persona), ("round", args.round),
                                         ("lens", args.lens), ("replicate", args.replicate), ("mode", args.mode),
                                         ("dims", args.dims), ("second_coder", args.second_coder),
                                         ("aim", args.aim), ("base", args.base)) if v}
            if args.papers:
                options["paper"] = [int(x) for x in args.papers.split(",") if x.strip()]
            folder = write_tasks(store, args.run, args.kind, args.agent, batch_size=args.batch_size, work_ids=works,
                                 use_fulltext=not args.no_fulltext, options=options)
        except ValueError as err:
            parser.exit(1, f"agent-tasks: {err}\n")
        meta = (folder / "task.json").read_text(encoding="utf-8")
        print(f"Task written: {folder}\n{meta}\nEach batch_NN.json needs a batch_NN_result.json next to it; "
              f"then run: python -m ichnos agent-import \"{folder}\"")
    elif args.command == "fulltext":
        store = Store(settings.db_path)
        run = store.db.execute("SELECT corpus FROM runs WHERE run_id = ?", (args.run,)).fetchone()
        if run is None:
            parser.error(f"no run {args.run}")
        ids = sheet_ids(store, args.run) if args.all else kept_ids(store, args.run)
        grobid = grobid_url(settings)
        print(f"{len(ids)} records; PDFs parsed with {'GROBID at ' + grobid if grobid else 'pypdf (GROBID not running)'}; "
              f"Unpaywall {'on' if settings.contact_email else 'off (no CONTACT_EMAIL in .env)'}")
        counts = retrieve(store, ids, settings, run["corpus"], args.run, refresh=args.refresh)
        out = _refresh(store, args.run)
        print("Done: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) + f"\nReport: {out}")
    elif args.command == "texts":
        from . import texts
        store = Store(settings.db_path)
        run = store.db.execute("SELECT corpus FROM runs WHERE run_id = ?", (args.run,)).fetchone()
        if run is None:
            parser.error(f"no run {args.run}")
        works = [int(w) for w in args.works.split(",") if w.strip()] if args.works else None
        grobid = grobid_url(settings)
        print(f"PDFs parsed with {'GROBID at ' + grobid if grobid else 'pypdf (GROBID not running)'}; "
              f"Unpaywall {'on' if settings.contact_email else 'off (no CONTACT_EMAIL in .env)'}; "
              f"CORE {'with a key' if settings.core_api_key else 'without a key (about 10 requests a minute)'}")
        out = texts.run(store, args.run, settings, run["corpus"], works, refresh=args.refresh)
        for step in ("identifiers", "abstracts", "fulltexts"):
            print(f"{step.capitalize()}: " + (", ".join(f"{k} {v}" for k, v in sorted(out[step].items())) or "none needed"))
        c = out["coverage"]
        print(f"Now: {c['abstract']} of {c['records']} records have an abstract, {c['fulltext']} a full text; "
              f"{c['neither']} have neither.")
        if out["duplicates"]:
            print(f"Duplicate records found by title ({len(out['duplicates'])}): "
                  + ", ".join(f"#{a} = #{b}" for a, b in out["duplicates"]))
        report = _refresh(store, args.run)
        print(f"Report: {report}\nThe rest is behind paywalls: python -m ichnos library {args.run} lists it, "
              f"with links, for your library.")
    elif args.command == "evidence-map":
        from . import checks, rulemap
        store = Store(settings.db_path)
        run = store.db.execute("SELECT corpus FROM runs WHERE run_id = ?", (args.run,)).fetchone()
        if run is None:
            parser.error(f"no run {args.run}")
        focus = None
        if args.focus:
            # [focus] is a view, not a protocol of the run: it is read from the protocol file as it stands now
            from .aim import with_aim
            focus = with_aim(load_protocol(run["corpus"])[2], args.aim).get("focus")
            if not focus:
                parser.error(f"protocol/corpus_{run['corpus'].lower()}.toml has no [focus]")
        md, page = rulemap.write(store, args.run, checks.current_protocol(store, run["corpus"]), focus=focus,
                                 name=f"evidence_map_focus_{args.aim}" if args.focus and args.aim else None)
        print(f"Evidence map: {page}\n(Markdown: {md})")
    elif args.command == "agent-run":
        model = args.model or settings.llm_model
        if not model:
            parser.error("agent-run needs --model (or LLM_MODEL in .env), e.g. --model qwen3:14b")
        endpoint = args.endpoint or settings.llm_endpoint or DEFAULT_ENDPOINT
        meta = json.loads((args.folder / "task.json").read_text(encoding="utf-8"))
        print(f"{meta['kind']} task for {meta['agent']}, {meta.get('batches', '?')} batches: {model} at {endpoint}")
        try:
            counts = run_task(args.folder, Store(settings.db_path),
                              RunSettings(endpoint, model, temperature=args.temperature, seed=args.seed,
                                          max_tokens=args.max_tokens, json_mode=not args.no_json_mode,
                                          retries=args.retries, timeout=args.timeout, api_key=settings.llm_api_key),
                              parallel=args.parallel, limit=args.limit)
        except RunnerError as err:
            parser.exit(1, f"agent-run: {err}\n")
        print(f"Answered {counts['answered']}, failed {counts['failed']}, answered before {counts['already']}. "
              f"Log: {args.folder / 'runner.json'}")
        if counts["failed"]:
            print("Failed (run the same command again to ask again):\n  " + "\n  ".join(
                f"{name}: {problems[0] if problems else '?'}" for name, problems in counts["failures"][:20]))
        if task_state(args.folder) == "answered":
            print(f"Every batch is answered; next: python -m ichnos agent-import \"{args.folder}\"")
        return 1 if counts["failed"] else 0
    elif args.command == "agent-import" and args.recheck_quotes:
        store = Store(settings.db_path)
        counts = recheck_quotes(args.folder, store)
        _refresh(store, json.loads((args.folder / "task.json").read_text(encoding="utf-8"))["run_id"])
        print(f"Rechecked {counts['checked']} quotes: {counts['grounded']} found, {counts['changed']} flags changed.")
    elif args.command == "agent-import":
        store = Store(settings.db_path)
        counts = import_task(args.folder, store)
        if counts["problems"]:
            print("Nothing stored; fix these first:\n  " + "\n  ".join(counts["problems"][:40]))
            return 1
        meta = json.loads((args.folder / "task.json").read_text(encoding="utf-8"))
        if meta.get("scope") == "discovery":
            print(f"Stored {counts['stored']} answers.\n"
                  f"Log: {discovery.write_log(store, ROOT / 'notes' / 'discoveries.md')}")
            return 0
        out = _refresh(store, meta["run_id"])
        extra = ", ".join(f"{k} {v}" for k, v in counts.items()
                          if k not in ("stored", "insights", "grounded", "evidence", "problems") and not isinstance(v, dict))
        print(f"Stored {counts['stored']} answers and {counts.get('insights', 0)} insights; "
              f"{counts.get('grounded', 0)} of {counts.get('evidence', 0)} quotes found verbatim in their records."
              + (f" ({extra})" if extra else "") + f"\nReport: {out}\nReview page: {out.parent / 'review.html'}")
    elif args.command == "insight":
        store = Store(settings.db_path)
        try:
            if args.action == "add":
                ids = lambda s: [int(x) for x in s.split(",") if x.strip()] if s else []  # noqa: E731
                new = insights.add(store, args.corpus.upper(), args.author, "manual", args.kind, args.statement,
                                   args.rationale, ids(args.works), ids(args.findings), run_id=args.run)
                corpus = args.corpus.upper()
                print(f"Insight #{new} noted.")
            else:
                insights.set_status(store, args.id, args.status, args.author, args.note)
                corpus = store.db.execute("SELECT corpus FROM insights WHERE insight_id = ?",
                                          (args.id,)).fetchone()["corpus"]
                print(f"Insight #{args.id} is now {args.status}.")
        except ValueError as err:
            parser.exit(1, f"insight: {err}\n")
        print(f"Log: {insights.write_log(store, corpus, _log_path(corpus))}")
    elif args.command == "app":
        if args.db:
            settings = dataclasses.replace(settings, db_path=args.db)
        serve(settings, ROOT, args.port, not args.no_browser, settings.reviewer)
    elif args.command == "lenses":
        from . import lenses
        store = Store(settings.db_path)
        corpus = store.db.execute("SELECT corpus FROM runs WHERE run_id = ?", (args.run,)).fetchone()["corpus"]
        if args.search:
            from . import novelty
            _, _, cfg = lenses.load(corpus, ROOT)
            tour = cfg["interpret"].get("tournament", "pilot")
            counts = novelty.search(store, args.run, settings, tour)
            print("Novelty searches: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
            filled = novelty.fill_plain(store, args.run, settings, tour)
            if filled:
                print("Plain searches redone on OpenAlex: " + ", ".join(f"{k} {v}" for k, v in sorted(filled.items())))
        result = lenses.evaluate(store, args.run, ROOT)
        path = ROOT / "notes" / f"interpretation_corpus_{corpus}.md"
        path.write_text(lenses.report(result, store, args.run), encoding="utf-8")
        (path.with_suffix(".json")).write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        from .document import to_html
        path.with_suffix(".html").write_text(to_html(path.read_text(encoding="utf-8"), "Lens pilot"), encoding="utf-8")
        print(f"Report: {path}")
    elif args.command == "evolve":
        from . import evolution
        store = Store(settings.db_path)
        if args.plan:
            print(evolution.plan_text(evolution.plan(store, ROOT, args.game, args.run)))
            return 0
        if args.run is None:
            if not (args.report and args.game):
                parser.error("evolve needs a run (only a pooled game's --plan and --report go without)")
            result = evolution.evaluate_pool(store, args.game, ROOT)
            for r in result["results"]:
                path = ROOT / "notes" / f"lens_evolution_{r['game']}.md"
                path.write_text(evolution.report(r, store, store.db.execute(
                    "SELECT run_id FROM evo_games WHERE game = ?", (r["game"],)).fetchone()[0]), encoding="utf-8")
                print(f"Report: {path}")
            path = ROOT / "notes" / f"lens_evolution_{args.game}.md"
            path.write_text(evolution.pool_report(result), encoding="utf-8")
            path.with_suffix(".json").write_text(json.dumps(result, ensure_ascii=False, indent=1, default=str),
                                                 encoding="utf-8")
            print(f"Pooled report: {path}")
            return 0
        corpus = store.db.execute("SELECT corpus FROM runs WHERE run_id = ?", (args.run,)).fetchone()["corpus"]
        if args.dates:
            _, _, cfg = evolution.load(corpus, ROOT, args.game, args.run)
            if (cfg.get("split") or {}).get("test") != "after_cutoff":
                parser.error(f"game {cfg['game']} splits by year; it needs no dates")
            counts = evolution.fetch_dates(store, args.run, cfg)
            print("Publication dates: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
            return 0
        if args.import_all:
            g = evolution.game(store, args.run, ROOT, args.game, start=False)
            for t in sorted(evolution.tasks(store, g["game"]).values(), key=lambda t: t["folder"]):
                folder = ROOT / t["folder"]
                if t["status"] == "open" and any(folder.glob("batch_*_result.json")):
                    counts = import_task(folder, store)
                    print(f"{folder.name}: " + ("stored " + str(counts["stored"]) if not counts["problems"] else
                                                "not stored:\n  " + "\n  ".join(counts["problems"][:20])))
        if args.report:
            result = evolution.evaluate(store, args.run, ROOT, args.game)
            own = result["config_path"] == f"protocol/evolution_corpus_{corpus.lower()}.toml"
            path = ROOT / "notes" / (f"lens_evolution_corpus_{corpus}.md" if own else f"lens_evolution_{result['game']}.md")
            path.write_text(evolution.report(result, store, args.run), encoding="utf-8")
            path.with_suffix(".json").write_text(json.dumps(result, ensure_ascii=False, indent=1, default=str),
                                                 encoding="utf-8")
            from .document import to_html
            path.with_suffix(".html").write_text(to_html(path.read_text(encoding="utf-8"), "Lens evolution"),
                                                 encoding="utf-8")
            sheet, key = evolution.rating_sheet(result, store)
            if key:
                out = ROOT / "output" / f"corpus_{corpus}" / f"run_{args.run:03d}" / (
                    "evolution_rating_sheet.md" if own else f"evolution_rating_sheet_{result['game']}.md")
                out.write_text(sheet, encoding="utf-8")
                print(f"Blind rating sheet: {out}")
            print(f"Report: {path}")
            return 0
        status = evolution.step(store, args.run, ROOT, args.game, start=args.start)
        for f in status["written"]:
            print(f"Wrote {f}")
        print(f"Game {status['game']}: {status['imported']} tasks imported, {len(status['open'])} open"
              + (", test opened" if status["test_opened"] else "") + (". Done: write the report with --report."
                                                                      if status["done"] else "."))
        for f in status["open"]:
            print(f"  open: {f}")
    elif args.command == "referee":
        from . import referee
        store = Store(settings.db_path)
        try:
            if args.plan:
                print(referee.plan_text(referee.plan(store, args.run, ROOT)))
                return 0
            if args.dates:
                counts = referee.fetch_dates(store, args.run, ROOT)
                print("Publication dates: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
                return 0
            if args.import_all:
                r = referee.round_(store, args.run, ROOT)
                for t in sorted(referee.tasks(store, r["round"]).values(), key=lambda t: t["folder"]):
                    folder = ROOT / t["folder"]
                    if t["status"] == "open" and any(folder.glob("batch_*_result.json")):
                        counts = import_task(folder, store)
                        print(f"{folder.name}: " + ("stored " + str(counts["stored"]) if not counts["problems"] else
                                                    "not stored:\n  " + "\n  ".join(counts["problems"][:20])))
            if args.report:
                result = referee.evaluate(store, args.run, ROOT)
                path = ROOT / "notes" / f"referee_corpus_{result['corpus']}.md"
                path.write_text(referee.report(result), encoding="utf-8")
                path.with_suffix(".json").write_text(json.dumps(result, ensure_ascii=False, indent=1, default=str),
                                                     encoding="utf-8")
                print(f"Report: {path}")
                return 0
            status = referee.step(store, args.run, ROOT, start=args.start)
        except ValueError as err:
            parser.exit(1, f"referee: {err}\n")
        for f in status["written"]:
            print(f"Wrote {f}")
        print(f"Round {status['round']}: {status['imported']} tasks imported, {len(status['open'])} open"
              + (f"; propositions frozen at {status['frozen_at']}, sha256 {status['frozen_sha256'][:16]}…"
                 if status["frozen_at"] else "; propositions not frozen yet")
              + (". Done: write the report with --report." if status["done"] else "."))
        for f in status["open"]:
            print(f"  open: {f}")
    elif args.command in ("library", "add-pdfs"):
        from . import checks, library
        store = Store(settings.db_path)

        def todo_list(run):
            corpus = store.db.execute("SELECT corpus FROM runs WHERE run_id = ?", (run,)).fetchone()["corpus"]
            md, page, n = library.write_todo(store, run, checks.current_protocol(store, corpus), ROOT, settings)
            print(f"Run {run}: {n} retained records still without a full text: {page}")
        if args.command == "library":
            todo_list(args.run)
        else:
            if settings.downloads_dir:
                print(f"Looking in {ROOT / library.INBOX} and {settings.downloads_dir} (DOWNLOADS_DIR=off to skip)")
            result = library.add_all(store, settings, ROOT, runs=[args.run] if args.run else None)
            unmatched = result.pop("unmatched_inbox")
            for run, counts in result.items():
                if not counts["gained"]:
                    continue
                _refresh(store, run)
                todo_list(run)
                print(f"Run {run}: {len(counts['gained'])} retained records gained a full text. To read them:")
                for cmd in library.next_steps(store, run, counts["gained"]):
                    print(f"  {cmd}")
            read = sum(c["downloads_read"] for c in result.values())
            print(f"Added {len({w for c in result.values() for w in c['added']})} PDFs; {read} downloads read; "
                  f"{len(unmatched)} inbox files matched no record.")
    elif args.command == "wrong-pdf":
        from . import library
        store = Store(settings.db_path)
        for w in [int(x) for x in args.works.split(",") if x.strip()]:
            try:
                library.reject(store, w, args.reason, ROOT)
            except ValueError as err:
                parser.exit(1, f"wrong-pdf: {err}\n")
            print(f"#{w}: its PDF is set aside as not its full text; it is back on the download list")
    elif args.command == "pdf":
        from . import paper as papers
        store = Store(settings.db_path)
        try:
            print(f"PDF: {papers.write_pdf(store, args.paper, ROOT, args.full)}")
            row = store.db.execute("SELECT method FROM papers WHERE paper_id = ?", (args.paper,)).fetchone()
            if row and json.loads(row["method"]).get("aim"):     # a focused paper's supplement is its own file
                print(f"Supplement: {papers.write_supplement_pdf(store, args.paper, ROOT)}")
        except (RuntimeError, TypeError, subprocess.SubprocessError) as err:
            parser.exit(1, f"pdf: {err}\n")
    elif args.command == "desktop":
        from . import desktop
        desktop.run(settings, ROOT, args.reviewer or settings.reviewer)
    elif args.command == "shortcuts":
        from . import desktop
        for path in desktop.shortcuts(ROOT, desktop=not args.no_desktop):
            print(f"Shortcut: {path}")
    elif args.command == "check-pack":
        from . import checkpack
        store = Store(settings.db_path)
        run = store.db.execute("SELECT corpus FROM runs WHERE run_id = ?", (args.run,)).fetchone()
        if run is None:
            raise SystemExit(f"no run {args.run}")
        _, _, protocol = load_protocol(run["corpus"])
        out = args.out or ROOT / "output" / f"corpus_{run['corpus']}" / f"run_{args.run:03d}" / "check_packs"
        coding_works = [int(w) for w in (args.coding_works or "").split(",") if w.strip()] or None
        coding_dims = [d.strip() for d in (args.coding_dims or "").split(",") if d.strip()] or None
        path = checkpack.write(store, args.run, protocol, out, paper_id=args.paper, sample=args.sample, seed=args.seed,
                               parts=tuple(p.strip() for p in args.parts.split(",") if p.strip()),
                               coding_works=coding_works, coding_dims=coding_dims, coding_note=args.coding_note,
                               findings_sample=args.findings_sample)
        print(f"Check pack: {path}")
        print("Send this one file to the person who checks; it opens in any browser. They send back the file "
              "'Download my checks' saves, and you run: python -m ichnos import-checks <that file>")
    elif args.command == "import-checks":
        from . import checkpack
        try:
            path = checkpack.find_checks_file(args.file)
        except FileNotFoundError as err:
            raise SystemExit(str(err))
        print(f"Importing {path}")
        store = Store(settings.db_path)
        try:
            out = checkpack.import_file(store, path, args.checker, args.dry_run)
        except ValueError as err:
            raise SystemExit(f"Not imported: {err}")
        c = out["counts"]
        print(("Would store" if out.get("dry_run") else "Stored") + f" for {out['checker']}: {c['findings']} finding "
              f"checks, {c['core']} core-study checks, {c['screening']} screening decisions, "
              f"{c.get('coding', 0)} coded studies.")
        for agent, v in out["agreement"].items():
            print(f"  agreement with {agent}: {v['agree']} of {v['n']}"
                  + (f", kappa {v['kappa']}" if v["kappa"] is not None else ""))
        for dim, row in (out.get("coding_agreement") or {}).items():
            print(f"  {dim}: " + "; ".join(
                f"{coder} coder {v['exact']} of {v['n']} studies identical"
                + (f", kappa {v['kappa']}" if v["kappa"] is not None else "") for coder, v in row.items()))
    elif args.command == "mine":
        from . import mining
        store = Store(settings.db_path)
        corpus = store.db.execute("SELECT corpus FROM runs WHERE run_id = ?", (args.run,)).fetchone()["corpus"]
        _, _, protocol = load_protocol(corpus)
        out = mining.mine(store, args.run, protocol, settings.s2_api_key, via=args.via,
                          openalex_key_=settings.openalex_api_key, contact_email=settings.contact_email)
        print(", ".join(f"{k} {v}" for k, v in out.items() if k != "new_ids"))
        if out["new_ids"]:
            print(f"New records on the sheet ({len(out['new_ids'])}), to screen with --works: "
                  + ",".join(map(str, out["new_ids"])))
    elif args.command == "add-sources":
        from .pilot import add_sources
        store = Store(settings.db_path)
        new = add_sources(store, args.run, settings, ROOT)
        if new:
            print(f"New records on the sheet ({len(new)}), to screen with --works: " + ",".join(map(str, new)))
    elif args.command == "update-search":
        from . import update
        store = Store(settings.db_path)
        try:
            new = update.search(store, args.run, settings, ROOT, supplement=args.supplement, only=args.source)
        except ValueError as err:
            parser.exit(1, f"update-search: {err}\n")
        print(f"{len(new)} records new to run {args.run}" + (": " + ",".join(map(str, new)) if new else "")
              + f". Next: python -m ichnos update {args.run}")
    elif args.command == "update":
        from . import referee, update
        from .store import now
        store = Store(settings.db_path)
        try:
            if args.report:
                corpus = store.db.execute("SELECT corpus FROM runs WHERE run_id = ?", (args.run,)).fetchone()["corpus"]
                path = ROOT / "notes" / f"{now()[:10]}-update-corpus-{corpus}.md"
                path.write_text(update.report(update.summary(store, args.run, ROOT)), encoding="utf-8")
                print(f"Report: {path}")
                return 0
            status = update.step(store, args.run, ROOT)
        except ValueError as err:
            parser.exit(1, f"update: {err}\n")
        print(f"Records the update searches added: {len(status['added'])}")
        for name, s in status["stages"].items():
            print(f"  {name}: {s['done']} of {s['records']} done")
        for f in status["written"]:
            print(f"Wrote {f}")
        for f in status["open"]:
            print(f"  open: {f}")
        if status["waiting"]:
            print(f"{status['waiting']} records wait: answer the open tasks, import them with agent-import, and run "
                  f"`update {args.run}` again.")
            return 0
        try:
            referee.round_(store, args.run, ROOT)
        except ValueError:
            print("Every added record is through the pipeline; this run has no forward check (referee round).")
            return 0
        counts = referee.fetch_dates(store, args.run, ROOT)
        print("Every added record is through the pipeline. Publication dates: "
              + (", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "none needed"))
        r = referee.step(store, args.run, ROOT)
        for f in r["written"]:
            print(f"Wrote {f}")
        print(f"Forward check {r['round']}: {len(r['open'])} tasks open" + ("; done" if r["done"] else "")
              + "".join(f"\n  open: {f}" for f in r["open"]))
    elif args.command == "recall":
        from . import recall
        store = Store(settings.db_path)
        corpus = store.db.execute("SELECT corpus FROM runs WHERE run_id = ?", (args.run,)).fetchone()["corpus"]
        _, _, protocol = load_protocol(corpus)
        if args.translations:
            new = recall.add_translations(store, args.run, protocol, openalex_key=settings.openalex_api_key,
                                          contact_email=settings.contact_email)
            print(f"New records on the sheet ({len(new)}), to screen with --works: " + ",".join(map(str, new)))
            return 0
        if not args.report:
            counts = recall.fetch(store, args.run, protocol, openalex_key=settings.openalex_api_key,
                                  contact_email=settings.contact_email)
            print("References: " + ", ".join(f"{k} {v}" for k, v in counts.items()))
            print(f"Not found by the search, for agents to judge: {len(recall.to_judge(store, args.run, protocol))} "
                  f"(python -m ichnos agent-tasks recall {args.run} --agent ...)")
            for row in recall.trial(store, args.run, protocol, openalex_key=settings.openalex_api_key,
                                    contact_email=settings.contact_email):
                print(f"Trial, {row['label']}: {row['total']} records in OpenAlex"
                      + (f"; {row['in_pool']} of {row['fetched']} fetched were found by the search" if row["fetched"]
                         is not None else ""))
        print(json.dumps({k: v for k, v in recall.summary(store, args.run, protocol).items()
                          if not k.endswith("_ids")}, indent=1))
    elif args.command == "abstracts":
        from . import abstracts, resolve
        store = Store(settings.db_path)
        works = [int(w) for w in args.works.split(",") if w.strip()] if args.works else None
        ids = resolve.resolve(store, args.run, works, openalex_key=settings.openalex_api_key,
                              contact_email=settings.contact_email, refresh=args.refresh)
        if ids:
            print("Identifiers by title: " + ", ".join(f"{k} {v}" for k, v in sorted(ids.items())))
        counts = abstracts.fill(store, args.run, works, contact_email=settings.contact_email,
                                openalex_key=settings.openalex_api_key, refresh=args.refresh,
                                s2_key=settings.s2_api_key, core_key=settings.core_api_key)
        print("Abstracts: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
        done = abstracts.summary(store, args.run)
        if done["ids"]:
            print(f"Records that gained an abstract ({len(done['ids'])}), to judge again with --works: "
                  + ",".join(map(str, done["ids"])))
    elif args.command == "biblio":
        from . import biblio
        store = Store(settings.db_path)
        counts = biblio.fetch(store, args.run, kept_ids(store, args.run), refresh=args.refresh)
        print("Crossref: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    elif args.command == "patterns":
        from . import patterns
        store = Store(settings.db_path)
        corpus = store.db.execute("SELECT corpus FROM runs WHERE run_id = ?", (args.run,)).fetchone()["corpus"]
        errs = patterns.validate(corpus, ROOT, store)
        for e in errs:
            print(f"Problem: {e}")
        if errs:
            return 1
        _, _, protocol = load_protocol(corpus)
        print(f"The {len(protocol.get('patterns') or [])} patterns of corpus {corpus} can be counted as written.")
        if not args.check:
            print(patterns.agreement_report(patterns.agreement(store, args.run, protocol)))
            print(patterns.report(patterns.count(store, args.run, protocol)))
            if args.second:
                print(patterns.report(patterns.count(store, args.run, protocol, second=True),
                                      "Robustness: the same patterns on the second coder's coding (not pre-registered)"))
            comp = protocol.get("comparator") or {}
            if args.comparator and comp.get("predictions"):
                _, _, other = load_protocol(comp["corpus"])
                print(patterns.report(patterns.count(store, args.comparator, other, comp["predictions"], protocol),
                                      f"Predictions on corpus {comp['corpus']} ({comp.get('title', '')})"))
    elif args.command == "insights":
        store = Store(settings.db_path)
        corpus = args.corpus.upper()
        for it in insights.rows(store, corpus):
            if json.loads(it["support"] or "{}").get("findings"):
                insights.verify_numbers(store, it["insight_id"])
        items = insights.rows(store, corpus)
        print(f"{len(items)} insights: " + ", ".join(f"{s} {sum(i['status'] == s for i in items)}"
                                                      for s in insights.STATUSES if any(i['status'] == s for i in items)))
        print(f"Log: {insights.write_log(store, corpus, _log_path(corpus))}")
    elif args.command == "discover" and args.task:
        store = Store(settings.db_path)
        try:
            ids = [int(x) for x in args.ids.split(",") if x.strip()] if args.ids else None
            folder = discovery.write_task(store, args.task, args.agent, ROOT, ids)
        except ValueError as err:
            parser.exit(1, f"discover: {err}\n")
        print(f"Task written: {folder}\n{(folder / 'task.json').read_text(encoding='utf-8')}\nEach batch_NN.json needs a "
              f"batch_NN_result.json next to it; then run: python -m ichnos agent-import \"{folder}\"")
    elif args.command == "discover" and args.search:
        store = Store(settings.db_path)
        counts = discovery.search(store, settings)
        print("Novelty searches: " + (", ".join(f"{k} {v}" for k, v in sorted(counts.items())) or "nothing to search"))
    elif args.command == "discover":
        store = Store(settings.db_path)
        if not args.no_sweep:
            counts = discovery.sweep(store, ROOT)
            print(f"Found {counts.pop('found')} candidates: "
                  + ", ".join(f"{discovery.KIND_LABELS[k].lower()} {counts[k]}" for k in discovery.KINDS if k in counts))
        items = [d for d in discovery.rows(store) if (not args.kind or d["kind"] == args.kind)
                 and (not args.corpus or args.corpus.upper() in d["corpora"])]
        for kind in discovery.KINDS:
            these = [d for d in items if d["kind"] == kind]
            if these:
                print(f"\n{discovery.KIND_LABELS[kind]} ({len(these)})")
            for d in these[:args.top]:
                print(f"  #{d['discovery_id']} [{','.join(d['corpora'])}] {d['title']}"
                      + ("" if d["status"] == "open" else f" ({d['status'].replace('_', ' ')})")
                      + f"\n      {d['summary']}")
        print(f"\nLog: {discovery.write_log(store, ROOT / 'notes' / 'discoveries.md')}\n"
              "Choose in the app (Discovery) or with: python -m ichnos discovery <id> chosen --note \"...\"")
    elif args.command == "discovery":
        store = Store(settings.db_path)
        try:
            discovery.set_status(store, args.id, args.status, args.author, args.note)
        except ValueError as err:
            parser.exit(1, f"discovery: {err}\n")
        print(f"Discovery #{args.id} is now {args.status.replace('_', ' ')}.\n"
              f"Log: {discovery.write_log(store, ROOT / 'notes' / 'discoveries.md')}")
    return 0


def _log_path(corpus: str) -> Path:
    return ROOT / "notes" / f"insights_corpus_{corpus}.md"


def _refresh(store: Store, run_id: int, out: Path | None = None) -> Path:
    """Rebuild a run's report and review page, and the corpus's insight log."""
    return refresh(store, run_id, ROOT, out)


def _register_protocol(store: Store, run_id: int) -> None:
    """Record the protocol file as it is now, so reports and tasks use the latest metrics, map and aliases."""
    run = store.db.execute("SELECT corpus FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if run is None:
        return
    try:
        path, text, _ = load_protocol(run["corpus"])
    except OSError:
        return
    store.add_protocol(run["corpus"], path.relative_to(ROOT).as_posix(), text)


if __name__ == "__main__":
    raise SystemExit(main())
