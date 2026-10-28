"""Projects: the registry, the main run of each corpus, the start page, and the search step on Home."""

from psyscientist import projects
from psyscientist.review import overview
from tests.test_agents_map import Base, PROTOCOL
import tomllib

REGISTRY = '''
[[project]]
corpus = "Z"
name = "Sleep and mood"
about = "A toy review."
owner = "Sam"
'''


class Projects(Base):
    def test_without_a_registry_a_project_is_named_by_its_protocol(self):
        [p] = projects.brief(self.store, self.root)
        self.assertEqual((p["corpus"], p["name"], p["owner"], p["run"]), ("Z", "Toy corpus: sleep and adolescent mood", "", self.run))

    def test_the_registry_names_the_project_and_its_main_run(self):
        (self.root / "protocol" / "projects.toml").write_text(REGISTRY + f"run = {self.run}\n", encoding="utf-8")
        later = self.store.start_run("pilot", "Z", None, {})
        self.store.add_sample(later, [1], "pilot_screening")
        [p] = projects.brief(self.store, self.root)
        self.assertEqual((p["name"], p["owner"], p["run"]), ("Sleep and mood", "Sam", self.run))
        self.assertEqual([(r["id"], r["main"]) for r in p["runs"]], [(later, False), (self.run, True)])
        self.assertEqual(projects.info(self.store, "Z", self.root)["about"], "A toy review.")

    def test_without_a_main_run_the_run_with_most_work_opens(self):
        later = self.store.start_run("pilot", "Z", None, {})
        self.store.add_sample(later, [1], "pilot_screening")
        for w in (1, 2):
            self.store.add_screening(self.run, w, "Z", "title_abstract", "model:s", "include")
        self.assertEqual(projects.brief(self.store, self.root)[0]["run"], self.run)

    def test_the_start_page_shows_cards_and_what_needs_a_person(self):
        (self.root / "protocol" / "projects.toml").write_text(REGISTRY, encoding="utf-8")
        html = projects.page(self.store, self.root)
        self.assertIn("Sleep and mood", html)
        self.assertIn(f'href="/run/{self.run}"', html)
        self.assertIn("4 records on the sheet are not screened yet.", html)   # nobody screened the toy sheet
        self.assertIn("Everything that needs you", html)

    def test_a_comparator_strand_shows_on_its_project_before_its_first_run(self):
        (self.root / "protocol" / "projects.toml").write_text(
            REGISTRY + '\n[[project]]\ncorpus = "Y"\nname = "Assessment stakes"\nstrand_of = "Z"\n', encoding="utf-8")
        [p] = projects.brief(self.store, self.root)
        self.assertEqual(p["strands"], [{"corpus": "Y", "name": "Assessment stakes", "run": None}])
        self.assertIn("Comparator strand", projects.page(self.store, self.root))
        self.assertIn("not searched yet", projects.page(self.store, self.root))
        sha = self.store.add_protocol("Y", "protocol/corpus_y.toml", 'corpus = "Y"\ntitle = "Stakes"\n')
        run = self.store.start_run("pilot", "Y", sha, {})
        self.store.add_sample(run, [1], "pilot_screening")
        by = {p["corpus"]: p for p in projects.brief(self.store, self.root)}
        self.assertEqual(by["Z"]["strands"][0]["run"], run)
        self.assertEqual(by["Y"]["strand_of"], "Z")

    def test_a_registered_project_without_a_run_shows_as_drafted(self):
        (self.root / "protocol" / "projects.toml").write_text(
            REGISTRY + '\n[[project]]\ncorpus = "X"\nname = "Fertility decline"\nowner = "Sam"\n', encoding="utf-8")
        self.assertEqual([p["corpus"] for p in projects.drafted(self.store, self.root)], ["X"])
        html = projects.page(self.store, self.root)
        self.assertIn("Fertility decline", html)
        self.assertIn("Approve the drafted protocol (<code>protocol/corpus_x.toml</code>)", html)
        sha = self.store.add_protocol("X", "protocol/corpus_x.toml", 'corpus = "X"\ntitle = "Fertility"\n')
        run = self.store.start_run("pilot", "X", sha, {})
        self.store.add_sample(run, [1], "pilot_screening")
        self.assertEqual(projects.drafted(self.store, self.root), [])

    def test_archived_projects_are_listed_apart(self):
        (self.root / "protocol" / "projects.toml").write_text(REGISTRY + "archived = true\n", encoding="utf-8")
        html = projects.page(self.store, self.root)
        self.assertIn(">Archived</h2>", html)


class FinishedPaper(Base):
    def test_a_final_polish_after_a_minor_revision_is_the_finished_paper(self):
        from psyscientist import paper
        from psyscientist.store import now
        paper.ensure(self.store)
        ids = []
        with self.store.db:
            for v in (1, 2):
                ids.append(self.store.db.execute(
                    "INSERT INTO papers (corpus, run_id, version, parent_id, author, title, content, method, "
                    "verification, created_at) VALUES ('Z', ?, ?, ?, 'model:x', 'T', '{}', '{}', '{}', ?)",
                    (self.run, v, ids[-1] if ids else None, now())).lastrowid)
            self.store.db.execute("INSERT INTO reviews (paper_id, kind, reviewer, round, content, overall, decision, "
                                  "created_at) VALUES (?, 'meta', 'model:e', 2, '{}', 7, 'minor_revision', ?)",
                                  (ids[0], now()))
        o = overview(self.store, self.run, tomllib.loads(PROTOCOL))
        self.assertEqual((o["decision"]["decision"], o["decision"]["polished"]), ("minor_revision", True))
        texts = [n["text"] for n in o["next"]]
        self.assertTrue(any(t.startswith("The paper is finished: version 2") for t in texts))
        self.assertFalse(any(t.startswith("Editor's decision on version") for t in texts))


class SearchStep(Base):
    def with_sources(self, sources):
        p = tomllib.loads(PROTOCOL)
        p["sources"] = sources
        return p

    def test_a_failed_or_unsearched_source_becomes_a_step_with_a_button(self):
        self.store.set_run_source(self.run, "eric", "q", None, 0, "failed", "HTTP 500")
        steps = overview(self.store, self.run, self.with_sources(["openalex", "eric", "pubmed"]))["next"]
        [s] = [n for n in steps if n.get("job") == "sources"]
        self.assertIn("The ERIC search failed", s["text"])
        self.assertIn("PubMed is in the protocol but not searched yet", s["text"])

    def test_a_source_another_run_of_the_corpus_searched_is_not_missing(self):
        other = self.store.start_run("pilot", "Z", None, {})
        self.store.set_run_source(other, "pubmed", "q", 3, 3, "ok")
        steps = overview(self.store, self.run, self.with_sources(["openalex", "pubmed"]))["next"]
        self.assertFalse([n for n in steps if n.get("job") == "sources"])
