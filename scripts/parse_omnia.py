#!/usr/bin/env python3
"""
Parse Omnia Partners WhereScape RED export (CSV + app_obj) and generate
Coalesce node YAML files.

Uses the pre-parsed CSV export (OMNIAPartners.csv) which contains:
- Object metadata (location, name, type)
- Column definitions (name, datatype, nullable)
- Source lineage (src_obj_loc, src_obj_name, src_col_name)
- Transforms (col_trans)
- JOIN conditions (obj_join)
- Business key indicators (col_key_type, col_bk_ind)

Also reads app_obj_*.wst for type-26 (Ds*) objects not in the CSV.
"""

import csv
import re
import uuid
import yaml
import os
import sys
from pathlib import Path
from collections import defaultdict

# Paths
BASE_DIR = Path(__file__).parent.parent.parent
CSV_FILE = BASE_DIR / "OMNIAPartners.csv"
APP_DIR = BASE_DIR / "ws_app_omnia"
OBJ_FILE = APP_DIR / "app_obj_prd_20260622_202606221116.wst"
OUTPUT_DIR = Path(__file__).parent.parent / "nodes"

# UUID namespace for deterministic generation
NS = uuid.UUID("a1b2c3d4-e5f6-7890-abcd-ef1234567890")


def stable_uuid(seed: str) -> str:
    return str(uuid.uuid5(NS, seed))


# ─── DATA TYPE MAPPING ───────────────────────────────────────────────────────

def map_datatype(sql_type: str) -> str:
    if not sql_type:
        return "VARCHAR"
    t = sql_type.strip().lower()
    if t in ("integer", "int"):
        return "NUMBER(38,0)"
    if t == "bigint":
        return "NUMBER(18,0)"
    if t == "smallint":
        return "NUMBER(5,0)"
    if t in ("float", "real"):
        return "FLOAT"
    if t in ("datetime", "datetime2", "smalldatetime"):
        return "TIMESTAMP"
    if t == "date":
        return "DATE"
    if t == "bit":
        return "BOOLEAN"
    if t in ("varchar(max)", "nvarchar(max)", "text", "ntext"):
        return "VARCHAR(16777216)"
    m = re.match(r"(n?varchar|n?char)\((\d+)\)", t)
    if m:
        return f"VARCHAR({m.group(2)})"
    m = re.match(r"(numeric|decimal)\((\d+)(?:,\s*(\d+))?\)", t)
    if m:
        p, s = m.group(2), m.group(3) or "0"
        return f"NUMBER({p},{s})"
    if t == "number" or t.startswith("number"):
        return t.upper()
    if t.startswith("varchar"):
        return t.upper()
    if t.startswith("timestamp"):
        return "TIMESTAMP"
    return t.upper() if t else "VARCHAR"


# ─── LOCATION MAPPING ────────────────────────────────────────────────────────

# Map WhereScape location names (from CSV loc_name field) to Coalesce locations
WS_LOCATION_MAP = {
    "LOAD": "LOAD",
    "STAGE": "STAGE",
    "DIM": "DIM",
    "FACT": "FACT",
    "ODS": "ODS",
    "OPC": "OPC",
    "REPORTS": "REPORTS",
    "OP": "OP",
    "OPS": "OPS",
    "PUBLIC": "PUBLIC",
    "TABLEAU": "TABLEAU",
    "DATARAILS": "DATARAILS",
}

# Map CSV obj_type to Coalesce sqlType
OBJ_TYPE_MAP = {
    "Source": ("Source", "sourceInput", "LOAD"),
    "STAGE": ("Stage", "sql", "STAGE"),
    "PERSISTENT STAGE": ("persistentStage", "sql", "STAGE"),
    "DIMENSION": ("Dimension", "sql", "DIM"),
    "FACT": ("Fact", "sql", "FACT"),
    "VIEW": ("View", "sql", "STAGE"),
    "AGGREGATE": ("Aggregate", "sql", "STAGE"),
}


def resolve_location(csv_loc: str, obj_type: str) -> str:
    """Resolve the Coalesce location for a node."""
    loc = csv_loc.strip().upper()
    if loc in WS_LOCATION_MAP:
        return WS_LOCATION_MAP[loc]
    # Fallback based on obj_type
    if obj_type in OBJ_TYPE_MAP:
        return OBJ_TYPE_MAP[obj_type][2]
    return "STAGE"


def resolve_source_location(src_loc: str) -> str:
    """Resolve source location from the CSV src_obj_loc field."""
    if not src_loc:
        return "LOAD"
    loc = src_loc.strip().upper()
    # Map WhereScape connection names to Coalesce locations
    loc_keywords = {
        "LOAD": "LOAD",
        "STAGE": "STAGE",
        "DIM": "DIM",
        "FACT": "FACT",
        "ODS": "ODS",
        "OPC": "OPC",
        "GOLDEN SOURCES": "LOAD",
        "REFERENCEDATA": "LOAD",
        "NIPA_SALESFORCE": "LOAD",
        "FORCE": "DATALAKE_FORCE",
        "VIZIENT": "DATALAKE_VIZIENT",
        "DATA LAKE": "LOAD",
        "SNOWFLAKE": "LOAD",
        "SNOWFLAKE_STITCH": "LOAD",
    }
    for keyword, coalesce_loc in loc_keywords.items():
        if keyword in loc:
            return coalesce_loc
    return "LOAD"


# ─── JOIN PARSING ────────────────────────────────────────────────────────────

def parse_join_to_coalesce(join_text: str, node_location: str, all_objects: dict) -> tuple:
    """
    Convert WhereScape JOIN text to Coalesce format.
    Returns (joinCondition, dependencies, aliases).
    """
    if not join_text or not join_text.strip():
        return "", [], {}

    # Extract table references from [TABLEOWNER].[Table] or bare table names
    # Pattern: FROM/JOIN ... [TABLEOWNER].[TableName] alias
    # or: FROM/JOIN ... TableName alias ON ...
    table_refs = []

    # Try [TABLEOWNER].[X] pattern first
    tableowner_refs = re.findall(r'\[TABLEOWNER\]\.\[(\w+)\](?:\s+(\w+))?', join_text, re.IGNORECASE)
    if tableowner_refs:
        for table, alias in tableowner_refs:
            alias = alias if alias else table
            table_refs.append((table, alias))
    else:
        # Try bare table/alias pattern from FROM/JOIN clauses
        from_join_refs = re.findall(
            r'(?:FROM|JOIN)\s+(\w+)(?:\s+(?:AS\s+)?(\w+))?',
            join_text, re.IGNORECASE
        )
        for table, alias in from_join_refs:
            if table.upper() not in ('SELECT', 'WHERE', 'ON', 'AND', 'OR', 'LEFT', 'RIGHT', 'INNER', 'OUTER', 'CROSS', 'FULL'):
                alias = alias if alias else table
                table_refs.append((table, alias))

    if not table_refs:
        return "", [], {}

    dependencies = []
    aliases = {}
    join_parts = []

    for i, (table, alias) in enumerate(table_refs):
        # Determine location for this source table
        obj_info = all_objects.get(table, {})
        src_loc = obj_info.get("location", "STAGE")

        node_uuid = stable_uuid(table)
        aliases[table.upper()] = node_uuid
        dependencies.append({"locationName": src_loc, "nodeName": table.upper()})

        if i == 0:
            join_parts.append(f"FROM {{{{ ref('{src_loc}', '{table.upper()}') }}}} \"{alias.upper()}\"")
        else:
            # Determine join type from the original text
            # Look backwards in the text for the join keyword before this table
            join_type = "LEFT JOIN"
            table_pos = join_text.upper().find(table.upper())
            if table_pos > 0:
                preceding = join_text[:table_pos].upper()
                if "INNER JOIN" in preceding[max(0, len(preceding)-30):]:
                    join_type = "INNER JOIN"
                elif "RIGHT" in preceding[max(0, len(preceding)-30):]:
                    join_type = "RIGHT JOIN"
                elif "CROSS" in preceding[max(0, len(preceding)-30):]:
                    join_type = "CROSS JOIN"
                elif "FULL" in preceding[max(0, len(preceding)-30):]:
                    join_type = "FULL JOIN"

            # Extract ON condition for this table
            on_condition = extract_on_condition(join_text, table, alias)
            if on_condition:
                join_parts.append(f"{join_type} {{{{ ref('{src_loc}', '{table.upper()}') }}}} \"{alias.upper()}\"\n  ON {on_condition}")
            else:
                join_parts.append(f"{join_type} {{{{ ref('{src_loc}', '{table.upper()}') }}}} \"{alias.upper()}\"")

    join_condition = "\n".join(join_parts)
    return join_condition, dependencies, aliases


def extract_on_condition(join_text: str, table: str, alias: str) -> str:
    """Extract the ON condition for a specific table join."""
    # Find ON clause after this table reference
    pattern = re.compile(
        rf'(?:\[TABLEOWNER\]\.\[{re.escape(table)}\]|(?:JOIN\s+){re.escape(table)})\s*(?:\w+)?\s*ON\s+(.*?)(?=(?:LEFT|RIGHT|INNER|CROSS|FULL|WHERE|ORDER|GROUP|;|\Z))',
        re.IGNORECASE | re.DOTALL
    )
    match = pattern.search(join_text)
    if match:
        cond = match.group(1).strip()
        # Clean up the condition - replace [TABLEOWNER].[X] with "X"
        cond = re.sub(r'\[TABLEOWNER\]\.\[(\w+)\]', r'"\1"', cond)
        # Replace table.col with "TABLE"."COL"
        cond = re.sub(r'(\w+)\.(\w+)', lambda m: f'"{m.group(1).upper()}"."{m.group(2).upper()}"', cond)
        return cond.strip().rstrip(';').strip()
    return ""


# ─── NODE GENERATION ─────────────────────────────────────────────────────────

def make_column(node_id: str, node_name: str, col_name: str, data_type: str,
                nullable: bool = True, description: str = "",
                src_node_name: str = "", src_col_name: str = "",
                transform: str = "", is_business_key: bool = False,
                is_surrogate_key: bool = False) -> dict:
    """Generate a column entry for a Coalesce node."""
    col_uuid = stable_uuid(f"{node_name}.{col_name.upper()}")

    # Build sourceColumnReferences
    if src_node_name and src_col_name and src_node_name.upper() != node_name.upper():
        src_node_id = stable_uuid(src_node_name)
        src_col_uuid = stable_uuid(f"{src_node_name}.{src_col_name.upper()}")
        src_refs = [{"columnReferences": [{"columnCounter": src_col_uuid, "stepCounter": src_node_id}], "transform": transform}]
    else:
        src_refs = [{"columnReferences": [], "transform": transform}]

    entry = {
        "appliedColumnTests": {},
        "columnReference": {"columnCounter": col_uuid, "stepCounter": node_id},
        "config": {},
        "dataType": map_datatype(data_type),
        "description": description,
        "name": col_name.upper(),
        "nullable": nullable,
        "sourceColumnReferences": src_refs,
    }
    if is_surrogate_key:
        entry["isSurrogateKey"] = True
    if is_business_key:
        entry["isBusinessKey"] = True
    return entry


def make_system_column(node_id: str, node_name: str, col_name: str,
                       data_type: str, sys_flag: str, transform: str = "") -> dict:
    col_uuid = stable_uuid(f"{node_name}.{col_name.upper()}")
    return {
        "appliedColumnTests": {},
        "columnReference": {"columnCounter": col_uuid, "stepCounter": node_id},
        "config": {},
        "dataType": data_type,
        "defaultValue": "",
        "description": "",
        "name": col_name.upper(),
        "nullable": True,
        sys_flag: True,
        "sourceColumnReferences": [{"columnReferences": [], "transform": transform}],
    }


def generate_source_node(obj: dict) -> dict:
    """Generate a Source (sourceInput) node."""
    node_name = obj["name"]
    node_id = stable_uuid(node_name)
    location = obj["location"]

    columns = []
    for col in obj["columns"]:
        columns.append(make_column(
            node_id, node_name,
            col["name"], col["data_type"],
            nullable=col["nullable"],
            description=col.get("description", ""),
        ))

    return {
        "fileVersion": 1,
        "id": node_id,
        "name": node_name.upper(),
        "operation": {
            "database": "",
            "deployEnabled": True,
            "description": obj.get("description", ""),
            "locationName": location,
            "metadata": {"columns": columns},
            "name": node_name.upper(),
            "schema": "",
            "sqlType": "Source",
            "type": "sourceInput",
            "version": 1,
        },
        "type": "Node",
    }


def generate_sql_node(obj: dict, all_objects: dict) -> dict:
    """Generate a Stage/Dimension/Fact/View/Aggregate/PersistentStage node."""
    node_name = obj["name"]
    node_id = stable_uuid(node_name)
    location = obj["location"]
    obj_type = obj["obj_type"]

    type_info = OBJ_TYPE_MAP.get(obj_type, ("Stage", "sql", "STAGE"))
    sql_type = type_info[0]

    columns = []
    dep_nodes = set()

    for col in obj["columns"]:
        src_obj = col.get("src_obj_name", "")
        src_col = col.get("src_col_name", "")
        transform = col.get("transform", "")
        is_bk = col.get("is_business_key", False)
        is_sk = col.get("is_surrogate_key", False)

        # Track dependencies
        if src_obj and src_obj.upper() != node_name.upper():
            dep_nodes.add(src_obj)

        columns.append(make_column(
            node_id, node_name,
            col["name"], col["data_type"],
            nullable=col["nullable"],
            description=col.get("description", ""),
            src_node_name=src_obj,
            src_col_name=src_col,
            transform=transform,
            is_business_key=is_bk,
            is_surrogate_key=is_sk,
        ))

    # Add system columns based on type
    if sql_type == "Dimension" or sql_type == "persistentStage":
        for sys_col, sys_flag, dt, xform in [
            ("SYSTEM_VERSION", "isSystemVersion", "NUMBER", ""),
            ("SYSTEM_CURRENT_FLAG", "isSystemCurrentFlag", "VARCHAR", ""),
            ("SYSTEM_START_DATE", "isSystemStartDate", "TIMESTAMP", "CAST(CURRENT_TIMESTAMP AS TIMESTAMP)"),
            ("SYSTEM_END_DATE", "isSystemEndDate", "TIMESTAMP", "CAST('2999-12-31 00:00:00' AS TIMESTAMP)"),
            ("SYSTEM_CREATE_DATE", "isSystemCreateDate", "TIMESTAMP", "CAST(CURRENT_TIMESTAMP AS TIMESTAMP)"),
            ("SYSTEM_UPDATE_DATE", "isSystemUpdateDate", "TIMESTAMP", "CAST(CURRENT_TIMESTAMP AS TIMESTAMP)"),
        ]:
            # Only add if not already present
            if not any(c["name"] == sys_col for c in columns):
                columns.append(make_system_column(node_id, node_name, sys_col, dt, sys_flag, xform))
    elif sql_type == "Fact" or sql_type == "Aggregate":
        for sys_col, sys_flag, dt, xform in [
            ("SYSTEM_CREATE_DATE", "isSystemCreateDate", "TIMESTAMP", "CAST(CURRENT_TIMESTAMP AS TIMESTAMP)"),
            ("SYSTEM_UPDATE_DATE", "isSystemUpdateDate", "TIMESTAMP", "CAST(CURRENT_TIMESTAMP AS TIMESTAMP)"),
        ]:
            if not any(c["name"] == sys_col for c in columns):
                columns.append(make_system_column(node_id, node_name, sys_col, dt, sys_flag, xform))

    # Build source mapping
    join_text = obj.get("join_text", "")
    if join_text:
        join_condition, dependencies, aliases = parse_join_to_coalesce(join_text, location, all_objects)
    else:
        # Derive from column source references
        join_condition, dependencies, aliases = derive_source_mapping(node_name, dep_nodes, all_objects)

    # Build config
    config = {"postSQL": "", "preSQL": "", "testsEnabled": True}
    if sql_type == "Stage":
        config["insertStrategy"] = "INSERT"
        config["truncateBefore"] = True
    if obj.get("config_truncate") == "Y":
        config["truncateBefore"] = True
    if obj.get("config_distinct") == "Y":
        config["selectDistinct"] = True

    # Business key columns for Dimension/PersistentStage
    bk_cols = [col["name"].upper() for col in obj["columns"] if col.get("is_business_key")]
    if bk_cols and sql_type in ("Dimension", "persistentStage"):
        config["businessKeyColumns"] = bk_cols

    # Materialization
    materialization = "table"
    if sql_type in ("View", "DimensionView"):
        materialization = "view"

    return {
        "fileVersion": 1,
        "id": node_id,
        "name": node_name.upper(),
        "operation": {
            "config": config,
            "database": "",
            "deployEnabled": True,
            "description": obj.get("description", ""),
            "isMultisource": False,
            "locationName": location,
            "materializationType": materialization,
            "metadata": {
                "appliedNodeTests": [],
                "columns": columns,
                "cteString": "",
                "enabledColumnTestIDs": [],
                "sourceMapping": [{
                    "aliases": aliases,
                    "customSQL": {"customSQL": ""},
                    "dependencies": dependencies,
                    "join": {"joinCondition": join_condition},
                    "name": node_name.upper(),
                    "noLinkRefs": [],
                }],
            },
            "name": node_name.upper(),
            "overrideSQL": False,
            "schema": "",
            "sqlType": sql_type,
            "type": "sql",
            "version": 1,
        },
        "type": "Node",
    }


def derive_source_mapping(node_name: str, dep_nodes: set, all_objects: dict) -> tuple:
    """Derive source mapping from column-level dependencies when no JOIN text exists."""
    if not dep_nodes:
        return "", [], {}

    # Use the most-referenced source as primary
    dep_list = sorted(dep_nodes)
    aliases = {}
    dependencies = []

    for dep in dep_list:
        dep_info = all_objects.get(dep, {})
        dep_loc = dep_info.get("location", "STAGE")
        dep_uuid = stable_uuid(dep)
        aliases[dep.upper()] = dep_uuid
        dependencies.append({"locationName": dep_loc, "nodeName": dep.upper()})

    # Build join condition
    parts = []
    for i, dep in enumerate(dep_list):
        dep_info = all_objects.get(dep, {})
        dep_loc = dep_info.get("location", "STAGE")
        if i == 0:
            parts.append(f"FROM {{{{ ref('{dep_loc}', '{dep.upper()}') }}}} \"{dep.upper()}\"")
        else:
            parts.append(f"LEFT JOIN {{{{ ref('{dep_loc}', '{dep.upper()}') }}}} \"{dep.upper()}\"")

    join_condition = "\n".join(parts)
    return join_condition, dependencies, aliases


# ─── YAML WRITING ────────────────────────────────────────────────────────────

def write_node_yaml(node_data: dict, location: str, name: str):
    filename = f"{location}-{name}.yml"
    filepath = OUTPUT_DIR / filename

    class CoalesceDumper(yaml.Dumper):
        pass

    def str_representer(dumper, data):
        if "\n" in data:
            return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
        return dumper.represent_scalar("tag:yaml.org,2002:str", data)

    def bool_representer(dumper, data):
        return dumper.represent_scalar("tag:yaml.org,2002:bool", "true" if data else "false")

    CoalesceDumper.add_representer(str, str_representer)
    CoalesceDumper.add_representer(bool, bool_representer)

    with open(filepath, "w", encoding="utf-8") as f:
        yaml.dump(node_data, f, Dumper=CoalesceDumper, default_flow_style=False, sort_keys=False, allow_unicode=True)


# ─── CSV PARSING ─────────────────────────────────────────────────────────────

def parse_csv() -> dict:
    """Parse the Omnia CSV export into object metadata grouped by node."""
    objects = {}

    with open(CSV_FILE, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            loc_name = row.get("loc_name", "").strip()
            obj_name = row.get("obj_name", "").strip()
            obj_type = row.get("obj_type", "").strip()
            col_name = row.get("col_name", "").strip()

            if not obj_name or not col_name:
                continue

            # Create object entry if not exists
            key = obj_name
            if key not in objects:
                location = resolve_location(loc_name, obj_type)
                objects[key] = {
                    "name": obj_name,
                    "obj_type": obj_type,
                    "location": location,
                    "description": row.get("obj_desc", "").strip(),
                    "join_text": row.get("obj_join", "").strip(),
                    "config_truncate": row.get("config_truncate", "").strip(),
                    "config_distinct": row.get("config_distinct", "").strip(),
                    "columns": [],
                }

            # Parse column
            is_bk = row.get("col_bk_ind", "").strip().upper() == "Y" or row.get("col_key_type", "").strip().upper() == "A"
            is_sk = row.get("col_key_type", "").strip().upper() in ("S", "SURROGATE")

            # Source lineage
            src_obj = row.get("src_obj_name", "").strip()
            src_col = row.get("src_col_name", "").strip()
            src_loc = row.get("src_obj_loc", "").strip()
            transform = row.get("col_trans", "").strip()
            issue = row.get("issue", "").strip()

            # If there's an INVALID MAPPING issue, clear source refs
            if "INVALID MAPPING" in issue:
                src_obj = ""
                src_col = ""

            col_entry = {
                "name": col_name,
                "data_type": row.get("col_datatype", "VARCHAR").strip(),
                "nullable": row.get("col_nullable", "Y").strip().upper() != "N",
                "description": row.get("col_desc", "").strip(),
                "src_obj_name": src_obj,
                "src_col_name": src_col,
                "src_obj_loc": src_loc,
                "transform": transform,
                "is_business_key": is_bk,
                "is_surrogate_key": is_sk,
            }
            objects[key]["columns"].append(col_entry)

    return objects


def parse_app_obj() -> dict:
    """Parse app_obj file for type-26 (Ds*) objects not in CSV."""
    ds_objects = {}
    with open(OBJ_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split(";")
            if len(parts) >= 3:
                try:
                    obj_type = int(parts[0])
                    obj_name = parts[2]
                    if obj_type == 26:
                        ds_objects[obj_name] = {
                            "name": obj_name,
                            "obj_type": "Source",
                            "location": "LOAD",
                            "description": f"Data Source: {obj_name}",
                            "join_text": "",
                            "columns": [],
                        }
                except (ValueError, IndexError):
                    continue
    return ds_objects


# ─── MAIN ────────────────────────────────────────────────────────────────────

def main():
    print("=" * 70)
    print("  Omnia Partners — WhereScape RED → Coalesce Migration")
    print("=" * 70)

    # Parse CSV
    print("\n[1] Parsing CSV export...")
    objects = parse_csv()
    print(f"  {len(objects)} objects found in CSV")

    # Parse app_obj for Ds* objects
    print("\n[2] Parsing app_obj for type-26 (Ds*) objects...")
    ds_objects = parse_app_obj()
    # Only add Ds objects that aren't already in CSV
    added_ds = 0
    for name, meta in ds_objects.items():
        if name not in objects:
            objects[name] = meta
            added_ds += 1
    print(f"  {added_ds} additional Ds* source stubs added")

    # Summary by type
    type_counts = defaultdict(int)
    for obj in objects.values():
        type_counts[obj["obj_type"]] += 1
    print(f"\n  Object types:")
    for t, c in sorted(type_counts.items(), key=lambda x: -x[1]):
        print(f"    {t:<20} {c:>4}")

    # Build lookup for all objects (for resolving source locations in JOINs)
    all_objects_lookup = {name: {"location": obj["location"]} for name, obj in objects.items()}

    # Generate nodes
    print(f"\n[3] Generating nodes in {OUTPUT_DIR}/")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    # Clean existing
    for f in OUTPUT_DIR.glob("*.yml"):
        f.unlink()

    generated = 0
    skipped = 0
    by_type = defaultdict(int)

    for name, obj in sorted(objects.items()):
        obj_type = obj["obj_type"]

        if obj_type == "Source":
            if not obj["columns"]:
                skipped += 1
                continue
            node = generate_source_node(obj)
            write_node_yaml(node, obj["location"], name.upper())
            by_type["Source"] += 1
        elif obj_type in ("STAGE", "PERSISTENT STAGE", "DIMENSION", "FACT", "VIEW", "AGGREGATE"):
            if not obj["columns"]:
                skipped += 1
                continue
            node = generate_sql_node(obj, all_objects_lookup)
            write_node_yaml(node, obj["location"], name.upper())
            by_type[obj_type] += 1
        else:
            skipped += 1
            continue

        generated += 1

    print(f"\n  Generated: {generated} nodes")
    print(f"  Skipped:   {skipped} (no columns or unknown type)")
    print(f"\n  By type:")
    for t, c in sorted(by_type.items(), key=lambda x: -x[1]):
        print(f"    {t:<20} {c:>4}")

    # Report business keys found
    bk_nodes = []
    for name, obj in objects.items():
        bk_cols = [c["name"] for c in obj["columns"] if c.get("is_business_key")]
        if bk_cols:
            bk_nodes.append((name, bk_cols))

    print(f"\n[4] Business Keys Identified: {len(bk_nodes)} nodes")
    for name, cols in bk_nodes[:10]:
        print(f"    {name}: {', '.join(cols[:3])}")
    if len(bk_nodes) > 10:
        print(f"    ... and {len(bk_nodes) - 10} more")

    # Report multi-source nodes (nodes with JOIN text)
    join_nodes = [(name, obj) for name, obj in objects.items() if obj.get("join_text")]
    print(f"\n[5] Multi-Source Nodes (with JOIN logic): {len(join_nodes)}")

    print(f"\n[6] Run: coa validate")
    print(f"\nDone. Output in: {OUTPUT_DIR}/")


if __name__ == "__main__":
    main()
