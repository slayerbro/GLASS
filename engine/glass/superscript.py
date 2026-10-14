"""Find powers in the image itself, because OCR engines don't read them.

Text recognisers are trained on ordinary text: a small raised "2" after x comes back
as "x?", "x°", "x”", "x2", "x3" or nothing at all — and which one is not even
consistent within a page. So instead of guessing from the text:

  1. per OCR line, find the glyphs in the image (connected ink components);
  2. a glyph that is clearly raised above the line's baseline, smaller than a full
     digit and right after another glyph is an exponent;
  3. each exponent is cut out, enlarged and read on its own (an isolated big digit is
     easy for any OCR engine);
  4. it is put back into the text as "^2" where the engine left junk, a flattened
     digit, or nothing.

An exponent that can't be read is reported, never guessed: the caller re-reads the
selection with the vision model or asks the student to check.
Pure numpy; coordinates are image pixels, the same as the OCR line boxes.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import numpy as np

# what OCR engines leave where a superscript was
JUNK = set("?°”“\"'’‘*^`´¹²³⁰⁴⁵⁶⁷⁸⁹º")
# isolated-glyph misreads -> digit
GLYPH_FIX = str.maketrans({"O": "0", "o": "0", "D": "0", "Q": "0", "l": "1", "I": "1", "|": "1", "i": "1", "!": "1",
                           "Z": "2", "z": "2", "S": "5", "s": "5", "B": "8", "g": "9", "q": "9", "G": "6", "b": "6",
                           "A": "4", "T": "7", "?": "2", "²": "2", "³": "3", "¹": "1", "−": "-", "–": "-"})


@dataclass
class Glyph:
    x0: int
    y0: int
    x1: int  # exclusive
    y1: int  # exclusive (bottom)
    n: int = 0

    @property
    def w(self) -> int:
        return self.x1 - self.x0

    @property
    def h(self) -> int:
        return self.y1 - self.y0

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2


@dataclass
class Exponent:
    line: int  # index into the OCR lines
    box: Glyph  # union of its glyphs, image coordinates
    base_right: int  # right edge of the glyph it belongs to
    cap: float  # height of a full-size character on this line (for spacing decisions)
    minus: bool = False  # a raised minus sign in front (x⁻¹)
    text: str = ""  # what it reads as, once read
    crop: np.ndarray | None = field(default=None, repr=False)


# ------------------------------------------------------------------ image analysis
def ink_mask(gray: np.ndarray) -> np.ndarray:
    """Text pixels, for dark-on-light and light-on-dark alike."""
    bg = float(np.median(gray))
    dev = np.abs(gray.astype(np.float32) - bg)
    top = float(np.percentile(dev, 99.5)) if dev.size else 0.0
    if top < 30:
        return np.zeros(gray.shape, bool)
    return dev > max(40.0, 0.45 * top)


def components(mask: np.ndarray) -> list[Glyph]:
    """8-connected components via run-length union-find (fast enough for line crops)."""
    parent: list[int] = []

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    runs: list[tuple[int, int, int, int]] = []  # (row, start, end_exclusive, id)
    prev: list[tuple[int, int, int]] = []
    for y in range(mask.shape[0]):
        row = mask[y]
        if not row.any():
            prev = []
            continue
        d = np.diff(np.concatenate(([0], row.view(np.int8), [0])))
        starts, ends = np.flatnonzero(d == 1), np.flatnonzero(d == -1)
        cur = []
        for s, e in zip(starts.tolist(), ends.tolist()):
            rid = len(parent)
            parent.append(rid)
            for ps, pe, pid in prev:
                if ps <= e and pe >= s:  # overlap incl. diagonal neighbours
                    ra, rb = find(rid), find(pid)
                    if ra != rb:
                        parent[ra] = rb
            cur.append((s, e, rid))
            runs.append((y, s, e, rid))
        prev = cur
    boxes: dict[int, Glyph] = {}
    for y, s, e, rid in runs:
        r = find(rid)
        g = boxes.get(r)
        if g is None:
            boxes[r] = Glyph(s, y, e, y + 1, e - s)
        else:
            g.x0, g.x1, g.y0, g.y1, g.n = min(g.x0, s), max(g.x1, e), min(g.y0, y), max(g.y1, y + 1), g.n + e - s
    return sorted(boxes.values(), key=lambda g: g.x0)


def merge_stacked(glyphs: list[Glyph]) -> list[Glyph]:
    """Join pieces of one character stacked vertically: i/j dots, '=', '÷', ':'."""
    out: list[Glyph] = []
    for g in glyphs:
        for o in out:
            ov = min(o.x1, g.x1) - max(o.x0, g.x0)
            if ov > 0.6 * min(o.w, g.w):
                o.x0, o.x1, o.y0, o.y1, o.n = min(o.x0, g.x0), max(o.x1, g.x1), min(o.y0, g.y0), max(o.y1, g.y1), o.n + g.n
                break
        else:
            out.append(Glyph(g.x0, g.y0, g.x1, g.y1, g.n))
    return sorted(out, key=lambda g: g.x0)


def find_exponents(gray: np.ndarray, lines: list[dict]) -> list[Exponent]:
    """Raised small glyphs right after another glyph, per OCR line."""
    H, W = gray.shape[:2]
    found: list[Exponent] = []
    for li, l in enumerate(lines):
        lh = max(4.0, float(l["h"]))
        x0, x1 = max(0, int(l["x"] - 2)), min(W, int(l["x"] + l["w"] + 0.9 * lh))  # a dropped power can sit past the box
        y0, y1 = max(0, int(l["y"] - 0.45 * lh)), min(H, int(l["y"] + lh * 1.15))
        if x1 - x0 < 4 or y1 - y0 < 4:
            continue
        crop = gray[y0:y1, x0:x1]
        gl = [g for g in merge_stacked(components(ink_mask(crop))) if g.n >= 3]
        # ignore what belongs to neighbouring lines: it must reach into this line's box
        box_top, box_bot = l["y"] - y0, l["y"] + lh - y0
        gl = [g for g in gl if g.y1 > box_top + 0.2 * lh and g.y0 < box_bot]
        if len(gl) < 2:
            continue
        hs = np.array([g.h for g in gl], float)
        cap = float(np.percentile(hs, 90))
        if cap < 5:
            continue
        body = [g for g in gl if g.h >= 0.35 * cap]
        base = float(np.median([g.y1 for g in body])) if body else float(np.median([g.y1 for g in gl]))

        def raised(g: Glyph) -> bool:
            return base - g.y1 >= 0.26 * cap and 0.42 * cap <= g.h <= 0.86 * cap and g.h >= 1.15 * g.w and g.y0 < base - 0.55 * cap

        def raised_minus(g: Glyph) -> bool:
            return base - g.y1 >= 0.3 * cap and g.h <= 0.22 * cap and g.w >= 1.6 * g.h

        i = 0
        while i < len(gl):
            g = gl[i]
            if not raised(g):
                i += 1
                continue
            # it must follow something on the line (a base): "x²", ")²", "5²"
            left = [o for o in gl[:i] if not raised(o) and not raised_minus(o)]
            minus = bool(gl[i - 1:i] and raised_minus(gl[i - 1]) and g.x0 - gl[i - 1].x1 < 0.35 * cap)
            anchor = left[-1] if left else None
            start = gl[i - 1] if minus else g
            if anchor is None or start.x0 - anchor.x1 > 0.6 * cap or anchor.y1 < base - 0.3 * cap:
                i += 1
                continue
            group = [g]
            j = i + 1
            while j < len(gl) and raised(gl[j]) and gl[j].x0 - group[-1].x1 < 0.4 * cap:  # "10", "12"
                group.append(gl[j])
                j += 1
            u = Glyph(min(x.x0 for x in group), min(x.y0 for x in group), max(x.x1 for x in group), max(x.y1 for x in group))
            pad = max(2, int(0.15 * cap))
            cy0, cy1 = max(0, u.y0 - pad), min(crop.shape[0], u.y1 + pad)
            cx0, cx1 = max(0, u.x0 - pad), min(crop.shape[1], u.x1 + pad)
            found.append(Exponent(li, Glyph(u.x0 + x0, u.y0 + y0, u.x1 + x0, u.y1 + y0), anchor.x1 + x0, cap, minus,
                                  crop=crop[cy0:cy1, cx0:cx1].copy()))
            i = j
    return found


def _normalised(e: Exponent, height: int) -> np.ndarray:
    from PIL import Image

    c = e.crop.astype(np.float32)
    if np.median(c) < 128:  # light text on a dark slide
        c = 255 - c
    c = np.clip((c - c.min()) * 255.0 / max(1.0, c.max() - c.min()), 0, 255).astype(np.uint8)
    im = Image.fromarray(c)
    w = max(1, int(im.width * height / max(1, im.height)))
    return np.array(im.resize((w, height), Image.BICUBIC))


def glyph_sheet(exps: list[Exponent], height: int = 64) -> tuple[np.ndarray, list[tuple[int, int]]]:
    """All exponents enlarged and laid out on ONE line, well apart, dark on white —
    page OCR engines skip lone characters but read a line of numbers reliably.
    Returns (BGRA image, x-range of each exponent)."""
    rows = [_normalised(e, height) for e in exps]
    gap = int(height * 1.6)
    W = sum(r.shape[1] for r in rows) + gap * (len(rows) + 1)
    page = np.full((height * 3, W), 255, np.uint8)
    spans, x = [], gap
    for r in rows:
        page[height:2 * height, x:x + r.shape[1]] = r
        spans.append((x, x + r.shape[1]))
        x += r.shape[1] + gap
    return np.dstack([page, page, page, np.full_like(page, 255)]), spans


def read_sheet_line(lines: list[dict], spans: list[tuple[int, int]]) -> list[str]:
    """Map what the OCR engine read on the glyph sheet back to each exponent. Engines
    sometimes merge neighbours ("223"): a word covering several exponents with one
    character each is split between them."""
    words = [w for l in lines for w in (l.get("words") or [])]
    out = [""] * len(spans)
    if words:
        for w in sorted(words, key=lambda w: w["x"]):
            slots = [i for i, (a, b) in enumerate(spans) if w["x"] - 10 <= (a + b) / 2 <= w["x"] + w["w"] + 10]
            chars = w["text"].replace(" ", "")
            if len(slots) > 1 and len(chars) == len(slots):
                for i, c in zip(slots, chars):
                    out[i] += c
            elif slots:
                out[slots[0]] += chars
        return out
    toks = " ".join(l["text"] for l in sorted(lines, key=lambda l: l["x"])).split()
    if len(toks) != len(spans) and len("".join(toks)) == len(spans):
        toks = list("".join(toks))
    return toks if len(toks) == len(spans) else out


# ------------------------------------------------------------------ reading a digit without any OCR engine
# Fallback when the OCR engine skips a lone character: compare the exponent with the
# digits 0-9 drawn in the fonts installed on this computer (slides and worksheets use
# these same fonts). A reading is only accepted when it is clearly better than the rest.
FONT_FILES = [
    "C:/Windows/Fonts/segoeui.ttf", "C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/calibri.ttf",
    "C:/Windows/Fonts/times.ttf", "C:/Windows/Fonts/cambria.ttc", "C:/Windows/Fonts/verdana.ttf",
    "C:/Windows/Fonts/tahoma.ttf", "C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/arialbd.ttf",
    "/System/Library/Fonts/Helvetica.ttc", "/System/Library/Fonts/SFNS.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf", "/System/Library/Fonts/Supplemental/Times New Roman.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf", "/usr/share/fonts/truetype/crosextra/Carlito-Regular.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSans.ttf",
]
_GW, _GH = 16, 24
_templates: dict[tuple, list[tuple[str, np.ndarray, float]]] = {}


def _bitmap(mask: np.ndarray) -> tuple[np.ndarray, float] | None:
    from PIL import Image

    ys, xs = np.nonzero(mask)
    if len(ys) < 3:
        return None
    m = mask[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    im = Image.fromarray((m * 255).astype(np.uint8)).resize((_GW, _GH), Image.BILINEAR)
    return np.asarray(im, np.float32) / 255.0, m.shape[0] / max(1, m.shape[1])


def _font_templates(fonts: list[str] | None = None) -> list[tuple[str, np.ndarray, float]]:
    import os

    from PIL import Image, ImageDraw, ImageFont

    files = tuple(f for f in (fonts or FONT_FILES) if os.path.exists(f))
    if files not in _templates:
        out = []
        for f in (*files, None):  # None: Pillow's own sans font, available everywhere
            try:
                font = ImageFont.truetype(f, 64) if f else ImageFont.load_default(size=64)
            except Exception:
                continue
            for ch in "0123456789":
                im = Image.new("L", (90, 100), 0)
                ImageDraw.Draw(im).text((10, 5), ch, font=font, fill=255)
                b = _bitmap(np.asarray(im) > 110)
                if b:
                    out.append((ch, *b))
        _templates[files] = out
    return _templates[files]


def classify_digits(crop: np.ndarray, fonts: list[str] | None = None) -> str:
    """Read an exponent crop ("2", "10") by comparing with font-drawn digits; "" if unsure."""
    temps = _font_templates(fonts)
    if not temps:
        return ""
    glyphs = [g for g in merge_stacked(components(ink_mask(crop))) if g.n >= 3]
    mask = ink_mask(crop)
    out = ""
    for g in glyphs[:3]:
        b = _bitmap(mask[g.y0:g.y1, g.x0:g.x1])
        if b is None:
            return ""
        bm, asp = b
        best: dict[str, float] = {}
        for ch, t, tasp in temps:
            a, c = bm - bm.mean(), t - t.mean()
            score = float((a * c).sum() / (np.sqrt((a * a).sum() * (c * c).sum()) + 1e-6))
            score -= 0.35 * abs(np.log(asp / tasp))
            best[ch] = max(best.get(ch, -1.0), score)
        ranked = sorted(best.items(), key=lambda kv: -kv[1])
        if ranked[0][1] < 0.55 or ranked[0][1] - ranked[1][1] < 0.04:
            return ""
        out += ranked[0][0]
    return out


def clean_reading(s: str) -> str:
    s = s.strip().translate(GLYPH_FIX).replace(" ", "")
    return s if re.fullmatch(r"-?(\d{1,3}|n)", s) else ""


# ------------------------------------------------------------------ putting powers back into the text
def _exp_text(e: Exponent, fallback: str = "") -> str:
    t = e.text or fallback
    if not t:
        return ""
    if e.minus and not t.startswith("-"):
        t = "-" + t
    return f"({t})" if t.startswith("-") else t


def _strip_tail(word: str, n_digits: int) -> tuple[str, str]:
    """-> (base, what OCR wrote for the exponent)."""
    i = len(word)
    while i > 0 and word[i - 1] in JUNK and len(word) - i < 3:
        i -= 1
    if i < len(word):
        return word[:i], word[i:]
    if n_digits and len(word) > n_digits and word[-n_digits:].isdigit():
        return word[:-n_digits], word[-n_digits:]
    return word, ""


def apply(lines: list[dict], exps: list[Exponent]) -> tuple[list[str], list[str]]:
    """Rewrite line texts with the exponents found. -> (fixed, unreadable) as short
    strings for the UI, e.g. (["x^2", "(x + 3)^2"], ["4a"])."""
    fixed: list[str] = []
    unreadable: list[str] = []
    by_line: dict[int, list[Exponent]] = {}
    for e in exps:
        by_line.setdefault(e.line, []).append(e)
    for li, es in by_line.items():
        l = lines[li]
        words = l.get("words")
        if words:
            _apply_words(l, words, es, fixed, unreadable)
        else:
            _apply_text(l, es, fixed, unreadable)
    return fixed, unreadable


def _apply_words(l: dict, words: list[dict], es: list[Exponent], fixed: list[str], unreadable: list[str]) -> None:
    ws = [dict(w) for w in words]
    for e in sorted(es, key=lambda e: e.box.x0, reverse=True):  # right to left keeps indices valid
        n = len(e.text.strip("-")) if e.text else 1
        # the word the exponent belongs to: the last one starting before it
        before = [k for k, w in enumerate(ws) if w["x"] < e.box.cx]
        if not before:
            continue
        k = before[-1]
        w = ws[k]
        covers = w["x"] + w["w"] >= e.box.x0 + 0.3 * e.box.w  # OCR wrote *something* for the exponent in this word
        base, tail = _strip_tail(w["text"], n) if covers else (w["text"], "")
        own_word = not base  # OCR made the exponent its own word ("x ?"): it joins the word before
        if own_word:
            if k == 0:
                continue
            base = ws[k - 1]["text"]
        if not _mathy_base(base):
            continue
        exp = _exp_text(e, tail if tail.isdigit() else "")
        if not exp:
            unreadable.append(base)
            continue
        if own_word:
            ws.pop(k)
            k -= 1
        ws[k]["text"] = base + "^" + exp
        fixed.append(ws[k]["text"])
    l["text"] = " ".join(w["text"] for w in ws)


# a power belongs to maths: x, 4a, 5, (x+3), cm — not to words like "John's" or "use"
_MATH_BASE = re.compile(r"[0-9A-Za-z()\[\]+\-*/.=×]{1,10}")


def _mathy_base(base: str) -> bool:
    tok = (base.split() or [""])[-1]
    return bool(_MATH_BASE.fullmatch(tok)) and not re.search(r"[A-Za-z]{3,}", tok)


_CAND = re.compile(r"(?<=[A-Za-z0-9)\]])(?:[?°”“\"'’*`´¹²³⁰⁴⁵⁶⁷⁸⁹º]{1,2}|(?<=[a-z)])\d)(?![0-9A-Za-z(])")


def _apply_text(l: dict, es: list[Exponent], fixed: list[str], unreadable: list[str]) -> None:
    """No word boxes (Apple Vision, Tesseract line re-reads): match the exponents, in
    order, to the places in the text where OCR left junk or a flattened digit."""
    text = l["text"]
    cands = list(_CAND.finditer(text))
    if len(cands) != len(es):
        if es:
            unreadable.append(text.strip()[:30])
        return
    out, last = [], 0
    for m, e in zip(cands, sorted(es, key=lambda e: e.box.x0)):
        tok = m.group()
        if not _mathy_base(text[:m.start()]):
            continue
        exp = _exp_text(e, tok if tok.isdigit() else "")
        if not exp:
            unreadable.append(text[max(0, m.start() - 4):m.start()].strip())
            out.append(text[last:m.end()])
        else:
            out.append(text[last:m.start()] + "^" + exp)
            fixed.append((text[max(0, m.start() - 6):m.start()].split() or [""])[-1] + "^" + exp)
        last = m.end()
    out.append(text[last:])
    l["text"] = "".join(out)
