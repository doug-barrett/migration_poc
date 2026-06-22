#!/usr/bin/env python3
"""
Parse WhereScape RED deployment files and generate Coalesce node YAML files.
Network Survey subject area POC - generates proper sourceColumnReferences and JOIN syntax.
"""

import re
import uuid
import yaml
import os
import sys
from pathlib import Path

# Paths
APP_DIR = Path(__file__).parent.parent.parent / "app_FullMetaData"
OUTPUT_DIR = Path(__file__).parent.parent / "nodes"
OBJ_FILE = APP_DIR / "app_obj_NP_FullMetaData.wst"
DATA_FILE = APP_DIR / "app_data_NP_FullMetaData.wst"

# UUID namespace for deterministic generation
NS = uuid.UUID("12345678-1234-5678-1234-567812345678")

# WhereScape object type codes
OBJ_TYPE_DIM_VIEW = 12

# Subject area definitions
SUBJECT_AREAS = {
    "network_survey": [
        "NorthpowerNetworkSurvey",
        "NPNetworkSur",
        "L_Email_SurveyDataDictionary",
        "Fact_NorthpowerNetworkSurvey",
        "Dim_NorthpowerNetworkSurvey",
    ],
    "fibre": [
        "Fibre",
        "fibre",
        "L_Foot_NPFibre",
        "L_Ref_Fibre",
        "L_SHP_Fibre",
        "L_Email_NorthpowerFibre",
        "L_Email_SurveyDataDictionaryFibre",
    ],
}

# Active subject area (set via command line)
ACTIVE_PATTERNS = []


def stable_uuid(seed: str) -> str:
    return str(uuid.uuid5(NS, seed))


def map_datatype(sql_server_type: str) -> str:
    t = sql_server_type.strip().lower()
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
    if t in ("varchar(max)", "nvarchar(max)", "text", "ntext"):
        return "VARCHAR(16777216)"
    m = re.match(r"(n?varchar|n?char)\((\d+)\)", t)
    if m:
        return f"VARCHAR({m.group(2)})"
    m = re.match(r"(numeric|decimal)\((\d+)(?:,(\d+))?\)", t)
    if m:
        return f"NUMBER({m.group(1)},{m.group(2) or '0'})" if m.group(2) else f"NUMBER({m.group(1)})"
    return "VARCHAR"


def is_target_object(name: str) -> bool:
    for pat in ACTIVE_PATTERNS:
        if pat.lower() in name.lower():
            return True
    return False


def parse_quoted_values(line: str) -> list:
    val_start = line.find("values")
    if val_start == -1:
        val_start = line.find("VALUES")
    if val_start == -1:
        return []
    val_section = line[val_start:]
    values = re.findall(r"'((?:[^']|'')*)'", val_section)
    values = [v.replace("''", "'") for v in values]
    if values and values[0] == "ws_obj_object":
        values = values[1:]
    return values


def parse_obj_file() -> dict:
    objects = {}
    with open(OBJ_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split(";")
            if len(parts) >= 3:
                try:
                    obj_type = int(parts[0])
                    obj_id = int(parts[1])
                    obj_name = parts[2]
                    objects[obj_name] = {"type": obj_type, "id": obj_id, "name": obj_name}
                except (ValueError, IndexError):
                    continue
    return objects


def parse_data_file(target_objects: dict) -> dict:
    tables = {}
    current_table_name = None

    with open(DATA_FILE, "r", encoding="utf-16-le") as f:
        content = f.read()
    lines = content.split("\n")

    for line in lines:
        if not line.strip():
            continue

        # Object boundary detection
        if "ws_obj_object" in line and "oo_obj_key" in line and "oo_name" in line:
            name_m = re.search(r"'([^']+)',\s*\d+,\s*\d+,\s*\d+", line)
            if name_m:
                obj_name = name_m.group(1)
                if not is_target_object(obj_name):
                    current_table_name = None
            continue

        if "ws_load_tab" in line and "values" in line:
            vals = parse_quoted_values(line)
            if len(vals) >= 2:
                tname = vals[0]
                if is_target_object(tname):
                    tables[tname] = {"ws_type": "load", "name": tname, "description": vals[3] if len(vals) > 3 else "", "columns": []}
                    current_table_name = tname
            continue

        if "ws_load_col" in line and "values" in line:
            if current_table_name and current_table_name in tables:
                vals = parse_quoted_values(line)
                if len(vals) >= 4:
                    tables[current_table_name]["columns"].append({
                        "name": vals[0], "data_type": vals[2], "nullable": vals[3] == "Y",
                        "description": vals[10] if len(vals) > 10 else vals[1],
                    })
            continue

        if "ws_stage_tab" in line and "values" in line:
            vals = parse_quoted_values(line)
            if len(vals) >= 2:
                tname = vals[0]
                if is_target_object(tname):
                    from_clause = ""
                    for v in vals:
                        if v.strip().upper().startswith("FROM"):
                            from_clause = v
                            break
                    tables[tname] = {"ws_type": "stage", "name": tname, "description": vals[3] if len(vals) > 3 else "", "from_clause": from_clause, "columns": []}
                    current_table_name = tname
            continue

        if "ws_view_tab" in line and "values" in line:
            vals = parse_quoted_values(line)
            if len(vals) >= 2:
                tname = vals[0]
                if is_target_object(tname):
                    tables[tname] = {"ws_type": "data_store", "name": tname, "description": vals[5] if len(vals) > 5 else "", "columns": []}
                    current_table_name = tname
            continue

        if "ws_stage_col" in line and "values" in line:
            if current_table_name and current_table_name in tables:
                vals = parse_quoted_values(line)
                if len(vals) >= 4:
                    tables[current_table_name]["columns"].append({
                        "name": vals[0], "data_type": vals[2], "nullable": vals[3] == "Y",
                        "description": vals[10] if len(vals) > 10 else vals[1],
                        "src_table": vals[8] if len(vals) > 8 else "",
                        "src_column": vals[9] if len(vals) > 9 else "",
                    })
            continue

        if "ws_dim_tab" in line and "values" in line:
            vals = parse_quoted_values(line)
            if len(vals) >= 2:
                tname = vals[0]
                if is_target_object(tname):
                    from_clause = ""
                    for v in vals:
                        if v.strip().upper().startswith("FROM"):
                            from_clause = v
                            break
                    obj_info = target_objects.get(tname, {})
                    ws_type = "dim_view" if obj_info.get("type") == OBJ_TYPE_DIM_VIEW else "dimension"
                    tables[tname] = {"ws_type": ws_type, "name": tname, "description": vals[5] if len(vals) > 5 else "", "from_clause": from_clause, "columns": []}
                    current_table_name = tname
            continue

        if "ws_dim_col" in line and "values" in line:
            if current_table_name and current_table_name in tables:
                vals = parse_quoted_values(line)
                if len(vals) >= 4:
                    key_type = vals[12] if len(vals) > 12 else ""
                    artificial_key_ind = vals[14] if len(vals) > 14 else ""
                    tables[current_table_name]["columns"].append({
                        "name": vals[0], "data_type": vals[2], "nullable": vals[3] == "Y",
                        "description": vals[11] if len(vals) > 11 else vals[1],
                        "is_surrogate_key": artificial_key_ind == "Y",
                        "is_business_key": key_type == "A",
                        "src_table": vals[9] if len(vals) > 9 else "",
                        "src_column": vals[10] if len(vals) > 10 else "",
                    })
            continue

        if "ws_fact_tab" in line and "values" in line:
            vals = parse_quoted_values(line)
            if len(vals) >= 2:
                tname = vals[0]
                if is_target_object(tname):
                    tables[tname] = {"ws_type": "fact", "name": tname, "description": vals[5] if len(vals) > 5 else "", "columns": []}
                    current_table_name = tname
            continue

        if "ws_fact_col" in line and "values" in line:
            if current_table_name and current_table_name in tables:
                vals = parse_quoted_values(line)
                if len(vals) >= 4:
                    tables[current_table_name]["columns"].append({
                        "name": vals[0], "data_type": vals[2], "nullable": vals[3] == "Y",
                        "description": vals[12] if len(vals) > 12 else vals[1],
                        "join_flag": vals[7] if len(vals) > 7 else "N",
                        "src_table": vals[10] if len(vals) > 10 else "",
                        "src_column": vals[11] if len(vals) > 11 else "",
                    })
            continue

    return tables


# ─── YAML GENERATION ─────────────────────────────────────────────────────────

def make_source_column(node_id: str, node_name: str, col: dict) -> dict:
    col_uuid = stable_uuid(f"{node_name}.{col['name'].upper()}")
    return {
        "appliedColumnTests": {},
        "columnReference": {"columnCounter": col_uuid, "stepCounter": node_id},
        "config": {},
        "dataType": map_datatype(col["data_type"]),
        "defaultValue": "",
        "description": col.get("description", ""),
        "name": col["name"].upper(),
        "nullable": col.get("nullable", True),
        "sourceColumnReferences": [{"columnReferences": [], "transform": ""}],
        "transform": "",
    }


def make_mapped_column(node_id: str, node_name: str, col: dict, src_node_id: str, src_col_name: str, src_node_name: str, transform: str = "") -> dict:
    col_uuid = stable_uuid(f"{node_name}.{col['name'].upper()}")
    # Use UPPERCASE source column name for UUID consistency
    src_col_uuid = stable_uuid(f"{src_node_name}.{src_col_name.upper()}")
    # If source is self-referencing or missing, use empty sourceColumnReferences
    if src_node_id == node_id or not src_col_name:
        src_refs = [{"columnReferences": [], "transform": transform}]
    else:
        src_refs = [{"columnReferences": [{"columnCounter": src_col_uuid, "stepCounter": src_node_id}], "transform": transform}]
    entry = {
        "appliedColumnTests": {},
        "columnReference": {"columnCounter": col_uuid, "stepCounter": node_id},
        "config": {},
        "dataType": map_datatype(col["data_type"]),
        "description": col.get("description", ""),
        "name": col["name"].upper(),
        "nullable": col.get("nullable", True),
        "sourceColumnReferences": src_refs,
    }
    if col.get("is_surrogate_key"):
        entry["isSurrogateKey"] = True
    if col.get("is_business_key"):
        entry["isBusinessKey"] = True
    return entry


def make_system_column(node_id: str, node_name: str, col_name: str, data_type: str, sys_flag: str, transform: str = "") -> dict:
    # Use UPPERCASE col_name for UUID to be consistent with column references
    col_uuid = stable_uuid(f"{node_name}.{col_name.upper()}")
    entry = {
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
    return entry


def generate_source_node(meta: dict) -> dict:
    node_id = stable_uuid(meta["name"])
    columns = [make_source_column(node_id, meta["name"], col) for col in meta["columns"]]
    return {
        "fileVersion": 1,
        "id": node_id,
        "name": meta["name"].upper(),
        "operation": {
            "database": "",
            "deployEnabled": True,
            "description": meta.get("description", ""),
            "locationName": "BRONZE",
            "metadata": {"columns": columns},
            "name": meta["name"].upper(),
            "schema": "",
            "sqlType": "Source",
            "type": "sourceInput",
            "version": 1,
        },
        "type": "Node",
    }


def generate_stage_node(meta: dict, all_tables: dict, location: str = "SILVER") -> dict:
    node_id = stable_uuid(meta["name"])
    node_name = meta["name"]
    columns = []
    dep_nodes = set()

    for col in meta["columns"]:
        src_table = col.get("src_table", "")
        src_col = col.get("src_column", col["name"])

        # Handle ODS system date columns - use parameter references
        if col["name"].upper() in ("ODSCREATEDATE", "ODSUPDATEDATE"):
            param_name = "ODSCreateDate" if "CREATE" in col["name"].upper() else "ODSUpdateDate"
            c = make_mapped_column(node_id, node_name, col, node_id, "", node_name, f"{{{{parameters.{param_name}}}}}")
            columns.append(c)
            continue

        # If source table exists in our generated set AND has columns, link to it
        if src_table and src_table in all_tables and all_tables[src_table]["columns"]:
            src_node_id = stable_uuid(src_table)
            columns.append(make_mapped_column(node_id, node_name, col, src_node_id, src_col, src_table))
            dep_nodes.add(src_table)
        elif src_table and src_table in all_tables and not all_tables[src_table]["columns"]:
            # Source node exists but has 0 cols (data store we couldn't parse)
            # Still create the reference - the node is known
            src_node_id = stable_uuid(src_table)
            columns.append(make_mapped_column(node_id, node_name, col, src_node_id, src_col, src_table))
            dep_nodes.add(src_table)
        elif src_table and src_table not in all_tables:
            # Source node not in our set at all - use empty reference
            columns.append(make_mapped_column(node_id, node_name, col, node_id, "", node_name))
        else:
            # No source - self-reference (empty)
            columns.append(make_mapped_column(node_id, node_name, col, node_id, "", node_name))

    # Build sourceMapping from lineage knowledge
    join_cond, dependencies, aliases, no_link_refs = build_stage_source_mapping(meta, all_tables, dep_nodes)

    return {
        "fileVersion": 1,
        "id": node_id,
        "name": node_name.upper(),
        "operation": {
            "config": {"insertStrategy": "INSERT", "postSQL": "", "preSQL": "", "testsEnabled": True, "truncateBefore": True},
            "database": "",
            "deployEnabled": True,
            "description": meta.get("description", ""),
            "isMultisource": False,
            "locationName": location,
            "materializationType": "table",
            "metadata": {
                "appliedNodeTests": [],
                "columns": columns,
                "cteString": "",
                "enabledColumnTestIDs": [],
                "sourceMapping": [{
                    "aliases": aliases,
                    "customSQL": {"customSQL": ""},
                    "dependencies": dependencies,
                    "join": {"joinCondition": join_cond},
                    "name": node_name.upper(),
                    "noLinkRefs": no_link_refs,
                }],
            },
            "name": node_name.upper(),
            "overrideSQL": False,
            "schema": "",
            "sqlType": "Stage",
            "type": "sql",
            "version": 1,
        },
        "type": "Node",
    }


def generate_dimension_node(meta: dict, all_tables: dict) -> dict:
    node_id = stable_uuid(meta["name"])
    node_name = meta["name"]
    columns = []

    # Determine primary source from column src_table references
    src_tables = {}
    for col in meta["columns"]:
        st = col.get("src_table", "")
        if st and st != node_name and st in all_tables and all_tables[st]["columns"]:
            src_tables[st] = src_tables.get(st, 0) + 1
    # Pick the most-referenced source table
    primary_src = max(src_tables, key=src_tables.get) if src_tables else ""
    primary_src_id = stable_uuid(primary_src) if primary_src else node_id

    for col in meta["columns"]:
        src_col = col.get("src_column", col["name"])
        if col.get("is_surrogate_key"):
            columns.append(make_system_column(node_id, node_name, col["name"].upper(), "NUMBER", "isSurrogateKey", ""))
        elif col["name"].upper() in ("ODSCREATEDATE", "ODSUPDATEDATE"):
            param_name = "ODSCreateDate" if "CREATE" in col["name"].upper() else "ODSUpdateDate"
            columns.append(make_mapped_column(node_id, node_name, col, node_id, "", node_name, f"{{{{parameters.{param_name}}}}}"))
        elif primary_src and src_col:
            # Check if the src_column exists on primary source
            src_col_names = [c["name"] for c in all_tables.get(primary_src, {}).get("columns", [])]
            if src_col in src_col_names:
                columns.append(make_mapped_column(node_id, node_name, col, primary_src_id, src_col, primary_src))
            else:
                # Column doesn't exist on primary source - empty ref
                columns.append(make_mapped_column(node_id, node_name, col, node_id, "", node_name))
        else:
            columns.append(make_mapped_column(node_id, node_name, col, node_id, "", node_name))

    # Add Dimension system columns
    for sys_col_name, sys_flag, dt, transform in [
        ("SYSTEM_VERSION", "isSystemVersion", "NUMBER", ""),
        ("SYSTEM_CURRENT_FLAG", "isSystemCurrentFlag", "VARCHAR", ""),
        ("SYSTEM_START_DATE", "isSystemStartDate", "TIMESTAMP", "CAST(CURRENT_TIMESTAMP AS TIMESTAMP)"),
        ("SYSTEM_END_DATE", "isSystemEndDate", "TIMESTAMP", "CAST('2999-12-31 00:00:00' AS TIMESTAMP)"),
        ("SYSTEM_CREATE_DATE", "isSystemCreateDate", "TIMESTAMP", "CAST(CURRENT_TIMESTAMP AS TIMESTAMP)"),
        ("SYSTEM_UPDATE_DATE", "isSystemUpdateDate", "TIMESTAMP", "CAST(CURRENT_TIMESTAMP AS TIMESTAMP)"),
    ]:
        columns.append(make_system_column(node_id, node_name, sys_col_name, dt, sys_flag, transform))

    # Source mapping
    src_loc = "SILVER"
    aliases = {primary_src.upper(): primary_src_id} if primary_src else {}
    dependencies = [{"locationName": src_loc, "nodeName": primary_src.upper()}] if primary_src else []
    join_cond = f"FROM {{{{ ref('{src_loc}', '{primary_src.upper()}') }}}} \"{primary_src.upper()}\"" if primary_src else ""

    # Business keys
    bk_cols = [col["name"].upper() for col in meta["columns"] if col.get("is_business_key")]

    config = {"postSQL": "", "preSQL": "", "testsEnabled": True}
    if bk_cols:
        config["businessKeyColumns"] = bk_cols

    return {
        "fileVersion": 1,
        "id": node_id,
        "name": node_name.upper(),
        "operation": {
            "config": config,
            "database": "",
            "deployEnabled": True,
            "description": meta.get("description", ""),
            "isMultisource": False,
            "locationName": "GOLD",
            "materializationType": "table",
            "metadata": {
                "appliedNodeTests": [],
                "columns": columns,
                "cteString": "",
                "enabledColumnTestIDs": [],
                "sourceMapping": [{
                    "aliases": aliases,
                    "customSQL": {"customSQL": ""},
                    "dependencies": dependencies,
                    "join": {"joinCondition": join_cond},
                    "name": node_name.upper(),
                    "noLinkRefs": [],
                }],
            },
            "name": node_name.upper(),
            "overrideSQL": False,
            "schema": "",
            "sqlType": "Dimension",
            "type": "sql",
            "version": 1,
        },
        "type": "Node",
    }



def generate_dimension_view_node(meta: dict, all_tables: dict) -> dict:
    """Generate a Dimension View node (type 12 with D_ prefix - role-playing dimension)."""
    node_id = stable_uuid(meta["name"])
    node_name = meta["name"]
    columns = []

    for col in meta["columns"]:
        if col.get("is_surrogate_key"):
            columns.append(make_system_column(node_id, node_name, col["name"].upper(), "NUMBER", "isSurrogateKey", ""))
        elif col["name"].upper() in ("ODSCREATEDATE", "ODSUPDATEDATE"):
            param_name = "ODSCreateDate" if "CREATE" in col["name"].upper() else "ODSUpdateDate"
            columns.append(make_mapped_column(node_id, node_name, col, node_id, "", node_name, f"{{{{parameters.{param_name}}}}}"))
        else:
            columns.append(make_mapped_column(node_id, node_name, col, node_id, "", node_name))

    return {
        "fileVersion": 1,
        "id": node_id,
        "name": node_name.upper(),
        "operation": {
            "config": {},
            "database": "",
            "deployEnabled": True,
            "description": meta.get("description", ""),
            "isMultisource": False,
            "locationName": "GOLD",
            "materializationType": "view",
            "metadata": {
                "appliedNodeTests": [],
                "columns": columns,
                "cteString": "",
                "enabledColumnTestIDs": [],
                "sourceMapping": [{
                    "aliases": {},
                    "customSQL": {"customSQL": ""},
                    "dependencies": [],
                    "join": {"joinCondition": ""},
                    "name": node_name.upper(),
                    "noLinkRefs": [],
                }],
            },
            "name": node_name.upper(),
            "overrideSQL": False,
            "schema": "",
            "sqlType": "DimensionView",
            "type": "sql",
            "version": 1,
        },
        "type": "Node",
    }


def generate_view_node(meta: dict, all_tables: dict) -> dict:
    node_id = stable_uuid(meta["name"])
    node_name = meta["name"]
    base_dim = node_name.replace("Dim_", "D_")
    # If base_dim resolves to self (D_ prefix type 12 role-playing dims), generate as DimensionView
    if base_dim == node_name:
        return generate_dimension_view_node(meta, all_tables)
    base_dim_id = stable_uuid(base_dim) if base_dim else node_id
    columns = []

    # Get column names available on base dimension
    if base_dim and base_dim in all_tables:
        base_dim_col_names = [c["name"] for c in all_tables[base_dim].get("columns", [])]
        base_dim_col_names.extend(["SYSTEM_VERSION", "SYSTEM_CURRENT_FLAG", "SYSTEM_START_DATE", "SYSTEM_END_DATE", "SYSTEM_CREATE_DATE", "SYSTEM_UPDATE_DATE"])
    else:
        base_dim_col_names = []

    for col in meta["columns"]:
        src_col = col.get("src_column", col["name"])
        # Try to match to a column on the base dimension
        if src_col in base_dim_col_names:
            columns.append(make_mapped_column(node_id, node_name, col, base_dim_id, src_col, base_dim))
        elif col["name"] in base_dim_col_names:
            columns.append(make_mapped_column(node_id, node_name, col, base_dim_id, col["name"], base_dim))
        else:
            # Can't find a match - empty source ref
            columns.append(make_mapped_column(node_id, node_name, col, node_id, "", node_name))

    if base_dim:
        aliases = {base_dim.upper(): base_dim_id}
        dependencies = [{"locationName": "GOLD", "nodeName": base_dim.upper()}]
        join_cond = f"FROM {{{{ ref('GOLD', '{base_dim.upper()}') }}}} \"{base_dim.upper()}\""
    else:
        aliases = {}
        dependencies = []
        join_cond = ""

    return {
        "fileVersion": 1,
        "id": node_id,
        "name": node_name.upper(),
        "operation": {
            "config": {},
            "database": "",
            "deployEnabled": True,
            "description": meta.get("description", ""),
            "isMultisource": False,
            "locationName": "GOLD",
            "materializationType": "view",
            "metadata": {
                "appliedNodeTests": [],
                "columns": columns,
                "cteString": "",
                "enabledColumnTestIDs": [],
                "sourceMapping": [{
                    "aliases": aliases,
                    "customSQL": {"customSQL": ""},
                    "dependencies": dependencies,
                    "join": {"joinCondition": join_cond},
                    "name": node_name.upper(),
                    "noLinkRefs": [],
                }],
            },
            "name": node_name.upper(),
            "overrideSQL": False,
            "schema": "",
            "sqlType": "View",
            "type": "sql",
            "version": 1,
        },
        "type": "Node",
    }


def generate_fact_node(meta: dict, all_tables: dict) -> dict:
    node_id = stable_uuid(meta["name"])
    node_name = meta["name"]

    # Determine primary source from column src_table references
    src_tables = {}
    for col in meta["columns"]:
        st = col.get("src_table", "")
        if st and st != node_name and st in all_tables:
            src_tables[st] = src_tables.get(st, 0) + 1
    # Pick the most-referenced source, preferring S_ tables
    if src_tables:
        s_tables = {k: v for k, v in src_tables.items() if k.startswith("S_")}
        primary_src = max(s_tables, key=s_tables.get) if s_tables else max(src_tables, key=src_tables.get)
    else:
        primary_src = ""
    primary_src_id = stable_uuid(primary_src) if primary_src else node_id
    columns = []

    # Get column names on primary source
    src_col_names = [c["name"].upper() for c in all_tables.get(primary_src, {}).get("columns", [])]

    for col in meta["columns"]:
        src_col = col.get("src_column", col["name"])
        # Handle ODS date columns with parameter references
        if col["name"].upper() in ("ODSCREATEDATE", "ODSUPDATEDATE"):
            param_name = "ODSCreateDate" if "CREATE" in col["name"].upper() else "ODSUpdateDate"
            columns.append(make_mapped_column(node_id, node_name, col, node_id, "", node_name, f"{{{{parameters.{param_name}}}}}"))
        # Only link if source column exists on the source node
        elif src_col.upper() in src_col_names:
            columns.append(make_mapped_column(node_id, node_name, col, primary_src_id, src_col, primary_src))
        else:
            # Computed or system column - empty ref
            columns.append(make_mapped_column(node_id, node_name, col, node_id, "", node_name))

    # System columns
    columns.append(make_system_column(node_id, node_name, "SYSTEM_CREATE_DATE", "TIMESTAMP", "isSystemCreateDate", "CAST(CURRENT_TIMESTAMP AS TIMESTAMP)"))
    columns.append(make_system_column(node_id, node_name, "SYSTEM_UPDATE_DATE", "TIMESTAMP", "isSystemUpdateDate", "CAST(CURRENT_TIMESTAMP AS TIMESTAMP)"))

    if primary_src:
        src_loc = "SILVER" if not primary_src.startswith("L_") else "BRONZE"
        aliases = {primary_src.upper(): primary_src_id}
        dependencies = [{"locationName": src_loc, "nodeName": primary_src.upper()}]
        join_cond = f"FROM {{{{ ref('{src_loc}', '{primary_src.upper()}') }}}} \"{primary_src.upper()}\""
    else:
        aliases = {}
        dependencies = []
        join_cond = ""

    return {
        "fileVersion": 1,
        "id": node_id,
        "name": node_name.upper(),
        "operation": {
            "config": {"postSQL": "", "preSQL": "", "testsEnabled": True},
            "database": "",
            "deployEnabled": True,
            "description": meta.get("description", ""),
            "isMultisource": False,
            "locationName": "GOLD",
            "materializationType": "table",
            "metadata": {
                "appliedNodeTests": [],
                "columns": columns,
                "cteString": "",
                "enabledColumnTestIDs": [],
                "sourceMapping": [{
                    "aliases": aliases,
                    "customSQL": {"customSQL": ""},
                    "dependencies": dependencies,
                    "join": {"joinCondition": join_cond},
                    "name": node_name.upper(),
                    "noLinkRefs": [],
                }],
            },
            "name": node_name.upper(),
            "overrideSQL": False,
            "schema": "",
            "sqlType": "Fact",
            "type": "sql",
            "version": 1,
        },
        "type": "Node",
    }


def get_stub_columns_for_data_store(ds_name: str, all_tables: dict) -> list:
    """Infer columns for a data store stub from what downstream nodes reference."""
    # Look at all tables that reference this data store as src_table
    cols = {}
    for tbl_name, tbl_meta in all_tables.items():
        for col in tbl_meta.get("columns", []):
            if col.get("src_table") == ds_name and col.get("src_column"):
                src_col = col["src_column"]
                if src_col not in cols:
                    cols[src_col] = {"name": src_col, "data_type": col["data_type"], "nullable": True, "description": col.get("description", "")}
    return list(cols.values())


def build_stage_source_mapping(meta: dict, all_tables: dict, dep_nodes: set) -> tuple:
    """Build proper FROM/JOIN clause based on known lineage."""
    node_name = meta["name"]
    aliases = {}
    dependencies = []

    # Known lineage from WhereScape procedure analysis
    if node_name == "I_NorthpowerNetworkSurveyMerge":
        # Sources from data stores: I_NorthpowerNetworkSurveyFaults, Lines, Fibre
        srcs = ["I_NorthpowerNetworkSurveyFaults", "I_NorthpowerNetworkSurveyLines", "I_NorthpowerNetworkSurveyFibre"]
        for s in srcs:
            aliases[s.upper()] = stable_uuid(s)
        dependencies = [{"locationName": "SILVER", "nodeName": s.upper()} for s in srcs]
        join_cond = (
            f"FROM {{{{ ref('SILVER', '{srcs[0].upper()}') }}}} \"{srcs[0].upper()}\"\n"
            f"INNER JOIN {{{{ ref('SILVER', '{srcs[1].upper()}') }}}} \"{srcs[1].upper()}\"\n"
            f"  ON \"{srcs[0].upper()}\".\"SURVEYNUMBER\" = \"{srcs[1].upper()}\".\"SURVEYNUMBER\"\n"
            f"INNER JOIN {{{{ ref('SILVER', '{srcs[2].upper()}') }}}} \"{srcs[2].upper()}\"\n"
            f"  ON \"{srcs[0].upper()}\".\"SURVEYNUMBER\" = \"{srcs[2].upper()}\".\"SURVEYNUMBER\""
        )

    elif node_name == "I_NorthpowerNetworkSurvey":
        src = "I_NorthpowerNetworkSurveyMerge"
        src_q = "I_NorthpowerNetworkSurveyQuestions"
        src_id = stable_uuid(src)
        src_q_id = stable_uuid(src_q)
        aliases[src.upper()] = src_id
        aliases[src_q.upper()] = src_q_id
        dependencies = [
            {"locationName": "SILVER", "nodeName": src.upper()},
            {"locationName": "SILVER", "nodeName": src_q.upper()},
        ]
        join_cond = (
            f"FROM {{{{ ref('SILVER', '{src.upper()}') }}}} \"{src.upper()}\"\n"
            f"LEFT JOIN {{{{ ref('SILVER', '{src_q.upper()}') }}}} \"{src_q.upper()}\"\n"
            f"  ON \"{src.upper()}\".\"QUESTIONNUMBER\" = \"{src_q.upper()}\".\"QUESTIONNUMBER\"\n"
            f"  AND \"{src.upper()}\".\"WAVE\" = \"{src_q.upper()}\".\"WAVE\"\n"
            f"  AND \"{src.upper()}\".\"SURVEYTYPE\" = \"{src_q.upper()}\".\"SURVEYTYPE\""
        )

    elif node_name == "S_NorthpowerNetworkSurvey":
        # Complex: FROM I_NorthpowerNetworkSurvey LEFT JOIN 3 dims for key lookups
        src_i = "I_NorthpowerNetworkSurvey"
        dim_c = "D_NorthpowerNetworkSurveyComments"
        dim_d = "D_NorthpowerNetworkSurveyDetails"
        dim_q = "D_NorthpowerNetworkSurveyQuestions"

        for s in [src_i, dim_c, dim_d, dim_q]:
            aliases[s.upper()] = stable_uuid(s)

        dependencies = [
            {"locationName": "SILVER", "nodeName": src_i.upper()},
            {"locationName": "GOLD", "nodeName": dim_c.upper()},
            {"locationName": "GOLD", "nodeName": dim_d.upper()},
            {"locationName": "GOLD", "nodeName": dim_q.upper()},
        ]

        join_cond = (
            f"FROM {{{{ ref('SILVER', '{src_i.upper()}') }}}} \"{src_i.upper()}\"\n"
            f"LEFT JOIN {{{{ ref('GOLD', '{dim_c.upper()}') }}}} \"{dim_c.upper()}\"\n"
            f"  ON \"{src_i.upper()}\".\"QUESTIONNUMBER\" = \"{dim_c.upper()}\".\"QUESTIONNUMBER\"\n"
            f"  AND \"{src_i.upper()}\".\"SURVEYNUMBER\" = \"{dim_c.upper()}\".\"SURVEYNUMBER\"\n"
            f"  AND \"{src_i.upper()}\".\"SURVEYTYPE\" = \"{dim_c.upper()}\".\"SURVEYTYPE\"\n"
            f"LEFT JOIN {{{{ ref('GOLD', '{dim_d.upper()}') }}}} \"{dim_d.upper()}\"\n"
            f"  ON \"{src_i.upper()}\".\"SURVEYNUMBER\" = \"{dim_d.upper()}\".\"SURVEYNUMBER\"\n"
            f"  AND \"{src_i.upper()}\".\"SURVEYTYPE\" = \"{dim_d.upper()}\".\"SURVEYTYPE\"\n"
            f"LEFT JOIN {{{{ ref('GOLD', '{dim_q.upper()}') }}}} \"{dim_q.upper()}\"\n"
            f"  ON \"{src_i.upper()}\".\"QUESTIONNUMBER\" = \"{dim_q.upper()}\".\"QUESTIONNUMBER\"\n"
            f"  AND \"{src_i.upper()}\".\"RESPONSECODE\" = \"{dim_q.upper()}\".\"RESPONSECODE\"\n"
            f"  AND \"{src_i.upper()}\".\"SURVEYTYPE\" = \"{dim_q.upper()}\".\"SURVEYTYPE\"\n"
            f"  AND \"{src_i.upper()}\".\"WAVE\" = \"{dim_q.upper()}\".\"WAVE\""
        )
    else:
        # Generic: use first dependency
        if dep_nodes:
            src = list(dep_nodes)[0]
            src_id = stable_uuid(src)
            src_loc = "BRONZE" if src.startswith("L_") else "SILVER"
            aliases[src.upper()] = src_id
            dependencies = [{"locationName": src_loc, "nodeName": src.upper()}]
            join_cond = f"FROM {{{{ ref('{src_loc}', '{src.upper()}') }}}} \"{src.upper()}\""
        else:
            join_cond = ""

    return join_cond, dependencies, aliases, []


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
    print(f"  Written: {filename}")


def main():
    global ACTIVE_PATTERNS

    # Parse command line argument for subject area
    subject_area = sys.argv[1] if len(sys.argv) > 1 else "network_survey"
    if subject_area not in SUBJECT_AREAS:
        print(f"Unknown subject area: {subject_area}")
        print(f"Available: {', '.join(SUBJECT_AREAS.keys())}")
        sys.exit(1)

    ACTIVE_PATTERNS = SUBJECT_AREAS[subject_area]

    print("=" * 60)
    print(f"WhereScape RED → Coalesce Migration ({subject_area})")
    print("=" * 60)

    print("\n[1] Parsing object registry...")
    all_objects = parse_obj_file()
    target_objects = {k: v for k, v in all_objects.items() if is_target_object(k)}
    print(f"  {len(target_objects)} objects found")

    print("\n[2] Parsing data file...")
    tables = parse_data_file(target_objects)
    print(f"  {len(tables)} tables with metadata:")
    for name, meta in sorted(tables.items()):
        print(f"    {meta['ws_type']:12s} | {name} ({len(meta['columns'])} cols)")

    print(f"\n[3] Generating nodes in {OUTPUT_DIR}/")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for f in OUTPUT_DIR.glob("*.yml"):
        f.unlink()

    generated = 0
    # Track which nodes were generated so we know what exists
    generated_nodes = set()

    for name, meta in sorted(tables.items()):
        if not meta["columns"]:
            # Generate stub Source nodes for data stores that other nodes reference
            if meta["ws_type"] == "data_store":
                # Create a minimal source stub so lineage references resolve
                stub_meta = {"name": name, "description": meta.get("description", ""), "columns": []}
                # Don't generate - but track for later stub generation
            print(f"  SKIP (0 cols): {name}")
            continue
        ws_type = meta["ws_type"]
        if ws_type == "load":
            node = generate_source_node(meta)
            write_node_yaml(node, "BRONZE", name.upper())
        elif ws_type in ("stage", "data_store"):
            node = generate_stage_node(meta, tables)
            write_node_yaml(node, "SILVER", name.upper())
        elif ws_type == "dimension":
            node = generate_dimension_node(meta, tables)
            write_node_yaml(node, "GOLD", name.upper())
        elif ws_type == "dim_view":
            node = generate_view_node(meta, tables)
            write_node_yaml(node, "GOLD", name.upper())
        elif ws_type == "fact":
            node = generate_fact_node(meta, tables)
            write_node_yaml(node, "GOLD", name.upper())
        else:
            continue
        generated += 1
        generated_nodes.add(name)

    # Generate stub Source nodes for any referenced data stores with no columns
    # Find all data stores that were skipped but are referenced by generated nodes
    referenced_sources = set()
    for name, meta in tables.items():
        if meta["columns"]:
            for col in meta["columns"]:
                st = col.get("src_table", "")
                if st and st in tables and not tables[st]["columns"] and st not in generated_nodes:
                    referenced_sources.add(st)

    for ds_name in sorted(referenced_sources):
        stub_cols = get_stub_columns_for_data_store(ds_name, tables)
        if stub_cols:
            stub_meta = {"name": ds_name, "description": tables[ds_name].get("description", ""), "columns": stub_cols}
            node = generate_source_node(stub_meta)
            node["operation"]["locationName"] = "SILVER"
            write_node_yaml(node, "SILVER", ds_name.upper())
            generated += 1
            print(f"  Written (stub): SILVER-{ds_name.upper()}.yml")

    print(f"\n  Total: {generated} nodes generated")

    print("\n[4] Parameters Required:")
    print("  The following parameters must be configured in Coalesce:")
    print("    - parameters.ODSCreateDate  (TIMESTAMP - set to CURRENT_TIMESTAMP at runtime)")
    print("    - parameters.ODSUpdateDate  (TIMESTAMP - set to CURRENT_TIMESTAMP at runtime)")

    print("\n[5] Run: coa validate")


if __name__ == "__main__":
    main()
