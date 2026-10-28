"""Bibliographic sources. Each module exposes NAME and search(http, query, cap, ...) -> SearchResult."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SearchResult:
    source: str
    query: str                      # exactly as sent
    reported_total: int | None      # what the source says matches
    records: list[dict] = field(default_factory=list)
    notes: str | None = None        # e.g. how the source translated the query
