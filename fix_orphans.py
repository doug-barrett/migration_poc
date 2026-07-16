#!/usr/bin/env python3
"""
Fix orphan column source mappings in Coalesce nodes.
"""
import os
import glob
import yaml
import json
from pathlib import Path
from collections import defaultdict

def load_all_nodes():
    """Load all nodes and build lookup of {node_id: {name, columns: {col_name_upper: col_counter}}}"""
    nodes = {}
    yml_files = glob.glob('/Users/dougy/GIT/migration_poc/nodes/**/*.yml', recursive=True)
    
    for yml_file in yml_files:
        try:
            with open(yml_file, 'r') as f:
                data = yaml.safe_load(f)
                if not data or 'id' not in data:
                    continue
                    
                node_id = data['id']
                node_name = data.get('name', '')
                columns = {}
                
                if 'operation' in data and 'metadata' in data['operation']:
                    for col in data['operation']['metadata'].get('columns', []):
                        col_name = col.get('name', '').upper()
                        col_ref = col.get('columnReference', {})
                        col_counter = col_ref.get('columnCounter')
                        if col_counter:
                            columns[col_name] = col_counter
                
                nodes[node_id] = {
                    'name': node_name,
                    'columns': columns,
                    'file': yml_file
                }
        except Exception as e:
            print(f"Error loading {yml_file}: {e}")
            
    return nodes

def get_node_dependencies(node_data):
    """Extract node dependencies from sourceMapping"""
    deps = {}
    if 'operation' in node_data and 'metadata' in node_data['operation']:
        for source_map in node_data['operation']['metadata'].get('sourceMapping', []):
            aliases = source_map.get('aliases', {})
            if isinstance(aliases, dict):
                for alias_name, node_id in aliases.items():
                    deps[node_id] = alias_name
    return deps

def fix_orphan_column(col, node_id, node_deps, all_nodes):
    """
    Fix an orphan column reference by finding the correct mapping.
    Returns True if fixed, False otherwise.
    """
    source_col_refs = col.get('sourceColumnReferences', [])
    
    if not source_col_refs:
        return False
        
    for source_idx, source_ref in enumerate(source_col_refs):
        col_refs = source_ref.get('columnReferences', [])
        
        if not col_refs:
            # Has no columnReferences - might be a transform, so skip
            continue
            
        for col_ref_idx, col_ref in enumerate(col_refs):
            step_counter = col_ref.get('stepCounter')
            col_counter = col_ref.get('columnCounter')
            
            # Check if this is an orphan
            if step_counter not in all_nodes:
                # Try to find the correct mapping
                col_name = col.get('name', '').upper()
                
                # Look through dependencies
                for dep_node_id in node_deps:
                    if dep_node_id in all_nodes:
                        dep_node = all_nodes[dep_node_id]
                        if col_name in dep_node['columns']:
                            # Found matching column on dependency
                            correct_col_counter = dep_node['columns'][col_name]
                            col['sourceColumnReferences'][source_idx]['columnReferences'][col_ref_idx]['stepCounter'] = dep_node_id
                            col['sourceColumnReferences'][source_idx]['columnReferences'][col_ref_idx]['columnCounter'] = correct_col_counter
                            return True
                
                # No match found - clear the reference
                col['sourceColumnReferences'][source_idx]['columnReferences'] = []
                if not col['sourceColumnReferences'][source_idx].get('transform'):
                    col['sourceColumnReferences'][source_idx]['transform'] = 'NULL'
                return True
                
            elif col_counter:
                # Check if col_counter exists on the referenced node
                ref_node = all_nodes.get(step_counter)
                if ref_node:
                    # Check if this column counter exists on the referenced node
                    if col_counter not in ref_node['columns'].values():
                        # Column counter doesn't exist - try to fix by column name
                        col_name = col.get('name', '').upper()
                        if col_name in ref_node['columns']:
                            correct_col_counter = ref_node['columns'][col_name]
                            col['sourceColumnReferences'][source_idx]['columnReferences'][col_ref_idx]['columnCounter'] = correct_col_counter
                            return True
                        else:
                            # No matching column on referenced node either
                            col['sourceColumnReferences'][source_idx]['columnReferences'] = []
                            if not col['sourceColumnReferences'][source_idx].get('transform'):
                                col['sourceColumnReferences'][source_idx]['transform'] = 'NULL'
                            return True
    
    return False

def process_all_nodes(all_nodes):
    """Process all nodes and fix orphan references"""
    fixed_count = 0
    error_count = 0
    
    for node_id, node_info in all_nodes.items():
        yml_file = node_info['file']
        
        try:
            with open(yml_file, 'r') as f:
                node_data = yaml.safe_load(f)
            
            if 'operation' not in node_data or 'metadata' not in node_data['operation']:
                continue
            
            # Get dependencies
            node_deps = set()
            for source_map in node_data['operation']['metadata'].get('sourceMapping', []):
                aliases = source_map.get('aliases', {})
                if isinstance(aliases, dict):
                    node_deps.update(aliases.values())
            
            # Fix columns
            for col in node_data['operation']['metadata'].get('columns', []):
                if fix_orphan_column(col, node_id, node_deps, all_nodes):
                    fixed_count += 1
            
            # Write back
            with open(yml_file, 'w') as f:
                yaml.dump(node_data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
                
        except Exception as e:
            print(f"Error processing {node_id}: {e}")
            error_count += 1
    
    return fixed_count, error_count

def main():
    print("Loading all nodes...")
    all_nodes = load_all_nodes()
    print(f"Loaded {len(all_nodes)} nodes")
    
    print("\nProcessing nodes and fixing orphan references...")
    fixed, errors = process_all_nodes(all_nodes)
    
    print(f"\nFixed {fixed} orphan column references")
    print(f"Encountered {errors} errors")

if __name__ == '__main__':
    main()
