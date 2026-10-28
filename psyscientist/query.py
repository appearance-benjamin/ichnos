"""Boolean search strings.

The protocol holds one canonical string per corpus, written the way the
blueprint writes it: AND / OR / NOT, "quoted phrases", trailing * for
truncation, parentheses. This module parses it once, renders the form each
database expects, and re-checks the string locally against titles and
abstracts so the report can show which terms drive the hits.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass

TOKEN = re.compile(r'"[^"]*"|\(|\)|[^\s()"]+')
OPERATORS = {"AND", "OR", "NOT"}


@dataclass(frozen=True)
class Term:
    text: str       # lowercased, no quotes, no trailing *
    phrase: bool
    prefix: bool    # trailing *: any word that starts with text

    @property
    def label(self) -> str:
        if self.phrase:
            return f'"{self.text}"' + ("*" if self.prefix else "")
        return self.text + ("*" if self.prefix else "")

    def regex(self) -> re.Pattern:
        words = [re.escape(w) for w in re.split(r"[\s\-‐-―]+", self.text) if w]
        body = r"[\s\-‐-―]+".join(words)
        tail = r"\w*" if self.prefix else r"\b"
        return re.compile(r"\b" + body + tail, re.IGNORECASE)


@dataclass(frozen=True)
class Node:
    op: str                  # and, or, not
    children: tuple


class QueryError(ValueError):
    pass


def parse(query: str) -> Node | Term:
    tokens = TOKEN.findall(" ".join(query.split()))
    pos = 0

    def peek() -> str | None:
        return tokens[pos] if pos < len(tokens) else None

    def take() -> str:
        nonlocal pos
        tok = tokens[pos]
        pos += 1
        return tok

    def expr():
        items = [and_expr()]
        while peek() == "OR":
            take()
            items.append(and_expr())
        return items[0] if len(items) == 1 else Node("or", tuple(items))

    def and_expr():
        items = [unary()]
        while peek() == "AND":
            take()
            items.append(unary())
        if peek() not in (None, "OR", ")"):
            raise QueryError(f"missing AND/OR before {peek()!r}: write operators explicitly")
        return items[0] if len(items) == 1 else Node("and", tuple(items))

    def unary():
        if peek() == "NOT":
            take()
            return Node("not", (unary(),))
        return primary()

    def primary():
        tok = peek()
        if tok is None:
            raise QueryError("query ends early")
        if tok == "(":
            take()
            node = expr()
            if peek() != ")":
                raise QueryError("unbalanced parentheses")
            take()
            return node
        if tok == ")" or tok in OPERATORS:
            raise QueryError(f"unexpected {tok!r}")
        take()
        if tok.startswith('"'):
            text = tok.strip('"').strip().lower()
            if not text:
                raise QueryError("empty phrase")
            if "*" in text or "?" in text:
                raise QueryError(f"no wildcards inside phrases (neither source supports them): {tok}")
            return Term(text, phrase=True, prefix=False)
        prefix = tok.endswith("*")
        text = tok.rstrip("*").lower()
        if not text or "*" in text:
            raise QueryError(f"wildcards only at the end of a word: {tok!r}")
        return Term(text, phrase=False, prefix=prefix)

    if not tokens:
        raise QueryError("empty query")
    tree = expr()
    if pos != len(tokens):
        raise QueryError(f"unexpected {tokens[pos]!r}")
    return tree


def _openalex_term(t: Term) -> str:
    if t.phrase:
        # Same hits either way, but hyphenated phrases can make OpenAlex silently
        # return 0 once another phrase is added (seen 2026-10-16).
        return '"' + re.sub(r"[\-‐-―]+", " ", t.text) + '"'
    return t.label


@dataclass(frozen=True)
class Dialect:
    name: str                          # where the string is pasted, for people
    and_: str = " AND "
    or_: str = " OR "
    not_: str = "NOT "                 # prefix negation: "a AND NOT b", or "-b"
    binary_not: str | None = None      # binary negation instead, e.g. " NOT " in "a NOT b"
    term: Callable[[Term], str] | None = None      # default: Term.label
    field: Callable[[str], str] | None = None      # wraps "(a OR b)" in title-and-abstract fields


# API sources first, then the databases searched by hand through the library.
DIALECTS = {
    "openalex": Dialect("OpenAlex (title_and_abstract.search.exact)", term=_openalex_term),
    "s2": Dialect("Semantic Scholar bulk search", and_=" + ", or_=" | ", not_="-"),
    "pubmed": Dialect("PubMed", binary_not=" NOT ", term=lambda t: t.label + "[tiab]"),
    "europepmc": Dialect("Europe PMC", term=lambda t: "TITLE_ABS:" + t.label),
    "eric": Dialect("ERIC API", binary_not=" NOT ", field=lambda g: f"(title:{g} OR description:{g})"),
    "ebsco": Dialect("APA PsycINFO on EBSCOhost (Advanced Search, one box)", binary_not=" NOT ",
                     field=lambda g: f"(TI {g} OR AB {g})"),
    "ovid": Dialect("APA PsycINFO on Ovid (Advanced Search, one line)", and_=" and ", or_=" or ",
                    binary_not=" not ", field=lambda g: f"{g}.ti,ab."),
    "scopus": Dialect("Scopus (Advanced document search)", field=lambda g: f"TITLE-ABS{g}"),
    "wos": Dialect("Web of Science (Advanced Search query builder)", binary_not=" NOT ",
                   field=lambda g: f"(TI={g} OR AB={g})"),
}
LIBRARY_DIALECTS = ("ebsco", "ovid", "scopus", "wos")


def render(tree: Node | Term, dialect: str) -> str:
    """Render for one source or database, every term restricted to titles and abstracts:
    by the source itself (openalex, s2), by field tags (pubmed, europepmc), or by wrapping
    each OR-group of terms in the database's title and abstract fields (library dialects)."""
    d = DIALECTS[dialect]
    term = d.term or (lambda t: t.label)

    def field_group(group: list[Term]) -> str:
        return d.field("(" + d.or_.join(term(t) for t in group) + ")")

    def go(node, parent: str | None) -> str:
        if isinstance(node, Term):
            return field_group([node]) if d.field else term(node)
        if d.field and node.op == "or" and all(isinstance(c, Term) for c in node.children):
            return field_group(list(node.children))
        if node.op == "not":
            if d.binary_not and parent != "and":
                raise QueryError(f"{d.name} needs NOT after another term, as in: a AND NOT b")
            return d.not_ + go(node.children[0], "not")
        if node.op == "and" and d.binary_not:
            inner = go(node.children[0], "and")
            for c in node.children[1:]:
                if isinstance(c, Node) and c.op == "not":
                    inner += d.binary_not + go(c.children[0], "not")
                else:
                    inner += d.and_ + go(c, "and")
        else:
            inner = (d.and_ if node.op == "and" else d.or_).join(go(c, node.op) for c in node.children)
        return f"({inner})" if parent is not None else inner

    return go(tree, None)


def matches(tree: Node | Term, text: str) -> bool:
    if isinstance(tree, Term):
        return bool(tree.regex().search(text))
    if tree.op == "and":
        return all(matches(c, text) for c in tree.children)
    if tree.op == "or":
        return any(matches(c, text) for c in tree.children)
    return not matches(tree.children[0], text)


def terms(tree: Node | Term) -> list[Term]:
    if isinstance(tree, Term):
        return [tree]
    return [t for c in tree.children for t in terms(c)]


def blocks(tree: Node | Term) -> list[Node | Term]:
    """The top-level AND blocks (concept groups) of the string."""
    if isinstance(tree, Node) and tree.op == "and":
        return list(tree.children)
    return [tree]
