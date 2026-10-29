"""The referee end to end with scripted agents: the dated split, training evidence only, the freeze before any test
study is shown, blind masks, the contamination probe, scoring by code, and the standing registry."""

import json
import tomllib
import unittest
from collections import Counter

from ichnos import agents, evolution, referee
from tests.test_agents_map import Base

REFEREE_FILE = '''
version = "test"
round = "r-test"
corpus = "Z"
run = {run}
seed = 1
domain = "adolescent sleep and mood"

[split]
T = "2026-07-01"
model_cutoff = "2026-06-30"
after_cutoff = true
evidence_max = 0

[propositions]
per_source = 3
replicates = 2

[[lenses]]
id = "bold"
name = "Bold"
stance = "Say something risky."

[[lenses]]
id = "plain"
name = "Plain"
stance = "Say what the evidence says."

[baselines]
placebo = {{ name = "Lens P", stance = "People differ." }}
no_lens = true
shuffled = true

[probe]
control = 3
exclude_flagged = true

[score]
direction = {{ higher = 1.0, lower = 1.0, no_difference = 0.8, depends = 0.3 }}
moderator_bonus = 0.25
max_moderators = 2
obvious = 0.5
bootstrap = 200
permutations = 500

[agents]
generate = "model:g"
probe = "model:p"
check = "model:c"
judges = ["model:j1", "model:j2"]
findings_per_batch = 2
batches_per_task = 2
'''

# (year, Crossref date or None, Crossref status): 8 studies by year, two 2026 studies before T, three after it, one
# 2026 study without a date
STUDIES = [(2018 + i, None, None) for i in range(8)] + [
    (2026, "2026-03-01", "found"), (2026, "2026-05-15", "found"),
    (2026, "2026-07-10", "found"), (2026, "2026-08-20", "found"), (2026, "2026-09-05", "found"),
    (2026, None, "no_doi")]


class Round(Base):
    def setUp(self):
        super().setUp()
        (self.root / "protocol" / "referee_corpus_z.toml").write_text(REFEREE_FILE.format(run=self.run), encoding="utf-8")
        evolution.ensure(self.store)
        self.known, self.works = [], []
        for i, (year, date, status) in enumerate(STUDIES):
            self.works.append(self.add_study(i, year, date, status))
        self.store.db.commit()

    def add_study(self, i, year, date, status, n=3):
        w = self.store.upsert_work({"title": f"Sleep study {i}", "year": year, "doi": f"10.9/r{i}",
                                    "authors": [f"Ann Smithson{i}", "Bo Lee"], "venue": "J Sleep"})
        for k in range(n):
            fields = {"outcome": "mood", "direction": ["lower_better", "lower_worse", "no_difference"][k % 3]}
            self.store.db.execute(
                "INSERT INTO findings (work_id, run_id, extractor, finding, fields, evidence, status, created_at) "
                "VALUES (?, ?, 'model:x', ?, ?, ?, 'extracted', '2026-01-01')",
                (w, self.run, f"Study {i}, finding {k}: mood differed.", json.dumps(fields), f"passage {i}.{k}"))
        if status:
            self.store.db.execute("INSERT OR REPLACE INTO evo_dates VALUES (?, 'crossref', ?, ?, '2026-10-21')",
                                  (w, status, date))
        return w

    def step(self, start=False):
        return referee.step(self.store, self.run, self.root, start=start)

    def meta(self, folder):
        return json.loads((folder / "task.json").read_text(encoding="utf-8"))

    def answer(self, folder, obj, batch="batch_01"):
        (folder / f"{batch}_result.json").write_text(json.dumps(obj), encoding="utf-8")

    # scripted agents

    def generate(self, folder):
        meta = self.meta(folder)
        src, first = meta["source"], meta["evidence_ids"][0]
        style = {"bold": ("lower", [{"condition": "in exam weeks", "effect": "stronger"},
                                    {"condition": "for night owls", "effect": "reversed"}]),
                 "plain": ("depends", [{"condition": "in exam weeks", "effect": "stronger"}]),
                 "placebo": ("higher", []), "no_lens": ("lower", [])}[src]
        props = [{"id": f"R{n}", "claim": f"Claim {n} about mood.", "population": f"teens of type {n}",
                  "contrast": "late versus early school start", "outcome": "mood", "direction": style[0],
                  "moderators": style[1], "confidence": 0.7, "confirms": "mood is better", "contradicts": "mood is worse",
                  "basis": [first]} for n in range(1, 4)]
        self.answer(folder, {"propositions": props})

    def run_open(self, status, skip_pair=True):
        for rel in status["open"]:
            folder = self.root / rel
            meta = self.meta(folder)
            kind = meta["kind"]
            if kind == "ref_generate":
                self.generate(folder)
            elif kind == "ref_probe":
                data = json.loads((folder / "batch_01.json").read_text(encoding="utf-8"))
                if meta["wave"] == 0:
                    self.known = meta["test"][:1]               # the model remembers one test study
                self.answer(folder, {"studies": [{"key": s["key"], "known": meta["keys"][s["key"]] in self.known,
                                                  "recalled": ["Mood was lower."]} for s in data["studies"]]})
            elif kind == "ref_check":
                data = json.loads((folder / "batch_01.json").read_text(encoding="utf-8"))
                self.answer(folder, {"studies": [
                    {"key": s["key"], "title_states": False,
                     "findings": [{"finding": f["id"], "reproduced": "yes" if meta["keys"][s["key"]] in self.known
                                   else "no"} for f in s["findings"]]} for s in data["studies"]]})
            elif kind == "ref_rate":
                self.answer(folder, {"labels": [{"key": k, "obvious": v.startswith("placebo"), "reason": "r"}
                                                for k, v in meta["mask"].items()]})
            elif kind == "ref_judge":
                for b in sorted(folder.glob("batch_[0-9][0-9].json")):
                    out = []
                    for n, f in enumerate(json.loads(b.read_text(encoding="utf-8"))["findings"]):
                        add = []
                        for k, key in meta["mask"].items():
                            if key.startswith("bold"):
                                add.append({"key": k, "verdict": "supports", "why": "fits"})
                            elif key.startswith("placebo") and not (skip_pair and meta["judge"] == "model:j2" and n == 0):
                                add.append({"key": k, "verdict": "contradicts", "why": "against"})
                        out.append({"finding": f["id"], "addressed": add})
                    self.answer(folder, {"findings": out}, b.stem)
            counts = agents.import_task(folder, self.store)
            self.assertEqual(counts["problems"], [], folder.name)

    def play(self):
        status = self.step(start=True)
        while status["open"]:
            self.run_open(status)
            status = self.step()
        return status


class Split(Round):
    def test_place(self):
        cfg = {"split": {"T": "2026-07-01", "model_cutoff": "2026-06-30"}}
        self.assertEqual(referee.place(cfg, 2026, "2026-06-30")[0], "training")
        self.assertEqual(referee.place(cfg, 2026, "2026-07-01")[0], "test")
        self.assertEqual(referee.place(cfg, 2025, None)[0], "training")
        self.assertEqual(referee.place(cfg, 2026, None), ("excluded", "2026", referee.UNDATED))
        self.assertEqual(referee.place(cfg, 2027, None)[0], "test")
        self.assertEqual(referee.place(cfg, None, None)[0], "excluded")
        self.assertEqual(referee.place(cfg, 2025, "2026-08-01")[0], "training")     # out by the end of its year
        early = {"split": {"T": "2026-01-01", "model_cutoff": "2026-06-30"}}         # the leakage guard
        self.assertEqual(referee.place(early, 2026, "2026-03-01")[0], "excluded")
        self.assertEqual(referee.place({"split": {**early["split"], "after_cutoff": False}}, 2026, "2026-03-01")[0], "test")

    def test_start_needs_dates(self):
        self.store.db.execute("DELETE FROM evo_dates WHERE work_id = ?", (self.works[-1],))
        with self.assertRaisesRegex(ValueError, "--dates"):
            self.step(start=True)

    def test_plan_writes_nothing(self):
        p = referee.plan(self.store, self.run, self.root)
        self.assertEqual((p["split"]["training"]["studies"], p["split"]["test"]["studies"],
                          p["split"]["excluded"]["studies"]), (10, 3, 1))
        self.assertFalse(self.store.db.execute("SELECT 1 FROM sqlite_master WHERE name = 'ref_rounds'").fetchone())
        self.assertIn("ref_generate 8", referee.plan_text(p))

    def test_round_must_be_started_and_unchanged(self):
        with self.assertRaisesRegex(ValueError, "has not begun"):
            self.step()
        self.step(start=True)
        path = self.root / "protocol" / "referee_corpus_z.toml"
        path.write_text(path.read_text(encoding="utf-8").replace("per_source = 3", "per_source = 4"), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "changed after round r-test began"):
            self.step()

    def test_generators_see_training_only(self):
        status = self.step(start=True)
        self.assertEqual(len(status["open"]), 8)                  # 2 lenses, placebo, no lens; 2 replicates each
        test = set(referee.split_findings(self.store, referee.round_(self.store, self.run, self.root), "test"))
        train = set(referee.split_findings(self.store, referee.round_(self.store, self.run, self.root), "training"))
        self.assertEqual(len(test), 9)
        for rel in status["open"]:
            folder = self.root / rel
            ids = {json.loads(line)["id"] for line in (folder / "evidence.jsonl").read_text(encoding="utf-8").splitlines()}
            self.assertEqual(ids, train)
            self.assertNotIn("Study 10,", (folder / "evidence.jsonl").read_text(encoding="utf-8"))
        placebo = next(self.root / r for r in status["open"] if "placebo-r1" in r)
        self.assertIn("Your perspective: Lens P", (placebo / "instructions.md").read_text(encoding="utf-8"))
        no_lens = next(self.root / r for r in status["open"] if "no-lens-r1" in r)
        self.assertNotIn("Your perspective", (no_lens / "instructions.md").read_text(encoding="utf-8"))


class Checks(unittest.TestCase):
    cfg = {"propositions": {"per_source": 2}}
    meta = {"evidence_ids": [1, 2]}

    def prop(self, **kw):
        p = {"id": "R1", "claim": "Late starts lift mood.", "population": "teens", "contrast": "late versus early",
             "outcome": "mood", "direction": "higher", "moderators": [], "confidence": 0.6, "confirms": "higher mood",
             "contradicts": "lower mood", "basis": [1]}
        return {**p, **kw}

    def test_refusals(self):
        good = [self.prop(), self.prop(id="R2", population="adults")]
        self.assertEqual(referee.check_generate(self.meta, {"propositions": good}, self.cfg), [])
        bad = [self.prop(direction="depends"), self.prop(id="R2", claim="As Smith et al found in 2021.", basis=[9],
                                                         confidence=1.0)]
        errs = " | ".join(referee.check_generate(self.meta, {"propositions": bad}, self.cfg))
        for want in ("depends needs at least one moderator", "names a study", "basis", "confidence"):
            self.assertIn(want, errs)
        self.assertIn("exactly 2", " ".join(referee.check_generate(self.meta, {"propositions": good[:1]}, self.cfg)))
        same = [self.prop(), self.prop(id="R2")]
        self.assertIn("the same population", " ".join(referee.check_generate(self.meta, {"propositions": same}, self.cfg)))

    def test_weights_and_leaks(self):
        cfg = {"score": {"moderator_bonus": 0.25, "max_moderators": 2}}
        two = [{"condition": "a", "effect": "stronger"}, {"condition": "b", "effect": "reversed"}]
        self.assertAlmostEqual(referee.spec_weight({"direction": "lower", "moderators": two}, cfg), 1.0)
        self.assertAlmostEqual(referee.spec_weight({"direction": "higher", "moderators": []}, cfg), 1 / 1.5)
        self.assertAlmostEqual(referee.spec_weight({"direction": "depends", "moderators": two[:1]}, cfg), 0.3 * 1.25 / 1.5)
        self.assertTrue(referee.leaked(True, True, {1: "partly"}))
        self.assertTrue(referee.leaked(False, False, {1: "yes"}))
        self.assertFalse(referee.leaked(False, True, {1: "yes"}))     # the title gives it away
        self.assertFalse(referee.leaked(True, False, {1: "no"}))


class FullRound(Round):
    def test_freeze_masks_scores_and_registry(self):
        status = self.step(start=True)
        self.run_open(status)
        status = self.step()                                    # every proposition in: frozen, then the rest
        R = referee.round_(self.store, self.run, self.root)
        self.assertTrue(R["row"]["frozen_at"])
        props = referee.propositions(self.store, R["round"])
        self.assertEqual(len(props), 30)                        # 5 sources (with shuffled) x 2 replicates x 3
        self.assertEqual(R["row"]["frozen_sha256"], referee.digest(props))
        frozen = self.root / "output" / "corpus_Z" / f"run_{self.run:03d}" / "referee_r-test_frozen.json"
        self.assertEqual(json.loads(frozen.read_text(encoding="utf-8"))["sha256"], R["row"]["frozen_sha256"])
        kinds = sorted(self.meta(self.root / r)["kind"] for r in status["open"])
        self.assertEqual(kinds, ["ref_judge"] * 6 + ["ref_probe"] + ["ref_rate"] * 2)

        # the freeze holds: a proposition written now is refused
        gen = next(t for t in referee.tasks(self.store, R["round"]).values() if t["kind"] == "ref_generate")
        folder = self.root / gen["folder"]
        out = referee.import_task(folder, self.store, self.meta(folder), {}, "")
        self.assertIn("frozen", out["problems"][0])

        # judges see masked propositions and test findings only
        test = set(referee.split_findings(self.store, R, "test"))
        for rel in status["open"]:
            folder = self.root / rel
            if self.meta(folder)["kind"] != "ref_judge":
                continue
            text = (folder / "propositions.json").read_text(encoding="utf-8")
            for name in ("bold", "plain", "placebo", "shuffled", "no_lens", "confidence"):
                self.assertNotIn(name, text)
            for b in folder.glob("batch_[0-9][0-9].json"):
                self.assertTrue({f["id"] for f in json.loads(b.read_text(encoding="utf-8"))["findings"]} <= test)

        while status["open"]:
            self.run_open(status)
            status = self.step()
        self.assertTrue(status["done"])

        result = referee.evaluate(self.store, self.run, self.root)
        self.assertTrue(result["intact"])
        m = result["main"]
        self.assertEqual((m["findings"], result["flagged_findings"]), (6, 3))   # the remembered study is left out
        self.assertEqual(result["contamination"]["test"]["flagged"], 1)
        self.assertEqual(result["contamination"]["control"]["studies"], 3)
        self.assertGreater(m["sources"]["bold"]["score"], 0)
        self.assertLess(m["sources"]["placebo"]["score"], 0)
        self.assertEqual(m["sources"]["placebo"]["obvious"], 1.0)
        self.assertEqual(m["sources"]["bold"]["hit"], 1.0)
        self.assertEqual(m["sources"]["plain"]["coverage"], 0.0)
        c = m["comparisons"]["bold|placebo"]
        self.assertGreater(c["diff"], 0)
        self.assertLess(c["p"], 0.2)
        self.assertIsNotNone(result["with_flagged"])
        self.assertLess(result["agreement"]["all"]["agree"], 1.0)
        self.assertIsNotNone(result["agreement"]["all"]["kappa"])
        self.assertIsNotNone(m["sources"]["bold"]["brier"])
        text = referee.report(result)
        for want in ("## Leaderboard", "| Bold |", "Placebo (Lens P)", "## Leakage", "## Example propositions",
                     "still match that hash"):
            self.assertIn(want, text)
        json.dumps(result, default=str)

        # the standing registry: a new test study is probed and judged on its own
        w = self.add_study(20, 2026, "2026-10-01", "found", n=2)
        self.store.db.commit()
        status = self.step()
        metas = [self.meta(self.root / r) for r in status["open"]]
        self.assertEqual(sorted(x["kind"] for x in metas), ["ref_judge", "ref_judge", "ref_probe"])
        new = {r["finding_id"] for r in self.store.db.execute("SELECT finding_id FROM findings WHERE work_id = ?", (w,))}
        self.assertTrue(all(set(x["findings"]) == new for x in metas if x["kind"] == "ref_judge"))
        self.assertEqual(next(x for x in metas if x["kind"] == "ref_probe")["test"], [w])
        while status["open"]:
            self.run_open(status)
            status = self.step()
        result = referee.evaluate(self.store, self.run, self.root)
        self.assertEqual(result["main"]["findings"], 8)
        self.assertEqual(result["waves"], {0: 3, 1: 1})

        # a proposition changed after the freeze shows
        self.store.db.execute("UPDATE ref_propositions SET content = replace(content, 'mood', 'sleep') WHERE key = ?",
                              ("bold.r1.R1",))
        self.assertFalse(referee.evaluate(self.store, self.run, self.root)["intact"])


CLAIMS_FILE = '''
version = "test"
mode = "claims"
round = "c-test"
corpus = "Z"
run = {run}
seed = 1
domain = "adolescent sleep and mood"

[split]
T = "2026-07-01"
model_cutoff = "2026-06-30"
after_cutoff = true

[propositions]
per_source = 8

[[sources]]
id = "paper1"
name = "The general paper"
paper = {p1}

[[sources]]
id = "paper2"
name = "The proposal"
paper = {p2}

[baselines]
shuffled = true

[score]
bootstrap = 50
permutations = 50

[agents]
generate = "model:g"
judges = ["model:j1", "model:j2"]
findings_per_batch = 2
batches_per_task = 2
'''


class ClaimsRound(Round):
    """The papers' own claims, frozen and checked against the studies the run gains later."""

    def setUp(self):
        super().setUp()
        from ichnos import paper
        paper.ensure(self.store)
        self.papers = []
        for title in ("General", "Proposal"):
            content = {"title": title, "abstract": [{"sentences": [{"text": f"{title} claims.", "frame": True}]}],
                       "sections": []}
            self.papers.append(self.store.db.execute(
                "INSERT INTO papers (corpus, run_id, version, author, title, content, method, verification, created_at) "
                "VALUES ('Z', ?, 1, 'model:x', ?, ?, '{}', '{}', '2026-10-22T10:00:00+00:00')",
                (self.run, title, json.dumps(content))).lastrowid)
        self.store.db.commit()
        (self.root / "protocol" / "referee_corpus_z.toml").write_text(
            CLAIMS_FILE.format(run=self.run, p1=self.papers[0], p2=self.papers[1]), encoding="utf-8")

    def claims_answer(self, folder):
        first = self.meta(folder)["evidence_ids"][0]
        props = [{"id": f"R{n}", "claim": f"Claim {n} about mood.", "population": f"teens of type {n}",
                  "contrast": "late versus early school start", "outcome": "mood", "direction": "higher",
                  "moderators": [], "confidence": 0.7, "confirms": "mood is better", "contradicts": "mood is worse",
                  "basis": [first], "where": f"Conclusion, claim {n}"} for n in range(1, 7)]
        self.answer(folder, {"propositions": props})

    def test_claims_are_frozen_then_tested_by_later_studies_only(self):
        status = self.step(start=True)                          # no dates needed: every study now is seen
        R = referee.round_(self.store, self.run, self.root)
        self.assertEqual({s["split"] for s in referee.studies(self.store, R["round"]).values()}, {"training"})
        self.assertEqual(len(status["open"]), 2)
        for rel in status["open"]:
            folder = self.root / rel
            self.assertIn("claims.", (folder / "manuscript.md").read_text(encoding="utf-8"))
            self.assertIn("finished paper", (folder / "instructions.md").read_text(encoding="utf-8"))
            meta = self.meta(folder)
            self.assertEqual(len(meta["evidence_ids"]), len(STUDIES) * 3)
            errs = referee.check_generate(meta, {"propositions": []}, R["cfg"])
            self.assertIn("6 to 8 propositions, not 0", errs[0])
            self.claims_answer(folder)
            self.assertEqual(agents.import_task(folder, self.store)["problems"], [])
        status = self.step()
        self.assertEqual(status["open"], [])                    # no probe, no rating, nothing to judge yet
        props = referee.propositions(self.store, R["round"])
        self.assertEqual(Counter(p["source"] for p in props.values()), {"paper1": 6, "paper2": 6, "shuffled": 6})
        self.assertEqual(props["paper1.r1.R1"]["content"]["where"], "Conclusion, claim 1")
        s = referee.claims_summary(self.store, self.run, self.root)
        self.assertEqual((s["test_studies"], s["papers"][0]["claims"][0]["status"]), (0, "not tested yet"))
        self.assertTrue(s["intact"])

        # a search update: one study first published after the cutoff tests the claims, one from before it does not
        late = self.add_study(30, 2026, "2026-03-01", "found", n=1)
        new = self.add_study(31, 2026, "2026-08-20", "found", n=2)
        self.store.db.commit()
        status = self.step()
        metas = [self.meta(self.root / r) for r in status["open"]]
        self.assertEqual(sorted(x["kind"] for x in metas), ["ref_judge", "ref_judge"])
        mine = {r[0] for r in self.store.db.execute("SELECT finding_id FROM findings WHERE work_id = ?", (new,))}
        self.assertTrue(all(set(x["findings"]) == mine for x in metas))
        for rel in status["open"]:
            folder, meta = self.root / rel, self.meta(self.root / rel)
            text = (folder / "propositions.json").read_text(encoding="utf-8")
            self.assertNotIn("paper1", text)
            self.assertNotIn("Conclusion, claim", text)          # the judges see neither the paper nor where
            for b in sorted(folder.glob("batch_[0-9][0-9].json")):
                out = []
                for f in json.loads(b.read_text(encoding="utf-8"))["findings"]:
                    add = [{"key": k, "verdict": "supports" if key == "paper1.r1.R1" else "contradicts", "why": "w"}
                           for k, key in meta["mask"].items() if key in ("paper1.r1.R1", "paper2.r1.R2")]
                    out.append({"finding": f["id"], "addressed": add})
                self.answer(folder, {"findings": out}, b.stem)
            self.assertEqual(agents.import_task(folder, self.store)["problems"], [])
        self.assertTrue(self.step()["done"])
        s = referee.claims_summary(self.store, self.run, self.root)
        self.assertEqual((s["test_studies"], s["judged"], s["late"]), (1, 2, 1))
        c1 = s["papers"][0]["claims"][0]
        self.assertEqual((c1["status"], c1["for"], c1["studies_for"]), ("supported", 2, 1))
        c2 = s["papers"][1]["claims"][1]
        self.assertEqual((c2["status"], c2["against"]), ("contradicted", 2))
        self.assertEqual(s["papers"][1]["claims"][0]["status"], "not tested yet")
        self.assertEqual([c["key"][-2:] for c in s["papers"][0]["claims"]], ["R1", "R2", "R3", "R4", "R5", "R6"])

        import tomllib
        from ichnos.review import overview
        from tests.test_agents_map import PROTOCOL
        o = overview(self.store, self.run, tomllib.loads(PROTOCOL), self.root)
        self.assertEqual(o["forward"]["round"], "c-test")
        self.assertTrue(any(n["text"].startswith("Later studies contradict 1 of the 6 claims of The proposal")
                            for n in o["next"]))
        text = referee.report(referee.evaluate(self.store, self.run, self.root))
        for want in ("# The forward check of the papers", "## The proposal\n", "| contradicted |",
                     "still match that hash"):
            self.assertIn(want, text)

    def test_a_claims_file_needs_papers(self):
        cfg = tomllib.loads(CLAIMS_FILE.format(run=1, p1=1, p2=2))
        referee.check_config(cfg)
        del cfg["sources"][0]["paper"]
        with self.assertRaisesRegex(ValueError, "source paper1 needs `paper`"):
            referee.check_config(cfg)


if __name__ == "__main__":
    unittest.main()
