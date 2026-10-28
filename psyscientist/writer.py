"""The evidence-first writer: a draft in which every sentence is traceable to finding rows.

AI Scientist-v2 writes first and looks for citations afterwards. Here the order
is reversed: the writer gets the run's findings (each with its verbatim
passage) and the insights you have not dropped, and must return the draft as
sentences, each declaring the finding rows it rests on. `verify` then checks
every sentence in code:

- a sentence that states something about the evidence cites at least one finding;
- every cited finding and insight exists in what the writer was given, and no dropped insight is used;
- every number in a sentence occurs in the findings it cites;
- framing sentences (signposts, transitions) cite nothing and contain no numbers.

The draft renders as Markdown with author-year citations and a reference list,
plus a verification appendix that lists every flagged sentence. Whether a
sentence represents its findings fairly is still for a person to judge; the
check guarantees only that the trail from sentence to source is complete.
"""

from __future__ import annotations

import html
import json
import re
from pathlib import Path

from . import insights, numgate
from .store import Store, now

# Input for the writer

def build_input(store: Store, run_id: int, protocol: dict) -> dict:
    rows = store.db.execute(
        """SELECT f.*, w.title, w.year, w.authors FROM findings f JOIN works w ON w.work_id = f.work_id
           WHERE f.run_id = ? AND f.status != 'superseded' AND f.extractor = (SELECT extractor FROM findings
                                                 WHERE run_id = ? ORDER BY created_at DESC LIMIT 1)
           ORDER BY f.work_id, f.finding_id""", (run_id, run_id)).fetchall()
    from .agents import removed_ids
    out = removed_ids(store, run_id)
    rows = [r for r in rows if r["work_id"] not in out]
    if not rows:
        raise ValueError("no findings in this run yet: run a findings task first")
    corpus = store.db.execute("SELECT corpus FROM runs WHERE run_id = ?", (run_id,)).fetchone()["corpus"]
    usable = [i for i in insights.rows(store, corpus) if i["status"] != "dropped"]
    from .tables import cite_map
    labels = cite_map(store, run_id)
    studies = {}
    for r in rows:
        studies[str(r["work_id"])] = {"cite": labels.get(r["work_id"]) or cite_label(r["authors"], r["year"]),
                                      "title": r["title"], "year": r["year"]}
    return {
        "question": " ".join((protocol.get("findings") or {}).get("question", "").split()),
        "findings": [{"id": r["finding_id"], "work_id": r["work_id"], "finding": r["finding"], "effect": r["effect"],
                      "for_whom": r["for_whom"], "fields": json.loads(r["fields"] or "{}"), "evidence": r["evidence"],
                      "source": r["source"]} for r in rows],
        "insights": [{"id": i["insight_id"], "kind": i["kind"], "status": i["status"], "statement": i["statement"],
                      "rationale": i["rationale"], "findings": json.loads(i["support"] or "{}").get("findings", [])}
                     for i in usable],
        "studies": studies,
    }


def instructions(protocol: dict) -> str:
    w = protocol.get("writing") or {}
    return "\n".join([
        f"# Writing task: {protocol.get('title', '')}",
        "",
        "batch_01.json holds the evidence you may use: `findings` (each with the verbatim passage that states it), "
        "`insights` (patterns, contradictions, gaps and hypotheses noticed so far; `status` says whether a person "
        "has reviewed them, `noted` means not yet) and `studies` (how to cite each study).",
        "",
        "Write " + w.get("section", "a synthesis section that answers the review question") + ", "
        f"about {w.get('words', '900 to 1400')} words, in clear academic English for psychologists.",
        "",
        "Review question: " + " ".join((protocol.get("findings") or {}).get("question", "").split()),
        "",
        "Rules, all checked by the pipeline:",
        "- Return the draft as paragraphs of sentences. Every sentence that says anything about the evidence lists "
        "in `cites` the finding ids it rests on (at least one). Cite only what supports that very sentence.",
        "- Every number in a sentence must appear in a finding it cites. No numbers otherwise.",
        "- A sentence may also list `insights` (ids) whose argument it follows. Insights are not evidence: a "
        "sentence built on an insight still cites the findings behind it.",
        "- Signposts and transitions that claim nothing set `frame` to true, cite nothing and contain no numbers. "
        "Keep them few.",
        "- No outside knowledge, no studies or theories not in the file, no citations in the text itself (the "
        "pipeline adds author-year citations from `cites`).",
        "- Say how strong the evidence is: number of studies, design (e.g. qualitative, cross-sectional, panel), "
        "abstract or full text. Present contradictions as contradictions. End with what the evidence cannot yet "
        "say.",
        "- Use the review's question to organise the section; headings are optional (`heading` on a paragraph).",
        "",
        insights.instructions(),
        "",
        "Output: one JSON object:",
        "",
        "```json",
        json.dumps({"title": "...", "paragraphs": [{"heading": "optional", "sentences": [
            {"text": "One sentence.", "cites": [12, 18], "insights": [17], "frame": False}]}], "insights": []}),
        "```",
    ]) + "\n"


# Checking

def check_shape(answer: dict, data: dict) -> list[str]:
    errs = []
    if not isinstance(answer, dict) or not isinstance(answer.get("paragraphs"), list) or not answer["paragraphs"]:
        return ["the answer needs a non-empty `paragraphs` list"]
    for p_i, p in enumerate(answer["paragraphs"], 1):
        sents = p.get("sentences") if isinstance(p, dict) else None
        if not isinstance(sents, list) or not sents:
            errs.append(f"paragraph {p_i}: needs `sentences`")
            continue
        for s_i, s in enumerate(sents, 1):
            if not isinstance(s, dict) or not (s.get("text") or "").strip():
                errs.append(f"paragraph {p_i} sentence {s_i}: needs `text`")
    return errs


def verify(answer: dict, data: dict, store: Store) -> dict:
    """Per sentence: verified, or the reasons it is not."""
    findings = {f["id"]: f for f in data["findings"]}
    given_insights = {i["id"] for i in data["insights"]}
    dropped = {r["insight_id"] for r in store.db.execute("SELECT insight_id FROM insights WHERE status = 'dropped'")}
    results, totals = [], {"sentences": 0, "verified": 0, "frame": 0, "flagged": 0, "words": 0}
    for p_i, p in enumerate(answer["paragraphs"]):
        for s_i, s in enumerate(p["sentences"]):
            text = s["text"].strip()
            cites = [int(c) for c in s.get("cites") or [] if str(c).lstrip("-").isdigit()]
            ins = [int(c) for c in s.get("insights") or [] if str(c).lstrip("-").isdigit()]
            problems = []
            nums = insights.numbers(text)
            if s.get("frame"):
                if cites:
                    problems.append("a framing sentence cites findings; mark it as a claim instead")
                if nums:
                    problems.append(f"a framing sentence contains numbers: {', '.join(nums)}")
            else:
                if not cites:
                    problems.append("states something but cites no finding")
                unknown = [c for c in cites if c not in findings]
                if unknown:
                    problems.append(f"cites findings that were not given: {unknown}")
                items = [numgate.finding_text(findings[c]["finding"], findings[c].get("effect"),
                                              findings[c].get("for_whom"), findings[c].get("evidence"))
                         for c in cites if c in findings]
                items += [str(data["studies"].get(str(findings[c]["work_id"]), {}).get("year") or "")
                          for c in cites if c in findings]
                flags = numgate.gate(insights.strip_references(text), items)
                missing = flags.pop("missing", None)
                if missing:
                    problems.append(f"numbers not in the cited findings: {', '.join(missing)}")
                problems += numgate.describe(flags)
            bad_ins = [i for i in ins if i not in given_insights or i in dropped]
            if bad_ins:
                problems.append(f"uses insights that were not given or are dropped: {bad_ins}")
            totals["sentences"] += 1
            totals["words"] += len(text.split())
            totals["frame"] += bool(s.get("frame"))
            totals["flagged"] += bool(problems)
            totals["verified"] += not problems and not s.get("frame")
            results.append({"p": p_i, "s": s_i, "problems": problems})
    return {"totals": totals, "sentences": results}


def store_draft(store: Store, corpus: str, run_id: int, author: str, answer: dict, verification: dict,
                call_id: int | None) -> int:
    with store.db:
        cur = store.db.execute(
            "INSERT INTO drafts (corpus, run_id, author, title, content, verification, call_id, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (corpus, run_id, author, answer.get("title"), json.dumps(answer, ensure_ascii=False),
             json.dumps(verification, ensure_ascii=False), call_id, now()))
    return cur.lastrowid


# Rendering

PARTICLES = {"van", "von", "de", "der", "den", "del", "della", "di", "da", "du", "le", "la", "ten", "ter", "zu", "dos",
             "das", "bin", "al", "el"}
# Double family names written without a hyphen, which no rule can tell from a middle name ('Monique Borgerhoff
# Mulder'): the family name as cited in the field, in lower case.
COMPOUND_FAMILY = {"borgerhoff mulder"}


def name_parts(name: str) -> tuple[str, str]:
    """(family, given): 'Janske van Eersel' -> ('van Eersel', 'Janske'); 'McNamara, N' -> ('McNamara', 'N')."""
    name = " ".join(name.replace("\u0640", "").split())       # Arabic tatweel pads letters, not names
    if "," in name:
        family, given = (x.strip() for x in name.split(",", 1))
        return family or "?", given
    parts = name.split()
    if not parts:
        return "?", ""
    last, first = parts[-1].replace(".", ""), parts[0].replace(".", "")
    if len(parts) > 1 and len(first) > 1 and last.isalpha() and len(last) <= 3 and (last.isupper() or len(last) == 1):
        return " ".join(parts[:-1]), parts[-1]          # PubMed style: 'Vercammen A', 'Crider YS'
    i = len(parts) - 1
    while i > 1 and parts[i - 1].lower() in PARTICLES:
        i -= 1
    if i > 1 and " ".join(parts[i - 1:]).lower() in COMPOUND_FAMILY:
        i -= 1
    return " ".join(parts[i:]), " ".join(parts[:i])


TITLES = re.compile(r"^(dr|prof|professor|ph\.?d|mr|mrs|ms)\.?$|^(أ\.?\s?م\.?\s?د|أ\.?\s?د|د)\.$", re.I)
TURKISH = set("ÇĞİÖŞÜçğıöşü")


def _recase(token: str) -> str:
    """A name written in capitals ('DZHEGUR', 'ÖZDEMİR') in the usual case; Turkish dotted and dotless i kept apart."""
    if len(token) < 4 or not token.isalpha() or not token.isupper():     # NAS, YS: initials
        return token
    rest = token[1:]
    if TURKISH & set(token):
        rest = rest.replace("I", "ı").replace("İ", "i")
    return token[0] + rest.lower()


def clean_names(names) -> list[str]:
    """Author names as the sources give them, cleaned: entities decoded, two people in one string split ('Sidra Raza,
    Shumaila Naz, Shiza Kazmi'; 'Riam Rahim Abd      Prof. Dr. Haider Lazim Khunaisir'), titles such as Dr. and Prof.
    dropped, stray quotes stripped and names written in capitals put in the usual case. 'Family, Given' stays one name."""
    out = []
    for raw in names:
        raw = html.unescape(str(raw)).replace("ـ", "")
        parts = [p for p in re.split(r"\s{3,}", raw.strip()) if p.strip()]
        split = []
        for p in parts:
            pieces = [x.strip() for x in p.split(",")]
            people = len(pieces) >= 2 and all(len(x.split()) >= 2 for x in pieces)
            several = people and (len(pieces) >= 3 or re.search(r",\S", p))
            split += pieces if several else [p]
        for p in split:
            tokens = [x.strip("'\"‘’“”") for x in p.split()]
            # repositories append a person id to the name ('R Layfield (7743791)'): it is not part of the name
            tokens = [x for x in tokens if x and not TITLES.match(x) and not re.fullmatch(r"\(\d+\)", x)]
            if len(tokens) > 1:
                tokens = [_recase(x) for x in tokens]
            name = " ".join(tokens).strip(" ,")
            if name:
                out.append(name)
    return out


def _names(authors_json) -> list[str]:
    try:
        return clean_names(n for n in json.loads(authors_json or "[]") if str(n).strip())
    except ValueError:
        return []


SMALL_WORDS = {"a", "an", "the", "of", "and", "in", "for", "on", "to", "at", "by", "with", "or", "as", "from", "&"}


def short_title(title: str | None) -> str:
    """APA's in-text label for a work without authors: the first words of its title in title case, in double
    quotes ('“Neuroticism and Artificial Intelligence,”')."""
    words = html.unescape(" ".join(str(title or "").split())).split()
    if not words:
        return "“Untitled,”"
    out, content = [], 0
    for w in words:
        core = w.strip(":;,.?!()[]\"'“”‘’")
        if core:
            out.append(core)
            content += core.lower() not in SMALL_WORDS
        if content >= 3 or len(out) >= 5 or w.endswith((":", "?", "!", ".")):
            break
    while len(out) > 1 and out[-1].lower() in SMALL_WORDS:
        out.pop()
    shouting = all(not c.islower() for c in "".join(words))       # a title in capitals: acronyms cannot be told
    cased = [w if not shouting and (any(c.isupper() for c in w[1:]) or (w.isupper() and len(w) <= 4))
             else (w.lower() if i and w.lower() in SMALL_WORDS else w[:1].upper() + w[1:].lower())
             for i, w in enumerate(out)]
    return "“" + " ".join(cased) + ",”"


def cite_label(authors_json: str | None, year, title: str | None = None) -> str:
    names = _names(authors_json)
    if not names:
        return f"{short_title(title)} {year or 'n.d.'}" if title else f"Anonymous, {year or 'n.d.'}"
    elif len(names) == 1:
        who = name_parts(names[0])[0]
    elif len(names) == 2:
        who = f"{name_parts(names[0])[0]} & {name_parts(names[1])[0]}"
    else:
        who = f"{name_parts(names[0])[0]} et al."
    return f"{who}, {year or 'n.d.'}"


def cite_labels(rows) -> dict[int, str]:
    """Author-year labels for a set of works, as APA gives them: works that would share a label get a, b, ...
    after the year, in the order of their titles ("n.d." becomes "n.d.-a")."""
    base = {r["work_id"]: cite_label(r["authors"], r["year"], r["title"]) for r in rows}
    by: dict[str, list] = {}
    for r in rows:
        by.setdefault(base[r["work_id"]], []).append(r)
    out = dict(base)
    for label, same in by.items():
        if len(same) > 1:
            same = sorted(same, key=lambda r: (" ".join((r["title"] or "").casefold().split()), r["work_id"]))
            for i, r in enumerate(same):
                out[r["work_id"]] = label + ("-" if label.endswith("n.d.") else "") + "abcdefghijklmnopqrstuvwxyz"[i % 26]
    return out


def initials(given: str) -> str:
    """'Lisa Marie' -> 'L. M.'; 'Jean-Paul' -> 'J.-P.'; 'JS' -> 'J. S.'; 'N' -> 'N.'."""
    out = []
    for tok in given.replace(".", ". ").split():
        if tok.isupper() and tok.isalpha() and 1 < len(tok) <= 3:
            out += [c + "." for c in tok]
        else:
            out.append("-".join(p[0].upper() + "." for p in tok.strip(".").split("-") if p))
    return " ".join(o for o in out if o)


def _stray_initial(name: str) -> bool:
    """'Bhashkar Mazumder B': a full name with a stray letter after it, which name_parts would read PubMed style."""
    parts = name.replace(",", " ").split()
    return len(parts) >= 3 and len(parts[-1].strip(".")) == 1 and all(len(p) > 2 and not p.isupper()
                                                                       for p in parts[:-1])


def apa_authors(authors_json) -> str:
    raw = _names(authors_json)
    # In a list of full names ('Esther Duflo', 'Pascaline Dupas Y'), a trailing letter is a stray, not initials.
    if any("," not in n and not _stray_initial(n) and len(n.split()) >= 2 and len(n.split()[-1]) > 1 for n in raw):
        raw = [" ".join(n.split()[:-1]) if _stray_initial(n) else n for n in raw]
    names, seen = [], set()
    for n in raw:
        family, given = name_parts(n)
        key = (family.casefold(), initials(given)[:1])      # a list given twice ('Christoph Kubitza', 'Kubitza, C.')
        if key in seen:
            continue
        seen.add(key)
        names.append(f"{family}, {initials(given)}" if given else family)
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    if len(names) > 20:
        return ", ".join(names[:19]) + ", ... " + names[-1]
    return ", ".join(names[:-1]) + ", & " + names[-1]


TYPE_LABELS = {"dissertation": "Thesis", "conference-abstract": "Conference abstract", "dataset": "Data set",
               "editorial": "Editorial", "preprint": "Preprint", "posted-content": "Preprint", "report": "Report"}
REPOSITORY_TYPES = {"dissertation", "dataset", "preprint", "posted-content", "report"}


MONTHS_DAYS = {"January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
               "November", "December", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"}


def case_lexicon(texts) -> set[str]:
    """Proper nouns of a body of text: words it capitalises mid-sentence and never writes in lower case."""
    context = " ".join(t for t in texts if t)
    lower_seen = set(re.findall(r"\b[a-z][\w’]*", context))
    proper = set(re.findall(r"(?<=[^.!?:\s] )[A-Z][a-z][\w’]*", context))
    return {w for w in proper if w.lower() not in lower_seen} | MONTHS_DAYS


def sentence_case(title: str, proper: set[str] | None = None) -> str:
    """APA sentence case: capitals stay on the first word, the first word after a colon, acronyms,
    words with inner capitals or digits, and proper nouns (see case_lexicon)."""
    proper = proper or MONTHS_DAYS
    punct = "\"'“”‘’()[],;:?!."

    def keep(part: str, first: bool) -> bool:
        core = part.strip(punct)
        letters = [c for c in core if c.isalpha()]
        if first or not letters or any(c.isdigit() for c in core):
            return True
        if len(letters) > 1 and all(c.isupper() for c in letters):      # AI, COVID, U.S.
            return True
        if any(c.isupper() for c in core[1:]):                          # McNamara, NZAVS
            return True
        return core in proper or re.sub(r"['’]s$", "", core) in proper

    def lower_first(part: str) -> str:
        i = next((i for i, c in enumerate(part) if c.isalpha()), None)
        return part if i is None else part[:i] + part[i].lower() + part[i + 1:]

    out, start = [], True
    for tok in re.split(r"(\s+)", title):
        if not tok or tok.isspace():
            out.append(tok)
            continue
        parts = tok.split("-")
        if start and len(out) > 1:                                      # first word after a colon
            i = next((i for i, c in enumerate(parts[0]) if c.isalpha()), None)
            if i is not None:
                parts[0] = parts[0][:i] + parts[0][i].upper() + parts[0][i + 1:]
        out.append("-".join(p if keep(p, start and i == 0) else lower_first(p) for i, p in enumerate(parts)))
        last = tok.rstrip("\"'”’)")
        start = last.endswith((":", "?", "!")) or (last.endswith(".") and not last[:-1].replace(".", "").isupper())
    return "".join(out)


INDEX_VENUE = re.compile(r"\s*(DOAJ\b|Directory of Open Access Journals)", re.I)


def reference(work, bib=None, proper: set[str] | None = None, suffix: str = "") -> str:
    """An APA-style reference from a works row (and its Crossref details, if any): sentence-case title
    (English titles only), labelled type, italic journal and volume, issue and pages, repository names
    plain. `proper`: proper nouns to keep capitalised, from case_lexicon over the corpus."""
    kind = (work["type"] or "").split(";")[0].strip()
    kind = {"JournalArticle": "article", "journal-article": "article", "Review": "review"}.get(kind, kind)
    title = html.unescape(" ".join((work["title"] or "Untitled").split())).rstrip(". ")
    abstract = (work["abstract"] if "abstract" in work.keys() else None) or ""
    if title.isupper():           # in capitals: keep the acronyms the abstract writes in capitals, and known proper nouns
        acronyms = set(re.findall(r"\b[A-Z][A-Z0-9-]+\b", abstract)) | {"AI"}
        def recase(w: str) -> str:
            core = w.strip("\"'“”‘’()[],;:?!.")
            if core in acronyms:
                return w
            if core.capitalize() in (proper or set()):
                return w.lower().replace(core.lower(), core.capitalize())
            return w.lower()
        title = " ".join("-".join(recase(p) for p in w.split("-")) for w in title.split())
        title = title[0].upper() + title[1:]
    lang = (work["language"] if "language" in work.keys() else None) or "en"
    if lang.lower().startswith("en"):
        title = sentence_case(title, proper or case_lexicon([work["abstract"] if "abstract" in work.keys() else ""]))
    label = TYPE_LABELS.get(kind)
    title += f" [{label}]." if label else ("" if title[-1:] in "?!" else ".")
    own = work["venue"] or ""
    if INDEX_VENUE.match(own):        # the index a record came through (DOAJ), not its journal: Crossref's, or none
        own = ""
    venue = html.unescape(" ".join((own or (bib["container"] if bib else "") or "").split()))
    if kind in REPOSITORY_TYPES:
        venue = re.sub(r"\s*\([^()]*\)$", "", venue)
        venue = f" {venue}." if venue else ""
    elif kind == "book-chapter":
        publisher = f" {bib['publisher']}." if bib and bib["publisher"] else ""
        venue = (f" In *{venue}*." if venue else "") + publisher
    elif venue:
        volume = f", *{bib['volume']}*" if bib and bib["volume"] else ""
        issue = f"({bib['issue']})" if bib and bib["volume"] and bib["issue"] else ""
        pages = f", {bib['pages']}" if bib and bib["pages"] else ""
        venue = f" *{venue}*{volume}{issue}{pages}."
    doi = f" https://doi.org/{work['doi']}" if work["doi"] else ""
    who = apa_authors(work["authors"])
    if not who:                                         # APA: a work without authors is listed by its title
        return f"{title} ({work['year'] or 'n.d.'}{suffix}).{venue}{doi}"
    return f"{who} ({work['year'] or 'n.d.'}{suffix}). {title}{venue}{doi}"


def latest(store: Store, run_id: int):
    return store.db.execute("SELECT * FROM drafts WHERE run_id = ? ORDER BY draft_id DESC LIMIT 1",
                            (run_id,)).fetchone()


def render_markdown(store: Store, draft_id: int) -> str:
    d = store.db.execute("SELECT * FROM drafts WHERE draft_id = ?", (draft_id,)).fetchone()
    content, ver = json.loads(d["content"]), json.loads(d["verification"] or "{}")
    flags = {(r["p"], r["s"]): r["problems"] for r in ver.get("sentences", [])}
    ids = sorted({c for p in content["paragraphs"] for s in p["sentences"] for c in s.get("cites") or []})
    rows = {r["finding_id"]: r for r in store.db.execute(
        f"""SELECT f.finding_id, f.work_id, f.evidence, f.source, w.title, w.year, w.authors, w.venue, w.doi
            FROM findings f JOIN works w ON w.work_id = f.work_id
            WHERE f.finding_id IN ({','.join('?' * len(ids))})""", tuple(ids))} if ids else {}
    t = ver.get("totals", {})
    unreviewed = store.db.execute("SELECT count(*) FROM insights WHERE corpus = ? AND status = 'noted'",
                                  (d["corpus"],)).fetchone()[0]
    out = [f"# {content.get('title') or 'Draft'}", "",
           f"*Draft {d['draft_id']}, written by {d['author']} on {d['created_at'][:10]} from run {d['run_id']}'s "
           f"findings. {t.get('verified', 0)} of {t.get('sentences', 0)} sentences verified, "
           f"{t.get('frame', 0)} framing, {t.get('flagged', 0)} flagged (see the end). "
           f"{unreviewed} of the insights it could use are not yet reviewed by a person.*", ""]
    for p_i, p in enumerate(content["paragraphs"]):
        if p.get("heading"):
            out += [f"## {p['heading']}", ""]
        parts = []
        for s_i, s in enumerate(p["sentences"]):
            text = s["text"].strip()
            works = []
            for c in s.get("cites") or []:
                r = rows.get(c)
                if r and r["work_id"] not in [w for w, _ in works]:
                    works.append((r["work_id"], cite_label(r["authors"], r["year"])))
            if works:
                end = text[-1] if text[-1:] in ".!?" else ""
                body = text[:-1] if end else text
                text = f"{body} ({'; '.join(label for _, label in works)}){end}"
            if flags.get((p_i, s_i)):
                text += " **[⚠]**"
            parts.append(text)
        out += [" ".join(parts), ""]
    out += ["## References", ""]
    seen = {}
    for r in rows.values():
        seen.setdefault(r["work_id"], r)
    for r in sorted(seen.values(), key=lambda r: cite_label(r["authors"], r["year"])):
        try:
            names = json.loads(r["authors"] or "[]")
        except ValueError:
            names = []
        who = ", ".join(names[:6]) + (" et al." if len(names) > 6 else "")
        link = f" https://doi.org/{r['doi']}" if r["doi"] else ""
        out.append(f"- {who or 'Anonymous'} ({r['year'] or 'n.d.'}). {r['title']}. *{r['venue'] or ''}*.{link}")
    out += ["", "## Verification", "",
            "Every sentence's citations were checked in code: cited findings exist, numbers occur in them, framing "
            "sentences claim nothing. Whether each sentence represents its findings fairly is for a person to judge.",
            ""]
    flagged = [r for r in ver.get("sentences", []) if r["problems"]]
    if not flagged:
        out.append("No sentence was flagged.")
    for r in flagged:
        s = content["paragraphs"][r["p"]]["sentences"][r["s"]]
        out.append(f"- “{s['text'].strip()}” — " + "; ".join(r["problems"]))
    out += ["", "## Evidence behind each citation", ""]
    for c in ids:
        r = rows.get(c)
        if r:
            out.append(f"- F{c} ({cite_label(r['authors'], r['year'])}, {r['source'] or 'quote not found'}): "
                       f"“{' '.join((r['evidence'] or '').split())}”")
    return "\n".join(out) + "\n"


def write_markdown(store: Store, draft_id: int, root: Path) -> Path:
    d = store.db.execute("SELECT * FROM drafts WHERE draft_id = ?", (draft_id,)).fetchone()
    path = root / "output" / f"corpus_{d['corpus']}" / f"run_{d['run_id']:03d}" / f"draft_{draft_id:02d}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_markdown(store, draft_id), encoding="utf-8")
    return path
