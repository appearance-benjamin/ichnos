"""Discovery: candidates found by code across projects, kept across sweeps, chosen on the Discovery page."""

import collections
import json
import unittest

from psyscientist import agents, discovery, insights, projects
from psyscientist.app import App
from psyscientist.config import Settings
from psyscientist.store import Store, now
from tests.test_agents_map import Base

FINDINGS = '''
[findings]
question = "What does sleep do to mood and grades?"
matrix = ["exposure", "outcome"]

[[findings.fields]]
name = "exposure"
question = "Exposure"
categories = ["sleep", "screen", "other"]

[[findings.fields]]
name = "outcome"
question = "Outcome"
categories = ["mood", "grades", "mood_symptoms", "other"]

[[findings.fields]]
name = "direction"
question = "Direction"
categories = ["better", "worse", "no_difference", "mixed"]

[[findings.fields]]
name = "evidence_type"
question = "Design"
categories = ["experiment", "cross_sectional"]
'''


def protocol(corpus: str) -> str:
    return f'corpus = "{corpus}"\ntitle = "Toy corpus {corpus}"\n' + FINDINGS


class Discovery(Base):
    def setUp(self):
        super().setUp()
        self.store.add_protocol("Z", "protocol/corpus_z.toml", protocol("Z"))
        self.works = [self.store.upsert_work({"title": f"Study {i}", "year": 2015 + i % 3, "doi": f"10.8/{i}"})
                      for i in range(1, 13)]
        self.store.add_sample(self.run, self.works, "pilot_screening")
        # 1-3: sleep improves mood in experiments; 4-6: sleep worsens mood in surveys; 7-12: screens worsen grades
        for i, w in enumerate(self.works):
            if i < 6:
                self.finding(self.run, w, "sleep", "mood", "better" if i < 3 else "worse",
                             "experiment" if i < 3 else "cross_sectional")
            else:
                self.finding(self.run, w, "screen", "grades", "worse", "cross_sectional")

    def finding(self, run, work, exposure, outcome, direction, design="cross_sectional"):
        fields = {"exposure": exposure, "outcome": outcome, "direction": direction, "evidence_type": design}
        with self.store.db:
            return self.store.db.execute(
                "INSERT INTO findings (work_id, run_id, extractor, finding, fields, evidence, created_at) "
                "VALUES (?, ?, 'model:x', ?, ?, ?, ?)",
                (work, run, f"{exposure} and {outcome}", json.dumps(fields), f"passage of study {work}", now())).lastrowid

    def by_kind(self, kind):
        return [d for d in discovery.find(self.store, self.root, log=lambda *a: None) if d["kind"] == kind]

    def test_opposite_directions_on_one_question_are_a_contradiction_with_its_moderator(self):
        [c] = self.by_kind("contradiction")
        self.assertEqual(c["corpora"], ["Z"])
        self.assertEqual(c["evidence"]["on"], {"exposure": "sleep", "outcome": "mood"})
        self.assertEqual((len(c["evidence"]["up"]), len(c["evidence"]["down"])), (3, 3))
        self.assertIn("3 studies better, 3 worse", c["summary"])
        top = c["evidence"]["moderators"][0]
        self.assertEqual((top["field"], abs(top["diff"])), ("evidence_type", 1.0))
        self.assertTrue(all(q["text"].startswith("passage of study") for q in c["evidence"]["quotes"]))

    def test_one_study_on_each_side_is_not_a_contradiction(self):
        with self.store.db:
            self.store.db.execute("DELETE FROM findings WHERE work_id IN (?, ?, ?, ?)", tuple(self.works[1:5]))
        self.assertEqual(self.by_kind("contradiction"), [])

    def test_a_cell_the_margins_make_likely_but_nobody_studied_is_a_gap(self):
        gaps = {(d["evidence"]["row"][1], d["evidence"]["col"][1]): d for d in self.by_kind("gap")}
        self.assertEqual(set(gaps), {("sleep", "grades"), ("screen", "mood")})
        self.assertEqual((gaps[("sleep", "grades")]["evidence"]["expected"], gaps[("sleep", "grades")]["strength"]),
                         (3.0, 3.0))

    def test_an_outcome_two_projects_study_is_a_bridge_but_not_between_a_project_and_its_strand(self):
        sha = self.store.add_protocol("Y", "protocol/corpus_y.toml", protocol("Y"))
        run = self.store.start_run("pilot", "Y", sha, {})
        ws = [self.store.upsert_work({"title": f"Other {i}", "year": 2020, "doi": f"10.7/{i}"}) for i in range(3)]
        self.store.add_sample(run, ws, "pilot_screening")
        for w in ws:
            self.finding(run, w, "other", "mood_symptoms", "worse")
        [b] = self.by_kind("bridge")
        self.assertEqual((b["corpora"], b["evidence"]["concept"], b["strength"]), (["Y", "Z"], "mood", 3.0))
        self.assertIn("Studies in both: 0", b["summary"])
        (self.root / "protocol" / "projects.toml").write_text(
            '[[project]]\ncorpus = "Y"\nname = "Strand"\nstrand_of = "Z"\n', encoding="utf-8")
        self.assertEqual(self.by_kind("bridge"), [])

    def test_the_same_insight_from_three_separate_passes_is_a_cluster(self):
        text = "Experiments find better mood after longer sleep while surveys find worse mood"
        for who in ("a", "b", "c"):
            insights.add(self.store, "Z", f"human:{who}", "manual", "contradiction", text + f" ({who})",
                         works=self.works[:2])
        insights.add(self.store, "Z", "human:d", "manual", "method", text)          # about the pipeline: left out
        [c] = self.by_kind("cluster")
        self.assertEqual((len(c["evidence"]["insights"]), c["strength"]), (3, 3.0))

    def test_two_passes_saying_the_same_are_not_yet_a_cluster(self):
        for who in ("a", "a", "b"):
            insights.add(self.store, "Z", f"human:{who}", "manual", "pattern", "Longer sleep goes with better mood")
        self.assertEqual(self.by_kind("cluster"), [])

    def test_a_value_much_more_common_among_recent_studies_is_a_trend(self):
        c = discovery.Corpus("Z", self.run, "Toy", {})
        c.fields = ["exposure"]
        for w in range(1, 41):
            recent = w > 20
            c.years[w] = 2025 if recent else 2015
            c.labels[w] = {"exposure": {"screen" if recent and w % 2 else "sleep"}}
        [t] = discovery.trends(c, this_year=2026)
        self.assertEqual((t["evidence"]["value"], t["evidence"]["recent"], t["evidence"]["earlier"]),
                         ("screen", [10, 20], [0, 20]))
        self.assertEqual(t["strength"], 50.0)

    def test_a_sweep_keeps_choices_and_marks_what_it_no_longer_finds(self):
        counts = discovery.sweep(self.store, self.root, log=lambda *a: None)
        self.assertEqual((counts["contradiction"], counts["gap"]), (1, 2))
        [c] = [d for d in discovery.rows(self.store) if d["kind"] == "contradiction"]
        discovery.set_status(self.store, c["discovery_id"], "chosen", "human:reviewer", "a moderator paper")
        discovery.sweep(self.store, self.root, log=lambda *a: None)
        [again] = [d for d in discovery.rows(self.store) if d["kind"] == "contradiction"]
        self.assertEqual((again["discovery_id"], again["status"], again["note"]), (c["discovery_id"], "chosen",
                                                                                  "a moderator paper"))
        with self.store.db:
            self.store.db.execute("DELETE FROM findings WHERE work_id IN (?, ?)", tuple(self.works[:2]))
        discovery.sweep(self.store, self.root, log=lambda *a: None)
        self.assertFalse([d for d in discovery.rows(self.store) if d["kind"] == "contradiction"])
        [gone] = [d for d in discovery.rows(self.store, include_gone=True) if d["kind"] == "contradiction"]
        self.assertTrue(gone["gone"])
        with self.assertRaises(ValueError):
            discovery.set_status(self.store, c["discovery_id"], "maybe", "human:reviewer")

    def test_the_page_the_start_page_and_choosing_in_the_app(self):
        discovery.sweep(self.store, self.root, log=lambda *a: None)
        page = discovery.page(self.store, self.root, "tok")
        self.assertIn("Mixed directions on sleep, mood", page)
        self.assertIn('data-kind="gap"', page)
        self.assertIn('"tok"', page)
        self.assertIn("Open Discovery", projects.page(self.store, self.root))
        app = App(Settings(None, None, None, self.root / "evidence.sqlite"), self.root, "Tester")
        [d] = [d for d in discovery.rows(self.store) if d["kind"] == "contradiction"]
        app.discovery_status(self.store, {"id": d["discovery_id"], "status": "set_aside", "note": "later"})
        [d] = [x for x in discovery.rows(self.store) if x["discovery_id"] == d["discovery_id"]]
        self.assertEqual((d["status"], d["decided_by"]), ("set_aside", "human:tester"))
        self.assertIn("[set aside]", (self.root / "notes" / "discoveries.md").read_text(encoding="utf-8"))


class Pipeline(Discovery):
    """Chosen discoveries: scoring by an agent, then the novelty check under the tournament "discovery"."""

    def setUp(self):
        super().setUp()
        discovery.sweep(self.store, self.root, log=lambda *a: None)
        [self.d] = [d for d in discovery.rows(self.store) if d["kind"] == "contradiction"]
        discovery.set_status(self.store, self.d["discovery_id"], "chosen", "human:reviewer")

    def answer(self, folder, payload, n=1):
        (folder / f"batch_{n:02d}_result.json").write_text(json.dumps(payload), encoding="utf-8")

    def score(self, agent="model:x", **change):
        folder = discovery.write_task(self.store, "discovery_score", agent, self.root)
        batch = json.loads((folder / "batch_01.json").read_text(encoding="utf-8"))["discoveries"][0]
        s = {"key": batch["key"], "statement": "Sleep extension improves adolescent mood in experiments but not in "
                                               "surveys", "output": "reanalysis", "value": 4, "feasibility": 5,
             "basis": "data_in_store", "artefact_risk": 2, "why": "It settles a split.", "risks": "Few studies.",
             "next_step": "Code design for every sleep finding.",
             "cites": {"findings": [batch["findings"][0]["finding_id"]], "insights": []}} | change
        self.answer(folder, {"scores": [s]})
        return folder, batch, agents.import_task(folder, self.store)

    def test_the_scoring_task_shows_the_dossier_and_stores_checked_scores(self):
        folder, batch, counts = self.score()
        self.assertEqual(folder.parent, self.root / "output" / "discovery" / "agents")
        self.assertEqual(batch["key"], f"d{self.d['discovery_id']}")
        self.assertEqual((batch["counts"]["up"], batch["counts"]["down"]), (3, 3))
        self.assertTrue(all(f["passage"].startswith("passage of study") for f in batch["findings"]))
        self.assertEqual(counts, {"stored": 1, "problems": []})
        [s] = discovery.scores(self.store)[self.d["discovery_id"]]
        self.assertEqual((s["value"], s["feasibility"], s["artefact_risk"]), (4, 5, 2))
        self.assertEqual(discovery.priority([s]), 16.0)             # 4 x 5 x (6 - 2) / 5
        with self.assertRaises(ValueError):                           # this agent has scored it already
            discovery.write_task(self.store, "discovery_score", "model:x", self.root)

    def test_a_score_that_cites_outside_its_dossier_or_leaves_the_scale_stores_nothing(self):
        _, _, counts = self.score(value=9, cites={"findings": [99999]}, output="paper")
        self.assertEqual(counts["stored"], 0)
        text = " ".join(counts["problems"])
        for part in ("value must be", "cites what is not in its dossier", "output must be"):
            self.assertIn(part, text)
        self.assertEqual(discovery.scores(self.store), {})

    def test_nothing_chosen_nothing_to_score(self):
        discovery.set_status(self.store, self.d["discovery_id"], "set_aside", "human:reviewer")
        with self.assertRaisesRegex(ValueError, "no discovery is chosen"):
            discovery.write_task(self.store, "discovery_score", "model:x", self.root)

    def test_the_novelty_check_runs_on_the_scored_statement_with_recall_controls(self):
        did = self.d["discovery_id"]
        with self.assertRaisesRegex(ValueError, "is scored yet"):
            discovery.write_task(self.store, "novelty_queries", "model:x", self.root)
        self.score()
        ss = discovery.scores(self.store)[did]
        self.assertEqual(discovery.next_step(self.d | {"status": "chosen"}, ss, None),
                         "write its search strings (novelty_queries)")
        folder = discovery.write_task(self.store, "novelty_queries", "model:q", self.root)
        items = json.loads((folder / "batch_01.json").read_text(encoding="utf-8"))["items"]
        self.assertEqual(items[0], {"key": f"d{did}", "statement": ss[0]["statement"]})
        self.assertEqual(len([i for i in items if i["key"].startswith("c")]), 2)     # recall controls
        self.answer(folder, {"queries": [{"key": i["key"], "strict": "sleep AND mood AND adolescen*",
                                          "broad": "sleep AND mood", "plain": "sleep mood adolescents"}
                                         for i in items]})
        self.assertEqual(agents.import_task(folder, self.store)["stored"], len(items))
        check = self.store.db.execute("SELECT * FROM novelty_checks WHERE tournament = 'discovery' AND item = ?",
                                      (f"d{did}",)).fetchone()
        self.assertEqual((check["lens"], check["hypothesis"], check["run_id"]), ("discovery", str(did), self.run))
        nv, controls = discovery.novelty_state(self.store)
        self.assertEqual((nv[did]["verdict"], controls["controls"]), ("not_searched", 2))

        # the searches (code, network) are replaced by two stored hits; one states the hypothesis
        with self.store.db:
            for cid, in self.store.db.execute("SELECT check_id FROM novelty_checks WHERE tournament = 'discovery'"):
                self.store.db.execute("UPDATE novelty_checks SET searched_at = ? WHERE check_id = ?", (now(), cid))
            for title, abstract in (("Sleep extension and teen mood", "Extending sleep improved mood in adolescents."),
                                    ("Diet and grades", "Unrelated.")):
                self.store.db.execute("INSERT INTO novelty_hits (check_id, source, query_kind, rank, title, abstract, "
                                      "year) VALUES (?, 'openalex', 'strict', 1, ?, ?, 2021)",
                                      (check["check_id"], title, abstract))
        self.assertEqual(discovery.novelty_state(self.store)[0][did]["verdict"], "unjudged")
        for judge in ("model:j1", "model:j2"):
            folder = discovery.write_task(self.store, "novelty_judge", judge, self.root)
            data = json.loads((folder / "batch_01.json").read_text(encoding="utf-8"))
            labels = [{"check": c["check"], "hit": h["hit"], "label": "unrelated", "quote": None}
                      for c in data["checks"] for h in c["hits"]]
            for x in labels:
                if x["check"] == f"d{did}" and x["hit"] == min(h["hit"] for c in data["checks"] for h in c["hits"]
                                                             if c["check"] == f"d{did}"):
                    x.update(label="tests_it", quote="Extending sleep improved mood in adolescents")
            self.answer(folder, {"labels": labels})
            self.assertEqual(agents.import_task(folder, self.store)["problems"], [])
        v = discovery.novelty_state(self.store)[0][did]
        self.assertEqual((v["verdict"], v["judges"]), ("prior", ["model:j1", "model:j2"]))
        self.assertIsNone(discovery.next_step(self.d | {"status": "chosen"}, ss, v))
        page = discovery.page(self.store, self.root, "tok")
        self.assertIn("already stated or tested", page)
        self.assertIn("Sleep extension improves adolescent mood", page)
        self.assertIn("discovery_score__model-x", page)
        log = discovery.write_log(self.store, self.root / "notes" / "discoveries.md").read_text(encoding="utf-8")
        self.assertIn("| 16.0 | prior |", log)

    def test_the_searches_run_once_per_run_of_the_checks(self):
        from unittest import mock
        discovery.ensure_tasks(self.store)
        with self.store.db:
            for item, run in (("d1", self.run), ("d2", self.run), ("c5", self.run)):
                self.store.db.execute("INSERT INTO novelty_checks (tournament, run_id, item, statement, created_at) "
                                      "VALUES ('discovery', ?, ?, 's', ?)", (run, item, now()))
        with mock.patch("psyscientist.novelty.search", return_value=collections.Counter(checks=3)) as s, \
                mock.patch("psyscientist.novelty.fill_plain", return_value=collections.Counter()):
            self.assertEqual(discovery.search(self.store, None, log=lambda *a: None), {"checks": 3})
        self.assertEqual(s.call_args.args[1:4:2], (self.run, "discovery"))


class EmptyStore(unittest.TestCase):
    def test_no_projects_no_candidates_and_the_page_still_renders(self):
        store = Store(":memory:")
        self.assertEqual(discovery.sweep(store, log=lambda *a: None), {"found": 0})
        self.assertIn("Nothing found yet", discovery.page(store))


if __name__ == "__main__":
    unittest.main()
