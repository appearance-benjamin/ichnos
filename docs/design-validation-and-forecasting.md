# Design note: validating Ichnos against people, and a forecasting registry

*Draft, 4 November 2026. Part A (the validation benchmark) is being built; part B (forecasting) is designed here and
built after it. The two belong together: a system that predicts which findings hold up can only be scored if its
reading of findings is known to be accurate. Part A measures that accuracy; part B uses it.*

## Why

Ichnos checks form by code (every quote is in its source, every number is in what it cites) but not substance (was the
quote read correctly?). On 4 November the store held 10,550 findings, of which people had checked 4; 162 screening
decisions, all by one person; no completed check pack. The referee tested 144 frozen propositions against 18 later
studies: too few to tell the lenses from the placebo. Two things are missing: **accuracy against people**, and **a
test of prediction large and clean enough to mean something**.

## Part A: the validation benchmark

Four tests, each against a ground truth that people made, each with its threshold fixed before Ichnos runs.

| Test | Ground truth | What Ichnos does | Measure | Threshold (proposed) |
| --- | --- | --- | --- | --- |
| **A1 Screening** | Labelled reviews: records a published review included or excluded at title and abstract. SYNERGY (26 reviews, 169,288 records, CC0; six tagged psychology) | The existing two-model screening, from a protocol written from the review's own criteria *before* screening | Recall of the review's inclusions (with 95% interval), precision, κ with the labels; every missed inclusion listed | recall ≥ 0.95 (the usual bar for automated screening) |
| **A2 Retrieval** | The list of studies a published review included | Ichnos's own search, from the review's question and dates | Recall at each stage: retrieved, on the sheet, kept, with findings | report; no bar yet |
| **A3 Extraction** | Study-level effect sizes from open meta-analysis data (psymetadata: 22 datasets of study-level effects) | Findings extracted from the same studies | Direction agreement; effect size within a tolerance; misread subgroup or outcome | direction ≥ 0.90 |
| **A4 People** | Blind double coding of a random 200 findings by the author and a colleague (check packs, already built) | Nothing new | Error rate by type (direction, subgroup, outcome, effect, overstatement) | report |

Rules that keep it honest:
- **Frozen before running.** Each benchmark's protocol (criteria, thresholds) is registered with its hash before any
  model sees a record, as every Ichnos protocol is.
- **Labels never reach the agents.** The labels sit in their own table; screening tasks are written from the records
  alone.
- **Contamination.** A published review may be in the models' training data. For screening this matters little (the
  label is a judgement on the record); for A2 and A3 the test set is split by publication date where possible.
- **Two model families when available.** Each test is run with the second family as well, so agreement between
  families is measured, not assumed.
- **Final inclusions as labels.** SYNERGY's labels are the reviews' final inclusions, not their title-and-abstract
  decisions. Recall is therefore the honest measure (a study the review finally included must survive
  title-and-abstract screening); precision is understated, because records a review kept at title and abstract and
  dropped at full text count as false positives.
- **Records without a title.** SYNERGY ships ids only; `benchmark from-ids` fetches titles and abstracts from
  OpenAlex. Records OpenAlex cannot return are left out and counted by label; missing abstracts are then looked up
  with `ichnos abstracts`.
- **RAISE.** The reports follow the joint Cochrane–Campbell–JBI–CEE guidance on AI in evidence synthesis: tool,
  version, purpose, and the evaluation behind each use.

## Part B: the forecasting registry

The model asked for: read the older literature, propose what will be found, and check later studies. The
referee already does this as a *backtest*. Three things stop it from being a reliable predictor yet: the models have
read most of the literature (contamination), the test sets are small, and nothing is scored over time. The registry
adds a prospective, standing test.

### Four kinds of forecast

| Kind | Question | Ground truth | Contamination |
| --- | --- | --- | --- |
| **F1 Propositions** (the referee) | What will studies published after T find on this question? | Later studies in the store, judged blind | Clean only after the models' cutoff (June 2026) |
| **F2 Registered studies** | Will this preregistered study's primary hypothesis be supported, and how large will the effect be? | The Stage 2 report or the published results | **Clean**: the result does not exist when the forecast is frozen |
| **F3 Replicability** | Will this published finding replicate? | FReD (1,239 original–replication pairs, JOPD 2024), SCORE (274 claims, *Nature* 2026), new replications as they appear | Old pairs are in training data: probe and flag; only new replications are clean |
| **F4 Solutions** | Which proposed intervention or explanation will later evidence support? | Later studies and trials | As F1 |

F2 is the strongest test and the most useful one: preregistered protocols (PCI Registered Reports Stage 1, OSF
registries, AsPredicted where public) give a steady supply of questions whose answers do not yet exist. A base rate is
known: in psychology, 44% of Registered Reports supported their first hypothesis, against 96% of standard reports
(Scheel, Schijen & Lakens 2021, *AMPPS*).

### Forecasters, all frozen with their sources

- **Grounded:** Ichnos reads the store's evidence on the question and forecasts (the system under test).
- **Bare model:** the same model without the evidence: what the evidence adds.
- **Lenses:** as in the referee.
- **Crowd:** several models, from different families when available, averaged.
- **Base rates:** the share of Registered Reports supported, the replication rate in the field: what any forecaster
  must beat.
- **People, when possible:** the author's own forecasts, or a small panel, for calibration against humans.

Each forecast states a probability and, for F2, an effect size with an interval. Forecasts are frozen with a hash and
a timestamp before the result exists (optionally also deposited on OSF, so the timestamp is public).

### Scoring

By code only: Brier score and log loss, calibration curve (do 70% forecasts come true 70% of the time?), resolution,
and skill against the base rate and the bare model, with intervals from resampling questions. A forecaster earns
weight only through its track record. Reported every quarter, with the full history.

### How it enhances reliability

- **A credibility score on each finding** in the store (F3), so evidence maps and papers weigh fragile findings less.
- **A track record for each kind of claim**: if Ichnos's forecasts on, say, intervention effects are badly
  calibrated, its papers say so and claim less.
- **Questions worth testing:** where grounded forecasts are most uncertain and the stakes are high, a new study pays
  most. This links synthesis to the conduct of new studies (design, power from pooled effects, preregistration).

### Honest limits

- A forecaster can be well calibrated and still useless (always predicting the base rate). Skill against the base
  rate is the measure, not accuracy.
- F2 resolves slowly (months to years). The registry is a long-term instrument; its first useful report needs about
  100 resolved questions.
- Contamination cannot be fully excluded for anything published before the cutoff; those results are reported apart.

## Build order

1. **A1 and A2 harness** (now): import labelled records or a reference list into a benchmark run, keep the labels away
   from the agents, score the existing screening and retrieval against them.
2. A3 extraction scoring against study-level effect sizes; A4 runs on the existing check packs.
3. The registry: questions, frozen forecasts, resolutions, scoring (F2 and F3 first).
4. Sources of questions: PCI RR Stage 1, OSF registries; FReD and SCORE for F3.
5. The quarterly report, and credibility scores fed back into the evidence maps.

## Sources

- SYNERGY: open dataset on study selection in systematic reviews (ASReview; DataverseNL, CC0).
- Röseler et al. (2024), The Replication Database, *Journal of Open Psychology Data*, doi 10.5334/jopd.101.
- Rodriguez & Williams (2022), psymetadata, *Journal of Open Psychology Data*, doi 10.5334/jopd.61.
- SCORE replicability results, *Nature* 652 (2026); Center for Open Science, cos.io/score.
- Hewitt, Ashokkumar, Ghezae & Willer, predicting results of social science experiments with large language models
  (*Nature*): accuracy held for studies not in the training data, but effect sizes were overestimated.
- RAISE: Responsible AI use in evidence synthesis (Cochrane, Campbell, JBI, CEE joint AI methods group).
