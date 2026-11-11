"""Unattended validation benchmarks: a plan file names the benchmarks and the screeners, one command does the rest.

For each benchmark in the plan, every step is done only if it is not done yet, so the same command resumes after a
stop, a full disk or a server that went away:

  1. labels   the labelled set; built from a SYNERGY ids file with OpenAlex if only that is given (from_ids)
  2. import   the benchmark run, with the corpus protocol frozen by its hash (import_labels)
  3. abstracts  missing abstracts looked up once (abstracts.fill; records already looked up are skipped)
  4. screen   per screener: the task written, its batches answered by the local model (runner.run_task), the
              answers checked and stored (import_task)
  5. score    once every screener is stored: the report in output/benchmarks/<name>.md and the summary table

What the plan cannot do, by design: write a benchmark's protocol. The criteria must be read in the review and frozen
by a person before any model sees a record (see docs/design-validation-and-forecasting.md); a benchmark whose
protocol file is missing is skipped with that reason.

The labels never reach the models: the batches are written from the records alone, as for any benchmark.
"""

from __future__ import annotations

import hashlib
import json
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import benchmark
from .config import ROOT
from .store import Store, now


@dataclass
class Screener:
    label: str                  # stored as model:<label>; never a Claude name for a local model
    model: str                  # the model's name on the server


@dataclass
class Plan:
    endpoint: str
    screeners: list[Screener]
    benchmarks: list[dict]
    tag: str                    # appended to each benchmark name: one set of labels, many screener pairs
    batch_size: int = 10
    parallel: int = 1
    timeout: int = 900
    retries: int = 2
    max_passes: int = 3         # rounds over failed batches in one command
    think: bool = True          # let reasoning models think before answering (slow on CPU)
    path: Path | None = None
    sha: str = ""
    options: dict = field(default_factory=dict)


def load_plan(path: Path) -> Plan:
    raw = path.read_bytes()
    cfg = tomllib.loads(raw.decode("utf-8"))
    screeners = [Screener(s["label"], s["model"]) for s in cfg.get("screeners", [])]
    if not screeners:
        raise ValueError(f"{path}: no [[screeners]]")
    for s in screeners:
        if any(w in s.label.lower() for w in ("claude", "opus", "sonnet", "haiku")):
            raise ValueError(f"screener {s.label}: a local model cannot be stored under a Claude name")
    if len({s.label for s in screeners}) != len(screeners):
        raise ValueError(f"{path}: two screeners share a label")
    benches = cfg.get("benchmarks", [])
    for b in benches:
        for key in ("name", "corpus"):
            if not b.get(key):
                raise ValueError(f"{path}: a benchmark needs `{key}`")
        if not b.get("labels") and not b.get("ids"):
            raise ValueError(f"benchmark {b['name']}: needs `labels` (a labelled CSV) or `ids` (a SYNERGY ids file)")
    return Plan(endpoint=cfg.get("endpoint", "http://localhost:11434/v1"), screeners=screeners, benchmarks=benches,
                tag=cfg.get("tag") or "-".join(s.label for s in screeners), batch_size=cfg.get("batch_size", 10),
                parallel=cfg.get("parallel", 1), timeout=cfg.get("timeout", 900), retries=cfg.get("retries", 2),
                max_passes=cfg.get("max_passes", 3), think=cfg.get("think", True), path=path, sha=hashlib.sha256(raw).hexdigest())


def bench_name(plan: Plan, b: dict) -> str:
    return f"{b['name']}--{plan.tag}"


class Log:
    def __init__(self, path: Path, echo=print):
        self.path, self.echo = path, echo
        path.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, msg: str) -> None:
        line = f"{now()}  {msg}"
        self.echo(line)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")


def _protocol_path(corpus: str, root: Path) -> Path:
    return root / "protocol" / f"corpus_{corpus.lower()}.toml"


def _sheet(store: Store, run_id: int) -> list[int]:
    return [r[0] for r in store.db.execute(
        "SELECT DISTINCT work_id FROM samples WHERE run_id = ? AND purpose = 'pilot_screening'", (run_id,))]


def run_plan(plan: Plan, store: Store, settings, root: Path = ROOT, only: str | None = None,
             dry_run: bool = False, echo=print, deps=None) -> list[dict]:
    """One pass over the plan. Returns one status row per benchmark. `deps` replaces the network steps in tests."""
    from . import agents, runner
    deps = deps or {}
    log = Log(root / "output" / "benchmarks" / "runner.log", echo)
    log(f"plan {plan.path} (sha256 {plan.sha[:12]}), tag {plan.tag}, endpoint {plan.endpoint}, think {plan.think}, "
        f"screeners {', '.join(f'{s.label}={s.model}' for s in plan.screeners)}")
    rows = []
    for b in plan.benchmarks:
        if only and b["name"] != only:
            continue
        name = bench_name(plan, b)
        row = {"bench": name, "corpus": b["corpus"], "state": "", "note": ""}
        rows.append(row)
        if not _protocol_path(b["corpus"], root).exists():
            row.update(state="skipped", note=f"no protocol/corpus_{b['corpus'].lower()}.toml: a person writes it "
                                             f"from the review's criteria first")
            log(f"{name}: {row['note']}")
            continue
        try:
            row.update(_one(plan, b, name, store, settings, root, dry_run, log, deps, agents, runner))
        except Exception as err:            # one benchmark's failure must not stop the others
            row.update(state="error", note=f"{type(err).__name__}: {err}")
            log(f"{name}: ERROR {row['note']}")
    if not dry_run:
        write_summary(store, root)
    return rows


def _one(plan, b, name, store, settings, root, dry_run, log, deps, agents, runner) -> dict:
    # 1. labels
    labels = Path(b["labels"]) if b.get("labels") else root / "data" / "benchmarks" / "labelled" / f"{b['name']}.csv"
    labels = labels if labels.is_absolute() else root / labels
    if not labels.exists():
        ids = Path(b["ids"]) if b.get("ids") else None
        if ids is None:
            raise FileNotFoundError(labels)
        ids = ids if ids.is_absolute() else root / ids
        if dry_run:
            return {"state": "would build labels", "note": f"{ids} -> {labels}"}
        log(f"{name}: labels from {ids.name} with OpenAlex")
        make = deps.get("from_ids") or (lambda i, o: benchmark.from_ids(
            i, o, _http(store), settings.openalex_api_key, settings.contact_email))
        r = make(ids, labels)
        log(f"{name}: {r['written']} of {r['rows']} records, {r['included']} included; left out without a title: "
            f"{r['lost']['include']} included, {r['lost']['exclude']} excluded")

    # 2. import
    row = store.db.execute("SELECT run_id FROM bench_sets WHERE bench = ?", (name,)).fetchone() \
        if _has_bench(store) else None
    if row is None:
        if dry_run:
            return {"state": "would import", "note": str(labels)}
        r = benchmark.import_labels(store, b["corpus"], labels, name, b.get("source"), root)
        log(f"{name}: imported as run {r['run_id']}, {r['records']} records, {r['included']} included")
        run_id = r["run_id"]
    else:
        run_id = row["run_id"]

    # 3. abstracts, once per run (records already looked up are skipped by fill itself)
    marker = root / "output" / "benchmarks" / "state" / f"{name}.abstracts"
    if not marker.exists() and not dry_run:
        bare = [w for w in _sheet(store, run_id) if not (store.db.execute(
            "SELECT abstract FROM works WHERE work_id = ?", (w,)).fetchone()[0] or "").strip()]
        if bare and b.get("fill_abstracts", True):
            log(f"{name}: looking up {len(bare)} missing abstracts")
            fill = deps.get("fill") or (lambda run, works: _fill(store, run, works, settings))
            fill(run_id, bare)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(now(), encoding="utf-8")

    # 4. screen, per screener
    corpus = store.db.execute("SELECT corpus FROM runs WHERE run_id = ?", (run_id,)).fetchone()[0]
    states = {}
    for s in plan.screeners:
        agent = f"model:{s.label}"
        folder = agents.task_dir(root, corpus, run_id, "screen", agent)
        if not (folder / "task.json").exists():
            if dry_run:
                states[s.label] = "would write"
                continue
            folder = agents.write_tasks(store, run_id, "screen", agent, root=root, batch_size=plan.batch_size)
            log(f"{name}: task for {s.label} written ({folder})")
        state = agents.task_state(folder)
        if dry_run:
            states[s.label] = state
            continue
        passes = 0
        while state in ("written", "partly answered") and passes < plan.max_passes:
            passes += 1
            ask = deps.get("run_task") or runner.run_task
            counts = ask(folder, store, runner.RunSettings(plan.endpoint, s.model, timeout=plan.timeout,
                                                           retries=plan.retries, api_key=settings.llm_api_key,
                                                           think=plan.think),
                         parallel=plan.parallel)
            log(f"{name}: {s.label} pass {passes}: answered {counts['answered']}, failed {counts['failed']}, "
                f"before {counts['already']}")
            state = agents.task_state(folder)
        if state == "answered":
            counts = agents.import_task(folder, store)
            if counts["problems"]:
                log(f"{name}: {s.label} import refused: {counts['problems'][:3]}")
            else:
                log(f"{name}: {s.label} stored {counts['stored']} answers; "
                    f"{counts.get('grounded', 0)} of {counts.get('evidence', 0)} quotes verbatim")
            state = agents.task_state(folder)
        states[s.label] = state
    if dry_run:
        return {"state": "dry run", "note": ", ".join(f"{k}: {v}" for k, v in states.items()), "run_id": run_id}
    if not all(v == "imported" for v in states.values()):
        return {"state": "screening", "note": ", ".join(f"{k}: {v}" for k, v in states.items()), "run_id": run_id}

    # 5. score
    path = benchmark.write_report(store, name, root)
    res = benchmark.score(store, name)
    c = res["combined"]
    log(f"{name}: {res['verdict']}; combined recall {c['recall']:.3f} ({path})")
    return {"state": "scored", "note": res["verdict"], "run_id": run_id, "report": str(path)}


def _has_bench(store: Store) -> bool:
    return store.db.execute("SELECT 1 FROM sqlite_master WHERE name = 'bench_sets'").fetchone() is not None


def _http(store):
    from .http import Http
    return Http(store, None, max_retries=4)


def _fill(store, run_id, works, settings):
    from . import abstracts
    return abstracts.fill(store, run_id, works, contact_email=settings.contact_email,
                          openalex_key=settings.openalex_api_key, s2_key=settings.s2_api_key,
                          core_key=settings.core_api_key, log=lambda *a, **k: None)


def write_summary(store: Store, root: Path = ROOT) -> Path:
    """One table over every benchmark in the store, scored now: output/benchmarks/summary.md and .json."""
    if not _has_bench(store):
        return root / "output" / "benchmarks" / "summary.md"
    rows = []
    for (name,) in store.db.execute("SELECT bench FROM bench_sets WHERE kind = 'screening' ORDER BY created_at"):
        res = benchmark.score(store, name)
        c = res.get("combined") or {}
        screened = res.get("screened", 0)
        kept = (c.get("tp", 0) + c.get("fp", 0)) if c else 0
        ci = c.get("recall_ci") or (None, None)
        rows.append({"bench": name, "records": res.get("records"), "included": res.get("included"),
                     "screened": screened, "screeners": sorted(res.get("screeners", {})),
                     "recall": c.get("recall"), "recall_ci": ci, "kept": kept,
                     "kept_share": kept / screened if screened else None, "missed": len(res.get("missed", [])),
                     "verdict": res.get("verdict")})
    out = root / "output" / "benchmarks"
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(json.dumps({"at": now(), "rows": rows}, indent=1, default=str), encoding="utf-8")
    lines = [f"# Validation benchmarks: summary ({now()[:16]})", "",
             "| Benchmark | Screeners | Included | Recall [95% CI] | Kept for full text | Missed | Verdict |",
             "| --- | --- | ---: | --- | ---: | ---: | --- |"]
    for r in rows:
        rec = "–" if r["recall"] is None else (f"{r['recall']:.1%}" + (
            f" [{r['recall_ci'][0]:.1%}, {r['recall_ci'][1]:.1%}]" if r["recall_ci"] and r["recall_ci"][0] is not None
            else ""))
        share = "–" if r["kept_share"] is None else f"{r['kept']} ({r['kept_share']:.0%})"
        lines.append(f"| {r['bench']} | {', '.join(s.removeprefix('model:') for s in r['screeners']) or '–'} | "
                     f"{r['included']} | {rec} | {share} | {r['missed']} | {r['verdict']} |")
    (out / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out / "summary.md"


def run_until_done(plan: Plan, store: Store, settings, root: Path = ROOT, wait: int = 600, max_rounds: int = 48,
                   echo=print) -> list[dict]:
    """Passes over the plan until every benchmark is scored, skipped or broken, waiting between passes (a server
    that is down, a GPU booking that has not started)."""
    rows = []
    for i in range(max_rounds):
        rows = run_plan(plan, store, settings, root, echo=echo)
        if all(r["state"] in ("scored", "skipped", "error") for r in rows):
            break
        echo(f"{now()}  pass {i + 1}: {sum(r['state'] == 'scored' for r in rows)} of {len(rows)} scored; "
             f"next pass in {wait} s")
        time.sleep(wait)
    return rows
