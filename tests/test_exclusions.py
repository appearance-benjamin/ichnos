"""A record the protocol excludes after the eligibility stage ([[exclusions]]) leaves the retained set."""

import shutil
import tempfile
import unittest
from pathlib import Path

from ichnos import agents
from ichnos.store import Store

PROTOCOL = '''
[[exclusions]]
doi = "10.1/excluded"
date = "2026-10-18"
who = "the reviewer"
reason = "not a study"

[[exclusions]]
s2_id = "abc123"
date = "2026-10-19"
who = "the reviewer"
reason = "a garbled repository record without a title"
'''


class ExclusionsTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "protocol").mkdir()
        (self.root / "protocol" / "corpus_t.toml").write_text(PROTOCOL, encoding="utf-8")
        self.saved, agents.ROOT = agents.ROOT, self.root

    def tearDown(self):
        agents.ROOT = self.saved
        shutil.rmtree(self.root, ignore_errors=True)

    def test_excluded_record_leaves_the_retained_set(self):
        store = Store(":memory:")
        run = store.start_run("pilot", "T", None, {})
        a = store.upsert_work({"title": "A kept study of anxiety about artificial intelligence", "doi": "10.1/kept"})
        b = store.upsert_work({"title": "A generated paper that reports no data of its own", "doi": "10.1/EXCLUDED"})
        store.add_sample(run, [a, b], "pilot_screening")
        for w in (a, b):
            store.add_screening(run, w, "T", "title_abstract", "model:x", "include")
        self.assertEqual(agents.kept_ids(store, run, "title_abstract"), [a, b])
        self.assertEqual(agents.kept_ids(store, run), [a])
        self.assertEqual(list(agents.protocol_exclusions(store, run)), [b])

    def test_a_record_without_doi_is_matched_by_its_source_id(self):
        store = Store(":memory:")
        run = store.start_run("pilot", "T", None, {})
        a = store.upsert_work({"title": "A kept study of programmatic assessment", "s2_id": "keep"})
        b = store.upsert_work({"title": "Journal of Teaching Journal of Teaching", "s2_id": "abc123"})
        store.add_sample(run, [a, b], "pilot_screening")
        for w in (a, b):
            store.add_screening(run, w, "T", "title_abstract", "model:x", "include")
        self.assertEqual(agents.kept_ids(store, run), [a])


if __name__ == "__main__":
    unittest.main()
