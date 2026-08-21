-- =====================================================================
-- Source-layer DDL  (Informatica -> Coalesce migration : PILOT)
-- Mapping        : m_rp_dcustctrct_dw_cust_contract
-- Target         : DOUG_POC.NP_HS  (Coalesce BRONZE location)
-- Generated from : IDMC/IICS export (Acenda_Coalesce_IDMC_Jobs)
--
-- NOTE: Column types reconstructed from IICS mapping metadata.
--       DW_* sources carry embedded schemas (authoritative).
--       RP_DCUSTCTRCT is a parameterized source -> columns are a
--       best-effort reconstruction of what the mapping consumes.
-- =====================================================================

CREATE SCHEMA IF NOT EXISTS DOUG_POC.NP_HS;
USE SCHEMA DOUG_POC.NP_HS;

-- ---------------------------------------------------------------------
-- RP_DCUSTCTRCT  (raw staging, schema RECONSTRUCTED from mapping usage)
-- ---------------------------------------------------------------------
CREATE OR REPLACE TABLE DOUG_POC.NP_HS.RP_DCUSTCTRCT (
    CONTRACT_ID                    VARCHAR(50),  -- Join key -> DW_CONTRACT.CONTRACT_ID
    PARTY_ID                       VARCHAR(50),  -- Join key -> DW_PARTY.PARTY_ID
    PRODUCT_SYSTEM_CODE            VARCHAR(10),  -- Source system code
    CUST_CONTRACT_RLSHP_TYPE_CODE  VARCHAR(10),  -- Relationship type (business key part)
    RELATIONSHIP_STATUS_CODE       VARCHAR(10),  -- Drives END_DTTM via DECODE(...'C'/'A'...)
    EFTV_DATE                      TIMESTAMP,  -- Source delivered effective date
    END_DATE                       TIMESTAMP,  -- Relationship end date
    PROCESS_DATE                   TIMESTAMP  -- Batch process date
);

-- ---------------------------------------------------------------------
-- DW_CONTRACT  (conformed source, embedded IICS schema)
-- ---------------------------------------------------------------------
CREATE OR REPLACE TABLE DOUG_POC.NP_HS.DW_CONTRACT (
    CONTRACT_KEY          NUMBER(38,0),
    CONTRACT_ID           VARCHAR(50),
    PRODUCT_SYSTEM_CODE   VARCHAR(10),
    DISPLAY_CONTRACT_ID   VARCHAR(50),
    EDW_CREATED_TSTP      TIMESTAMP,
    EDW_UPDATED_TSTP      TIMESTAMP,
    _MRV_CREATED_DTTM     TIMESTAMP,
    _MRV_CREATED_TASK_ID  NUMBER(38,0),
    _MRV_MD5_BUSN_KEY     VARCHAR(32)
);

-- ---------------------------------------------------------------------
-- DW_PARTY  (conformed source, embedded IICS schema)
-- ---------------------------------------------------------------------
CREATE OR REPLACE TABLE DOUG_POC.NP_HS.DW_PARTY (
    PARTY_KEY             NUMBER(38,0),
    PRODUCT_SYSTEM_CODE   VARCHAR(10),
    PARTY_ID              VARCHAR(50),
    CIS_PARTYID1          NUMBER(38,0),
    EDW_LOAD_ID           NUMBER(38,0),
    TAG_ID                NUMBER(38,0),
    EDW_CREATED_TSTP      TIMESTAMP,
    EDW_UPDATED_TSTP      TIMESTAMP,
    _MRV_CREATED_DTTM     TIMESTAMP,
    _MRV_CREATED_TASK_ID  NUMBER(38,0),
    _MRV_MD5_BUSN_KEY     VARCHAR(32)
);
