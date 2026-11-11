# Design note: choosing a direction with the board, and the argument log

*Draft, 4 November 2026. Not built yet. This note turns the independent board (notes/board/independent-1/) from a
one-off run into an Ichnos stage: a procedure for choosing a paper's direction before committing to it, and a log that
shows every argument, every change of mind and every agreement with its reasons.*

## Why

The independent board showed that splitting the evidence works where personas did not: real disagreement, four minority
reports, and about a dozen points the no-role control did not find. But two of its steps were not a procedure:

| Step | How it was done on 4 Nov | The problem |
| --- | --- | --- |
| **Lanes** (which evidence each member gets) | The coordinator chose five by judgement: the store, pre-1990 history, computation, contrary recent studies, law and sociology | Not reproducible. Nobody checked that every proposition was covered: no lane covered demography, so the retirement argument got no evidence |
| **Sources** (which studies each member read) | M1 searched the store by keyword. M2–M5 and B0 searched the web freely, under one rule: cite only what you read, with a DOI | Searches were not logged. Nothing records what was found and not used, or why. Several key points rest on summaries |
| **Agreement** | One blind round, merged by the coordinator | Members never saw each other's points, so we cannot tell what they would have accepted or rejected. The merge is the coordinator's reading |

The project's own evidence (`notes/multi_agent_creativity_evidence.md`) sets the limits: discussion favours shared facts
and pulls towards the majority (Stasser & Titus; Estornell & Liu), so a reconsideration round must not become a debate.
It can be a round in which each change of mind must state its reason, and changes made without new evidence are flagged.

## The shape of it

```
candidate directions (author)
  → PROPOSITION LEDGER   agent + author: each direction's load-bearing propositions
  → LANE DESIGN          code proposes, author approves: coverage matrix, frozen with a hash
  → SOURCE SELECTION     code searches each lane; members screen; every search logged; full-text requests
  → R1 BLIND             each member writes points from its lane only; B0 control in parallel
  → R2 RECONSIDER        each member rules on the others' points and on its own, with a reason type
  → CONSENSUS MAP        code: status of every point, by independent lanes
  → DIRECTION SCORE      code: table per direction; the author decides, and the decision is logged
```

### 1. Candidate directions and the proposition ledger

The author names two to four candidate directions, each as a one-paragraph thesis. For the Entry Door work these might
be: D1 the Entry Door as it stands; D2 the retirement paradox (`notes/2026-11-04-retirement-pipeline-paradox.md`); D3
"brake and door are one instrument" (merge C2). An agent restates each as load-bearing propositions, the way the
disputation note's claim ledger does. The author can add, strike or merge them. Directions may share propositions.

### 2. Lane design: a coverage matrix, not a judgement

A **lane** is an evidence partition: what a member may read. Lanes come from a catalogue of evidence *types*, not
disciplines-as-personas:

| Type | Example |
| --- | --- |
| store | an Ichnos corpus, or a filter on one |
| historical | episodes before a cut-off year |
| computational | the model and its runs |
| contemporary, for | recent empirical studies, search framed for the claim |
| contemporary, against | the same, framed to find contrary or null results |
| institutional | law, collective agreements, regulators, firms' own documents |
| neighbouring field | a discipline the author does not work in |
| demography and accounting | population, flows and identities (where a claim is partly arithmetic) |

Code builds the matrix **propositions × lane types** and proposes the smallest set of lanes that meets three rules:
1. every load-bearing proposition is covered by **at least two lanes of different types**, including any type the
   proposition `requires` (built after the first check showed why: "two types" was met without demography, so the
   retirement claims would again have had no demographic evidence; the author's judgement that a claim needs a kind
   of evidence is now stated in the frozen file);
2. every proposition has one lane whose search is framed against it (a search instruction, not a persona);
3. no two lanes share more than a set share of their sources (independence; checked again after selection, step 3).

The author sees the matrix, the proposed lanes, and the **lanes considered and dropped, with the reason**, and approves.
The lane file (`protocol/board_<id>.toml`) is then frozen with its sha256, as referee rounds are. Under rule 1 the
retirement direction would have forced a demography lane, the gap the 4 November board missed.

### 3. Source selection: logged, screened, and requested in full text

- **Code runs each lane's search strings** through the existing sources (OpenAlex, Europe PMC, Semantic Scholar, ...)
  into a **board run**. Every source becomes a record in `works`, so the existing full-text pipeline and
  `ichnos add-pdfs` work for board sources too (today they cannot match web-found studies).
- **Members screen their lane's pool** (include or exclude, with a reason), using the existing screening tables.
- **Members may add sources from the web**, and every such search is logged: query, date, what was seen, what was
  taken. Nothing is cited that is not in the log.
- **Overlap check:** after screening, code computes the share of sources each pair of lanes has in common, and flags
  pairs above the threshold.
- **Full-text requests:** a point that rests on a source read only as an abstract or a summary is flagged. These go on
  the library to-do list (`ichnos library`) ranked by how many points rest on them, so the author knows which PDFs to
  get. Points keep a *read from* tag: full text, abstract, summary.

### 4. R1: blind

As on 4 November: one file per member, points numbered `[M<n>-k]`, each with type, status, the sources it rests on
(work ids), the proposition it bears on, and its stance on it (supports / qualifies / contradicts / cannot say). Each
conclusion states in advance what would change the member's mind (Mellers, Hertwig & Kahneman). B0, one agent with all
lanes and no role, runs in parallel as the control.

### 5. R2: reconsideration with reasons

Each member receives the **ledger** of all R1 points (statements, sources, lane), not the other members' prose. It does
two things:
- **On every other member's point that touches its lane:** *endorse*, *dispute* or *cannot judge from my evidence*.
- **On each of its own points:** *keep*, *revise* (new text) or *withdraw*.

Every endorsement, dispute or change carries a **reason type**:

| Reason type | Meaning | How it counts |
| --- | --- | --- |
| *own evidence* | a source from my lane, cited | full weight |
| *their evidence* | I checked the other member's cited source | full weight, logged as cross-checked |
| *argument* | logic, no new source | counted, shown separately |
| *deference* | persuaded without a source or argument I can name | allowed but **flagged as possible conformity** |

Members cannot bring in sources from outside their lane without logging them (step 3). One more round is allowed for
contested points only, at most one exchange per point, as in the disputation design.

### 6. Consensus map (computed by code)

Every point gets a status from the R2 rulings:

| Status | Rule |
| --- | --- |
| consensus | endorsed by members from at least *k* lanes of different types, no evidence-based dispute |
| majority | more evidence-based endorsements than disputes |
| contested | at least one evidence-based dispute and one endorsement |
| minority | only its owner holds it, and no one disputes it with evidence (kept, not dropped) |
| withdrawn | its owner withdrew it, with the reason |

Two counts sit beside each point: **support by independent lanes** (endorsements from different lane types, not
members), and **convergence by deference** (how much of its support came from *deference* rulings). B0's overlap is
marked on every point: a point B0 also found is not the board's added value.

Contested and minority points go to the author as the disputation's challenges: the author answers, with "I would give
this up if …", and rules *stands / qualified / narrowed / withdrawn / open*.

### 7. Direction score, and the decision

For each candidate direction, code tabulates over its propositions:
- support from independent lanes, and the contrary evidence found by the *against* lanes;
- the share of that support read in full text, from an abstract, or from a summary;
- points that are contested, and the tests that would settle them (the members' predictions);
- novelty: points not found by B0, and (later) the novelty check against the literature (`novelty.py`);
- open full-text requests.

Code does not choose. The author decides, and the decision is stored with its reasons and the table it was made on.

## The argument log: what the author sees

One page per board (`output/board_<id>/index.html`, like the other run pages):

1. **The stance matrix:** points as rows, members as columns, rounds as layers. Each cell shows endorse, dispute,
   cannot judge or owner. A changed cell shows its arrow, its reason type and, on hover, the cited source. Deference
   changes are marked.
2. **Lineage:** which point built on, revised or answered which, as a graph or an indented list.
3. **The consensus board:** points grouped by status, each with its independent-lane count and its B0 mark.
4. **The direction table** and the author's logged decision.
5. **The search log and coverage matrix:** what each lane searched, what it kept, the overlap between lanes, and the
   full texts still missing.

The markdown files (R1, R2, merge) stay as the readable record. The page is rebuilt from the store.

## Storage

New tables in `data/evidence.sqlite`, following `ref_rounds` / `ref_tasks`:

| Table | Holds |
| --- | --- |
| `board_rounds` | the board, its run, the frozen lane file and its sha256, the directions |
| `board_lanes` | lane id, type, evidence rule, search strings, the propositions it covers |
| `board_searches` | every logged search: lane, member, query, source, date, hits, taken |
| `board_sources` | lane × work, with screening decision and *read from* |
| `board_points` | id, round, owner, type, status, text, proposition, stance, sources, built-on |
| `board_rulings` | R2 and R3: ruler, point, ruling, reason type, cited source, text |
| `board_decisions` | the author's rulings on points and the direction decision, with reasons |

The task kinds `board_write` (R1) and `board_reconsider` (R2) run in task folders, like every other agent step. Each
answer is checked by code: cited sources must be in the lane's log, and point ids must exist.

## Controls and honest limits

- **B0 on every board.** Without it, "the board found X" cannot be told from "one agent would have found X".
- **One model family.** Every member is Claude, and no other provider is configured. When one is, a lane can be run by
  another model family, and the overlap check then also measures agreement between models.
- **R2 can still converge for the wrong reasons.** The reason types make this visible, they do not prevent it. If most
  of a consensus is *deference*, the page says so.
- **The lane catalogue is itself a choice.** It should be revised when a board misses something, as the demography gap
  shows.

## Build order

1. Board run and lane file (step 2), with source registration (step 3), so that full texts can be supplied at once.
2. R1 and R2 task kinds and their checkers; the consensus computation.
3. The page.
4. The direction score.

First use: the three Entry Door directions above, with the 4 November reports imported as R1 so that R2 can start from
them.
