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
    return created, patched


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    complete(args.repo, dry_run=args.dry_run)
