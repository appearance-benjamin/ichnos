# Design note: the effects matrix, the interpretive playground and open discovery

*Draft, 30 October 2026. Not built yet. This note proposes the next three layers of Ichnos and the order to build
them in.*

## The principle: play freely, keep score

Ichnos has two jobs, and they must never mix.

- **The ledger** holds what is verified: findings with their verbatim passages, numbers checked against their
  sources, decisions with who made them. Papers are written only from the ledger.
- **The playground** holds what is imagined: reframings, juxtapositions, mechanisms, policy options. Anything goes,
  as long as every move points to the ledger rows it rests on and is shown as speculation.

The two meet in one place: the **referee**. Whatever a move in the playground predicts can be frozen and later
scored against studies published after it. Over time the scoreboard shows which ways of interpreting forecast well.

## Where Ichnos stands

| Part | What it does now | Its limit |
| --- | --- | --- |
| `findings` | One sentence per finding, with `effect` (as reported, free text), `for_whom`, the protocol's fields and the verbatim passage. | Effects are text, so they cannot be pooled, compared or modelled. |
| `study_features` | The evidence map's dimensions per study, with canonical labels. | The dimensions are set in the protocol before anything is read. |
| Discovery | Five code-only generators: gap, contradiction, bridge, cluster, trend. | They search only along the dimensions the protocol defined. |
| Lenses | Rival theoretical readings, three gates, test cards. | No lens beat the plain reading or the placebo (best +0.03, p = 0.95). |
| Referee | Propositions frozen with a hash, scored on studies first published after the models' cutoff, with placebo, no-lens and shuffled controls. | It scores lenses only; nothing else feeds it yet. |

Both remaining limits are hypothesis-led: people choose the axes in the protocol, and a language model reads with
the literature's own priors. The three layers below address that, in order.

---

## Part 1: the effects matrix

Turn each quantitative finding into a row that statistics can use.

### What gets stored

A new table, `effects`, one row per effect (a finding can have several):

| Column | What it holds |
| --- | --- |
| `effect_id`, `finding_id`, `work_id`, `run_id`, `extractor`, `call_id` | Where it comes from, as elsewhere in the store. |
| `outcome`, `outcome_canonical` | The outcome as stated, and a short label for matching. |
| `contrast` | What is compared: exposure against control, before against after, a predictor. |
| `population`, `setting` | Who and where, as stated. |
| `design` | RCT, quasi-experiment, panel, cross-sectional, qualitative (no effect then). |
| `timing` | Follow-up or lag between exposure and outcome. |
| `n` | Analysed sample for this effect. |
| `metric`, `value`, `ci_low`, `ci_high`, `se`, `p` | The effect as reported (d, g, r, OR, RR, β, ...). |
| `direction` | positive, negative, null, mixed, relative to the contrast. |
| `adjusted` | Whether it is adjusted, and for what. |
| `evidence`, `grounded` | The verbatim passage, and whether every number appears in it (the number gate). |
| `d_common`, `d_se`, `conversion` | The effect converted to a common metric by code, with the formula used, or empty when no honest conversion exists. |
| `status` | extracted, verified, disputed, superseded, as for findings. |

### Rules

- **Code converts, agents do not.** Agents copy what the paper reports. Code converts it (r to d, OR to d, and so
  on) with the standard formulas, and logs the formula per row. If the conversion needs a guess, it is not made.
- **Every number is checked.** The number gate already checks that numbers appear in their passages; it applies to
  every numeric column here.
- **Dependence is explicit.** Several effects from one study share `work_id`. Any pooled estimate treats them as
  clustered, never as independent.
- **Qualitative findings stay findings.** They get no effects row and are not forced into numbers.

### Commands

```bash
python -m ichnos agent-tasks effects N --agent model:claude-sonnet-5-5
python -m ichnos agent-import output/corpus_X/run_N/agents/effects__model-claude-sonnet-5-5
python -m ichnos effects-export N          # a CSV ready for R (metafor) or Python
```

The standard library stays the only requirement. Heavier statistics run outside Ichnos on the export, or as an
optional extra like `pdf`.

### Done when

- On one corpus with many quantitative studies (D is a good candidate), at least 90% of effect rows are grounded.
- A person double-codes a random sample of 50 effects. The error rate is reported per column. Corpus E is the
  warning: on re-checking, 29% of its focused findings needed a correction, and statistics multiply such errors.
- The export reproduces one simple pooled estimate that a person can redo by hand.

---

## Part 2: the interpretive playground

A place to think with the evidence, where nothing is mistaken for evidence.

### Moves

A new table, `moves`:

| Column | What it holds |
| --- | --- |
| `move_id`, `corpus`, `author`, `call_id`, `created_at` | Who made it (`human:...` or `model:...`) and when. |
| `kind` | One of the modes below. |
| `statement` | The move in plain words. |
| `rests_on` | JSON: the finding and effect ids it uses. Required. |
| `parent` | The move it branches from, so ideas form a tree. |
| `predicts` | Optional: a structured proposition in the referee's format (population, contrast, outcome, direction, moderators, confidence, what would contradict it). |
| `status` | sketch, sharpened, frozen, scored, retired. |

### Modes

- **Juxtapose.** Put findings from distant corpora side by side. Discovery's bridge generator suggests the pairs.
- **Stress-test.** Take a pattern and ask for whom, where or when it would fail. The effects matrix shows which
  populations and settings have any evidence at all.
- **Transport.** Would this hold in setting X? The playground lists the effects that exist for X, the ones that do
  not, and marks the move speculative where evidence is missing.
- **Mechanism.** Sketch a causal diagram. Every arrow links to the ledger rows behind it or is marked *assumed*.
- **Crux debate.** Two rival positions argue over the same evidence. Unlike the lenses, the aim is not a winner or a
  hypothesis: it is the one observation that would settle the disagreement. The output is a test card.
- **Policy option.** See Part 4.

### Keeping score

- **Freeze.** A move with a `predicts` block can be frozen: hashed and timestamped, like the referee's propositions.
  It then enters the next referee round with the usual controls.
- **Scoreboard.** Per author and per mode: how many frozen predictions, how many confirmed or contradicted, and
  calibration (Brier score against the stated confidence). Lens evolution showed that one run says little, so the
  board reports intervals, and no ranking is shown until there are enough scored predictions to separate sources.
- **The wall.** Nothing in the playground reaches a paper unless it is promoted: rewritten from ledger rows and
  passed through the usual checks.

### In the app

A **Playground** page per project: a canvas of finding cards that can be dragged together, the tree of moves, and
the scoreboard. Every card opens its passage; every move shows *speculation* until it is scored.

### Done when

- Juxtapose and stress-test work on one corpus. Every move cites ledger rows, and the app refuses a move that cites
  none.
- At least 30 moves are frozen and enter one referee round together with the existing controls.

---

## Part 3: open discovery

Find patterns that nobody proposed, then make them earn their place.

### 3a. Dimensions that emerge from the data

- Represent each finding as a vector. A standard-library baseline (TF-IDF over the finding and its passage) comes
  first; embeddings from an open-weight model on university GPUs come second.
- Cluster the findings, and repeat the clustering over bootstrap samples and seeds. Only clusters that come back
  most of the time are kept.
- An agent names each stable cluster, quoting the findings at its centre and its edges.
- Ichnos compares the clusters with the protocol's dimensions and reports what the protocol did not anticipate.
  These become candidate new dimensions, which a person accepts or rejects.

### 3b. Pattern search over the effects matrix

New Discovery generators that run on effects, not on text:

- **heterogeneity:** for each outcome, which coded features explain the spread of effects (meta-regression over
  each feature, with false-discovery-rate control across all of them);
- **reversal:** combinations of conditions under which an effect changes sign;
- **interaction:** pairs of features that matter only together (a tree-based search on the export, run outside the
  standard library);
- **small-study effects:** per outcome, whether small studies report larger effects, a sign of publication bias.

### 3c. Absence as a signal

The literature reflects what people chose to study. Ichnos already flags empty cells (the gap generator). On the
effects matrix it can also show which outcomes are measured only in some designs or populations, and how much of an
apparent pattern rests on a handful of studies.

### 3d. A bridge to primary data

A pattern in the literature is a pattern in what was published. Where open panel data exist, a pattern can be
re-estimated on them: the literature proposes, primary data test. This is where personal data and GDPR enter, so
this step runs on university infrastructure, with a data steward's approval, and only after 3a to 3c work.

### The discipline that makes it worth anything

Searching without hypotheses mostly finds noise. Every discovered pattern therefore goes through three steps:

1. **Split before searching.** The studies are split in two at random. The search sees only one half.
2. **Freeze, then test.** A pattern found in the first half is frozen as a proposition and tested on the second half,
   and later by the referee on newly published studies.
3. **Count the search.** The report states how many patterns were tried, not only the ones that held, so a reader
   can judge how many would hold by chance.

### Done when

- On one corpus, the emergent clusters are stable across bootstraps and at least one candidate dimension is accepted
  by a person.
- The pattern search reports how many patterns it tried and how many held on the held-out half. Any number held,
  including none, is a result.

---

## Part 4: from patterns to policy options

Pattern recognition can say what the evidence shows. It cannot say whether that applies here or what is worth more.
A policy option in Ichnos keeps those three apart:

| Layer | What it holds | Who decides |
| --- | --- | --- |
| Evidence | The expected effects, each linked to ledger rows, with its uncertainty. | The data, through the checks. |
| Applicability | Which populations and settings the evidence covers, and where it is silent. | The data, plus a person who knows the setting. |
| Values | Costs, trade-offs, who gains and who loses. | People. Ichnos lists the trade-offs and does not weigh them. |

Each option is a move of kind *policy option*. Its expected effects are frozen as predictions, so options proposed
today can be scored when later evaluations appear.

---

## Order of work

0. **Prove accuracy first (outside this note).** Blind human screening on a sample, with recall and κ, and one
   replication of a published review. Without these, nothing built on top is credible.
1. **Effects matrix** on one corpus, with its double-coding check.
2. **Playground**, minimal: moves, juxtapose and stress-test, freezing into the referee.
3. **Open discovery**: emergent dimensions, then the effect-level generators with split-sample testing.
4. **Primary data and policy options**, once 1 to 3 have shown results.

## Risks

- **Errors multiply.** Wrong numbers in the effects matrix produce confident nonsense. The double-coding check in
  Part 1 is a gate, not a formality.
- **Forking paths.** An open search will find something. The split, the freeze and the count of the search are what
  separate a pattern from luck.
- **Complexity.** Ichnos already has many parts. Rule: a new part that shows no measured payoff after one full trial
  is archived, as lens evolution should be.
- **Cost.** Effects extraction reads every quantitative finding again. Measure tokens and hours per stage from the
  first run.

## Open questions

- Which corpus first for the effects matrix: D (many quantitative studies on stakes) or C (the showcase)?
- Should the playground allow moves by models on their own, or only by a person with a model's help?
- Which open panel datasets are within reach for Part 3d, and under which access conditions?
