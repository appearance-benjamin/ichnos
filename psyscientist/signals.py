"""Text signals that show a screener, at a glance, what a record says about design and heterogeneity.

The built-in lexicons are research-methods vocabulary, so they work in any
psychological domain. A protocol can extend or replace them under [signals],
and the corpus search string adds one lexicon per concept block, named by the
protocol's `query_blocks`. Terms follow the query rules: words in order (spaces
and hyphens interchangeable), a trailing * for any ending.

A signal is a hint for the screener, never a decision.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import query
from .query import Term

DEFAULT_LEXICONS: dict[str, list[str]] = {
    # Names a way of modelling change over time or its shape.
    "trajectory": [
        "trajector*", "growth curve*", "latent growth", "growth model*", "growth mixture",
        "latent class growth", "group-based trajectory", "latent transition", "piecewise",
        "change over time", "changes over time", "within-person", "intraindividual", "intra-individual",
        "developmental course", "set point", "hedonic adaptation", "anticipation and adaptation",
        "before and after", "across the transition", "intercept and slope", "random slope*", "rate of change",
    ],
    # Says that people differ: subgroups, moderators, classes or profiles.
    "heterogeneity": [
        "moderator*", "moderated", "moderation", "moderating", "subgroup*", "heterogene*",
        "individual differences", "for whom", "interaction effect*", "interacted with", "differed by",
        "differ by", "differs by", "varied by", "varies by", "depending on", "latent class*", "latent profile*",
        "person-centered", "person-centred", "mixture model*", "cluster analysis", "stratified by",
    ],
    # Says the design follows people over time.
    "longitudinal": [
        "longitudinal", "panel", "prospective*", "wave*", "follow-up", "followed up", "cohort",
        "repeated measure*", "repeated assessment*", "diary", "experience sampling", "ecological momentary",
        "years later", "baseline", "over time",
    ],
    # Says the opposite.
    "cross_sectional": ["cross-sectional", "single time point", "one-time survey", "single measurement"],
}

LEVELS = ("strong", "moderate", "weak", "none")
LEVEL_HELP = {
    "strong": "names a trajectory or change-modelling method",
    "moderate": "longitudinal design and subgroup or moderator language",
    "weak": "only one of the two",
    "none": "neither",
}


@dataclass(frozen=True)
class Hit:
    lexicon: str
    label: str
    start: int
    end: int


def lexicons(protocol: dict) -> dict[str, list[Term]]:
    """Built-in lexicons, the protocol's changes, then one lexicon per query block."""
    words = {name: list(terms) for name, terms in DEFAULT_LEXICONS.items()}
    custom = protocol.get("signals") or {}
    for name, terms in (custom.get("replace") or {}).items():
        words[name] = list(terms)
    for name, terms in (custom.get("add") or {}).items():
        words.setdefault(name, []).extend(terms)
    out = {name: _dedupe(to_term(t) for t in terms) for name, terms in words.items()}
    if protocol.get("query"):
        names = protocol.get("query_blocks") or []
        for i, block in enumerate(query.blocks(query.parse(protocol["query"]))):
            name = names[i] if i < len(names) else f"block_{i + 1}"
            out[f"query:{name}"] = _dedupe(query.terms(block))
    return out


def to_term(text: str) -> Term:
    text = text.strip().lower()
    prefix = text.endswith("*")
    body = text.rstrip("*").strip('"')
    return Term(body, phrase=bool(re.search(r"[\s\-]", body)), prefix=prefix)


def detect(text: str, lex: dict[str, list[Term]]) -> list[Hit]:
    hits = []
    for name, terms in lex.items():
        for t in terms:
            hits.extend(Hit(name, t.label, m.start(), m.end()) for m in t.regex().finditer(text or ""))
    return sorted(hits, key=lambda h: (h.start, -h.end))


def trajectory_level(hits: list[Hit]) -> str:
    found = {h.lexicon for h in hits}
    if "trajectory" in found:
        return "strong"
    if "heterogeneity" in found and "longitudinal" in found:
        return "moderate"
    if "heterogeneity" in found or "longitudinal" in found:
        return "weak"
    return "none"


def summary(title: str | None, abstract: str | None, lex: dict[str, list[Term]]) -> dict:
    """Per record: hits in title and abstract, the trajectory level, and a cross-sectional warning."""
    t_hits, a_hits = detect(title or "", lex), detect(abstract or "", lex)
    hits = t_hits + a_hits
    return {
        "title": t_hits,
        "abstract": a_hits,
        "level": trajectory_level(hits),
        "cross_sectional": any(h.lexicon == "cross_sectional" for h in hits),
        "terms": sorted({f"{h.lexicon}:{h.label}" for h in hits if not h.lexicon.startswith("query:")}),
    }


def _dedupe(terms) -> list[Term]:
    seen, out = set(), []
    for t in terms:
        key = (re.sub(r"[\s\-]+", " ", t.text), t.prefix)
        if key not in seen:
            seen.add(key)
            out.append(t)
    return out
