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
    if depth > 800:
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
# Marker emitted for an unconnected-lookup call.  It is shaped like a normal
# function call so the column qualifier still qualifies the ARGUMENTS, and the
# emitter later resolves it to "<join_alias>.<returnPort>" plus a LEFT JOIN.
LKP_CALL_PREFIX = "LKPCALL_"


def _translate_unconnected_lookups(expr: str) -> str:
    # :LKP.some_lookup(<balanced args>) -> LKPCALL_some_lookup(<args>)
    pat = re.compile(r':LKP\.([A-Za-z0-9_]+)\s*\(', re.IGNORECASE)
    while True:
        m = pat.search(expr)
        if not m:
            return expr
        name = m.group(1)
        i = m.end() - 1                 # at '('
        depth, q, close = 0, None, None
        for j in range(i, len(expr)):
            ch = expr[j]
            if q:
                if ch == q:
                    q = None
                continue
            if ch in ("'", '"'):
                q = ch
            elif ch == '(':
                depth += 1
            elif ch == ')':
                depth -= 1
                if depth == 0:
                    close = j
                    break
        if close is None:
            return expr                 # unbalanced -> leave as-is
        args = expr[i + 1:close]
        expr = (expr[:m.start()] + f"{LKP_CALL_PREFIX}{name}({args})"
                + expr[close + 1:])


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


def _translate_in(expr: str, depth=0) -> str:
    # Informatica IN(value, v1, v2, ... [,caseFlag]) -> (value IN (v1, v2, ...))
    # Snowflake IN is an operator, not a function.  Recursive so the operator
    # form we emit is never re-scanned, and nested IN(...) still converts.
    if depth > 800:
        return expr
    found = _find_call(expr, "IN")
    if not found:
        return expr
    start, end, inner = found
    args = _split_args(inner)
    if len(args) >= 2:
        value = _translate_in(args[0], depth + 1)
        lst = [_translate_in(a, depth + 1) for a in args[1:]]
        rewritten = f"({value} IN ({', '.join(lst)}))"
    else:
        rewritten = f"({_translate_in(inner, depth + 1)})"
    return expr[:start] + rewritten + _translate_in(expr[end:], depth + 1)


_DATE_UNIT = {
    "D": "DAY", "DD": "DAY", "DDD": "DAY", "DY": "DAY", "J": "DAY",
    "MM": "MONTH", "MON": "MONTH", "MONTH": "MONTH", "RM": "MONTH",
    "Y": "YEAR", "YY": "YEAR", "YYY": "YEAR", "YYYY": "YEAR", "RR": "YEAR",
    "HH": "HOUR", "HH12": "HOUR", "HH24": "HOUR",
    "MI": "MINUTE", "SS": "SECOND", "MS": "MILLISECOND", "US": "MICROSECOND",
    "Q": "QUARTER", "W": "WEEK", "WW": "WEEK",
}


def _date_unit(fmt: str) -> str:
    f = (fmt or "").strip().strip("'\"").upper()
    return _DATE_UNIT.get(f, "DAY")


def _translate_add_to_date(expr: str) -> str:
    # Informatica ADD_TO_DATE(date, format, amount) -> DATEADD(unit, amount, date)
    while True:
        found = _find_call(expr, "ADD_TO_DATE")
        if not found:
            return expr
        start, end, inner = found
        args = _split_args(inner)
        if len(args) >= 3:
            unit = _date_unit(args[1])
            rewritten = f"DATEADD({unit}, {args[2]}, {args[0]})"
        else:
            rewritten = f"({inner})"
        expr = expr[:start] + rewritten + expr[end:]


def _translate_get_date_part(expr: str) -> str:
    # Informatica GET_DATE_PART(date, format) -> DATE_PART(unit, date)
    while True:
        found = _find_call(expr, "GET_DATE_PART")
        if not found:
            return expr
        start, end, inner = found
        args = _split_args(inner)
        if len(args) >= 2:
            rewritten = f"DATE_PART({_date_unit(args[1])}, {args[0]})"
        else:
            rewritten = f"({inner})"
        expr = expr[:start] + rewritten + expr[end:]


def _translate_make_date_time(expr: str) -> str:
    # Informatica MAKE_DATE_TIME(y,mon,d,h,mi,s) -> TIMESTAMP_FROM_PARTS(...)
    while True:
        found = _find_call(expr, "MAKE_DATE_TIME")
        if not found:
            return expr
        start, end, inner = found
        args = _split_args(inner)
        while len(args) < 6:
            args.append("0")
        rewritten = f"TIMESTAMP_FROM_PARTS({', '.join(args[:6])})"
        expr = expr[:start] + rewritten + expr[end:]


def _translate_replace(expr: str) -> str:
    # REPLACESTR(caseFlag, subject, old, new) / REPLACECHR(...) -> REPLACE(subject, old, new)
    for fn in ("REPLACESTR", "REPLACECHR"):
        while True:
            found = _find_call(expr, fn)
            if not found:
                break
            start, end, inner = found
            args = _split_args(inner)
            if len(args) >= 4:
                rewritten = f"REPLACE({args[1]}, {args[2]}, {args[-1]})"
            elif len(args) == 3:
                rewritten = f"REPLACE({args[0]}, {args[1]}, {args[2]})"
            else:
                rewritten = f"({inner})"
            expr = expr[:start] + rewritten + expr[end:]
    return expr


def _translate_instr(expr: str) -> str:
    # Informatica INSTR(string, search[, start[, occurrence]])
    # -> Snowflake CHARINDEX(search, string[, start])  (args flip; occ dropped)
    while True:
        found = _find_call(expr, "INSTR")
        if not found:
            return expr
        start, end, inner = found
        args = _split_args(inner)
        if len(args) >= 2:
            new = [args[1], args[0]] + args[2:3]      # search, string, [start]
            rewritten = f"CHARINDEX({', '.join(new)})"
        else:
            rewritten = f"CHARINDEX({inner})"
        expr = expr[:start] + rewritten + expr[end:]


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


def find_lkp_call(expr: str):
    """Locate the first LKPCALL_<name>(<balanced args>) in expr.
    Returns (start, end, lookup_name, args_string) or None."""
    m = re.search(re.escape(LKP_CALL_PREFIX) + r'([A-Za-z0-9_]+)\s*\(', expr)
    if not m:
        return None
    i = m.end() - 1
    depth, q = 0, None
    for j in range(i, len(expr)):
        ch = expr[j]
        if q:
            if ch == q:
                q = None
            continue
        if ch in ("'", '"'):
            q = ch
        elif ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
            if depth == 0:
                return (m.start(), j + 1, m.group(1), expr[i + 1:j])
    return None


def split_args(s: str) -> list:
    """Public wrapper: split a call's argument list on top-level commas."""
    return _split_args(s)


def _translate_params(expr: str) -> str:
    # Informatica mapping/session parameters ($name$, $$name) are runtime values
    # not in the export -> emit a tagged NULL for a human to wire up.
    def repl(m):
        return f"/*PARAM:{m.group(0).strip('$')}*/ NULL"
    return re.sub(r'\$\$?[A-Za-z_][A-Za-z0-9_]*\$?', repl, expr)


def translate(expr: str) -> str:
    """Best-effort Informatica-expression -> Snowflake-SQL translation."""
    if not expr or not expr.strip():
        return ""
    s = expr
    # strip Informatica '--' line comments: the transform is rendered on ONE
    # line, so a '--' would comment out the rest of the expression (incl. AS).
    s = re.sub(r'--[^\n]*', ' ', s)
    s = _translate_unconnected_lookups(s)
    # loop the idempotent translators to a fixpoint (translating one call can
    # expose another).  _translate_in is NON-idempotent (it emits the operator
    # form "x IN (...)" which would be re-matched), so run it exactly once after.
    for _ in range(12):
        before = s
        s = _translate_iif(s)
        s = _translate_isnull(s)
        s = _translate_instr(s)
        s = _translate_add_to_date(s)
        s = _translate_get_date_part(s)
        s = _translate_make_date_time(s)
        s = _translate_replace(s)
        s = _translate_reg_match(s)
        if s == before:
            break
    s = _translate_in(s)
    for pat, rep in _SIMPLE:
        s = re.sub(pat, rep, s, flags=re.IGNORECASE)
    s = _translate_params(s)
    # any expression variable still present could not be inlined (stateful:
    # SCD2 current-record compare, running counters, prev-row) -> tag as NULL
    s = re.sub(r'\bv_[A-Za-z0-9_]+\b',
               lambda m: f"/*VAR:{m.group(0)}*/ NULL", s, flags=re.IGNORECASE)
    # normalise whitespace/newlines to keep YAML tidy
    s = re.sub(r'[ \t]+', ' ', s)
    s = re.sub(r'\n\s*', ' ', s).strip()
    return s


def is_variable_field(name: str) -> bool:
    """Expression 'v_' ports are internal variables, not output columns."""
    n = (name or "")
    return n.lower().startswith("v_")
