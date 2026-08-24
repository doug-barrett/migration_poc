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
class Lookup:
    table: str                         # reconstructed table name (dw_party ...)
    conditions: list                   # [(lkp_col, op, in_port), ...]
    fields: list = field(default_factory=list)   # [Col, ...] embedded schema
    unconnected: bool = True


@dataclass
class MappingIR:
    name: str                          # mapping name  (m_...)
    folder: str                        # export folder  (03_dq, 04_cnf, ...)
    sources: list = field(default_factory=list)   # [(schema, table), ...]
    targets: list = field(default_factory=list)   # [(schema, table), ...]
    target_update_cols: list = field(default_factory=list)
    lookups: list = field(default_factory=list)   # [Lookup, ...]
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


def build_ir(dtemplate_path: str, mtt_path: str | None, folder: str) -> MappingIR:
    imf = load_dtemplate(dtemplate_path)
    ir = MappingIR(name=imf.name, folder=folder)

    # --- MTT bindings (concrete source/target tables) ---
    if mtt_path and os.path.exists(mtt_path):
        mtt = load_mtt(mtt_path)
        ir.sources, ir.targets, ir.target_update_cols = _mtt_bindings(mtt)

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
            for f in tx.get("fields", []) or []:
                nm = f.get("name", "")
                ex = f.get("expression") or ""
                ir.ref_ports |= _extract_ports(ex)
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
            ir.lookups.append(Lookup(
                table=_lookup_table_name(tx.get("name", "")),
                conditions=conds, fields=lfields,
                unconnected=str(tx.get("unconnected", "")).lower() == "true"))

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
