-- ==========================================================================
-- WhereScape UDF Functions for United Rentals Migration
-- Deploy to: DOUG_POC.TEST (or target schema)
-- ==========================================================================

-- --------------------------------------------------------------------------
-- UDF_CHAR_TO_INT: Safely converts a character string to an integer.
-- Returns NULL if the input is not a valid integer.
-- Usage: UDF_CHAR_TO_INT(varchar_value)
-- --------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION UDF_CHAR_TO_INT(P_VALUE VARCHAR)
RETURNS NUMBER(38,0)
LANGUAGE SQL
AS
$$
    CASE
        WHEN TRIM(P_VALUE) IS NULL OR TRIM(P_VALUE) = '' THEN NULL
        WHEN TRY_CAST(TRIM(P_VALUE) AS NUMBER(38,0)) IS NOT NULL THEN CAST(TRIM(P_VALUE) AS NUMBER(38,0))
        ELSE NULL
    END
$$;

-- --------------------------------------------------------------------------
-- UDF_DEC_TO_DATE: Converts a decimal date (YYYYMMDD or CYYMMDD format)
-- to a DATE. Returns NULL for invalid/zero dates.
-- Usage: UDF_DEC_TO_DATE(numeric_date)
-- --------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION UDF_DEC_TO_DATE(P_DATE_DEC NUMBER(38,0))
RETURNS DATE
LANGUAGE SQL
AS
$$
    CASE
        WHEN P_DATE_DEC IS NULL OR P_DATE_DEC = 0 THEN NULL
        WHEN P_DATE_DEC > 19000101 THEN
            TRY_TO_DATE(LPAD(P_DATE_DEC::VARCHAR, 8, '0'), 'YYYYMMDD')
        WHEN P_DATE_DEC > 1000101 THEN
            -- CYYMMDD format (century + YYMMDD): C=0 for 1900s, C=1 for 2000s
            TRY_TO_DATE(
                CAST((1900 + FLOOR(P_DATE_DEC / 10000)) AS VARCHAR) ||
                LPAD(CAST(MOD(FLOOR(P_DATE_DEC / 100), 100) AS VARCHAR), 2, '0') ||
                LPAD(CAST(MOD(P_DATE_DEC, 100) AS VARCHAR), 2, '0'),
                'YYYYMMDD'
            )
        ELSE NULL
    END
$$;

-- --------------------------------------------------------------------------
-- UDF_DEC_TO_DATE_EH: Enhanced decimal-to-date with extended error handling.
-- Same as UDF_DEC_TO_DATE but handles additional edge cases (negative values,
-- partial dates). Returns NULL for any invalid input.
-- Usage: UDF_DEC_TO_DATE_EH(numeric_date)
-- --------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION UDF_DEC_TO_DATE_EH(P_DATE_DEC NUMBER(38,0))
RETURNS DATE
LANGUAGE SQL
AS
$$
    CASE
        WHEN P_DATE_DEC IS NULL OR P_DATE_DEC <= 0 THEN NULL
        WHEN P_DATE_DEC > 19000101 THEN
            TRY_TO_DATE(LPAD(P_DATE_DEC::VARCHAR, 8, '0'), 'YYYYMMDD')
        WHEN P_DATE_DEC > 1000101 THEN
            TRY_TO_DATE(
                CAST((1900 + FLOOR(P_DATE_DEC / 10000)) AS VARCHAR) ||
                LPAD(CAST(MOD(FLOOR(P_DATE_DEC / 100), 100) AS VARCHAR), 2, '0') ||
                LPAD(CAST(MOD(P_DATE_DEC, 100) AS VARCHAR), 2, '0'),
                'YYYYMMDD'
            )
        ELSE NULL
    END
$$;

-- --------------------------------------------------------------------------
-- UDF_DEC_TO_TIME_ALPHA_EH: Converts a decimal time value (HHMMSS or HHMM)
-- to a VARCHAR time string 'HH:MI:SS'. Returns NULL for invalid input.
-- Usage: UDF_DEC_TO_TIME_ALPHA_EH(numeric_time)
-- --------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION UDF_DEC_TO_TIME_ALPHA_EH(P_TIME_DEC NUMBER(38,0))
RETURNS VARCHAR(8)
LANGUAGE SQL
AS
$$
    CASE
        WHEN P_TIME_DEC IS NULL OR P_TIME_DEC < 0 THEN NULL
        WHEN P_TIME_DEC = 0 THEN '00:00:00'
        ELSE
            LPAD(CAST(FLOOR(P_TIME_DEC / 10000) AS VARCHAR), 2, '0') || ':' ||
            LPAD(CAST(MOD(FLOOR(P_TIME_DEC / 100), 100) AS VARCHAR), 2, '0') || ':' ||
            LPAD(CAST(MOD(P_TIME_DEC, 100) AS VARCHAR), 2, '0')
    END
$$;

-- --------------------------------------------------------------------------
-- UDF_DEC_TO_TIMESTAMP: Converts decimal date + decimal time to a TIMESTAMP.
-- Date in YYYYMMDD/CYYMMDD format, time in HHMMSS format.
-- Usage: UDF_DEC_TO_TIMESTAMP(numeric_date, numeric_time)
-- --------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION UDF_DEC_TO_TIMESTAMP(P_DATE_DEC NUMBER(38,0), P_TIME_DEC NUMBER(38,0))
RETURNS TIMESTAMP_NTZ
LANGUAGE SQL
AS
$$
    CASE
        WHEN P_DATE_DEC IS NULL OR P_DATE_DEC <= 0 THEN NULL
        ELSE
            TRY_TO_TIMESTAMP_NTZ(
                CASE
                    WHEN P_DATE_DEC > 19000101 THEN LPAD(P_DATE_DEC::VARCHAR, 8, '0')
                    ELSE
                        CAST((1900 + FLOOR(P_DATE_DEC / 10000)) AS VARCHAR) ||
                        LPAD(CAST(MOD(FLOOR(P_DATE_DEC / 100), 100) AS VARCHAR), 2, '0') ||
                        LPAD(CAST(MOD(P_DATE_DEC, 100) AS VARCHAR), 2, '0')
                END || ' ' ||
                LPAD(CAST(FLOOR(COALESCE(P_TIME_DEC, 0) / 10000) AS VARCHAR), 2, '0') || ':' ||
                LPAD(CAST(MOD(FLOOR(COALESCE(P_TIME_DEC, 0) / 100), 100) AS VARCHAR), 2, '0') || ':' ||
                LPAD(CAST(MOD(COALESCE(P_TIME_DEC, 0), 100) AS VARCHAR), 2, '0'),
                'YYYYMMDD HH24:MI:SS'
            )
    END
$$;

-- --------------------------------------------------------------------------
-- UDF_GEN_CONTRACT_KEY: Generates a composite contract key from company code
-- and contract/invoice number.
-- Usage: UDF_GEN_CONTRACT_KEY(company_code, contract_number)
-- --------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION UDF_GEN_CONTRACT_KEY(P_COMPANY VARCHAR, P_CONTRACT VARCHAR)
RETURNS VARCHAR(20)
LANGUAGE SQL
AS
$$
    TRIM(P_COMPANY) || '-' || TRIM(P_CONTRACT)
$$;

-- --------------------------------------------------------------------------
-- UDF_GEN_CONTRACT_SEQ_KEY: Generates a composite key from company code,
-- contract number, and sequence number.
-- Usage: UDF_GEN_CONTRACT_SEQ_KEY(company_code, contract_number, sequence)
-- --------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION UDF_GEN_CONTRACT_SEQ_KEY(P_COMPANY VARCHAR, P_CONTRACT VARCHAR, P_SEQ NUMBER(38,0))
RETURNS VARCHAR(30)
LANGUAGE SQL
AS
$$
    TRIM(P_COMPANY) || '-' || TRIM(P_CONTRACT) || '-' || LPAD(P_SEQ::VARCHAR, 5, '0')
$$;

-- --------------------------------------------------------------------------
-- UDF_GEN_CONTRACT_LINE_KEY: Generates a composite key from type, sequence,
-- and sub-sequence for contract line items.
-- Usage: UDF_GEN_CONTRACT_LINE_KEY(type, sequence, sub_sequence)
-- --------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION UDF_GEN_CONTRACT_LINE_KEY(P_TYPE VARCHAR, P_SEQ NUMBER(38,0), P_SUBSEQ NUMBER(38,0))
RETURNS VARCHAR(30)
LANGUAGE SQL
AS
$$
    TRIM(P_TYPE) || '-' || LPAD(P_SEQ::VARCHAR, 7, '0') || '-' || LPAD(P_SUBSEQ::VARCHAR, 5, '0')
$$;

-- --------------------------------------------------------------------------
-- UDF_GEN_INVOICE_NUM: Generates an invoice number from contract ID and
-- order sequence number.
-- Usage: UDF_GEN_INVOICE_NUM(contract_id, order_seq_num)
-- --------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION UDF_GEN_INVOICE_NUM(P_CONTRACT_ID VARCHAR, P_ORDER_SEQ NUMBER(38,0))
RETURNS VARCHAR(30)
LANGUAGE SQL
AS
$$
    TRIM(P_CONTRACT_ID) || '-' || LPAD(P_ORDER_SEQ::VARCHAR, 5, '0')
$$;

-- --------------------------------------------------------------------------
-- UDF_GEN_INVOICE_ID: Generates an invoice ID from party, contract, and
-- sequence number.
-- Usage: UDF_GEN_INVOICE_ID(party_id, contract_id, contract_seq_num)
-- --------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION UDF_GEN_INVOICE_ID(P_PARTY_ID VARCHAR, P_CONTRACT_ID VARCHAR, P_SEQ_NUM VARCHAR)
RETURNS VARCHAR(50)
LANGUAGE SQL
AS
$$
    TRIM(P_PARTY_ID) || '-' || TRIM(P_CONTRACT_ID) || '-' || TRIM(P_SEQ_NUM)
$$;

-- --------------------------------------------------------------------------
-- UDF_IS_NUMERIC_10: Returns 1 if the input string is a valid 10-digit
-- numeric value, 0 otherwise. Used to validate employee IDs etc.
-- Usage: UDF_IS_NUMERIC_10(padded_string)
-- --------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION UDF_IS_NUMERIC_10(P_VALUE VARCHAR)
RETURNS NUMBER(1,0)
LANGUAGE SQL
AS
$$
    CASE
        WHEN P_VALUE IS NULL THEN 0
        WHEN LENGTH(TRIM(P_VALUE)) > 10 THEN 0
        WHEN TRY_CAST(TRIM(P_VALUE) AS NUMBER(10,0)) IS NOT NULL THEN 1
        ELSE 0
    END
$$;

-- --------------------------------------------------------------------------
-- UDF_RATE_DECISION_CODE: Determines rate decision code based on rate
-- comparisons between contract, CT (customer tier), manager, and MX rates.
-- Returns a code indicating which rate level was applied.
-- Usage: UDF_RATE_DECISION_CODE(employee, order_type, rate_used, rate_decision_ind,
--   contract_day, contract_week, contract_4week,
--   ct_day, ct_week, ct_4week,
--   mgr_day, mgr_week, mgr_4week,
--   mx_day, mx_week, mx_4week)
-- --------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION UDF_RATE_DECISION_CODE(
    P_EMPLOYEE VARCHAR,
    P_ORDER_TYPE VARCHAR,
    P_RATE_USED VARCHAR,
    P_RATE_DECISION_IND VARCHAR,
    P_CONTRACT_DAY NUMBER(18,2),
    P_CONTRACT_WEEK NUMBER(18,2),
    P_CONTRACT_4WEEK NUMBER(18,2),
    P_CT_DAY NUMBER(18,2),
    P_CT_WEEK NUMBER(18,2),
    P_CT_4WEEK NUMBER(18,2),
    P_MGR_DAY NUMBER(18,2),
    P_MGR_WEEK NUMBER(18,2),
    P_MGR_4WEEK NUMBER(18,2),
    P_MX_DAY NUMBER(18,2),
    P_MX_WEEK NUMBER(18,2),
    P_MX_4WEEK NUMBER(18,2)
)
RETURNS VARCHAR(10)
LANGUAGE SQL
AS
$$
    CASE
        WHEN P_RATE_DECISION_IND IS NULL OR P_RATE_DECISION_IND = '' THEN NULL
        WHEN P_RATE_USED = 'C' THEN 'CONTRACT'
        WHEN P_RATE_USED = 'T' THEN 'CT'
        WHEN P_RATE_USED = 'M' THEN 'MANAGER'
        WHEN P_RATE_USED = 'X' THEN 'MAX'
        ELSE P_RATE_USED
    END
$$;

-- --------------------------------------------------------------------------
-- UDF_RATE_DECISION_TYPE_CODE: Determines the rate decision type based on
-- rate comparisons. Returns a type code (e.g., 'ABOVE', 'BELOW', 'AT').
-- Same parameter signature as UDF_RATE_DECISION_CODE.
-- --------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION UDF_RATE_DECISION_TYPE_CODE(
    P_EMPLOYEE VARCHAR,
    P_ORDER_TYPE VARCHAR,
    P_RATE_USED VARCHAR,
    P_CONTRACT_DAY NUMBER(18,2),
    P_CONTRACT_WEEK NUMBER(18,2),
    P_CONTRACT_4WEEK NUMBER(18,2),
    P_CT_DAY NUMBER(18,2),
    P_CT_WEEK NUMBER(18,2),
    P_CT_4WEEK NUMBER(18,2),
    P_MGR_DAY NUMBER(18,2),
    P_MGR_WEEK NUMBER(18,2),
    P_MGR_4WEEK NUMBER(18,2),
    P_MX_DAY NUMBER(18,2),
    P_MX_WEEK NUMBER(18,2),
    P_MX_4WEEK NUMBER(18,2)
)
RETURNS VARCHAR(10)
LANGUAGE SQL
AS
$$
    CASE
        WHEN P_CONTRACT_DAY > P_MX_DAY OR P_CONTRACT_WEEK > P_MX_WEEK
             OR P_CONTRACT_4WEEK > P_MX_4WEEK THEN 'ABOVE'
        WHEN P_CONTRACT_DAY < P_CT_DAY OR P_CONTRACT_WEEK < P_CT_WEEK
             OR P_CONTRACT_4WEEK < P_CT_4WEEK THEN 'BELOW'
        ELSE 'AT'
    END
$$;
