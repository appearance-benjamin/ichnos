# Design note: the disputation, a debate the author has to win

*Draft, 1 November 2026. Not built yet. This note proposes a debate stage between the editor's list and the final
version of a paper, with the author as the respondent.*

## Why another adversarial step

Ichnos already has four places where something argues against a claim. None of them is a debate.

| Part | What it does | Why it is not a debate |
| --- | --- | --- |
| Peer reviewer, `critical` persona | One reading of the manuscript, one report | One shot: it never hears the answer |
| Artifact lens (`role = "adversary"`) | Reads the findings for non-causal explanations | Lives in the interpretation layer and does not feed the papers |
| Editor's "your calls" | Lists the decisions only the author may make | Asks for a decision, not a defence |
| Blueprint "objection at full strength" | The author states the opponent's case | The author writes both sides |

The project's own evidence says what a debate should *not* be (`notes/multi_agent_creativity_evidence.md`):
- **AI against AI does not help.** Debate among similar agents settles on the majority view or on errors they share
  (Estornell & Liu 2024), and voting explains most of debate's gains (Choi et al. 2025). On corpus D no lens beat the
  plain reading of the evidence.
- **Role-played dissent is weak, authentic dissent works.** Real minority views beat an appointed devil's advocate
  (Nemeth, Brown & Rogers 2001), and dissent fed innovation only when the people involved took part in the decision
  (De Dreu & West 2001).
- **The active ingredient of adversarial collaboration** is that both sides agree *in advance* which result would
  count against them (Mellers, Hertwig & Kahneman 2001).

So the value is not in the challenger. It is in the one party who holds a real view: the author. In a debate between
models, nobody is committed to anything. When the author has to answer, the commitment is real, and the record
shows which claims the author can defend and how. This is the old *disputatio* (objection, answer, reply to the
objection), or a thesis defence held before submission rather than after it.

## The shape of it

```
draft after the editor's round
  → CLAIM LEDGER       code + agent: the paper's load-bearing claims, each with its sources and evidence counts
  → CHALLENGE          challenger agent: the strongest objection to each chosen claim, grounded in the store
  → COMMIT             author: "I would give this up if …"; challenger: "I would withdraw if …"
  → EXCHANGE           author answers in their own words; challenger replies; at most 3 rounds per claim
  → RULING             author (with an optional human moderator): stands / qualified / narrowed / withdrawn / open
  → CONSEQUENCES       changes to the draft; "open" claims become directions or test cards; an appendix of objections
```

### 1. The claim ledger

An agent restates the paper's claims as a numbered list, the same way the referee's claims round already does
(`mode = "claims"`). Each claim keeps where it stands in the paper. Code attaches what the store knows: the findings
it cites, the audit verdicts of its sentences, and for a proposition paper the for/against counts.

Code then ranks the claims by **load × weakness**:
- *load*: does the thesis need it (abstract, a section title, a proposition)?
- *weakness*: few studies, an even for/against split, an "overstated" audit verdict, abstract-only sources.

The author can add claims and strike claims, and sees the ranking. A paper gets 5–8 claims, not 40.

### 2. The challenge

One challenger per claim, from a **different model family** than the writer where one is available. Each objection
has to be one of four kinds, and must show its ground:

| Kind | Must point to |
| --- | --- |
| *Evidence against* | Findings in the store that contradict the claim, with their passages |
| *Rival explanation* | A mechanism that fits the same findings, and a result that would tell the two apart |
| *Scope* | The populations, designs or periods the claim silently extends to |
| *Conceptual* | A named author and work that argues the opposite, with what exactly they say |

An objection with no ground is refused by code, as a quote that is not in its source is refused now. This keeps the
challenger from winning by rhetoric.

### 3. The commitment

Before the exchange, both sides write one sentence each:
- the author: *what would make me give this claim up*;
- the challenger: *what answer would make me withdraw the objection*.

These are frozen with a hash, like referee propositions. They are the adversarial-collaboration ingredient, and they
let anyone see afterwards whether the ruling followed the rule both sides set.

### 4. The exchange (the human in the loop)

The author answers **in their own words, first**, in the app or by voice (transcribed). Only then may an assistant
help sharpen the answer, and the record keeps both versions. If the AI drafts the defence and the author only clicks
"agree", the debate is AI against AI again and worth nothing.

The challenger replies to the answer it was given. At most three rounds per claim: the challenger cannot win by
persistence, and the author cannot escape by delay.

A second person can join as **moderator**: a supervisor, a fellow student or a philosopher. The moderator sees the
claim, the evidence and the exchange, not the author's name, and gives an independent ruling. Where they differ from
the author, both rulings are kept.

### 5. The ruling

| Ruling | Meaning | What follows |
| --- | --- | --- |
| **Stands** | The answer meets the objection | The objection and answer go to the appendix "Objections considered" |
| **Qualified** | The claim holds with a stated condition | The sentence is rewritten with the condition |
| **Narrowed** | The claim holds for less (fewer people, designs, periods) | The sentence and the abstract are narrowed |
| **Withdrawn** | The author gives it up | The sentence goes; if the thesis needed it, the editor is told |
| **Open** | Only new evidence could decide | A direction or test card, frozen for the referee's forward check |

The ruling is the author's, and recorded under the author's name. That is the point: it turns "a person is
accountable for the claims" from a sentence in the AI-use statement into a record anyone can read.

## Does it add anything? How to tell

The project has been strict about measuring its experiments (the lenses were reported as adding nothing). The
disputation should be held to the same standard:

1. **Pre-empted objections.** When a human referee or supervisor reads the paper, count how many of their objections
   the disputation had already raised and answered. This is the main measure.
2. **A control.** Split the claims at random: half go through the disputation, half do not. Compare their later fate
   in human review and in the referee's forward check.
3. **Change rate.** How often a ruling is not "stands". If it is always "stands", either the claims are strong or the
   challenger is weak; the moderator's rulings tell the two apart.
4. **No-human control.** Let a model answer for the author on a copy, and compare the rulings and the later referee
   objections. If the AI-answered disputation does as well, the human step adds nothing measurable here, and that
   should be reported too.

## Where it fits in the code

- A new task kind `dispute` (challenge) and an import that checks the grounds, as `novelty_judge` checks its quotes.
- The claim ledger reuses the referee's claims round and the audit tables.
- The author's answers come through the app (a **Disputation** tab on the paper), not through an agent folder,
  because they are a person's input like a check pack.
- Tables: `disputations`, `dispute_claims`, `dispute_turns` (who, text, version, time), `dispute_rulings`.
- The paper's AI-use statement and Method report which claims were disputed, by whom, and with which ruling.

## Order to build

1. A paper version only: the ledger, the challenge and the rulings, with the author answering in a Markdown file.
   Try it on *Arriving Again* before it goes to a human reader, so measure 1 can be counted on that reader's
   comments.
2. The app tab, the commitments and the moderator.
3. The control (measure 2) on the next proposition paper.
