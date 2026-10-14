"""Deterministic maths: extraction from lesson text and exact solving with SymPy.

Language models are bad at arithmetic, so GLASS never trusts them with it:
  * `find_problems(text)` pulls equations / expressions out of OCR or speech.
  * `MathService.run(expr)` evaluates a (possibly LLM-written) SymPy expression
    in a separate, killable worker process with a strict whitelist and timeout.
"""
from __future__ import annotations

import logging
import multiprocessing as mp
import re
import threading
import time
from dataclasses import dataclass, field

log = logging.getLogger("glass.math")

# --------------------------------------------------------------------------- text normalisation
_UNICODE = {
    "×": "*", "·": "*", "÷": "/", "−": "-", "–": "-", "—": "-", "√": "sqrt",
    "π": "pi", "≤": "<=", "≥": ">=", "＝": "=", "⁄": "/", "°": "",
    "½": "(1/2)", "⅓": "(1/3)", "⅔": "(2/3)", "¼": "(1/4)", "¾": "(3/4)", "⅕": "(1/5)", "⅛": "(1/8)",
}
# a run of superscript characters is one exponent: x¹² -> x^12, x⁻¹ -> x^(-1)
_SUPER = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺ⁿˣ", "0123456789-+nx")
_SUPER_RUN = re.compile(r"[⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺ⁿˣ]+")
_WORD_OPS = [
    (r"\bsquared\b", "^2"), (r"\bcubed\b", "^3"), (r"\btimes\b", "*"), (r"\bmultiplied by\b", "*"),
    (r"\bdivided by\b", "/"), (r"\bover\b", "/"), (r"\bplus\b", "+"), (r"\bminus\b", "-"),
    (r"\bequals\b", "="), (r"\bis equal to\b", "="),
]


def normalise(text: str) -> str:
    def sup(m: re.Match) -> str:
        p = m.group().translate(_SUPER)
        return f"^{p}" if p.isalnum() else f"^({p})"

    text = _SUPER_RUN.sub(sup, text)
    for k, v in _UNICODE.items():
        text = text.replace(k, v)
    return text


# OCR flattens superscripts: "x²" often comes back as "x2". A single letter (or a
# closing bracket) glued to a digit inside maths is almost always a power.
_FLAT_POWER = re.compile(r"(?<![A-Za-z^_])([a-z]|\))([2-9])(?![0-9a-z.(])")
_MATHY_LINE = re.compile(r"[=+\-*/^]|\b(solve|simplify|expand|factori[sz]e|evaluate|differentiate|find)\b", re.I)


def flat_powers(text: str) -> list[str]:
    """Tokens like "x2" or "(x+1)3" in maths lines that are probably powers the OCR
    flattened. Shown to the student to check — never silently rewritten."""
    out: list[str] = []
    for line in text.splitlines():
        if not _MATHY_LINE.search(line):
            continue
        body = re.sub(r"^\s*(?:Q(?:uestion)?\s*)?\d+\s*\(?[a-h]?\)?\s*[.):]?\s*", "", line, flags=re.I)
        for m in _FLAT_POWER.finditer(body):
            tok = m.group(1) + m.group(2)
            if tok not in out:
                out.append(tok)
    return out


SAFE_CHARS = re.compile(r"^[\w\s+\-*/^().,=<>!\[\]{}:]*$")
FORBIDDEN = re.compile(r"__|import|lambda|exec|eval|open|globals|locals|getattr|setattr|compile|input|os\b|sys\b|subprocess|[A-Za-z_]\s*\.\s*[A-Za-z_]|['\"`;\\@$#]")

ALLOWED_FUNCS = {
    "solve", "Eq", "simplify", "expand", "factor", "diff", "integrate", "sqrt", "sin", "cos", "tan",
    "asin", "acos", "atan", "sind", "cosd", "tand", "asind", "acosd", "atand", "pi", "E", "exp", "log", "ln",
    "Rational", "N", "nsimplify", "Abs", "abs", "round", "rnd", "sigfig", "Symbol", "symbols", "linsolve",
    "nsolve", "limit", "oo", "summation", "Matrix", "gcd", "lcm", "factorint", "isprime", "binomial",
    "factorial", "ceiling", "floor", "Max", "Min", "mean", "median", "stdev", "percent", "cancel", "apart",
    "together", "collect", "roots", "degrees", "radians", "deg", "rad", "solveset", "S", "I", "re", "im",
    "And", "Or", "Interval", "reduce_inequalities", "Poly", "expand_log", "logcombine", "x", "y", "z",
    "a", "b", "c", "n", "m", "t", "k", "p", "q", "r", "s", "u", "v", "w", "h", "d", "f", "g", "j", "l", "o",
}


# --------------------------------------------------------------------------- worker process
def _worker(conn) -> None:  # pragma: no cover - runs in a child process
    import sympy as sp
    from sympy.parsing.sympy_parser import (convert_xor, implicit_multiplication_application,
                                            parse_expr, standard_transformations)

    T = standard_transformations + (implicit_multiplication_application, convert_xor)
    deg = sp.pi / 180

    def sigfig(v, n=3):
        v = float(sp.N(v))
        if v == 0:
            return 0
        from math import floor, log10
        return round(v, -int(floor(log10(abs(v)))) + (n - 1))

    def stats(fn):
        def f(*vals):
            import statistics
            vs = [float(sp.N(v)) for v in (vals[0] if len(vals) == 1 and hasattr(vals[0], "__iter__") else vals)]
            return getattr(statistics, fn)(vs)
        return f

    ns = {name: getattr(sp, name) for name in dir(sp) if name in ALLOWED_FUNCS}
    ns.update({
        "sind": lambda v: sp.sin(v * deg), "cosd": lambda v: sp.cos(v * deg), "tand": lambda v: sp.tan(v * deg),
        "asind": lambda v: sp.asin(v) / deg, "acosd": lambda v: sp.acos(v) / deg, "atand": lambda v: sp.atan(v) / deg,
        "ln": sp.log, "abs": sp.Abs, "round": lambda v, n=0: sp.Float(round(float(sp.N(v)), int(n))),
        "rnd": lambda v, n=2: sp.Float(round(float(sp.N(v)), int(n))), "sigfig": sigfig,
        "mean": stats("mean"), "median": stats("median"), "stdev": stats("pstdev"),
        "percent": lambda p, v: sp.nsimplify(p) / 100 * v, "deg": lambda v: v * 180 / sp.pi,
        "rad": lambda v: v * deg, "degrees": lambda v: v * 180 / sp.pi, "radians": lambda v: v * deg,
    })
    for s in "xyzabcnmtkpqrsuvwhdfgjlo":
        ns[s] = sp.Symbol(s)
    ns["E"] = sp.E
    ns["I"] = sp.I
    # constructors the parser itself emits (never reachable from user text: the
    # parent process rejects any name outside ALLOWED_FUNCS)
    ns.update({"Integer": sp.Integer, "Float": sp.Float, "Rational": sp.Rational, "Symbol": sp.Symbol,
               "Function": sp.Function})

    def fmt(v):
        try:
            if isinstance(v, (list, tuple, set, sp.FiniteSet)):
                items = list(v)
                if items and isinstance(items[0], dict):
                    return "; ".join(", ".join(f"{k} = {fmt(val)}" for k, val in d.items()) for d in items)
                return ", ".join(fmt(i) for i in items) if items else "no solution"
            if isinstance(v, dict):
                return ", ".join(f"{k} = {fmt(val)}" for k, val in v.items())
            if isinstance(v, (int, float)):
                v = sp.nsimplify(v) if isinstance(v, int) else sp.Float(v)
            if hasattr(v, "is_number") and v.is_number:
                exact = sp.nsimplify(v)
                num = sp.N(v, 6)
                if exact.is_Integer or exact == v and (exact.is_Rational and exact.q < 1000):
                    if exact.is_Integer:
                        return str(exact)
                    return f"{exact} ≈ {float(num):.4g}"
                if v.is_real is False:
                    return str(sp.N(v, 5))
                surd = any(p.exp.is_Rational and not p.exp.is_Integer for p in v.atoms(sp.Pow))
                if surd and not v.atoms(sp.Function) and not v.has(sp.pi) and len(str(v)) < 40:
                    # keep surds exact, e.g. -3/4 + sqrt(65)/4 ≈ 1.266
                    return f"{str(v).replace('**', '^')} ≈ {float(num):.4g}"
                return f"{float(num):.6g}" if not v.free_symbols else str(v)
            return str(v).replace("**", "^")
        except Exception:
            return str(v)

    def latex(v):
        try:
            return sp.latex(v)
        except Exception:
            return ""

    while True:
        try:
            msg = conn.recv()
        except EOFError:
            break
        kind, payload = msg
        try:
            if kind == "eval":
                expr = payload
                if "=" in expr and not any(op in expr for op in ("==", "<=", ">=", "!=")) and not expr.strip().startswith("solve"):
                    lhs, rhs = expr.split("=", 1)
                    e = sp.Eq(parse_expr(lhs, local_dict=ns, transformations=T, global_dict={"__builtins__": {}, **ns}),
                              parse_expr(rhs, local_dict=ns, transformations=T, global_dict={"__builtins__": {}, **ns}))
                    free = sorted(e.free_symbols, key=lambda s: s.name)
                    v = sp.solve(e, free[0] if len(free) == 1 else free, dict=len(free) > 1) if free else bool(e)
                    if free and len(free) == 1:
                        v = {free[0]: v[0]} if len(v) == 1 else [f"{free[0]} = {fmt(r)}" for r in v] or "no solution"
                    conn.send(("ok", {"value": fmt(v), "latex": latex(e), "kind": "solve", "vars": [s.name for s in free]}))
                else:
                    v = parse_expr(expr, local_dict=ns, transformations=T, global_dict={"__builtins__": {}, **ns})
                    if hasattr(v, "doit"):
                        v = v.doit()
                    conn.send(("ok", {"value": fmt(v), "latex": latex(v), "kind": "eval"}))
            elif kind == "ping":
                conn.send(("ok", {}))
        except Exception as e:  # malformed expression: report, keep serving
            conn.send(("err", f"{type(e).__name__}: {e}"))


@dataclass
class MathResult:
    ok: bool
    expr: str
    value: str = ""
    latex: str = ""
    kind: str = ""
    error: str = ""
    vars: list[str] = field(default_factory=list)


class MathService:
    """Persistent SymPy worker process.

    Readiness is explicit: a freshly spawned worker must answer a ping (spawn +
    `import sympy` can take several seconds while Whisper/CUDA load) before any
    expression is sent, and only the *computation* is bound by `timeout`.
    A computation that overruns kills the worker; a replacement is pre-spawned
    in the background so the next call doesn't pay the start-up cost.
    """

    def __init__(self, timeout: float = 3.0, start_timeout: float = 45.0):
        self.timeout = timeout
        self.start_timeout = start_timeout
        self._lock = threading.Lock()
        self._proc: mp.Process | None = None
        self._conn = None
        self._ready = False
        self.restarts = 0

    def _spawn(self) -> None:
        ctx = mp.get_context("spawn")
        parent, child = ctx.Pipe()
        self._proc = ctx.Process(target=_worker, args=(child,), daemon=True, name="glass-sympy")
        self._proc.start()
        child.close()  # so a dead worker shows up as EOF instead of a silent hang
        self._conn = parent
        self._ready = False

    def _ensure_ready(self) -> bool:
        """Must hold the lock."""
        if self._proc is None or not self._proc.is_alive():
            if self._proc is not None:
                log.warning("sympy worker exited (code %s); restarting", self._proc.exitcode)
            self._spawn()
        if self._ready:
            return True
        t = time.perf_counter()
        try:
            self._conn.send(("ping", None))
            if self._conn.poll(self.start_timeout):
                self._conn.recv()
                self._ready = True
                log.info("sympy worker ready in %.1fs", time.perf_counter() - t)
                return True
        except (EOFError, OSError) as e:
            log.warning("sympy worker failed to start: %s (exit %s)", e, self._proc.exitcode)
        log.warning("sympy worker not ready after %.0fs (alive=%s exit=%s)", time.perf_counter() - t,
                    self._proc.is_alive(), self._proc.exitcode)
        self._kill()
        return False

    def _kill(self) -> None:
        if self._proc is not None:
            try:
                self._proc.kill()
            except Exception:
                pass
        self._proc = None
        self._ready = False

    def warm(self) -> None:
        with self._lock:
            self._ensure_ready()

    def close(self) -> None:
        with self._lock:
            self._kill()

    def run(self, expr: str) -> MathResult:
        expr = clean_expr(expr)
        if not expr:
            return MathResult(False, expr, error="empty")
        if not SAFE_CHARS.match(expr) or FORBIDDEN.search(expr):
            return MathResult(False, expr, error="rejected by sandbox")
        for name in re.findall(r"[A-Za-z_]\w*", expr):
            if name not in ALLOWED_FUNCS:
                return MathResult(False, expr, error=f"unknown name {name}")
        with self._lock:
            if not self._ensure_ready():
                return MathResult(False, expr, error="maths engine unavailable")
            try:
                self._conn.send(("eval", expr))
                if not self._conn.poll(self.timeout):
                    log.warning("sympy computation exceeded %.0fs on %r; restarting worker", self.timeout, expr)
                    self._kill()
                    self.restarts += 1
                    threading.Thread(target=self.warm, daemon=True).start()
                    return MathResult(False, expr, error="timeout")
                status, payload = self._conn.recv()
            except (EOFError, OSError) as e:
                log.warning("sympy worker connection lost: %s", e)
                self._kill()
                return MathResult(False, expr, error="maths engine restarted")
        if status == "ok":
            return MathResult(True, expr, payload["value"], payload.get("latex", ""), payload.get("kind", ""),
                              vars=payload.get("vars", []))
        return MathResult(False, expr, error=payload)


def clean_expr(expr: str) -> str:
    expr = normalise(expr).strip().strip("`").strip()
    expr = re.sub(r"^(CALC|calc)\s*:\s*", "", expr)
    expr = expr.rstrip(".?! ")
    return expr


# --------------------------------------------------------------------------- extraction
_TOK = re.compile(r"\d+(?:\.\d+)?|[A-Za-z]+|[+\-*/^()=.]|\s+|\S")
_MATH_WORDS = {"sqrt", "sin", "cos", "tan", "pi"}


def _mathy(tok: str) -> bool:
    if tok.isspace() or tok in "+-*/^()." or re.fullmatch(r"\d+(?:\.\d+)?", tok):
        return True
    return tok.isalpha() and (len(tok) == 1 or tok.lower() in _MATH_WORDS or
                              (len(tok) <= 3 and tok.lower() in {"xy", "ab", "xx"}))


def find_problems(text: str) -> list[str]:
    """Return candidate equations found in free text (OCR lines, transcripts).

    Grows outwards from every '=' over math-looking tokens, stopping at real
    words, so "Find x when 5x − 3 = 2x + 9." yields "5x - 3 = 2x + 9".
    """
    t = normalise(text)
    out: list[str] = []
    for line in re.split(r"[\n\r]+", t):
        line = re.sub(r"^\s*(?:Q(?:uestion)?\s*)?\d+\s*[.)]\s*", "", line.strip(), flags=re.I)
        toks = _TOK.findall(line)
        for i, tok in enumerate(toks):
            if tok != "=":
                continue
            lo = i
            while lo > 0 and _mathy(toks[lo - 1]):
                lo -= 1
            hi = i + 1
            while hi < len(toks) and _mathy(toks[hi]):
                hi += 1
            cand = "".join(toks[lo:hi]).strip().strip(".").strip()
            cand = re.sub(r"^[+*/^=.]+|[+\-*/^=]+$", "", cand).strip()
            if "=" not in cand:
                continue
            lhs, rhs = (s.strip() for s in cand.split("=", 1))
            if not lhs or not rhs or cand.count("(") != cand.count(")"):
                continue
            letters = set(re.findall(r"(?<![a-z])([a-z])(?![a-z])", cand.lower()))
            if not letters or len(cand) < 5 or not re.search(r"\d", cand):
                continue
            out.append(cand)
    seen, uniq = set(), []
    for c in out:
        k = re.sub(r"\s", "", c)
        if k not in seen:
            seen.add(k)
            uniq.append(c)
    return uniq


NUM_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
    "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30,
    "forty": 40, "fifty": 50, "sixty": 60, "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
    "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10, "last": -1,
}


def word_to_int(w: str) -> int | None:
    w = w.lower().strip()
    if w.isdigit():
        return int(w)
    if w in NUM_WORDS:
        return NUM_WORDS[w]
    parts = re.split(r"[\s-]+", w)
    if len(parts) == 2 and parts[0] in NUM_WORDS and parts[1] in NUM_WORDS:
        return NUM_WORDS[parts[0]] + NUM_WORDS[parts[1]]
    return None


# --------------------------------------------------------------------------- whole questions
# A 4B language model is not reliable at algebra. For the common question types the exact
# answer is computed here and handed to the model, which only explains it.
_VERBS = [
    ("expand", re.compile(r"\b(expand( and simplify)?|multiply out)\b", re.I)),
    ("factor", re.compile(r"\b(factori[sz]e( fully| completely)?|factor)\b", re.I)),
    ("diff", re.compile(r"\b(differentiate|find\s+(?:dy\s*/\s*dx|the derivative of))\b", re.I)),
    ("simplify", re.compile(r"\bsimplify( fully)?\b", re.I)),
    ("solve", re.compile(r"\b(solve|find the value of x|find x)\b", re.I)),
    ("eval", re.compile(r"\b(work out|calculate|evaluate|find the value of|what is|find)\b", re.I)),
]
_FUNC_DEF = re.compile(r"\b([fgh])\(\s*([a-z])\s*\)\s*=\s*([^\n,;]+)")
_INEQ = re.compile(r"(<=|>=|<|>)")


def _left_span(s: str) -> str:
    """The maths just before a position: "Solve 3x + 2" -> "3x + 2"."""
    toks = _TOK.findall(s)
    i = len(toks)
    while i > 0 and _mathy(toks[i - 1]):
        i -= 1
    span = "".join(toks[i:]).strip()
    return span if span.count("(") == span.count(")") else ""


def _span(s: str) -> str:
    """The maths right after a command word: "Expand (x + 3)^2 giving…" -> "(x + 3)^2"."""
    s = re.sub(r"^[\s:,-]+", "", s)
    toks = _TOK.findall(s)
    out = []
    for t in toks:
        if t in ",;:" or (t.isalpha() and not _mathy(t) and t.lower() not in {"of"}):
            break
        out.append(t)
    span = "".join(out).strip().rstrip(".=+-*/^ ")
    if span.count("(") != span.count(")"):
        return ""
    return span


def _times_sign(expr: str) -> str:
    """OCR reads × as the letter x: "4a^3 x 2a^2" -> "4a^3 * 2a^2" (only when x is not a variable there)."""
    if re.search(r"\s[x×]\s", expr) and not re.search(r"\dx|x\^|\(x|x\)", expr) and \
            (re.search(r"[a-wyz]", expr) or not re.search(r"[a-z]", expr.replace(" x ", " "))):
        return re.sub(r"\s[x×]\s", " * ", expr)
    return expr


def plan_math(question: str, stem: str = "") -> list[str]:
    """SymPy expressions that answer a worksheet question exactly, or []."""
    q = normalise(question)
    q = re.sub(r"(\d+(?:\.\d+)?)\s*%\s*of\b", r"(\1/100)*", q)  # 15% of 80
    q = re.sub(r"(\d)\s+of\s+(?=\d)", r"\1*", q)  # 3/4 of 20
    # f(x) defined in the stem (or the question): use it
    for m in _FUNC_DEF.finditer(normalise(stem) + "\n" + q):
        name, var, body = m.group(1), m.group(2), m.group(3).strip().rstrip(".")
        if not body:
            continue
        q = q.replace(m.group(0), "")
        q = re.sub(rf"\b{name}\(\s*{var}\s*\)", f"({body})", q)
        q = re.sub(rf"\b{name}\(\s*(-?\d+(?:\.\d+)?)\s*\)",
                   lambda n: "(" + re.sub(rf"(?<![A-Za-z]){var}(?![A-Za-z])", f"({n.group(1)})", body) + ")", q)
    verb = next((v for v, rx in _VERBS if rx.search(q)), None)
    eqs = find_problems(q)
    if verb in ("solve", None) or eqs:
        if len(eqs) >= 2:  # simultaneous equations
            letters = sorted(set(re.findall(r"(?<![a-z])([a-z])(?![a-z])", " ".join(eqs).lower())))
            if 2 <= len(letters) <= 3 and len(eqs) == len(letters):
                pairs = [e.split("=", 1) for e in eqs]
                return [f"solve([{', '.join(f'Eq({a.strip()}, {b.strip()})' for a, b in pairs)}], [{', '.join(letters)}])"]
        if eqs:
            return eqs[:2]
        if verb == "solve":
            m = _INEQ.search(q)
            if m:
                lhs, rhs = _left_span(q[:m.start()]), _span(q[m.end():])
                if lhs and rhs and re.search(r"[a-z]", lhs + rhs):
                    return [f"reduce_inequalities({lhs} {m.group(1)} {rhs})"]
    if verb is None:
        return []
    m = _VERBS[[v for v, _ in _VERBS].index(verb)][1].search(q)
    rest = re.sub(r"^\s*(?:the\s+)?(?:exact\s+)?(?:value\s+of|answer\s+to)?\s*", "", q[m.end():], flags=re.I)
    span = _times_sign(_span(rest))
    if not span or not re.search(r"\d|[a-z]", span):
        return []
    if verb == "expand":
        return [f"expand({span})"]
    if verb == "factor":
        return [f"factor({span})"]
    if verb == "simplify":
        return [f"simplify({span})"]
    if verb == "diff":
        v = "x" if "x" in span else (re.findall(r"(?<![a-z])([a-z])(?![a-z])", span) or ["x"])[0]
        return [f"diff({span}, {v})"]
    if verb == "eval" and not re.search(r"(?<![a-z])[a-z](?![a-z])", span.replace("pi", "").replace("sqrt", "")):
        return [span]
    return []


def _closing(s: str, i: int) -> int:
    depth = 0
    for j in range(i, len(s)):
        depth += {"(": 1, ")": -1}.get(s[j], 0)
        if depth == 0:
            return j
    return -1


def tidy_result(v: str) -> str:
    """SymPy's output as a student writes it: "(3 < x) & (x < oo)" -> "x > 3", "6*x" -> "6x"."""
    v = re.sub(r"\s*&\s*\((?:-oo < \w|\w < oo)\)|\((?:-oo < \w|\w < oo)\)\s*&\s*", "", v)
    v = v.strip()
    if v.startswith("(") and _closing(v, 0) == len(v) - 1:  # one bracket around everything
        v = v[1:-1]
    m = re.fullmatch(r"(-?[\d/.]+)\s*(<=|<)\s*([a-z])", v)
    if m:
        v = f"{m.group(3)} {'>=' if m.group(2) == '<=' else '>'} {m.group(1)}"
    v = re.sub(r"(\d)\*(?=[a-z(])", r"\1", v)
    v = re.sub(r"\)\*\(", ")(", v)
    return v
