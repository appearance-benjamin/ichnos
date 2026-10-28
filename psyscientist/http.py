"""HTTP GET with retries, where every attempt is logged to the evidence store.

A source can be paced (`pace`): its requests are spaced at least that many seconds apart, so a service with a low
limit (CORE without a key, Semantic Scholar without a key) is not run into its 429s. A 429 waits as long as the
service says, in either header it uses (Retry-After in seconds, or CORE's X-RateLimit-Retry-After timestamp)."""

from __future__ import annotations

import hashlib
import http.client
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from . import __version__
from .store import Store

# 202: a server still preparing the file (some repositories answer so before the PDF is ready).
RETRY_STATUSES = {202, 429, 500, 502, 503, 504}
REDACT_PARAMS = {"api_key", "mailto", "email"}
LOGGED_HEADERS = ("x-ratelimit-remaining", "x-ratelimit-cost-usd", "x-ratelimit-remaining-usd",
                  "x-ratelimit-limit", "retry-after", "x-ratelimit-retry-after")
MAX_WAIT = 120.0
GUARDED_WAIT = 20.0      # longest wait after a refusal from a source with a breaker


class ApiError(RuntimeError):
    def __init__(self, source: str, status: int | None, message: str):
        super().__init__(f"{source}: HTTP {status}: {message}")
        self.status = status


class Http:
    """The User-Agent never carries the contact email: it reaches every host, PDF servers included.
    The scholarly APIs that ask for an email get it as their own parameter instead."""

    def __init__(self, store: Store, run_id: int | None, max_retries: int = 6, sleep=time.sleep, clock=time.monotonic):
        self.store = store
        self.run_id = run_id
        self.max_retries = max_retries
        self.sleep = sleep
        self.clock = clock
        self.user_agent = f"PsyScientist/{__version__}"
        self.intervals: dict[str, float] = {}     # source prefix -> least seconds between its requests
        self._last: dict[str, float] = {}
        self.breakers: dict[str, int] = {}        # source prefix -> refusals in a row after which it is paused
        self._refused: dict[str, int] = {}
        self.paused: set[str] = set()

    def pace(self, prefix: str, seconds: float) -> None:
        """Space the requests of every source whose name starts with `prefix` at least `seconds` apart."""
        self.intervals[prefix] = seconds

    def breaker(self, prefix: str, after: int = 3) -> None:
        """Pause a source for the rest of this run once it has refused `after` requests in a row (429 after every
        retry), instead of waiting out its limit on every record; the records it missed are asked again next run."""
        self.breakers[prefix] = after

    def _prefix(self, source: str, table) -> str | None:
        return next((p for p in table if source.startswith(p)), None)

    def _wait_turn(self, source: str) -> None:
        prefix = self._prefix(source, self.intervals)
        if prefix:
            last, gap = self._last.get(prefix), self.intervals[prefix]
            if last is not None and self.clock() - last < gap:
                self.sleep(gap - (self.clock() - last))
            self._last[prefix] = self.clock()

    def get_json(self, source: str, url: str, params: dict, headers: dict | None = None,
                 reuse: bool = False) -> dict:
        return json.loads(self.get(source, url, params, {"Accept": "application/json", **(headers or {})},
                                   **({"reuse": True} if reuse else {})))

    def get(self, source: str, url: str, params: dict, headers: dict | None = None,
            keep_body: bool = True, reuse: bool = False) -> bytes:
        """keep_body=False logs only the response's SHA-256 and size (for PDFs, which stay on disk).
        reuse=True answers from this run's log when the same request already succeeded (a resumed job does not
        ask a slow service again for what it already has)."""
        url = urllib.parse.quote(url, safe=":/?&=%#+,;@!$'()*[]~")
        sep = "&" if "?" in url else "?"
        full = url + (sep + urllib.parse.urlencode(params) if params else "")
        logged = url + (sep + urllib.parse.urlencode(
            {k: ("REDACTED" if k in REDACT_PARAMS else v) for k, v in params.items()}) if params else "")
        if reuse:
            row = self.store.db.execute(
                "SELECT call_id FROM calls WHERE source = ? AND request = ? AND status = 200 AND run_id IS ? "
                "AND response_gz IS NOT NULL ORDER BY call_id DESC LIMIT 1", (source, logged, self.run_id)).fetchone()
            if row:
                return self.store.call_response(row["call_id"])
        guarded = self._prefix(source, self.breakers)
        if guarded in self.paused:
            raise ApiError(source, 429, "paused for this run after repeated refusals (asked again next run)")
        req_headers = {"User-Agent": self.user_agent, **(headers or {})}

        for attempt in range(self.max_retries + 1):
            self._wait_turn(source)
            started = time.monotonic()
            try:
                with urllib.request.urlopen(urllib.request.Request(full, headers=req_headers), timeout=90) as resp:
                    status, body, resp_headers = resp.status, resp.read(), resp.headers
            except urllib.error.HTTPError as err:
                status, body, resp_headers = err.code, err.read(), err.headers
            except ValueError as err:                 # not a fetchable URL
                raise ApiError(source, None, str(err)) from err
            except (OSError, http.client.HTTPException) as err:
                status, body, resp_headers = None, str(err).encode(), {}
            duration_ms = int((time.monotonic() - started) * 1000)
            meta = {h: resp_headers.get(h) for h in LOGGED_HEADERS if resp_headers.get(h) is not None}
            meta["attempt"] = attempt
            if not keep_body:
                meta.update(bytes=len(body), sha256=hashlib.sha256(body).hexdigest(),
                            content_type=resp_headers.get("content-type"))
            self.store.log_call(self.run_id, "http", source, logged, status, duration_ms,
                                body if keep_body or status != 200 else None, meta)

            if status == 200:
                if guarded:
                    self._refused[guarded] = 0
                return body
            if guarded and status == 429:
                # Every refusal counts, retry or not, and is waited out briefly: a service that keeps refusing is
                # paused after a few, not after a few rounds of two-minute waits.
                self._refused[guarded] = self._refused.get(guarded, 0) + 1
                if self._refused[guarded] >= self.breakers[guarded]:
                    self.paused.add(guarded)
                    raise ApiError(source, status, _message(body) + " (paused for this run)")
                if attempt < self.max_retries:
                    self.sleep(min(self._wait(attempt, resp_headers.get("retry-after"),
                                              resp_headers.get("x-ratelimit-retry-after")), GUARDED_WAIT))
                    continue
                raise ApiError(source, status, _message(body))
            if (status is None or status in RETRY_STATUSES) and attempt < self.max_retries:
                self.sleep(self._wait(attempt, resp_headers.get("retry-after"),
                                      resp_headers.get("x-ratelimit-retry-after")))
                continue
            raise ApiError(source, status, _message(body))
        raise AssertionError("unreachable")

    @staticmethod
    def _wait(attempt: int, retry_after: str | None, retry_at: str | None = None,
              now: datetime | None = None) -> float:
        if retry_after:
            try:
                return min(float(retry_after), MAX_WAIT)
            except ValueError:
                pass
        if retry_at:                              # CORE: the moment the limit resets, e.g. 2026-10-25T10:56:52+0000
            try:
                at = datetime.strptime(retry_at.strip(), "%Y-%m-%dT%H:%M:%S%z")
                seconds = (at - (now or datetime.now(timezone.utc))).total_seconds() + 1.0
                return min(max(seconds, 1.0), MAX_WAIT)
            except ValueError:
                pass
        return min(2.0 * 2 ** attempt, 60.0)


def _message(body: bytes) -> str:
    try:
        data = json.loads(body)
        return str(data.get("message") or data.get("error") or data)[:500]
    except (ValueError, AttributeError):
        text = body[:20000].decode("utf-8", "replace")
        if "<html" in text.lower() or "<!doctype" in text.lower():
            # A web page instead of data: keep its title, the most telling part.
            m = re.search(r"<title[^>]*>(.*?)</title>", text, re.I | re.S)
            text = "web page: " + (" ".join(m.group(1).split()) if m else "no title")
        return " ".join(text.split())[:300]
