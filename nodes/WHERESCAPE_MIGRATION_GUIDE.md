# WhereScape RED to Coalesce Migration Guide

Lessons learned from the Omnia Partners migration POC.

## Input Files

A WhereScape RED deployment application consists of:

| File | Purpose |
|------|---------|
| `app_obj_*.wst` | Object registry — semicolon-delimited: `type;id;name` |
| `app_data_*.wst` | Database code/metadata — UTF-16LE encoded SQL INSERT statements |
| `app_con_*.wst` | Connection definitions (map to Coalesce storage locations) |
| `*.csv` (Metabase export) | Pre-parsed lineage with columns, sources, transforms, JOINs |

The CSV export from Metabase is the richest data source for migration but has naming issues (see below).

## Critical Issue: Name Mangling in CSV Export

The Metabase CSV export converts all names to `UPPER_SNAKE_CASE`:
- `ReportingPeriods` → `REPORTING_PERIODS`
- `LoadReportingPeriods` → `LOAD_REPORTING_PERIODS`
- `ProductId` → `PRODUCT_ID`
- `AccountGeography` → `ACCOUNT_GEOGRAPHY`

**You MUST resolve names back to originals** using the `app_obj_*.wst` file (for object names) and `app_data_*.wst` file (for column names). Use normalized matching (strip underscores + uppercase compare) to map CSV names back to their originals.

## Object Type Mapping

| WS Type | Code | Coalesce sqlType | Coalesce Node Type |
|---------|------|------------------|-------------------|
| Load Table | 8 | `Source` / `sourceInput` | Source |
| Stage Table | 7 | `Stage` | Stage |
| Dimension | 6 | `Dimension` | Dimension |
| Dimension View | 12 | `View` | View (materialized as view) |
| Fact Table | 5 | `Fact` | Fact |
| Data Store/View | 18 | `View` | View |
| Aggregate | 9 | `"390"` (node type ID) | Aggregate (copy of Fact with GROUP BY ALL) |
| Type 26 (Ds*) | 26 | `Source` | Source stub |

**Important:** Aggregate node type uses ID `"390"` not the name `"Aggregate"` for `sqlType`.

## Aggregate Tables

- Use a custom node type (copy of Fact) with `GROUP BY ALL` appended to the joinCondition
- Do NOT add system columns (SYSTEM_CREATE_DATE, SYSTEM_UPDATE_DATE) to aggregates
- Transforms like `SUM()`, `COUNT()`, `MAX()`, `AVG()` come from the CSV `col_trans` field

## Column Transforms vs Source Mapping

**Critical rule:** If a column has a transform, its `columnReferences` must be EMPTY (otherwise Coalesce shows "Unknown Source").

```yaml
# Column WITH transform — columnReferences must be empty
sourceColumnReferences:
  - columnReferences: []
    transform: SUM(SALES)

# Column WITHOUT transform — columnReferences points to source
sourceColumnReferences:
  - columnReferences:
      - columnCounter: <source-col-uuid>
        stepCounter: <source-node-uuid>
    transform: ""
```

## Dimension Key Lookups in Stage AddKeys Tables

Stage tables named `*_AddKeys` perform surrogate key lookups from dimensions. Pattern:

1. FROM the prior stage table (e.g., `StageAccount_BizXForms`)
2. LEFT JOIN each dimension to look up its surrogate key
3. Column transform: `COALESCE("<DimTable>"."<KeyCol>", 0)` for unmatched rows

Only `_AddKeys` stages should get implied LEFT JOINs from column sources. Other stages should NOT have implied JOINs without explicit ON conditions.

## Fact Tables

Fact tables read ALL columns (including dimension keys) from their staging table. The CSV metadata misleadingly shows dimension tables as column sources for key columns, but the actual `obj_join` shows `FROM <StagingTable>`.

**Rule:** For Fact tables, redirect dimension key columns (`*Key`) to the FROM staging table. Do NOT add dimension tables as dependencies or JOINs.

## View FROM/WHERE Patterns

Views in WhereScape store their FROM/WHERE in `vt_view_where` (ws_view_tab) which appears in the CSV `obj_join` field. Three patterns:

1. **FROM with JOINs** — Full `FROM ... JOIN ... ON ...` clause
2. **WHERE only** — Just `WHERE <condition>`. The FROM is implied from column sources (single source table).
3. **Empty** — No join info. These are role-playing dimensions or views over external sources.

For WHERE-only views, derive the FROM from the primary column source table, then append the WHERE clause.

## Handling FROM/WHERE Combined

When `obj_join` starts with `FROM` but also contains `WHERE`:
1. Split the WHERE clause from the FROM/JOIN text BEFORE parsing
2. Parse the FROM/JOIN portion
3. Append the WHERE clause to the resulting joinCondition

Use `rfind('\nWHERE ')` or `rfind(' WHERE ')` to find the WHERE boundary.

## Fully Qualified Table References

Some views use fully-qualified names: `DATABASE.SCHEMA."TABLE" AS ALIAS`

Normalize by stripping:
1. `[TABLEOWNER].[X]` → `X`
2. `DATABASE.SCHEMA."TABLE"` → `TABLE`
3. `SCHEMA.TABLE` → `TABLE` (for known schemas: STAGE, LOAD, DIM, FACT, ODS, OPC, REPORTS, OP, SQLBIT)
4. `"TABLE"` → `TABLE` (remove remaining quotes)

## JOIN Keyword Handling

The regex split for JOIN keywords must handle:
- `LEFT JOIN`, `LEFT OUTER JOIN`
- `RIGHT JOIN`, `RIGHT OUTER JOIN`
- `INNER JOIN`
- `FULL JOIN`, `FULL OUTER JOIN`
- `CROSS JOIN`
- Bare `JOIN` (= INNER JOIN)

The `AS` keyword in aliases must be handled: `TABLE AS ALIAS` vs `TABLE ALIAS`.

## Circular Dependencies (ref_no_link)

Run cycle detection after generation. Common cycles in star schemas:
- Stage tables that look up keys from dimensions that are built FROM those stages
- Example: `StageAccountHierarchy` JOINs `Account` dimension, but `Account` is built FROM `StageAccount_AddKeys` which is downstream of `StageAccountHierarchy`

Fix by converting the back-edge to `ref_no_link`:
1. Change `ref('LOC', 'NODE')` to `ref_no_link('LOC', 'NODE')` in joinCondition
2. Move from `dependencies[]` to `noLinkRefs[]`
3. Remove from `aliases{}`
4. Clear column-level `sourceColumnReferences` pointing to the cycle target (stepCounter)

## Duplicate Column IDs

The CSV export may contain duplicate rows for the same column (from multiple source mappings). Deduplicate by column name (case-insensitive) — keep only the first occurrence per object.

## Modified vs Generated Scripts

Detection signals in `ws_scr_header`:
- `sh_modified` field: non-null/non-`1900-01-01` date means hand-edited
- Template identification: `#-- Template :` comment in script content
- No template = truly custom code

For modified scripts (`sh_modified` set), the actual SQL in `ws_scr_line` should be used for transforms and joins rather than the column/table metadata.

## System/Audit Columns

Auto-assign transforms for unmapped WhereScape system columns:

| Column | Transform |
|--------|-----------|
| DSSCreateTime / DSSCREATE_TIME | `CAST(CURRENT_TIMESTAMP AS TIMESTAMP)` |
| DSSUpdateTime / DSSUPDATE_TIME | `CAST(CURRENT_TIMESTAMP AS TIMESTAMP)` |
| DSSStartDate | `CAST(CURRENT_TIMESTAMP AS TIMESTAMP)` |
| DSSEndDate | `CAST('2999-12-31 00:00:00' AS TIMESTAMP)` |
| DSSCurrentFlag | `'Y'` |
| DSSVersion | `1` |
| DSSCount | `1` |

## Coalesce Node YAML Structure

### File naming
```
<LOCATION>-<NodeName>.yml
```
Example: `STAGE-StageAccount_AddKeys.yml`, `DIM-Account.yml`, `FACT-SalesAndRevenue.yml`

### UUID Strategy
Deterministic UUIDs using `uuid5` with a fixed namespace:
- Node ID: `uuid5(NS, node_name)` — uses ORIGINAL case name
- Column ID: `uuid5(NS, f"{node_name}.{COLUMN_NAME_UPPER}")` — column uppercased for UUID consistency

### Source node (sourceInput)
```yaml
fileVersion: 1
id: <uuid>
name: <OriginalName>
operation:
  database: ""
  deployEnabled: true
  description: ""
  locationName: LOAD
  metadata:
    columns: [...]
  name: <OriginalName>
  schema: ""
  sqlType: Source
  type: sourceInput
  version: 1
type: Node
```

### SQL node (Stage/Dimension/Fact/View/Aggregate)
```yaml
fileVersion: 1
id: <uuid>
name: <OriginalName>
operation:
  config:
    postSQL: ""
    preSQL: ""
    testsEnabled: true
    # Stage only:
    insertStrategy: INSERT
    truncateBefore: true
    # Dimension/PersistentStage only:
    businessKeyColumns: [col1, col2]
  database: ""
  deployEnabled: true
  description: ""
  isMultisource: false
  locationName: <LOCATION>
  materializationType: table  # or "view" for View types
  metadata:
    appliedNodeTests: []
    columns: [...]
    cteString: ""
    enabledColumnTestIDs: []
    sourceMapping:
      - aliases:
          <SourceTable>: <source-node-uuid>
        customSQL:
          customSQL: ""
        dependencies:
          - locationName: <LOC>
            nodeName: <SourceTable>
        join:
          joinCondition: |
            FROM {{ ref('<LOC>', '<SourceTable>') }} "<ALIAS>"
            LEFT JOIN {{ ref('<LOC2>', '<Table2>') }} "<ALIAS2>"
              ON <condition>
        name: <NodeName>
        noLinkRefs: []
  name: <OriginalName>
  overrideSQL: false
  schema: ""
  sqlType: <Stage|Dimension|Fact|View|persistentStage|"390">
  type: sql
  version: 1
type: Node
```

## Output Directory Structure

```
migration_poc/
├── data.yml              # defaultStorageMapping
├── locations.yml         # List of locations
├── workspace.yml         # Location-to-database/schema mapping
├── nodeTypes/            # Node type definitions
│   ├── Aggregate-390/
│   ├── Dimension-Dimension/
│   ├── DimensionView-DimensionView/
│   ├── Fact-Fact/
│   ├── PersistentStage-persistentStage/
│   ├── Stage-Stage/
│   └── View-View/
├── nodes/                # Generated node YAML files
│   └── <LOCATION>-<NodeName>.yml
└── scripts/
    └── parse_<client>.py # The parser script
```

## Storage Location Mapping

Map WhereScape target locations (from `app_con_*.wst`) to Coalesce locations:

```yaml
# workspace.yml
locations:
  LOAD:
    database: <DB>
    schema: LOAD
  STAGE:
    database: <DB>
    schema: STAGE
  DIM:
    database: <DB>
    schema: DIM
  FACT:
    database: <DB>
    schema: FACT
  # ... etc
```

## Validation Checklist

After generation:
1. Zero duplicate column IDs within any node
2. Zero circular dependencies (run cycle detection)
3. All non-source nodes have a FROM clause in joinCondition
4. All JOINs have ON conditions (except CROSS JOIN and implied AddKeys lookups)
5. No `"AS"` appearing as a table alias
6. Column transforms have empty columnReferences
7. Column source mappings (no transform) have populated columnReferences
8. Node names, column names, and table references use original WhereScape case
9. Fact dimension key columns point to their staging table, not dimensions
10. Run `coa validate` for final structural check
