"""The number gate: a number in a claim must stand in what the claim cites, as the same statistic.

Interpretation is free; numbers are not. Checking that a number merely occurs somewhere in the cited
text is too weak: "β = 0.18" passes when 0.18 is an F value, and "n = 2720" passes when 2720 is the
degrees of freedom of F(1, 2720). So each number is read with the statistic written just before it,
and the same is looked up in each cited item:

- missing:    the number is in none of the cited items;
- statistic:  it is there, but only as another statistic (β in the text, F in the source);
- df_as_n:    a sample size that is only degrees of freedom in the source;
- ambiguous:  the source gives several values of that statistic and the variable named next to the
              number in the text is not next to it in the source.

The gate flags; it never deletes or rewrites a claim.
"""

from __future__ import annotations

import re

# A comma is a decimal mark only after a lone 0 ("0,18"); "F(1,2720)" holds two numbers.
NUMBER = re.compile(r"(?<![\w.])(0,\d+|\d+(?:\.\d+)?)(%?)")
SYMBOL = re.compile(
    r"(β|Β|beta|ηp²|ηp2|η²|η2|ω²|χ²|chi2|R²|R2|OR|HR|RR|SD|SE|[bBrRtFdgnNMpzZ])([a-z_]{0,12})"
    r"\s*(?:\([^()]{0,24}\))?\s*(?:=|<|>|≈|≤|≥)\s*[-−–]?\s*$")
DF = re.compile(r"(?:\b[Ft]|χ²|chi2)\s*\(\s*(?:[\d.]+\s*,\s*)?$")
NAMES = {"β": "beta", "Β": "beta", "beta": "beta", "b": "b", "B": "b", "r": "r", "R": "r", "R²": "r2", "R2": "r2",
         "F": "F", "t": "t", "d": "d", "g": "g", "n": "n", "N": "n", "M": "mean", "SD": "SD", "SE": "SE",
         "p": "p", "z": "z", "Z": "z", "OR": "OR", "HR": "HR", "RR": "RR", "χ²": "chi2", "chi2": "chi2",
         "ηp²": "eta2", "ηp2": "eta2", "η²": "eta2", "η2": "eta2", "ω²": "omega2"}
STOP = {"the", "a", "an", "of", "and", "or", "with", "for", "in", "on", "to", "was", "were", "is", "are", "had", "has",
        "went", "by", "at", "as", "than", "more", "less", "effect", "association", "associated", "between", "among"}
CLAUSE_END = re.compile(r";|\n|\.(?=\s|$)")
LABELS = {"missing": "numbers not in what the sentence cites",
          "statistic": "numbers that are another statistic in the source",
          "df_as_n": "sample sizes that are degrees of freedom in the source",
          "ambiguous": "numbers whose variable the source does not confirm"}


def worth_checking(value: str) -> bool:
    """Decimals, percentages and integers of 10 or more (not '3 studies')."""
    return "." in value or value.endswith("%") or float(value.rstrip("%")) >= 10


def stats(text: str) -> list[tuple[str, str | None, int]]:
    """(value, statistic or None, position) for each number in a text."""
    out = []
    for m in NUMBER.finditer(text or ""):
        value = m.group(1).replace(",", ".") + m.group(2)
        before = text[max(0, m.start() - 30):m.start()]
        if DF.search(before):
            out.append((value, "df", m.start()))
            continue
        s = SYMBOL.search(before)
        out.append((value, NAMES.get(s.group(1)) if s else None, m.start()))
    return out


def same(a: str, b: str) -> bool:
    """0.38 and .38, 5.40 and 5.4, 12% and 12 are the same number."""
    try:
        return abs(float(a.rstrip("%")) - float(b.rstrip("%"))) < 1e-9
    except ValueError:
        return False


def _term(text: str, pos: int) -> set[str]:
    """Content words just before a number's statistic: what the number is about."""
    words = re.findall(r"[^\W\d_][\w-]*", text[max(0, pos - 60):pos])
    return {w.lower() for w in words[-6:] if w.lower() not in STOP and len(w) > 2} - set(NAMES)


def _clause(text: str, pos: int) -> str:
    """The clause around a position: between semicolons, line breaks or sentence ends (not decimal points)."""
    bounds = [0] + [m.end() for m in CLAUSE_END.finditer(text)] + [len(text)]
    start = max(b for b in bounds if b <= pos)
    end = min((b for b in bounds if b > pos), default=len(text))
    return text[start:end].lower()


def finding_text(finding: str, effect: str | None = None, for_whom: str | None = None, evidence: str | None = None,
                 correction: str | None = None) -> str:
    """One cited finding as the gate reads it: the effect stays in the statement's clause, so a value from
    the effect field keeps its variable."""
    out = (finding or "").strip().rstrip(".")
    if effect:
        out += f" ({effect})"
    if for_whom:
        out += f" for {for_whom}"
    return out + ". " + " ".join(x for x in (evidence, correction) if x)


def gate(text: str, items: list[str], exempt: set[str] | None = None) -> dict[str, list[str]]:
    """Check every number in `text` against the cited `items` (one string per cited finding, fact or table).
    `exempt` holds numbers that are not data (ids, counts of the corpus). Returns problems by kind."""
    exempt = exempt or set()
    parsed = [(item, stats(item)) for item in items]
    problems: dict[str, list[str]] = {k: [] for k in LABELS}
    for value, sym, pos in stats(text):
        if not worth_checking(value) or any(same(value, e) for e in exempt):
            continue
        hits = [(item, s, p) for item, found in parsed for v, s, p in found if same(value, v)]
        if not hits:
            problems["missing"].append(value)
            continue
        if not sym or sym in ("p", "df"):
            continue
        fitting = [h for h in hits if h[1] in (None, sym)]
        if not fitting:
            kinds = sorted({h[1] for h in hits if h[1]})
            if sym == "n" and kinds == ["df"]:
                problems["df_as_n"].append(value)
            else:
                problems["statistic"].append(f"{value} ({sym} in the text, {', '.join(kinds)} in the source)")
            continue
        labelled = [h for h in fitting if h[1] == sym]
        term = _term(text, pos)
        if labelled and term:
            # confirmed if any cited item gives this value only once for the statistic, or names the variable
            # in the value's clause
            def confirms(hit):
                item, _, where = hit
                rivals = {v for v, s, _ in stats(item) if s == sym}
                return len(rivals) <= 1 or any(t in _clause(item, where) for t in term)
            if not any(confirms(h) for h in labelled):
                n = max(len({v for v, s, _ in stats(h[0]) if s == sym}) for h in labelled)
                problems["ambiguous"].append(f"{value} ({sym}; the source gives {n} {sym} values)")
    return {k: v for k, v in problems.items() if v}


def describe(problems: dict[str, list[str]]) -> list[str]:
    return [f"{LABELS[k]}: {', '.join(v)}" for k, v in problems.items()]
