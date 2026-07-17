#!/usr/bin/env python3
"""
Fix malformed joinCondition WHERE clauses in Coalesce nodes.
Removes SCD2 self-join patterns and keeps real filter conditions.
"""

import os
import re
import yaml
from pathlib import Path
from typing import Optional, Tuple

NODES_DIR = Path("/Users/dougy/GIT/migration_poc/nodes")


def contains_subquery(text: str) -> bool:
    """Check if text contains a WHERE inside parentheses (subquery)."""
    return bool(re.search(r'\(\s*SELECT\s+.*WHERE\s+', text, re.IGNORECASE | re.DOTALL))


def extract_table_references(join_condition: str) -> set:
    """Extract table names/aliases from a joinCondition."""
    # Look for quoted identifiers like "TABLE_NAME" or unquoted ones
    quoted = re.findall(r'"(\w+)"', join_condition)
    unquoted = re.findall(r'\b([A-Z_]\w*)\b(?=\s*\.|\s*\()', join_condition)
    return set(quoted) | set(unquoted)


def has_self_join_pattern(join_condition: str) -> bool:
    """Check if condition has SCD2-like self-join pattern."""
    # Pattern: TABLE_NAME.column = SOURCE_TABLE.column
    # e.g., AR_INVOICE.Company_Code = STG_AR_INVOICE.Company_Code
    # or "AR_INVOICE"."Company_Code" = "STG_AR_INVOICE"."Company_Code"
    
    # Look for pattern where a table appears on both sides of = with different tables
    self_join = re.search(
        r'(\w+)\s*\.\s*\w+\s*=\s*(\w+)\s*\.\s*\w+',
        join_condition
    )
    
    if self_join:
        left_table = self_join.group(1)
        right_table = self_join.group(2)
        # Check if they're different (one is likely STG_ variant)
        if left_table != right_table:
            return True
    
    # Also check quoted version
    self_join_quoted = re.search(
        r'"(\w+)"\s*\.\s*"?(\w+)"?\s*=\s*"(\w+)"\s*\.\s*"?(\w+)"?',
        join_condition
    )
    
    if self_join_quoted:
        left_table = self_join_quoted.group(1)
        right_table = self_join_quoted.group(3)
        if left_table != right_table:
            return True
    
    return False


def clean_joincondition(join_condition: str, node_name: str) -> str:
    """
    Clean a joinCondition by removing SCD2 self-join patterns.
    Returns the cleaned joinCondition.
    """
    if not join_condition or not join_condition.strip():
        return ""
    
    original = join_condition
    
    # Handle "AND Where" / "AND WHERE" pattern (Pattern A)
    and_where_match = re.search(r'AND\s+Where\s+', join_condition, re.IGNORECASE)
    if and_where_match:
        # Find the real filter part (after "AND Where")
        after_and_where = join_condition[and_where_match.end():].strip()
        
        # Find the FROM clause before it
        from_match = re.search(r'FROM\s+[^W]+', join_condition, re.IGNORECASE)
        if from_match:
            from_part = join_condition[from_match.start():from_match.end()].strip()
            # Reconstruct with proper spacing
            if after_and_where:
                # Remove the "AND Where" and rebuild
                result = f"{from_part}\nWHERE {after_and_where}"
                return result
    
    # Handle multiple FROM/WHERE pattern (Pattern B) with SCD2 or self-join
    from_count = len(re.findall(r'\bFROM\b', join_condition, re.IGNORECASE))
    where_count = len(re.findall(r'\bWHERE\b', join_condition, re.IGNORECASE))
    
    # Only process if there are multiple FROM or WHERE statements
    if from_count >= 2 or where_count >= 2:
        has_dss = "DSS_CURRENT_FLAG" in join_condition
        has_self_join = has_self_join_pattern(join_condition)
        
        if has_dss or has_self_join:
            # Find the FROM with ref() or the first proper FROM
            ref_from_match = re.search(r'FROM\s*{{\s*ref\s*\([^)]+\)[^W]*', join_condition, re.IGNORECASE)
            
            if ref_from_match:
                from_end = ref_from_match.end()
                # Find the last WHERE in the join condition
                remaining = join_condition[from_end:]
                where_matches = list(re.finditer(r'WHERE\s+', remaining, re.IGNORECASE))
                
                if where_matches:
                    # Use the last WHERE (the real filter)
                    real_where_match = where_matches[-1]
                    real_where_content = remaining[real_where_match.end():].strip()
                    
                    # Check if this WHERE references only the source table or non-target conditions
                    # Remove lines that are clearly self-join conditions
                    lines = real_where_content.split('\n')
                    filtered_lines = []
                    
                    for line in lines:
                        # Skip lines that look like self-join comparisons (TABLE.col = TABLE2.col)
                        if re.search(r'\b\w+\s*\.\s*\w+\s*=\s*\w+\s*\.\s*\w+', line):
                            # This is a join condition, might need to filter it
                            # Only skip if both sides reference different tables (self-join pattern)
                            left_match = re.search(r'(\w+)\s*\.\s*\w+\s*=\s*(\w+)\s*\.\s*\w+', line)
                            if left_match and left_match.group(1) != left_match.group(2):
                                continue  # Skip this line (self-join)
                        
                        if line.strip():
                            filtered_lines.append(line)
                    
                    # Clean up the WHERE content
                    real_where_content = '\n'.join(filtered_lines).strip()
                    real_where_content = re.sub(r'\s+', ' ', real_where_content)
                    
                    from_part = join_condition[:from_end].strip()
                    
                    if real_where_content:
                        result = f"{from_part}\nWHERE {real_where_content}"
                    else:
                        result = from_part
                    
                    return result
                else:
                    # No WHERE found, just return FROM clause
                    return join_condition[:from_end].strip()
    
    return original


def should_fix_node(join_condition: str) -> bool:
    """Determine if a node's joinCondition needs fixing."""
    if not join_condition:
        return False
    
    # Check for obvious problems
    has_and_where = bool(re.search(r'AND\s+Where\s+', join_condition, re.IGNORECASE))
    has_dss_flag = "DSS_CURRENT_FLAG" in join_condition
    has_self_join = has_self_join_pattern(join_condition)
    has_multiple_from = len(re.findall(r'\bFROM\b', join_condition, re.IGNORECASE)) >= 2
    has_multiple_where = len(re.findall(r'\bWHERE\b', join_condition, re.IGNORECASE)) >= 2
    
    # Don't fix if it's just a subquery with WHERE
    is_subquery = contains_subquery(join_condition)
    
    return (has_and_where or ((has_dss_flag or has_self_join) and (has_multiple_from or has_multiple_where))) and not is_subquery


def process_node_file(file_path: Path) -> Tuple[bool, str]:
    """
    Process a single node YAML file.
    Returns: (was_modified, message)
    """
    try:
        with open(file_path, 'r') as f:
            content = yaml.safe_load(f)
        
        if not content:
            return False, f"  {file_path.name}: Empty file"
        
        # Navigate to operation.metadata.sourceMapping
        operation = content.get('operation', {})
        if not operation:
            return False, None
        
        metadata = operation.get('metadata', {})
        if not metadata:
            return False, None
        
        source_mapping = metadata.get('sourceMapping', [])
        if not source_mapping:
            return False, None
        
        node_name = content.get('name', '')
        modified = False
        
        for source in source_mapping:
            if isinstance(source, dict) and 'join' in source and isinstance(source['join'], dict):
                if 'joinCondition' in source['join']:
                    join_condition = source['join']['joinCondition']
                    
                    if should_fix_node(join_condition):
                        cleaned = clean_joincondition(join_condition, node_name)
                        
                        if cleaned and cleaned != join_condition:
                            source['join']['joinCondition'] = cleaned
                            modified = True
                            print(f"  ✓ Fixed: {file_path.name}")
        
        if modified:
            # Write back
            with open(file_path, 'w') as f:
                yaml.dump(content, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
            return True, f"Modified {file_path.name}"
        
        return False, None
    
    except Exception as e:
        return False, f"  ERROR {file_path.name}: {str(e)}"


def main():
    """Main entry point."""
    print("Starting joinCondition cleanup...")
    print(f"Processing nodes in: {NODES_DIR}\n")
    
    modified_count = 0
    error_count = 0
    checked_count = 0
    
    for yml_file in sorted(NODES_DIR.glob("*.yml")):
        checked_count += 1
        was_modified, message = process_node_file(yml_file)
        
        if was_modified:
            modified_count += 1
        elif message and "ERROR" in message:
            error_count += 1
            print(message)
    
    print(f"\n{'='*60}")
    print(f"Summary:")
    print(f"  Files checked: {checked_count}")
    print(f"  Files modified: {modified_count}")
    print(f"  Errors: {error_count}")
    print(f"{'='*60}")
    
    if modified_count > 0:
        print(f"\n✓ Successfully fixed {modified_count} node(s)")
    else:
        print("\nNo nodes needed fixing")


if __name__ == "__main__":
    main()
