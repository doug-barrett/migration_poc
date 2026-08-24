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

    def col_uuid(self, colname):
        return stable_uuid(f"{self.location}.{self.name}.{colname}")


IDENT = re.compile(r'^[A-Z_][A-Z0-9_]*$')


_RESERVED = {"DATE", "TIME", "TIMESTAMP", "NUMBER", "TABLE", "VALUES",
             "COL_NAME", "SET_PROCESS", "ALTER_TYPE"}


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


def _plan_specs(irs, produced, external, existing):
    lookup_reg = _lookup_schema_registry(irs)
    srcport_reg = _source_ports_registry(irs)

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
    # cross-node lineage and upstream type inheritance
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

    return specs, registry


def _has_joiner_edge(ir, table, joined):
    for e in ir.joins:
        if (table == e.detail and e.master in joined) or \
           (table == e.master and e.detail in joined):
            return True
    return False


def _joiner_on(ir, table, joined):
    """Build (join_keyword, on_clause) for `table` using the mapping's
    reconstructed Joiner edges, pairing it with an already-joined counterpart.
    Falls back to a TODO placeholder when no edge connects them."""
    for e in ir.joins:
        if table == e.detail and e.master in joined:
            other, this_cols, other_first = e.master, "detail", True
        elif table == e.master and e.detail in joined:
            other, this_cols, other_first = e.detail, "master", True
        else:
            continue
        a_other, a_this = other.lower(), table.lower()
        preds = []
        for (mcol, op, dcol) in e.conditions:
            if this_cols == "detail":     # other=master, this=detail
                preds.append(f"{a_other}.{mcol} {op} {a_this}.{dcol}")
            else:                          # other=detail, this=master
                preds.append(f"{a_this}.{mcol} {op} {a_other}.{dcol}")
        kw = "INNER JOIN" if e.join_type == "Normal Join" else "LEFT JOIN"
        on = " AND ".join(preds)
        if e.join_type not in ("Normal Join",):
            on += f" /* {e.join_type} */"
        return kw, on
    return "LEFT JOIN", "/* TODO join key (Joiner not resolved) */ 1=1"


def _resolve_join(spec, ir, resolver, registry, raw_of=None):
    """Fill spec.deps and spec.join_condition using an authoritative
    table->(location, node) resolver.  A node never depends on itself or on a
    sibling target produced by the same mapping (that would be a false cycle),
    except an in-place read==write source, which resolves to its raw
    "<TABLE>_RAW" BRONZE node."""
    raw_of = raw_of or {}
    sibling_tables = {t for (_s, t) in ir.targets}

    def resolve_table(table):
        if table in sibling_tables:
            if table in raw_of:
                return ("BRONZE", raw_of[table])
            return None
        return resolver(table)

    deps = []

    # primary source: prefer a source matching the mapping's source token,
    # else the first source.
    sources = list(ir.sources)
    primary = None
    tok = source_token(ir.name).lower()
    for (s, t) in sources:
        if tok and tok in t.lower():
            primary = (s, t)
            break
    if primary is None and sources:
        primary = sources[0]

    from_line = ""
    if primary:
        r = resolve_table(primary[1])
        if r:
            loc, nm = r
            alias = nm.lower()
            from_line = f"FROM {{{{ ref('{loc}', '{nm}') }}}} {alias}"
            deps.append((loc, nm))
        else:
            from_line = f"-- FROM {primary[1]} (source node not generated)"
    else:
        from_line = "-- FROM <no source bound>"

    join_lines = [from_line]
    joined = {primary[1]} if primary else set()

    # other MTT sources -> JOIN using the reconstructed Joiner conditions.
    # Place a source once its Joiner counterpart is already in the FROM (a
    # fixpoint, so join chains resolve regardless of source ordering).
    pending = []
    for (s, t) in sources:
        if primary and t == primary[1]:
            continue
        r = resolve_table(t)
        if r:
            pending.append((t, r))

    def _emit(t, r):
        loc, nm = r
        deps.append((loc, nm))
        kw, on = _joiner_on(ir, t, joined)
        join_lines.append(f"{kw} {{{{ ref('{loc}', '{nm}') }}}} {nm.lower()} ON {on}")
        joined.add(t)

    progress = True
    while progress and pending:
        progress = False
        still = []
        for (t, r) in pending:
            if _has_joiner_edge(ir, t, joined):
                _emit(t, r)
                progress = True
            else:
                still.append((t, r))
        pending = still
    for (t, r) in pending:        # no Joiner edge (SCD2 look-back / independent)
        _emit(t, r)

    # lookups -> LEFT JOIN with ON from lookup conditions
    for lk in ir.lookups:
        r = resolve_table(lk.table)
        if not r:
            continue
        loc, nm = r
        if (loc, nm) not in deps:
            deps.append((loc, nm))
        ons = []
        for (lcol, op, port) in lk.conditions:
            scol = source_col_for_port(port)
            base = primary[1].lower() if primary else "src"
            ons.append(f"{nm.lower()}.{lcol} {op} {base}.{scol}")
        on = " AND ".join(ons) if ons else "1=1 /* TODO lookup key */"
        join_lines.append(
            f"LEFT JOIN {{{{ ref('{loc}', '{nm}') }}}} {nm.lower()} ON {on}")

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

    specs, registry = _plan_specs(irs, produced, external, existing)

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
