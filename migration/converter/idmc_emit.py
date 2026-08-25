"""
idmc_emit.py  --  Render migration IR as Coalesce V1 node YAML.

Model (automatic pass):
  * one BRONZE Source node per external (raw / reference) table
  * one node per Informatica mapping, in a layer chosen from its target:
        03_dq / STG_* / *_VALIDATION / *_TEMP     -> SILVER  Stage
        05_dm_Currentview / MRVCONS_*             -> GOLD    View
        conformed DW_*_TRNX/EVENT/BALANCE/...      -> GOLD    Fact
        conformed DW_*                            -> GOLD    Dimension
        other conformed                           -> GOLD    Stage
  * dependencies + joinCondition give node-level lineage:
        FROM <primary source>
        LEFT JOIN <lookup table> ON <lkp key> = <source key>   (per lookup)
        WHERE <filter conditions>
  * Expression outputs become column transforms (Informatica->Snowflake).

Existing hand-built nodes in the repo are NEVER overwritten -- they are the
full-fidelity exemplars and are protected by name.

Column fidelity note: parameterised IDMC sources/targets carry no schema in the
export, so source columns are reconstructed from mapping usage + embedded
lookup schemas (the same best-effort approach the pilot used).  This is stated
in each generated node's description.
"""
from __future__ import annotations
import hashlib
import os
import re
import yaml

import idmc_expr


# ---- deterministic identity ------------------------------------------------
def stable_uuid(s: str) -> str:
    h = hashlib.md5(s.encode()).hexdigest()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


# ---- type heuristics -------------------------------------------------------
def heuristic_type(col: str) -> str:
    c = (col or "").upper()
    if c.endswith("_KEY") or c.endswith("_SK"):
        return "NUMBER(38,0)"
    if re.search(r'(_DTTM|_DATE|_TSTP|_TIME|_TS|_DT)$', c):
        return "TIMESTAMP"
    if re.search(r'(_AMT|_AMOUNT|_QTY|_NUM|_COUNT|_RATE|_PCT|_BAL|_VALUE)$', c):
        return "NUMBER(18,2)"
    if c.endswith("_IND") or c.endswith("_FLAG"):
        return "VARCHAR(1)"
    if c.endswith("_ID") or c.endswith("_CODE") or c.endswith("_NO"):
        return "VARCHAR(50)"
    if c.startswith("_MRV_MD5") or c.endswith("_MD5"):
        return "VARCHAR(32)"
    return "VARCHAR(256)"


def clean_output_name(name: str) -> str:
    """Map an Expression output port to its target column name by stripping
    the conventional out_/o_tgt_/o_ prefixes."""
    n = name
    for pfx in ("O_TGT_", "OUT_TGT_", "O_TARGET_", "OUT_", "O_", "TGT_"):
        if n.upper().startswith(pfx):
            return n[len(pfx):].upper()
    return n.upper()


# ---- port -> source column (strip lookup-input prefixes) -------------------
def source_col_for_port(port: str) -> str:
    p = port.upper()
    for pfx in ("IN_", "LN_", "N_UN_", "N_", "P_", "REF_"):
        if p.startswith(pfx):
            return p[len(pfx):]
    return p


# ---- layer / node-type classification --------------------------------------
STG_SCHEMA = re.compile(r'^STG_')
FACT_PAT = re.compile(r'(TRNX|TRANSACTION|CFLOW|EVENT|SDUTY|BALANCE|EXPOSURE|'
                      r'INCOME_STREAM_DTL|SUBREQ_HIST)')


def classify(schema: str, table: str, folder: str):
    """Return (location, sqlType, materializationType)."""
    t = (table or "").upper()
    s = (schema or "").upper()
    if (folder == "03_dq" or STG_SCHEMA.match(s)
            or t.endswith("_DATA_VALIDATION") or t.endswith("_VALIDATION")
            or t.endswith("_TEMP")):
        return ("SILVER", "Stage", "table")
    if folder == "05_dm_Currentview" or s.startswith("MRVCONS"):
        return ("GOLD", "View", "view")
    if t.startswith("DW_") and FACT_PAT.search(t):
        return ("GOLD", "Fact", "table")
    if t.startswith("DW_"):
        return ("GOLD", "Dimension", "table")
    return ("GOLD", "Stage", "table")


def source_token(mapping_name: str) -> str:
    """'m_rp_dcustctrct_dw_cust_contract' -> 'RP'  (disambiguates shared
    targets)."""
    n = re.sub(r'^m_', '', mapping_name, flags=re.IGNORECASE)
    tok = n.split("_")[0]
    return tok.upper()


# ---------------------------------------------------------------------------
def _existing_node_names(nodes_dir: str) -> set:
    return set(_existing_node_locs(nodes_dir).keys())


def _existing_node_locs(nodes_dir: str) -> dict:
    """name -> location, for hand-built nodes already in the repo."""
    locs = {}
    if os.path.isdir(nodes_dir):
        for f in os.listdir(nodes_dir):
            if f.endswith(".yml") and "-" in f:
                loc, name = f[:-4].split("-", 1)
                locs[name] = loc
    return locs


def _existing_node_cols(nodes_dir: str) -> dict:
    """name -> {col: (node_id, col_id, dtype)} for nodes already on disk, so
    refs to hand-built / pilot nodes resolve in the column registry."""
    out = {}
    if not os.path.isdir(nodes_dir):
        return out
    for f in os.listdir(nodes_dir):
        if not (f.endswith(".yml") and "-" in f):
            continue
        try:
            d = yaml.safe_load(open(os.path.join(nodes_dir, f)))
        except Exception:
            continue
        nid = d.get("id")
        reg = {}
        for c in d.get("operation", {}).get("metadata", {}).get("columns", []) or []:
            cr = c.get("columnReference", {}) or {}
            cc = cr.get("columnCounter")
            if cc:
                reg[c["name"]] = (nid, cc, c.get("dataType", ""))
        out[d.get("name")] = reg
    return out


def _lookup_schema_registry(irs) -> dict:
    """table -> {COL: dtype}  from embedded lookup field schemas."""
    reg = {}
    for ir in irs:
        for lk in ir.lookups:
            if not lk.fields:
                continue
            d = reg.setdefault(lk.table, {})
            for c in lk.fields:
                d.setdefault(c.name, c.dtype or heuristic_type(c.name))
    return reg


def _primary_source(ir):
    """Pick a mapping's primary (driving) source: prefer one matching the
    mapping's source token, else the first bound source."""
    tok = source_token(ir.name).lower()
    for (s, t) in ir.sources:
        if tok and tok in t.lower():
            return (s, t)
    return ir.sources[0] if ir.sources else None


def _clean_port(sc: str) -> bool:
    return bool(re.match(r'^[A-Z_][A-Z0-9_]*$', sc)) and not sc.startswith("V_")


def _joinkey_registry(irs) -> dict:
    """table -> set(columns) that appear as Joiner keys for that table.  Gives
    otherwise-schemaless source views enough columns to exist as nodes so join
    chains through them resolve."""
    reg = {}
    for ir in irs:
        for e in ir.joins:
            for (mcol, _op, dcol) in e.conditions:
                if mcol:
                    reg.setdefault(e.master, set()).add(mcol)
                if dcol:
                    reg.setdefault(e.detail, set()).add(dcol)
    return reg


def _source_ports_registry(irs) -> dict:
    """table -> set(reconstructed source columns) from mapping usage.

    A mapping's referenced ports are attributed to its PRIMARY source (the
    driving table), so multi-source mappings still reconstruct a schema for
    their raw primary source -- not only sole-source mappings."""
    reg = {}
    for ir in irs:
        prim = _primary_source(ir)
        if not prim:
            continue
        cols = reg.setdefault(prim[1], set())
        for p in ir.ref_ports:
            sc = source_col_for_port(p)
            if _clean_port(sc):
                cols.add(sc)
    return reg


# ---- node spec -------------------------------------------------------------
class NodeSpec:
    def __init__(self, name, location, sqltype, materialization):
        self.name = name
        self.location = location
        self.sqltype = sqltype
        self.materialization = materialization
        self.uuid = stable_uuid(f"{location}.{name}")
        self.description = ""
        self.columns = []          # [(colname, dtype, transform, is_key)]
        self.deps = []             # [(location, nodename, alias, kind)]
        self.join_condition = ""
        self.is_source = False
        self.primary_source = None   # table name of the FROM source
        self.surrogate_cols = set()  # columns to flag isSurrogateKey
        self.alias_nodes = []        # [(alias, [nodeName,...])] in join order

    def col_uuid(self, colname):
        return stable_uuid(f"{self.location}.{self.name}.{colname}")


IDENT = re.compile(r'^[A-Z_][A-Z0-9_]*$')


_RESERVED = {"DATE", "TIME", "TIMESTAMP", "NUMBER", "TABLE", "VALUES",
             "COL_NAME", "SET_PROCESS", "ALTER_TYPE"}


_SQL_KW = {
    "CASE", "WHEN", "THEN", "ELSE", "END", "AND", "OR", "NOT", "IN", "IS",
    "NULL", "TRUE", "FALSE", "LIKE", "RLIKE", "AS", "CAST", "DISTINCT",
    "BETWEEN", "OVER", "PARTITION", "BY", "ORDER", "ASC", "DESC", "INTERVAL",
    "CURRENT_TIMESTAMP", "CURRENT_DATE", "DATE", "TIMESTAMP", "NUMBER",
    "VARCHAR", "FLOAT", "BOOLEAN", "DECIMAL", "INTEGER",
    # date-part units used by DATEADD / DATE_PART (not columns)
    "YEAR", "QUARTER", "MONTH", "WEEK", "DAY", "HOUR", "MINUTE", "SECOND",
    "MILLISECOND", "MICROSECOND", "NANOSECOND", "DAYOFWEEK", "DAYOFYEAR",
    "WEEKISO", "YEAROFWEEK",
}


def _qualify(expr, alias_cols, on_missing=None):
    """Prefix bare column identifiers with the alias of the first join source
    that carries them (skips string literals, /*comments*/, function names,
    already-qualified refs, and SQL keywords).  Unknown identifiers are passed
    to on_missing(tok) which may add the column to the primary source and
    return its alias."""
    if not expr:
        return expr
    out, i, n = [], 0, len(expr)
    while i < n:
        c = expr[i]
        if c == "'":                                   # string literal
            j = i + 1
            while j < n:
                if expr[j] == "'":
                    if j + 1 < n and expr[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            out.append(expr[i:j + 1]); i = j + 1; continue
        if expr[i:i + 2] == "/*":                       # comment
            j = expr.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append(expr[i:j]); i = j; continue
        m = re.match(r'[A-Za-z_][A-Za-z0-9_]*', expr[i:])
        if m:
            tok = m.group(0); end = i + len(tok)
            k = end
            while k < n and expr[k] == ' ':
                k += 1
            is_func = k < n and expr[k] == '('
            is_qual = i > 0 and expr[i - 1] in ('.', ':')
            if not is_func and not is_qual and tok.upper() not in _SQL_KW:
                alias = None
                for a, cols in alias_cols:
                    if tok.upper() in cols:
                        alias = a
                        break
                if alias is None and on_missing is not None:
                    alias = on_missing(tok.upper())
                out.append(f"{alias}.{tok}" if alias else tok)
            else:
                out.append(tok)
            i = end; continue
        out.append(c); i += 1
    return "".join(out)


def _is_surrogate(col: str, ir) -> bool:
    """A generated surrogate/sequence key: ends _KEY/_SK, or ends _NUM/_RRN /
    is RRN when the mapping has a Sequence generator."""
    c = (col or "").upper()
    if c.endswith("_KEY") or c.endswith("_SK") or c.endswith("_SRGT_KEY"):
        return True
    if getattr(ir, "has_sequence", False) and (
            c.endswith("_NUM") or c == "RRN" or c.endswith("_RRN")):
        return True
    return False


def _valid_table(t: str) -> bool:
    """A real relational table name (not a flat file, debug sink, reserved
    word, or a reusable/unconnected-lookup macro name)."""
    if not t or not IDENT.match(t):
        return False
    if t.startswith("DEBUG"):
        return False
    if t in _RESERVED:
        return False
    if re.match(r'^U_?LOOKUP', t) or t.startswith("ULKP"):
        return False
    return True


def _plan_specs(irs, produced, external, existing, existing_cols=None):
    lookup_reg = _lookup_schema_registry(irs)
    srcport_reg = _source_ports_registry(irs)
    joinkey_reg = _joinkey_registry(irs)

    # union chunks share one table's schema -> pool columns across members so
    # every chunk (even one that is never a primary or a join key) gets a node.
    union_cols_for = {}
    for ir in irs:
        for g in ir.unions:
            pool = {}
            for t in g:
                for c, dt in lookup_reg.get(t, {}).items():
                    pool.setdefault(c, dt)
                for c in sorted(srcport_reg.get(t, set())):
                    pool.setdefault(c, heuristic_type(c))
                for c in sorted(joinkey_reg.get(t, set())):
                    pool.setdefault(c, heuristic_type(c))
            for t in g:
                union_cols_for.setdefault(t, {}).update(pool)

    # Real external sources = MTT-bound source objects  +  lookup tables that
    # carry an embedded schema.  Everything else (unconnected-lookup macros,
    # port/sequence names mistaken for tables) is dropped.
    mtt_sources = set()
    src_schema = {}
    for ir in irs:
        for s, t in ir.sources:
            mtt_sources.add(t)
            src_schema.setdefault(t, s)
    lookup_fielded = set(lookup_reg.keys())

    # In-place tables: read AND written by the same mapping (e.g. DQ
    # validations that update a staging table in place).  The write side is the
    # produced (SILVER) node; the read side is a raw BRONZE source, created
    # under the name "<TABLE>_RAW" so the two nodes never collide.
    inplace = set()
    for ir in irs:
        srct = {t for _s, t in ir.sources}
        tgtt = {t for _s, t in ir.targets}
        inplace |= (srct & tgtt)

    external = {t: external.get(t, src_schema.get(t, ""))
                for t in (mtt_sources | lookup_fielded)
                if t not in produced}

    # writers per target table (for disambiguation)
    writers = {}
    for ir in irs:
        for _s, t in ir.targets:
            writers.setdefault(t, []).append(ir.name)

    specs = {}          # node_name -> NodeSpec
    registry = {}       # node_name -> {col: (node_uuid, col_uuid)}

    def node_ref(table):
        """Resolve a referenced table to (location, node_name) if we have a
        node for it (produced target or external source); else None."""
        if table in produced:
            sch, mname = produced[table]
            # find the emitted node name for this producer/target
            return _target_node_name(table, writers), _loc_for_target(table, sch)
        if table in external:
            return table, "BRONZE"
        return None

    def _loc_for_target(table, sch):
        loc, _t, _m = classify(sch, table, "")
        return loc

    def _target_node_name(table, writers):
        # single writer -> table name; else it's produced by one mapping we
        # picked (setdefault) so still the table name (siblings handled below)
        return table

    # --- BRONZE source nodes for external tables ---
    for tbl, sch in sorted(external.items()):
        if tbl in existing or not _valid_table(tbl):
            continue
        cols = {}
        for c, dt in lookup_reg.get(tbl, {}).items():
            cols[c] = dt
        if not cols:
            for c in sorted(srcport_reg.get(tbl, set())):
                cols[c] = heuristic_type(c)
        # always include Joiner-key columns so schemaless source views still
        # get a node (and the join chains through them resolve)
        for c in sorted(joinkey_reg.get(tbl, set())):
            cols.setdefault(c, heuristic_type(c))
        # union chunks inherit the pooled schema of their group
        for c, dt in union_cols_for.get(tbl, {}).items():
            cols.setdefault(c, dt)
        if not cols:
            # last resort: keys used to join to this table, so the node is not
            # empty and lineage still resolves
            for ir in irs:
                for lk in ir.lookups:
                    if lk.table == tbl:
                        for (lcol, _op, _p) in lk.conditions:
                            cols.setdefault(lcol, heuristic_type(lcol))
        if not cols:
            continue   # nothing recoverable -> skip (avoid empty source node)
        spec = NodeSpec(tbl, "BRONZE", "Source", "table")
        spec.is_source = True
        spec.description = (f"Migrated source (IICS): {tbl}. Columns "
                            f"reconstructed from mapping usage / lookup schema.")
        for c, dt in cols.items():
            spec.columns.append((c, dt, "", False))
        specs[tbl] = spec

    # --- raw BRONZE source for in-place (read==write) tables ---
    raw_of = {}   # table -> raw source node name
    for tbl in sorted(inplace):
        if not _valid_table(tbl):
            continue
        # In Informatica this is ONE physical table read+written by the same
        # mapping.  If a BRONZE source node for it already exists (e.g. created
        # for the pilot), reuse it rather than adding a second <T>_RAW node for
        # the same table.
        if (existing or {}).get(tbl) == "BRONZE":
            raw_of[tbl] = tbl
            continue
        raw_name = f"{tbl}_RAW"
        if raw_name in existing or raw_name in specs:
            raw_of[tbl] = raw_name
            continue
        cols = {c: heuristic_type(c) for c in sorted(srcport_reg.get(tbl, set()))}
        for c, dt in lookup_reg.get(tbl, {}).items():
            cols.setdefault(c, dt)
        if not cols:
            continue
        spec = NodeSpec(raw_name, "BRONZE", "Source", "table")
        spec.is_source = True
        spec.description = (f"Raw pre-validation source for in-place mapping "
                            f"target {tbl} (IICS DQ/update-in-place pattern).")
        for c, dt in cols.items():
            spec.columns.append((c, dt, "", False))
        specs[raw_name] = spec
        raw_of[tbl] = raw_name

    # --- one node per mapping (per distinct target) ---
    for ir in irs:
        tgts = ir.targets or [("", re.sub(r'^m_', '', ir.name).upper())]
        for sch, tbl in tgts:
            if not _valid_table(tbl):
                continue
            multi = len(set(writers.get(tbl, []))) > 1
            name = f"{tbl}_{source_token(ir.name)}" if multi else tbl
            if name in existing or name in specs:
                # protect hand-built nodes and avoid clobbering siblings
                if name in existing:
                    continue
                name = f"{tbl}_{source_token(ir.name)}"
                if name in existing or name in specs:
                    continue
            loc, sqltype, mat = classify(sch, tbl, ir.folder)
            spec = NodeSpec(name, loc, sqltype, mat)
            spec.description = (f"Migrated from IICS mapping {ir.name} "
                                f"(folder {ir.folder}). Transforms: "
                                f"{', '.join(f'{k}x{v}' for k, v in ir.tx_roles.items())}.")

            # columns: target update cols (keys) + cleaned expression outputs
            seen = set()
            key_cols = [c for c in ir.target_update_cols]
            for c in key_cols:
                if c in seen:
                    continue
                seen.add(c)
                spec.columns.append((c, heuristic_type(c), "", True))
            for oc in ir.outputs:
                cn = clean_output_name(oc.name)
                if cn in seen:
                    continue
                seen.add(cn)
                dt = oc.dtype or heuristic_type(cn)
                spec.columns.append((cn, dt, oc.transform, False))
            if not spec.columns:
                spec.columns.append(("ROW_ID", "NUMBER(38,0)", "", True))

            # dependencies + join
            spec._ir = ir
            spec._sch = sch
            spec._tbl = tbl
            specs[name] = spec

    # column registry (name -> {col: (node_uuid, col_uuid, dtype)}) used for
    # cross-node lineage and upstream type inheritance.  Seed it with hand-built
    # / pilot nodes read from disk so refs to them resolve too.
    registry.update(existing_cols or {})
    for name, spec in specs.items():
        reg = {}
        for (c, dt, tr, key) in spec.columns:
            reg[c] = (spec.uuid, spec.col_uuid(c), dt)
        registry[name] = reg

    # authoritative table -> (location, node_name) resolver.  Built from the
    # nodes we actually emit (plus hand-built nodes), so refs never point at a
    # location the node was not written to.
    # produced (mapping) nodes take precedence over raw sources, so a
    # downstream ref to an in-place table resolves to the validated node.
    node_of_table = {}
    for name, spec in specs.items():
        if spec.is_source:
            continue
        node_of_table.setdefault(getattr(spec, "_tbl", name),
                                 (spec.location, spec.name))
    for name, spec in specs.items():
        if spec.is_source:
            node_of_table.setdefault(name, (spec.location, spec.name))
    existing_locs = existing if isinstance(existing, dict) else {}

    def resolver(table):
        if table in node_of_table:
            return node_of_table[table]
        if table in existing_locs:
            return (existing_locs[table], table)
        return None

    # resolve joins/deps now that all node names/registries exist
    for name, spec in specs.items():
        if spec.is_source:
            continue
        _resolve_join(spec, spec._ir, resolver, registry, raw_of)

    # ensure every non-source column has a source OR a transform.  A column
    # with neither is either a surrogate key (flag it) or a pass-through whose
    # source column the reconstruction missed (add it to the primary source so
    # the reference resolves).
    for name, spec in specs.items():
        if spec.is_source:
            continue
        dep_names = [nm for (_l, nm) in spec.deps]
        for i, (c, dt, tr, key) in enumerate(spec.columns):
            if tr or any(c in registry.get(dn, {}) for dn in dep_names):
                continue                       # already has transform or source
            if _is_surrogate(c, spec._ir):
                spec.surrogate_cols.add(c)
                continue
            ps = specs.get(spec.primary_source)
            if ps is not None and ps.is_source:
                if c not in {cc for (cc, *_r) in ps.columns}:
                    ps.columns.append((c, dt, "", False))
                    registry.setdefault(ps.name, {})[c] = (
                        ps.uuid, ps.col_uuid(c), dt)
            else:
                spec.columns[i] = (
                    c, dt, "NULL /* TODO: source column not reconstructed */", key)

    # every Dimension/Fact needs at least one business key or the generated
    # MERGE has an empty ON clause.  If none was flagged (target had no update
    # columns), promote a natural-key column.
    for name, spec in specs.items():
        if spec.is_source or spec.sqltype not in ("Dimension", "Fact"):
            continue
        # a surrogate key that is also a target-update col does NOT count as a
        # business key (it renders as isSurrogateKey) -> ignore surrogates here
        if any(key and c not in spec.surrogate_cols
               for (c, _d, _t, key) in spec.columns):
            continue
        cand = None
        for pref in (r'_KEY$', r'_ID$', r'_CODE$', r'_NUM$'):
            for i, (c, dt, tr, key) in enumerate(spec.columns):
                if c in spec.surrogate_cols or c.startswith("SYSTEM_"):
                    continue
                if re.search(pref, c):
                    cand = i
                    break
            if cand is not None:
                break
        if cand is None:                      # fall back to first eligible column
            for i, (c, dt, tr, key) in enumerate(spec.columns):
                if c not in spec.surrogate_cols and not c.startswith("SYSTEM_"):
                    cand = i
                    break
        if cand is not None:
            c, dt, tr, _ = spec.columns[cand]
            spec.columns[cand] = (c, dt, tr, True)

    # qualify bare column identifiers in transforms to their source alias, so
    # multi-join SELECTs are not ambiguous; add any referenced-but-missing
    # column to the primary source so it resolves.  source_names = every node
    # that is a Source (generated spec OR an existing/pilot BRONZE node);
    # add_source_cols records columns to append to on-disk source nodes.
    source_names = {n for n, sp in specs.items() if sp.is_source}
    source_names |= {n for n, loc in (existing or {}).items() if loc == "BRONZE"}
    add_source_cols = {}
    for name, spec in specs.items():
        if spec.is_source or not spec.alias_nodes:
            continue
        alias_cols = []
        for alias, nodes in spec.alias_nodes:
            cols = set()
            for nd in nodes:
                cols |= set(registry.get(nd, {}).keys())
            alias_cols.append((alias, cols))
        primary_alias = alias_cols[0][0]
        psrc = spec.primary_source
        primary_is_source = (psrc in source_names)

        def on_missing(tok, _ac=alias_cols, _pa=primary_alias,
                       _src=psrc, _ok=primary_is_source):
            # qualify to the primary alias and make sure the column exists on
            # the primary SOURCE (recorded for on-disk sources not in specs).
            if not _ok:
                return None
            if tok not in _ac[0][1]:
                _ac[0][1].add(tok)
                add_source_cols.setdefault(_src, set()).add(tok)
                sp = specs.get(_src)
                if sp is not None and sp.is_source and \
                        tok not in {cc for (cc, *_r) in sp.columns}:
                    ht = heuristic_type(tok)
                    sp.columns.append((tok, ht, "", False))
                    registry.setdefault(sp.name, {})[tok] = (
                        sp.uuid, sp.col_uuid(tok), ht)
            return _pa

        for i, (c, dt, tr, key) in enumerate(spec.columns):
            if tr:
                spec.columns[i] = (c, dt, _qualify(tr, alias_cols, on_missing), key)

    # unconnected-lookup calls -> real LEFT JOINs (after qualification, so the
    # call arguments are already alias-qualified)
    for name, spec in specs.items():
        if spec.is_source:
            continue
        _resolve_lookup_calls(spec, spec._ir, resolver)

    # propagate precise column types downstream: a pass-through column adopts
    # the dataType of the upstream column it references.  Iterate over the DAG
    # (depth is small: BRONZE->SILVER->GOLD->View) until types stabilise, so
    # the registry the renderer reads holds final, consistent types.
    for _ in range(6):
        changed = False
        for name, spec in specs.items():
            if spec.is_source:
                continue
            dep_names = [nm for (_l, nm) in spec.deps]
            for i, (c, dt, tr, key) in enumerate(spec.columns):
                if tr:
                    continue
                # match the reference resolver: adopt the type of the FIRST
                # dependency that carries this column (break either way).
                for dn in dep_names:
                    up = registry.get(dn, {}).get(c)
                    if up is None:
                        continue
                    if up[2] and up[2] != dt:
                        spec.columns[i] = (c, up[2], tr, key)
                        registry[name][c] = (spec.uuid, spec.col_uuid(c), up[2])
                        changed = True
                    break
        if not changed:
            break

    # drop any <T>_RAW read-side node nothing ended up depending on (the same
    # physical table is already represented by another BRONZE node)
    referenced = {nm for sp in specs.values() for (_l, nm) in sp.deps}
    for nm in [n for n, sp in specs.items()
               if sp.is_source and n.endswith("_RAW") and n not in referenced]:
        del specs[nm]
        registry.pop(nm, None)

    return specs, registry


def _has_joiner_edge(ir, table, joined, norm):
    for e in ir.joins:
        m, d = norm(e.master), norm(e.detail)
        if (table == d and m in joined) or (table == m and d in joined):
            return True
    return False


def _joiner_on(ir, table, joined, norm):
    """Build (join_keyword, on_clause) for `table` using the mapping's
    reconstructed Joiner edges, pairing it with an already-joined counterpart.
    Union-member tables are normalised to their representative for both
    matching and aliasing.  Falls back to a TODO placeholder when no edge
    connects them."""
    for e in ir.joins:
        m, d = norm(e.master), norm(e.detail)
        if table == d and m in joined:
            other, this_is_detail = m, True
        elif table == m and d in joined:
            other, this_is_detail = d, False
        else:
            continue
        a_other, a_this = other.lower(), table.lower()
        preds = []
        for (mcol, op, dcol) in e.conditions:
            if this_is_detail:            # other=master, this=detail
                preds.append(f"{a_other}.{mcol} {op} {a_this}.{dcol}")
            else:                          # other=detail, this=master
                preds.append(f"{a_this}.{mcol} {op} {a_other}.{dcol}")
        kw = "INNER JOIN" if e.join_type == "Normal Join" else "LEFT JOIN"
        on = " AND ".join(preds)
        if e.join_type != "Normal Join":
            on += f" /* {e.join_type} */"
        return kw, on
    return "LEFT JOIN", ("/* MANUAL REVIEW: no Joiner key in export for this "
                         "source (wired via expression lookup, or unused) */ 1=1")


def _resolve_lookup_calls(spec, ir, resolver):
    """Turn unconnected-lookup calls into real LEFT JOINs.

    idmc_expr leaves each ":LKP.name(args)" as "LKPCALL_name(args)".  The
    export gives us, per lookup: the table, its lookupConditions
    (lookup_column = input_port), the ordered inputPortNames, and the
    returnPortName.  So a call maps positionally onto the lookup's inputs and
    the expression becomes "<alias>.<returnPort>", with

        LEFT JOIN <lookup table> <alias> ON <alias>.<key> = <arg>

    One alias per distinct (lookup, argument-list): the same unconnected
    lookup called with different inputs is a different join in SQL."""
    lk_by_name = {}
    for lk in ir.lookups:
        if lk.name:
            lk_by_name[lk.name.lower()] = lk

    used = {a for (a, _n) in spec.alias_nodes}
    call_alias = {}                       # (lkp, args_key) -> (alias, ret)
    new_joins = []

    def resolve_call(lkp_name, args):
        lk = lk_by_name.get(lkp_name.lower())
        if lk is None:
            return None
        r = resolver(lk.table)
        if not r:
            return None
        loc, nodenm = r
        key = (lkp_name.lower(), "|".join(a.strip() for a in args))
        if key in call_alias:
            return call_alias[key]
        base = lkp_name.lower()
        if not base.startswith(("lkp_", "ulkp_")):
            base = f"lkp_{base}"
        alias, n = base, 1
        while alias in used:
            n += 1
            alias = f"{base}_{n}"
        used.add(alias)
        # map each call argument to the lookup column it is compared against
        preds = []
        for i, arg in enumerate(args):
            col = None
            if i < len(lk.input_ports):
                port = lk.input_ports[i]
                for (lcol, _op, rport) in lk.conditions:
                    if rport.upper() == port.upper():
                        col = lcol
                        break
            if col is None and i < len(lk.conditions):
                col = lk.conditions[i][0]          # positional fallback
            if col:
                preds.append(f"{alias}.{col} = {arg.strip()}")
        on = " AND ".join(preds) if preds else \
            "1=1 /* MANUAL REVIEW: lookup key not resolvable */"
        new_joins.append(
            f"LEFT JOIN {{{{ ref('{loc}', '{nodenm}') }}}} {alias} ON {on}")
        if (loc, nodenm) not in spec.deps:
            spec.deps.append((loc, nodenm))
        spec.alias_nodes.append((alias, [nodenm]))
        ret = lk.return_port or (lk.conditions[0][0] if lk.conditions else None)
        call_alias[key] = (alias, ret)
        return call_alias[key]

    pfx = idmc_expr.LKP_CALL_PREFIX

    def resolve_text(text, depth=0):
        """Replace every LKPCALL marker in `text`.  Arguments are resolved
        first (a lookup call can be nested inside another call's argument)."""
        if not text or pfx not in text or depth > 20:
            return text
        out, guard = text, 0
        while pfx in out and guard < 200:
            guard += 1
            found = idmc_expr.find_lkp_call(out)
            if not found:
                break
            start, end, lkp_name, argstr = found
            args = [resolve_text(a, depth + 1)
                    for a in (idmc_expr.split_args(argstr)
                              if argstr.strip() else [])]
            res = resolve_call(lkp_name, args)
            if res and res[1]:
                repl = f"{res[0]}.{res[1]}"
            else:
                repl = f"NULL /* MANUAL REVIEW: lookup {lkp_name} unresolved */"
            out = out[:start] + repl + out[end:]
        return out

    for i, (c, dt, tr, key) in enumerate(spec.columns):
        if tr and pfx in tr:
            spec.columns[i] = (c, dt, resolve_text(tr), key)

    if new_joins:
        lines = spec.join_condition.split("\n")
        where_at = next((j for j, l in enumerate(lines)
                         if l.strip().upper().startswith("WHERE")), len(lines))
        spec.join_condition = "\n".join(
            lines[:where_at] + new_joins + lines[where_at:])
    # a join ON built from an argument may itself hold a nested call
    if pfx in spec.join_condition:
        spec.join_condition = resolve_text(spec.join_condition)


def _resolve_join(spec, ir, resolver, registry, raw_of=None):
    """Fill spec.deps and spec.join_condition using an authoritative
    table->(location, node) resolver.  A node never depends on itself or on a
    sibling target produced by the same mapping (that would be a false cycle),
    except an in-place read==write source, which resolves to its raw
    "<TABLE>_RAW" BRONZE node."""
    raw_of = raw_of or {}
    sibling_tables = {t for (_s, t) in ir.targets}

    # union groups: every member maps to its representative (first part), and
    # a whole group is emitted once as a UNION ALL subquery aliased by the rep.
    rep_of, group_of = {}, {}
    for g in ir.unions:
        if len(g) > 1:
            for t in g:
                rep_of[t] = g[0]
            group_of[g[0]] = g
    norm = lambda t: rep_of.get(t, t)

    def resolve_table(table):
        if table in sibling_tables:
            if table in raw_of:
                return ("BRONZE", raw_of[table])
            return None
        return resolver(table)

    def ref_sql(table):
        """(sql_expr, alias, key_table, node_names) for `table`, registering
        deps.  alias is the actual FROM/JOIN alias; node_names are the nodes
        that alias exposes (so column qualification uses the right schema).  A
        union member expands to a UNION ALL subquery aliased by its rep."""
        rep = norm(table)
        grp = group_of.get(rep)
        if grp:
            branches, nodes = [], []
            for pt in grp:
                pr = resolve_table(pt)
                if pr:
                    deps.append(pr)
                    nodes.append(pr[1])
                    branches.append(f"SELECT * FROM {{{{ ref('{pr[0]}', '{pr[1]}') }}}}")
            if branches:
                return (f"( {' UNION ALL '.join(branches)} ) {rep.lower()}",
                        rep.lower(), rep, nodes)
            return None
        r = resolve_table(table)
        if not r:
            return None
        deps.append(r)
        return (f"{{{{ ref('{r[0]}', '{r[1]}') }}}} {r[1].lower()}",
                r[1].lower(), table, [r[1]])

    deps = []

    # primary source: prefer the join *hub* (the source touching the most
    # Joiner edges) so the fixpoint can reach every other source; fall back to
    # the source-token match, then the first source.
    sources = list(ir.sources)
    degree = {}
    for e in ir.joins:
        for side in (norm(e.master), norm(e.detail)):
            degree[side] = degree.get(side, 0) + 1
    src_tables = [t for (_s, t) in sources]
    primary = None
    if degree:
        hub = max((t for t in src_tables if degree.get(norm(t))),
                  key=lambda t: degree.get(norm(t), 0), default=None)
        if hub:
            primary = next((st for st in sources if st[1] == hub), None)
    if primary is None:
        tok = source_token(ir.name).lower()
        for (s, t) in sources:
            if tok and tok in t.lower():
                primary = (s, t)
                break
    if primary is None and sources:
        primary = sources[0]

    joined, covered = set(), set()
    primary_alias = None
    used_aliases = set()
    if primary:
        res = ref_sql(primary[1])
        if res:
            expr, alias, keytbl, nodes = res
            primary_alias = alias
            used_aliases.add(alias)
            from_line = f"FROM {expr}"
            joined.add(keytbl)
            covered |= set(group_of.get(keytbl, [keytbl]))
            spec.primary_source = nodes[0] if nodes else keytbl
            spec.alias_nodes.append((alias, nodes))
        else:
            from_line = f"-- FROM {primary[1]} (source node not generated)"
    else:
        from_line = "-- FROM <no source bound>"
    join_lines = [from_line]

    # collect the remaining join units (one per source table / union group)
    units = []                       # (key_table, sql_expr)
    for (s, t) in sources:
        if norm(t) in covered:
            continue
        if t in sibling_tables:      # SCD2 look-back on own target -> skip
            continue
        res = ref_sql(t)
        if not res:
            continue
        expr, alias, keytbl, nodes = res
        if keytbl in covered:
            continue
        covered |= set(group_of.get(keytbl, [keytbl]))
        covered.add(keytbl)
        units.append((keytbl, expr, alias, nodes))

    # place a unit once its Joiner counterpart is in the FROM (fixpoint, so
    # chains resolve regardless of ordering).
    def _emit(keytbl, expr, alias, nodes):
        if alias in used_aliases:        # table already joined -> skip duplicate
            joined.add(keytbl)
            return
        used_aliases.add(alias)
        kw, on = _joiner_on(ir, keytbl, joined, norm)
        join_lines.append(f"{kw} {expr} ON {on}")
        joined.add(keytbl)
        spec.alias_nodes.append((alias, nodes))

    progress = True
    while progress and units:
        progress = False
        still = []
        for u in units:
            if _has_joiner_edge(ir, u[0], joined, norm):
                _emit(*u)
                progress = True
            else:
                still.append(u)
        units = still
    for u in units:                  # no Joiner edge (independent / look-back)
        _emit(*u)

    # CONNECTED lookups -> LEFT JOIN against the primary source.  Unconnected
    # ones are resolved from their actual call sites (_resolve_lookup_calls),
    # which knows the real arguments, so they are skipped here.
    for lk in ir.lookups:
        if lk.unconnected:
            continue
        r = resolve_table(lk.table)
        if not r:
            continue
        loc, nm = r
        if nm.lower() in used_aliases:   # already joined -> skip duplicate alias
            continue
        used_aliases.add(nm.lower())
        if (loc, nm) not in deps:
            deps.append((loc, nm))
        ons = []
        for (lcol, op, port) in lk.conditions:
            scol = source_col_for_port(port)
            base = primary_alias or "src"
            ons.append(f"{nm.lower()}.{lcol} {op} {base}.{scol}")
        on = " AND ".join(ons) if ons else "1=1 /* TODO lookup key */"
        join_lines.append(
            f"LEFT JOIN {{{{ ref('{loc}', '{nm}') }}}} {nm.lower()} ON {on}")
        spec.alias_nodes.append((nm.lower(), [nm]))

    if ir.filters:
        join_lines.append("WHERE " + " AND ".join(f"({f})" for f in ir.filters))

    # de-duplicate dependencies, preserving order
    seen, uniq = set(), []
    for d in deps:
        if d not in seen:
            seen.add(d)
            uniq.append(d)
    spec.deps = uniq
    spec.join_condition = "\n".join(join_lines)


# ---- YAML rendering --------------------------------------------------------
def _column_yaml(spec, colname, dtype, transform, is_key, registry, dep_names):
    col_uuid = spec.col_uuid(colname)
    entry = {
        "appliedColumnTests": {},
        "columnReference": {
            "columnCounter": col_uuid,
            "stepCounter": spec.uuid,
        },
        "config": {},
        "dataType": dtype,
        "defaultValue": "",
        "description": "",
        "name": colname,
        "nullable": not is_key,
    }
    if not spec.is_source:
        if colname in spec.surrogate_cols:
            # generated surrogate key: no source, no transform (node type fills)
            entry["isSurrogateKey"] = True
            entry["nullable"] = False
            entry["sourceColumnReferences"] = [{"columnReferences": [],
                                                "transform": ""}]
            return entry
        # resolve a single upstream column reference by name across deps
        if transform:
            srcrefs = [{"columnReferences": [], "transform": transform}]
        else:
            found = None
            for dn in dep_names:
                reg = registry.get(dn, {})
                if colname in reg:
                    nid, cid, up_dtype = reg[colname]
                    found = {"columnCounter": cid, "stepCounter": nid}
                    # inherit the upstream column's precise type
                    if up_dtype:
                        entry["dataType"] = up_dtype
                    break
            if found:
                srcrefs = [{"columnReferences": [found], "transform": ""}]
            else:
                srcrefs = [{"columnReferences": [], "transform": ""}]
        entry["sourceColumnReferences"] = srcrefs
        if is_key and spec.sqltype in ("Dimension", "Fact"):
            entry["isBusinessKey"] = True
    return entry


def _render(spec, registry):
    op = {
        "database": "",
        "deployEnabled": True,
        "description": spec.description,
        "locationName": spec.location,
        "name": spec.name,
        "schema": "",
        "version": 1,
    }
    dep_names = [nm for (_l, nm) in spec.deps]

    if spec.is_source:
        op["sqlType"] = "Source"
        op["type"] = "sourceInput"
        cols = [_column_yaml(spec, c, dt, tr, key, registry, [])
                for (c, dt, tr, key) in spec.columns]
        op["metadata"] = {"columns": cols}
    else:
        op["sqlType"] = spec.sqltype
        op["type"] = "sql"
        op["materializationType"] = spec.materialization
        op["isMultisource"] = False
        op["overrideSQL"] = False
        op["config"] = {"insertStrategy": "INSERT", "preSQL": "",
                        "postSQL": "", "testsEnabled": True}
        cols = [_column_yaml(spec, c, dt, tr, key, registry, dep_names)
                for (c, dt, tr, key) in spec.columns]
        aliases = {nm: stable_uuid(f"{loc}.{nm}") for (loc, nm) in spec.deps}
        deps = [{"locationName": loc, "nodeName": nm} for (loc, nm) in spec.deps]
        op["metadata"] = {
            "appliedNodeTests": [],
            "enabledColumnTestIDs": [],
            "columns": cols,
            "sourceMapping": [{
                "name": "source1",
                "aliases": aliases,
                "customSQL": {"customSQL": ""},
                "dependencies": deps,
                "join": {"joinCondition": spec.join_condition},
                "noLinkRefs": [],
            }],
        }

    return {"fileVersion": 1, "id": spec.uuid, "name": spec.name,
            "operation": op, "type": "Node"}


MANIFEST = ".idmc_generated.json"


def emit_all(irs, produced, external, repo_dir, dry_run=False):
    import json
    nodes_dir = os.path.join(repo_dir, "nodes")
    os.makedirs(nodes_dir, exist_ok=True)

    # Remove nodes generated by a previous run so re-runs are idempotent and
    # overwrite cleanly, while hand-built nodes are never touched.
    manifest_path = os.path.join(nodes_dir, MANIFEST)
    if not dry_run and os.path.exists(manifest_path):
        for stem in json.load(open(manifest_path)):
            p = os.path.join(nodes_dir, stem + ".yml")
            if os.path.exists(p):
                os.remove(p)

    existing = _existing_node_locs(nodes_dir)   # now only hand-built remain
    existing_cols = _existing_node_cols(nodes_dir)

    specs, registry = _plan_specs(
        irs, produced, external, existing, existing_cols)

    written, skipped = [], []
    for name, spec in sorted(specs.items()):
        if name in existing:
            skipped.append(name)
            continue
        doc = _render(spec, registry)
        path = os.path.join(nodes_dir, f"{spec.location}-{spec.name}.yml")
        if not dry_run:
            with open(path, "w") as f:
                yaml.safe_dump(doc, f, sort_keys=True, default_flow_style=False,
                               width=1000)
        written.append(f"{spec.location}-{spec.name}")

    if not dry_run:
        with open(manifest_path, "w") as f:
            json.dump(sorted(written), f, indent=0)

    print(f"generated nodes : {len(written)}")
    print(f"protected (existing): {len(existing)}")
    by_loc = {}
    for w in written:
        by_loc[w.split('-')[0]] = by_loc.get(w.split('-')[0], 0) + 1
    print("by location     :", by_loc)
    return written
