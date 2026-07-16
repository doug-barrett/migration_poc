#!/usr/bin/env python3
"""
Phase 1 PLAN: WhereScape RED to Coalesce Migration Script Analyzer.

Parses WhereScape export files (app_obj, app_data) to:
1. Extract SQL statements from Python-wrapped scripts
2. Build column-level mappings from INSERT/UPDATE/MERGE statements
3. Map scripts to target objects using naming conventions
4. Generate three CSV outputs: mapping, scripts, unmapped_objects
"""

import re
import csv
import sys
from pathlib import Path
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass, asdict
import logging

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
log = logging.getLogger(__name__)


@dataclass
class ColumnMapping:
    obj_name: str
    obj_type: str
    col_name: str
    data_type: str
    src_obj: str
    src_col: str
    transform: str
    obj_join: str
    location: str


@dataclass
class ScriptMetadata:
    script_name: str
    target_obj: str
    target_obj_type: str
    line_count: int
    sql_stmt_count: int
    from_tables: str


@dataclass
class UnmappedObject:
    obj_name: str
    obj_type: str
    obj_id: str
    reason: str


class WhereScapeParser:
    """Parses WhereScape RED export files."""

    def __init__(self, wst_dir: Path):
        self.wst_dir = Path(wst_dir)
        self.obj_file = None
        self.data_file = None
        self._find_wst_files()

    def _find_wst_files(self):
        """Auto-detect app_obj and app_data files."""
        files = list(self.wst_dir.glob("app_obj_*.wst"))
        if not files:
            raise FileNotFoundError(f"No app_obj_*.wst files found in {self.wst_dir}")
        self.obj_file = files[0]

        files = list(self.wst_dir.glob("app_data_*.wst"))
        if not files:
            raise FileNotFoundError(f"No app_data_*.wst files found in {self.wst_dir}")
        self.data_file = files[0]

        log.info(f"Using obj file: {self.obj_file.name}")
        log.info(f"Using data file: {self.data_file.name}")

    def parse_obj_registry(self) -> Dict[str, Dict]:
        """
        Parse app_obj_*.wst (ASCII, semicolon-delimited).
        Returns: {name: {type, id, name}}
        """
        registry = {}
        with open(self.obj_file, 'r', encoding='ascii', errors='ignore') as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith(';'):
                    continue
                parts = line.split(';')
                if len(parts) >= 3:
                    obj_type, obj_id, obj_name = parts[0], parts[1], parts[2]
                    registry[obj_name] = {
                        'type': obj_type,
                        'id': obj_id,
                        'name': obj_name
                    }
        log.info(f"Loaded {len(registry)} objects from registry")
        return registry

    def parse_data_file(self) -> Dict[str, str]:
        """
        Parse app_data_*.wst (UTF-16LE encoded).
        Extract ws_obj_object INSERT to get object names.
        Extract ws_scr_line entries to reassemble scripts.
        Returns: {script_name: script_text}
        """
        # Read UTF-16LE file
        with open(self.data_file, 'r', encoding='utf-16-le', errors='ignore') as f:
            content = f.read()

        # Split into object blocks by the identity insert marker
        blocks = content.split('_WS&SQL_ SET IDENTITY_INSERT ws_obj_object ON;')
        scripts = {}

        for block in blocks[1:]:  # Skip header
            # Extract object name from ws_obj_object INSERT (case-insensitive)
            # Pattern: insert into _WS&DATABASE_ws_obj_object ... values (..., 'object_name', ...)
            obj_match = re.search(
                r"insert into _WS&DATABASE_ws_obj_object\s*\([^)]*\)\s*values\s*\([^,]*,\s*'(.*?)'",
                block,
                re.IGNORECASE | re.DOTALL
            )
            if not obj_match:
                continue

            obj_name = obj_match.group(1).replace("''", "'")

            # Extract script lines from ws_scr_line entries
            # Pattern: insert into _WS&DATABASE_ws_scr_line ... values (IDENT_CURRENT(...), line_no, 'line_text')
            script_lines = {}
            line_pattern = r"insert into _WS&DATABASE_ws_scr_line\s*\([^)]*\)\s*values\s*\([^,]+,\s*(\d+),\s*'((?:[^']|'')*?)'\s*\)"
            for match in re.finditer(line_pattern, block, re.IGNORECASE | re.DOTALL):
                line_no = int(match.group(1))
                line_text = match.group(2).replace("''", "'")
                script_lines[line_no] = line_text

            if script_lines:
                sorted_lines = [script_lines[k] for k in sorted(script_lines.keys())]
                script_text = '\n'.join(sorted_lines)
                scripts[obj_name] = script_text

        log.info(f"Extracted {len(scripts)} scripts from data file")
        return scripts

    def extract_sql_statements(self, script_text: str) -> List[str]:
        """
        Extract SQL statements from Python f-string literals.
        Handles triple-quoted strings with or without f-prefix.
        """
        statements = []

        # Pattern: f"""...""" or """..."""
        pattern = r'(?:f)?"""(.*?)"""'
        for match in re.finditer(pattern, script_text, re.DOTALL):
            sql = match.group(1).strip()
            if sql:
                statements.append(sql)

        return statements


class SQLParser:
    """Parses SQL statements to extract column mappings."""

    # Database prefix to location mapping
    DB_LOCATION_MAP = {
        'P_BI_EDWTAB_DB': 'GOLD',
        'P_BI_STGTAB_DB': 'SILVER',
        'P_BI_MTAB_DB': 'GOLD',
    }

    @staticmethod
    def strip_db_prefix(table_ref: str) -> str:
        """Strip database prefix: P_BI_EDWTAB_DB.TABLE -> TABLE"""
        match = re.match(r'P_\w+_DB\.(\w+)', table_ref.strip(), re.IGNORECASE)
        if match:
            return match.group(1)
        return table_ref.strip()

    @staticmethod
    def get_location(table_ref: str) -> str:
        """Determine location (GOLD/SILVER/BRONZE) from database prefix."""
        for prefix, location in SQLParser.DB_LOCATION_MAP.items():
            if table_ref.upper().startswith(prefix):
                return location
        return 'BRONZE'

    @staticmethod
    def parse_insert_statement(sql: str) -> Optional[Dict]:
        """
        Parse INSERT INTO statement.
        Returns: {target_table, columns, select_part, raw_sql}
        """
        # Handle INSERT ALL
        if 'INSERT ALL' in sql.upper():
            # Get first INTO clause (take first target only)
            # More robust pattern to handle newlines: match THEN INTO table (col1, col2, ...)
            match = re.search(
                r'THEN\s+INTO\s+(\S+)\s*\(\s*(.*?)\s*\)\s*SELECT',
                sql,
                re.IGNORECASE | re.DOTALL
            )
            if match:
                target = match.group(1)
                # Extract column list and clean up
                col_str = match.group(2)
                col_str = re.sub(r'\s+', ' ', col_str)
                columns = [c.strip() for c in col_str.split(',') if c.strip()]
                
                # For INSERT ALL, the main SELECT comes after the columns
                # Extract main SELECT...FROM by looking after first INTO...() section
                # Find position after the ") SELECT" from our match
                match_end = match.end()
                main_select_part = sql[match_end - 6:]  # Include "SELECT"
                
                # Now extract SELECT ... FROM from this part
                select_from_match = re.search(
                    r'SELECT\s+(.*?)\s+FROM',
                    main_select_part,
                    re.IGNORECASE | re.DOTALL
                )
                select_part = select_from_match.group(1) if select_from_match else ''
                
                # Extract FROM clause (from main FROM to WHERE/GROUP/HAVING or end)
                from_match = re.search(
                    r'FROM\s+(.*?)(?:WHERE|GROUP|HAVING|$)',
                    main_select_part,
                    re.IGNORECASE | re.DOTALL
                )
                from_part = from_match.group(1) if from_match else ''
                
                return {
                    'type': 'insert',
                    'target_table': target,
                    'columns': columns,
                    'select_part': select_part,
                    'from_part': from_part,
                    'raw_sql': sql
                }
        else:
            # Regular INSERT INTO ... SELECT
            match = re.search(
                r'INSERT\s+INTO\s+(\S+)\s*\(\s*(.*?)\s*\)\s*SELECT\s+(.*?)\s+FROM\s+(.*?)(?:WHERE|GROUP|$)',
                sql,
                re.IGNORECASE | re.DOTALL
            )
            if match:
                target = match.group(1)
                columns = [c.strip() for c in match.group(2).split(',')]
                select_part = match.group(3)
                from_part = match.group(4)
                return {
                    'type': 'insert',
                    'target_table': target,
                    'columns': columns,
                    'select_part': select_part,
                    'from_part': from_part,
                    'raw_sql': sql
                }
        return None

    @staticmethod
    def parse_merge_statement(sql: str) -> Optional[Dict]:
        """Parse MERGE INTO statement."""
        match = re.search(
            r'MERGE\s+INTO\s+(\S+)',
            sql,
            re.IGNORECASE
        )
        if match:
            target = match.group(1)
            from_match = re.search(r'USING\s+(.*?)(?:ON|$)', sql, re.IGNORECASE | re.DOTALL)
            from_part = from_match.group(1) if from_match else ''
            return {
                'type': 'merge',
                'target_table': target,
                'from_part': from_part,
                'raw_sql': sql
            }
        return None

    @staticmethod
    def parse_update_statement(sql: str) -> Optional[Dict]:
        """Parse UPDATE statement."""
        match = re.search(
            r'UPDATE\s+(\S+)',
            sql,
            re.IGNORECASE
        )
        if match:
            target = match.group(1)
            from_match = re.search(r'FROM\s+(.*?)(?:WHERE|$)', sql, re.IGNORECASE | re.DOTALL)
            from_part = from_match.group(1) if from_match else ''
            return {
                'type': 'update',
                'target_table': target,
                'from_part': from_part,
                'raw_sql': sql
            }
        return None

    @staticmethod
    def extract_from_tables(from_part: str) -> List[str]:
        """Extract table names and aliases from FROM/JOIN clause."""
        tables = []
        # Pattern: TABLE [alias] or TABLE AS alias
        pattern = r'(?:FROM|JOIN|CROSS JOIN|LEFT\s+JOIN|RIGHT\s+JOIN|INNER\s+JOIN|FULL\s+JOIN)\s+(\S+)(?:\s+(?:AS\s+)?(\w+))?'
        for match in re.finditer(pattern, from_part, re.IGNORECASE):
            table = match.group(1).strip()
            table = SQLParser.strip_db_prefix(table)
            tables.append(table)
        return tables

    @staticmethod
    def map_select_to_columns(select_part: str, columns: List[str]) -> Dict[str, str]:
        """
        Map SELECT expressions to INSERT columns (positional).
        Returns: {column_name: expression}
        """
        mapping = {}
        # Simple split by comma (naive, but handles most cases)
        expressions = [e.strip() for e in re.split(r',(?![^()]*\))', select_part)]
        expressions = [e for e in expressions if e]

        for i, col in enumerate(columns):
            if i < len(expressions):
                expr = expressions[i]
                # Extract alias if present
                alias_match = re.search(r'\s+(?:AS\s+)?(\w+)\s*$', expr, re.IGNORECASE)
                if alias_match:
                    alias = alias_match.group(1)
                    if alias.upper() not in ('FROM', 'WHERE', 'GROUP', 'HAVING', 'ORDER'):
                        expr = alias

                # Simplify expression: if it's TABLE.COLUMN, extract just COLUMN
                simple_match = re.match(r'\w+\.(\w+)', expr)
                if simple_match:
                    simple_col = simple_match.group(1)
                    mapping[col] = simple_col
                else:
                    mapping[col] = expr

        return mapping


class MigrationPlanner:
    """Orchestrates the migration planning."""

    def __init__(self, wst_dir: Path, output_dir: Path):
        self.wst_dir = Path(wst_dir)
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.parser = WhereScapeParser(wst_dir)
        self.obj_registry = self.parser.parse_obj_registry()
        self.scripts = self.parser.parse_data_file()

        self.mappings: List[ColumnMapping] = []
        self.script_metadata: List[ScriptMetadata] = []
        self.unmapped_objects: List[UnmappedObject] = []

    def map_script_name_to_object(self, script_name: str) -> Optional[str]:
        """
        Map script name to target object using naming conventions.
        Strips prefixes: UPDATE_, upd_, custom_, drv_, update_stage_, update_model_
        """
        # Normalize script name
        normalized = script_name.lower()

        # Try each naming convention
        patterns = [
            (r'^update_(.+)$', 'UPDATE_'),
            (r'^upd_(.+)$', 'upd_'),
            (r'^custom_(.+)$', 'custom_'),
            (r'^drv_(.+)$', 'drv_'),
            (r'^update_stage_(.+)$', 'update_stage_'),
            (r'^update_model_(.+)$', 'update_model_'),
        ]

        target_name = None
        for pattern, prefix in patterns:
            match = re.match(pattern, normalized)
            if match:
                target_name = match.group(1).upper()
                break

        if target_name:
            # Find in registry (case-insensitive)
            for reg_name, reg_data in self.obj_registry.items():
                if reg_name.upper() == target_name:
                    return reg_name
        return None

    def process_scripts(self):
        """Process all extracted scripts and build mappings."""
        for script_name, script_text in self.scripts.items():
            target_obj_name = self.map_script_name_to_object(script_name)
            if not target_obj_name:
                log.warning(f"Could not map script: {script_name}")
                continue

            target_obj = self.obj_registry.get(target_obj_name)
            if not target_obj:
                continue

            obj_type = target_obj['type']
            sql_statements = self.parser.extract_sql_statements(script_text)
            from_tables_set = set()

            for sql in sql_statements:
                # Skip CREATE TEMPORARY TABLE
                if 'CREATE TEMPORARY TABLE' in sql.upper() or 'CREATE TEMP TABLE' in sql.upper():
                    continue

                # Try to parse INSERT
                insert_info = SQLParser.parse_insert_statement(sql)
                if insert_info:
                    target_table = SQLParser.strip_db_prefix(insert_info['target_table'])
                    location = SQLParser.get_location(insert_info['target_table'])
                    columns = insert_info['columns']
                    select_part = insert_info['select_part']
                    from_part = insert_info['from_part']

                    # Extract FROM tables
                    from_tables = SQLParser.extract_from_tables(from_part)
                    from_tables_set.update(from_tables)

                    # Map SELECT to columns
                    col_mapping = SQLParser.map_select_to_columns(select_part, columns)

                    for col_name, expression in col_mapping.items():
                        # Determine src_obj and src_col
                        src_obj = ''
                        src_col = ''
                        transform = expression

                        # If expression is simple TABLE.COLUMN, extract both
                        table_col_match = re.match(r'(\w+)\.(\w+)', expression)
                        if table_col_match:
                            src_obj = table_col_match.group(1)
                            src_col = table_col_match.group(2)
                            transform = ''

                        mapping = ColumnMapping(
                            obj_name=target_obj_name,
                            obj_type=obj_type,
                            col_name=col_name,
                            data_type='',
                            src_obj=src_obj,
                            src_col=src_col,
                            transform=transform,
                            obj_join=from_part.strip(),
                            location=location
                        )
                        self.mappings.append(mapping)

            # Record script metadata
            script_metadata = ScriptMetadata(
                script_name=script_name,
                target_obj=target_obj_name,
                target_obj_type=obj_type,
                line_count=len(script_text.split('\n')),
                sql_stmt_count=len(sql_statements),
                from_tables=', '.join(sorted(from_tables_set))
            )
            self.script_metadata.append(script_metadata)

    def find_unmapped_objects(self):
        """Identify objects (type 6/7/8/18) without matching scripts."""
        mapped_obj_names = {m.script_name for m in self.script_metadata}

        for obj_name, obj_data in self.obj_registry.items():
            obj_type = obj_data['type']
            obj_id = obj_data['id']

            # Only care about types 6, 7, 8, 18
            if obj_type not in ('6', '7', '8', '18'):
                continue

            if obj_name not in mapped_obj_names:
                reason = f"No matching script found (type={obj_type})"
                unmapped = UnmappedObject(
                    obj_name=obj_name,
                    obj_type=obj_type,
                    obj_id=obj_id,
                    reason=reason
                )
                self.unmapped_objects.append(unmapped)

    def write_outputs(self):
        """Write three CSV files: mapping, scripts, unmapped_objects."""
        # mapping.csv
        mapping_file = self.output_dir / 'mapping.csv'
        with open(mapping_file, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=[
                'obj_name', 'obj_type', 'col_name', 'data_type',
                'src_obj', 'src_col', 'transform', 'obj_join', 'location'
            ])
            writer.writeheader()
            for m in self.mappings:
                writer.writerow(asdict(m))
        log.info(f"Wrote {len(self.mappings)} column mappings to {mapping_file}")

        # scripts.csv
        scripts_file = self.output_dir / 'scripts.csv'
        with open(scripts_file, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=[
                'script_name', 'target_obj', 'target_obj_type',
                'line_count', 'sql_stmt_count', 'from_tables'
            ])
            writer.writeheader()
            for s in self.script_metadata:
                writer.writerow(asdict(s))
        log.info(f"Wrote {len(self.script_metadata)} scripts to {scripts_file}")

        # unmapped_objects.csv
        unmapped_file = self.output_dir / 'unmapped_objects.csv'
        with open(unmapped_file, 'w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, fieldnames=[
                'obj_name', 'obj_type', 'obj_id', 'reason'
            ])
            writer.writeheader()
            for u in self.unmapped_objects:
                writer.writerow(asdict(u))
        log.info(f"Wrote {len(self.unmapped_objects)} unmapped objects to {unmapped_file}")

    def run(self):
        """Execute the full planning pipeline."""
        log.info("Starting Phase 1 PLAN...")
        self.process_scripts()
        self.find_unmapped_objects()
        self.write_outputs()
        log.info("Phase 1 PLAN complete")


def main():
    if len(sys.argv) != 3:
        print("Usage: python3 01_plan.py <wst_dir> <output_dir>")
        print("  wst_dir: Directory containing app_obj_*.wst and app_data_*.wst files")
        print("  output_dir: Output directory for CSV files")
        sys.exit(1)

    wst_dir = Path(sys.argv[1])
    output_dir = Path(sys.argv[2])

    if not wst_dir.exists():
        print(f"Error: wst_dir does not exist: {wst_dir}")
        sys.exit(1)

    planner = MigrationPlanner(wst_dir, output_dir)
    planner.run()


if __name__ == '__main__':
    main()
