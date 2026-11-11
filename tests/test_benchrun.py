"""The unattended benchmark runner: a plan, a fake local model, every step once, resumable, summary written."""

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from ichnos import agents, benchrun
from ichnos.store import Store
from tests.test_agents_map import PROTOCOL
from tests.test_benchmark import LABELS

PLAN = """
endpoint = "http://localhost:9/v1"
tag = "fake"
batch_size = 2

[[screeners]]
label = "fake-a"
model = "fake:a"

[[screeners]]
label = "fake-b"
model = "fake:b"

[[benchmarks]]
name = "toy"
corpus = "Z"
labels = "labels.csv"

[[benchmarks]]
name = "waiting"
corpus = "Q"
labels = "labels.csv"
"""

SETTINGS = SimpleNamespace(openalex_api_key=None, contact_email=None, s2_api_key=None, core_api_key=None,
                           llm_api_key=None)


def fake_run_task(keep_all: bool, calls: list):
    """Answers every open batch: score 2 everywhere (keep) or 0 on the first metric (exclude), quoting the title."""
    def run(folder, store, settings, parallel=1):
        calls.append((folder.name, settings.model))
        protocol = json.loads((folder / "task.json").read_text(encoding="utf-8"))
        metrics = [m["id"] for m in agents.stage_rules(
            __import__("tomllib").loads(PROTOCOL), "screen")["metrics"]]
        answered = 0
        for batch in agents.batch_files(folder):
            out = batch.with_name(batch.stem + "_result.json")
            if out.exists():
                continue
            recs = []
            for r in json.loads(batch.read_text(encoding="utf-8")):
                score = 2 if keep_all or "Sleep" in r["title"] or "Insomnia" in r["title"] else 0
                recs.append({"work_id": r["work_id"], "kind": "study",
                             "scores": {m: {"score": score, "evidence": r["title"][:10]} for m in metrics}})
            out.write_text(json.dumps({"records": recs, "insights": []}), encoding="utf-8")
            answered += 1
        return {"answered": answered, "failed": 0, "already": 0, "failures": []}
    return run


class Runner(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "protocol").mkdir()
        (self.root / "protocol" / "corpus_z.toml").write_text(PROTOCOL + "\n[benchmark]\nrecall_min = 0.9\n",
                                                              encoding="utf-8")
        (self.root / "labels.csv").write_text(LABELS, encoding="utf-8")
        (self.root / "plan.toml").write_text(PLAN, encoding="utf-8")
        self.store = Store(self.root / "evidence.sqlite")
        self.plan = benchrun.load_plan(self.root / "plan.toml")

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.root, ignore_errors=True)

    def run_plan(self, **kw):
        return benchrun.run_plan(self.plan, self.store, SETTINGS, self.root, echo=lambda *_: None, **kw)

    def test_plan_end_to_end_and_resume(self):
        calls = []
        dry = self.run_plan(dry_run=True)
        self.assertEqual(dry[0]["state"], "would import")
        self.assertEqual(dry[1]["state"], "skipped")             # no protocol: a person writes it first
        rows = self.run_plan(deps={"run_task": fake_run_task(False, calls), "fill": lambda run, works: None})
        toy = rows[0]
        self.assertEqual(toy["bench"], "toy--fake")
        self.assertEqual(toy["state"], "scored", toy)
        self.assertEqual(toy["note"], "meets every threshold")
        self.assertEqual({m for _, m in calls}, {"fake:a", "fake:b"})
        summary = (self.root / "output" / "benchmarks" / "summary.md").read_text(encoding="utf-8")
        self.assertIn("| toy--fake | fake-a, fake-b | 3 | 100.0%", summary)
        self.assertTrue((self.root / "output" / "benchmarks" / "runner.log").exists())
        # a second pass changes nothing and asks no model
        calls.clear()
        again = self.run_plan(deps={"run_task": fake_run_task(False, calls), "fill": lambda run, works: None})
        self.assertEqual(again[0]["state"], "scored")
        self.assertEqual(calls, [])

    def test_unfinished_screener_is_not_scored(self):
        def half(folder, store, settings, parallel=1):
            return {"answered": 0, "failed": 3, "already": 0, "failures": []}
        rows = self.run_plan(deps={"run_task": half, "fill": lambda run, works: None})
        self.assertEqual(rows[0]["state"], "screening")
        self.assertIn("fake-a: written", rows[0]["note"])

    def test_claude_label_refused(self):
        bad = self.root / "bad.toml"
        bad.write_text(PLAN.replace('label = "fake-a"', 'label = "claude-opus-5-5"'), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Claude name"):
            benchrun.load_plan(bad)


if __name__ == "__main__":
    unittest.main()
