"""The evidence store: one SQLite file shared by every pipeline stage.

Every external call (HTTP now, model calls later) is logged in `calls` with its
response body, so any record or claim can be traced back to what was received.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import re
import sqlite3
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS protocols (
    sha256      TEXT PRIMARY KEY,
    corpus      TEXT NOT NULL,
    path        TEXT NOT NULL,
    content     TEXT NOT NULL,
    first_used  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    run_id           INTEGER PRIMARY KEY,
    kind             TEXT NOT NULL,              -- pilot, search, screening, ...
    corpus           TEXT NOT NULL,
    protocol_sha256  TEXT REFERENCES protocols(sha256),
    params           TEXT,                       -- JSON
    started_at       TEXT NOT NULL,
    finished_at      TEXT,
    status           TEXT NOT NULL DEFAULT 'running',
    notes            TEXT
);

-- One row per source per run: what was asked, what the source said it had, what we got.
CREATE TABLE IF NOT EXISTS run_sources (
    run_id          INTEGER NOT NULL REFERENCES runs(run_id),
    source          TEXT NOT NULL,
    query           TEXT NOT NULL,               -- the string exactly as sent
    reported_total  INTEGER,
    fetched         INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL,               -- ok, truncated, failed
    error           TEXT,
    notes           TEXT,                        -- e.g. the source's own translation of the query
    PRIMARY KEY (run_id, source)
);

CREATE TABLE IF NOT EXISTS calls (
    call_id          INTEGER PRIMARY KEY,
    run_id           INTEGER REFERENCES runs(run_id),
    kind             TEXT NOT NULL,              -- http, llm
    source           TEXT NOT NULL,
    request          TEXT NOT NULL,              -- URL with keys redacted, or prompt
    status           INTEGER,
    duration_ms      INTEGER,
    response_sha256  TEXT,
    response_gz      BLOB,
    meta             TEXT,                       -- JSON: rate-limit headers, model, tokens
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS works (
    work_id      INTEGER PRIMARY KEY,
    doi          TEXT UNIQUE,
    openalex_id  TEXT UNIQUE,
    s2_id        TEXT UNIQUE,
    pmid         TEXT,
    title        TEXT,
    title_key    TEXT,
    abstract     TEXT,
    year         INTEGER,
    venue        TEXT,
    type         TEXT,
    language     TEXT,
    authors      TEXT,                           -- JSON list of names
    oa_url       TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS works_title_key ON works(title_key);
CREATE INDEX IF NOT EXISTS works_pmid ON works(pmid);

CREATE TABLE IF NOT EXISTS retrievals (
    run_id     INTEGER NOT NULL REFERENCES runs(run_id),
    work_id    INTEGER NOT NULL REFERENCES works(work_id),
    source     TEXT NOT NULL,
    source_id  TEXT,
    rank       INTEGER,
    PRIMARY KEY (run_id, work_id, source)
);

-- Fixed draws of records, e.g. the pilot screening sheet or the 300-record validation set.
CREATE TABLE IF NOT EXISTS samples (
    run_id   INTEGER NOT NULL REFERENCES runs(run_id),
    work_id  INTEGER NOT NULL REFERENCES works(work_id),
    purpose  TEXT NOT NULL,
    PRIMARY KEY (run_id, work_id, purpose)
);

CREATE TABLE IF NOT EXISTS screening (
    screening_id  INTEGER PRIMARY KEY,
    run_id        INTEGER REFERENCES runs(run_id),
    work_id       INTEGER NOT NULL REFERENCES works(work_id),
    corpus        TEXT NOT NULL,
    stage         TEXT NOT NULL CHECK (stage IN ('title_abstract', 'full_text')),
    screener      TEXT NOT NULL,                 -- human:reviewer, model:<id>
    decision      TEXT NOT NULL CHECK (decision IN ('include', 'exclude', 'unsure')),
    reason        TEXT,
    note          TEXT,
    call_id       INTEGER REFERENCES calls(call_id),
    created_at    TEXT NOT NULL,
    UNIQUE (run_id, work_id, stage, screener)
);

-- One row per finding: what a study found, for whom, and the passage that says so.
-- The evidence-first writer may cite only these rows.
CREATE TABLE IF NOT EXISTS findings (
    finding_id  INTEGER PRIMARY KEY,
    work_id     INTEGER NOT NULL REFERENCES works(work_id),
    run_id      INTEGER REFERENCES runs(run_id),
    extractor   TEXT NOT NULL,
    finding     TEXT NOT NULL,                   -- one sentence, in the extractor's words
    effect      TEXT,                            -- effect size, CI or test as reported
    for_whom    TEXT,                            -- subgroup, moderator or trajectory class
    fields      TEXT,                            -- JSON: the protocol's [findings] fields
    evidence    TEXT NOT NULL,                   -- verbatim passage
    location    TEXT,                            -- "abstract" or the full-text section
    source      TEXT,                            -- abstract or fulltext, from where the quote was found
    grounded    INTEGER,
    status      TEXT NOT NULL DEFAULT 'extracted'
                CHECK (status IN ('extracted', 'verified', 'disputed', 'superseded')),
    call_id     INTEGER REFERENCES calls(call_id),
    created_at  TEXT NOT NULL
);

-- Every reading of a study for its findings, also one that found none: what the extractor read and how many it found.
-- A study read in full that yields no findings reports no results of its own ([[paper.core]] no_results_fails).
CREATE TABLE IF NOT EXISTS findings_reads (
    read_id     INTEGER PRIMARY KEY,
    run_id      INTEGER REFERENCES runs(run_id),
    work_id     INTEGER NOT NULL REFERENCES works(work_id),
    extractor   TEXT NOT NULL,
    read_from   TEXT NOT NULL,                   -- full text, abstract
    findings    INTEGER NOT NULL,
    call_id     INTEGER REFERENCES calls(call_id),
    created_at  TEXT NOT NULL
);

-- Insights that emerge along the way: patterns, anomalies, gaps, hypotheses nobody asked for.
CREATE TABLE IF NOT EXISTS insights (
    insight_id  INTEGER PRIMARY KEY,
    corpus      TEXT NOT NULL,
    run_id      INTEGER REFERENCES runs(run_id),
    author      TEXT NOT NULL,                   -- human:reviewer or model:<id>
    stage       TEXT NOT NULL,                   -- screen, extract, findings, synthesis, manual
    kind        TEXT NOT NULL CHECK (kind IN ('pattern', 'anomaly', 'contradiction', 'gap', 'method',
                                              'hypothesis', 'question')),
    statement   TEXT NOT NULL,
    rationale   TEXT,
    support     TEXT,                            -- JSON: {"works": [...], "findings": [...], "quotes": [...]}
    status      TEXT NOT NULL DEFAULT 'noted',
    call_id     INTEGER REFERENCES calls(call_id),
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- Drafts from the evidence-first writer: sentences with the finding rows they rest on, and their checks.
CREATE TABLE IF NOT EXISTS drafts (
    draft_id     INTEGER PRIMARY KEY,
    corpus       TEXT NOT NULL,
    run_id       INTEGER REFERENCES runs(run_id),
    author       TEXT NOT NULL,
    title        TEXT,
    content      TEXT NOT NULL,                  -- JSON: paragraphs of sentences with their citations
    verification TEXT,                           -- JSON: per-sentence check results and totals
    call_id      INTEGER REFERENCES calls(call_id),
    created_at   TEXT NOT NULL
);

-- Every status change of an insight, so the path from first hunch to the paper stays visible.
CREATE TABLE IF NOT EXISTS insight_events (
    event_id    INTEGER PRIMARY KEY,
    insight_id  INTEGER NOT NULL REFERENCES insights(insight_id),
    at          TEXT NOT NULL,
    author      TEXT NOT NULL,
    status      TEXT NOT NULL,
    note        TEXT
);

-- Philosophy rows: you extract these yourself; the agents only index them.
CREATE TABLE IF NOT EXISTS philosophy_claims (
    claim_id            INTEGER PRIMARY KEY,
    source              TEXT NOT NULL,           -- e.g. Aristotle, Nicomachean Ethics
    locator             TEXT NOT NULL,           -- e.g. X.7, 1177b4
    claim               TEXT NOT NULL,
    concept             TEXT,
    relates_to_finding  INTEGER REFERENCES findings(finding_id),
    relation            TEXT CHECK (relation IN ('explains', 'contradicts', 'qualifies')),
    note                TEXT,
    created_at          TEXT NOT NULL
);

-- Per-criterion scores behind a metrics screener's decision.
CREATE TABLE IF NOT EXISTS screening_scores (
    run_id     INTEGER NOT NULL REFERENCES runs(run_id),
    work_id    INTEGER NOT NULL REFERENCES works(work_id),
    screener   TEXT NOT NULL,
    metric     TEXT NOT NULL,                    -- id from the protocol's [[screening.metrics]]
    score      INTEGER NOT NULL CHECK (score IN (0, 1, 2)),   -- 0 not met, 1 cannot tell, 2 met
    evidence   TEXT,                             -- quote from the title or abstract
    grounded   INTEGER,                          -- 1 if that quote is verbatim in the record
    call_id    INTEGER REFERENCES calls(call_id),
    PRIMARY KEY (run_id, work_id, screener, metric)
);

-- What a study did, one row per map dimension from the protocol's [map].
CREATE TABLE IF NOT EXISTS study_features (
    work_id    INTEGER NOT NULL REFERENCES works(work_id),
    extractor  TEXT NOT NULL,
    dimension  TEXT NOT NULL,
    value      TEXT NOT NULL,                    -- as the record states it, or not_reported
    canonical  TEXT NOT NULL,                    -- short label(s) used for matching, "; " between several
    evidence   TEXT,
    grounded   INTEGER,
    run_id     INTEGER REFERENCES runs(run_id),
    call_id    INTEGER REFERENCES calls(call_id),
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_id, work_id, extractor, dimension)    -- per run: two corpora may chart the same record
);

-- Open-access full text per work: where it came from, under which licence, and its sections.
CREATE TABLE IF NOT EXISTS fulltexts (
    work_id     INTEGER PRIMARY KEY REFERENCES works(work_id),
    status      TEXT NOT NULL,                   -- parsed, no_open_access, failed
    source      TEXT,                            -- europepmc, unpaywall, openalex
    url         TEXT,
    license     TEXT,
    version     TEXT,                            -- publishedVersion, acceptedVersion, submittedVersion
    file_path   TEXT,                            -- relative to the project; the file itself stays local
    file_sha256 TEXT,
    parser      TEXT,                            -- jats, grobid <version>, pypdf
    sections    TEXT,                            -- JSON list of {head, text}
    chars       INTEGER,
    error       TEXT,
    updated_at  TEXT NOT NULL
);

-- Proposed links between a study and a claim from the protocol's [[claims]].
CREATE TABLE IF NOT EXISTS claim_links (
    work_id    INTEGER NOT NULL REFERENCES works(work_id),
    extractor  TEXT NOT NULL,
    claim      TEXT NOT NULL,
    relation   TEXT NOT NULL CHECK (relation IN ('supports', 'challenges', 'qualifies')),
    evidence   TEXT,
    grounded   INTEGER,
    reason     TEXT,
    run_id     INTEGER REFERENCES runs(run_id),
    call_id    INTEGER REFERENCES calls(call_id),
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_id, work_id, extractor, claim)
);
"""

ID_COLUMNS = ("doi", "openalex_id", "s2_id")
MERGE_COLUMNS = ("pmid", "title", "abstract", "year", "venue", "type", "language", "authors", "oa_url")
BUSY_TIMEOUT_S = 120.0   # how long a write waits for another process's write to finish (an agent import can take a minute)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_doi(doi: str | None) -> str | None:
    if not doi:
        return None
    doi = doi.strip().lower()
    doi = re.sub(r"^(https?://)?(dx\.)?doi\.org/", "", doi)
    doi = re.sub(r"^doi:\s*", "", doi)
    return doi or None


def _flag(value: bool | None) -> int | None:
    return None if value is None else int(bool(value))


def work_link(w) -> str:
    if w["doi"]:
        return f"https://doi.org/{w['doi']}"
    if w["openalex_id"]:
        return f"https://openalex.org/{w['openalex_id']}"
    if w["s2_id"]:
        return f"https://www.semanticscholar.org/paper/{w['s2_id']}"
    return ""


def title_key(title: str | None) -> str | None:
    if not title:
        return None
    # NFKC: a ligature or full-width letter from a PDF ("artiﬁcial") keys like the plain letters
    key = re.sub(r"[^\w]+", " ", unicodedata.normalize("NFKC", title).lower()).strip()
    return re.sub(r"\s+", " ", key) or None


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        # A long command (abstracts, mine, fulltext) writes while another one may import: wait for the other
        # writer instead of failing with "database is locked".
        self.db = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT_S)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Changes made after the first store was created."""
        cols = {r["name"] for r in self.db.execute("PRAGMA table_info(run_sources)")}
        if "notes" not in cols:
            with self.db:
                self.db.execute("ALTER TABLE run_sources ADD COLUMN notes TEXT")
        # findings first had fixed, corpus-specific columns; its fields now come from the protocol.
        cols = {r["name"] for r in self.db.execute("PRAGMA table_info(findings)")}
        if "extractor" not in cols and self.db.execute("SELECT count(*) FROM findings").fetchone()[0] == 0:
            with self.db:
                self.db.execute("DROP TABLE findings")
            self.db.executescript(SCHEMA)
        # Re-extracted findings are kept as 'superseded'; older stores do not allow that status yet.
        sql = self.db.execute("SELECT sql FROM sqlite_master WHERE name = 'findings'").fetchone()[0]
        if "'superseded'" not in sql:
            self._rebuild("findings", sql.replace("'disputed')", "'disputed', 'superseded')"))
        # Charting and claim links were first keyed without the run, so a record charted in two corpora by the same
        # extractor kept only the later corpus's values.
        for table, old, new in (("study_features", "PRIMARY KEY (work_id, extractor, dimension)",
                                 "PRIMARY KEY (run_id, work_id, extractor, dimension)"),
                                ("claim_links", "PRIMARY KEY (work_id, extractor, claim)",
                                 "PRIMARY KEY (run_id, work_id, extractor, claim)")):
            sql = self.db.execute("SELECT sql FROM sqlite_master WHERE name = ?", (table,)).fetchone()[0]
            if old in sql:
                self._rebuild(table, sql.replace(old, new))

    def _rebuild(self, table: str, create: str) -> None:
        """Recreate a table with a new definition, keeping its rows and ids (SQLite cannot alter a CHECK).
        The copy is built under a new name and renamed, so other tables' references to it stay valid."""
        create = re.sub(rf'^CREATE TABLE\s+(IF NOT EXISTS\s+)?"?{table}"?', f"CREATE TABLE {table}_new", create.strip())
        self.db.execute("PRAGMA foreign_keys = OFF")
        try:
            with self.db:
                self.db.execute(create)
                self.db.execute(f"INSERT INTO {table}_new SELECT * FROM {table}")
                self.db.execute(f"DROP TABLE {table}")
                self.db.execute(f"ALTER TABLE {table}_new RENAME TO {table}")
            self.db.executescript(SCHEMA)          # indexes on the table
        finally:
            self.db.execute("PRAGMA foreign_keys = ON")

    def close(self) -> None:
        self.db.close()

    # Protocols and runs

    def add_protocol(self, corpus: str, path: str, content: str) -> str:
        sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO protocols (sha256, corpus, path, content, first_used) VALUES (?, ?, ?, ?, ?)",
                (sha, corpus, path, content, now()),
            )
        return sha

    def start_run(self, kind: str, corpus: str, protocol_sha256: str | None, params: dict) -> int:
        with self.db:
            cur = self.db.execute(
                "INSERT INTO runs (kind, corpus, protocol_sha256, params, started_at) VALUES (?, ?, ?, ?, ?)",
                (kind, corpus, protocol_sha256, json.dumps(params, ensure_ascii=False), now()),
            )
        return cur.lastrowid

    def finish_run(self, run_id: int, status: str = "ok", notes: str | None = None) -> None:
        with self.db:
            self.db.execute(
                "UPDATE runs SET finished_at = ?, status = ?, notes = ? WHERE run_id = ?",
                (now(), status, notes, run_id),
            )

    def set_run_source(self, run_id: int, source: str, query: str, reported_total: int | None,
                       fetched: int, status: str, error: str | None = None, notes: str | None = None) -> None:
        with self.db:
            self.db.execute(
                """INSERT INTO run_sources (run_id, source, query, reported_total, fetched, status, error, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (run_id, source) DO UPDATE SET
                     query = excluded.query, reported_total = excluded.reported_total,
                     fetched = excluded.fetched, status = excluded.status, error = excluded.error,
                     notes = excluded.notes""",
                (run_id, source, query, reported_total, fetched, status, error, notes),
            )

    # Call log

    def log_call(self, run_id: int | None, kind: str, source: str, request: str, status: int | None,
                 duration_ms: int, response: bytes | None, meta: dict | None = None) -> int:
        sha = hashlib.sha256(response).hexdigest() if response is not None else None
        blob = gzip.compress(response) if response is not None else None
        with self.db:
            cur = self.db.execute(
                """INSERT INTO calls (run_id, kind, source, request, status, duration_ms,
                                      response_sha256, response_gz, meta, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (run_id, kind, source, request, status, duration_ms, sha, blob,
                 json.dumps(meta or {}, ensure_ascii=False), now()),
            )
        return cur.lastrowid

    def call_response(self, call_id: int) -> bytes | None:
        row = self.db.execute("SELECT response_gz FROM calls WHERE call_id = ?", (call_id,)).fetchone()
        return gzip.decompress(row["response_gz"]) if row and row["response_gz"] else None

    # Works

    def find_work(self, rec: dict) -> int | None:
        for col in ID_COLUMNS:
            if rec.get(col):
                row = self.db.execute(f"SELECT work_id FROM works WHERE {col} = ?", (rec[col],)).fetchone()
                if row:
                    return row["work_id"]
        if rec.get("pmid"):
            row = self.db.execute("SELECT work_id FROM works WHERE pmid = ?", (rec["pmid"],)).fetchone()
            if row:
                return row["work_id"]
        key = title_key(rec.get("title"))
        if key:
            for row in self.db.execute("SELECT work_id, year FROM works WHERE title_key = ?", (key,)):
                # Same title within a year: a preprint and its journal version count as one work.
                if row["year"] is None or rec.get("year") is None or abs(row["year"] - rec["year"]) <= 1:
                    return row["work_id"]
        return None

    def upsert_work(self, rec: dict) -> int:
        """Insert a normalized record, or fill the gaps of the work it duplicates."""
        rec = dict(rec)
        rec["doi"] = normalize_doi(rec.get("doi"))
        if isinstance(rec.get("authors"), list):
            rec["authors"] = json.dumps(rec["authors"], ensure_ascii=False)
        work_id = self.find_work(rec)
        stamp = now()
        with self.db:
            if work_id is None:
                cols = ID_COLUMNS + MERGE_COLUMNS
                cur = self.db.execute(
                    f"""INSERT INTO works ({', '.join(cols)}, title_key, created_at, updated_at)
                        VALUES ({', '.join('?' * len(cols))}, ?, ?, ?)""",
                    tuple(rec.get(c) for c in cols) + (title_key(rec.get("title")), stamp, stamp),
                )
                return cur.lastrowid
            existing = self.db.execute("SELECT * FROM works WHERE work_id = ?", (work_id,)).fetchone()
            updates = {}
            for col in MERGE_COLUMNS:
                if existing[col] in (None, "") and rec.get(col) not in (None, ""):
                    updates[col] = rec[col]
            for col in ID_COLUMNS:
                if existing[col] is None and rec.get(col):
                    taken = self.db.execute(f"SELECT 1 FROM works WHERE {col} = ?", (rec[col],)).fetchone()
                    if not taken:
                        updates[col] = rec[col]
            if "title" in updates:
                updates["title_key"] = title_key(updates["title"])
            if updates:
                updates["updated_at"] = stamp
                assignments = ", ".join(f"{c} = ?" for c in updates)
                self.db.execute(f"UPDATE works SET {assignments} WHERE work_id = ?",
                                (*updates.values(), work_id))
        return work_id

    def add_retrieval(self, run_id: int, work_id: int, source: str, source_id: str | None, rank: int) -> None:
        with self.db:
            self.db.execute(
                "INSERT OR IGNORE INTO retrievals (run_id, work_id, source, source_id, rank) VALUES (?, ?, ?, ?, ?)",
                (run_id, work_id, source, source_id, rank),
            )

    def add_sample(self, run_id: int, work_ids: list[int], purpose: str) -> None:
        with self.db:
            self.db.executemany(
                "INSERT OR IGNORE INTO samples (run_id, work_id, purpose) VALUES (?, ?, ?)",
                [(run_id, w, purpose) for w in work_ids],
            )

    def set_fulltext(self, work_id: int, status: str, source: str | None = None, url: str | None = None,
                     license: str | None = None, version: str | None = None, file_path: str | None = None,
                     file_sha256: str | None = None, parser: str | None = None,
                     sections: list[dict] | None = None, error: str | None = None) -> None:
        text = json.dumps(sections, ensure_ascii=False) if sections is not None else None
        chars = sum(len(s["text"]) for s in sections) if sections else None
        with self.db:
            self.db.execute(
                """INSERT OR REPLACE INTO fulltexts (work_id, status, source, url, license, version, file_path,
                       file_sha256, parser, sections, chars, error, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (work_id, status, source, url, license, version, file_path, file_sha256, parser, text, chars,
                 error, now()),
            )

    def add_findings_read(self, run_id: int | None, work_id: int, extractor: str, read_from: str, findings: int,
                          call_id: int | None = None, at: str | None = None) -> None:
        with self.db:
            self.db.execute("INSERT INTO findings_reads (run_id, work_id, extractor, read_from, findings, call_id, "
                            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                            (run_id, work_id, extractor, read_from, findings, call_id, at or now()))

    def replace_findings(self, run_id: int | None, work_id: int, extractor: str, rows: list[dict],
                         call_id: int | None = None) -> list[int]:
        """A study's findings from one extractor in one run; that extractor's earlier ones in the run are marked
        superseded (another corpus's findings on the same record stay as they are)."""
        ids = []
        with self.db:
            # Kept, not deleted: earlier manuscripts cite them and human checks refer to them.
            self.db.execute("UPDATE findings SET status = 'superseded' WHERE work_id = ? AND extractor = ? "
                            "AND run_id IS ? AND status != 'superseded'", (work_id, extractor, run_id))
            for r in rows:
                cur = self.db.execute(
                    """INSERT INTO findings (work_id, run_id, extractor, finding, effect, for_whom, fields, evidence,
                                             location, source, grounded, call_id, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (work_id, run_id, extractor, r["finding"], r.get("effect") or None, r.get("for_whom") or None,
                     json.dumps(r.get("fields") or {}, ensure_ascii=False), r["evidence"], r.get("location") or None,
                     r.get("source"), _flag(r.get("grounded")), call_id, now()))
                ids.append(cur.lastrowid)
        return ids

    def get_fulltext(self, work_id: int):
        return self.db.execute("SELECT * FROM fulltexts WHERE work_id = ?", (work_id,)).fetchone()

    def add_score(self, run_id: int, work_id: int, screener: str, metric: str, score: int,
                  evidence: str | None, grounded: bool | None, call_id: int | None = None) -> None:
        with self.db:
            self.db.execute(
                """INSERT OR REPLACE INTO screening_scores
                   (run_id, work_id, screener, metric, score, evidence, grounded, call_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (run_id, work_id, screener, metric, score, evidence, _flag(grounded), call_id),
            )

    def add_feature(self, run_id: int | None, work_id: int, extractor: str, dimension: str, value: str,
                    canonical: str, evidence: str | None, grounded: bool | None, call_id: int | None = None) -> None:
        with self.db:
            self.db.execute(
                """INSERT OR REPLACE INTO study_features
                   (work_id, extractor, dimension, value, canonical, evidence, grounded, run_id, call_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (work_id, extractor, dimension, value, canonical, evidence, _flag(grounded), run_id, call_id, now()),
            )

    def add_claim_link(self, run_id: int | None, work_id: int, extractor: str, claim: str, relation: str,
                       evidence: str | None, grounded: bool | None, reason: str | None,
                       call_id: int | None = None) -> None:
        with self.db:
            self.db.execute(
                """INSERT OR REPLACE INTO claim_links
                   (work_id, extractor, claim, relation, evidence, grounded, reason, run_id, call_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (work_id, extractor, claim, relation, evidence, _flag(grounded), reason, run_id, call_id, now()),
            )

    def add_screening(self, run_id: int | None, work_id: int, corpus: str, stage: str, screener: str,
                      decision: str, reason: str | None = None, note: str | None = None,
                      call_id: int | None = None) -> None:
        with self.db:
            self.db.execute(
                """INSERT INTO screening (run_id, work_id, corpus, stage, screener, decision, reason, note,
                                          call_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT (run_id, work_id, stage, screener) DO UPDATE SET
                     decision = excluded.decision, reason = excluded.reason, note = excluded.note,
                     call_id = excluded.call_id, created_at = excluded.created_at""",
                (run_id, work_id, corpus, stage, screener, decision, reason, note, call_id, now()),
            )
