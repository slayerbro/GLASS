"""Study texts: the student's own copies of set texts, notes and revision guides.

Why: a small local model cannot recite a novel. Asked for quotations from a set
text it either refuses (unhelpful) or invents them (dangerous in class). With the
book added, GLASS retrieves the most relevant passages for each question, the
model may only quote from those, and every quotation in an answer is checked
word-for-word against the book before it is shown as verified — the same idea as
SymPy for maths.

Formats: PDF, EPUB, DOCX, TXT/MD. Text stays on this computer (SQLite FTS5).
"""
from __future__ import annotations

import html
import logging
import re
import unicodedata
import zipfile
from html.parser import HTMLParser
from pathlib import Path

log = logging.getLogger("glass.texts")

CHAPTER_RE = re.compile(r"^\s*((chapter|part|book|act|scene|section|stave|canto|letter|volume)\s+[\w.-]+|[IVXLC]+\.?)"
                        r"\s*[:.\-–]?\s*.{0,60}$", re.I)


# ---------------------------------------------------------------------------- extraction
class _HTMLText(HTMLParser):
    BLOCK = {"p", "div", "br", "h1", "h2", "h3", "h4", "li", "tr", "section", "blockquote"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head"):
            self._skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head"):
            self._skip = max(0, self._skip - 1)
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def _html_to_text(markup: str) -> str:
    p = _HTMLText()
    p.feed(markup)
    return html.unescape("".join(p.parts))


def extract_text(path: Path) -> str:
    ext = path.suffix.lower()
    if ext == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        pages = []
        for page in reader.pages:
            try:
                pages.append(page.extract_text() or "")
            except Exception:
                pages.append("")
        text = "\n\n".join(pages)
        if len(text.strip()) < 200:
            raise ValueError("This PDF has no selectable text (it is probably scanned images). "
                             "Use a text PDF or an ebook (EPUB) instead.")
        return text
    if ext == ".epub":
        out = []
        with zipfile.ZipFile(path) as z:
            names = [n for n in z.namelist() if n.lower().endswith((".xhtml", ".html", ".htm"))]
            for n in sorted(names, key=_natural):
                out.append(_html_to_text(z.read(n).decode("utf-8", errors="replace")))
        return "\n\n".join(out)
    if ext == ".docx":
        with zipfile.ZipFile(path) as z:
            xml = z.read("word/document.xml").decode("utf-8", errors="replace")
        xml = re.sub(r"</w:p>", "\n", xml)
        return html.unescape(re.sub(r"<[^>]+>", "", xml))
    if ext in (".txt", ".md", ".text"):
        raw = path.read_bytes()
        for enc in ("utf-8-sig", "utf-16", "cp1252", "latin-1"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                continue
    raise ValueError(f"Unsupported file type {ext}. Use PDF, EPUB, DOCX or TXT.")


def _natural(s: str):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]


def chunk_text(text: str, size: int = 700, overlap: int = 120) -> list[tuple[str, str]]:
    """[(chapter, passage)] — paragraph-aware passages of ~size chars."""
    text = text.replace("\r", "")
    paras = [p.strip() for p in re.split(r"\n\s*\n|\n(?=\s{2,})", text) if p.strip()]
    if len(paras) < 3:  # one-line-per-paragraph files
        paras = [p.strip() for p in text.split("\n") if p.strip()]
    chunks: list[tuple[str, str]] = []
    chapter = ""
    buf = ""
    for p in paras:
        p = re.sub(r"[ \t]+", " ", p)
        if len(p) < 70 and CHAPTER_RE.match(p):
            if buf:
                chunks.append((chapter, buf.strip()))
                buf = ""
            chapter = p[:70]
            continue
        if len(buf) + len(p) > size and buf:
            chunks.append((chapter, buf.strip()))
            buf = buf[-overlap:] + " "
        buf += p + "\n"
    if buf.strip():
        chunks.append((chapter, buf.strip()))
    return chunks


# ---------------------------------------------------------------------------- quotes
# an opening mark must start a word run (not the apostrophe in "don't"), a closing one must end it
_QUOTE_RE = re.compile(r"""(?:^|(?<=[\s(\[:—–-]))["“‘'](?P<q>[^"“”]{12,300}?)["”’'](?=[\s.,;:!?)\]—–-]|$)""")


def normalise(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).lower()
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"').replace("—", "-").replace("–", "-")
    s = re.sub(r"[^\w' -]+", " ", s)  # punctuation differences never fail a real quote
    return re.sub(r"\s+", " ", s).strip()


def find_quotes(answer_text: str) -> list[str]:
    """Quotations (≥3 words) a model put in an answer."""
    out = []
    for m in _QUOTE_RE.finditer(answer_text or ""):
        q = m.group("q").strip(" ,.;:!?")
        if len(q.split()) >= 3 and q not in out:
            out.append(q)
    return out


def quote_in(quote: str, corpus_norm: str) -> bool:
    """Exact (punctuation/case-insensitive) match; '...' elisions allowed between parts."""
    parts = [normalise(p) for p in re.split(r"\.\.\.|…", quote) if normalise(p)]
    if not parts:
        return False
    pos = 0
    for part in parts:
        i = corpus_norm.find(part, pos)
        if i < 0:
            return False
        pos = i + len(part)
    return True


QUOTE_REQUEST = re.compile(r"\b(quotes?|quotations?|textual evidence|evidence from the (text|book|novel|poem|play)|"
                           r"which (line|words?|phrase)|find (a|the) (line|phrase|word)|lines? (that|which|where))\b", re.I)
