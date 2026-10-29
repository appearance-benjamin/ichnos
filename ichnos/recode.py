"""Recoding existing findings on the protocol's [findings] fields, keeping their ids.

When the protocol gains a field (a predictor, the sign of a relation) or a coding turns out to be wrong
(grief coded as work centrality because work centrality predicted it), the findings are not extracted
again: their ids are cited by manuscripts and by human checks. An agent recodes the fields of each
finding from its statement and passage; every change is kept in `finding_recodes` with the old value,
and the finding's text and passage stay untouched.

A second coder (`--second-coder`) codes the same fields blind (the batch does not show the current values); its
codes go to `finding_second_codings`, never to the findings, and `field_agreement` compares the two.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import tables
from .store import Store, now

KINDS = ("recode",)
SCHEMA = """
CREATE TABLE IF NOT EXISTS finding_second_codings (
    finding_id  INTEGER NOT NULL REFERENCES findings(finding_id),
    coder       TEXT NOT NULL,
    field       TEXT NOT NULL,
    value       TEXT,
    call_id     INTEGER REFERENCES calls(call_id),
    created_at  TEXT NOT NULL,
    PRIMARY KEY (finding_id, coder, field)
);
CREATE TABLE IF NOT EXISTS finding_recodes (
    recode_id   INTEGER PRIMARY KEY,
    finding_id  INTEGER NOT NULL REFERENCES findings(finding_id),
    field       TEXT NOT NULL,
    old         TEXT,
    new         TEXT,
    reason      TEXT,
    recoder     TEXT NOT NULL,
    call_id     INTEGER REFERENCES calls(call_id),
    created_at  TEXT NOT NULL
);
"""


def ensure(store: Store) -> None:
    store.db.executescript(SCHEMA)


def only_fields(protocol: dict, names: list[str] | None) -> dict:
    """The protocol with only these findings fields, for a recoding of some fields; the others stay as they are."""
    if not names:
        return protocol
    fields = (protocol.get("findings") or {}).get("fields") or []
    unknown = sorted(set(names) - {f["name"] for f in fields})
    if unknown:
        raise ValueError(f"no findings field {unknown} in the protocol")
    return dict(protocol, findings=dict(protocol["findings"], fields=[f for f in fields if f["name"] in names]))


def instructions(protocol: dict) -> str:
    spec = protocol.get("findings") or {}
    fields = spec.get("fields") or []
    lines = ["# Recoding task: the fields of existing findings", "",
             "Each batch_NN.json lists findings already extracted from the studies of a review, each with its "
             "statement (`finding`), effect, subgroup (`for_whom`), the verbatim `passage` it rests on, the study's "
             "title, and its current field values (`fields`). Some fields are new and empty; some current values "
             "may be wrong.", "",
             "Code every field below for every finding, from its statement and passage only. Keep a current value "
             "when it is right; change it when it is wrong, and then give a `reason` of at most 15 words.", ""]
    for f in fields:
        cats = f.get("categories")
        lines.append(f"- `{f['name']}`: {' '.join(f['question'].split())}"
                     + (f" One of: {', '.join(cats)} (several separated by '; ' only if the finding really "
                        "covers several)." if cats else " Free text, a few words; empty if none.")
                     + "".join(f"\n  - {' '.join(str(x).split())}" for x in f.get("definitions") or []))
    lines += ["", "Rules:",
              "- The outcome is what is predicted or what changes; the predictor is what it is related to. In "
              "'X went with more Y', Y is the outcome and X the predictor, whatever the study is about.",
              "- The sign is about the relation between that predictor and that outcome, as the passage reports it.",
              "- Do not rewrite statements or passages, and use nothing but the batch.", "",
              "Output: one JSON object per batch, written to batch_NN_result.json:", "",
              "```json",
              json.dumps({"findings": [{"id": 1, "fields": {f["name"]: "..." for f in fields},
                                        "changed": ["outcome"], "reason": "grief is the outcome, centrality predicts it"}]},
                         ensure_ascii=False),
              "```", "", "Every finding id of the batch exactly once."]
    return "\n".join(lines) + "\n"


def write_task(store: Store, run_id: int, kind: str, agent: str, protocol: dict, sha: str, root: Path,
               options: dict, batch_size: int = 40) -> Path:
    from .agents import _new_folder
    ensure(store)
    names = [d.strip() for d in str(options.get("dims") or "").split(",") if d.strip()]
    protocol = only_fields(protocol, names)
    corpus = store.db.execute("SELECT corpus FROM runs WHERE run_id = ?", (run_id,)).fetchone()["corpus"]
    rows = tables.finding_rows(store, run_id)
    wanted = set(options.get("findings") or [])
    rows = [r for r in rows if not wanted or r["finding_id"] in wanted]
    if not rows:
        raise ValueError("no findings to recode")
    meta = tables.works(store, {r["work_id"] for r in rows})
    second = bool(options.get("second_coder"))
    hide = set(names) if second else set()          # a second coder codes blind: the current values are not shown
    items = [{"id": r["finding_id"], "work_id": r["work_id"], "study": meta[r["work_id"]]["cite"],
              "title": meta[r["work_id"]]["title"], "finding": r["finding"], "effect": r["effect"],
              "for_whom": r["for_whom"], "passage": r["evidence"],
              "fields": {k: v for k, v in json.loads(r["fields"] or "{}").items() if k not in hide}}
             for r in sorted(rows, key=lambda r: r["finding_id"])]
    out = _new_folder(root, corpus, run_id, kind, agent)
    batches = [items[i:i + batch_size] for i in range(0, len(items), batch_size)]
    for n, b in enumerate(batches, 1):
        (out / f"batch_{n:02d}.json").write_text(json.dumps(b, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "instructions.md").write_text(instructions(protocol), encoding="utf-8")
    (out / "task.json").write_text(json.dumps({"kind": kind, "agent": agent, "run_id": run_id, "corpus": corpus,
                                               "protocol_sha256": sha, "batches": len(batches),
                                               "records": len(items), **({"fields": names} if names else {}),
                                               **({"second_coder": True} if second else {})},
                                              indent=1), encoding="utf-8")
    return out


def field_agreement(store: Store, run_id: int, names: list[str], main=None,
                    works: set[int] | None = None) -> dict[str, dict]:
    """Per findings field: Cohen's kappa and raw agreement between the findings' codes and a blind second coder's, on
    the findings both coded. `main` picks one value of a multi-valued code (default: the first). `works`: only the
    findings of these studies (a paper's study set)."""
    from .agreement import cohen_kappa
    ensure(store)
    pick = main or (lambda name, v: str(v or "").split(";")[0].strip())
    rows = {r["finding_id"]: json.loads(r["fields"] or "{}") for r in tables.finding_rows(store, run_id)
            if works is None or r["work_id"] in works}
    out = {}
    for name in names:
        pairs = []
        for r in store.db.execute("SELECT finding_id, value FROM finding_second_codings WHERE field = ?", (name,)):
            if r["finding_id"] in rows and name in rows[r["finding_id"]]:
                pairs.append((pick(name, rows[r["finding_id"]][name]), pick(name, r["value"])))
        if pairs:
            k = cohen_kappa(pairs)
            out[name] = {"n": len(pairs), "agree": sum(a == b for a, b in pairs),
                         "kappa": None if k is None else round(k, 2)}
    return out


def check(batch: list[dict], answer, protocol: dict) -> list[str]:
    from .agents import _check_category
    if not isinstance(answer, dict) or not isinstance(answer.get("findings"), list):
        return ["the answer must be {\"findings\": [...]}"]
    fields = (protocol.get("findings") or {}).get("fields") or []
    want = [f["id"] for f in batch]
    got = [a.get("id") for a in answer["findings"]]
    errs = []
    if sorted(got) != sorted(want):
        errs.append(f"every finding id exactly once (missing {sorted(set(want) - set(got))[:8]}, "
                    f"unknown {sorted(set(got) - set(want))[:8]})")
    for a in answer["findings"]:
        values = a.get("fields") or {}
        for f in fields:
            if f["name"] not in values:
                errs.append(f"finding {a.get('id')}: field {f['name']} is missing")
                continue
            errs += _check_category(f"finding {a.get('id')}", f, str(values[f["name"]]).strip().lower())
    return errs


def import_task(folder: Path, store: Store, meta: dict, protocol: dict, instructions_text: str) -> dict:
    from .agents import _strip_fences, batch_files
    ensure(store)
    protocol = only_fields(protocol, meta.get("fields"))
    fields = [f["name"] for f in (protocol.get("findings") or {}).get("fields") or []]
    problems, answers = [], []
    for batch in batch_files(folder):
        result = batch.with_name(batch.stem + "_result.json")
        if not result.exists():
            problems.append(f"{result.name} is missing")
            continue
        data = json.loads(batch.read_text(encoding="utf-8"))
        raw = result.read_text(encoding="utf-8")
        try:
            answer = json.loads(_strip_fences(raw))
        except ValueError as err:
            problems.append(f"{result.name}: not valid JSON ({err})")
            continue
        errs = check(data, answer, protocol)
        if errs:
            problems += [f"{result.name}: {e}" for e in errs]
            continue
        call_id = store.log_call(meta["run_id"], "llm", meta["agent"], f"{instructions_text}\n\n[recode batch, sha256 "
                                 f"{hashlib.sha256(batch.read_bytes()).hexdigest()}]", 200, 0, raw.encode("utf-8"),
                                 {"agent": meta["agent"], "task": "recode"})
        answers.append((answer, call_id))
    if problems:
        return {"stored": 0, "problems": problems}
    if meta.get("second_coder"):
        with store.db:
            for answer, call_id in answers:
                for a in answer["findings"]:
                    for name in fields:
                        store.db.execute("INSERT OR REPLACE INTO finding_second_codings (finding_id, coder, field, value, "
                                         "call_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                                         (a["id"], meta["agent"], name, str(a["fields"][name]).strip().lower(), call_id,
                                          now()))
        return {"stored": sum(len(a["findings"]) for a, _ in answers), "second_coder": True, "problems": []}
    changed = 0
    with store.db:
        for answer, call_id in answers:
            for a in answer["findings"]:
                row = store.db.execute("SELECT fields FROM findings WHERE finding_id = ?", (a["id"],)).fetchone()
                old = json.loads(row["fields"] or "{}")
                new = dict(old)
                for name in fields:
                    value = str(a["fields"][name]).strip()
                    value = value.lower() if any(f["name"] == name and f.get("categories") for f in
                                                 protocol["findings"]["fields"]) else value
                    if str(old.get(name, "")).strip() != value:
                        store.db.execute(
                            "INSERT INTO finding_recodes (finding_id, field, old, new, reason, recoder, call_id, "
                            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                            (a["id"], name, old.get(name), value, (a.get("reason") or None) if name in
                             (a.get("changed") or []) else None, meta["agent"], call_id, now()))
                        changed += 1
                    new[name] = value
                store.db.execute("UPDATE findings SET fields = ? WHERE finding_id = ?",
                                 (json.dumps(new, ensure_ascii=False), a["id"]))
    return {"stored": sum(len(a["findings"]) for a, _ in answers), "changed": changed, "problems": []}
