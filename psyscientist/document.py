"""A manuscript as a readable HTML document: the Markdown the pipeline writes, typeset for reading.

Handles what the renderer produces: headings, paragraphs, lists, tables, bold, italics, inline code and
links. No external files: the page opens from disk, from the app, or attached to an e-mail.
"""

from __future__ import annotations

import html
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

STYLE = """
:root { --bg: #fbfbf8; --ink: #1d1d1f; --muted: #66666c; --line: #deded8; --head: #f0f0ea; --accent: #2f4fa8; }
@media (prefers-color-scheme: dark) {
  :root { --bg: #1c1c1e; --ink: #ececec; --muted: #a0a0a6; --line: #3a3a3e; --head: #2a2a2e; --accent: #8fa8ff; }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink);
  font: 17px/1.65 Georgia, "Times New Roman", serif; }
main { max-width: 46rem; margin: 0 auto; padding: 40px 20px 80px; }
h1 { font-size: 1.75rem; line-height: 1.3; margin: 0 0 1rem; }
h2 { font-size: 1.3rem; margin: 2.4rem 0 .8rem; padding-top: .6rem; border-top: 1px solid var(--line); }
h3 { font-size: 1.08rem; margin: 1.8rem 0 .5rem; }
p { margin: 0 0 1rem; }
a { color: var(--accent); overflow-wrap: anywhere; }
code { font: .85em ui-monospace, Consolas, monospace; background: var(--head); padding: 1px 4px; border-radius: 4px;
  overflow-wrap: anywhere; }
ul { padding-left: 1.3rem; }
li { margin: .25rem 0; }
.wide { overflow-x: auto; margin: 0 0 1.4rem; }
table { border-collapse: collapse; font: 13px/1.4 system-ui, -apple-system, "Segoe UI", sans-serif; min-width: 100%; }
th, td { border-bottom: 1px solid var(--line); padding: 5px 7px; text-align: left; vertical-align: top; }
th { background: var(--head); }
td.num, th.num { text-align: right; }
.flag { color: #c0392b; }
@media (max-width: 640px) { body { font-size: 16px; } main { padding: 24px 16px 60px; } }
@media print { body { background: #fff; color: #000; } main { max-width: none; } a { color: #000; } }
"""


def inline(text: str) -> str:
    out = html.escape(text, quote=False)
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
    out = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?![\w*])", r"<em>\1</em>", out)
    out = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2" target="_blank" rel="noopener">\1</a>', out)
    out = re.sub(r"(?<![\"'>=])(https?://[^\s<)]+[^\s<).,;])", r'<a href="\1">\1</a>', out)
    return out.replace("[⚠]", '<span class="flag">[⚠]</span>')


def _row(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


PAPER_STYLE = """
@page { size: A4; margin: 2.3cm 2.2cm 2.4cm;
  @bottom-center { content: counter(page); font: 9pt "Times New Roman", Times, serif; color: #444; } }
* { box-sizing: border-box; }
body { margin: 0; background: #fff; color: #000; font: 11pt/1.45 "Times New Roman", Times, serif; }
main { padding: 0; }
h1 { font-size: 16pt; line-height: 1.25; text-align: center; margin: 0 0 12pt; }
.authors { text-align: center; margin: 0 0 14pt; }
.authors .name { font-size: 12pt; }
.authors .aff { font-size: 10.5pt; font-style: italic; }
p { margin: 0 0 6pt; text-align: justify; hyphens: auto; }
p.note { font-size: 9pt; line-height: 1.35; border-top: .5pt solid #888; border-bottom: .5pt solid #888;
  padding: 5pt 0; margin: 0 0 14pt; }
h2 { font-size: 12pt; margin: 16pt 0 6pt; break-after: avoid; }
h2.abstract-head { font-size: 10.5pt; text-align: center; text-transform: uppercase; letter-spacing: .06em; }
p.abstract, p.keywords { margin-left: 1.1cm; margin-right: 1.1cm; font-size: 10pt; }
p.keywords { margin-bottom: 14pt; }
h3 { font-size: 11pt; font-style: italic; margin: 10pt 0 4pt; break-after: avoid; }
p.caption { font-size: 9.5pt; text-align: left; margin-top: 10pt; break-after: avoid; }
a { color: #000; text-decoration: none; overflow-wrap: anywhere; }
code { font: 8.5pt Consolas, monospace; overflow-wrap: anywhere; }
ul { padding-left: 14pt; margin: 0 0 8pt; }
li { margin: 1pt 0; }
ul.refs { list-style: none; padding: 0; }
ul.refs li { padding-left: 1.4em; text-indent: -1.4em; font-size: 9.5pt; text-align: left; margin: 0 0 3pt; }
.wide { margin: 0 0 10pt; }
table { border-collapse: collapse; width: 100%; font: 7.5pt/1.3 "Arial Narrow", Arial, sans-serif;
  border-top: 1pt solid #000; border-bottom: 1pt solid #000; }
thead { display: table-header-group; }
th { text-align: left; border-bottom: .6pt solid #000; padding: 2pt 4pt; }
td { padding: 1.5pt 4pt; vertical-align: top; }
td.num, th.num { text-align: right; }
tr { break-inside: avoid; }
"""


def _authors(text: str) -> str:
    out = []
    for a in text.split("; "):
        m = re.fullmatch(r"(.+?)\s*\((.+)\)", a.strip())
        name, aff = (m.group(1), m.group(2)) if m else (a.strip(), "")
        out.append(f'<div class="name">{inline(name)}</div>' + (f'<div class="aff">{inline(aff)}</div>' if aff else ""))
    return '<div class="authors">' + "".join(out) + "</div>"


FLOW_HEAD = ["Stage", "Records", "Excluded or set aside"]
FLOW_STYLE = """
.flow { margin: 0.6em 0 1.2em; font-size: 0.86em; }
.flow .row { display: grid; grid-template-columns: 7.5em 1fr 2.2em 1fr; align-items: center; }
.flow .stage { font-weight: 600; color: #555; padding-right: 0.6em; }
.flow .box { border: 1px solid #333; padding: 0.45em 0.6em; background: #fff; }
.flow .side { border: 1px solid #999; padding: 0.4em 0.6em; color: #333; background: #f6f6f6; }
.flow .to { text-align: center; color: #333; }
.flow .down { display: grid; grid-template-columns: 7.5em 1fr 2.2em 1fr; }
.flow .down span { text-align: center; color: #333; line-height: 1.3em; }
"""


def _flow(stages: list[list[str]]) -> str:
    """A PRISMA flow diagram from the rows of the flow table (stage, records, excluded or set aside)."""
    out = ['<div class="flow">']
    for k, s in enumerate(stages):
        stage, records, side = (s + ["", "", ""])[:3]
        if k:
            out.append('<div class="down"><span></span><span>&darr;</span><span></span><span></span></div>')
        gone = side.strip() not in ("", "–", "-")
        out.append(f'<div class="row"><div class="stage">{inline(stage)}</div><div class="box">{inline(records)}</div>'
                   + (f'<div class="to">&rarr;</div><div class="side">{inline(side)}</div>' if gone else
                      "<div></div><div></div>") + "</div>")
    return "\n".join(out + ["</div>"])


def to_html(markdown: str, title: str = "Manuscript", layout: str = "screen") -> str:
    """layout "screen" for reading on a screen; "paper" for a journal-style page to print or save as PDF."""
    paper = layout == "paper"
    body, para, items = [], [], []
    section = [""]

    def flush():
        if para:
            text = " ".join(para)
            cls = ""
            if paper and text.startswith("**Authors:**"):
                body.append(_authors(text[len("**Authors:**"):].strip()))
                para.clear()
                return
            if paper:
                if text.startswith("**Declaration of AI use:**"):
                    cls = "note"
                elif text.startswith("**Keywords:**"):
                    cls = "keywords"
                elif re.match(r"\*\*(Table|Figure) S?\d+\.\*\*", text):
                    cls = "caption"
                elif section[0] == "Abstract":
                    cls = "abstract"
            body.append((f'<p class="{cls}">' if cls else "<p>") + inline(text) + "</p>")
            para.clear()
        if items:
            refs = paper and section[0] == "References"
            body.append(('<ul class="refs">' if refs else "<ul>") + "".join(f"<li>{inline(i)}</li>" for i in items)
                        + "</ul>")
            items.clear()

    lines = markdown.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        m = re.match(r"(#{1,4})\s+(.*)", line)
        if m:
            flush()
            n = len(m.group(1))
            if n == 2:
                section[0] = m.group(2).strip()
            cls = ' class="abstract-head"' if paper and n == 2 and section[0] == "Abstract" else ""
            body.append(f"<h{n}{cls}>{inline(m.group(2))}</h{n}>")
        elif line.startswith("|") and i + 1 < len(lines) and re.match(r"\|\s*-", lines[i + 1]):
            flush()
            head, aligns = _row(line), _row(lines[i + 1])
            if head == FLOW_HEAD and body and "<p" in body[-1] and "<strong>Figure" in body[-1]:
                # the flow of records, drawn as a flow diagram: each stage a box, what left it beside it
                i += 2
                stages = []
                while i < len(lines) and lines[i].startswith("|"):
                    stages.append(_row(lines[i]))
                    i += 1
                body.append(_flow(stages))
                continue
            right = [a.endswith(":") for a in aligns]
            rows = []
            i += 2
            while i < len(lines) and lines[i].startswith("|"):
                rows.append(_row(lines[i]))
                i += 1
            cls = lambda k: ' class="num"' if k < len(right) and right[k] else ""  # noqa: E731
            body.append('<div class="wide"><table><thead><tr>' + "".join(f"<th{cls(k)}>{inline(c)}</th>"
                                                                        for k, c in enumerate(head))
                        + "</tr></thead><tbody>" + "".join("<tr>" + "".join(f"<td{cls(k)}>{inline(c)}</td>"
                                                                            for k, c in enumerate(r)) + "</tr>"
                                                         for r in rows) + "</tbody></table></div>")
            continue
        elif line.startswith("- "):
            if para:
                flush()
            items.append(line[2:])
        elif not line.strip():
            flush()
        elif items:
            items[-1] += " " + line.strip()
        else:
            para.append(line.strip())
        i += 1
    flush()
    return ("<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            f"<title>{html.escape(title)}</title><style>{(PAPER_STYLE if paper else STYLE) + FLOW_STYLE}</style>"
            "</head><body><main>\n"
            + "\n".join(body) + "\n</main></body></html>\n")


# PDF: a Chromium-based browser prints the paper layout; Chrome or Edge is on most computers.

BROWSERS = [
    r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
    r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
    r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser",
]


def find_browser() -> str | None:
    for b in BROWSERS:
        path = os.path.expandvars(b)
        if "%" not in path and os.path.exists(path):
            return path
    return shutil.which("chrome") or shutil.which("msedge") or shutil.which("chromium")


def to_pdf(markdown: str, title: str, out: Path) -> Path:
    """Write `out` (a .pdf) from the paper layout, printed by a headless Chromium browser."""
    browser = find_browser()
    if not browser:
        raise RuntimeError("no Chrome or Edge found to print the PDF; open the .html file and print it to PDF instead")
    out = Path(out).resolve()                  # the browser resolves a relative path against its own folder
    page = out.with_suffix(".print.html")
    page.write_text(to_html(markdown, title, layout="paper"), encoding="utf-8")
    try:
        with tempfile.TemporaryDirectory() as profile:
            subprocess.run([browser, "--headless=new", "--disable-gpu", "--no-pdf-header-footer",
                            f"--user-data-dir={profile}", f"--print-to-pdf={out}", page.resolve().as_uri()],
                           check=True, timeout=180, capture_output=True)
    finally:
        page.unlink(missing_ok=True)
    if not out.exists() or out.stat().st_size == 0:
        raise RuntimeError("the browser did not write the PDF")
    return out
