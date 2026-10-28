"""Agreement between screeners on one run's sheet.

A pair is compared only once both screeners have decided every record on the
sheet, so importing part of a sheet never reveals another screener's calls.
"""

from __future__ import annotations

from dataclasses import dataclass

LEVELS = ("include", "unsure", "exclude")


@dataclass
class Pair:
    a: str
    b: str
    work_ids: list[int]
    labels: list[tuple[str, str]]          # (a's decision, b's decision) per work_id

    @property
    def n(self) -> int:
        return len(self.labels)

    @property
    def observed(self) -> float:
        return sum(x == y for x, y in self.labels) / self.n

    def table(self) -> dict[tuple[str, str], int]:
        counts = {(x, y): 0 for x in LEVELS for y in LEVELS}
        for pair in self.labels:
            counts[pair] += 1
        return counts

    def kappa(self, collapse_unsure: bool = False) -> float | None:
        """Cohen's kappa; collapse_unsure compares keep (include or unsure) against exclude."""
        fold = (lambda d: "keep" if d != "exclude" else "exclude") if collapse_unsure else (lambda d: d)
        labels = [(fold(x), fold(y)) for x, y in self.labels]
        return cohen_kappa(labels)

    def dropped_by(self, who: str) -> list[int]:
        """Records the other screener kept (include or unsure) and `who` excluded."""
        idx = 1 if who == self.b else 0
        return [w for w, lab in zip(self.work_ids, self.labels)
                if lab[idx] == "exclude" and lab[1 - idx] != "exclude"]


def cohen_kappa(labels: list[tuple[str, str]]) -> float | None:
    n = len(labels)
    if n == 0:
        return None
    cats = sorted({c for pair in labels for c in pair})
    p_o = sum(x == y for x, y in labels) / n
    p_e = sum((sum(x == c for x, _ in labels) / n) * (sum(y == c for _, y in labels) / n) for c in cats)
    if p_e == 1:
        return 1.0 if p_o == 1 else None
    return (p_o - p_e) / (1 - p_e)


def complete_pairs(screening: list, sample_ids: set[int]) -> tuple[dict[str, dict[int, object]], list[Pair]]:
    """Group screening rows by screener and build a Pair for every two screeners who finished the sheet."""
    by: dict[str, dict[int, object]] = {}
    for row in screening:
        if row["work_id"] in sample_ids:
            by.setdefault(row["screener"], {})[row["work_id"]] = row
    done = sorted(s for s, rows in by.items() if sample_ids and sample_ids <= rows.keys())
    pairs = []
    for i, a in enumerate(done):
        for b in done[i + 1:]:
            ids = sorted(sample_ids)
            pairs.append(Pair(a, b, ids, [(by[a][w]["decision"], by[b][w]["decision"]) for w in ids]))
    return by, pairs
