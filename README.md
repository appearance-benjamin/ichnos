# Ichnos

An auditable evidence pipeline for the conceptual review *When Automation Takes
the Work*. This is phase 1 of the blueprint: retrieval from OpenAlex, Semantic
Scholar, PubMed and Europe PMC, plus exports from library databases (APA
PsycINFO, Scopus, Web of Science), into one SQLite evidence store, and a pilot
on corpus A (analog settings) for tuning the search string.

To start a review of your own, copy [examples/corpus_a.toml](examples/corpus_a.toml) to
`protocol/corpus_a.toml` and edit it.

## The app (no terminal needed)

Ichnos is a desktop app. Once, from this folder:

```bash
python -m ichnos shortcuts
```

puts **Ichnos**, with its icon, on the desktop and in the Start menu.
Opening it starts the app without a console and shows it in a window of its own
(Edge or Chrome in app mode: no tabs, no address bar). The app stops by itself a
few seconds after you close the window; opening it while it runs just opens
another window. Double-clicking **`Ichnos.bat`** does the same, and
`python -m ichnos desktop` starts it from a terminal. It runs only on
your computer; what it writes goes to `output/app.log`.

**Projects.** The app opens on your projects, one card per corpus. Each card
shows:
- its owner, its progress, and the latest manuscript with the editor's decision;
- the first things that wait for you there.

Below the cards, **Everything that needs you** lists every open item across the
projects, grouped by project, and each line opens the page where you do it.
Names, owners and each project's main run live in `protocol/projects.toml`.
No review protocol reads that file, so editing it changes no protocol hash. Set
`archived = true` there to move a project to the bottom of the start page. A
corpus without an entry still shows up, under its protocol's title.

Inside a project, the top of the sidebar has:
- **← All projects**, which takes you back to the start page;
- the project's name and owner;
- a switcher to another project, or to an earlier run of the same project
  (corpus A's runs 1 and 3, for example).

Pages with nothing in them for that project are left out, for example
Interpretations without a lens file. Where a next step can be done from the app,
its button does it. A failed search, or a source in the protocol that no run
has searched yet, gets **Run the search**: the new records go on the sheet.

The sidebar follows the review: **Home**, then *Screen and select* (Records),
*Evidence* (Evidence map, Findings, Insights), *Interpret* (Interpretations),
*Write* (Draft, Manuscripts),
*Verify* (Check findings), *Pipeline* (Agent tasks) and *Help*. Numbers next to
a page say how much is there or, in orange, how much waits for you.
`Alt`+`1`…`9` jumps between pages, the browser's back and forward work, and on a
narrow window the sidebar slides in from the menu button.

- **Home**: where the review stands, stage by stage (search, screening,
  selection, charting, extraction, writing, checking, and the lens tournament
  once there is one), the next steps with a
  button to the page for each, the latest manuscript with the editor's decision
  and the audit, and a check pack to download for someone else.
- **Your name** at the bottom of the sidebar is who your decisions and changes
  are saved under.
- **Full texts**: the retained records without a full text, core studies and
  the studies behind the headline claims first. DOI and Google Scholar open in
  your normal browser (where your library login works), *Copy file name* gives
  the name to save the PDF under, *Open the inbox folder* opens
  `fulltext/inbox`, and *Add downloaded PDFs* matches and parses what you saved
  there. Then ask Claude to chart and extract the new full texts.
  - The *Agents had* column says whether the agents read an abstract or only the
    title. The *Title only* filter lists the records that had no abstract.
  - **Missing abstracts.** Many publishers (Springer, Elsevier, APA and others)
    show an abstract on their own page but do not pass it to the open databases.
    Their pages also refuse automated reading, so the pipeline cannot copy it
    (`python -m ichnos abstracts <run>` tries PubMed, Europe PMC, Crossref,
    Semantic Scholar and OpenAlex first). A library database export brings these
    abstracts all at once:
    1. Pick the database. The box shows the protocol's search string for it.
    2. Run the string in the database and export the results as RIS or CSV,
       with abstracts.
    3. Add the file with *Add the export*.

    The app registers the export in the protocol, as `add-export` does, and
    loads it into the shown run. New records go on the sheet, and records
    already there get the export's abstract.
  - `python -m ichnos add-sources <run>` does the same from the terminal
    for exports registered with `add-export`.
- **Records**: decide each record with the buttons (exclusions are saved once
  you pick a reason). Everything is saved straight away; there is nothing to
  export.
- **Evidence map**, **Findings**: what the agents extracted, with the passages.
- **Insights**: everything noticed along the way. Picking a status
  (supported, contradicted, tested, promoted, dropped) saves it at once; a note
  is saved when you leave its field. You can also add your own insight at the
  top.
- **Interpretations**: the lens tournament (see Lenses below), read-only. It
  shows:
  - the gates as a table, with a bar showing where each lens's hypotheses
    dropped out (gate-2 columns count only what passed gate 1);
  - every hypothesis with its fate at each gate, why the judges said so, and
    its literature verdict with the hits behind it (title, link, the judge's
    verbatim quote);
  - the cartographer's test cards;
  - the lens cores, so you can rewrite them.

  Filter by lens, by verdict or by text; a click on a lens in the table filters
  too.
- **Check findings**: every finding the latest manuscript's headline claims
  rest on (Abstract, principal findings, implications, conclusion; set by
  `[paper] headline`). Each one shows the sentences that use it, what the
  finding says, the verbatim passage and a link to the study. Mark it
  **Confirmed** or **Corrected** (with what the source really says), say
  whether you checked it against the passage or the full text, and add a note.
  The manuscript's Declaration of AI use is generated from these checks (who
  checked how many, against what, how many were corrected), and the next
  revision must follow every correction. Your own declarations (funding,
  ethics, competing interests, CRediT, data deposit) go in
  `[paper.declarations]`, and your full name in `[paper.names]`.
- **Agents**: prepare a screening, extraction, findings or synthesis task, and
  import an agent's answers once it has done the task. The agent is Claude in
  Claude Code (ask it to run the task in the folder shown) or, for screening,
  eligibility, charting and findings, a local model (`agent-run`, below).
- **Rebuild report** and **Find full texts** run those steps for the shown run.

Searching (`pilot`) and registering library exports still use the commands
below.

## Setup

Python 3.11 or newer. No packages to install: everything uses the standard library.

The commands below run from this folder (`python -m ichnos ...`). To use
the `ichnos` command from any folder, install it once, from this folder:

```bash
python -m pip install -e .
```

The `-e` keeps the install pointing at this folder, so protocols, the evidence
store and the output stay here. `pip uninstall ichnos` removes it again.
Add `.[pdf]` instead of `.` to also install pypdf, for reading PDFs when
GROBID is not running.

API keys are optional but recommended. Copy `.env.example` to `.env` and fill
in what you have; without keys the run still works, more slowly and on shared
limits.

## Commands

Run from this folder.

```bash
python -m ichnos pilot A
```

Searches every source in `protocol/corpus_a.toml`, merges duplicates, and writes
`output/corpus_A/run_NNN/` with `screening_sheet.csv` (semicolon-separated, for
German Excel) and `report.md`. Each run gets a new folder, so a filled-in sheet
is never overwritten. A record that was already on an earlier sheet of the same
corpus is left off and keeps the decisions made there, so a new run's sheet
holds only what it added. Use `--full-sheet` to put the whole pool on the sheet.

```bash
python -m ichnos import-screening output/corpus_A/run_001/screening_sheet.csv
```

Reads your decisions back (plain CSV or CSV UTF-8, `;` or `,`), checks them
against the protocol the run used, and rebuilds the report with precision by
source. Use `--screener human:coder2` for a second screener. Once two screeners
have both decided every record on the sheet, the report adds their agreement
(Cohen's κ, records one excluded that the other kept) and writes
`disagreements.csv`. Until then it shows only progress, so a partial import
reveals nothing about the other screener.

```bash
python -m ichnos report 1
```

Rebuilds a run's report from the evidence store.

```bash
python -m ichnos mine 1
```

Citation searching: fetches from Semantic Scholar the reference lists of the
records excluded as reviews and of the core studies, and the works citing the
core studies. A mined record joins the run when its title or abstract matches
the search string (or the broader `[mining] query`, e.g. with synonyms); new
ones go on the screening sheet and pass the same stages as every other record
(screen them with `agent-tasks screen <run> --works <ids>`, which the command
prints). Every request is logged, and the Method and Figure 1 report the
counts apart from the database search. Run it again once core studies exist:
before that only the excluded reviews are mined, and there is nothing to
search forward from. A failed lookup tries the record's other identifiers
(Semantic Scholar id, DOI, PubMed ID), a record without any is matched by
title, and every pass is kept, so the Method reconciles each pass's counts and
says what became of the records it added.

```bash
python -m ichnos recall 5
```

Recall test: fetches from OpenAlex the reference lists of the background
sources named in `[recall] sources` (consensus statements, prior reviews) and
marks which of the cited records the search found. The ones it did not find are
scored by agents on the screening criteria (`agent-tasks recall 5 --agent ...`);
their judgements stay out of the review's screening. `recall 5 --report`
counts relevant records found and missed, and the paper's Method and
supplement report them.

```bash
python -m ichnos abstracts 5
```

Looks up the abstract of every retained record that has none, by DOI or PubMed
ID, in PubMed, Europe PMC, Crossref, Semantic Scholar and OpenAlex in turn. A
source's record counts only when its DOI or title is the record's own (a DOI
search can return another paper), and a citation given as abstract is refused.
Every call is logged and each outcome kept in `abstract_fills`; the command
prints the records that gained an abstract, to judge again with
`agent-tasks eligibility <run> --works <ids>`.

```bash
python -m ichnos add-sources 5
```

Searches the protocol's sources that a run has not searched yet, for example
`"eric"` added to `sources` after peer review. Records new to the run's pool go
on its screening sheet, and the command prints their ids for
`agent-tasks screen <run> --works <ids>`. Records the run already had are only
marked as found by the new source as well. Record the addition under
`[[deviations]]`. ERIC (the Education Resources Information Center) is free and
needs no key. The search is limited to titles and abstracts, DOIs come from
ERIC's record links, and ERIC's own PDFs count as open-access copies.

```bash
python -m unittest discover -s tests -t .
```

## Review page

Every report comes with `review.html` in the same folder: open it in your
browser (double-click works). It shows every record on the run's sheet with:

- the **trajectory signal** (strong, moderate, weak, none) and highlighted
  words: trajectory and change-modelling methods, subgroup and moderator
  language, longitudinal design words, cross-sectional wording, and the
  search string's own concept blocks (named by `query_blocks` in the protocol);
- each **agent's decision**, its metric scores and the quotes behind them, with
  ⚠ where a quote is not in the record;
- the **features** an extraction agent recorded, and the **evidence map** tab.

Filter by *Needs you*, *Agents disagree*, *Possible misses* (excluded although
the trajectory signal is strong) and more. Decide with the buttons or the
keys `i` `u` `e` (`j` `k` to move). Your decisions stay in this browser until
you click **Export my decisions**, then:

```bash
python -m ichnos import-screening "$env:USERPROFILE\Downloads\screening_decisions_corpus_A_run_002.csv"
```

**Blind mode** hides every agent judgement, for screening that must stay
independent, such as a validation set.

## Agents: screening on metrics, extraction for the evidence map

Agent steps are generated from the protocol, so they work in any domain. The
code writes the task, an agent does it, and the code checks and stores the
answer:

```bash
python -m ichnos agent-tasks screen 2 --agent model:claude-sonnet-5-5
python -m ichnos agent-import output/corpus_A/run_002/agents/screen__model-claude-sonnet-5-5
```

- **screen** scores every record of the run's sheet on `[[screening.metrics]]`
  (0 not met, 1 cannot tell, 2 met), each score backed by a quote from the
  record. The agent does not decide: the first metric scored 0, in protocol
  order, excludes with its `fail_reason`; all 2 includes; anything else is
  unsure.
- **eligibility** (optional) is the second stage: the records kept at title
  and abstract are scored again on stricter `[[eligibility.metrics]]`, from
  the abstract and the full text where one has been retrieved. It uses the same
  decision rule, a human's call wins, and a record not yet judged stays in. The
  Method and Figure 1 report it as its own stage. Record why it was added under
  `[[deviations]]` if it was not in the protocol from the start.
- **extract** fills the `[map]` dimensions for every kept study (a human's
  call wins; otherwise any agent's include or unsure) and, if the protocol has
  `[[claims]]`, proposes which studies support, challenge or qualify them.

`agent-import` rejects the whole task if any answer is malformed, checks
every quote against the record, logs each batch's prompt and answer in
`calls`, and rebuilds the report and review page. The agents that write
`batch_NN_result.json` have been Claude Code subagents; a local model can
write them with `agent-run` (below). A task of more than 99 batches
numbers them `batch_100.json` and on, and is imported in batch order like any
other.

### Local models: agent-run

```bash
python -m ichnos agent-tasks screen 8 --agent model:qwen3-14b --batch-size 10
python -m ichnos agent-run output/corpus_F/run_008/agents/screen__model-qwen3-14b --model qwen3:14b
python -m ichnos agent-import output/corpus_F/run_008/agents/screen__model-qwen3-14b
```

`agent-run` sends each batch, with `instructions.md` as the system prompt, to
an OpenAI-compatible server and writes the answers: Ollama
(`http://localhost:11434/v1`, the default), LM Studio
(`http://localhost:1234/v1`), llama.cpp's server or vLLM (`--endpoint`).
`LLM_ENDPOINT`, `LLM_MODEL` and `LLM_API_KEY` in `.env` save typing them.

- Each answer is checked as the import will check it before it is written.
  A failed answer is asked for again with its problems named (`--retries`,
  default 2). A batch that still fails gets no result file, and running the
  same command again asks only for the open batches.
- The answer is stored as plain JSON. A reasoning block (`<think>`), code
  fences and words around the JSON are dropped.
- A server that cut the prompt to its context length, or an answer cut off at
  the token limit, fails the batch at once. For Ollama, set
  `OLLAMA_CONTEXT_LENGTH` (e.g. 32768) before `ollama serve`; its default is
  far shorter than a batch. Otherwise write the task with a smaller
  `--batch-size`.
- `runner.json` in the task folder records the server, the model as the
  server lists it, temperature (0) and seed (1), and each batch's attempts,
  tokens and time. A task begun with one model refuses another, and a task
  labelled for Claude refuses a local model: the `--agent` label is the name
  the answers are stored under.
- `--parallel N` asks N batches at once (for vLLM). `--limit N` answers only
  N batches, for a trial.
- It runs the kinds whose batch holds everything the agent needs: `screen`,
  `eligibility`, `extract`, `findings`, `synthesize`, `write`, `recode` and
  `recall`. Manuscripts, audits, peer reviews and the experiments are read
  piece by piece from several files, which one prompt cannot hold; they still
  need Claude Code.

To try a model without touching the review, copy `data/evidence.sqlite` and
point `EVIDENCE_DB` at the copy: every agent screener counts, so a trial
screener in the real store would change which records go on.

**Evidence map.** `[map]` in the protocol sets the dimensions, the tree order,
`match_across` (the dimension to compare, e.g. setting) and `match_on` (what
two studies must share to be comparable, e.g. construct, measure, design).
The report and review page then show the tree, the **matches** (comparable
studies in different settings), the **gaps** (settings with no study in a
comparable group, plus matrices of empty cells), claim links, and the label
vocabulary. Merge synonyms under `[map.aliases.<dimension>]` and run
`python -m ichnos report N` again.

## Findings

```bash
python -m ichnos agent-tasks findings 2 --agent model:claude-opus-5-5
```

One row per result of each kept study that bears on the protocol's
`[findings] question`: the finding in one sentence, the effect as reported,
for whom, the protocol's `[[findings.fields]]` (categories are checked), and
the passage that states it, copied exactly. The import checks every passage
against the record and notes whether it came from the abstract or the full
text. These rows are what a writer may later cite. The report shows them with
a matrix of two fields (`matrix`), and the review page has a *Findings* tab.

## The writer

```bash
python -m ichnos agent-tasks write 2 --agent model:claude-opus-5-5
```

The evidence-first writer gets the run's findings (each with its verbatim
passage) and every insight you have not dropped, and returns the draft as
sentences, each listing the finding rows it rests on. On import every
sentence is checked in code: a claim cites at least one finding, cited
findings exist, every number occurs in the findings that sentence cites,
framing sentences claim nothing, no dropped insight is used. The draft is
written to `output/corpus_A/run_NNN/draft_NN.md` with author-year citations,
references, a verification section and the passage behind each citation, and
the app shows it in the **Draft** tab (hover a citation to see its passage).
`[writing]` in the protocol says what section to draft and how long.

Whether a sentence represents its findings fairly is still for you to judge:
the checks guarantee only that the trail from sentence to source is complete.

## Full papers with independent review

The blueprint's stages 6 to 8 for a whole manuscript, each an agent task
(`agent-tasks <kind> 2 --agent ...`, then `agent-import <folder>`):

1. **paper**: the manuscript: title, abstract, introduction, results,
   discussion, conclusion. The **Method section, Figure 1 and Tables 1-5 are
   generated from the evidence store**, so their numbers are true by
   construction. Every other sentence cites finding rows, method facts
   (M1...) or `[[background]]` sources from the protocol, and is checked like
   the writer's sentences. The generated tables are numbered by role:
   - in the paper: Figure 1 (the flow of records); Table 1, the core
     studies' characteristics (the protocol's own map dimensions); count
     tables of the findings (`[paper] count_tables` or `findings.matrix`), as
     coded on each finding; the reports that may share a sample; and the
     evidence map as counts, as charted for each study;
   - in the supplement: S1 every retained record, S2 every retained record
     against the inclusion criteria in `[[paper.core]]` (records meeting every
     criterion are the **core**, the rest a labelled set), S3 the evidence map
     naming each study, S4 why each retained record yielded no finding.
   - count tables and the evidence map have a Samples column (reports that may
     share a sample counted once); a count table with `text = true` gives in
     brackets the studies charted from a full text. `[paper] supplement` can add
     `repository` (the records under `[repository] records`, each with the check
     for a published version and the decision) and `findings_list` (each finding
     matching `[paper.findings_list] where`, with its effect as reported).

   Reports are grouped as one sample when they name the same data set, have
   the same title or abstract, are paired under `[[overlaps]]`, or (unless
   `overlap_by_authors = false`) share two or more authors. A named programme
   counts as a data set unless its name marks an organisation (Board,
   Association, Council, Matching, ...): the "National Resident Matching
   Program (NRMP)" that many reports mention is not a sample they share.
   Author-year labels
   that would be the same get a, b, ... after the year, the same in text,
   tables and references.

   The Method also reports protocol deviations it finds itself (records a
   human kept although the criteria excluded them, with date and whether a
   reason was recorded) and any you list under `[[deviations]]`.
   `[[metadata.fix]]` corrects bibliographic records the sources got wrong
   (e.g. two author names run together, a type, an online-first year) before
   the references are built. `[[exclusions]]` removes a retained record by a
   recorded decision (DOI, who, when, why), for example a paper with no data
   of its own. A record without a DOI is named by `s2_id`, `openalex_id` or
   `pmid`, or by its id in the source that found it (`source = "eric"`,
   `source_id = "EJ1458030"`). It leaves the tables and the findings, and the Method and
   Figure 1 report it. `[findings.brief]` gives some in-scope studies a
   smaller findings cap (`when` and `unless` rules on the charting, `focus`),
   reported in the Method. `[paper] rules` adds writing rules for one corpus;
   `[paper] protocol_by` says who wrote the protocol, for the Declaration.
   Each paper and revise task also gets `findings.md`, the findings as a list
   to read, grouped by their fields.
   Sentences can cite the figure and tables (`"tables": ["T2"]`) as well as
   findings; the code checks their numbers against the cited table, and the
   auditor sees the table and each cited study's charted details. The
   references list every retained record in APA style; run
   `python -m ichnos biblio 2` first to add volume, issue and pages from
   Crossref (logged like every other call; no email is sent). Each version is
   also written as an HTML document, and `python -m ichnos pdf <paper id>`
   prints the submission as a journal-style A4 PDF through Chrome or Edge
   (`--full` adds the response to reviewers and the appendices). Authors go in
   `[paper] authors` as `{name = "...", affiliation = "..."}`.
2. **audit**: a different model sees each claim sentence with exactly the
   passages it cites and rules faithful, overstated, understated, unsupported
   or misattributed.
3. **peer** (`--persona methodologist|domain|theory|critical`): independent
   reviewers who see only the submitted manuscript, scoring originality,
   rigour, evidence-claim alignment, contribution, clarity and transparency
   (1-4), overall (1-10) and a decision.
4. **meta**: the editor weighs the reviews and the audit and lists numbered
   required changes; for a revised version (round 2, 3, ...) it judges each
   earlier change and says whether what is left needs the pipeline or you.
5. **revise**: the writer revises and answers every required change.

Each version is written to `output/corpus_A/run_NNN/papers/` as the full
manuscript (with response to reviewers and a verification appendix) and as the
submission reviewers see. The app's **Paper** tab shows the versions, scores,
audit and decisions. `[paper]` in the protocol sets authors, kind and target
journal. Background sources say exactly what may be claimed from them and how
far they were checked: check each against the source before submitting.

**Before any real submission:** the AI use must be disclosed (the manuscript
carries a declaration), you are the author and answer for every claim, and
sending AI-generated papers to real peer review as an experiment needs the
venue's consent (Sakana AI arranged this with the ICLR workshop organisers).

## Human checks by someone else: check packs

An editor will not accept an AI-assisted review that no person has checked. A
check pack lets anyone do that without installing anything:

```bash
python -m ichnos check-pack 4
```

writes one HTML file to `output/corpus_X/run_NNN/check_packs/` (or use
**Download a check pack** on the app's Check findings tab). Send the file to
the person who checks; it opens in any browser, works offline, and keeps their
progress in that browser. It has three parts:

1. **Headline findings:** every finding the Abstract, principal findings,
   implications and conclusion rest on, with the study's link and the verbatim
   passage. The checker marks each *confirmed* or *corrected* (with what the
   source says), against the passage or the full text.
2. **Core studies:** each study that meets every inclusion criterion, with what
   the charting says on each criterion and its quote. The checker says whether
   it meets them; a study a person says fails leaves the core.
3. **Blind screening:** a seeded random sample (`--sample 60`) stratified by the
   agents' decisions (both excluded, both kept, disagreed) but shown without
   them. The checker screens each record against the inclusion rule; their
   decision counts as a person's call, and the import reports agreement with
   each agent (Cohen's κ).
4. **Blind coding** (only when asked: `--parts coding --coding-works 12,34,...`):
   - The chosen studies appear in a seeded order, with exactly the text the
     coders saw (abstract, full-text sections, authors and affiliations).
   - The checker codes the given charting dimensions (`--coding-dims`; by
     default, those with a second coding) without seeing any coder's codes.
   - The import stores the codes in `coding_checks` and reports agreement with
     the primary coding and with the second coder, per dimension.

**Download my checks** saves a JSON file; the checker sends it back, and you run

```bash
python -m ichnos import-checks checks_B_940f1cc4bc6a_Sam.json
```

(or **Import a returned checks file** in the app; `--dry-run` shows what would be
stored). The import refuses a file from another store, a file imported before,
and incomplete answers (a correction without what the source says, a failed
criterion not named). The manuscript's Declaration of AI use, its appendix and
the Method then report what each person checked, with their name and the date;
the next revision must follow every correction. Several people can check: send
each the same pack, or make one pack each.

## Insights

Working through many studies turns up things nobody asked for. They are
captured with their evidence and their history:

- Every agent task may return `insights` next to its answers: a recurring
  pattern, an anomaly, a contradiction, a method problem, a gap, a hypothesis
  or a question, citing work_ids and exact quotes (checked like all quotes).
- `agent-tasks synthesize 2 --agent ...` hands all findings of a run to one
  agent, which proposes cross-study insights, each citing finding ids.
- Your own:

  ```bash
  python -m ichnos insight add A "One sentence: what you noticed" --kind hypothesis --works 20,46
  ```

Each insight starts as *noted* and moves on with
`python -m ichnos insight status 7 supported --note "two more studies"`:
supported, contradicted, tested, promoted (into the paper) or dropped. Every
change is kept with its date and author. `notes/insights_corpus_A.md` is the
log, rebuilt with every report (or `python -m ichnos insights A`); the
review page has an *Insights* tab.

## Numbers are verified; interpretation is scored

Two currencies:

- **Numbers** go through the number gate (`ichnos/numgate.py`). A
  number in a sentence, insight or hypothesis must stand in what it cites, and
  as the same statistic:
  - β in the text cannot be an F value in the source;
  - a sample size cannot be the degrees of freedom of F(1, 2720);
  - where the source gives several β values, the variable named next to the
    number must be the one next to it in the source.

  The gate flags; it never deletes. `notes/number_gate_corpus_A.md` has the
  flags for the current insights.
- **Interpretation** is not audited for faithfulness. It is attributed to a
  lens and scored for what it yields.

### Recoding findings

`agent-tasks recode 2 --agent ...` has an agent code every protocol field of
the existing findings again, for example a new `predictor` and `sign`, or an
`outcome` that was coded as the predictor. Finding ids stay the same, so
manuscripts and human checks keep working. Every change is kept in
`finding_recodes` with its old value and reason. `--dims direction` recodes
only the named fields; a field's `definitions` in the protocol (operational
definitions of its codes) are shown to the recoding and findings agents.

`agent-tasks extract 2 --agent ... --dims fidelity --works <ids>` charts only
dimensions added to `[map]` later, leaving each study's other charted values
as they are.

### Lenses: rival interpretations

`protocol/lenses_corpus_a.toml` declares lenses: research programmes with a
hard core, a heuristic and a role (theory, adversary or placebo). It also holds
a probe grid of variables on which the lenses may predict different things,
and a positive control (two lenses that must diverge). The file is kept apart
from the review protocol, so editing a lens does not change the review's hash.

- `agent-tasks interpret 2 --agent ... --lens jahoda --replicate 1`: one lens
  reads every finding blind and returns five things:
  - a free reading;
  - a fit label per finding (predicts, accommodates with a named auxiliary,
    silent, strains);
  - grid signs;
  - stances on the contradictions in the insight log;
  - at most eight hypotheses, each saying what it forbids.

  Run each lens at least twice.
- `agent-tasks lens_control 2 --agent ... --mode all_cards` (one agent plays
  every lens) and `--mode no_card` (no lens: what the model says anyway) are
  the controls.
- Hypotheses pass three gates, each judged blind by two judges (run each task
  twice with different `--agent` names, e.g. `model:claude-sonnet-5-5` and
  `model:claude-sonnet-5-5-j2`). A hypothesis is credited only when both
  judges agree on every gate.
  1. `agent-tasks lens_judge 2 --agent ...` (gate 1): is it testable, does it
     restate a finding, is it already in the no-lens baseline?
  2. `agent-tasks lens_derive 2 --agent ...` (gate 2): does it follow from its
     lens's core, only with a named auxiliary, or from common sense? Lens
     names are masked; the judge sees only the core and heuristic. This is the
     gate that separates a theory from the placebo.
  3. The literature (gate 3):
     - `agent-tasks novelty_queries 2 --agent ...`: an agent writes a strict
       and a broad boolean search string and a plain query for each credited
       hypothesis. Three statements from the review's own core studies are
       mixed in as recall controls.
     - `python -m ichnos lenses 2 --search` runs the searches and logs
       every call. Strict and broad strings go to OpenAlex, ranked by
       relevance; the plain query goes to Semantic Scholar, with OpenAlex as a
       fallback.
     - `agent-tasks novelty_judge 2 --agent ...`: judges label every hit
       (states_it, tests_it, tests_neighbour, related, unrelated, unreadable).
       states_it and tests_it need a verbatim quote, which code checks.
     - Verdicts:
       - prior: both judges say a hit states or tests it, with a checked quote;
       - contested: only one judge says so;
       - near: both judges say a hit tests a close neighbour (same mechanism,
         another outcome or population);
       - not searchable: no hit with an abstract came back;
       - novel: none of these. That means nothing was found in the logged
         searches, which is not proof that none exists. Only abstracts are
         read.

     Without API keys, Semantic Scholar refuses most requests (HTTP 429), and
     OpenAlex has a free daily budget shared by everyone on your network's IP.
     Both keys are free: put `S2_API_KEY` and `OPENALEX_API_KEY` in `.env`.
     A key does not fix every gap. OpenAlex's fallback matches a plain query
     only in titles and abstracts that contain all its words, so a plain query
     of 8-10 keywords often finds nothing even when the request succeeds.
     Semantic Scholar also returned nothing for such a query in a test.
     So plain queries are now 3-5 key terms (at most 8 words; a phrase such as
     *meaning in life* counts as one term). For checks whose plain search
     found nothing, `agent-tasks novelty_plain 2 --agent ...` has a fresh
     agent rewrite only the plain query. The check, its key, its hits and its
     boolean strings stay. The old and new query are kept in
     `novelty_rewrites`. Then `lenses 2 --search` runs the new plain queries
     on OpenAlex, and `novelty_judge` labels the new hits.

     `novelty_queries` takes only credited hypotheses that have no search
     strings yet. `novelty_judge` gives each judge only the hits it has not
     labelled. A hit with an abstract that some judge has not labelled keeps
     its hypothesis at *unjudged*.
- `agent-tasks cartograph 2 --agent ...`: the cartographer turns the
  disagreements between two lenses into at most five test cards. The pair
  defaults to the positive-control pair, with the adversary added. Each card
  holds:
  - each lens's position;
  - a concrete design, data, measures and timing;
  - the possible results and the lens each favours;
  - the existing evidence and why it is not decisive;
  - artifact threats;
  - facts to verify.

  A card has no winner field, and code refuses one, and refuses a card whose
  possible results do not split the lenses.
- `python -m ichnos lenses 2` counts everything in code and writes
  `notes/interpretation_corpus_A.md` (plus `.html` and `.json`):
  - agreement between a lens's runs;
  - whether lenses differ more than runs of the same lens (an exact
    permutation test);
  - grid divergences, with the placebo's sign and the noise floor;
  - the positive control;
  - separate lenses compared with one agent playing them all;
  - hypotheses through the three gates;
  - the test cards;
  - contested findings;
  - copying between blind answers.

  The app's **Interpretations** page shows the same tournament, live from the
  evidence store.

### Lens evolution: programmes bred against held-out evidence

`protocol/evolution_corpus_a.toml` sets up a game. Its fitness is computed by
code, never by an agent.

- **The split.** Findings are split by whole study:
  - development: every agent sees these results;
  - validation: the selection signal;
  - test: the newest studies, sealed until evolution ends.
- **The probes.** A probe writer turns each validation and test finding into a
  neutral question. A probe counts only where the writer's reading of the
  result agrees with the extraction's coding. The disagreements are listed;
  they are a free audit of the coding.
- **The forecasts.** A lens reads the development findings and forecasts the
  hidden results (positive, negative, null, mixed) as probabilities. Code
  scores the forecasts with the Brier score.
- **Fitness** is the smaller of the lens's two gains, over the placebo lens and
  over a forecaster with no lens, minus a penalty for each auxiliary assumption.

**Arms.** Generation 1 is shared. After that, the three arms differ only in how
they choose parents:
- held-out fitness;
- at random (the same compute with no selection);
- an LLM judge that sees no scores.

**Controls:**
- the placebo lens;
- a shuffled-core lens, made of one proposition from each seed;
- a base-rate forecaster;
- a recall probe: the study is named, or neither the study nor any evidence is shown;
- two runs of every seed, for the noise floor.

**What is logged.** Every child is labelled after Lakatos as progressive,
degenerating, no gain or worse. Its parents, operator and change are logged,
and seeds are never deleted. The file is pre-registered: if it changes after a
game has begun, the game stops.

- `python -m ichnos evolve 2` writes every task the state allows, for
  example the probe task, generation 0, the mutations, the judge or the test
  forecasts. It also lists the open ones.
- `python -m ichnos evolve 2 --import-all` first imports every answered
  task.
- `python -m ichnos evolve 2 --report` counts and writes
  `notes/lens_evolution_corpus_A.md` (plus `.html` and `.json`) and a blind
  rating sheet (seed against evolved programme). The sheet goes to the run's
  output folder; its key is stored in the evidence store.

**Pooled games (0.2).** A file with `[[parts]]`, such as
`protocol/evolution_corpus_ab.toml`, pre-registers one game per corpus under
one hash, and pools their arms at the end. The game id picks the part, as in
`--game evo-b1`. A new game begins only with `--start`.
- `python -m ichnos evolve --game evo-ab1 --plan` prints the split, the
  probes to expect, the power, the noise floor and the agent tasks per
  generation. It writes nothing. Once generation 0 is in, it resizes the game
  with the game's own noise.
- `python -m ichnos evolve 4 --game evo-b1 --dates` fetches Crossref
  publication dates, for a test split after the forecaster's training cutoff.
- `python -m ichnos evolve 4 --game evo-b1 --start` begins the game.
- `python -m ichnos evolve --game evo-ab1 --report` writes each game's
  report and the pooled one.

### The referee: do the lenses' propositions come true?

`protocol/referee_corpus_d.toml` sets up a temporal backtest. Its score is
computed by code, never by an agent. The file is pre-registered: if it changes
after a round has begun, the round stops.

- **The split.** Studies are split by their first publication date (Crossref):
  - training: published before T. Every lens reads these findings, and only these;
  - test: published from T on and after the models' training cutoff;
  - excluded: inside the training window, or without the exact date the split needs.
- **The propositions.** Each lens (conservative, creative, cross-field) writes
  falsifiable propositions: claim, population, contrast, outcome, direction,
  moderators, a stated confidence, and what would confirm or contradict it.
  They are frozen, with a timestamp and a sha256 in the evidence store and a
  copy in the run's folder, before any agent sees a test study.
- **The judging.** Two judges who do not know which source wrote what say, for
  every test finding and proposition, whether the finding supports it,
  contradicts it or does not address it. Their agreement is reported.
- **The score.** Weighted net confirmations per proposition: a direction
  counts more than "depends", each moderator adds, and a proposition every
  rater calls obvious counts half. Beside it: coverage, hit and contradiction
  rates, and the Brier score of the stated confidences. Each lens is set
  against the controls with a 95% interval (resampling studies) and a
  permutation test (reassigning propositions).

**Controls:**
- the placebo lens (Lens P);
- one agent with the evidence and no lens;
- shuffled propositions, made by code from the lenses' parts;
- a contamination probe: the model says what each test study found from its
  title alone. Studies it seems to remember are flagged and left out.

The round is a standing registry: when a search update adds studies, the same
frozen propositions are judged against the new test studies only.

- `python -m ichnos referee 6 --dates` fetches the Crossref dates.
- `python -m ichnos referee 6 --plan` prints the split, the test set, the
  power and the agent tasks. It writes nothing.
- `python -m ichnos referee 6 --start` begins the round; afterwards
  `referee 6` writes every task the state allows and lists the open ones.
  `--import-all` first imports every answered task.
- `python -m ichnos referee 6 --report` writes
  `notes/referee_corpus_D.md` (and `.json`).

#### The forward check of the papers (a claims round)

`protocol/referee_corpus_c.toml` uses the same machinery to check the review's
own finished papers forward (`mode = "claims"`).

- **The sources** are papers (`[[sources]]` with the `paper` id). An agent
  restates each paper's claims about the world as 6 to `per_source`
  propositions, with `where` in the paper each comes from. They are frozen like
  any others, before any later study is seen.
- **The split.** The papers read every study the run has when the round
  begins, so all of these are seen. Only studies the run gains in later search
  updates, first published after the models' training cutoff, test the claims.
- **No probe and no rating.** A later study cannot be in the papers or in the
  models' training data, and no lenses are compared. The shuffled control
  stays, as the judges' check.
- **Home** shows each claim with the later studies for and against it
  ("Forward check of the papers"). When later studies contradict a claim,
  "What needs you" says so.
- After a search update of the run (screening, eligibility and findings for
  the new records), run `referee 5 --dates`, then `referee 5`. That writes
  the judge tasks for the new test findings only.

### Focused papers and what to explore next

A protocol with an `[aim]` produces a focused paper instead of a scoping-review
manuscript. The aim might be `kind = "solutions"`: which design choices make
something work, with a `question` and an `audience`.

- **Findings.** The findings get three fields, coded by a recoding pass:
  - `by`: the design choice the finding concerns;
  - `link`: whether it connects that choice to the aim's outcomes (tested,
    associated, reported or none);
  - `domain`: the outcome it concerns.
- **Evidence.** Only the findings that bear on the aim reach the writer.
- **Structure.** The writer writes its own short Method from the method facts.
  Facts A1–A4 say how the evidence was selected, where the comparator evidence
  comes from, how the patterns were tested, and what no person checked.
- **Main text.** Figure 1 and three generated tables:
  - Table 1, the solutions table;
  - Table 2, the comparator corpus's findings on lowered stakes;
  - Table 3, the pre-registered patterns and predictions, with the second
    coder's verdicts.
- **Supplement.** The scoping tables and the full generated Method go to a
  separate supplement (`_supplement.md`; `pdf <id>` also writes the
  supplement PDF).
- **Editor.** The editor does not reject a paper, or ask for a revision,
  because of what only the author can do. The paper states plainly what no
  person checked.

**Several aims.** A protocol can hold several named aims: `[aim]` is the
default (give it an `id`), and `[[aims]]` hold the others. Write for one with
`agent-tasks paper <run> --aim <id>`. A manuscript keeps the aim it was
written for, and its revisions follow it. Three kinds exist:
- `solutions`: the design choices that make something work.
- `effectiveness`: the review's own question, from all core findings,
  organised by outcome.
- `proposal`: an evidence-based redesign. Its sections are Introduction,
  Method, Evidence, Proposal, Evaluation, Discussion and Conclusion, plus a
  model table (Table 5). The writer writes that table, and each of its rows is
  checked and audited like a sentence.

After a paper, `agent-tasks directions <run>` asks an agent for 6–10 ranked
directions. Each has its question, why it matters, what is known, what is
not, the next step, and its value, feasibility and novelty. They appear on
the project's Home page under **What to explore next**. Choose with
**Explore this** or **Set aside**; the next run is drafted from what you
choose. The Home page lists what needs you first: a finished paper to read
and directions to choose. Checks are folded under "Optional".

### Discovery: directions across all projects

Directions come from one finished paper. Discovery reads everything the
projects hold at once (findings, charting and insights) and lists candidate
directions. Code finds them, not an agent, and each card shows the counts,
studies and verbatim passages it rests on.

```bash
python -m ichnos discover                 # sweep, print the top 5 of each kind
python -m ichnos discover --kind gap --corpus E --top 20 --no-sweep
python -m ichnos discovery 12 chosen --note "moderator paper"
```

Five kinds:
- **Contradiction**: findings on the same question (the findings matrix
  fields and the outcome) whose directions disagree, with at least two
  studies on each side. The card names the fields and charted dimensions that
  best separate the two sides: candidate moderators.
- **Gap**: a cell of the findings matrix or the evidence map (match_across
  against a match_on dimension), or of a cause against the outcome, with at
  most one study, where the row and column totals predict at least three.
- **Bridge**: an outcome that two projects both study (matched by its words),
  with the causes each project studied and the studies they share. A project
  and its comparator strand are not a bridge.
- **Recurring insight**: at least three insights from three separate passes
  (agent calls or people) that say the same thing, in shared words or about
  shared studies. Method notes are left out.
- **Rising topic**: a value at least twice as common among the studies of
  the last three years as before.

A protocol can say what counts as the same question and as a cause; by
default it is the findings matrix and the outcome, without the direction:

```toml
[discovery]
same_question = ["candidate_rule", "modern_input", "outcome"]
causes = ["modern_input"]
```

Each sweep recomputes every candidate. A candidate keeps its id across
sweeps, so your choice and note stay with it. One that a sweep no longer finds
is hidden but kept. A candidate is noticed in the studies it lists, so a
pattern claimed from it is tested on other studies (`noticed_in` in
`[[patterns]]`). The start page links to the **Discovery** page, where you
filter by status (to choose, chosen, set aside), kind and project, read the
evidence, choose **Explore this** or **Set aside**, and run a new sweep.
`notes/discoveries.md` is the log, with a table of the chosen ones.

**What you choose is scored, then checked for novelty.** Both are agent
tasks of their own in `output/discovery/agents/`. You prepare them on the
Discovery page or with `discover --task`, an agent answers them in a Claude
session, and you import them on the page or with `agent-import`.

```bash
python -m ichnos discover --task discovery_score --agent model:claude-opus-5-5
python -m ichnos discover --task novelty_queries --agent model:claude-opus-5-5
python -m ichnos discover --search
python -m ichnos discover --task novelty_judge --agent model:claude-opus-5-5
python -m ichnos discover --task novelty_judge --agent model:claude-sonnet-5-5
```

1. **Scoring.** The agent reads each discovery's dossier: its counts, its
   projects, and the findings (with their passages) and insights it rests
   on. For each discovery it writes:
   - a testable **statement** in one sentence;
   - the kind of output (new review, reanalysis, empirical study,
     intervention design, theory);
   - **value** and **feasibility** (1–5), with the basis of the feasibility:
     the data already coded, a new search, or new data;
   - the **artefact risk** (1–5): how likely the pattern comes from how
     studies were coded, selected or labelled;
   - why it matters, its risks and the next step.

   It may cite only findings and insights in the dossier, and code checks
   every answer before storing it. **Priority** is mean value × mean
   feasibility × (6 − mean artefact risk) / 5. Chosen discoveries are listed
   by priority.
2. **Novelty.** This is the lens layer's novelty check, run under the
   tournament `discovery`:
   - search strings for the first scorer's statement, with two recall
     controls: findings of studies the discoveries rest on, which the search
     should find;
   - the OpenAlex and Semantic Scholar searches (`--search`);
   - two judges labelling the hits.

   The verdict follows the lens rules: *prior* only when every judge finds a
   hit that states or tests the statement, with a quote found in it.
   Otherwise it is *near*, *contested*, *novel* and so on. Each chosen card
   shows its scores, its verdict with the hits behind it, and its next step.

### Pre-registered patterns and comparator strands

Agents notice patterns across studies a few studies at a time, as insights.
A pattern worth claiming is written into the protocol as `[[patterns]]`
before the coding it needs. The entry says:
- which studies count (`among`) and what it expects of them (`expect`);
- for a contrast, the two groups it compares (`a`, `b`);
- the range of the share, or of the difference, in which it holds (`holds`);
- what each lens of the corpus predicts (holds, fails, none).

Code counts the patterns, not agents. Each one is judged only on the studies
it was not drawn from: the studies cited by the insights in `noticed_in` are
counted beside it as discovery studies. Reports that may share a sample count
once. Only retained studies count: a record charted and excluded afterwards
(by a recorded decision or at the eligibility stage) is left out, and so is
its second coding when kappa is computed.

A comparator strand is a corpus of its own, registered with
`strand_of = "C"` in `protocol/projects.toml`. It shows on C's card before
its first run. Its predictions sit in the main corpus's protocol under
`[comparator]`, and are counted on the strand's coding.
- `python -m ichnos agent-tasks extract 5 --dims a,b --agent model:... --second-coder`
  writes an independent second coding of those dimensions. On import it
  goes to `second_codings`, never to the charting. `patterns` then reports
  Cohen's kappa per category, and marks patterns that rest on a category
  below `kappa_min` (default 0.6) as unreliable.
- `python -m ichnos patterns 5 --check` checks that every pattern and
  prediction can be counted as written: the dimensions, categories, ranges,
  lens predictions and insights exist.
- `python -m ichnos patterns 5 --comparator <run of D>` counts them.

## Full texts

### Through your own library

Most studies are not open access. Download them yourself through your
university library:

1. `python -m ichnos library 2` writes
   `output/corpus_A/run_002/fulltext_todo.html`: every retained record without a
   full text, with DOI and Google Scholar links. The records you kept yourself
   (your include or unsure in the app) come first, then core studies, then
   studies with findings. With `LIBRARY_PROXY` in `.env` (your library's
   EZproxy prefix, e.g. `https://ezproxy.example.edu/login?url=`, or a template
   with `{url}`) each record also gets a Library link; with the library's
   browser extension the DOI link already opens through the library. You sign
   in yourself; the pipeline never handles a password.
2. Download the PDFs. Leave them in your Downloads folder, or save them in
   `fulltext/inbox/`. In the inbox a file named with the record number
   (`20.pdf`, `20_smith.pdf`) is matched at once, and any other file by the DOI
   or title on its first pages. In Downloads a file is matched by its DOI or
   title only, and only to a retained record still without a full text.
3. Press **Add downloaded PDFs** in the app, or run
   `python -m ichnos add-pdfs` (every run at once; `add-pdfs 2` for one).
   - Each matching PDF is parsed (by GROBID when it runs) and stored.
   - Inbox files move to `fulltext/inbox/done/`; unmatched ones stay.
   - Downloads stays as it is: matching files are copied, and nothing there is
     moved or deleted. A file already compared with the same records is not
     read again (`fulltext/inbox/.downloads_checked.json`).
   - The command prints, per run, the records that gained a full text and the
     `agent-tasks extract` and `findings` commands that read them. These use
     the run's own agents, so the earlier charting is replaced.
   - Another folder: set `DOWNLOADS_DIR`. To skip Downloads: `DOWNLOADS_DIR=off`.

The PDFs stay on this computer (`fulltext/` is never shared). Re-extracting a
study's findings from its full text marks its earlier findings as
*superseded* rather than deleting them, so earlier manuscripts and your checks
keep their history. A finding re-extracted from the full text is a new finding
and needs its own check.

### Open access

Abstracts rarely name instruments or analyses, so the extraction agent can read
the methods and results of an open-access copy:

```bash
python -m ichnos fulltext 2
```

For each kept study of the run (`--all` for the whole sheet) it looks for an
open-access copy: Europe PMC's structured XML first, then the PDF at Unpaywall's
locations (only with `CONTACT_EMAIL` in `.env`, as Unpaywall requires) and at
OpenAlex's. PDFs are parsed by GROBID when it runs on `localhost:8070`, otherwise
by pypdf with heading heuristics. Files stay in `fulltext/`, which is not
committed; the store keeps source, URL, licence, version, SHA-256 and the
sections. Nothing goes through a university login. Failed downloads are tried
again next time; `--refresh` fetches parsed ones again too.

The next `agent-tasks extract` gives each study its methods and results text
(up to 14,000 characters), and quotes from it are checked like any other. Give
the extractor a new name, e.g. `model:claude-opus-5-5:fulltext`; the report
then compares it with the abstract-only extraction.

**GROBID** runs in Docker:

```bash
docker run -d --name ichnos-grobid --restart unless-stopped -p 127.0.0.1:8070:8070 grobid/grobid:0.9.1-crf
```

If Docker Desktop stops at start-up with "initializing Inference manager ...
dockerInference ... cannot be accessed by the system", quit it, rename
`%LOCALAPPDATA%\Docker\run` (stale socket files), and start it again.

## Library databases: PsycINFO, Scopus, Web of Science

These have no API we can use, so you run the search yourself through your
library, export the results, and register the export. The next `pilot` run
merges it with the API sources like any other source.

1. Get the search string in each database's syntax:

   ```bash
   python -m ichnos strings A
   ```

   It prints the strings and saves them to `output/corpus_A/search_strings.md`:
   EBSCOhost and Ovid (for PsycINFO, whichever platform the library uses),
   Scopus and Web of Science. Every term is limited to titles and abstracts, as
   for the API sources.

2. Paste the string into the database's advanced search, run it, and note the
   number of results. Compare it with the API sources in the latest report. If
   it is far off, the database read something differently (hyphens,
   truncation): fix the string there and save what you actually ran to a text
   file.

3. Export all results with abstracts. RIS works on all four platforms;
   Scopus CSV and Web of Science plain-text or tab-delimited files work too.
   If the database limits how many records go into one file (Web of Science
   allows 1,000), export several files and give them all in step 4.

4. Register the export:

   ```bash
   python -m ichnos add-export A psycinfo.ris --source psycinfo --platform ebsco --hits 312
   ```

   `--platform` (ebsco, ovid, scopus or wos) says you pasted the string from
   step 1 unchanged. If you changed it, pass `--query-file what-i-ran.txt`
   instead, or as well. `--searched` sets the search date, which defaults to
   today; `--hits` is the count from step 2. The command checks the files,
   copies them to `imports/corpus_a/`, and adds an `[[imports]]` entry to the
   protocol. Each source name can be used once, so call a second PsycINFO
   search `psycinfo_2`.

5. Run `python -m ichnos pilot A`. The new records appear on the run's
   sheet. Records already found by another source merge with it, by DOI or
   by title and year. Each export file is stored in full in the evidence store
   at every run.

Each `add-export` changes the protocol, so the next run records a new protocol
hash. Bump `version` when you freeze the protocol for the preregistration.

## Layout

| Path | What it is |
| --- | --- |
| `examples/corpus_a.toml` | An example protocol to copy into `protocol/`. |
| `protocol/corpus_a.toml` | Search string, sources, pilot size and seed, screening rule and codes. Yours to edit; every run stores its full text and hash. |
| `ichnos/query.py` | Parses the canonical boolean string, renders it per source and database, re-checks it locally. |
| `ichnos/sources/` | OpenAlex, Semantic Scholar, PubMed (E-utilities), Europe PMC and ERIC clients. |
| `ichnos/imports.py` | Reads library exports (RIS, CSV / tab-delimited, Web of Science plain text); `add-export` and `strings`. |
| `imports/` | Export files registered with `add-export`, one folder per corpus. |
| `ichnos/store.py` | Evidence store schema and writes. |
| `ichnos/pilot.py`, `report.py` | Pilot run, screening sheet and import, report. |
| `ichnos/signals.py` | Trajectory, heterogeneity and design word lists, and the trajectory signal. |
| `ichnos/agents.py` | Agent tasks from the protocol (screen, extract), decision rule, quote check, import. |
| `ichnos/mapping.py` | Evidence map: tree, matches, gaps, matrices, claim links. |
| `ichnos/review.py` | The review page. |
| `ichnos/insights.py` | Insight capture, status lifecycle, and the insight log. |
| `ichnos/discovery.py` | Candidate directions across all projects, found by code: contradictions, gaps, bridges, recurring insights, rising topics; scoring and novelty tasks for the chosen ones; the Discovery page. |
| `output/discovery/agents/` | The discovery tasks (scoring, search strings, judging), one folder per task. |
| `ichnos/writer.py` | The evidence-first writer: input, sentence checks, Markdown with references. |
| `ichnos/paper.py` | Full manuscripts: generated Method, flow and table; audit, peer review, editor, revision. |
| `ichnos/app.py` | The local app (`Ichnos.bat`). |
| `ichnos/fulltext.py` | Open-access full texts: Europe PMC XML, Unpaywall and OpenAlex PDFs, GROBID or pypdf parsing. |
| `fulltext/` | The downloaded copies (not committed). |
| `data/evidence.sqlite` | The evidence store. |
| `output/` | Generated sheets and reports, one folder per run. |
| `notes/` | Dated lab notes: what a run showed and what was decided. |

## Evidence store

| Table | Holds |
| --- | --- |
| `protocols` | Full text and SHA-256 of every protocol version a run used |
| `runs`, `run_sources` | Each run, and per source the query exactly as sent, reported total, fetched count, status |
| `calls` | Every HTTP call (URL with keys redacted, status, timing, rate-limit headers, gzipped response body), every library export file read by a run, and model screening calls |
| `works`, `retrievals` | Merged records (DOI, then source IDs, then title within a year), and which source found each in which run |
| `samples`, `screening` | Fixed draws (the pilot sheet) and screening decisions per screener and stage |
| `screening_scores` | Per metric score and quote behind each agent screening decision, and whether the quote is in the record |
| `study_features`, `claim_links` | What each kept study did, per map dimension, and proposed links to protocol claims, each with its quote; per run, so a record in two corpora keeps each corpus's charting |
| `fulltexts` | Per work: open-access source, licence, version, file hash and parsed sections, or why there is none |
| `findings` | One row per finding: sentence, effect, for whom, protocol fields, verbatim passage, where it was found |
| `insights`, `insight_events` | Insights captured along the way, their evidence, and every status change |
| `discoveries`, `discovery_sweeps` | Candidate directions found by `discover`, with their evidence and your choice; every sweep and its counts |
| `discovery_scores` | Each agent's score of a chosen discovery: statement, output, value, feasibility and its basis, artefact risk, citations. Their novelty checks are in `novelty_checks` under the tournament `discovery` |
| `drafts` | Writer drafts: sentences with the finding rows they cite, and each sentence's check result |
| `papers`, `reviews` | Manuscript versions (with method facts and checks) and their audits, peer reviews and editor decisions |
| `philosophy_claims` | Empty for now; claims currently live in the protocol's `[[claims]]` |

To open it, use any SQLite viewer, such as DB Browser for SQLite. A command
that writes while another one does (an import during `abstracts` or `mine`)
waits up to 30 seconds for it instead of failing with "database is locked".
Re-extracting a study's findings supersedes the earlier findings of the same
extractor in the same run only.

## Search-string rules

Write the string once, in the protocol, as `AND` / `OR` / `NOT`, `"phrases"`
and trailing `*`. Operators must be explicit, commas are not allowed, and
wildcards only go at the end of single words, not inside phrases. The code
renders each source's syntax and logs exactly what it sent. See the pilot note
for the OpenAlex behaviour behind these rules.

## Not built yet

A runner for the agent tasks that read several files (manuscripts, audits,
reviews) without Claude Code, validation against two human screeners on a
fresh blind set, and the novelty check (stage 5).
