#!/usr/bin/env python3
"""
Fix hardcoded DOUG_POC references and uppercase identifiers in Coalesce node files.
"""
import os
import re
import yaml
from pathlib import Path
from typing import Dict, List, Tuple, Optional

NODE_DIR = Path("/Users/dougy/GIT/migration_poc/nodes")

def load_node_map() -> Dict[str, Tuple[str, str]]:
    """Load all nodes and map NAME -> (location, filename)"""
    node_map = {}
    for yml_file in NODE_DIR.glob("*.yml"):
        try:
            with open(yml_file, 'r') as f:
                data = yaml.safe_load(f)
                if not data:
                    continue
                node_name = data.get('name', '')
                location = data.get('operation', {}).get('locationName', '')
                if node_name and location:
                    node_map[node_name] = (location, yml_file.name)
        except Exception as e:
            print(f"Error loading {yml_file.name}: {e}")
    return node_map

def extract_table_name(ref: str) -> Optional[str]:
    """Extract TABLE_NAME from 'DOUG_POC'.'DP001'.'TABLE_NAME' pattern"""
    match = re.search(r'"DOUG_POC"\."DP001"\.\"([^"]+)"', ref)
    if match:
        return match.group(1)
    return None

def replace_doug_poc_refs(join_condition: str, node_map: Dict[str, Tuple[str, str]]) -> str:
    """Replace DOUG_POC references with ref() or IDENTIFIER()"""
    
    def replacer(match):
        full_ref = match.group(0)
        table_name = extract_table_name(full_ref)
        
        if not table_name:
            return full_ref
        
        # Check if this table exists as a node
        if table_name in node_map:
            location, _ = node_map[table_name]
            # Return ref() with uppercase table name
            return f"{{{{ ref('{location}', '{table_name}') }}}} \"{table_name}\""
        else:
            # Use IDENTIFIER() for external tables
            return f"IDENTIFIER('WS_MIGRATION.GOLD.{table_name}')"
    
    # Replace all DOUG_POC references
    pattern = r'"DOUG_POC"\."DP001"\.\"[^"]+\"'
    result = re.sub(pattern, replacer, join_condition)
    return result

def remove_duplicate_from(join_condition: str) -> str:
    """Remove duplicate FROM clauses, keeping only the first one"""
    # Check if there are multiple FROM keywords
    from_count = len(re.findall(r'\bFROM\b', join_condition, re.IGNORECASE))
    
    if from_count <= 1:
        return join_condition
    
    # Split on FROM
    from_parts = re.split(r'\bFROM\b', join_condition, flags=re.IGNORECASE)
    
    # First part is before first FROM, usually empty
    result_parts = []
    
    # Process FROM clauses
    where_conditions = []
    
    for i, part in enumerate(from_parts[1:], 1):
        # Find WHERE in this part
        where_match = re.search(r'\bWHERE\b', part, re.IGNORECASE)
        
        if i == 1:
            # First FROM clause - keep it all
            result_parts.append("FROM " + part)
        else:
            # Subsequent FROM - extract WHERE clause only
            if where_match:
                where_start = where_match.start()
                where_part = part[where_start:]
                # Clean up - remove subsequent FROM if present
                where_part = re.sub(r'\s+FROM\s+.*', '', where_part, flags=re.IGNORECASE | re.DOTALL)
                where_conditions.append(where_part)
    
    # Combine result
    result = result_parts[0] if result_parts else "FROM "
    
    # Extract WHERE from first FROM if present
    if 'WHERE' in result or 'where' in result:
        # Already has WHERE, extract it
        match = re.search(r'(\bWHERE\b.*)', result, re.IGNORECASE | re.DOTALL)
        if match:
            first_where = match.group(1)
            result = re.sub(r'\bWHERE\b.*', '', result, flags=re.IGNORECASE | re.DOTALL).rstrip()
            where_conditions.insert(0, first_where)
    
    # Consolidate WHERE clauses
    if where_conditions:
        # Filter out SCD2 patterns
        filtered = []
        for cond in where_conditions:
            cond = cond.strip()
            # Remove SCD2 self-join pattern at start
            # Pattern: TABLE.COL = STGTABLE.COL AND ...
            cond = re.sub(r'WHERE\s+\w+\.\w+\s*=\s*\w+\.\w+\s*AND\s+', 'WHERE ', cond, flags=re.IGNORECASE)
            if cond not in filtered:
                filtered.append(cond)
        
        if filtered:
            where_clause = " AND ".join(c.strip() for c in filtered if c.strip() and c.upper() != 'WHERE')
            if where_clause:
                result += f"\n{where_clause if where_clause.upper().startswith('WHERE') else 'WHERE ' + where_clause}"
    
    return result

def uppercase_identifiers(sql_text: str) -> str:
    """Uppercase all table and column identifiers"""
    if not sql_text:
        return sql_text
    
    # Keywords to avoid replacing
    keywords = {
        'FROM', 'WHERE', 'AND', 'OR', 'NOT', 'IN', 'BETWEEN', 'CASE', 'WHEN', 
        'THEN', 'ELSE', 'END', 'SELECT', 'AS', 'ON', 'JOIN', 'LEFT', 'RIGHT', 
        'INNER', 'OUTER', 'CROSS', 'FULL', 'UNION', 'ORDER', 'BY', 'GROUP', 
        'HAVING', 'DISTINCT', 'ALL', 'NULL', 'TRUE', 'FALSE'
    }
    
    # Step 1: Replace Table.Column with "TABLE"."COLUMN"
    def replace_table_col(match):
        table = match.group(1)
        col = match.group(2)
        # Only if not already quoted
        if not (table.startswith('"') or col.startswith('"')):
            return f'"{table.upper()}"."{col.upper()}"'
        return match.group(0)
    
    result = re.sub(
        r'\b([A-Za-z_]\w*)\.([A-Za-z_]\w*)\b',
        replace_table_col,
        sql_text
    )
    
    # Step 2: Replace unquoted identifiers with underscores or mixed case
    def replace_identifier(match):
        word = match.group(0)
        # Skip if already quoted
        if word.startswith('"'):
            return word
        # Skip keywords
        if word.upper() in keywords:
            return word
        # If has underscores or mixed case, uppercase and quote
        if '_' in word or any(c.isupper() for c in word):
            return f'"{word.upper()}"'
        return word
    
    # More careful regex: word boundary, not preceded/followed by quotes
    result = re.sub(r'\b[A-Za-z_]\w*\b(?!["\'])', replace_identifier, result)
    
    return result

def fix_node_file(file_path: Path, node_map: Dict[str, Tuple[str, str]]) -> Tuple[bool, str]:
    """Fix a single node file. Returns (changed, message)"""
    try:
        with open(file_path, 'r') as f:
            data = yaml.safe_load(f)
        
        if not data:
            return False, f"Empty file: {file_path.name}"
        
        # Navigate to metadata
        metadata = data.get('operation', {}).get('metadata', {})
        if not metadata:
            return False, f"No operation.metadata in {file_path.name}"
        
        changed = False
        
        # Process sourceMapping
        for mapping in metadata.get('sourceMapping', []):
            if 'join' not in mapping or 'joinCondition' not in mapping['join']:
                continue
            
            join_cond = mapping['join']['joinCondition']
            original_join = join_cond
            
            # Fix DOUG_POC references
            if 'DOUG_POC' in join_cond:
                join_cond = replace_doug_poc_refs(join_cond, node_map)
            
            # Remove duplicate FROM clauses
            if join_cond.count('FROM') > 1 or join_cond.count('from') > 1:
                join_cond = remove_duplicate_from(join_cond)
            
            # Uppercase identifiers
            join_cond = uppercase_identifiers(join_cond)
            
            if join_cond != original_join:
                mapping['join']['joinCondition'] = join_cond
                changed = True
        
        # Also fix transform fields
        for col in metadata.get('columns', []):
            if 'sourceColumnReferences' in col:
                for src_ref in col.get('sourceColumnReferences', []):
                    if 'transform' in src_ref:
                        transform = src_ref['transform']
                        new_transform = uppercase_identifiers(transform)
                        if new_transform != transform:
                            src_ref['transform'] = new_transform
                            changed = True
        
        if changed:
            # Write back with proper YAML formatting
            with open(file_path, 'w') as f:
                yaml.dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True, width=1000)
            return True, f"Fixed {file_path.name}"
        else:
            return False, f"No changes: {file_path.name}"
    
    except Exception as e:
        import traceback
        return False, f"Error fixing {file_path.name}: {e}"

def main():
    print("Loading node map...")
    node_map = load_node_map()
    print(f"Found {len(node_map)} nodes\n")
    
    # Get all DOUG_POC files
    doug_poc_files = []
    for f in NODE_DIR.glob("*.yml"):
        content = f.read_text()
        if "DOUG_POC" in content:
            doug_poc_files.append(f)
    
    print(f"Found {len(doug_poc_files)} files with DOUG_POC references\n")
    
    fixed_count = 0
    for file_path in sorted(doug_poc_files):
        changed, message = fix_node_file(file_path, node_map)
        status = "✓" if changed else " "
        print(f"{status} {message}")
        if changed:
            fixed_count += 1
    
    print(f"\n✓ Fixed {fixed_count}/{len(doug_poc_files)} files")

if __name__ == "__main__":
    main()
