# IDMC → Coalesce converter

Reusable converter that migrates an **Informatica IDMC / IICS** export package
into **Coalesce Transform** node YAML. Built for the Acenda POC
(`Acenda_Coalesce_IDMC_Jobs`), it turns all 59 exported mappings into a
complete, validating Coalesce DAG.

## What it does

For every Informatica mapping it reads two assets:

| Asset | Holds | Used for |
|-------|-------|----------|
| `*.DTEMPLATE` (`bin/@N.bin`) | the mapping **logic** — transformations, expressions, lookups, links | transforms, joins, column derivations |
| `*.MTT.zip` (`mtTask.json`) | the runtime **bindings** — concrete source/target/lookup objects + connections | table names, layers, dependencies |

and emits Coalesce V1 nodes:

- **one BRONZE `Source`** per external raw / reference / lookup table
- **one node per mapping**, layered from its target:
  - `03_dq` / `STG_*` / `*_VALIDATION` / `*_TEMP` → **SILVER `Stage`**
  - `05_dm_Currentview` / `MRVCONS_*` → **GOLD `View`**
  - conformed `DW_*_TRNX|EVENT|BALANCE|…` → **GOLD `Fact`**
  - conformed `DW_*` → **GOLD `Dimension`**
- `joinCondition` = `FROM <primary source>` + `LEFT JOIN <lookup> ON <keys>` + `WHERE <filters>`
- Expression outputs → column `transform`s, translated Informatica → Snowflake
  (`IIF`→`CASE`, `ISNULL`→`IS NULL`, `REG_MATCH`→`RLIKE`, `SYSDATE`→`CURRENT_TIMESTAMP`, …)
- deterministic UUIDs, so re-runs are stable and diffable
- downstream **type propagation** so a column adopts its upstream's precise type

Hand-built nodes already in `nodes/` are **never overwritten** — the converter
tracks its own output in `nodes/.idmc_generated.json` and only rewrites those.

## Run

```bash
cd migration/converter
# 1. generate the DAG from the export (rewrites its own nodes; never touches hand-built ones)
python3 idmc_convert.py \
  --export ~/work/POC/Acenda_Coalesce_IDMC_Jobs \
  --repo   ../.. \
  --write            # omit --write, add --summary for a dry-run report
# 2. close residual lineage gaps in the hand-built pilot nodes
#    (MUST run after step 1 — the converter overwrites its own generated nodes)
python3 complete_pilot_sources.py --repo ../..
coa validate         # from repo root -> should report: no problems found
```

Requires Python 3.10+ and `pyyaml`.

Run order matters: `idmc_convert.py` rewrites every node it owns (tracked in
`nodes/.idmc_generated.json`), so `complete_pilot_sources.py` — which patches a
column onto a generated node — must run second.

## Files

| File | Responsibility |
|------|----------------|
| `idmc_imf.py` | decode the IMF `$$ID`/`##ID` object graph; platform-type → Snowflake |
| `idmc_expr.py` | Informatica expression → Snowflake SQL translation |
| `idmc_model.py` | DTEMPLATE + MTT → migration IR (sources, targets, lookups, filters, outputs) |
| `idmc_emit.py` | IR → Coalesce V1 node YAML (layering, joins, lineage, type propagation) |
| `idmc_convert.py` | orchestrator: discover → pair by frsGuid → model → classify → emit |

## Result (Acenda export)

- 59 mappings parsed, 59/59 targets resolved
- **159 nodes generated** — 102 BRONZE sources, 12 SILVER, 45 GOLD — plus 22
  hand-built pilot nodes
- `coa validate`: **0 errors**; every converter-generated node is warning-free

## Known limitations (by design — for hand-finishing)

The IDMC export does not contain everything a warehouse does, so a few things
are captured as clearly-marked TODOs rather than guessed:

- **Parameterised source/target schemas** carry no columns in the export;
  source columns are reconstructed from mapping usage + embedded lookup schemas
  (stated in each node's description). A multi-source mapping's referenced
  ports are attributed to its *primary* source, so a raw primary can end up
  with a superset of columns.
- **In-place mappings** (read == write, e.g. DQ validations) get a raw
  `<TABLE>_RAW` BRONZE source for the read side; the written node keeps the
  base name. Downstream refs resolve to the written (validated) node.
- **Joiner join keys** ARE in the export (`joinConditions` + `joinType` on
  every Joiner). The converter traces each Joiner's Master/Detail inputs back
  to their source tables via the link graph and emits real ON clauses
  (`INNER`/`LEFT` by join type). **Union** chunk-groups (e.g. CP_DBCP1/2/3) are
  rebuilt as `UNION ALL` subqueries aliased by the representative, so joins
  onto them resolve. Source-to-target joins (SCD2 look-back) are dropped — the
  node type handles that. ~94% of joins resolve automatically (408 real ONs);
  the rest are flagged `/* MANUAL REVIEW: no Joiner key in export … */` —
  sources wired via expression-level unconnected lookups, or unused.
- **Unconnected lookups** called inside expressions (`:LKP.name(...)`) become
  `/*LKP:name(args)*/ NULL` placeholders.
- **SCD2 mechanics** (surrogate-key sequences, change-hash, effective dating)
  are represented structurally; full SCD2 is applied when a pipeline is
  hand-finished (see `GOLD-DW_CUST_CONTRACT` and its SILVER stages for the
  fully-finished exemplar).
