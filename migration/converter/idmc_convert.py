"""
idmc_convert.py  --  Orchestrate the IDMC -> Coalesce migration.

Pipeline:
  1. discover  : find every DTEMPLATE (mapping logic) and MTT (task/bindings),
                 pair them via frsGuid.
  2. model     : build a MappingIR per mapping (idmc_model).
  3. classify  : globally decide, for every table referenced, whether it is
                 produced inside the package (an internal dependency) or is an
                 external raw source (a BRONZE Source node).
  4. emit      : write Coalesce V1 node YAML (idmc_emit).

Run:
  python3 idmc_convert.py --export <export_dir> --repo <repo_dir> [--summary]
  python3 idmc_convert.py ... --write        # actually write node files
"""
from __future__ import annotations
import argparse
import glob
import json
import os
import zipfile

import idmc_model as M


# ---------------------------------------------------------------------------
def _read_json_from_zip(zip_path: str, member: str):
    with zipfile.ZipFile(zip_path) as z:
        return json.loads(z.read(member))


def _dtemplate_guid(dt_path: str) -> str | None:
    try:
        if dt_path.endswith(".zip"):
            mt = _read_json_from_zip(dt_path, "mappingTemplate.json")
        else:
            mt = json.load(open(os.path.join(dt_path, "mappingTemplate.json")))
        return mt[0].get("assetFrsGuid")
    except Exception:
        return None


def _mtt_guid(mtt_path: str) -> str | None:
    try:
        t = _read_json_from_zip(mtt_path, "mtTask.json")
        mid = t[0].get("mappingId", "")
        return mid.lstrip("@") or None
    except Exception:
        return None


def _folder_of(path: str, export_root: str) -> str:
    rel = os.path.relpath(path, export_root)
    parts = rel.split(os.sep)
    # .../Explore/IICS Minerva/<folder>/<asset>
    try:
        i = parts.index("IICS Minerva")
        return parts[i + 1] if i + 1 < len(parts) - 1 else "IICS Minerva"
    except ValueError:
        return parts[-2] if len(parts) > 1 else ""


def discover(export_root: str):
    """Return list of dicts: {name, folder, dtemplate, mtt}."""
    dts = glob.glob(os.path.join(export_root, "**", "*.DTEMPLATE.zip"),
                    recursive=True)
    # include any manually-extracted DTEMPLATE dir that lacks a .zip sibling
    for d in glob.glob(os.path.join(export_root, "**", "*.DTEMPLATE"),
                       recursive=True):
        if os.path.isdir(d) and not os.path.exists(d + ".zip"):
            dts.append(d)
    mtts = glob.glob(os.path.join(export_root, "**", "*.MTT.zip"),
                     recursive=True)

    mtt_by_guid = {}
    for m in mtts:
        g = _mtt_guid(m)
        if g:
            mtt_by_guid[g] = m

    mappings = []
    for dt in sorted(dts):
        base = os.path.basename(dt).replace(".DTEMPLATE.zip", "") \
                                   .replace(".DTEMPLATE", "")
        guid = _dtemplate_guid(dt)
        mtt = mtt_by_guid.get(guid) if guid else None
        mappings.append({"name": base,
                         "folder": _folder_of(dt, export_root),
                         "dtemplate": dt, "mtt": mtt})
    return mappings


def build_all(export_root: str):
    mappings = discover(export_root)
    irs = []
    for mp in mappings:
        ir = M.build_ir(mp["dtemplate"], mp["mtt"], mp["folder"])
        irs.append(ir)
    return irs


# ---------------------------------------------------------------------------
def classify_tables(irs):
    """Decide, for each table name, whether it is produced in-package.

    Returns:
      produced : {TABLE -> (schema, producing_mapping_name)}
      external : {TABLE -> schema}   (raw sources + lookup tables not produced)
    """
    produced = {}
    for ir in irs:
        for sch, tbl in ir.targets:
            produced.setdefault(tbl, (sch, ir.name))

    external = {}
    for ir in irs:
        for sch, tbl in ir.sources:
            if tbl not in produced:
                external.setdefault(tbl, sch)
        for lk in ir.lookups:
            if lk.table not in produced:
                external.setdefault(lk.table, "")
    return produced, external


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--export", required=True)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--summary", action="store_true")
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    export_root = os.path.expanduser(args.export)
    irs = build_all(export_root)
    produced, external = classify_tables(irs)

    if args.summary:
        print(f"mappings parsed : {len(irs)}")
        no_tgt = [ir.name for ir in irs if not ir.targets]
        print(f"targets resolved: {len(irs) - len(no_tgt)}/{len(irs)}")
        if no_tgt:
            print("  NO TARGET:", no_tgt)
        print(f"produced tables : {len(produced)}")
        print(f"external tables : {len(external)}")
        roles = {}
        for ir in irs:
            for r, c in ir.tx_roles.items():
                roles[r] = roles.get(r, 0) + c
        print("tx roles        :", dict(sorted(roles.items(),
                                               key=lambda x: -x[1])))
        print("\n--- produced (target -> producing mapping) ---")
        for t, (s, m) in sorted(produced.items()):
            print(f"  {s+'.' if s else '':>16}{t:40s} <- {m}")
        print("\n--- external (BRONZE source candidates) ---")
        for t, s in sorted(external.items()):
            print(f"  {s+'.' if s else '':>16}{t}")

    if args.write:
        import idmc_emit
        idmc_emit.emit_all(irs, produced, external, args.repo)


if __name__ == "__main__":
    main()
