"""The lens tournament end to end with scripted answers: tasks, checks, controls, judge, evaluation."""

import json

from ichnos import agents, lenses
from tests.test_findings_insights import FindingsAndInsights

LENS_FILE = '''
version = "test"

[interpret]
tournament = "t1"
budget = 3
baseline_budget = 4
settings = ["teens"]
outcome = "mood"

[[interpret.variables]]
id = "sleep"
contrast = "long vs short sleep"

[[interpret.variables]]
id = "screens"
contrast = "much vs little screen time"

[interpret.positive_control]
lenses = ["circadian", "social"]
variable = "sleep"

[[lenses]]
id = "circadian"
name = "Circadian"
role = "theory"
heuristic = "What did this do to the body clock?"
core = ["C1: Mood follows sleep timing."]

[[lenses]]
id = "social"
name = "Social"
role = "theory"
heuristic = "What did this do to friendships?"
core = ["S1: Mood follows social contact."]

[[lenses]]
id = "placebo"
name = "Lens P"
role = "placebo"
heuristic = "What mattered?"
core = ["P1: People differ."]
'''


class Tournament(FindingsAndInsights):
    def setUp(self):
        super().setUp()
        (self.root / "protocol" / "lenses_corpus_z.toml").write_text(LENS_FILE, encoding="utf-8")

    def answer(self, folder, obj):
        (folder / "batch_01_result.json").write_text(json.dumps(obj), encoding="utf-8")
        return agents.import_task(folder, self.store)

    def interpret(self, lens, rep, label, signs):
        folder = agents.write_tasks(self.store, self.run, "interpret", "model:a", root=self.root,
                                    options={"lens": lens, "replicate": rep})
        data = json.loads((folder / "batch_01.json").read_text(encoding="utf-8"))
        self.assertNotIn("placebo", json.dumps(data["lens"]).lower())
        ids = [f["id"] for f in data["findings"]]
        obj = {"reading": "Through this lens mood tracks the night.",
               "fit": [{"finding": i, "label": label} for i in ids],
               "auxiliaries": [{"id": "A1", "statement": "teens sleep late", "consequence": "later bedtimes in teens"}],
               "hypotheses": [{"id": "H1", "statement": f"{lens} predicts X", "forbids": "no X", "derived_from": ids[:1]}],
               "grid": [{"cell": c["cell"], "sign": s} for c, s in zip(data["grid"], signs)],
               "tensions": [{"insight": t["insight"], "stance": "silent"} for t in data["tensions"]]}
        return folder, obj

    def test_tournament(self):
        self.findings_task()
        bad_folder, bad = self.interpret("circadian", 1, "predicts", ["+", "+"])
        bad["fit"] = bad["fit"][:-1]
        bad["hypotheses"][0]["forbids"] = ""
        problems = self.answer(bad_folder, bad)["problems"]
        self.assertTrue(any("every finding" in p for p in problems))
        self.assertTrue(any("forbids" in p for p in problems))

        for lens, label, signs in (("circadian", "predicts", ["+", "0"]), ("social", "strains", ["0", "-"]),
                                   ("placebo", "silent", ["?", "?"])):
            for rep in (1, 2):
                folder, obj = self.interpret(lens, rep, label, signs)
                self.assertEqual(self.answer(folder, obj)["problems"], [])

        ctrl = agents.write_tasks(self.store, self.run, "lens_control", "model:a", root=self.root,
                                  options={"mode": "all_cards"})
        data = json.loads((ctrl / "batch_01.json").read_text(encoding="utf-8"))
        names = json.loads((ctrl / "task.json").read_text(encoding="utf-8"))["names"]
        cells = [c["cell"] for c in data["grid"]]
        part = lambda s: {"grid": [{"cell": c, "sign": s} for c in cells],  # noqa: E731
                          "hypotheses": [{"id": "H1", "statement": "one agent's X", "forbids": "no X"}]}
        obj = {"lenses": {n: part("+" if lens == "circadian" else "0") for n, lens in names.items()}, "divergences": []}
        self.assertEqual(self.answer(ctrl, obj)["problems"], [])
        base = agents.write_tasks(self.store, self.run, "lens_control", "model:a", root=self.root,
                                  options={"mode": "no_card"})
        self.assertEqual(self.answer(base, {"hypotheses": [{"id": "H1", "statement": "circadian predicts X",
                                                            "forbids": "no X"}]})["problems"], [])

        judge = agents.write_tasks(self.store, self.run, "lens_judge", "model:j", root=self.root)
        jdata = json.loads((judge / "batch_01.json").read_text(encoding="utf-8"))
        self.assertNotIn("circadian", json.dumps(jdata["hypotheses"][0]["key"]))
        keys = json.loads((judge / "task.json").read_text(encoding="utf-8"))["keys"]
        labels = [{"key": k, "testable": True, "restates": None,
                   "baseline": "b1" if v["lens"] == "circadian" else None} for k, v in keys.items()]
        self.assertEqual(self.answer(judge, {"labels": labels})["problems"], [])

        result = lenses.evaluate(self.store, self.run, self.root)
        self.assertTrue(result["positive_control"]["passed"])
        self.assertEqual({(d["cell"], tuple(d["lenses"])) for d in result["divergences"]},
                         {("G1", ("circadian", "social")), ("G2", ("circadian", "social"))})
        self.assertEqual(result["control"]["shared_with_tournament"], 2)
        self.assertEqual(result["hypotheses"]["tournament:circadian"]["credited"], 0)    # in the baseline
        self.assertEqual(result["hypotheses"]["tournament:social"]["credited"], 2)
        self.assertEqual(result["hypotheses"]["tournament:social"]["credited_core"], 0)  # gate 2 not judged yet
        self.assertEqual(result["fit_test"]["pairings"], 3)
        self.assertIn("## Divergences on the grid", lenses.report(result, self.store, self.run))

        # gate 2: does the hypothesis follow from its core or from common sense? (programme names masked)
        derive = agents.write_tasks(self.store, self.run, "lens_derive", "model:j", root=self.root)
        ddata = json.loads((derive / "batch_01.json").read_text(encoding="utf-8"))
        self.assertEqual(set(ddata["programmes"][0]), {"programme", "core", "heuristic"})
        keys = json.loads((derive / "task.json").read_text(encoding="utf-8"))["keys"]
        self.assertTrue(self.answer(derive, {"labels": [{"key": k, "label": "needs_auxiliary", "auxiliary": ""}
                                                         for k in keys]})["problems"])
        derive = agents.write_tasks(self.store, self.run, "lens_derive", "model:j", root=self.root)
        keys = json.loads((derive / "task.json").read_text(encoding="utf-8"))["keys"]
        labels = [{"key": k, "label": "follows_from_core" if v["lens"] == "social" else "common_sense"}
                  for k, v in keys.items()]
        self.assertEqual(self.answer(derive, {"labels": labels})["problems"], [])
        result = lenses.evaluate(self.store, self.run, self.root)
        self.assertEqual(result["hypotheses"]["tournament:social"]["credited_core"], 2)
        self.assertEqual(result["hypotheses"]["tournament:placebo"]["credited_core"], 0)
        # a second judge: directly vs with an auxiliary still credits (as with an auxiliary); a hypothesis only one
        # judge has labelled is pending, not credited on one judgement
        derive2 = agents.write_tasks(self.store, self.run, "lens_derive", "model:j2", root=self.root)
        keys = json.loads((derive2 / "task.json").read_text(encoding="utf-8"))["keys"]
        labels = [{"key": k, "label": "needs_auxiliary" if v["lens"] == "social" else "common_sense",
                   "auxiliary": "teens text at night"} for k, v in keys.items()]
        self.assertEqual(self.answer(derive2, {"labels": labels})["problems"], [])
        result = lenses.evaluate(self.store, self.run, self.root)
        social = result["hypotheses"]["tournament:social"]
        self.assertEqual((social["credited_core"], social["derivation_passed"]), (2, {"needs_auxiliary": 2}))
        last = self.store.db.execute("SELECT max(derivation_id) FROM lens_derivations WHERE judge = 'model:j2' AND "
                                     "lens = 'social'").fetchone()[0]
        row = dict(self.store.db.execute("SELECT * FROM lens_derivations WHERE derivation_id = ?", (last,)).fetchone())
        self.store.db.execute("DELETE FROM lens_derivations WHERE derivation_id = ?", (last,))
        social = lenses.evaluate(self.store, self.run, self.root)["hypotheses"]["tournament:social"]
        self.assertEqual((social["credited_core"], social["derivation_passed"]), (1, {"needs_auxiliary": 1, "unjudged": 1}))
        self.store.db.execute(f"INSERT INTO lens_derivations ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                              tuple(row.values()))

        # the cartographer: tests that split the lenses, never a verdict
        carto = agents.write_tasks(self.store, self.run, "cartograph", "model:c", root=self.root)
        cdata = json.loads((carto / "batch_01.json").read_text(encoding="utf-8"))
        self.assertEqual([x["id"] for x in cdata["lenses"]], ["circadian", "social"])
        card = {"id": "T1", "question": "Sleep or friends?",
                "positions": [{"lens": "circadian", "prediction": "a", "from": "C1"},
                              {"lens": "social", "prediction": "b", "from": "S1"}],
                "test": {"design": "panel"}, "patterns": [{"result": "x", "favours": ["circadian"]},
                                                          {"result": "y", "favours": ["social"]}],
                "existing_evidence": [{"finding": cdata["findings"][0]["id"], "why_not_decisive": "one study"}]}
        self.assertTrue(self.answer(carto, {"cards": [dict(card, winner="circadian")]})["problems"])
        problems = self.answer(agents.write_tasks(self.store, self.run, "cartograph", "model:c", root=self.root),
                               {"cards": [dict(card, artifact_threats="selection")]})["problems"]
        self.assertTrue(any("must be a list" in p for p in problems))
        one_sided = dict(card, patterns=[{"result": "x", "favours": ["circadian"]}])
        self.assertTrue(self.answer(agents.write_tasks(self.store, self.run, "cartograph", "model:c", root=self.root),
                                    {"cards": [one_sided]})["problems"])
        carto = agents.write_tasks(self.store, self.run, "cartograph", "model:c", root=self.root)
        self.assertEqual(self.answer(carto, {"cards": [card]})["problems"], [])

        # gate 3: the literature, with a stubbed search
        from ichnos import novelty
        from ichnos.config import Settings
        from ichnos.http import Http
        q = agents.write_tasks(self.store, self.run, "novelty_queries", "model:q", root=self.root)
        items = json.loads((q / "batch_01.json").read_text(encoding="utf-8"))["items"]
        self.assertEqual(len(items), 2)
        self.assertTrue(self.answer(q, {"queries": [{"key": i["key"], "strict": "sleep, mood", "broad": "mood",
                                                     "plain": "x"} for i in items]})["problems"])
        q = agents.write_tasks(self.store, self.run, "novelty_queries", "model:q", root=self.root)
        items = json.loads((q / "batch_01.json").read_text(encoding="utf-8"))["items"]
        self.assertEqual(self.answer(q, {"queries": [{"key": i["key"], "strict": '"social contact" AND mood',
                                                      "broad": "mood", "plain": "social contact mood"}
                                                     for i in items]})["problems"], [])
        hit = [{"doi": "10.1234/prior", "title": "Friends and mood", "year": 2020, "openalex_id": "W9",
                "abstract": "We tested whether social contact predicts mood in teens."}]
        saved = novelty._openalex, novelty._s2
        novelty._openalex, novelty._s2 = (lambda *a: hit), (lambda *a: [])
        try:
            counts = novelty.search(self.store, self.run, Settings(None, None, None, self.root / "evidence.sqlite"),
                                    "t1", log=lambda *a: None, http=Http(self.store, self.run, sleep=lambda s: None))
        finally:
            novelty._openalex, novelty._s2 = saved
        self.assertEqual((counts["checks"], counts["hits"]), (2, 2))
        # the plain search came back empty (as when Semantic Scholar refuses it): OpenAlex fills it, once
        saved_plain = novelty._openalex_plain
        novelty._openalex_plain = lambda *a: [{"doi": "10.1234/other", "title": "Teens and friends", "year": 2019,
                                               "openalex_id": "W10", "abstract": "Friends matter."}] + hit
        quiet = Http(self.store, self.run, sleep=lambda s: None)
        try:
            settings = Settings(None, None, None, self.root / "evidence.sqlite")
            filled = novelty.fill_plain(self.store, self.run, settings, "t1", log=lambda *a: None, http=quiet)
            again = novelty.fill_plain(self.store, self.run, settings, "t1", log=lambda *a: None, http=quiet)
        finally:
            novelty._openalex_plain = saved_plain
        self.assertEqual((filled["checks"], filled["hits"]), (2, 2))        # the duplicate of `hit` is skipped
        self.assertEqual(again.get("checks", 0), 0)
        nj = agents.write_tasks(self.store, self.run, "novelty_judge", "model:n", root=self.root)
        checks = json.loads((nj / "batch_01.json").read_text(encoding="utf-8"))["checks"]
        labels = [{"check": c["check"], "hit": h["hit"], "label": "tests_it",
                   "quote": "We tested whether social contact predicts mood"} for c in checks for h in c["hits"]]
        self.assertEqual(self.answer(nj, {"labels": labels})["problems"], [])
        result = lenses.evaluate(self.store, self.run, self.root)
        self.assertEqual(result["hypotheses"]["tournament:social"]["prior"], 2)
        self.assertEqual(result["novelty"]["verdicts"], {"prior": 2})
        text = lenses.report(result, self.store, self.run)
        self.assertIn("### The literature check", text)
        self.assertIn("## Test cards: circadian vs social", text)

        # the app's Interpretations page: the gates as a funnel, every hypothesis's fate, the hits behind a verdict
        from ichnos.review import build_data, render_page
        page = build_data(self.store, self.run, root=self.root)
        data = page["interpret"]
        gates = {g["lens"]: g for g in data["gates"] if g["source"] == "tournament"}
        self.assertEqual(list(gates), ["circadian", "social", "placebo"])          # the lens file's order
        for g in data["gates"]:                                                     # gate 2 adds up to gate 1
            self.assertEqual(g["gate1"], g["common_sense"] + g["from_core"] + g["with_auxiliary"] + g["contradicts"]
                             + g["disputed"] + g["unjudged2"])
        self.assertEqual((gates["circadian"]["gate1"], gates["circadian"]["in_baseline"]), (0, 2))
        self.assertEqual((gates["social"]["credited"], gates["social"]["prior"]), (2, 2))
        self.assertEqual((gates["placebo"]["common_sense"], gates["placebo"]["credited"]), (2, 0))
        fates = {(h["lens"], h["gate1"], h["derivation"], h["credited"]) for h in data["hypotheses"]}
        self.assertEqual(fates, {("circadian", "in_baseline", "common_sense", False),
                                 ("social", "passed", "needs_auxiliary", True),
                                 ("placebo", "passed", "common_sense", False)})
        social = next(h for h in data["hypotheses"] if h["lens"] == "social")
        self.assertEqual(social["novelty"], "prior")
        prior = social["literature"]["prior"][0]
        self.assertEqual((prior["link"], prior["quote"]), ("https://doi.org/10.1234/prior",
                                                           "We tested whether social contact predicts mood"))
        self.assertIsNone(next(h for h in data["hypotheses"] if h["lens"] == "placebo")["literature"])
        self.assertEqual(len(data["cards"][0]["cards"]), 1)
        fid = str(card["existing_evidence"][0]["finding"])
        self.assertIn("cite", data["findings"][fid])
        stage = page["overview"]["stages"][-1]
        self.assertEqual((stage["stage"], stage["value"], stage["of"]), ("Interpret", 2, 6))
        self.assertEqual(stage["detail"], "prior 2; 1 test cards")
        html = render_page(page)
        self.assertIn('data-tab="interpret"', html)
        self.assertIn("function renderInterpret()", html)

        # the literature, step by step: queried but not searched, searched again after the judges ran
        with self.assertRaises(ValueError):          # every credited hypothesis has its search strings already
            agents.write_tasks(self.store, self.run, "novelty_queries", "model:q", root=self.root)
        social_out = next(h["output_id"] for h in data["hypotheses"] if h["lens"] == "social")
        cur = self.store.db.execute(
            "INSERT INTO novelty_checks (tournament, run_id, item, output_id, lens, hypothesis, statement, created_at) "
            "VALUES ('t1', ?, 'q99', ?, 'social', 'H9', 'friends lift mood', '2026-10-18')", (self.run, social_out))
        key = (social_out, "social", "H9")
        self.assertEqual(novelty.verdicts(self.store, "t1")[key]["verdict"], "not_searched")
        self.store.db.execute("UPDATE novelty_checks SET searched_at = '2026-10-18' WHERE check_id = ?", (cur.lastrowid,))
        self.store.db.execute("INSERT INTO novelty_hits (check_id, source, query_kind, title, abstract) VALUES "
                              "(?, 'openalex', 'plain', 'Peers and mood', 'Peer contact and mood in adolescents.')",
                              (cur.lastrowid,))
        self.assertEqual(novelty.verdicts(self.store, "t1")[key]["verdict"], "unjudged")
        nj = agents.write_tasks(self.store, self.run, "novelty_judge", "model:n", root=self.root)
        checks = json.loads((nj / "batch_01.json").read_text(encoding="utf-8"))["checks"]
        self.assertEqual([(c["check"], len(c["hits"])) for c in checks], [("q99", 1)])   # only what is unlabelled
        self.assertEqual(self.answer(nj, {"labels": [{"check": "q99", "hit": checks[0]["hits"][0]["hit"],
                                                      "label": "tests_neighbour"}]})["problems"], [])
        self.assertEqual(novelty.verdicts(self.store, "t1")[key]["verdict"], "near")
        with self.assertRaises(ValueError):
            agents.write_tasks(self.store, self.run, "novelty_judge", "model:n", root=self.root)

    def test_plain_rewrite(self):
        """novelty_plain rewrites only the plain query of checks whose plain search found nothing."""
        from ichnos import novelty
        from ichnos.config import Settings
        from ichnos.http import Http
        novelty.ensure(self.store)
        long = "teens social contact mood friendship quality loneliness school peers"

        def check(n, searched, hit_kind):
            cur = self.store.db.execute(
                "INSERT INTO novelty_checks (tournament, run_id, item, output_id, lens, hypothesis, statement, strict, "
                "broad, plain, searched_at, created_at) VALUES ('t1', ?, ?, ?, 'social', ?, ?, '\"social contact\" AND "
                "mood', 'mood', ?, ?, '2026-10-18')",
                (self.run, f"q{n}", n, f"H{n}", f"statement {n}", long, "2026-10-18" if searched else None))
            if hit_kind:
                self.store.db.execute("INSERT INTO novelty_hits (check_id, source, query_kind, title, abstract) "
                                      "VALUES (?, 'openalex', ?, ?, 'An abstract.')", (cur.lastrowid, hit_kind, f"Hit {n}"))
            self.store.db.commit()
            return cur.lastrowid

        q1, q2 = check(1, True, "strict"), check(2, True, "plain")
        check(3, False, None)                                        # not searched yet: left to `search`
        folder = agents.write_tasks(self.store, self.run, "novelty_plain", "model:p", root=self.root)
        items = json.loads((folder / "batch_01.json").read_text(encoding="utf-8"))["items"]
        self.assertEqual(items, [{"key": "q1", "statement": "statement 1"}])    # blind to the old queries
        self.assertIn("3-5 key terms", (folder / "instructions.md").read_text(encoding="utf-8"))
        self.assertIn("3-5 key terms", novelty.queries_instructions())
        problems = self.answer(folder, {"queries": [{"key": "q1", "plain": long}]})["problems"]
        self.assertTrue(any("at most 8 words" in p for p in problems))
        problems = self.answer(folder, {"queries": [{"key": "q1", "plain": '"social contact" AND mood'}]})["problems"]
        self.assertTrue(any("plain words only" in p for p in problems))
        self.assertTrue(self.answer(folder, {"queries": []})["problems"])
        self.assertEqual(self.answer(folder, {"queries": [{"key": "q1", "plain": " social contact  mood teens "}]})
                         ["problems"], [])
        # the check keeps its row, key, hit and boolean strings; the change is logged with the agent's call
        row = self.store.db.execute("SELECT * FROM novelty_checks WHERE check_id = ?", (q1,)).fetchone()
        self.assertEqual((row["item"], row["plain"], row["strict"], row["broad"]),
                         ("q1", "social contact mood teens", '"social contact" AND mood', "mood"))
        rewrites = self.store.db.execute("SELECT * FROM novelty_rewrites").fetchall()
        self.assertEqual([(r["check_id"], r["field"], r["old"], r["new"], r["agent"]) for r in rewrites],
                         [(q1, "plain", long, "social contact mood teens", "model:p")])
        call = self.store.db.execute("SELECT kind, source FROM calls WHERE call_id = ?", (rewrites[0]["call_id"],))
        self.assertEqual(tuple(call.fetchone()), ("llm", "model:p"))
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM novelty_hits WHERE check_id = ?", (q1,)).fetchone()[0], 1)
        self.assertEqual(self.store.db.execute("SELECT plain FROM novelty_checks WHERE check_id = ?", (q2,)).fetchone()[0], long)
        # the new query is what OpenAlex gets; its hit closes the gap
        asked = []
        saved = novelty._openalex_plain
        novelty._openalex_plain = lambda http, text, *a: asked.append(text) or [
            {"doi": "10.1234/new", "title": "Friends lift teen mood", "year": 2021, "openalex_id": "W11",
             "abstract": "Social contact and mood."}]
        try:
            filled = novelty.fill_plain(self.store, self.run, Settings(None, None, None, self.root / "evidence.sqlite"),
                                        "t1", log=lambda *a: None, http=Http(self.store, self.run, sleep=lambda s: None))
        finally:
            novelty._openalex_plain = saved
        self.assertEqual(asked, ["social contact mood teens"])
        self.assertEqual((filled["checks"], filled["hits"], filled["empty"]), (1, 1, 0))
        s = novelty.summary(self.store, "t1")
        self.assertEqual((s["plain_rewritten"], s["without_plain"]), (1, 0))
        with self.assertRaises(ValueError):                                     # no plain gap left
            agents.write_tasks(self.store, self.run, "novelty_plain", "model:p", root=self.root)
        # a check that got plain hits after the task was written keeps the query that found them
        q4 = check(4, True, None)
        folder = agents.write_tasks(self.store, self.run, "novelty_plain", "model:p", root=self.root)
        self.store.db.execute("INSERT INTO novelty_hits (check_id, source, query_kind, title) VALUES (?, 'openalex', "
                              "'plain', 'Late hit')", (q4,))
        self.store.db.commit()
        problems = self.answer(folder, {"queries": [{"key": "q4", "plain": "peers mood"}]})["problems"]
        self.assertEqual(problems, ["q4: has plain-search hits now; write the task again"])
        self.assertEqual(self.store.db.execute("SELECT plain FROM novelty_checks WHERE check_id = ?", (q4,)).fetchone()[0], long)

    def test_interpret_page_without_lenses(self):
        from ichnos.review import build_data
        self.assertIsNone(build_data(self.store, self.run, root=self.root / "elsewhere")["interpret"])
        self.findings_task()
        data = build_data(self.store, self.run, root=self.root)["interpret"]
        self.assertTrue(data["empty"])
        self.assertEqual(data["file"], "protocol/lenses_corpus_z.toml")
        self.assertEqual([lens["role"] for lens in data["lenses"]], ["theory", "theory", "placebo"])
        # a typo in the lens file is shown, not mistaken for a missing file
        broken = self.root / "broken"
        (broken / "protocol").mkdir(parents=True)
        (broken / "protocol" / "lenses_corpus_z.toml").write_text(LENS_FILE + "\n[[lenses]\n", encoding="utf-8")
        page = build_data(self.store, self.run, root=broken)
        self.assertIn("TOMLDecodeError", page["interpret"]["error"])
        # whatever the agents or the APIs wrote, the embedded data cannot close the script or open a comment
        from ichnos.review import render_page
        page["title"] = "</script><!--<script>alert(1)</script>"
        html = render_page(page)
        self.assertNotIn("<!--<script>", html)
        self.assertEqual(html.count("</script>"), render_page(dict(page, title="")).count("</script>"))
