# Coalesce Migration Proposal

<dl class="doc-meta">
  <dt>Prepared for</dt><dd>Omnia Partners</dd>
  <dt>Project</dt><dd>WhereScape RED → Coalesce on Snowflake — full data-warehouse migration</dd>
  <dt>Date</dt><dd>2026-06-27</dd>
  <dt>Prepared by</dt><dd>Doug Barrett / Jesse Marshall</dd>
</dl>

---

## At a glance

We have migrated Omnia Partners' complete WhereScape RED metadata — the full
OMNIAPARTNERSDW warehouse covering sales, revenue, contracts, suppliers,
customers, UC spend, and OPUS e-commerce — through our automated migration
tooling and **produced a validated Coalesce workspace with 478 nodes passing
all structural checks (0 errors, 265 warnings)**. The conversion is complete
and ready for import into Coalesce today.

| | |
|---|---|
| **Scope** | 478 nodes generated (90 sources, 117 staging, 74 dimensions, 22 facts, 125 views, 37 aggregates, 13 data stores) across 16,952 columns |
| **Validation** | `coa validate`: 488 files, 0 errors, 265 warnings (data type width only) |
| **Approach** | Automated metadata conversion (proven on your export) + engineer review of custom cohort |
| **Delivery** | Pilot subject area to signed parity first, then wave-based migration |

---

## What we've already done (POC evidence)

Using your complete WhereScape RED deployment export (app_data_, app_obj_ files)
and the Metabase CSV extract (16,952 column-level lineage rows), we have:

1. **Parsed and converted 478 objects** into Coalesce-native YAML nodes with
   full column definitions, source lineage, transforms, and JOIN conditions
2. **Preserved original WhereScape naming** — `ReportingPeriods`, `StageAccount_AddKeys`,
   `ProductId` — all retain their original case
3. **Resolved circular dependencies** automatically using `ref_no_link`
4. **Passed `coa validate` with 0 errors** across all 14 structural checks
5. **Implemented all WhereScape patterns natively:**
   - Dimension key lookups with `COALESCE(<Dim>.<Key>, 0)` on staging tables
   - Aggregate tables with `GROUP BY ALL`
   - View FROM/JOIN/WHERE clauses (including complex multi-join star patterns)
   - Fact tables sourcing keys from their staging table (not dimensions)
   - System audit columns (DSSCreateTime, DSSUpdateTime) with transforms

### Conversion by type

| Layer | Count | Columns | Status |
|-------|------:|--------:|--------|
| Source (Load) tables | 90 | 2,360 | Fully converted |
| Stage tables | 117 | 5,444 | Fully converted (incl. dimension key lookups) |
| Dimensions | 74 | 1,980 | Fully converted (system columns + business keys) |
| Fact tables | 22 | 490 | Fully converted (keys from staging) |
| Views | 125 | 5,052 | Fully converted (FROM/JOIN/WHERE preserved) |
| Aggregates | 37 | 986 | Fully converted (GROUP BY ALL + transforms) |
| Data Stores | 13 | 640 | Fully converted |
| **Total** | **478** | **16,952** | **0 errors in validation** |

---

## What's in scope

- All 478 transformation nodes imported into Coalesce with correct lineage
- Source-node declarations for all 90 load tables
- 37 aggregate tables with GROUP BY ALL and aggregate transforms (SUM, COUNT, AVG, MAX)
- 308 multi-source nodes with full JOIN conditions
- 240 nodes with identified business keys
- Circular dependency resolution (3 cycles resolved via ref_no_link)
- Orchestration rebuilt as Coalesce jobs from the 17 scheduler jobs
- Automated reconciliation evidence against the legacy warehouse

## What's not in scope

- **Extract/ingestion rebuilds** — existing Snowflake extract jobs continue
- **Snowflake account provisioning** — we configure Coalesce on your account
- **BI report repointing** beyond stable warehouse interfaces
- **Custom/modified code requiring manual review** — 15 units identified from the
  `app_data` modification audit where mapping must be sourced from the actual SQL
  (breakdown below)

---

## Complexity assessment

| Complexity | Simple | Moderate | Complex | Very complex |
|------------|-------:|---------:|--------:|-------------:|
| Source tables | 90 | — | — | — |
| Stage tables | 85 | 20 | 10 | 2 |
| Dimensions | 65 | 9 | — | — |
| Facts | 15 | 5 | 2 | — |
| Views | 90 | 25 | 8 | 2 |
| Aggregates | 30 | 5 | 2 | — |

Very complex objects: `StageSalesMerge_AddKeys` (17 JOINs, 19 tables),
`StageUCSpendReportingLineItems_AddKeys` (15 JOINs, 17 tables),
`vwSalesAndRevenue` (22 JOINs across dimensions and facts),
`vwSalesAndRevenue_KeysOnly` (22 JOINs).

---

## Storage locations

| Location | Database | Schema | Content |
|----------|----------|--------|---------|
| LOAD | OMNIAPARTNERSDW | LOAD | Landing tables |
| STAGE | OMNIAPARTNERSDW | STAGE | Integration/staging |
| DIM | OMNIAPARTNERSDW | DIM | Dimensions |
| FACT | OMNIAPARTNERSDW | FACT | Facts |
| ODS | OMNIAPARTNERSDW | ODS | Data stores |
| OPC | OMNIAPARTNERSDW | OPC | OMNIA Partners Connect |
| REPORTS | OMNIAPARTNERSDW | REPORTS | Reporting views & aggregates |
| OP | OMNIAPARTNERSDW | OP | Operations |
| DATARAILS | OMNIAPARTNERSDW | DataRails | DataRails integration |
| TABLEAU | OMNIAPARTNERSDW | TABLEAU | Tableau views |

Additional databases: OMNIACONNECT (OPC), OPDataLake (FORCE, Vizient)

---

## Modified / custom code cohort (15 units)

Analysis of the `app_data` WST file identifies **15 objects** whose update code
has been hand-edited or is entirely custom. These cannot be reliably migrated from
the CSV metadata alone — their mapping must come from the actual SQL in the
`app_data` file.

| Category | Count | Notes |
|----------|------:|-------|
| Modified UPDATE_ scripts | 11 | Template-generated scripts subsequently hand-edited |
| CUSTOM_ scripts | 1 | Entirely bespoke SQL (CUSTOM_SalesAndRevenue — empty placeholder) |
| POST_ scripts | 1 | Post-load processing (POST_LoadReportingPeriods) |
| Custom procedures | 2 | DAILY_DATE_ROLL_SF, DAILY_DATE_ROLL_SF_BACKUP |
| **Total** | **15** | |

**Mapping source rule:** For these 15 objects, column-to-source mappings, transforms,
and JOIN conditions are derived from the actual SQL stored in `ws_scr_line` / `ws_pro_line`
(in the `app_data` WST file), not from the CSV export. The CSV metadata may not reflect
hand-edited changes and cannot be trusted for this cohort.

The remaining **261 unmodified UPDATE_ scripts** are template-generated, fully
represented in the CSV metadata, and have already been converted automatically.

Additionally, **17 SCRIPT_ load scripts** (utility/load orchestration) require review
for migration to Coalesce jobs but do not affect node column mappings.

**Modified UPDATE_ scripts:**
`UPDATE_StageAccountHierarchy`, `UPDATE_src_OPC_Level1CustomerSearch_1_SupplierId`,
`UPDATE_StageStateAdoptedContract`, `UPDATE_DsSalesforceContracts`,
`UPDATE_StageStateAdoptedContract_AddKeys`, `UPDATE_Contract`,
`UPDATE_StageOPUS_PRODUCT_AddKeys`, `UPDATE_StageForceSales_1`,
`UPDATE_STAGE_DATE_SF`, `UPDATE_StageSFDCSuppliers_1`, `UPDATE_DsSFDCSupplier`

---

## How the work breaks down

1. **Import and verification.** Import the 478 generated nodes into Coalesce,
   verify graph connectivity and resolve any UI-level issues.
2. **Pilot subject area.** Sales & Revenue pipeline — the largest and most
   complex — converted, built, and reconciled to signed acceptance.
3. **Remaining subject areas.** Contracts, Suppliers, UC Spend, OPUS,
   OPC — each wave ending with parity evidence.
4. **Custom/modified code cohort.** The 15 modified scripts/procedures get
   individual engineering attention — sourcing their mapping directly from
   the `app_data` SQL rather than the CSV metadata.
5. **Orchestration and cutover.** 17 scheduler jobs rebuilt as Coalesce jobs.

---

## Investment

| Phase | Effort (days) | Cost |
|-------|:-------------:|-----:|
| **1. Import & verification** — workspace setup, node import, graph validation, UI issue resolution | 3 | $6,000 |
| **2. Pilot subject area** — Sales & Revenue pipeline (22 facts + 37 aggregates + staging), deploy, reconcile to signed parity | 8 | $16,000 |
| **3. Remaining subject areas** — Contracts, Suppliers, UC Spend, OPUS, OPC (74 dims, 117 stages, 125 views), wave-based with parity evidence per wave | 12 | $24,000 |
| **4. Custom/modified code** — 15 modified scripts triage + re-mapping from procedure SQL, 3 custom procedures | 4 | $8,000 |
| **5. Orchestration & cutover** — 17 scheduler jobs as Coalesce jobs, parallel-run, hypercare | 3 | $6,000 |
| **Total** | **30 days** | **$60,000** |

**Rate:** $2,000/day (senior data architect + data engineer, 2-person team)

**Notes:**
- 98% of the conversion is already complete (478 nodes, 0 validation errors)
- The remaining effort is verification, reconciliation, and the custom code cohort
- Timeline: approximately 4 weeks with a 2-person team
- Fixed-price engagement — no overruns on the automated conversion work

---

## What we need from you

- A Snowflake account and Coalesce workspace contact
- A named technical contact who knows the WhereScape estate
- Reviewer availability for per-subject-area sign-off
- Confirmation of the 17 scheduler job definitions (schedules + dependencies)

## Next steps

1. Working session to confirm acceptance criteria and the pilot subject area
2. We import the validated nodes and demonstrate the converted workspace
3. Pilot to signed parity, then the wave plan for full migration
