#!/usr/bin/env python3
"""
Parse WhereScape RED deployment files and generate Coalesce node YAML files.
Focused on Network Survey subject area as POC.
"""

import re
import uuid
import yaml
import os
from pathlib import Path

# Paths
APP_DIR = Path(__file__).parent.parent.parent / "app_FullMetaData"
OUTPUT_DIR = Path(__file__).parent.parent / "nodes"
OBJ_FILE = APP_DIR / "app_obj_NP_FullMetaData.wst"
DATA_FILE = APP_DIR / "app_data_NP_FullMetaData.wst"

# WhereScape object type codes
OBJ_TYPE_PROCEDURE = 1
OBJ_TYPE_LOAD = 8
OBJ_TYPE_STAGE = 7
OBJ_TYPE_DIMENSION = 6
OBJ_TYPE_FACT = 5
OBJ_TYPE_INDEX = 10
OBJ_TYPE_DIM_VIEW = 12
OBJ_TYPE_DATA_STORE = 18

# Network Survey filter patterns
SURVEY_PATTERNS = [
    "NorthpowerNetworkSurvey",
    "NPNetworkSur",
    "L_Email_SurveyDataDictionary",
    "Fact_NorthpowerNetworkSurvey",
    "Dim_NorthpowerNetworkSurvey",
]


def map_datatype(sql_server_type: str) -> str:
    """Map SQL Server data type to Snowflake."""
    t = sql_server_type.strip().lower()
    if t in ("integer", "int"):
        return "NUMBER"
    if t == "bigint":
        return "NUMBER(18,0)"
    if t == "smallint":
        return "NUMBER(5,0)"
    if t == "tinyint":
        return "NUMBER(3,0)"
    if t == "bit":
        return "BOOLEAN"
    if t in ("float", "real"):
        return "FLOAT"
    if t in ("datetime", "datetime2", "smalldatetime"):
        return "TIMESTAMP"
    if t == "date":
        return "DATE"
    if t == "time":
        return "TIME"
    if t in ("varchar(max)", "nvarchar(max)", "text", "ntext"):
        return "VARCHAR(16777216)"
    m = re.match(r"(n?varchar|n?char)\((\d+)\)", t)
    if m:
        return f"VARCHAR({m.group(2)})"
    m = re.match(r"(numeric|decimal)\((\d+)(?:,(\d+))?\)", t)
    if m:
        prec = m.group(2)
        scale = m.group(3) or "0"
        return f"NUMBER({prec},{scale})"
    m = re.match(r"(n?varchar|n?char)$", t)
    if m:
        return "VARCHAR"
    return "VARCHAR"


def is_survey_object(name: str) -> bool:
    for pat in SURVEY_PATTERNS:
        if pat.lower() in name.lower():
            return True
    return False


def parse_obj_file() -> dict:
    """Parse the object file to get type;id;name registry."""
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


def parse_quoted_values(line: str) -> list:
    """Extract quoted string values from a SQL INSERT values(...) clause.
    Skips the first value which is always 'ws_obj_object' from IDENT_CURRENT.
    """
    # Find the values(...) part
    val_start = line.find("values")
    if val_start == -1:
        val_start = line.find("VALUES")
    if val_start == -1:
        return []
    val_section = line[val_start:]
    # Extract all quoted values
    values = re.findall(r"'((?:[^']|'')*)'", val_section)
    values = [v.replace("''", "'") for v in values]
    # Skip the first value ('ws_obj_object' from IDENT_CURRENT)
    if values and values[0] == "ws_obj_object":
        values = values[1:]
    return values


def parse_data_file(survey_objects: dict) -> dict:
    """Parse the UTF-16LE data file extracting metadata for survey objects."""
    tables = {}
    current_table_name = None

    print("  Reading data file...")
    with open(DATA_FILE, "r", encoding="utf-16-le") as f:
        content = f.read()

    lines = content.split("\n")
    print(f"  Processing {len(lines)} lines...")

    for line in lines:
        if not line.strip():
            continue

        # Detect new object boundary - resets current_table tracking
        # This fires when a new ws_obj_object INSERT creates a new object
        if "ws_obj_object" in line and "oo_obj_key" in line and "oo_name" in line:
            # A new object is starting - only track it if it's a survey object
            name_m = re.search(r"'([^']+)',\s*\d+,\s*\d+,\s*\d+", line)
            if name_m:
                obj_name = name_m.group(1)
                if not is_survey_object(obj_name):
                    current_table_name = None
            continue

        # Load table metadata (type 8)
        if "ws_load_tab" in line and "values" in line:
            vals = parse_quoted_values(line)
            if len(vals) >= 2:
                tname = vals[0]  # lt_table_name
                if is_survey_object(tname):
                    description = vals[3] if len(vals) > 3 else ""
                    tables[tname] = {
                        "ws_type": "load",
                        "name": tname,
                        "description": description,
                        "columns": [],
                    }
                    current_table_name = tname
            continue

        # Load column metadata
        if "ws_load_col" in line and "values" in line:
            if current_table_name and current_table_name in tables:
                vals = parse_quoted_values(line)
                if len(vals) >= 4:
                    col_name = vals[0]  # lc_col_name
                    display_name = vals[1]  # lc_display_name
                    data_type = vals[2]  # lc_data_type
                    nulls_flag = vals[3]  # lc_nulls_flag
                    src_strategy = vals[10] if len(vals) > 10 else display_name
                    tables[current_table_name]["columns"].append({
                        "name": col_name,
                        "display_name": display_name,
                        "data_type": data_type,
                        "nullable": nulls_flag == "Y",
                        "description": src_strategy if src_strategy else display_name,
                    })
            continue

        # Stage table metadata (type 7)
        if "ws_stage_tab" in line and "values" in line:
            vals = parse_quoted_values(line)
            if len(vals) >= 2:
                tname = vals[0]  # st_table_name
                if is_survey_object(tname):
                    description = vals[3] if len(vals) > 3 else ""
                    from_clause = ""
                    # Look for FROM clause in st_where field
                    for v in vals:
                        if v.strip().upper().startswith("FROM"):
                            from_clause = v
                            break
                    tables[tname] = {
                        "ws_type": "stage",
                        "name": tname,
                        "description": description,
                        "from_clause": from_clause,
                        "columns": [],
                    }
                    current_table_name = tname
            continue

        # View table metadata (type 18 data stores)
        if "ws_view_tab" in line and "values" in line:
            vals = parse_quoted_values(line)
            if len(vals) >= 2:
                tname = vals[0]  # vt_table_name
                if is_survey_object(tname):
                    description = vals[5] if len(vals) > 5 else ""
                    tables[tname] = {
                        "ws_type": "data_store",
                        "name": tname,
                        "description": description,
                        "columns": [],
                    }
                    current_table_name = tname
            continue

        # Stage column metadata (used by type 7 AND type 18)
        if "ws_stage_col" in line and "values" in line:
            if current_table_name and current_table_name in tables:
                vals = parse_quoted_values(line)
                if len(vals) >= 4:
                    col_name = vals[0]
                    display_name = vals[1]
                    data_type = vals[2]
                    nulls_flag = vals[3]
                    src_strategy = vals[10] if len(vals) > 10 else display_name
                    tables[current_table_name]["columns"].append({
                        "name": col_name,
                        "display_name": display_name,
                        "data_type": data_type,
                        "nullable": nulls_flag == "Y",
                        "description": src_strategy if src_strategy else display_name,
                    })
            continue

        # Dimension table metadata (type 6 AND type 12)
        if "ws_dim_tab" in line and "values" in line:
            vals = parse_quoted_values(line)
            if len(vals) >= 2:
                tname = vals[0]  # dt_table_name
                if is_survey_object(tname):
                    description = vals[5] if len(vals) > 5 else ""
                    from_clause = ""
                    for v in vals:
                        if v.strip().upper().startswith("FROM"):
                            from_clause = v
                            break
                    # Check if this is type 12 (dim view) from registry
                    obj_info = survey_objects.get(tname, {})
                    if obj_info.get("type") == OBJ_TYPE_DIM_VIEW:
                        ws_type = "dim_view"
                    else:
                        ws_type = "dimension"
                    tables[tname] = {
                        "ws_type": ws_type,
                        "name": tname,
                        "description": description,
                        "from_clause": from_clause,
                        "columns": [],
                    }
                    current_table_name = tname
            continue

        # Dimension column metadata
        if "ws_dim_col" in line and "values" in line:
            if current_table_name and current_table_name in tables:
                vals = parse_quoted_values(line)
                if len(vals) >= 4:
                    col_name = vals[0]  # dc_col_name
                    display_name = vals[1]  # dc_display_name
                    data_type = vals[2]  # dc_data_type
                    nulls_flag = vals[3]  # dc_nulls_flag
                    # dc_key_type at index 12, dc_business_key_ind at 13, dc_artificial_key_ind at 14
                    key_type = vals[12] if len(vals) > 12 else ""
                    business_key_ind = vals[13] if len(vals) > 13 else ""
                    artificial_key_ind = vals[14] if len(vals) > 14 else ""
                    src_strategy = vals[11] if len(vals) > 11 else display_name

                    is_surrogate = artificial_key_ind == "Y"
                    is_business_key = key_type == "A"

                    tables[current_table_name]["columns"].append({
                        "name": col_name,
                        "display_name": display_name,
                        "data_type": data_type,
                        "nullable": nulls_flag == "Y",
                        "description": src_strategy if src_strategy else display_name,
                        "is_surrogate_key": is_surrogate,
                        "is_business_key": is_business_key,
                    })
            continue

        # Fact table metadata (type 5)
        if "ws_fact_tab" in line and "values" in line:
            vals = parse_quoted_values(line)
            if len(vals) >= 2:
                tname = vals[0]  # ft_table_name
                if is_survey_object(tname):
                    description = vals[5] if len(vals) > 5 else ""
                    tables[tname] = {
                        "ws_type": "fact",
                        "name": tname,
                        "description": description,
                        "columns": [],
                    }
                    current_table_name = tname
            continue

        # Fact column metadata
        if "ws_fact_col" in line and "values" in line:
            if current_table_name and current_table_name in tables:
                vals = parse_quoted_values(line)
                if len(vals) >= 4:
                    col_name = vals[0]  # fc_col_name
                    display_name = vals[1]  # fc_display_name
                    data_type = vals[2]  # fc_data_type
                    nulls_flag = vals[3]  # fc_nulls_flag
                    # fc_join_flag at index 7, fc_src_table at 10, fc_src_strategy at 12
                    join_flag = vals[7] if len(vals) > 7 else "N"
                    src_table = vals[10] if len(vals) > 10 else ""
                    src_strategy = vals[12] if len(vals) > 12 else display_name

                    tables[current_table_name]["columns"].append({
                        "name": col_name,
                        "display_name": display_name,
                        "data_type": data_type,
                        "nullable": nulls_flag == "Y",
                        "description": src_strategy if src_strategy else display_name,
                        "is_join": join_flag == "Y",
                        "src_table": src_table,
                    })
            continue

    return tables


def generate_source_node(table_meta: dict) -> dict:
    """Generate a Source node YAML structure."""
    columns = []
    for i, col in enumerate(table_meta["columns"], 1):
        col_def = {
            "name": col["name"].upper(),
            "dataType": map_datatype(col["data_type"]),
            "description": col.get("description", ""),
            "columnReference": {"stepCounter": "1", "columnCounter": str(i)},
        }
        if col.get("nullable", True):
            col_def["nullable"] = True
        columns.append(col_def)

    return {
        "name": table_meta["name"].upper(),
        "id": str(uuid.uuid4()),
        "fileVersion": 1,
        "type": "Node",
        "operation": {
            "locationName": "BRONZE",
            "name": table_meta["name"].upper(),
            "sqlType": "Source",
            "type": "sourceInput",
            "metadata": {
                "columns": columns,
            },
        },
    }


def generate_stage_node(table_meta: dict, location: str = "SILVER") -> dict:
    """Generate a Stage node YAML structure."""
    columns = []

    # Determine source from from_clause
    src_tables = set()
    from_clause = table_meta.get("from_clause", "")
    if from_clause:
        refs = re.findall(r"\[TABLEOWNER\]\.\[([^\]]+)\]", from_clause)
        if refs:
            src_tables.update(refs)
        else:
            refs = re.findall(r"\[([^\]]+)\]", from_clause)
            if refs:
                src_tables.update(refs)

    for i, col in enumerate(table_meta["columns"], 1):
        col_def = {
            "name": col["name"].upper(),
            "dataType": map_datatype(col["data_type"]),
            "description": col.get("description", ""),
            "columnReference": {"stepCounter": "1", "columnCounter": str(i)},
            "transform": f'"{col["name"].upper()}"',
        }
        if col.get("nullable", True):
            col_def["nullable"] = True
        columns.append(col_def)

    # Build source mapping
    source_mapping = []
    if src_tables:
        src_name = list(src_tables)[0]
        src_loc = "BRONZE" if src_name.startswith("L_") else "SILVER"
        source_mapping.append({
            "name": "Step 1",
            "join": {"joinCondition": f"FROM {{{{ ref('{src_loc}', '{src_name.upper()}') }}}}"},
            "dependencies": [{"locationName": src_loc, "nodeName": src_name.upper()}],
        })
    else:
        source_mapping.append({
            "name": "Step 1",
            "join": {"joinCondition": ""},
            "dependencies": [],
        })

    return {
        "name": table_meta["name"].upper(),
        "id": str(uuid.uuid4()),
        "fileVersion": 1,
        "type": "Node",
        "operation": {
            "locationName": location,
            "name": table_meta["name"].upper(),
            "sqlType": "Stage",
            "type": "sql",
            "isMultisource": False,
            "materializationType": "table",
            "config": {"truncateBefore": True},
            "metadata": {
                "columns": columns,
                "sourceMapping": source_mapping,
            },
        },
    }


def generate_dimension_node(table_meta: dict) -> dict:
    """Generate a Dimension node YAML structure."""
    columns = []
    business_keys = []

    # Determine source
    src_tables = set()
    from_clause = table_meta.get("from_clause", "")
    if from_clause:
        refs = re.findall(r"\[TABLEOWNER\]\.\[([^\]]+)\]", from_clause)
        if refs:
            src_tables.update(refs)
        else:
            refs = re.findall(r"\[([^\]]+)\]", from_clause)
            if refs:
                src_tables.update(refs)

    for i, col in enumerate(table_meta["columns"], 1):
        col_def = {
            "name": col["name"].upper(),
            "dataType": map_datatype(col["data_type"]),
            "description": col.get("description", ""),
            "columnReference": {"stepCounter": "1", "columnCounter": str(i)},
        }
        if col.get("nullable", True):
            col_def["nullable"] = True

        if col.get("is_surrogate_key"):
            col_def["isSurrogateKey"] = True
            col_def["keyColumnType"] = "surrogateKey"
        elif col.get("is_business_key"):
            col_def["isBusinessKey"] = True
            col_def["keyColumnType"] = "primaryBusinessKey"
            business_keys.append(col["name"].upper())

        if not col.get("is_surrogate_key"):
            col_def["transform"] = f'"{col["name"].upper()}"'

        columns.append(col_def)

    # Build source mapping
    source_mapping = []
    if src_tables:
        src_name = list(src_tables)[0]
        src_loc = "SILVER"
        source_mapping.append({
            "name": "Step 1",
            "join": {"joinCondition": f"FROM {{{{ ref('{src_loc}', '{src_name.upper()}') }}}}"},
            "dependencies": [{"locationName": src_loc, "nodeName": src_name.upper()}],
        })
    else:
        source_mapping.append({
            "name": "Step 1",
            "join": {"joinCondition": ""},
            "dependencies": [],
        })

    config = {}
    if business_keys:
        config["businessKeyColumns"] = business_keys

    return {
        "name": table_meta["name"].upper(),
        "id": str(uuid.uuid4()),
        "fileVersion": 1,
        "type": "Node",
        "operation": {
            "locationName": "GOLD",
            "name": table_meta["name"].upper(),
            "sqlType": "Dimension",
            "type": "sql",
            "isMultisource": False,
            "materializationType": "table",
            "config": config,
            "metadata": {
                "columns": columns,
                "sourceMapping": source_mapping,
            },
        },
    }


def generate_view_node(table_meta: dict) -> dict:
    """Generate a View node YAML structure (for type 12 dim views)."""
    columns = []

    # Determine the base dimension this view references
    base_dim = table_meta["name"].replace("Dim_", "D_")
    from_clause = table_meta.get("from_clause", "")
    if from_clause:
        refs = re.findall(r"\[TABLEOWNER\]\.\[([^\]]+)\]", from_clause)
        if refs:
            base_dim = refs[0]

    for i, col in enumerate(table_meta["columns"], 1):
        col_def = {
            "name": col["name"].upper(),
            "dataType": map_datatype(col["data_type"]),
            "description": col.get("description", ""),
            "columnReference": {"stepCounter": "1", "columnCounter": str(i)},
            "transform": f'"{col["name"].upper()}"',
        }
        if col.get("nullable", True):
            col_def["nullable"] = True
        columns.append(col_def)

    source_mapping = [{
        "name": "Step 1",
        "join": {"joinCondition": f"FROM {{{{ ref('GOLD', '{base_dim.upper()}') }}}}"},
        "dependencies": [{"locationName": "GOLD", "nodeName": base_dim.upper()}],
    }]

    return {
        "name": table_meta["name"].upper(),
        "id": str(uuid.uuid4()),
        "fileVersion": 1,
        "type": "Node",
        "operation": {
            "locationName": "GOLD",
            "name": table_meta["name"].upper(),
            "sqlType": "View",
            "type": "sql",
            "isMultisource": False,
            "materializationType": "view",
            "config": {},
            "metadata": {
                "columns": columns,
                "sourceMapping": source_mapping,
            },
        },
    }


def generate_fact_node(table_meta: dict) -> dict:
    """Generate a Fact node YAML structure."""
    columns = []

    # Determine source from column references
    src_tables = set()
    for col in table_meta["columns"]:
        if col.get("src_table"):
            src_tables.add(col["src_table"])

    for i, col in enumerate(table_meta["columns"], 1):
        col_def = {
            "name": col["name"].upper(),
            "dataType": map_datatype(col["data_type"]),
            "description": col.get("description", ""),
            "columnReference": {"stepCounter": "1", "columnCounter": str(i)},
            "transform": f'"{col["name"].upper()}"',
        }
        if col.get("nullable", True):
            col_def["nullable"] = True
        columns.append(col_def)

    # Primary source is the S_ summary table
    primary_source = "S_NORTHPOWERNETWORKSURVEY"
    source_mapping = [{
        "name": "Step 1",
        "join": {"joinCondition": f"FROM {{{{ ref('SILVER', '{primary_source}') }}}}"},
        "dependencies": [{"locationName": "SILVER", "nodeName": primary_source}],
    }]

    return {
        "name": table_meta["name"].upper(),
        "id": str(uuid.uuid4()),
        "fileVersion": 1,
        "type": "Node",
        "operation": {
            "locationName": "GOLD",
            "name": table_meta["name"].upper(),
            "sqlType": "Fact",
            "type": "sql",
            "isMultisource": False,
            "materializationType": "table",
            "config": {},
            "metadata": {
                "columns": columns,
                "sourceMapping": source_mapping,
            },
        },
    }


def write_node_yaml(node_data: dict, location: str, name: str):
    """Write a node YAML file."""
    filename = f"{location}-{name}.yml"
    filepath = OUTPUT_DIR / filename

    class CoalesceYamlDumper(yaml.Dumper):
        pass

    def str_representer(dumper, data):
        if "\n" in data:
            return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
        return dumper.represent_scalar("tag:yaml.org,2002:str", data)

    CoalesceYamlDumper.add_representer(str, str_representer)

    with open(filepath, "w", encoding="utf-8") as f:
        yaml.dump(node_data, f, Dumper=CoalesceYamlDumper, default_flow_style=False, sort_keys=False, allow_unicode=True)

    print(f"  Written: {filename}")


def main():
    print("=" * 60)
    print("WhereScape RED to Coalesce Migration - Network Survey POC")
    print("=" * 60)

    # Step 1: Parse object registry
    print("\n[1] Parsing object registry...")
    all_objects = parse_obj_file()
    survey_objects = {k: v for k, v in all_objects.items() if is_survey_object(k)}
    print(f"  Found {len(all_objects)} total objects")
    print(f"  Found {len(survey_objects)} Network Survey objects")

    by_type = {}
    for name, obj in survey_objects.items():
        t = obj["type"]
        by_type.setdefault(t, []).append(name)

    type_labels = {8: "Load", 7: "Stage", 6: "Dimension", 5: "Fact", 12: "Dim View", 18: "Data Store", 1: "Procedure", 10: "Index"}
    for t, names in sorted(by_type.items()):
        print(f"  Type {t} ({type_labels.get(t, '?')}): {len(names)} - {', '.join(sorted(names)[:3])}...")

    # Step 2: Parse data file for column metadata
    print("\n[2] Parsing data file for column metadata...")
    tables = parse_data_file(survey_objects)
    print(f"  Extracted metadata for {len(tables)} tables:")
    for name, meta in sorted(tables.items()):
        print(f"    {meta['ws_type']:12s} | {name} ({len(meta['columns'])} cols)")

    # Step 3: Generate YAML nodes
    print(f"\n[3] Generating Coalesce node YAML files in {OUTPUT_DIR}/")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Clean existing generated files
    for f in OUTPUT_DIR.glob("*.yml"):
        f.unlink()

    generated = 0
    for name, meta in sorted(tables.items()):
        ws_type = meta["ws_type"]
        if not meta["columns"]:
            print(f"  SKIPPED (0 cols): {name}")
            continue

        if ws_type == "load":
            node = generate_source_node(meta)
            write_node_yaml(node, "BRONZE", name.upper())
        elif ws_type == "stage":
            node = generate_stage_node(meta, "SILVER")
            write_node_yaml(node, "SILVER", name.upper())
        elif ws_type == "data_store":
            node = generate_stage_node(meta, "SILVER")
            write_node_yaml(node, "SILVER", name.upper())
        elif ws_type == "dimension":
            node = generate_dimension_node(meta)
            write_node_yaml(node, "GOLD", name.upper())
        elif ws_type == "dim_view":
            node = generate_view_node(meta)
            write_node_yaml(node, "GOLD", name.upper())
        elif ws_type == "fact":
            node = generate_fact_node(meta)
            write_node_yaml(node, "GOLD", name.upper())
        else:
            print(f"  SKIPPED (unknown type): {name} ({ws_type})")
            continue
        generated += 1

    print(f"\n  Total nodes generated: {generated}")
    print("\n[4] Done! Run 'coa validate' to verify the generated nodes.")


if __name__ == "__main__":
    main()
