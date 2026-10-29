"""Lens evolution end to end with scripted agents: the split, probes, forecasts, the three arms, the sealed test
split, lineage labels, the judge and the recall probe."""

import json
import re
import unittest
from collections import Counter
from unittest import mock

from ichnos import agents, evolution
from ichnos.http import ApiError
from tests.test_agents_map import Base

LENS_FILE = '''
version = "test"

[interpret]
settings = ["teens"]

[[interpret.variables]]
id = "sleep"
contrast = "long vs short sleep"

[[lenses]]
id = "clock"
name = "Circadian"
role = "theory"
heuristic = "What did this do to the body clock?"
core = ["C1: Mood follows sleep timing.", "C2: Late chronotypes suffer in early schedules."]

[[lenses]]
id = "friends"
name = "Social"
role = "theory"
heuristic = "What did this do to friendships?"
core = ["F1: Mood follows social contact.", "F2: Screens displace friends."]

[[lenses]]
id = "placebo"
name = "Lens P"
role = "placebo"
heuristic = "What mattered?"
core = ["P1: People differ."]
'''

EVO_FILE = '''
version = "test"
game = "g-test"
seed = 1
islands = ["clock", "friends"]
placebo = "placebo"
domain = "adolescent sleep and mood"

[split]
test_from_year = 2024
validation_share = 0.5

[score]
classes = ["positive", "negative", "null", "mixed"]
aux_penalty = 0.005
log_floor = 0.005
bootstrap = 200

[schedule]
generations = 2
replicates = 2
crossover_from = 2

[agents]
probe = "model:p"
predict = "model:f"
mutate = "model:m"
judge = "model:j"

[archive]
levels = ["individual", "interpersonal"]
mechanisms = ["timing", "contact", "other"]

[archive.seeds]
clock = { level = "individual", mechanism = "timing" }
friends = { level = "interpersonal", mechanism = "contact" }
'''

SIGNS = ["positive", "negative", "null", "mixed", "positive; null", "not_applicable"]


class Game(Base):
    """A corpus Z run with 12 studies of 3 findings each, its lens file and its own evolution file, and scripted
    agents for every task kind."""

    def setUp(self):
        super().setUp()
        (self.root / "protocol" / "lenses_corpus_z.toml").write_text(LENS_FILE, encoding="utf-8")
        (self.root / "protocol" / "evolution_corpus_z.toml").write_text(EVO_FILE, encoding="utf-8")
        n = 0
        for i in range(12):
            w = self.store.upsert_work({"title": f"Study {i} of sleep", "year": 2016 + i, "doi": f"10.9/s{i}",
                                        "authors": [f"Ann Smithson{i}", "Bo Lee"], "venue": "J Sleep"})
            for k in range(3):
                sign = SIGNS[(i + k) % len(SIGNS)] if (i, k) != (0, 0) else "positive"
                fields = {"setting": "teens", "predictor": f"sleep length {k}", "outcome": "mood", "sign": sign}
                n += 1
                self.store.db.execute(
                    "INSERT INTO findings (work_id, run_id, extractor, finding, fields, evidence, status, created_at) "
                    "VALUES (?, ?, 'model:x', ?, ?, ?, 'extracted', '2026-01-01')",
                    (w, self.run, f"Finding {n}: mood was {sign} with sleep.", json.dumps(fields), f"passage {n}"))
        self.store.db.commit()

    # scripted agents

    def truth(self, game="g-test"):
        """The extraction's coded result of every finding of a game, by finding id."""
        return {r["finding_id"]: r["coded"] for r in self.store.db.execute(
            "SELECT finding_id, coded FROM evo_probes WHERE game = ?", (game,))}

    def answer(self, folder, obj, batch="batch_01"):
        (folder / f"{batch}_result.json").write_text(json.dumps(obj), encoding="utf-8")

    def step(self, game=None, run=None):
        return evolution.step(self.store, run or self.run, self.root, game)

    def forecast(self, folder, skill):
        meta = json.loads((folder / "task.json").read_text(encoding="utf-8"))
        data = json.loads((folder / "batch_01.json").read_text(encoding="utf-8"))
        truth = {k: t["answer"] for k, t in evolution.targets(self.store, meta["game"]).items()}
        cls = data["classes"]
        preds = []
        for q, f in meta["keys"].items():
            hit = cls.index(truth[str(f)])
            p = {c: (skill if k == hit else (1 - skill) / 3) for k, c in enumerate(cls)}
            basis = ["core:" + re.match(r"(\w+):", data["lens"]["core"][0]).group(1)] if "lens" in data else ["background"]
            x = {"probe": q, "p": p, "basis": basis, "why": "because"}
            if meta["mode"] == "recall":
                x["known"] = False
            preds.append(x)
        out = {"predictions": preds}
        if meta["mode"] == "lens":
            out["strains"] = [data["evidence"][0]["id"]] if "aux" not in json.dumps(data["lens"]).lower() else []
        self.answer(folder, out)
        return meta

    def skill_of(self, lens_id):
        if lens_id.startswith(("clock", "calm", "x.")):
            return 0.55 + 0.05 * lens_id.count(".")       # children of the clock island forecast better
        return {"friends": 0.4, "placebo": 0.3, "shuffled": 0.3, "no_card": 0.32, "prior": 0.28,
                "recall": 0.3}.get(lens_id, 0.38)

    def mutate(self, folder):
        meta = json.loads((folder / "task.json").read_text(encoding="utf-8"))
        data = json.loads((folder / "batch_01.json").read_text(encoding="utf-8"))
        p = data["parent"]
        if meta["operator"] == "crossover":
            child = {"name": "Joined view", "core": [p["core"][0], data["parent_b"]["core"][0]],
                     "change": "recombination"}
        else:
            child = {"name": f"Child {meta['child']}", "core": p["core"], "change": "belt"}
        child.update(heuristic="What changed tonight?", level="individual", mechanism="timing",
                     auxiliaries=[{"id": "A1", "statement": "Weekends reset the clock.",
                                   "consequence": "Monday mood is lowest."}],
                     anomalies=[data["evidence"][0]["id"]], rationale="Fits the anomaly.")
        self.answer(folder, child)
        return meta

    def run_open(self, status):
        for rel in status["open"]:
            folder = self.root / rel
            kind = json.loads((folder / "task.json").read_text(encoding="utf-8"))["kind"]
            if kind == "evo_probe":
                self.write_probes(folder)
            elif kind == "evo_predict":
                meta = json.loads((folder / "task.json").read_text(encoding="utf-8"))
                self.forecast(folder, self.skill_of(meta["lens_id"]))
            elif kind == "evo_mutate":
                self.mutate(folder)
            elif kind == "evo_judge":
                meta = json.loads((folder / "task.json").read_text(encoding="utf-8"))
                data = json.loads((folder / "batch_01.json").read_text(encoding="utf-8"))
                if meta["mode"] == "rank":
                    self.answer(folder, {"ranking": list(reversed(sorted(meta["mask"], key=lambda k: int(k[1:]))))})
                else:
                    choices = {k: v[-1] for k, v in data["islands"].items()}
                    pair = [data["islands"]["I1"][0], data["islands"]["I2"][0]]
                    self.answer(folder, {"choices": choices, "recombine": pair})
            counts = agents.import_task(folder, self.store)
            self.assertEqual(counts["problems"], [], folder.name)

    def write_probes(self, folder, extras=True):
        """Every finding read as coded; a further probe for another outcome, or a mixed finding split in two."""
        meta = json.loads((folder / "task.json").read_text(encoding="utf-8"))
        truth = self.truth(meta["game"])
        for b in sorted(folder.glob("batch_[0-9][0-9].json")):
            out = []
            for f in json.loads(b.read_text(encoding="utf-8"))["findings"]:
                coded = truth[f["id"]]
                x = {"finding": f["id"], "population": "teens in a cohort", "first": "long sleepers",
                     "second": "short sleepers", "outcome": "mood", "answer": coded}
                if extras:
                    x["more"] = ([{"population": "girls", "answer": "positive"},
                                  {"population": "boys", "answer": "negative"}] if coded == "mixed" else
                                 [{"outcome": "daytime sleepiness", "answer": coded}])
                out.append(x)
            self.answer(folder, {"probes": out}, b.stem)

    def probes(self):
        status = self.step()
        self.assertEqual(len(status["written"]), 1)
        folder = status["written"][0]
        text = json.dumps([json.loads(b.read_text(encoding="utf-8")) for b in folder.glob("batch_*.json")])
        self.assertNotIn('"sign"', text)                    # the probe writer reads the result itself
        truth = self.truth()
        batches = sorted(folder.glob("batch_[0-9][0-9].json"))
        for i, b in enumerate(batches):
            data = json.loads(b.read_text(encoding="utf-8"))
            out = []
            for f in data["findings"]:
                coded = truth[f["id"]]
                if f["id"] == data["findings"][0]["id"] and i == 0:
                    out.append({"finding": f["id"], "skip": "only describes"})
                    continue
                read = "null" if f["id"] == data["findings"][1]["id"] and i == 0 and coded != "null" else coded
                out.append({"finding": f["id"], "population": "teens in a cohort", "first": "long sleepers",
                            "second": "short sleepers", "outcome": "mood", "answer": read})
            self.answer(folder, {"probes": out}, b.stem)
        leaky = json.loads((batches[0].with_name("batch_01_result.json")).read_text(encoding="utf-8"))
        good = json.dumps(leaky)
        leaky["probes"][1]["outcome"] = "mood increased"
        leaky["probes"][2]["population"] = "teens studied by Smithson3"
        self.answer(folder, leaky)
        problems = agents.import_task(folder, self.store)["problems"]
        self.assertTrue(any("gives the result" in p for p in problems), problems)
        (batches[0].with_name("batch_01_result.json")).write_text(good, encoding="utf-8")
        self.assertEqual(agents.import_task(folder, self.store)["problems"], [])
        status = {r["status"] for r in self.store.db.execute("SELECT status FROM evo_probes WHERE game = 'g-test'")}
        self.assertEqual(status, {"evidence", "ok", "excluded"})


class Evolution(Game):

    def test_split_is_by_study_and_test_is_newest(self):
        evolution.game(self.store, self.run, self.root)
        rows = self.store.db.execute("SELECT p.work_id, p.split, w.year FROM evo_probes p JOIN works w USING (work_id) "
                                     "WHERE game = 'g-test'").fetchall()
        by = {}
        for r in rows:
            by.setdefault(r["work_id"], set()).add(r["split"])
            self.assertEqual(r["split"] == "test", r["year"] >= 2024)
        self.assertTrue(all(len(s) == 1 for s in by.values()))
        self.assertEqual({r["split"] for r in rows}, {"development", "validation", "test"})
        lenses = evolution.lens_rows(self.store, "g-test")
        self.assertEqual(lenses["shuffled"]["card"]["core"][0][:3], "S1:")
        self.assertEqual(len(lenses["shuffled"]["card"]["core"]), 2)

    def test_changed_file_stops_the_game(self):
        evolution.game(self.store, self.run, self.root)
        (self.root / "protocol" / "evolution_corpus_z.toml").write_text(EVO_FILE.replace("bootstrap = 200",
                                                                                         "bootstrap = 300"))
        with self.assertRaisesRegex(ValueError, "changed after game"):
            evolution.game(self.store, self.run, self.root)

    def test_bad_answers_are_refused(self):
        self.probes()
        status = self.step()
        folder = next(f for f in status["written"] if "clock-validation-r1" in f.name)
        meta = self.forecast(folder, 0.5)
        obj = json.loads((folder / "batch_01_result.json").read_text(encoding="utf-8"))
        obj["predictions"][0]["p"]["positive"] = 5
        obj["predictions"][1]["basis"] = ["core:Z9"]
        obj["predictions"] = obj["predictions"][:-1]
        self.answer(folder, obj)
        problems = agents.import_task(folder, self.store)["problems"]
        self.assertTrue(any("sum to" in p for p in problems))
        self.assertTrue(any("core:Z9" in p for p in problems))
        self.assertTrue(any("missing" in p for p in problems))
        data = json.loads((folder / "batch_01.json").read_text(encoding="utf-8"))
        self.assertTrue(all(e["id"] for e in data["evidence"]))
        dev = {r[0] for r in self.store.db.execute("SELECT finding_id FROM evo_probes WHERE game = 'g-test' AND "
                                                   "split = 'development'")}
        self.assertEqual({e["id"] for e in data["evidence"]}, dev)
        self.assertFalse({int(str(k).split(".")[0]) for k in meta["keys"].values()} & dev)

    def test_the_game_end_to_end(self):
        self.probes()
        status = self.step()
        self.assertEqual(len(status["written"]), 5 * 2 + 2)        # 5 lenses x 2 replicates, prior and recall
        recall = next(f for f in status["written"] if "recall" in f.name)
        self.assertIn("study", json.loads((recall / "batch_01.json").read_text(encoding="utf-8"))["probes"][0])
        self.assertEqual(self.step()["written"], [])               # nothing new until those are answered
        self.run_open(status)
        status = self.step()                                       # generation 1: one shared child per island
        self.assertEqual(sorted(f.name.split("__")[1] for f in status["written"]),
                         ["g-test-clock-g1", "g-test-friends-g1"])
        bad = status["written"][0]
        data = json.loads((bad / "batch_01.json").read_text(encoding="utf-8"))
        self.answer(bad, {"name": "New", "core": ["C1: Mood follows sleep timing in 2019."], "heuristic": "h",
                          "level": "individual", "mechanism": "timing", "change": "belt"})
        problems = agents.import_task(bad, self.store)["problems"]
        self.assertTrue(any("2 to 4" in p for p in problems) and any("general" in p for p in problems), problems)
        self.run_open(status)
        status = self.step()            # their forecasts; no_selection mutates at once, the judge arm asks the judge
        names = sorted(f.name.split("__")[1] for f in status["written"])
        self.assertIn("g-test-clock-g1-validation-r1", names)
        self.assertIn("g-test-parents-j-g2", names)
        self.assertIn("g-test-x-g2-n", names)
        self.assertFalse(any(n.endswith("-g2-s") for n in names))  # selection waits for the children's scores
        self.run_open(status)
        status = self.step()
        names = sorted(f.name.split("__")[1] for f in status["written"])
        self.assertIn("g-test-clock-g2-s", names)
        self.assertIn("g-test-clock-g2-j", names)
        chosen = {(c["arm"], c["island"]): c["parent"] for c in evolution.events(self.store, "g-test", "parent_chosen")}
        self.assertEqual(chosen[("selected", "clock")], "clock.g1")    # the fitter member
        self.assertEqual(sorted(chosen[("selected", "x")]), ["clock.g1", "friends"])
        self.assertFalse(status["test_opened"])
        for _ in range(4):
            if status["test_opened"]:
                break
            self.run_open(status)
            status = self.step()
        self.assertTrue(status["test_opened"])                     # evolution ended: the test split opens
        tests = [f for f in status["written"] if "-test-r1" in f.name]
        self.assertEqual(len(tests), 2 + 3 + 9)
        self.assertTrue(any("rank" in f.name for f in status["written"]))
        test_data = json.loads((tests[0] / "batch_01.json").read_text(encoding="utf-8"))
        val = {r[0] for r in self.store.db.execute("SELECT finding_id FROM evo_probes WHERE game = 'g-test' AND "
                                                   "split IN ('development', 'validation')")}
        self.assertEqual({e["id"] for e in test_data["evidence"]}, val)
        self.run_open(status)
        self.assertTrue(self.step()["done"])

        r = evolution.evaluate(self.store, self.run, self.root)
        v = r["validation"]
        self.assertGreater(v["clock"]["fitness"], v["friends"]["fitness"])
        self.assertGreater(v["clock"]["gain_placebo"][0], 0)
        self.assertAlmostEqual(r["noise_floor"], 0.0)              # scripted replicates are identical
        labels = {x["lens"]: x["label"] for x in r["lineage"]}
        self.assertEqual(labels["clock.g1"], "progressive")
        self.assertEqual(labels["friends.g1"], "degenerating")     # an auxiliary and fewer anomalies, no gain
        self.assertIsNotNone(r["comparisons"]["test"]["selected vs no_selection"][0])
        self.assertIsNotNone(r["transfer"])
        self.assertIsNotNone(r["judge"]["vs_test"])
        self.assertIn("recall_gain", r["contamination"]["test"])
        self.assertEqual(r["arms"]["selected"]["champion"][:5], "clock")
        text = evolution.report(r, self.store, self.run)
        for part in ("## Generation 0", "## Lineage", "## Arms", "## The LLM judge", "## Contamination",
                     "## Diversity", "progressive"):
            self.assertIn(part, text)
        self.assertEqual(r["changed_after_import"], [])
        rank = next(self.root.glob("output/corpus_Z/run_*/agents/evo_judge__g-test-rank"))
        (rank / "batch_01_result.json").write_text('{"ranking": []}', encoding="utf-8")
        self.assertEqual(evolution.changed_after_import(self.store, evolution.game(self.store, self.run, self.root)),
                         ["judge:rank"])
        sheet, key = evolution.rating_sheet(r, self.store)
        self.assertEqual(set(key), {"P1", "P2"})
        self.assertNotIn("Circadian", sheet)
        self.assertEqual(evolution.events(self.store, "g-test", "rating_key"), [key])


LENS_Y = '''
version = "test"

[interpret]
settings = ["adults"]

[[interpret.variables]]
id = "use"
contrast = "frequent vs rare use"

[[lenses]]
id = "calm"
name = "Calm"
role = "theory"
heuristic = "Did attention settle?"
core = ["K1: Mindful attention lowers worry.", "K2: Reappraisal turns threat into challenge."]

[[lenses]]
id = "threat"
name = "Threat"
role = "theory"
heuristic = "What was at stake?"
core = ["T1: Worry follows perceived threat.", "T2: Resources buffer threat."]

[[lenses]]
id = "placebo"
name = "Lens P"
role = "placebo"
heuristic = "What mattered?"
core = ["P1: People differ."]
'''

POOL_FILE = '''
version = "test-0.2"
game = "pool-test"
seed = 2

[split]
test = "after_cutoff"
cutoff = "2025-06-30"
validation_share = 0.5
evidence_max = 10

[probes]
per_finding = 3
identity = "anonymous"

[score]
classes = ["positive", "negative", "null", "mixed"]
aux_penalty = 0.015
log_floor = 0.005
bootstrap = 200

[schedule]
generations = 2
crossover_from = 2

[replicates]
seeds = 3
children = 2
test = 2

[agents]
probe = "model:p"
predict = "model:f"
mutate = "model:m"
judge = "model:j"
probes_per_task = 8

[[parts]]
game = "g-z2"
corpus = "Z"
run = RUN_Z
islands = ["clock", "friends"]
placebo = "placebo"
domain = "adolescent sleep and mood"

[parts.archive]
levels = ["individual", "interpersonal"]
mechanisms = ["timing", "contact", "other"]

[parts.archive.seeds]
clock = { level = "individual", mechanism = "timing" }
friends = { level = "interpersonal", mechanism = "contact" }

[[parts]]
game = "g-y1"
corpus = "Y"
run = RUN_Y
islands = ["calm", "threat"]
placebo = "placebo"
domain = "worry about machines"

[parts.coding]
field = "direction"
map = { positive_association = "positive", negative_association = "negative", no_association = "null", mixed = "mixed" }

[parts.archive]
levels = ["individual", "interpersonal"]
mechanisms = ["timing", "contact", "other"]

[parts.archive.seeds]
calm = { level = "individual", mechanism = "timing" }
threat = { level = "interpersonal", mechanism = "contact" }
'''

DIRECTIONS = ["positive_association", "negative_association", "no_association", "mixed", "differs_by_group",
              "positive_association; no_association"]
# Crossref records: s9 (2025) appeared before the cutoff, s10 (2026) after it (registered in December 2025), s11
# (2027) is unknown to Crossref; both corpus Y studies from 2025 on appeared after the cutoff.
CROSSREF = {"10.9/s9": {"created": {"date-parts": [[2025, 5, 2]]}, "published-online": {"date-parts": [[2025, 7]]}},
            "10.9/s10": {"published-print": {"date-parts": [[2026, 1]]}, "created": {"date-parts": [[2025, 12, 20]]}},
            "10.8/w8": {"issued": {"date-parts": [[2025, 9]]}},
            "10.8/w9": {"created": {"date-parts": [[2026, 2, 3]]}}}


class Crossref:
    def __init__(self):
        self.asked = []

    def get_json(self, source, url, params):
        doi = url.split("/works/", 1)[1]
        self.asked.append(doi)
        if doi not in CROSSREF:
            raise ApiError(source, 404, "not found")
        return {"message": CROSSREF[doi]}


class Pooled(Game):
    """Game 0.2: a pooled file with a game on corpus Z and one on corpus Y (coded by `direction`), further probes,
    replicates of every forecast, forecasts split into several tasks and a test split after a training cutoff."""

    def setUp(self):
        super().setUp()
        proto = (self.root / "protocol" / "corpus_z.toml").read_text(encoding="utf-8")
        (self.root / "protocol" / "corpus_y.toml").write_text(proto, encoding="utf-8")
        self.run_y = self.store.start_run("pilot", "Y", self.store.add_protocol("Y", "protocol/corpus_y.toml", proto), {})
        (self.root / "protocol" / "lenses_corpus_y.toml").write_text(LENS_Y, encoding="utf-8")
        for i in range(10):
            w = self.store.upsert_work({"title": f"Worry study {i}", "year": 2017 + i, "doi": f"10.8/w{i}",
                                        "authors": [f"Cy Marlowe{i}"], "venue": "J Worry"})
            for k in range(3):
                fields = {"anxiety_type": "ai", "process": f"process {k}", "outcome": "worry",
                          "direction": DIRECTIONS[(i + k) % len(DIRECTIONS)]}
                self.store.db.execute(
                    "INSERT INTO findings (work_id, run_id, extractor, finding, fields, evidence, status, created_at) "
                    "VALUES (?, ?, 'model:x', ?, ?, ?, 'extracted', '2026-01-01')",
                    (w, self.run_y, f"Worry finding {i}.{k}.", json.dumps(fields), f"worry passage {i}.{k}"))
        self.store.db.commit()
        self.pool_text = POOL_FILE.replace("RUN_Z", str(self.run)).replace("RUN_Y", str(self.run_y))
        (self.root / "protocol" / "evolution_corpus_zy.toml").write_text(self.pool_text, encoding="utf-8")

    def cfg(self, corpus, game, run):
        return evolution.load(corpus, self.root, game, run)[2]

    def dates(self):
        http = Crossref()
        evolution.fetch_dates(self.store, self.run, self.cfg("Z", "g-z2", self.run), http)
        evolution.fetch_dates(self.store, self.run_y, self.cfg("Y", "g-y1", self.run_y), http)
        return http

    def test_plan_dates_and_the_start_guard(self):
        p = evolution.plan(self.store, self.root, "pool-test")
        self.assertEqual([g["game"] for g in p["games"]], ["g-z2", "g-y1"])
        self.assertFalse(any(g["begun"] for g in p["games"]))
        self.assertEqual(p["games"][0]["dates"]["missing"], 3)             # 2025, 2026, 2027 may postdate the cutoff
        self.assertEqual(self.store.db.execute("SELECT name FROM sqlite_master WHERE name LIKE 'evo_%'").fetchall(), [])
        self.assertFalse((self.root / "output").exists())                  # the plan wrote nothing
        self.assertIn("Pooled:", evolution.plan_text(p))
        # the corpus's own file stays its default game; a pooled part is chosen by its id or its pool's
        self.assertEqual(self.cfg("Z", None, self.run)["game"], "g-test")
        z = self.cfg("Z", "g-z2", self.run)
        self.assertEqual((z["pool"], z["islands"], z["score"]["aux_penalty"]), ("pool-test", ["clock", "friends"], 0.015))
        self.assertEqual(self.cfg("Y", None, self.run_y)["game"], "g-y1")
        self.assertEqual(self.cfg("Y", "pool-test", self.run_y)["game"], "g-y1")
        with self.assertRaisesRegex(ValueError, "has not begun"):
            evolution.step(self.store, self.run, self.root, "g-z2", start=False)
        with self.assertRaisesRegex(ValueError, "no publication date"):
            evolution.step(self.store, self.run, self.root, "g-z2")
        http = self.dates()
        self.assertEqual(sorted(http.asked), ["10.8/w8", "10.8/w9", "10.9/s10", "10.9/s11", "10.9/s9"])
        self.assertEqual(evolution.fetch_dates(self.store, self.run, z, http), Counter(cached=3))
        published = [r["published"] for r in self.store.db.execute(
            "SELECT e.published FROM evo_dates e JOIN works w USING (work_id) WHERE w.doi LIKE '10.9/%'")]
        self.assertEqual(sorted(v or "" for v in published), ["", "2025-05-02", "2025-12-20"])
        p = evolution.plan(self.store, self.root, "g-z2")
        self.assertEqual((p["games"][0]["dates"]["after"], p["games"][0]["split"]["test"]["studies"]), (1, 1))
        G = evolution.game(self.store, self.run, self.root, "g-z2")
        self.assertEqual(G["sha"], evolution._sha(self.pool_text))
        rows = self.store.db.execute("SELECT DISTINCT w.doi FROM evo_probes p JOIN works w USING (work_id) "
                                     "WHERE game = 'g-z2' AND split = 'test'").fetchall()
        self.assertEqual([r["doi"] for r in rows], ["10.9/s10"])           # after the cutoff; s11 has no date
        self.assertEqual(len(evolution.events(self.store, "g-z2", "game_started")[0]["undated"]), 1)
        self.assertTrue(evolution.plan(self.store, self.root, "pool-test")["games"][0]["begun"])
        (self.root / "protocol" / "evolution_corpus_zy.toml").write_text(
            self.pool_text.replace("aux_penalty = 0.015", "aux_penalty = 0.02"), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "changed after game g-z2"):
            evolution.step(self.store, self.run, self.root, "g-z2")

    def test_settings_are_checked(self):
        lens_cfg = {"lenses": [{"id": i} for i in ("clock", "friends", "placebo")]}
        z = self.cfg("Z", "g-z2", self.run)
        for bad, msg in ((dict(z, split={"test": "after_cutoff"}), "cutoff"),
                         (dict(z, replicates={"seeds": 0}), "replicates"),
                         (dict(z, probes={"identity": "secret"}), "identity"),
                         (dict(z, coding={"field": "direction", "map": {"up": "upward"}}), "coding")):
            with self.assertRaisesRegex(ValueError, msg):
                evolution.check_config(bad, lens_cfg)
        pool = {"game": "p", "split": {"test": "after_cutoff", "cutoff": "2025-06-30", "validation_share": 0.4},
                "parts": [{"game": "a", "split": {"test": "year", "test_from_year": 2024}}]}
        self.assertEqual(evolution.part(pool, "a")["split"],                # a part's table changes what it names
                         {"test": "year", "cutoff": "2025-06-30", "validation_share": 0.4, "test_from_year": 2024})

    def test_a_corpus_coded_by_its_own_field(self):
        self.dates()
        status = self.step("g-y1", self.run_y)
        coded = {r["finding_id"]: (r["coded"], r["status"], r["note"]) for r in self.store.db.execute(
            "SELECT * FROM evo_probes WHERE game = 'g-y1'")}
        directions = {r["finding_id"]: json.loads(r["fields"])["direction"] for r in self.store.db.execute(
            "SELECT finding_id, fields FROM findings WHERE run_id = ?", (self.run_y,))}
        want = {"positive_association": "positive", "negative_association": "negative", "no_association": "null",
                "mixed": "mixed", "differs_by_group": None, "positive_association; no_association": "mixed"}
        for f, (c, st, note) in coded.items():
            self.assertEqual(c, want[directions[f]])
            if st == "excluded":
                self.assertEqual(note, "no sign to forecast (differs_by_group)")
        text = json.dumps([json.loads(b.read_text(encoding="utf-8")) for f in status["written"]
                           for b in f.glob("batch_*.json")])
        self.assertNotIn("direction", text)                                # the probe writer reads the result itself
        self.assertNotIn("_association", text)
        self.assertIn("anxiety_type", text)

    def test_further_probes(self):
        self.dates()
        folder = self.step("g-z2")["written"][0]
        instructions = (folder / "instructions.md").read_text(encoding="utf-8")
        self.assertIn("further probes", instructions)
        self.assertIn("Name no sample", instructions)
        self.write_probes(folder)
        result = folder / "batch_01_result.json"
        good = json.loads(result.read_text(encoding="utf-8"))
        bad = json.loads(json.dumps(good))
        bad["probes"][0]["more"] = [{"outcome": f"outcome {k}", "answer": "null"} for k in "abc"]
        bad["probes"][1]["more"] = [{"outcome": "mood increased", "answer": "positive"}]
        bad["probes"][2]["population"] = "teens in 12 schools"
        bad["probes"][3]["more"] = [{"answer": "positive"}]
        bad["probes"][4]["more"] = [{"outcome": "sleepiness", "answer": "mixed"}]
        self.answer(folder, bad)
        problems = agents.import_task(folder, self.store)["problems"]
        for part in ("at most 2 further probes", "further probe 2: `outcome` gives the result", "'12'",
                     "give the part(s)", "answer must be one of"):
            self.assertTrue(any(part in p for p in problems), (part, problems))
        truth = self.truth("g-z2")
        probes = good["probes"]
        plain = [x for x in probes if truth[x["finding"]] != "mixed"]
        split_ = [x for x in probes if truth[x["finding"]] == "mixed"]
        other = "null" if truth[plain[0]["finding"]] != "null" else "positive"
        plain[0]["more"].append({"population": "boys", "answer": other})     # contradicts the finding's coding
        plain[1]["answer"] = "mixed" if truth[plain[1]["finding"]] != "mixed" else "null"   # its own probe disagrees
        split_[0]["more"] = split_[0]["more"][:1]                            # a mixed finding with one part only
        self.answer(folder, good)
        self.assertEqual(agents.import_task(folder, self.store)["problems"], [])
        extra = {(r["finding_id"], r["k"]): (r["status"], r["note"]) for r in self.store.db.execute(
            "SELECT * FROM evo_extra WHERE game = 'g-z2'")}
        self.assertEqual(extra[(plain[0]["finding"], 2)][0], "ok")
        self.assertIn("coded the whole finding", extra[(plain[0]["finding"], 3)][1])
        self.assertEqual(extra[(plain[1]["finding"], 2)], ("excluded", "the finding's own probe did not count"))
        self.assertIn("one result only", extra[(split_[0]["finding"], 2)][1])
        for x in split_[1:]:
            self.assertEqual({extra[(x["finding"], k)][0] for k in (2, 3)}, {"ok"})
        tg = evolution.targets(self.store, "g-z2")
        key = f"{plain[0]['finding']}.2"
        self.assertEqual(tg[key]["answer"], truth[plain[0]["finding"]])
        self.assertIn("daytime sleepiness", tg[key]["question"])
        status = self.step("g-z2")
        asked = {str(k) for f in status["written"]
                 for k in json.loads((f / "task.json").read_text(encoding="utf-8"))["keys"].values()}
        self.assertIn(key, asked)

    def play(self, game, run, until=None, patience=12):
        status = self.step(game, run)
        for _ in range(patience):
            if status["done"] or (until and until(status)):
                break
            self.run_open(status)
            status = self.step(game, run)
        return status

    def test_pooled_game_end_to_end(self):
        self.dates()
        with mock.patch.object(evolution, "PROBE_BATCH", 2):                 # several probe-writer tasks
            G = evolution.game(self.store, self.run_y, self.root, "g-y1")
            status = self.step("g-y1", self.run_y)
            groups = evolution.probe_groups(self.store, G)
            self.assertGreater(len(groups), 1)
            self.assertEqual(len(status["written"]), len(groups))
            ids = [x["id"] for f in status["written"] for b in f.glob("batch_*.json")
                   for x in json.loads(b.read_text(encoding="utf-8"))["findings"]]
            self.assertEqual(sorted(ids), sorted(f for g in groups for f in g))   # each finding in one task
            y_probe_tasks = evolution.schedule_calls(G["cfg"], 0, 0, len(ids))["probes"]["evo_probe"]
            self.run_open(status)
        # corpus Z: generation 0 runs every seed and control three times, each forecast split into several tasks
        status = self.play("g-z2", self.run, until=lambda s: any("-g1" in f for f in s["open"]))
        tasks = evolution.tasks(self.store, "g-z2")
        gen0 = [n for n in tasks if n.startswith("predict:") and ":validation:" in n]
        n_val = len(evolution.targets(self.store, "g-z2", "validation"))
        n_test = len(evolution.targets(self.store, "g-z2", "test"))
        z = self.cfg("Z", "g-z2", self.run)
        kv = evolution.chunks(z, n_val)
        self.assertGreater(kv, 1)
        self.assertEqual(len(gen0), 5 * 3 * kv)
        self.assertTrue(any(n.startswith("predict:clock:validation:3:") for n in gen0))
        mutate = next(f for f in status["open"] if "-g1" in f)
        self.assertIn("0.015 in Brier score", (self.root / mutate / "instructions.md").read_text(encoding="utf-8"))
        # generation 1's children: selection waits until both runs of every child are in
        self.run_open(status)
        status = self.step("g-z2", self.run)
        first = [f for f in status["open"] if "-g1-validation-r1-c1" in f]
        self.run_open({"open": first})
        self.assertFalse(evolution.forecasts(self.store, "g-z2").get(("clock.g1", "validation")))   # one part only
        self.run_open({"open": [f for f in status["open"] if "-g1-validation-r1" in f and f not in first]})
        status = self.step("g-z2", self.run)
        self.assertEqual(len(evolution.forecasts(self.store, "g-z2")[("clock.g1", "validation")]), 1)
        self.assertFalse(any(f.endswith("-g2-s") for f in status["open"]))
        self.run_open({"open": [f for f in status["open"] if "-g1-validation-r2" in f]})
        status = self.step("g-z2", self.run)
        self.assertTrue(any(f.endswith("clock-g2-s") for f in status["open"]))
        status = self.play("g-z2", self.run)
        self.assertTrue(status["done"])
        tasks = evolution.tasks(self.store, "g-z2")
        n_asked = sum(1 for _ in self.store.db.execute(
            "SELECT 1 FROM evo_probes WHERE game = 'g-z2' AND split != 'development' AND coded IS NOT NULL"))
        planned = sum(evolution.schedule_calls(z, n_val, n_test, n_asked).values(), Counter())
        self.assertEqual(Counter(t["kind"] for t in tasks.values()), +planned)   # the plan counts what was written
        self.assertTrue(any(n.startswith("predict:x.g2.S:test:2") for n in tasks))
        shown = Counter()                       # one sample of the development findings, in every task that shows them
        for t in tasks.values():
            if t["kind"] in ("evo_predict", "evo_mutate", "evo_judge"):
                meta = json.loads((self.root / t["folder"] / "task.json").read_text(encoding="utf-8"))
                data = json.loads((self.root / t["folder"] / "batch_01.json").read_text(encoding="utf-8"))
                if "evidence" in data and meta.get("split") != "test":
                    shown[tuple(e["id"] for e in data["evidence"])] += 1
        self.assertEqual([len(k) for k in shown], [10])
        v = evolution.plan(self.store, self.root, "g-z2")["games"][0]["variance"]   # resized with its own noise
        self.assertTrue(v["source"].startswith("measured on generation 0 of g-z2"))
        self.assertEqual((v["sigma_probe"], v["sigma_run"]), (0, 0))       # scripted runs do not differ
        r = evolution.evaluate(self.store, self.run, self.root, "g-z2")
        self.assertAlmostEqual(r["noise_floor"], 0.0)
        self.assertGreater(r["splits"]["validation"]["further"], 0)
        text = evolution.report(r, self.store, self.run)
        for part in ("corpus Z, game g-z2", "--game g-z2", "first published after 2025-06-30", "Further probes",
                     "evolution_corpus_zy.toml"):
            self.assertIn(part, text)
        # corpus Y, then the two games pooled
        status = self.play("g-y1", self.run_y)
        self.assertTrue(status["done"])
        y_tasks = Counter(t["kind"] for t in evolution.tasks(self.store, "g-y1").values())
        self.assertEqual(y_tasks["evo_probe"], y_probe_tasks)
        pool = evolution.evaluate_pool(self.store, "pool-test", self.root)
        self.assertEqual(pool["games"], ["g-z2", "g-y1"])
        self.assertEqual(pool["probes"]["test"], n_test + len(evolution.targets(self.store, "g-y1", "test")))
        self.assertIsNotNone(pool["comparisons"]["test"]["selected vs no_selection"][0])
        self.assertIsNotNone(pool["transfer"])
        self.assertIn("pooled game pool-test", evolution.pool_report(pool))

    def test_power(self):
        many = evolution.power(0.02, 400, 1.2, 3, 3, 3, 3)
        few = evolution.power(0.02, 25, 1.2, 1, 1)
        self.assertGreater(many["power"], 0.6)
        self.assertLess(few["power"], 0.2)
        self.assertIsNone(evolution.power(0.005, 400, 1.2, 1, 1)["n_for_80"])   # only more runs can get there
        self.assertAlmostEqual(evolution.deff(1.0), 1.0)
        self.assertGreater(evolution.deff(6, 2), evolution.deff(3, 1))


class Intervals(unittest.TestCase):
    """In a game with [replicates] the bootstrap resamples each lens's runs as well as whole studies, so a shift of a
    whole forecast task widens the intervals; a game without [replicates] keeps its runs fixed."""

    def scores(self, shift: float, replicated: bool = True) -> evolution.Scores:
        """A lens and the placebo, three runs each, on 12 studies of 3 probes. The lens's runs differ from its scripted
        per-probe Brier score by -shift, 0 and +shift on every probe; the placebo's runs are identical."""
        cfg = {"score": {"bootstrap": 1000}, "schedule": {"replicates": 3}, "placebo": "placebo"}
        if replicated:
            cfg["replicates"] = {"seeds": 3, "children": 3, "test": 3}
        tg = {str(f): {"work_id": f // 3, "split": "validation", "answer": "positive"} for f in range(36)}

        def run(brier, d):          # [1 - q, q, 0, 0] against "positive" scores 2q^2: the Brier score asked for
            return {"by_finding": {f: {"p": [1 - (q := ((brier(int(f)) + d) / 2) ** 0.5), q, 0.0, 0.0]} for f in tg},
                    "strains": []}

        lens, placebo = (lambda f: 0.2 + 0.1 * (f * 7 % 5)), (lambda f: 0.35 + 0.08 * (f * 5 % 7))
        fc = {("lens", "validation"): [run(lens, d) for d in (-shift, 0.0, shift)],
              ("placebo", "validation"): [run(placebo, 0.0) for _ in range(3)]}
        rows = {"lens": {"generation": 0}, "placebo": {"generation": 0}}
        with mock.patch.object(evolution, "targets", return_value=tg), \
                mock.patch.object(evolution, "forecasts", return_value=fc), \
                mock.patch.object(evolution, "lens_rows", return_value=rows), \
                mock.patch.object(evolution, "base_rate", return_value=[0.25] * 4):
            return evolution.Scores(None, {"cfg": cfg, "game": "g-intervals"}, "validation")

    def test_runs_that_shift_widen_the_interval(self):
        same, shifted = self.scores(0.0).gain("lens", "placebo"), self.scores(0.1).gain("lens", "placebo")
        fixed = self.scores(0.1, replicated=False).gain("lens", "placebo")
        self.assertAlmostEqual(shifted[0], same[0])                      # the shifts cancel in the mean
        self.assertGreater(shifted[2] - shifted[1], 1.5 * (same[2] - same[1]))
        self.assertLess(shifted[1], same[1])
        self.assertGreater(shifted[2], same[2])
        for a, b in zip(same, fixed):                                   # identical runs add nothing to resample, and
            self.assertAlmostEqual(a, b, places=12)                     # without [replicates] the runs stay fixed
        self.assertEqual(shifted, self.scores(0.1).gain("lens", "placebo"))   # seeded
        # the arm comparisons weigh each lens of a group the same way
        S = self.scores(0.1)
        group = evolution.cluster_ci(evolution.group_diffs(S, ["lens"], ["placebo"]), S.study, S.n,
                                     "lens-placebo-validation",
                                     S.run_terms(evolution.group_weights(S, ["lens"], ["placebo"])))
        self.assertEqual(group, shifted)
