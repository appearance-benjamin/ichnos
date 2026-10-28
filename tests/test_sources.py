import unittest

from psyscientist import query
from psyscientist.sources import europepmc, pubmed
from psyscientist.store import Store

PUBMED_XML = b"""<?xml version="1.0" ?>
<PubmedArticleSet>
 <PubmedArticle>
  <MedlineCitation>
   <PMID Version="1">12345</PMID>
   <Article>
    <Journal><JournalIssue><PubDate><MedlineDate>2019 Jan-Feb</MedlineDate></PubDate></JournalIssue>
     <Title>Journal of Vocational Behavior</Title></Journal>
    <ArticleTitle>Purpose in life after <i>job loss</i>.</ArticleTitle>
    <ELocationID EIdType="doi">10.1/ABC</ELocationID>
    <Abstract>
     <AbstractText Label="BACKGROUND">Work ends.</AbstractText>
     <AbstractText Label="RESULTS">Purpose fell.</AbstractText>
    </Abstract>
    <AuthorList>
     <Author><LastName>Doe</LastName><ForeName>Jane</ForeName></Author>
     <Author><CollectiveName>Panel Group</CollectiveName></Author>
    </AuthorList>
    <Language>eng</Language>
    <PublicationTypeList><PublicationType>Journal Article</PublicationType></PublicationTypeList>
   </Article>
  </MedlineCitation>
  <PubmedData><ArticleIdList>
   <ArticleId IdType="pubmed">12345</ArticleId><ArticleId IdType="pmc">PMC999</ArticleId>
  </ArticleIdList></PubmedData>
 </PubmedArticle>
 <PubmedBookArticle>
  <BookDocument><PMID>777</PMID><ArticleTitle>A chapter</ArticleTitle>
   <Book><PubDate><Year>2020</Year></PubDate><BookTitle>A book</BookTitle></Book></BookDocument>
 </PubmedBookArticle>
</PubmedArticleSet>"""


class Rendering(unittest.TestCase):
    def test_pubmed_tags_every_term(self):
        tree = query.parse('("job loss" OR retire*) AND panel')
        self.assertEqual(query.render(tree, "pubmed"), '("job loss"[tiab] OR retire*[tiab]) AND panel[tiab]')

    def test_pubmed_not_is_binary(self):
        tree = query.parse("retire* AND NOT review")
        self.assertEqual(query.render(tree, "pubmed"), "retire*[tiab] NOT review[tiab]")
        with self.assertRaises(query.QueryError):
            query.render(query.parse("NOT review"), "pubmed")

    def test_europepmc_field(self):
        tree = query.parse('"meaning in life" AND (unemploy* OR NEET)')
        self.assertEqual(query.render(tree, "europepmc"),
                         'TITLE_ABS:"meaning in life" AND (TITLE_ABS:unemploy* OR TITLE_ABS:neet)')


class PubmedParsing(unittest.TestCase):
    def test_article_and_book(self):
        art, book = pubmed.parse_articles(PUBMED_XML)
        self.assertEqual((art["pmid"], art["doi"], art["year"]), ("12345", "10.1/abc", 2019))
        self.assertEqual(art["title"], "Purpose in life after job loss.")
        self.assertEqual(art["abstract"], "BACKGROUND: Work ends. RESULTS: Purpose fell.")
        self.assertEqual(art["authors"], ["Jane Doe", "Panel Group"])
        self.assertEqual(art["oa_url"], "https://pmc.ncbi.nlm.nih.gov/articles/PMC999/")
        self.assertEqual((book["pmid"], book["title"], book["year"], book["type"]), ("777", "A chapter", 2020, "Book"))


class EuropePmcParsing(unittest.TestCase):
    def test_result(self):
        r = europepmc.parse_result({
            "id": "PPR1", "source": "PPR", "doi": "10.2/X", "title": "Meaning <i>after</i> work",
            "abstractText": "<h4>Aims</h4>Follow &amp; see.", "pubYear": "2024",
            "authorList": {"author": [{"fullName": "Roe R"}]}, "pubTypeList": {"pubType": ["Preprint"]},
            "fullTextUrlList": {"fullTextUrl": [{"availabilityCode": "OA", "url": "https://x/y"}]},
        })
        self.assertEqual((r["source_id"], r["doi"], r["year"]), ("PPR:PPR1", "10.2/x", 2024))
        self.assertEqual((r["title"], r["abstract"]), ("Meaning after work", "Aims Follow & see."))
        self.assertEqual((r["oa_url"], r["type"], r["authors"]), ("https://x/y", "Preprint", ["Roe R"]))


class PmidDedupe(unittest.TestCase):
    def test_pubmed_record_without_doi_merges_on_pmid(self):
        store = Store(":memory:")
        a = store.upsert_work({"openalex_id": "W1", "pmid": "12345", "title": "Purpose in life", "year": 2019})
        b = store.upsert_work({"pmid": "12345", "title": "Purpose in life: a panel study", "year": 2019,
                               "abstract": "From PubMed."})
        self.assertEqual(a, b)
        self.assertEqual(store.db.execute("SELECT abstract FROM works").fetchone()[0], "From PubMed.")


if __name__ == "__main__":
    unittest.main()


class EricSource(unittest.TestCase):
    def test_render_limits_every_group_to_title_and_abstract(self):
        tree = query.parse('("programmatic assessment" OR portfolio*) AND student* AND NOT review')
        self.assertEqual(query.render(tree, "eric"),
                         '(title:("programmatic assessment" OR portfolio*) OR description:("programmatic assessment" OR '
                         'portfolio*)) AND (title:(student*) OR description:(student*)) NOT (title:(review) OR '
                         'description:(review))')

    def test_parse_doc(self):
        from psyscientist.sources import eric
        rec = eric.parse_doc({"id": "EJ1385100", "title": "Teacher,  Gatekeeper", "author": ["Jamieson, Janica", "Hay"],
                              "description": "We  conducted interviews.", "source": "Advances in Health Sciences Education",
                              "publicationtype": ["Journal Articles", "Reports - Evaluative"],
                              "publicationdateyear": 2023, "url": "http://dx.doi.org/10.1007/s10459-022-10193-9",
                              "e_fulltextauth": 1, "language": ["English"]})
        self.assertEqual(rec["doi"], "10.1007/s10459-022-10193-9")
        self.assertEqual(rec["authors"], ["Janica Jamieson", "Hay"])
        self.assertEqual((rec["type"], rec["year"], rec["title"]), ("journal-article", 2023, "Teacher, Gatekeeper"))
        self.assertEqual(rec["oa_url"], "https://files.eric.ed.gov/fulltext/EJ1385100.pdf")
        self.assertIsNone(eric.parse_doc({"id": "ED1", "url": "https://example.org/x"})["doi"])
