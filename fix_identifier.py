#!/usr/bin/env python3
"""Fix broken IDENTIFIER() replacements."""
import re
import yaml
from pathlib import Path

NODE_DIR = Path("/Users/dougy/GIT/migration_poc/nodes")

for yml_file in NODE_DIR.glob("*.yml"):
    with open(yml_file, 'r') as f:
        data = yaml.safe_load(f)
    
    if not data:
        continue
    
    metadata = data.get('operation', {}).get('metadata', {})
    changed = False
    
    for mapping in metadata.get('sourceMapping', []):
        if 'join' not in mapping or 'joinCondition' not in mapping['join']:
            continue
        
        join_cond = mapping['join']['joinCondition']
        
        # Fix broken IDENTIFIER pattern: "IDENTIFIER"('...')  -> IDENTIFIER('...')
        if '"IDENTIFIER"' in join_cond:
            join_cond = join_cond.replace('"IDENTIFIER"', 'IDENTIFIER')
            mapping['join']['joinCondition'] = join_cond
            changed = True
    
    if changed:
        with open(yml_file, 'w') as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True, width=1000)
        print(f"Fixed {yml_file.name}")

print("Done!")
