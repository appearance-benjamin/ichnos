"""Agent harness: local models as tool-using agents for the tasks one prompt cannot hold.

agent-run (runner.py) answers self-contained batches in one prompt. Manuscripts, audits, reviews, revisions and the
other multi-file kinds are read piece by piece, which Claude Code does with its file tools. This harness gives a
local model (Ollama, vLLM: any OpenAI-compatible server with tool calls) the same way of working:

- tools to read the task folder in pieces: json_outline, json_get, read_text, search, list_files (read-only);
- note: what the agent learned; kept when old tool results are dropped to stay inside the context;
- draft_set / draft_append / draft_get: the answer built in parts, so a long answer needs no single huge reply;
- submit: runs the import's own checks (paper, strong, novelty, directions, lenses) and writes
  batch_NN_result.json only when they pass; otherwise the problems go back to the agent.

Several agents run at once (--parallel, one batch each), e.g. two on one GPU. Every step is logged in the task's
harness/ folder and harness.json records model, server, settings and each batch's steps, tokens and time, so a run
can be audited and repeated. Import as usual with agent-import.

    python -m ichnos.harness <task folder> [<task folder> ...] --model qwen3.8:27b-q8_0 --parallel 2 --context 131072
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
import tomllib
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path

from .agents import IMPORTED, ROOT, SELF_CONTAINED, _strip_fences, batch_files
from .runner import OTHER_FAMILIES, _write, clean
from .store import now

LOG = "harness.json"
LOG_DIR = "harness"
BACKOFF = 5.0

PREAMBLE = """You are an agent answering one batch of an Ichnos research task. The task's own instructions follow \
below the line; they say what to judge or write and the exact JSON to give.

The batch file is too large to read at once, so you work with tools:
1. Start with json_outline on the batch file to see its structure. Then read what you need with json_get: use a \
path (e.g. `sentences[3]`, `tables.T2`), `start`/`count` to read long lists in pieces, and `fields` to see only some \
keys of each item. read_text and search work on any file of the folder. Never read a large file front to back with \
read_text: the batch can be many times larger than your whole context. Read the parts the task needs, and search \
for the rest when you need it.
2. Write down what you learn with note. When the conversation gets long, old tool results are removed; your notes \
and your draft are kept.
3. Build the answer in parts with draft_set and draft_append (for long lists, append 10 to 30 items at a time as \
you go) and look at it with draft_get. Start the draft early and add to it as you read: a part in the draft is safe, \
a part only in your head is lost when old tool results are removed. When it is complete, call submit. submit runs the same checks as the \
import; if it lists problems, fix them in the draft and submit again.
4. Never invent ids, quotes, numbers or sources: everything must come from the files. Copy quotes exactly.
5. Only what you submit is stored. Do not write the answer as plain text.
"""


def tool(name: str, description: str, properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "function", "function": {"name": name, "description": description, "parameters": {
        "type": "object", "properties": properties, "required": required or []}}}


FILE = {"type": "string", "description": "a file name from list_files, e.g. batch_01.json"}
TOOLS = [
    tool("list_files", "The task folder's files with their sizes.", {}),
    tool("json_outline", "The structure of a JSON file at a path: keys, types, list lengths, sizes and one example "
         "item. Use it before json_get.", {"file": FILE, "path": {"type": "string", "description":
                                           "e.g. '' (the whole file), 'sentences', 'findings[0]', 'tables.T2'"}},
         ["file"]),
    tool("json_get", "The value at a path of a JSON file. For a list, give start and count to read it in pieces, and "
         "fields to keep only some keys of each item.", {
             "file": FILE, "path": {"type": "string"}, "start": {"type": "integer"}, "count": {"type": "integer"},
             "fields": {"type": "array", "items": {"type": "string"}}}, ["file"]),
    tool("read_text", "Characters of a file from offset, for Markdown and other text.", {
        "file": FILE, "offset": {"type": "integer"}, "length": {"type": "integer"}}, ["file"]),
    tool("search", "Where a text occurs in a file (case-insensitive), with the text around each hit and its "
         "offset for read_text.", {"file": FILE, "query": {"type": "string"},
                                   "max_hits": {"type": "integer"}}, ["file", "query"]),
    tool("note", "Keep a note for later. Notes survive when old tool results are removed.", {
        "text": {"type": "string"}}, ["text"]),
    tool("draft_set", "Set one top-level key of the answer to a JSON value (object, list, string, number).", {
        "key": {"type": "string"}, "value": {"description": "any JSON value"}}, ["key", "value"]),
    tool("draft_append", "Append items to a list under a top-level key of the answer (the list is created if "
         "needed).", {"key": {"type": "string"}, "items": {"type": "array", "items": {}}}, ["key", "items"]),
    tool("draft_get", "Without key: the draft's keys and sizes. With key: that part of the draft.", {
        "key": {"type": "string"}}),
    tool("submit", "Check the answer and store it if it passes. Without `answer`, the draft is submitted.", {
        "answer": {"description": "optional: the whole answer as a JSON object, instead of the draft"}}),
]


@dataclass(frozen=True)
class Settings:
    endpoint: str
    model: str
    context: int = 32768            # the server's context length in tokens (Ollama: OLLAMA_CONTEXT_LENGTH)
    temperature: float = 0.0
    seed: int | None = 1
    think: bool = True              # let reasoning models think before each step
    max_tokens: int = 8192          # per step: long answers go into the draft in parts; stops a reply that loops
    max_steps: int = 120
    max_submits: int = 6
    timeout: int = 900
    api_key: str | None = None

    @property
    def tool_chars(self) -> int:
        """The most characters one tool result may hold: about a sixteenth of the context."""
        return max(6000, min(26000, self.context * 4 // 20))


class HarnessError(Exception):
    """Stops the whole run: no server, or a task the harness cannot do."""


# ---------------------------------------------------------------------------------------------------------- checks

def checker(folder: Path, meta: dict):
    """The import's checks for this kind, as f(batch data, answer) -> problems. Kinds without a pure check get only
    the JSON check; agent-import still checks them in full."""
    kind = meta["kind"]
    from . import directions, insights, lenses, novelty, paper, strong

    def run(data, answer) -> list[str]:
        if kind in paper.KINDS:
            errs = paper._check(kind, answer, data)
            if kind in ("paper", "revise") and isinstance(answer, dict):
                errs += insights.check(answer.get("insights") or [], {int(w) for w in data["studies"]},
                                       {f["id"] for f in data["findings"]})
            return errs
        if kind in strong.KINDS:
            return strong._check(kind, data, answer)
        if kind in novelty.KINDS:
            return novelty._check(kind, data, answer)
        if kind in directions.KINDS:
            return directions.check(answer, data)
        if kind in lenses.KINDS and meta.get("lens_file"):
            cfg = tomllib.loads((ROOT / meta["lens_file"]).read_text(encoding="utf-8"))
            return lenses.check_answer(kind, meta, data, answer, cfg)
        return []

    def safe(data, answer) -> list[str]:
        if not isinstance(answer, (dict, list)):
            return ["the answer must be a JSON object"]
        try:
            return run(data, answer)
        except (KeyError, TypeError, ValueError, AttributeError, IndexError) as err:
            return [f"the answer does not have the expected shape ({type(err).__name__}: {err})"]
    return safe


# ----------------------------------------------------------------------------------------------------------- tools

PATH_PART = re.compile(r"\.?([^.\[\]]+)|\[(-?\d+)\]")


def at_path(value, path: str):
    for key, index in PATH_PART.findall(path or ""):
        if index:
            value = value[int(index)]
        elif isinstance(value, dict):
            if key not in value:
                raise KeyError(f"no key {key!r} here; keys: {', '.join(list(value)[:40])}")
            value = value[key]
        elif isinstance(value, list) and key.lstrip("-").isdigit():
            value = value[int(key)]
        else:
            raise KeyError(f"cannot take {key!r} of a {type(value).__name__}")
    return value


def size(value) -> int:
    return len(json.dumps(value, ensure_ascii=False))


def describe(value) -> str:
    if isinstance(value, dict):
        return f"object, {len(value)} keys, {size(value):,} chars"
    if isinstance(value, list):
        return f"list of {len(value)}, {size(value):,} chars"
    if isinstance(value, str):
        return f"text, {len(value):,} chars"
    return f"{type(value).__name__}: {value!r}"


class Workspace:
    """One agent's view of one batch: the folder's files (read-only), its notes and its draft."""

    def __init__(self, folder: Path, batch: Path, s: Settings, check, data):
        self.folder, self.batch, self.s, self.check, self.data = folder, batch, s, check, data
        self.notes: list[str] = []
        self.draft: dict = {}
        self.submits = 0
        self.done = False
        self.problems: list[str] = []
        self._json: dict[str, object] = {batch.name: data}

    def files(self) -> list[Path]:
        return sorted(p for p in self.folder.iterdir() if p.is_file() and not p.name.endswith(".tmp"))

    def _file(self, name: str) -> Path:
        path = self.folder / Path(str(name)).name
        if not path.is_file():
            raise KeyError(f"no file {name!r}; files: {', '.join(p.name for p in self.files())}")
        return path

    def _load(self, name: str):
        path = self._file(name)
        if path.name not in self._json:
            self._json[path.name] = json.loads(path.read_text(encoding="utf-8"))
        return self._json[path.name]

    def _cap(self, text: str, hint: str) -> str:
        limit = self.s.tool_chars
        if len(text) <= limit:
            return text
        return text[:limit] + f"\n[cut at {limit:,} of {len(text):,} chars: {hint}]"

    # each tool returns text for the model
    def list_files(self) -> str:
        return "\n".join(f"{p.name}: {p.stat().st_size:,} bytes (about {p.stat().st_size // 4:,} tokens)"
                         for p in self.files())

    def json_outline(self, file: str, path: str = "") -> str:
        value = at_path(self._load(file), path)
        lines = [f"{path or file}: {describe(value)}"]
        if isinstance(value, dict):
            lines += [f"  {k}: {describe(v)}" for k, v in list(value.items())[:80]]
            if len(value) > 80:
                lines.append(f"  ... {len(value) - 80} more keys")
        elif isinstance(value, list) and value:
            first = value[0]
            if isinstance(first, dict):
                lines.append(f"  items are objects with keys: {', '.join(first)}")
                lines.append(f"  an item is about {sum(size(v) for v in value[:20]) // min(20, len(value)):,} chars")
            lines.append("  first item: " + json.dumps(first, ensure_ascii=False)[:800])
        elif isinstance(value, str):
            lines.append("  begins: " + value[:800])
        return "\n".join(lines)

    def json_get(self, file: str, path: str = "", start: int | None = None, count: int | None = None,
                 fields: list[str] | None = None) -> str:
        value = at_path(self._load(file), path)
        head = ""
        if isinstance(value, list) and (start is not None or count is not None or fields):
            a = max(0, int(start or 0))
            b = len(value) if count is None else min(len(value), a + max(0, int(count)))
            value = value[a:b]
            head = f"items {a} to {b - 1} of {len(at_path(self._load(file), path))}\n"
        if fields:
            keep = set(fields)
            if isinstance(value, list):
                value = [{k: v for k, v in x.items() if k in keep} if isinstance(x, dict) else x for x in value]
            elif isinstance(value, dict):
                value = {k: v for k, v in value.items() if k in keep}
        return self._cap(head + json.dumps(value, ensure_ascii=False),
                         "read fewer items (start/count), fewer fields, or a deeper path")

    def read_text(self, file: str, offset: int = 0, length: int | None = None) -> str:
        text = self._file(file).read_text(encoding="utf-8")
        offset = max(0, int(offset or 0))
        length = min(int(length or self.s.tool_chars), self.s.tool_chars)
        part = text[offset:offset + length]
        rest = len(text) - offset - len(part)
        return part + (f"\n[{rest:,} more chars: read_text with offset {offset + len(part)}]" if rest > 0 else "")

    def search(self, file: str, query: str, max_hits: int = 20) -> str:
        text = self._file(file).read_text(encoding="utf-8")
        low, q = text.lower(), str(query).lower()
        if not q:
            return "empty query"
        hits, i = [], low.find(q)
        while i >= 0 and len(hits) < max(1, min(int(max_hits or 20), 100)):
            hits.append(f"@{i}: ...{text[max(0, i - 150):i + len(q) + 150]}...")
            i = low.find(q, i + len(q))
        total = low.count(q)
        out = f"{total} hits for {query!r}" + (f", first {len(hits)}" if total > len(hits) else "") + "\n"
        return self._cap(out + "\n".join(hits), "give max_hits or a more exact query")

    def note(self, text: str) -> str:
        self.notes.append(str(text))
        return f"noted ({len(self.notes)} notes)"

    def draft_set(self, key: str, value) -> str:
        self.draft[str(key)] = _maybe_json(value)
        return f"draft.{key} = {describe(self.draft[str(key)])}"

    def draft_append(self, key: str, items) -> str:
        items = _maybe_json(items)
        if not isinstance(items, list):
            items = [items]
        current = self.draft.setdefault(str(key), [])
        if not isinstance(current, list):
            raise ValueError(f"draft.{key} is a {type(current).__name__}, not a list: use draft_set")
        current.extend(items)
        return f"draft.{key} now has {len(current)} items"

    def draft_get(self, key: str | None = None) -> str:
        if not key:
            return "draft: " + (", ".join(f"{k} ({describe(v)})" for k, v in self.draft.items()) or "empty")
        return self._cap(json.dumps(self.draft.get(key), ensure_ascii=False), "look at a smaller part")

    def submit(self, answer=None) -> str:
        self.submits += 1
        if answer is None:
            value = self.draft
        elif isinstance(answer, str):
            try:
                value = json.loads(clean(answer))
            except ValueError as err:
                return f"not valid JSON ({err}); nothing stored"
        else:
            value = answer
        problems = self.check(self.data, value)
        if problems:
            self.problems = problems[:20]
            left = self.s.max_submits - self.submits
            return ("Not stored. The import would refuse this answer:\n" + "\n".join(f"- {p}" for p in problems[:20])
                    + f"\nFix these in the draft and submit again ({left} submits left).")
        _write(self.batch.with_name(self.batch.stem + "_result.json"), json.dumps(value, ensure_ascii=False, indent=1))
        self.done, self.problems = True, []
        return "Stored. The batch is answered; you are done."

    def memory(self) -> str:
        """What is kept when old tool results are removed."""
        notes = "\n".join(f"- {n}" for n in self.notes) or "(none)"
        return f"Your notes so far:\n{notes}\n\nYour {self.draft_get()}"

    def call(self, name: str, args: dict) -> str:
        if name not in {t["function"]["name"] for t in TOOLS}:
            return f"no tool {name!r}"
        try:
            return getattr(self, name)(**{k: v for k, v in (args or {}).items() if v is not None})
        except TypeError as err:
            return f"wrong arguments for {name}: {err}"
        except (KeyError, IndexError, ValueError, OSError) as err:
            return f"{name} failed: {err}"


def _maybe_json(value):
    """Models sometimes pass JSON as a string; take it as the value it spells."""
    if isinstance(value, str) and value.strip()[:1] in "[{":
        try:
            return json.loads(value)
        except ValueError:
            pass
    return value


# ---------------------------------------------------------------------------------------------------------- server

def looping(text: str) -> bool:
    """A reply that keeps repeating the same passage (small models do this): its last 80 characters occur five
    times or more in its last 3000."""
    tail = text[-3000:]
    return len(tail) >= 1500 and tail.count(tail[-80:]) >= 5


def read_stream(response, deadline: float) -> dict:
    """An OpenAI-style streamed reply put back together as a non-streamed one. Stops early, with finish_reason
    'loop' or 'time', when the reply repeats itself or the step runs past its deadline."""
    content, reasoning, calls, usage, finish, model = [], [], {}, None, None, None
    seen = 0
    for raw in response:
        line = raw.decode("utf-8", "replace").strip()
        if not line.startswith("data:") or line[5:].strip() == "[DONE]":
            continue
        try:
            j = json.loads(line[5:])
        except ValueError:
            continue
        if j.get("error"):
            raise HarnessError(f"the server reported: {j['error']}")
        model = j.get("model") or model
        usage = j.get("usage") or usage
        for ch in j.get("choices") or []:
            d = ch.get("delta") or {}
            if d.get("content"):
                content.append(d["content"])
            if d.get("reasoning") or d.get("reasoning_content"):
                reasoning.append(d.get("reasoning") or d.get("reasoning_content"))
            for tc in d.get("tool_calls") or []:
                slot = calls.setdefault(tc.get("index", len(calls)), {"id": None, "type": "function",
                                                                       "function": {"name": "", "arguments": ""}})
                slot["id"] = tc.get("id") or slot["id"]
                f = tc.get("function") or {}
                slot["function"]["name"] += f.get("name") or ""
                args = f.get("arguments")
                slot["function"]["arguments"] += json.dumps(args) if isinstance(args, dict) else (args or "")
            finish = ch.get("finish_reason") or finish
        seen += 1
        if seen % 50 == 0:
            if looping("".join(reasoning) + "".join(content)):
                finish = "loop"
                break
            if time.monotonic() > deadline:
                finish = "time"
                break
    message = {"role": "assistant", "content": "".join(content), "reasoning": "".join(reasoning)}
    if calls:
        message["tool_calls"] = [calls[k] for k in sorted(calls)]
    return {"model": model, "usage": usage or {}, "choices": [{"message": message, "finish_reason": finish}]}


def chat(s: Settings, messages: list[dict]) -> dict:
    body = {"model": s.model, "messages": messages, "tools": TOOLS, "temperature": s.temperature, "stream": True,
            "stream_options": {"include_usage": True}, "max_tokens": s.max_tokens}
    if s.seed is not None:
        body["seed"] = s.seed
    if not s.think:
        body["reasoning_effort"] = "none"                              # Ollama
        body["chat_template_kwargs"] = {"enable_thinking": False}      # vLLM (Qwen's chat template)
    headers = {"Content-Type": "application/json", **({"Authorization": f"Bearer {s.api_key}"} if s.api_key else {})}
    request = urllib.request.Request(s.endpoint.rstrip("/") + "/chat/completions",
                                     data=json.dumps(body).encode("utf-8"), headers=headers)
    wait = BACKOFF
    for n in range(3):
        try:
            with urllib.request.urlopen(request, timeout=min(s.timeout, 600)) as response:
                return read_stream(response, time.monotonic() + s.timeout)
        except urllib.error.HTTPError as err:
            detail = err.read().decode("utf-8", "replace")[:300]
            if err.code < 500:
                raise HarnessError(f"the server refused the request (HTTP {err.code}): {detail}") from None
            problem = f"HTTP {err.code}: {detail}"
        except urllib.error.URLError as err:
            if not isinstance(err.reason, TimeoutError):
                raise HarnessError(f"no server answers at {s.endpoint} ({err.reason}): is the tunnel running?") from None
            problem = f"no answer within {s.timeout} s"
        except (TimeoutError, ConnectionError) as err:
            problem = f"{type(err).__name__}: {err}"
        time.sleep(wait)
        wait *= 2
    raise HarnessError(f"the server failed 3 times ({problem})")


def _args(call: dict) -> dict:
    raw = (call.get("function") or {}).get("arguments") or {}
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except ValueError:
        return {}


# ------------------------------------------------------------------------------------------------------- the agent

def run_batch(folder: Path, batch: Path, s: Settings, check, progress=print) -> dict:
    """One agent answers one batch. Returns its entry for harness.json."""
    data = json.loads(batch.read_text(encoding="utf-8"))
    ws = Workspace(folder, batch, s, check, data)
    instructions = (folder / "instructions.md").read_text(encoding="utf-8")
    logdir = folder / LOG_DIR
    logdir.mkdir(exist_ok=True)
    transcript = logdir / f"{batch.stem}.transcript.jsonl"
    tag = f"[{folder.name}/{batch.stem}]"

    # Resume: an earlier attempt that stopped (lost connection, step limit) left its draft and notes behind.
    resumed = ""
    draft_file, notes_file = logdir / f"{batch.stem}.draft.json", logdir / f"{batch.stem}.notes.md"
    if draft_file.exists():
        try:
            ws.draft = json.loads(draft_file.read_text(encoding="utf-8"))
        except ValueError:
            ws.draft = {}
    if notes_file.exists():
        ws.notes = [line[2:] for line in notes_file.read_text(encoding="utf-8").splitlines() if line.startswith("- ")]
    if ws.draft or ws.notes:
        resumed = ("\n\nAn earlier attempt at this batch stopped before it was finished. You continue it: its draft and "
                   f"notes are kept.\n\n{ws.memory()}\n\nLook at the draft with draft_get, read what the draft still "
                   f"needs, and complete it.")
    messages = [{"role": "system", "content": PREAMBLE + "\n---\n\n" + instructions},
                {"role": "user", "content": f"Answer {batch.name} of this task. The folder holds:\n"
                                            f"{ws.list_files()}\n\n" + (resumed.strip() if resumed else
                                            f"Begin with json_outline on {batch.name}.")}]
    if resumed:
        progress(f"{tag} resuming: draft {', '.join(ws.draft) or 'empty'}, {len(ws.notes)} notes")
    entry = {"status": "failed", "steps": 0, "submits": 0, "prompt_tokens": 0, "completion_tokens": 0,
             "largest_context": 0, "compactions": 0, "problems": [], "started": now()}
    started = time.monotonic()

    def log(message: dict) -> None:
        with transcript.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"at": now(), **message}, ensure_ascii=False) + "\n")

    for m in messages:
        log(m)
    try:
        while entry["steps"] < s.max_steps and not ws.done and ws.submits < s.max_submits:
            entry["steps"] += 1
            if estimate(messages) > 0.6 * s.context:              # before sending, not only after the server counted
                compact(messages, ws)
                entry["compactions"] += 1
                if estimate(messages) > 0.8 * s.context:
                    compact(messages, ws, keep=0)
                progress(f"{tag} context about {estimate(messages) / 1000:.0f}k of {s.context / 1000:.0f}k after "
                         f"removing old tool results")
                log({"role": "harness", "content": "compacted before sending"})
            try:
                reply = chat(s, messages)
            except HarnessError as err:
                if "context length" not in str(err) and "maximum context" not in str(err):
                    raise
                compact(messages, ws, keep=0)                     # the server counted more than the estimate
                entry["compactions"] += 1
                log({"role": "harness", "content": f"server refused the length; compacted hard: {str(err)[:200]}"})
                reply = chat(s, messages)
            usage = reply.get("usage") or {}
            entry["prompt_tokens"] += usage.get("prompt_tokens") or 0
            entry["completion_tokens"] += usage.get("completion_tokens") or 0
            entry["largest_context"] = max(entry["largest_context"], usage.get("prompt_tokens") or 0)
            entry["served_model"] = reply.get("model")
            choice = (reply.get("choices") or [{}])[0]
            msg = choice.get("message") or {}
            calls = msg.get("tool_calls") or []
            content = msg.get("content") or ""
            if choice.get("finish_reason") in ("loop", "time") and calls:
                calls = []      # a call written by a reply that was cut short may be incomplete
            said = {"role": "assistant", "content": content}
            if calls:
                said["tool_calls"] = calls
            messages.append(said)
            log({**said, "reasoning": msg.get("reasoning") or msg.get("reasoning_content"), "usage": usage})

            if calls:
                for c in calls:
                    name = (c.get("function") or {}).get("name", "")
                    args = _args(c)
                    result = ws.call(name, args)
                    shown = ", ".join(f"{k}={str(v)[:40]}" for k, v in args.items() if k not in ("value", "items",
                                                                                                  "answer", "text"))
                    progress(f"{tag} step {entry['steps']}: {name}({shown}) "
                             f"| context {(usage.get('prompt_tokens') or 0) / 1000:.1f}k")
                    out = {"role": "tool", "tool_call_id": c.get("id") or name, "name": name, "content": result}
                    messages.append(out)
                    log(out)
                    if ws.done:
                        break
            elif (text := clean(content)) and text[0] in "[{":
                result = ws.submit(content)        # an answer given as text instead of through submit
                progress(f"{tag} step {entry['steps']}: answer as text, submitted: {result.splitlines()[0]}")
                if not ws.done:
                    messages.append({"role": "user", "content": result})
            else:
                nudge = {
                    "length": "Your reply was cut off. Build long answers with draft_set and draft_append in parts.",
                    "loop": "You were repeating yourself, so your reply was stopped. Take the next single step with "
                            "a tool; keep what you found with note.",
                    "time": "That step took too long and was stopped. Take smaller steps: read less at once, and "
                            "append to the draft in groups.",
                }.get(choice.get("finish_reason"), "Continue with the tools. When the answer is complete, call submit.")
                if choice.get("finish_reason") in ("loop", "time"):
                    progress(f"{tag} step {entry['steps']}: reply stopped ({choice['finish_reason']})")
                messages.append({"role": "user", "content": nudge})
                log(messages[-1])

            if (usage.get("prompt_tokens") or 0) > 0.6 * s.context and not ws.done:
                compact(messages, ws)
                entry["compactions"] += 1
                progress(f"{tag} context {usage['prompt_tokens'] / 1000:.0f}k of {s.context / 1000:.0f}k: "
                         f"old tool results removed, notes and draft kept")
                log({"role": "harness", "content": "compacted"})
    except HarnessError as err:
        ws.problems = [str(err)]
        entry["error"] = str(err)
    entry.update(status="answered" if ws.done else "failed", submits=ws.submits, problems=ws.problems,
                 seconds=round(time.monotonic() - started, 1), finished=now())
    if not ws.done and not ws.problems:
        entry["problems"] = [f"no answer passed the checks within {entry['steps']} steps"]
    (logdir / f"{batch.stem}.notes.md").write_text("\n".join(f"- {n}" for n in ws.notes), encoding="utf-8")
    if ws.draft:
        (logdir / f"{batch.stem}.draft.json").write_text(json.dumps(ws.draft, ensure_ascii=False, indent=1),
                                                       encoding="utf-8")
    return entry


MEMORY_HEAD = "Older tool results were removed to save space."


def estimate(messages: list[dict]) -> int:
    """Tokens the messages take, roughly (3.2 characters a token, plus the tool definitions)."""
    chars = sum(len(m.get("content") or "") + len(json.dumps(m.get("tool_calls") or "")) for m in messages)
    return int(chars / 3.2) + 3000


def compact(messages: list[dict], ws: Workspace, keep: int = 2) -> None:
    """Free the context: drop all but the last `keep` tool results, shorten older replies, and replace any earlier
    reminder with one new reminder of the agent's notes and draft (reminders never pile up)."""
    messages[:] = [m for m in messages if not (m["role"] == "user" and (m.get("content") or "").startswith(MEMORY_HEAD))]
    tools = [i for i, m in enumerate(messages) if m["role"] == "tool"]
    for i in tools[:-keep] if keep else tools:
        if not messages[i]["content"].startswith("[removed"):
            messages[i]["content"] = "[removed to save space; call the tool again if you need it]"
    last = max((i for i, m in enumerate(messages) if m["role"] == "assistant"), default=0)
    for i, m in enumerate(messages[2:], 2):
        if m["role"] == "assistant" and i != last:
            m["content"] = (m.get("content") or "")[:600]
    memory = ws.memory()
    if len(memory) > 24000:          # very long notes: keep the newest
        memory = "(older notes cut)\n" + memory[-24000:]
    messages.append({"role": "user", "content": f"{MEMORY_HEAD}\n\n{memory}\n\nContinue from here."})


# --------------------------------------------------------------------------------------------------------- the run

def plan(folders: list[Path], s: Settings, limit: int | None) -> list[tuple[Path, Path, object]]:
    jobs = []
    for folder in folders:
        if (folder / IMPORTED).exists():
            raise HarnessError(f"{folder.name}: already imported")
        meta = json.loads((folder / "task.json").read_text(encoding="utf-8"))
        if meta.get("kind") in SELF_CONTAINED:
            raise HarnessError(f"{folder.name}: a {meta['kind']} batch fits one prompt; run it with "
                               f"`python -m ichnos agent-run` (which also takes --parallel)")
        clash = [f for f in OTHER_FAMILIES if f in meta["agent"].lower() and f not in s.model.lower()]
        if clash:
            raise HarnessError(f"{folder.name} is labelled {meta['agent']}, so {s.model}'s answers would be stored "
                               f"under that name: write the task with --agent model:<this model> instead")
        log = _read(folder / LOG)
        if log.get("model") not in (None, s.model) and any(b.with_name(b.stem + "_result.json").exists()
                                                            for b in batch_files(folder)):
            raise HarnessError(f"{folder.name} was started with {log['model']}: finish it with the same model")
        check = checker(folder, meta)
        open_ = [b for b in batch_files(folder) if not b.with_name(b.stem + "_result.json").exists()]
        jobs += [(folder, b, check) for b in open_]
    return jobs[:limit] if limit else jobs


def find_folder(given: Path) -> Path:
    """A task folder as given, or relative to the project (so the command works from any folder). A folder that is
    not there gets the closest task folder names in the error."""
    for candidate in (given, ROOT / given):
        if (candidate / "task.json").is_file():
            return candidate.resolve()
    parent = next((p for p in (given.parent, ROOT / given.parent) if p.is_dir()), None)
    import difflib
    names = [p.name for p in parent.iterdir() if (p / "task.json").is_file()] if parent else []
    close = difflib.get_close_matches(given.name, names, n=3, cutoff=0.5)
    raise HarnessError(f"no task folder {given}" + (f"; in {parent} there is: {', '.join(close)}" if close else
                       f" (looked in {Path.cwd()} and {ROOT})"))


def run(folders: list[Path], s: Settings, parallel: int = 2, limit: int | None = None, progress=print) -> dict:
    jobs = plan(folders, s, limit)
    if not jobs:
        progress("Nothing to do: every batch already has a result.")
        return {"answered": 0, "failed": 0}
    locks = {f: threading.Lock() for f in folders}
    counts = {"answered": 0, "failed": 0, "failures": []}
    settings = {k: v for k, v in asdict(s).items() if k not in ("endpoint", "model", "api_key")}
    for folder in {job[0] for job in jobs}:      # model and settings on record from the start, not only at the end
        log = _read(folder / LOG)
        log.update(endpoint=s.endpoint, model=s.model, settings=settings, updated=now())
        log.setdefault("started", now())
        _write(folder / LOG, json.dumps(log, ensure_ascii=False, indent=1))
    progress(f"{len(jobs)} batch(es), {min(parallel, len(jobs))} agent(s) at once, model {s.model}, "
             f"context {s.context // 1024}k")

    def one(job) -> None:
        folder, batch, check = job
        entry = run_batch(folder, batch, s, check, progress)
        with locks[folder]:
            log = _read(folder / LOG)
            log.update(endpoint=s.endpoint, model=s.model, updated=now(),
                       settings={k: v for k, v in asdict(s).items() if k not in ("endpoint", "model", "api_key")})
            log.setdefault("started", entry["started"])
            log.setdefault("batches", {})[batch.name] = entry
            _write(folder / LOG, json.dumps(log, ensure_ascii=False, indent=1))
            counts[entry["status"]] += 1
            if entry["status"] == "failed":
                counts["failures"].append((f"{folder.name}/{batch.name}", entry["problems"]))
        progress(f"[{folder.name}/{batch.stem}] {entry['status']} after {entry['steps']} steps, "
                 f"{entry['submits']} submit(s), {entry['seconds'] / 60:.1f} min"
                 + (f": {entry['problems'][0]}" if entry["status"] == "failed" and entry["problems"] else ""))

    with ThreadPoolExecutor(max_workers=max(1, parallel)) as pool:
        list(pool.map(one, jobs))
    return counts


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def main(argv: list[str] | None = None) -> int:
    from .config import load_settings
    cfg = load_settings()
    ap = argparse.ArgumentParser(prog="python -m ichnos.harness", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folders", nargs="+", type=Path, help="task folders written by agent-tasks")
    ap.add_argument("--model", default=cfg.llm_model, help="default: LLM_MODEL in .env")
    ap.add_argument("--endpoint", default=cfg.llm_endpoint or "http://localhost:11434/v1",
                    help="the server's /v1 address (default: LLM_ENDPOINT in .env)")
    ap.add_argument("--parallel", type=int, default=2, help="agents at once (default 2)")
    ap.add_argument("--context", type=int, default=32768,
                    help="the server's context length in tokens")
    ap.add_argument("--max-steps", type=int, default=120)
    ap.add_argument("--no-think", action="store_true", help="answer each step without a reasoning block (faster)")
    ap.add_argument("--limit", type=int, help="answer only this many batches, for a trial")
    ap.add_argument("--timeout", type=int, default=900, help="seconds for one step (default 900)")
    ap.add_argument("--max-tokens", type=int, default=8192, help="tokens one step may write (default 8192)")
    a = ap.parse_args(argv)
    if not a.model:
        ap.error("give --model or set LLM_MODEL in .env")
    s = Settings(endpoint=a.endpoint, model=a.model, context=a.context, think=not a.no_think,
                 max_tokens=a.max_tokens, max_steps=a.max_steps, timeout=a.timeout,
                 api_key=getattr(cfg, "llm_api_key", None))
    try:
        counts = run([find_folder(f) for f in a.folders], s, a.parallel, a.limit)
    except HarnessError as err:
        print(f"Stopped: {err}")
        return 1
    print(f"\nDone: {counts['answered']} answered, {counts['failed']} failed.")
    for name, problems in counts.get("failures", []):
        print(f"  {name}: " + "; ".join(problems[:3]))
    if counts["answered"]:
        print("Import each answered folder with:  python -m ichnos agent-import <folder>")
    return 0 if not counts["failed"] else 2


if __name__ == "__main__":
    sys.exit(main())
