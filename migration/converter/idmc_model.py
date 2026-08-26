"""
idmc_model.py  --  Turn a decoded IDMC mapping (DTEMPLATE + MTT) into a
migration IR (intermediate representation) that idmc_emit.py can render as a
Coalesce node.

The IR captures, per mapping:
  * sources  -- concrete source tables bound by the MTT   (-> upstream deps)
  * targets  -- concrete target tables bound by the MTT   (-> the node itself)
  * lookups  -- lookup tables + their ON conditions        (-> LEFT JOINs)
  * filters  -- Filter transform conditions                (-> WHERE)
  * outputs  -- Expression output columns (translated)     (-> column transforms)
  * ports    -- source ports the mapping references        (-> reconstructed
                                                              source columns)

Because parameterised sources/targets carry NO schema in the export, source
columns are reconstructed from mapping usage (the same best-effort approach the
hand-built pilot used).  Lookup tables DO carry embedded schemas, which we use
verbatim.
"""
from __future__ import annotations
import os
import re
from dataclasses import dataclass, field

from idmc_imf import (Imf, load_dtemplate, load_mtt,
                      platform_type_to_snowflake)
import idmc_expr


# tokens that are Informatica functions/keywords, never source ports
_KEYWORDS = {
    "IIF", "ISNULL", "TO_CHAR", "TO_DATE", "DECODE", "MD5", "RTRIM", "LTRIM",
    "ADD_TO_DATE", "SUBSTR", "TO_BIGINT", "TO_INTEGER", "TO_DECIMAL", "INSTR",
    "REG_MATCH", "REG_REPLACE", "MAKE_DATE_TIME", "CONCAT", "GET_DATE_PART",
    "RPAD", "LPAD", "LENGTH", "UPPER", "LOWER", "ABS", "CHR", "SUM", "COUNT",
    "MIN", "MAX", "AVG", "IN", "AND", "OR", "NOT", "NULL", "TRUE", "FALSE",
    "IS", "IS_SPACES", "SESSSTARTTIME", "SYSDATE", "SYSTIMESTAMP", "TRUNC",
    "ROUND", "SIGN", "MOD", "POWER", "SQRT", "LKP", "TO_NUMBER", "CASE",
    "WHEN", "THEN", "ELSE", "END", "LIKE", "RLIKE", "DD", "DDL",
    "DD_INSERT", "DD_UPDATE", "DD_DELETE", "DD_REJECT", "SET", "MM", "YYYY",
    "HH24", "MI", "SS", "MON", "DAY", "YY", "HH", "D", "Y",
}

_ROLE_KEYS = {
    "TmplSource": "Source", "TmplTarget": "Target", "TmplLookup": "Lookup",
    "TmplExpression": "Expression", "TmplJoiner": "Joiner",
    "TmplFilter": "Filter", "TmplSorter": "Sorter",
    "TmplAggregator": "Aggregator", "TmplRouter": "Router",
    "TmplUnion": "Union", "TmplRank": "Rank", "TmplNormalizer": "Normalizer",
    "TmplGenerator": "Sequence", "TmplMapplet": "Mapplet",
}


@dataclass
class Col:
    name: str
    dtype: str = ""
    transform: str = ""     # translated Snowflake expression (output cols)
    is_output: bool = False  # produced by an Expression (vs pass-through)


@dataclass
class JoinEdge:
    master: str                    # master-side source table
    detail: str                    # detail-side source table
    join_type: str                 # Normal / Detail Outer / Master Outer / Full
    conditions: list               # [(master_col, op, detail_col), ...]


@dataclass
class Lookup:
    table: str                         # reconstructed table name (dw_party ...)
    conditions: list                   # [(lkp_col, op, in_port), ...]
    fields: list = field(default_factory=list)   # [Col, ...] embedded schema
    unconnected: bool = True
    name: str = ""                     # transformation name (lkp_dw_assess)
    return_port: str = ""              # column the lookup RETURNS
    input_ports: list = field(default_factory=list)  # ordered input port names


@dataclass
class MappingIR:
    name: str                          # mapping name  (m_...)
    folder: str                        # export folder  (03_dq, 04_cnf, ...)
    sources: list = field(default_factory=list)   # [(schema, table), ...]
    targets: list = field(default_factory=list)   # [(schema, table), ...]
    target_update_cols: list = field(default_factory=list)
    lookups: list = field(default_factory=list)   # [Lookup, ...]
    joins: list = field(default_factory=list)     # [JoinEdge, ...]
    unions: list = field(default_factory=list)    # [[part_table, ...], ...]
    filters: list = field(default_factory=list)   # [condition_str, ...]
    outputs: list = field(default_factory=list)   # [Col, ...] expression outs
    ref_ports: set = field(default_factory=set)   # source ports referenced
    tx_roles: dict = field(default_factory=dict)  # {role: count}
    has_sequence: bool = False
    has_aggregator: bool = False
    has_router: bool = False
    has_union: bool = False


def _role(imf: Imf, tx: dict) -> str:
    cls = tx.get("$$class")
    cn = imf.class_name(cls).split(".")[-1]
    return _ROLE_KEYS.get(cn, cn or f"class{cls}")


def _lookup_table_name(tx_name: str) -> str:
    n = tx_name
    for pfx in ("ULKP_", "ulkp_", "LKP_", "lkp_", "Lkp_"):
        if n.startswith(pfx):
            n = n[len(pfx):]
            break
    return n.upper()


def _extract_ports(expr: str) -> set:
    """Identifiers referenced in an Informatica expression that are plausibly
    input ports (upper-cased).  Excludes function names, keywords, quoted
    strings, numbers and :LKP calls."""
    if not expr:
        return set()
    e = re.sub(r"'[^']*'", " ", expr)               # strip string literals
    e = re.sub(r':LKP\.[A-Za-z0-9_]+', ' ', e)      # strip lookup calls
    out = set()
    for tok in re.findall(r'\b([A-Za-z_][A-Za-z0-9_]*)\b', e):
        if re.match(r'^\d', tok):
            continue
        if tok.upper() in _KEYWORDS:
            continue
        if idmc_expr.is_variable_field(tok):
            continue
        # skip if immediately followed by '(' -> it's a function call
        out.add(tok.upper())
    return out


def _split_obj(objname: str):
    """'MDW/STG_AL4/AL4_DCUSTCTRCT' -> (schema='STG_AL4', table='AL4_DCUSTCTRCT')."""
    parts = [p for p in (objname or "").split("/") if p]
    if not parts:
        return ("", "")
    if len(parts) >= 2:
        return (parts[-2].upper(), parts[-1].upper())
    return ("", parts[-1].upper())


def _tgt_from_paramname(pname: str) -> str:
    """Fallback: derive the target table from a param name such as
    '$tgt_dw_cust_contract_insert$' -> 'DW_CUST_CONTRACT'."""
    n = (pname or "").strip("$")
    for pfx in ("tgt_", "target_"):
        if n.lower().startswith(pfx):
            n = n[len(pfx):]
            break
    for sfx in ("_closure_insert", "_closure_update", "_insert", "_update",
                "_delete", "_upsert"):
        if n.lower().endswith(sfx):
            n = n[: -len(sfx)]
            break
    return n.upper()


def _mtt_bindings(mtt: dict):
    """Return (sources, targets, target_update_cols) from an mtTask.

    Sources bind via extendedObject.object.name; targets via targetObject /
    targetObjectLabel.  A mapping often has several TARGET params that are the
    same physical table under different SCD strategies (insert/update/closure)
    -- these are de-duplicated to one (schema, table)."""
    sources, targets, tgt_cols = [], [], []
    saw_source_param = False

    def add(lst, sch, tbl):
        if tbl and (sch, tbl) not in lst:
            lst.append((sch, tbl))

    for p in mtt.get("parameters", []) or []:
        ptype = (p.get("type") or "").upper()
        rpd = p.get("runtimeParameterData") or {}
        default = rpd.get("objectDefaultValue")
        if "SOURCE" in ptype:
            saw_source_param = True
            eo = p.get("extendedObject") or {}
            objs = eo.get("objects") or ([eo.get("object")] if eo.get("object") else [])
            names = [o.get("name") for o in objs if o and o.get("name")]
            picked = names[0] if names else (default or "")
            sch, tbl = _split_obj(picked)
            if not tbl and default:
                sch, tbl = "", default.upper()
            add(sources, sch, tbl)
        elif "TARGET" in ptype:
            picked = p.get("targetObject") or ""
            sch, tbl = _split_obj(picked)
            if not tbl:
                tbl = (p.get("targetObjectLabel") or "").upper()
            if not tbl:
                tbl = _tgt_from_paramname(p.get("name", ""))
            add(targets, sch, tbl)
            for c in p.get("targetUpdateColumns") or []:
                if c and c.upper() not in tgt_cols:
                    tgt_cols.append(c.upper())

    # In-place validation/update mappings often leave the SOURCE param
    # unbound (only the target object is set).  When a source param exists but
    # bound no object, fall back to the target table(s) -- the mapping reads
    # and writes the same table.
    if saw_source_param and not sources and targets:
        for sch, tbl in targets:
            add(sources, sch, tbl)

    return sources, targets, tgt_cols


def clean_join_operand(op: str) -> str:
    """Recover the underlying column name from a joiner port operand.

    Detail-side ports are usually renamed with a lowercase table-hint prefix
    (dw_CONTRACT_ID, tgt_CONTRACT_KEY); real columns are upper-case, so strip a
    single leading lowercase prefix."""
    o = (op or "").strip()
    o = re.sub(r'^[a-z][a-z0-9]*_', '', o)
    return o.upper()


def _source_tx_tables(mtt: dict) -> dict:
    """source transformation name -> (schema, table).  The MTT source param is
    named '$' + source-transformation-name + '$'."""
    out = {}
    if not mtt:
        return out
    for p in mtt.get("parameters", []) or []:
        if "SOURCE" not in (p.get("type") or "").upper():
            continue
        txn = (p.get("name") or "").strip("$")
        eo = p.get("extendedObject") or {}
        objs = eo.get("objects") or ([eo.get("object")] if eo.get("object") else [])
        names = [o.get("name") for o in objs if o and o.get("name")]
        if names:
            sch, tbl = _split_obj(names[0])
            if tbl:
                out[txn] = (sch, tbl)
    return out


def _build_graph(imf, mtt):
    """Return (name2tx, rev_adjacency, trace_to_source_fn).

    rev[to_tx] = [(from_tx, to_group_name), ...]
    trace(tx) walks backward to the nearest TmplSource and returns its table."""
    src_tbl = _source_tx_tables(mtt)
    name2tx = {t.get("name"): t for t in imf.transformations}
    grp = {}
    for t in imf.transformations:
        for g in t.get("groups", []) or []:
            grp[g.get("$$ID")] = g.get("name")
    rev = {}
    for l in imf.links:
        fo = imf.index.get(l.get("fromTransformation", {}).get("##ID"), {})
        to = imf.index.get(l.get("toTransformation", {}).get("##ID"), {})
        gname = grp.get(l.get("toGroup", {}).get("##ID"), "")
        rev.setdefault(to.get("name"), []).append((fo.get("name"), gname))

    def trace(txn, seen=None):
        if seen is None:
            seen = set()
        if not txn or txn in seen:
            return None
        seen.add(txn)
        t = name2tx.get(txn, {})
        cn = imf.class_name(t.get("$$class")).split(".")[-1]
        if cn == "TmplSource":
            st = src_tbl.get(txn)
            return st[1] if st else None
        for (frm, _g) in rev.get(txn, []):
            r = trace(frm, seen)
            if r:
                return r
        return None

    return name2tx, rev, trace


def _extract_unions(imf, mtt):
    """Groups of source tables combined by a Union transformation (UNION ALL).

    Only *chunk* unions qualify -- every input must be a DIRECT source (e.g.
    CP_DBCP1/2/3 read straight into the Union).  Delta/heterogeneous unions
    (whose inputs are joined/aggregated streams) are ignored: their branches
    are different shapes, not chunks of one table."""
    src_tbl = _source_tx_tables(mtt)
    name2tx, rev, _trace = _build_graph(imf, mtt)
    groups = []
    for t in imf.transformations:
        if not imf.class_name(t.get("$$class")).split(".")[-1] == "TmplUnion":
            continue
        parts, all_direct = [], True
        for (frm, _g) in rev.get(t.get("name"), []):
            ft = name2tx.get(frm, {})
            if not imf.class_name(ft.get("$$class")).split(".")[-1] == "TmplSource":
                all_direct = False
                break
            st = src_tbl.get(frm)
            tbl = st[1] if st else None
            if tbl and tbl not in parts:
                parts.append(tbl)
        if all_direct and len(parts) > 1:
            groups.append(parts)
    return groups


def _extract_joins(imf, mtt, target_tables):
    """Reconstruct source-to-source joins from Joiner transformations.

    Traces each Joiner's Master/Detail inputs back to their originating source
    tables via the link graph, and pairs them with the Joiner's joinConditions.
    Joins where one side is the mapping target (SCD2 look-back against the
    existing target) are skipped -- Coalesce handles that internally."""
    name2tx, rev, trace = _build_graph(imf, mtt)

    edges = []
    for t in imf.transformations:
        if not imf.class_name(t.get("$$class")).split(".")[-1] == "TmplJoiner":
            continue
        sides = {}
        for (frm, g) in rev.get(t.get("name"), []):
            if g in ("Master", "Detail") and g not in sides:
                sides[g] = trace(frm)
        master, detail = sides.get("Master"), sides.get("Detail")
        if not master or not detail or master == detail:
            continue
        if master in target_tables or detail in target_tables:
            continue                      # SCD2 look-back, not a real join
        conds = []
        for c in t.get("joinConditions", []) or []:
            l = clean_join_operand(c.get("leftOperand", ""))
            r = clean_join_operand(c.get("rightOperand", ""))
            if l and r:
                conds.append((l, c.get("operator", "="), r))
        if conds:
            edges.append(JoinEdge(master=master, detail=detail,
                                  join_type=t.get("joinType", "Normal Join"),
                                  conditions=conds))
    return edges


def _sorter_order_by(imf, rev, name2tx, tx_name):
    """ORDER BY list from the nearest upstream Sorter: [(FIELD, ascending)].

    Informatica's stateful variables depend on row order, which a Sorter
    upstream of the Expression establishes."""
    seen = set()
    stack = [tx_name]
    while stack:
        cur = stack.pop(0)
        if not cur or cur in seen:
            continue
        seen.add(cur)
        t = name2tx.get(cur, {})
        if imf.class_name(t.get("$$class")).split(".")[-1] == "TmplSorter":
            out = []
            for e in t.get("sortEntries", []) or []:
                fn = e.get("fieldName")
                if fn:
                    asc = str(e.get("ascending", "true")).lower() == "true"
                    out.append((fn.upper(), asc))
            if out:
                return out
        for (frm, _g) in rev.get(cur, []):
            stack.append(frm)
    return []


def _resolve_stateful_vars(fields, order_by):
    """Turn Informatica stateful variables into SQL window functions.

    In Informatica an Expression's ports are evaluated top-down and variables
    PERSIST across rows.  So a field that references a variable assigned LATER
    in the port list reads that variable's value from the PREVIOUS row.  That
    is exactly LAG():

        v_prev = v_curr ; ... ; v_curr = MD5_KEY
            -> any forward reference to v_curr  ==  LAG(MD5_KEY) OVER (...)

    The one exception is a self-incrementing counter
    (v_n = v_row_num + 1, where v_row_num is computed from v_n): that is a
    per-group row counter, i.e. ROW_NUMBER().

    Returns {lowercased_var_name: sql_expression} for forward-referenced vars.
    """
    if not order_by:
        return {}
    names = [(f.get("name") or "") for f in fields]
    raw = {}
    for n, f in zip(names, fields):
        if n:
            raw[n.lower()] = f.get("expression") or ""

    def refs(expr):
        return {t.lower() for t in re.findall(r'\b[A-Za-z_][A-Za-z0-9_]*\b', expr or "")
                if t.lower() in raw}

    # variables involved in a reference cycle are counters
    def reaches(start, target, depth=0):
        if depth > 12:
            return False
        for r in refs(raw.get(start, "")):
            if r == target or reaches(r, target, depth + 1):
                return True
        return False

    ob = ", ".join(f"{c}{'' if asc else ' DESC'}" for c, asc in order_by)

    # backward-only inlining, so a LAG argument never contains a forward ref
    inlined, resolved = {}, {}
    for i, n in enumerate(names):
        if not n:
            continue
        e = raw[n.lower()]
        for j in range(i):
            pn = names[j]
            if pn and pn.lower() in inlined:
                e = re.sub(r'\b' + re.escape(pn) + r'\b',
                           lambda _m, d=inlined[pn.lower()]: f"({d})",
                           e, flags=re.IGNORECASE)
        inlined[n.lower()] = e

    def forward_used(i, nl):
        return any(nl in refs(raw[names[k].lower()])
                   for k in range(i) if names[k])

    # "v_prev_x = v_curr_x" style aliases, used to find the partition key
    alias_of = {}
    for n in names:
        if not n:
            continue
        d = (raw[n.lower()] or "").strip()
        if d.lower() in raw:
            alias_of[n.lower()] = d.lower()

    # pass 1: plain previous-row variables -> LAG(base)
    lag_base_of = {}
    for i, n in enumerate(names):
        if not n:
            continue
        nl = n.lower()
        if not forward_used(i, nl) or reaches(nl, nl):
            continue
        base = inlined.get(nl, "").strip()
        if base and not refs(base):
            lag_base_of[nl] = base
            resolved[nl] = f"LAG({base}) OVER (ORDER BY {ob})"

    # pass 2: cyclic variables.  Only an arithmetic self-increment is a row
    # counter (ROW_NUMBER); any other cycle (e.g. carry-forward of the last
    # accepted value) has no safe single-expression SQL form -> leave it.
    for i, n in enumerate(names):
        if not n:
            continue
        nl = n.lower()
        if not forward_used(i, nl) or not reaches(nl, nl):
            continue
        if not re.search(r'\+\s*1\b', raw.get(nl, "")):
            continue                              # not a counter -> unresolved
        # partition key: a LAG variable used alongside the counter
        partition = None
        for k, kn in enumerate(names):
            if not kn or nl not in refs(raw[kn.lower()]):
                continue
            for t in refs(raw[kn.lower()]):
                if t == nl:
                    continue
                tgt = alias_of.get(t, t)
                if tgt in lag_base_of:
                    partition = lag_base_of[tgt]
                    break
            if partition:
                break
        part = f"PARTITION BY {partition} " if partition else ""
        resolved[nl] = f"ROW_NUMBER() OVER ({part}ORDER BY {ob})"
    return resolved


def build_ir(dtemplate_path: str, mtt_path: str | None, folder: str) -> MappingIR:
    imf = load_dtemplate(dtemplate_path)
    ir = MappingIR(name=imf.name, folder=folder)

    # --- MTT bindings (concrete source/target tables) ---
    mtt = None
    if mtt_path and os.path.exists(mtt_path):
        mtt = load_mtt(mtt_path)
        ir.sources, ir.targets, ir.target_update_cols = _mtt_bindings(mtt)

    # --- source-to-source joins + unions reconstructed from the DAG ---
    target_tables = {t for _s, t in ir.targets}
    ir.joins = _extract_joins(imf, mtt, target_tables)
    ir.unions = _extract_unions(imf, mtt)

    # link graph, reused below to find the Sorter that orders each Expression
    name2tx_all, rev_adj, _tr = _build_graph(imf, mtt)

    # --- transformations ---
    for tx in imf.transformations:
        role = _role(imf, tx)
        ir.tx_roles[role] = ir.tx_roles.get(role, 0) + 1
        if role == "Sequence":
            ir.has_sequence = True
        elif role == "Aggregator":
            ir.has_aggregator = True
        elif role == "Router":
            ir.has_router = True
        elif role == "Union":
            ir.has_union = True

        if role == "Expression":
            # Informatica evaluates fields top-down; variable (v_*) fields are
            # local and must be INLINED into the output expressions (they are
            # not real columns).  Build the substitution as we go so each field
            # only sees earlier-defined variables.
            # Every field (variable v_* AND output o_*/out_*) can be referenced
            # by a LATER field; SQL has no such self-reference, so inline them.
            var_defs = {}   # lowercase field name -> already-inlined expression

            # Forward-referenced variables read the PREVIOUS row's value in
            # Informatica; resolve them to SQL window functions up front so the
            # inliner below substitutes real SQL instead of leaving a NULL.
            flds_all = tx.get("fields", []) or []
            var_defs.update(_resolve_stateful_vars(
                flds_all,
                _sorter_order_by(imf, rev_adj, name2tx_all, tx.get("name"))))

            def _inline(expr):
                if not expr or not var_defs:
                    return expr
                prev = None
                out = expr
                for _ in range(40):
                    if out == prev:
                        break
                    prev = out
                    for nm_l, defn in var_defs.items():
                        out = re.sub(r'\b' + re.escape(nm_l) + r'\b',
                                     lambda _m, d=defn: f"({d})", out,
                                     flags=re.IGNORECASE)
                return out

            for f in tx.get("fields", []) or []:
                nm = f.get("name", "")
                ex = f.get("expression") or ""
                ir.ref_ports |= _extract_ports(ex)
                ex = _inline(ex)
                # register this field so later fields can inline it
                if nm:
                    var_defs[nm.lower()] = ex
                if idmc_expr.is_variable_field(nm):
                    continue
                dt = ""
                pt = f.get("platformType") or {}
                if isinstance(pt, dict) and pt.get("##SID"):
                    dt = platform_type_to_snowflake(pt["##SID"],
                                                    f.get("precision"),
                                                    f.get("scale"))
                ir.outputs.append(Col(name=nm.upper(), dtype=dt,
                                      transform=idmc_expr.translate(ex),
                                      is_output=True))

        elif role == "Lookup":
            conds = []
            for c in tx.get("lookupConditions", []) or []:
                conds.append((str(c.get("leftOperand", "")).upper(),
                              c.get("operator", "="),
                              str(c.get("rightOperand", "")).upper()))
                ir.ref_ports.add(str(c.get("rightOperand", "")).upper())
            lfields = []
            for f in tx.get("fields", []) or []:
                nm = f.get("name", "")
                if not nm:
                    continue
                dt = ""
                pt = f.get("platformType") or {}
                if isinstance(pt, dict) and pt.get("##SID"):
                    dt = platform_type_to_snowflake(pt["##SID"],
                                                    f.get("precision"),
                                                    f.get("scale"))
                lfields.append(Col(name=nm.upper(), dtype=dt))
            ips = tx.get("inputPortNames") or []
            if isinstance(ips, str):
                ips = [p.strip() for p in ips.split(",") if p.strip()]
            ir.lookups.append(Lookup(
                table=_lookup_table_name(tx.get("name", "")),
                conditions=conds, fields=lfields,
                unconnected=str(tx.get("unconnected", "")).lower() == "true",
                name=tx.get("name", ""),
                return_port=(tx.get("returnPortName") or "").upper(),
                input_ports=[str(p).upper() for p in ips]))

        elif role == "Filter":
            for a in tx.get("advancedProperties", []) or []:
                if str(a.get("name", "")).lower().startswith("filter"):
                    val = a.get("value")
                    if val:
                        ir.filters.append(idmc_expr.translate(val))
            fc = tx.get("filterCondition")
            if fc:
                ir.filters.append(idmc_expr.translate(fc))

    return ir
