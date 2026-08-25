"""
complete_pilot_sources.py  --  Close dangling references in the repo.

The hand-built pilot nodes reference BRONZE source nodes (and a few columns)
that were never created, which shows up as coa validate warnings
(namedDependencyUnresolved / sourceNodeMissing / sourceColumnMissing).

Column-level lineage in Coalesce is by UUID: a consumer column carries
sourceColumnReferences -> {stepCounter: <upstream node id>, columnCounter:
<upstream column id>}, and the consumer's sourceMapping.aliases maps an
upstream node NAME to that node id.  This tool reads those references and
materialises exactly the missing nodes/columns with the UUIDs the consumers
expect, so lineage resolves cleanly.

Idempotent: only creates/patches what is missing; never edits consumer nodes.
Run:  python3 complete_pilot_sources.py --repo ../..
"""
from __future__ import annotations
import argparse
import glob
import os
import re
import yaml


def _load(nodes_dir):
    nodes = {}
    for f in glob.glob(os.path.join(nodes_dir, "*.yml")):
        d = yaml.safe_load(open(f))
        nodes[f] = d
    return nodes


def _refs_in_join(jc):
    return re.findall(r"ref\(\s*'([^']+)'\s*,\s*'([^']+)'\s*\)", jc or "")


def _ensure_join_columns(nodes_dir, dry_run=False):
    """Make every `alias.COLUMN` referenced in a joinCondition exist on the
    source node that alias points at (join keys the schema reconstruction
    missed -- covers both generated and hand-built nodes).  Only SOURCE nodes
    are augmented."""
    by_name = {}
    for f in glob.glob(os.path.join(nodes_dir, "*.yml")):
        d = yaml.safe_load(open(f))
        by_name[d["name"]] = (f, d)

    def cols_of(d):
        return {c["name"] for c in
                d["operation"].get("metadata", {}).get("columns", []) or []}

    patched = {}
    for f, d in list(by_name.values()):
        op = d["operation"]
        for sm in op.get("metadata", {}).get("sourceMapping", []) or []:
            jc = sm.get("join", {}).get("joinCondition", "") or ""
            # alias -> [node names], parsed per line (left of ' ON ')
            alias_nodes = {}
            for line in jc.splitlines():
                left = re.split(r'\bON\b', line, 1)[0]
                refs = re.findall(r"ref\(\s*'[^']+'\s*,\s*'([^']+)'\s*\)", left)
                toks = re.findall(r'[A-Za-z_][A-Za-z0-9_]*', left)
                if refs and toks:
                    alias_nodes.setdefault(toks[-1].lower(), []).extend(refs)
            # scan the joinCondition AND every column transform for alias.COL
            scan = [jc]
            for col in op.get("metadata", {}).get("columns", []) or []:
                for scr in col.get("sourceColumnReferences", []) or []:
                    scan.append(scr.get("transform", "") or "")
            text = "\n".join(scan)
            # every alias.COLUMN must exist on the aliased source node(s)
            for al, col in re.findall(r'\b([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)', text):
                for nd in alias_nodes.get(al.lower(), []):
                    if nd not in by_name:
                        continue
                    _nf, nde = by_name[nd]
                    if nde["operation"].get("sqlType") != "Source":
                        continue
                    existing_cols = {c["name"].upper(): c
                                     for c in nde["operation"]["metadata"].get("columns", []) or []}
                    if col.upper() in existing_cols:
                        # retype a generic VARCHAR(256) placeholder so date /
                        # number functions on it compile
                        ec = existing_cols[col.upper()]
                        ht = _htype(col)
                        if ec.get("dataType") == "VARCHAR(256)" and ht != "VARCHAR(256)":
                            ec["dataType"] = ht
                            patched.setdefault(nd, 0)
                            patched[nd] += 1
                        continue
                    nde["operation"]["metadata"].setdefault("columns", []).append({
                        "appliedColumnTests": {},
                        "columnReference": {
                            "columnCounter": _stable(nde["id"] + col.upper()),
                            "stepCounter": nde["id"]},
                        "config": {}, "dataType": _htype(col),
                        "defaultValue": "", "description": "",
                        "name": col.upper(), "nullable": True})
                    patched.setdefault(nd, 0)
                    patched[nd] += 1
    for nd, cnt in patched.items():
        f, d = by_name[nd]
        if not dry_run:
            with open(f, "w") as fh:
                yaml.safe_dump(d, fh, sort_keys=True, width=1000)
    print(f"join-column patch: +{sum(patched.values())} cols on {len(patched)} source nodes")
    return patched


def _stable(s):
    import hashlib
    h = hashlib.md5(s.encode()).hexdigest()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


def _htype(col):
    """Heuristic Snowflake type by column-name suffix (so date/number
    functions on reconstructed columns compile)."""
    c = (col or "").upper()
    if c.endswith("_KEY") or c.endswith("_SK"):
        return "NUMBER(38,0)"
    if re.search(r'(_DTTM|_DATE|_TSTP|_TS|_DT|_TIME)$', c):
        return "TIMESTAMP"
    if re.search(r'(_AMT|_AMOUNT|_QTY|_NUM|_RATE|_PCT|_BAL|_VALUE)$', c):
        return "NUMBER(18,2)"
    if c.endswith("_IND") or c.endswith("_FLAG"):
        return "VARCHAR(1)"
    return "VARCHAR(256)"


def complete(repo_dir, dry_run=False):
    nodes_dir = os.path.join(repo_dir, "nodes")
    nodes = _load(nodes_dir)

    id_to_node = {}          # node id -> (location, name, doc)
    name_loc = {}            # (location, name) present?
    for _f, d in nodes.items():
        op = d["operation"]
        id_to_node[d["id"]] = (op["locationName"], d["name"], d)
        name_loc[(op["locationName"], d["name"])] = d

    # uuid -> (location, name) intended, from consumer aliases + join refs
    uuid_loc_name = {}
    referenced_cols = {}     # node_uuid -> set(col_uuid)
    for _f, d in nodes.items():
        op = d["operation"]
        for sm in op.get("metadata", {}).get("sourceMapping", []) or []:
            aliases = sm.get("aliases", {}) or {}
            jrefs = {nm: loc for (loc, nm) in _refs_in_join(
                sm.get("join", {}).get("joinCondition", ""))}
            for dep in sm.get("dependencies", []) or []:
                jrefs.setdefault(dep["nodeName"], dep["locationName"])
            for nm, uid in aliases.items():
                loc = jrefs.get(nm)
                if loc:
                    uuid_loc_name[uid] = (loc, nm)
            for col in op["metadata"].get("columns", []) or []:
                for scr in col.get("sourceColumnReferences", []) or []:
                    for cr in scr.get("columnReferences", []) or []:
                        st = cr.get("stepCounter"); cc = cr.get("columnCounter")
                        if st and cc:
                            referenced_cols.setdefault(st, {})
                            # remember a candidate name/type from this consumer
                            referenced_cols[st].setdefault(
                                cc, (col["name"], col.get("dataType", "VARCHAR(256)")))

    created, patched = [], []

    # 1) missing NODES: a referenced node id with no node file
    for uid, cols in referenced_cols.items():
        if uid in id_to_node:
            continue
        if uid not in uuid_loc_name:
            continue
        loc, name = uuid_loc_name[uid]
        columns = []
        for cc, (cname, ctype) in cols.items():
            columns.append({
                "appliedColumnTests": {},
                "columnReference": {"columnCounter": cc, "stepCounter": uid},
                "config": {}, "dataType": ctype, "defaultValue": "",
                "description": "", "name": cname, "nullable": True,
            })
        doc = {
            "fileVersion": 1, "id": uid, "name": name, "type": "Node",
            "operation": {
                "database": "", "deployEnabled": True,
                "description": (f"Raw source completing pilot lineage for "
                                f"{name} (created by complete_pilot_sources)."),
                "locationName": loc, "name": name, "schema": "",
                "sqlType": "Source", "type": "sourceInput", "version": 1,
                "metadata": {"columns": columns},
            },
        }
        path = os.path.join(nodes_dir, f"{loc}-{name}.yml")
        if not dry_run:
            with open(path, "w") as fh:
                yaml.safe_dump(doc, fh, sort_keys=True, width=1000)
        created.append(f"{loc}-{name} ({len(columns)} cols)")

    # 2) missing COLUMNS on existing nodes
    for uid, cols in referenced_cols.items():
        if uid not in id_to_node:
            continue
        loc, name, doc = id_to_node[uid]
        existing_cc = {c["columnReference"]["columnCounter"]
                       for c in doc["operation"]["metadata"].get("columns", [])
                       if c.get("columnReference")}
        added = 0
        for cc, (cname, ctype) in cols.items():
            if cc in existing_cc:
                continue
            doc["operation"]["metadata"].setdefault("columns", []).append({
                "appliedColumnTests": {},
                "columnReference": {"columnCounter": cc, "stepCounter": uid},
                "config": {}, "dataType": ctype, "defaultValue": "",
                "description": "", "name": cname, "nullable": True,
            })
            added += 1
        if added:
            path = None
            for f, dd in nodes.items():
                if dd is doc:
                    path = f; break
            if path and not dry_run:
                with open(path, "w") as fh:
                    yaml.safe_dump(doc, fh, sort_keys=True, width=1000)
            patched.append(f"{loc}-{name} (+{added} cols)")

    print(f"created source nodes : {len(created)}")
    for c in created:
        print("   +", c)
    print(f"patched columns on   : {len(patched)}")
    for p in patched:
        print("   ~", p)
    _ensure_join_columns(nodes_dir, dry_run=dry_run)
    return created, patched


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    complete(args.repo, dry_run=args.dry_run)
