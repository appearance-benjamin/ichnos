"""Local models as agents: answer a task folder's batches through an OpenAI-compatible chat endpoint.

agent-tasks writes the task, this module asks a model for each batch_NN_result.json, and agent-import checks and
stores the answers as it does for any agent. Ollama, LM Studio, llama.cpp's server and vLLM all serve the same
endpoint (/v1/chat/completions), so one runner works on a home PC and on a GPU cluster.

- Every answer is checked as the import will check it (agents.answer_checker) before it is written. A failed answer
  is asked for again with its problems named; a batch that still fails gets no result file, so it blocks nothing
  else and the next run asks again.
- Batches that already have a result are skipped, so a stopped run resumes.
- runner.json in the folder records the server, the model as the server lists it, the settings and each batch's
  attempts. Fixed local weights make a run repeatable; this file says which weights.

Only tasks whose batch holds everything the agent needs run here (agents.SELF_CONTAINED). Manuscripts, audits and
reviews are read piece by piece from several files, which a model answering one prompt cannot do.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from pathlib import Path

from .agents import IMPORTED, _strip_fences, answer_checker, batch_files
from .store import Store, now

DEFAULT_ENDPOINT = "http://localhost:11434/v1"     # Ollama; LM Studio serves http://localhost:1234/v1
LOG = "runner.json"
BACKOFF = 5.0           # seconds before asking again after a server error, doubled each time
THINKING = re.compile(r"<think>.*?</think>", re.S)
OTHER_FAMILIES = ("claude", "opus", "sonnet", "haiku", "gpt", "gemini")


class RunnerError(Exception):
    """Stops the whole run: no server, or a task this runner cannot do."""


class BatchError(Exception):
    """Stops one batch: asking again would not help."""


@dataclass(frozen=True)
class RunSettings:
    endpoint: str
    model: str
    temperature: float = 0.0
    seed: int | None = 1
    max_tokens: int | None = None       # None: the server's own limit
    json_mode: bool = True              # response_format json_object; some servers refuse it
    retries: int = 2                    # how often a failed answer is asked again
    timeout: int = 900                  # seconds for one answer
    api_key: str | None = None
    think: bool = True                  # False: reasoning models answer without a thinking phase (reasoning_effort none)


def run_task(folder: Path, store: Store, s: RunSettings, parallel: int = 1, limit: int | None = None,
             progress=print) -> dict:
    """Answer every open batch of the task. Returns the counts and the batches that failed with their problems."""
    if (folder / IMPORTED).exists():
        raise RunnerError("this task is already imported")
    meta = json.loads((folder / "task.json").read_text(encoding="utf-8"))
    clash = [f for f in OTHER_FAMILIES if f in meta["agent"].lower() and f not in s.model.lower()]
    if clash:
        raise RunnerError(f"the task is labelled {meta['agent']}, so {s.model}'s answers would be stored under that "
                          f"name: write the task with --agent model:<this model> instead")
    try:
        check = answer_checker(folder, store)
    except ValueError as err:
        raise RunnerError(str(err)) from None
    log = _read_log(folder)
    batches = batch_files(folder)
    done = [b for b in batches if _result(b).exists()]
    if done and log.get("model") not in (None, s.model):
        raise RunnerError(f"this task was started with {log['model']}: finish it with the same model, or write a new "
                          f"task for {s.model}")
    listed = _listed(s)
    if listed is not None and not listed.get("id"):
        progress(f"Note: the server does not list {s.model}; it lists {', '.join(listed['others'][:8]) or 'nothing'}.")
    instructions = (folder / "instructions.md").read_text(encoding="utf-8")
    todo = [b for b in batches if not _result(b).exists()][:limit]
    log.update(endpoint=s.endpoint, model=s.model, listed=listed["entry"] if listed else None,
               settings={k: v for k, v in asdict(s).items() if k not in ("endpoint", "model", "api_key")},
               started=log.get("started") or now())
    log.setdefault("batches", {})
    counts = {"answered": 0, "failed": 0, "already": len(done), "failures": []}
    lock = threading.Lock()

    def one(batch: Path) -> None:
        entry = answer_batch(s, instructions, batch, check)
        with lock:
            log["batches"][batch.name] = entry
            log["updated"] = now()
            _write(folder / LOG, json.dumps(log, ensure_ascii=False, indent=1))
            counts[entry["status"]] += 1
            if entry["status"] == "failed":
                counts["failures"].append((batch.name, entry["problems"]))
            progress(f"{batch.name}: {entry['status']} after {entry['attempts']} attempt(s), {entry['seconds']} s"
                     + (f": {entry['problems'][0]}" if entry["status"] == "failed" and entry["problems"] else ""))

    with ThreadPoolExecutor(max_workers=max(1, parallel)) as pool:
        list(pool.map(one, todo))
    return counts


def answer_batch(s: RunSettings, instructions: str, batch: Path, check) -> dict:
    """Ask for one batch until its answer passes the import's checks, then write batch_NN_result.json. Returns the
    batch's entry for runner.json."""
    text = batch.read_text(encoding="utf-8")
    data = json.loads(text)
    first = [{"role": "system", "content": instructions},
             {"role": "user", "content": f"{batch.name}:\n\n{text}\n\nAnswer for this batch with the JSON object "
                                         f"only."}]
    messages = first
    entry = {"status": "failed", "attempts": 0, "problems": [], "prompt_tokens": 0, "completion_tokens": 0}
    started = time.monotonic()
    try:
        for _ in range(1 + max(0, s.retries)):
            reply = _ask(s, messages)
            entry["attempts"] += 1
            usage = reply.get("usage") or {}
            entry["prompt_tokens"] += usage.get("prompt_tokens") or 0
            entry["completion_tokens"] += usage.get("completion_tokens") or 0
            entry["served_model"] = reply.get("model")
            sent = sum(len(m["content"]) for m in messages)
            # About 4 characters make a token; a server that read far fewer cut the prompt to its context length.
            if usage.get("prompt_tokens") and usage["prompt_tokens"] * 6 < sent:
                raise BatchError(f"the server read {usage['prompt_tokens']} tokens of a prompt of about {sent // 4}: "
                                 f"it cut the prompt to its context length (Ollama: set OLLAMA_CONTEXT_LENGTH), or "
                                 f"write the task with a smaller --batch-size")
            choice = (reply.get("choices") or [{}])[0]
            if choice.get("finish_reason") == "length":
                raise BatchError("the answer was cut off at the token limit: raise --max-tokens or the server's "
                                 "context length, or write the task with a smaller --batch-size")
            content = (choice.get("message") or {}).get("content") or ""
            answer = clean(content)
            problems = check(data, answer)
            if not problems:
                _write(_result(batch), answer)
                entry.update(status="answered", problems=[])
                break
            entry["problems"] = problems[:10]
            messages = first + [
                {"role": "assistant", "content": content},
                {"role": "user", "content": "Your answer has these problems:\n"
                                            + "\n".join(f"- {p}" for p in problems[:20])
                                            + "\n\nGive the whole corrected JSON object for the same batch, and "
                                              "nothing else."}]
    except BatchError as err:
        entry["problems"] = [str(err)]
    entry["seconds"] = round(time.monotonic() - started, 1)
    return entry


def clean(content: str) -> str:
    """The JSON in a model's reply as plain JSON: without a reasoning block, code fences or words around it. A reply
    with no JSON in it is returned as it is, for the check to name the problem."""
    text = _strip_fences(THINKING.sub("", content)).strip()
    candidates = [text]
    starts = [i for i in (text.find("{"), text.find("[")) if i >= 0]
    end = max(text.rfind("}"), text.rfind("]"))
    if starts and end > min(starts):
        candidates.append(text[min(starts):end + 1])
    for c in candidates:
        try:
            return json.dumps(json.loads(c), ensure_ascii=False, indent=1)
        except ValueError:
            continue
    return text


def chat(s: RunSettings, messages: list[dict]) -> dict:
    body = {"model": s.model, "messages": messages, "temperature": s.temperature, "stream": False}
    if s.seed is not None:
        body["seed"] = s.seed
    if s.max_tokens:
        body["max_tokens"] = s.max_tokens
    if s.json_mode:
        body["response_format"] = {"type": "json_object"}
    if not s.think:
        body["reasoning_effort"] = "none"                              # Ollama
        body["chat_template_kwargs"] = {"enable_thinking": False}      # vLLM (Qwen's chat template)
    request = urllib.request.Request(s.endpoint.rstrip("/") + "/chat/completions",
                                     data=json.dumps(body).encode("utf-8"), headers=_headers(s))
    with urllib.request.urlopen(request, timeout=s.timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _ask(s: RunSettings, messages: list[dict], tries: int = 3) -> dict:
    """chat(), asked again after a server error or a timeout; no server at all stops the run."""
    wait = BACKOFF
    for n in range(1, tries + 1):
        try:
            return chat(s, messages)
        except urllib.error.HTTPError as err:
            detail = err.read().decode("utf-8", "replace")[:300]
            if err.code < 500:
                hint = " (try --no-json-mode)" if "response_format" in detail else ""
                raise BatchError(f"the server refused the request (HTTP {err.code}{hint}): {detail}") from None
            problem = f"HTTP {err.code}: {detail}"
        except urllib.error.URLError as err:
            if not isinstance(err.reason, TimeoutError):
                raise RunnerError(f"no server answers at {s.endpoint} ({err.reason}): start it first, e.g. "
                                  f"`ollama serve`") from None
            problem = f"no answer within {s.timeout} s"
        except (TimeoutError, ConnectionError) as err:
            problem = f"{type(err).__name__}: {err}"
        if n < tries:
            time.sleep(wait)
            wait *= 2
    raise BatchError(f"the server failed {tries} times ({problem})")


def _listed(s: RunSettings) -> dict | None:
    """The server's own entry for the model (GET /models), recorded with the run; None when the server lists none."""
    request = urllib.request.Request(s.endpoint.rstrip("/") + "/models", headers=_headers(s))
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            models = json.loads(response.read().decode("utf-8")).get("data") or []
    except urllib.error.HTTPError:
        return None
    except urllib.error.URLError as err:
        raise RunnerError(f"no server answers at {s.endpoint} ({err.reason}): start it first, e.g. "
                          f"`ollama serve`") from None
    except (ValueError, TimeoutError, ConnectionError):
        return None
    entry = next((m for m in models if m.get("id") == s.model), None)
    return {"id": entry and entry["id"], "entry": entry, "others": [str(m.get("id")) for m in models]}


def _headers(s: RunSettings) -> dict:
    return {"Content-Type": "application/json", **({"Authorization": f"Bearer {s.api_key}"} if s.api_key else {})}


def _result(batch: Path) -> Path:
    return batch.with_name(batch.stem + "_result.json")


def _read_log(folder: Path) -> dict:
    path = folder / LOG
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _write(path: Path, text: str) -> None:
    """Whole or not at all: an import never reads a half-written answer."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
