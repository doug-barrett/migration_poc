"""
idmc_expr.py  --  Translate Informatica expression language to Snowflake SQL.

Informatica's expression grammar differs from ANSI/Snowflake SQL in a handful
of functions and in how it references ports.  This module does a best-effort,
regex-driven rewrite that covers the functions actually used in the Acenda
export (see the function inventory produced by the survey step).  It is
deliberately conservative: anything it does not recognise is passed through
unchanged, and unconnected-lookup calls are rewritten to a readable
placeholder so the surrounding SQL stays syntactically sane.

Perfect semantic fidelity for every expression is NOT the goal of the
automatic pass -- the goal is that (a) simple/expressions convert correctly and
(b) complex ones survive as legible SQL that a human can finish.  The
representative pipelines are hand-finished on top of this.
"""
from __future__ import annotations
import re


# ---- IIF(cond, t, f) -> CASE WHEN cond THEN t ELSE f END -------------------
def _split_args(s: str) -> list[str]:
    """Split a function argument list on top-level commas (respecting parens
    and quotes)."""
    args, depth, buf, q = [], 0, [], None
    for ch in s:
        if q:
            buf.append(ch)
            if ch == q:
                q = None
            continue
        if ch in ("'", '"'):
            q = ch; buf.append(ch); continue
        if ch in "([":
            depth += 1; buf.append(ch)
        elif ch in ")]":
            depth -= 1; buf.append(ch)
        elif ch == "," and depth == 0:
            args.append("".join(buf)); buf = []
        else:
            buf.append(ch)
    if buf:
        args.append("".join(buf))
    return [a.strip() for a in args]


def _find_call(expr: str, fname: str):
    """Find the first top-level call to fname(...) (case-insensitive) and
    return (start, end, inner_args_string), or None."""
    pat = re.compile(r'\b' + re.escape(fname) + r'\s*\(', re.IGNORECASE)
    m = pat.search(expr)
    if not m:
        return None
    i = m.end() - 1  # at '('
    depth, q = 0, None
    for j in range(i, len(expr)):
        ch = expr[j]
        if q:
            if ch == q:
                q = None
            continue
        if ch in ("'", '"'):
            q = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return (m.start(), j + 1, expr[i + 1:j])
    return None


def _translate_iif(expr: str, depth=0) -> str:
    if depth > 60:
        return expr
    found = _find_call(expr, "IIF")
    if not found:
        return expr
    start, end, inner = found
    args = _split_args(inner)
    if len(args) == 2:
        cond, tval = args
        fval = "NULL"
    elif len(args) >= 3:
        cond, tval, fval = args[0], args[1], ",".join(args[2:])
    else:
        # malformed; leave the call but recurse past it
        return expr[:end] + _translate_iif(expr[end:], depth + 1)
    cond = _translate_iif(cond, depth + 1)
    tval = _translate_iif(tval, depth + 1)
    fval = _translate_iif(fval, depth + 1)
    rewritten = f"CASE WHEN {cond} THEN {tval} ELSE {fval} END"
    return expr[:start] + rewritten + _translate_iif(expr[end:], depth + 1)


# ---- ISNULL(x) -> (x IS NULL) ---------------------------------------------
def _translate_isnull(expr: str) -> str:
    while True:
        found = _find_call(expr, "ISNULL")
        if not found:
            return expr
        start, end, inner = found
        expr = f"{expr[:start]}({inner.strip()} IS NULL){expr[end:]}"


# ---- unconnected lookups --------------------------------------------------
# In an expression, an unconnected lookup is invoked as ":LKP.name(args)".
# Some mappings also use bare macro tokens (ULKP_*, LKP_*).  We rewrite the
# ":LKP." form to a comment-tagged placeholder that keeps the SQL parseable
# and records the intent for the human finisher.
def _translate_unconnected_lookups(expr: str) -> str:
    # :LKP.some_lookup(a, b)  ->  /*LOOKUP some_lookup(a, b)*/ NULL
    def repl(m):
        return f"/*LKP:{m.group(1)}({m.group(2)})*/ NULL"
    pat = re.compile(r':LKP\.([A-Za-z0-9_]+)\s*\(([^()]*)\)', re.IGNORECASE)
    prev = None
    while prev != expr:
        prev = expr
        expr = pat.sub(repl, expr)
    return expr


# ---- simple function / operator swaps -------------------------------------
_SIMPLE = [
    # (informatica, snowflake)  -- word-boundary, case-insensitive
    (r'\bSESSSTARTTIME\b', 'CURRENT_TIMESTAMP'),
    (r'\bSYSTIMESTAMP\b', 'CURRENT_TIMESTAMP'),
    (r'\bSYSDATE\b', 'CURRENT_TIMESTAMP'),
    (r'\bTO_BIGINT\b', 'TO_NUMBER'),
    (r'\bTO_INTEGER\b', 'TO_NUMBER'),
    (r'\bTO_DECIMAL\b', 'TO_NUMBER'),
    (r'\bREG_REPLACE\b', 'REGEXP_REPLACE'),
    (r'\bIS_SPACES\s*\(\s*([^()]*?)\s*\)', r"(TRIM(\1) = '')"),
]


def _translate_reg_match(expr: str) -> str:
    # REG_MATCH(subject, pattern) -> RLIKE(subject, pattern)
    while True:
        found = _find_call(expr, "REG_MATCH")
        if not found:
            return expr
        start, end, inner = found
        args = _split_args(inner)
        if len(args) >= 2:
            rewritten = f"RLIKE({args[0]}, {args[1]})"
        else:
            rewritten = f"RLIKE({inner})"
        expr = expr[:start] + rewritten + expr[end:]


def translate(expr: str) -> str:
    """Best-effort Informatica-expression -> Snowflake-SQL translation."""
    if not expr or not expr.strip():
        return ""
    s = expr
    s = _translate_unconnected_lookups(s)
    s = _translate_iif(s)
    s = _translate_isnull(s)
    s = _translate_reg_match(s)
    for pat, rep in _SIMPLE:
        s = re.sub(pat, rep, s, flags=re.IGNORECASE)
    # normalise whitespace/newlines to keep YAML tidy
    s = re.sub(r'[ \t]+', ' ', s)
    s = re.sub(r'\n\s*', ' ', s).strip()
    return s


def is_variable_field(name: str) -> bool:
    """Expression 'v_' ports are internal variables, not output columns."""
    n = (name or "")
    return n.lower().startswith("v_")
