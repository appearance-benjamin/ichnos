"""agent-run: a local model answers a task through an OpenAI-compatible server (here a fake one)."""

import json
import shutil
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from psyscientist import agents, runner
from psyscientist.runner import RunnerError, RunSettings, run_task
from tests.test_agents_map import Base

METRICS = ("empirical", "population", "design")


class FakeServer:
    """Answers /chat/completions with reply(request): a string (the content), a dict (content and what to report)
    or (status, payload) for an error. Lists `models` at /models."""

    def __init__(self, reply, models=("toy-model",)):
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def send(self, status, payload):
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self.send(200, {"object": "list", "data": [{"id": m, "object": "model"} for m in models]})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append(body)
                out = reply(body)
                if isinstance(out, tuple):
                    return self.send(*out)
                out = {"content": out} if isinstance(out, str) else out
                sent = sum(len(m["content"]) for m in body["messages"])
                self.send(200, {"model": body["model"],
                                "choices": [{"index": 0, "finish_reason": out.get("finish_reason", "stop"),
                                             "message": {"role": "assistant", "content": out["content"]}}],
                                "usage": {"prompt_tokens": out.get("prompt_tokens", sent // 4),
                                          "completion_tokens": 50}})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.endpoint = f"http://127.0.0.1:{self.httpd.server_port}/v1"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def batch_of(request) -> tuple[str, list]:
    name, _, rest = request["messages"][1]["content"].partition(":\n\n")
    return name, json.loads(rest.rsplit("\n\nAnswer", 1)[0])


def screen_answer(records, skip=0) -> str:
    return json.dumps({"records": [{"work_id": r["work_id"], "kind": "study", "note": "toy",
                                    "scores": {m: {"score": 2, "evidence": r["title"][:15]} for m in METRICS}}
                                   for r in records[skip:]], "insights": []})


class Runner(Base):
    def setUp(self):
        super().setUp()
        self.folder = agents.write_tasks(self.store, self.run, "screen", "model:toy-model", root=self.root,
                                         batch_size=3)
        backoff, runner.BACKOFF = runner.BACKOFF, 0
        self.addCleanup(setattr, runner, "BACKOFF", backoff)

    def serve(self, reply, **kw) -> FakeServer:
        server = FakeServer(reply, **kw)
        self.addCleanup(server.close)
        return server

    def go(self, server, model="toy-model", folder=None, limit=None, **kw):
        return run_task(folder or self.folder, self.store, RunSettings(server.endpoint, model, **kw), limit=limit,
                        progress=lambda _: None)

    def test_answers_are_cleaned_checked_and_pass_the_import(self):
        server = self.serve(lambda req: f"<think>Let me see.</think>\n```json\n{screen_answer(batch_of(req)[1])}\n```")
        counts = self.go(server)
        self.assertEqual((counts["answered"], counts["failed"]), (2, 0))
        result = (self.folder / "batch_01_result.json").read_text(encoding="utf-8")
        self.assertNotIn("think", result)
        self.assertEqual(len(json.loads(result)["records"]), 3)
        first = server.requests[0]
        self.assertEqual(first["messages"][0], {"role": "system",
                                                "content": (self.folder / "instructions.md").read_text("utf-8")})
        self.assertEqual((first["temperature"], first["seed"], first["response_format"]),
                         (0.0, 1, {"type": "json_object"}))
        log = json.loads((self.folder / runner.LOG).read_text(encoding="utf-8"))
        self.assertEqual((log["model"], log["listed"]["id"]), ("toy-model", "toy-model"))
        self.assertEqual({b["status"] for b in log["batches"].values()}, {"answered"})
        imported = agents.import_task(self.folder, self.store)
        self.assertEqual((imported["problems"], imported["stored"]), ([], 4))

    def test_a_failed_answer_is_asked_again_with_its_problems(self):
        seen = {}

        def reply(req):
            name, records = batch_of(req)
            seen[name] = seen.get(name, 0) + 1
            return screen_answer(records, skip=1 if seen[name] == 1 else 0)    # first: one record left out
        server = self.serve(reply)
        counts = self.go(server)
        self.assertEqual(counts["answered"], 2)
        retry = server.requests[1]["messages"]
        self.assertEqual([m["role"] for m in retry], ["system", "user", "assistant", "user"])
        self.assertIn("work_ids differ from the batch", retry[3]["content"])
        log = json.loads((self.folder / runner.LOG).read_text(encoding="utf-8"))
        self.assertEqual({b["attempts"] for b in log["batches"].values()}, {2})
        self.assertEqual(agents.import_task(self.folder, self.store)["problems"], [])

    def test_a_batch_that_keeps_failing_gets_no_result_and_is_asked_again_next_run(self):
        server = self.serve(lambda req: "no JSON here" if batch_of(req)[0] == "batch_01.json"
                            else screen_answer(batch_of(req)[1]))
        counts = self.go(server, retries=2)
        self.assertEqual((counts["answered"], counts["failed"]), (1, 1))
        self.assertEqual(counts["failures"][0][0], "batch_01.json")
        self.assertIn("not valid JSON", counts["failures"][0][1][0])
        self.assertFalse((self.folder / "batch_01_result.json").exists())
        self.assertEqual(len(server.requests), 4)       # batch_01 three times, batch_02 once
        self.assertEqual(agents.task_state(self.folder), "partly answered")

        fixed = self.serve(lambda req: screen_answer(batch_of(req)[1]))
        counts = self.go(fixed)
        self.assertEqual((counts["answered"], counts["already"]), (1, 1))
        self.assertEqual([batch_of(r)[0] for r in fixed.requests], ["batch_01.json"])
        self.assertEqual(agents.task_state(self.folder), "answered")

    def test_a_cut_prompt_or_a_cut_answer_is_not_asked_again(self):
        server = self.serve(lambda req: {"content": screen_answer(batch_of(req)[1]), "prompt_tokens": 10})
        counts = self.go(server, limit=1)
        self.assertEqual(counts["failed"], 1)
        self.assertIn("cut the prompt", counts["failures"][0][1][0])
        self.assertEqual(len(server.requests), 1)

        server = self.serve(lambda req: {"content": screen_answer(batch_of(req)[1])[:40], "finish_reason": "length"})
        counts = self.go(server, limit=1)
        self.assertIn("cut off at the token limit", counts["failures"][0][1][0])
        self.assertEqual(len(server.requests), 1)

    def test_server_errors_are_asked_again(self):
        calls = []

        def reply(req):
            calls.append(1)
            return (503, {"error": "loading model"}) if len(calls) == 1 else screen_answer(batch_of(req)[1])
        counts = self.go(self.serve(reply))
        self.assertEqual((counts["answered"], counts["failed"]), (2, 0))

    def test_refuses_what_it_cannot_do_well(self):
        server = self.serve(lambda req: screen_answer(batch_of(req)[1]))
        claude = agents.write_tasks(self.store, self.run, "screen", "model:claude-sonnet-5-5", root=self.root)
        with self.assertRaisesRegex(RunnerError, "write the task with --agent"):
            self.go(server, folder=claude)

        audit = self.folder.with_name("audit__model-toy-model")
        shutil.copytree(self.folder, audit)
        meta = json.loads((audit / "task.json").read_text(encoding="utf-8"))
        (audit / "task.json").write_text(json.dumps(dict(meta, kind="audit")), encoding="utf-8")
        with self.assertRaisesRegex(RunnerError, "audit tasks need an agent that reads"):
            self.go(server, folder=audit)

        closed = FakeServer(lambda req: "")
        closed.close()
        with self.assertRaisesRegex(RunnerError, "no server answers"):
            self.go(closed)

        self.go(server, limit=1)
        with self.assertRaisesRegex(RunnerError, "started with toy-model"):
            self.go(self.serve(lambda req: "", models=("other-model",)), model="other-model")

        self.go(server)
        agents.import_task(self.folder, self.store)
        with self.assertRaisesRegex(RunnerError, "already imported"):
            self.go(server)


if __name__ == "__main__":
    unittest.main()
