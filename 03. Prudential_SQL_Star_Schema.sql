/*
==========================================================================
PRUDENTIAL SINGAPORE
BUILD THE SQL STAR SCHEMA
==========================================================================
Purpose
-------
Create the governed analytical warehouse structure required by the
Prudential.

Source cleaned dataset
----------------------
prudential_sg_cleaned_imputed.csv
60 columns after cleansing / imputation.

Design principles
-----------------
1. Dimensions describe the business entities.
2. Facts store measurable business events.
3. Surrogate keys are used for warehouse joins.
4. Policy, customer, product, channel, agent and date analysis are supported.
5. Claims and assessor analytics are separated from policy transactions.
6. Agent history is designed as SCD Type 2.
7. Data-quality status is retained in the fact layer.
8. Scenario tables support Monte Carlo simulations.
9. Constraints prevent negative financial values and invalid percentages.
10. The script is SQL Server / SSMS compatible.

Warehouse output
--------------
Dimension tables
    dim_date
    dim_customer
    dim_product
    dim_channel
    dim_agent
    dim_assessor
    dim_claim_type

Fact tables
    fact_policy_new_business
    fact_claim

Scenario tables
    scenario_run
    scenario_parameter
    scenario_result

Source-to-model mapping is documented at the end of the script.
==========================================================================
*/
SET NOCOUNT ON;

SET XACT_ABORT ON;


GO
/* ========================================================================
   0. Create warehouse schema
   ======================================================================== */
IF NOT EXISTS (SELECT 1
               FROM   sys.schemas
               WHERE  name = 'prudential')
    EXECUTE ('CREATE SCHEMA prudential');


GO
/* ========================================================================
   1. DIMENSION: DATE
   ======================================================================== */
IF OBJECT_ID('prudential.dim_date', 'U') IS NULL
    BEGIN
        CREATE TABLE prudential.dim_date (
            date_key       INT          NOT NULL,
            full_date      DATE         NOT NULL,
            day_of_month   TINYINT      NOT NULL,
            day_name       VARCHAR (10) NOT NULL,
            day_of_week    TINYINT      NOT NULL,
            week_of_year   TINYINT      NOT NULL,
            month_number   TINYINT      NOT NULL,
            month_name     VARCHAR (10) NOT NULL,
            quarter_number TINYINT      NOT NULL,
            quarter_name   VARCHAR (2)  NOT NULL,
            year_number    SMALLINT     NOT NULL,
            year_month     CHAR (7)     NOT NULL,
            is_month_end   BIT          DEFAULT 0 NOT NULL,
            is_quarter_end BIT          DEFAULT 0 NOT NULL,
            is_year_end    BIT          DEFAULT 0 NOT NULL,
            CONSTRAINT PK_dim_date PRIMARY KEY (date_key),
            CONSTRAINT UQ_dim_date_full_date UNIQUE (full_date)
        );
    END


GO
/* ========================================================================
   2. DIMENSION: CUSTOMER
   ======================================================================== */
IF OBJECT_ID('prudential.dim_customer', 'U') IS NULL
    BEGIN
        CREATE TABLE prudential.dim_customer (
            customer_key      INT             IDENTITY (1, 1) NOT NULL,
            customer_id       VARCHAR (30)    NOT NULL,
            customer_name     VARCHAR (150)   NULL,
            gender            VARCHAR (20)    NULL,
            date_of_birth     DATE            NULL,
            age               TINYINT         NULL,
            marital_status    VARCHAR (30)    NULL,
            residency_status  VARCHAR (40)    NULL,
            occupation_group  VARCHAR (80)    NULL,
            annual_income_sgd DECIMAL (18, 2) NULL,
            income_band       VARCHAR (40)    NULL,
            CONSTRAINT PK_dim_customer PRIMARY KEY (customer_key),
            CONSTRAINT UQ_dim_customer_customer_id UNIQUE (customer_id),
            CONSTRAINT CK_dim_customer_age CHECK (age IS NULL
                                                  OR age BETWEEN 18 AND 100),
            CONSTRAINT CK_dim_customer_income CHECK (annual_income_sgd IS NULL
                                                     OR annual_income_sgd >= 0)
        );
    END


GO
/* ========================================================================
   3. DIMENSION: PRODUCT
   ======================================================================== */
IF OBJECT_ID('prudential.dim_product', 'U') IS NULL
    BEGIN
        CREATE TABLE prudential.dim_product (
            product_key      INT           IDENTITY (1, 1) NOT NULL,
            product_name     VARCHAR (100) NOT NULL,
            product_category VARCHAR (50)  NOT NULL,
            is_low_margin    BIT           NOT NULL,
            valid_from       DATE          DEFAULT CONVERT (DATE, GETDATE()) NOT NULL,
            valid_to         DATE          DEFAULT CONVERT (DATE, '9999-12-31') NOT NULL,
            is_current       BIT           DEFAULT 1 NOT NULL,
            CONSTRAINT PK_dim_product PRIMARY KEY (product_key),
            CONSTRAINT UQ_dim_product_name_category UNIQUE (product_name, product_category)
        );
    END


GO
/* ========================================================================
   4. DIMENSION: CHANNEL
   ======================================================================== */
IF OBJECT_ID('prudential.dim_channel', 'U') IS NULL
    BEGIN
        CREATE TABLE prudential.dim_channel (
            channel_key   INT          IDENTITY (1, 1) NOT NULL,
            channel_name  VARCHAR (50) NOT NULL,
            channel_group VARCHAR (50) NULL,
            is_agent_led  BIT          NULL,
            is_digital    BIT          NULL,
            valid_from    DATE         DEFAULT CONVERT (DATE, GETDATE()) NOT NULL,
            valid_to      DATE         DEFAULT CONVERT (DATE, '9999-12-31') NOT NULL,
            is_current    BIT          DEFAULT 1 NOT NULL,
            CONSTRAINT PK_dim_channel PRIMARY KEY (channel_key),
            CONSTRAINT UQ_dim_channel_name UNIQUE (channel_name)
        );
    END


GO
/* ========================================================================
   5. DIMENSION: AGENT - SCD TYPE 2
   Historical agent assignment is preserved so past sales remain attributed
   to the correct agency unit.
   ======================================================================== */
IF OBJECT_ID('prudential.dim_agent', 'U') IS NULL
    BEGIN
        CREATE TABLE prudential.dim_agent (
            agent_key                INT             IDENTITY (1, 1) NOT NULL,
            agent_id                 VARCHAR (30)    NOT NULL,
            agent_name               VARCHAR (150)   NULL,
            agent_tier               VARCHAR (30)    NULL,
            agent_tenure_months      INT             NULL,
            agency_unit              VARCHAR (30)    NULL,
            agent_status             VARCHAR (30)    NULL,
            agent_productivity_score DECIMAL (10, 2) NULL,
            agent_attrition_risk     DECIMAL (5, 3)  NULL,
            valid_from               DATE            NOT NULL,
            valid_to                 DATE            DEFAULT CONVERT (DATE, '9999-12-31') NOT NULL,
            is_current               BIT             DEFAULT 1 NOT NULL,
            CONSTRAINT PK_dim_agent PRIMARY KEY (agent_key),
            CONSTRAINT CK_dim_agent_tenure CHECK (agent_tenure_months IS NULL
                                                  OR agent_tenure_months >= 0),
            CONSTRAINT CK_dim_agent_productivity CHECK (agent_productivity_score IS NULL
                                                        OR agent_productivity_score >= 0),
            CONSTRAINT CK_dim_agent_attrition CHECK (agent_attrition_risk IS NULL
                                                     OR agent_attrition_risk BETWEEN 0 AND 1)
        );
    END


GO
/* Only one current version of an agent may exist. */
IF NOT EXISTS (SELECT 1
               FROM   sys.indexes
               WHERE  name = 'UX_dim_agent_current_agent'
                      AND object_id = OBJECT_ID('prudential.dim_agent'))
    BEGIN
        CREATE UNIQUE INDEX UX_dim_agent_current_agent
            ON prudential.dim_agent(agent_id) WHERE is_current = 1;
    END


GO
/* ========================================================================
   6. DIMENSION: ASSESSOR
   ======================================================================== */
IF OBJECT_ID('prudential.dim_assessor', 'U') IS NULL
    BEGIN
        CREATE TABLE prudential.dim_assessor (
            assessor_key                INT             IDENTITY (1, 1) NOT NULL,
            assessor_id                 VARCHAR (30)    NOT NULL,
            assessor_approval_limit_sgd DECIMAL (18, 2) NULL,
            valid_from                  DATE            DEFAULT CONVERT (DATE, GETDATE()) NOT NULL,
            valid_to                    DATE            DEFAULT CONVERT (DATE, '9999-12-31') NOT NULL,
            is_current                  BIT             DEFAULT 1 NOT NULL,
            CONSTRAINT PK_dim_assessor PRIMARY KEY (assessor_key),
            CONSTRAINT UQ_dim_assessor_id UNIQUE (assessor_id),
            CONSTRAINT CK_dim_assessor_limit CHECK (assessor_approval_limit_sgd IS NULL
                                                    OR assessor_approval_limit_sgd >= 0)
        );
    END


GO
/* ========================================================================
   7. DIMENSION: CLAIM TYPE
   ======================================================================== */
IF OBJECT_ID('prudential.dim_claim_type', 'U') IS NULL
    BEGIN
        CREATE TABLE prudential.dim_claim_type (
            claim_type_key   INT          IDENTITY (1, 1) NOT NULL,
            claim_type       VARCHAR (80) NOT NULL,
            claim_family     VARCHAR (80) NULL,
            is_medical       BIT          NULL,
            is_high_severity BIT          NULL,
            CONSTRAINT PK_dim_claim_type PRIMARY KEY (claim_type_key),
            CONSTRAINT UQ_dim_claim_type UNIQUE (claim_type)
        );
    END


GO
/* ========================================================================
   8. FACT: POLICY / NEW BUSINESS
   Grain: one row per policy record from the cleaned policy dataset.
   ======================================================================== */
IF OBJECT_ID('prudential.fact_policy_new_business', 'U') IS NULL
    BEGIN
        CREATE TABLE prudential.fact_policy_new_business (
            policy_key               BIGINT          IDENTITY (1, 1) NOT NULL,
            record_id                VARCHAR (30)    NULL,
            policy_id                VARCHAR (30)    NOT NULL,
            customer_key             INT             NULL,
            product_key              INT             NOT NULL,
            channel_key              INT             NOT NULL,
            agent_key                INT             NULL,
            start_date_key           INT             NOT NULL,
            quote_date               DATE            NULL,
            policy_start_date        DATE            NOT NULL,
            policy_term_years        SMALLINT        NULL,
            payment_frequency        VARCHAR (30)    NULL,
            policy_status            VARCHAR (30)    NULL,
            annual_premium_sgd       DECIMAL (18, 2) NOT NULL,
            sum_assured_sgd          DECIMAL (20, 2) NULL,
            ape_sgd                  DECIMAL (18, 2) NULL,
            nbp_margin_pct           DECIMAL (6, 4)  NULL,
            /* NBP = APE × NBP margin. Stored as a computed warehouse measure. */
            nbp_sgd                  AS              (CONVERT (DECIMAL (18, 2), ISNULL(ape_sgd, 0) * ISNULL(nbp_margin_pct, 0))) PERSISTED,
            months_inforce           INT             NULL,
            lapse_flag               BIT             NULL,
            persistency_13m          BIT             NULL,
            chronic_condition_flag   BIT             NULL,
            ip_plan_tier             VARCHAR (60)    NULL,
            rider_attached_flag      BIT             NULL,
            rider_premium_before_sgd DECIMAL (18, 2) NULL,
            rider_premium_after_sgd  DECIMAL (18, 2) NULL,
            copay_pct                DECIMAL (5, 2)  NULL,
            deductible_sgd           DECIMAL (18, 2) NULL,
            care_delay_days          INT             NULL,
            dq_row_status            VARCHAR (20)    DEFAULT 'OK' NOT NULL,
            load_ts                  DATETIME2 (0)   DEFAULT SYSDATETIME() NOT NULL,
            CONSTRAINT PK_fact_policy_new_business PRIMARY KEY (policy_key),
            CONSTRAINT UQ_fact_policy_new_business_policy_id UNIQUE (policy_id),
            CONSTRAINT FK_fpn_customer FOREIGN KEY (customer_key) REFERENCES prudential.dim_customer (customer_key),
            CONSTRAINT FK_fpn_product FOREIGN KEY (product_key) REFERENCES prudential.dim_product (product_key),
            CONSTRAINT FK_fpn_channel FOREIGN KEY (channel_key) REFERENCES prudential.dim_channel (channel_key),
            CONSTRAINT FK_fpn_agent FOREIGN KEY (agent_key) REFERENCES prudential.dim_agent (agent_key),
            CONSTRAINT FK_fpn_start_date FOREIGN KEY (start_date_key) REFERENCES prudential.dim_date (date_key),
            CONSTRAINT CK_fpn_premium CHECK (annual_premium_sgd > 0),
            CONSTRAINT CK_fpn_sum_assured CHECK (sum_assured_sgd IS NULL
                                                 OR sum_assured_sgd >= 0),
            CONSTRAINT CK_fpn_ape CHECK (ape_sgd IS NULL
                                         OR ape_sgd >= 0),
            CONSTRAINT CK_fpn_margin CHECK (nbp_margin_pct IS NULL
                                            OR nbp_margin_pct BETWEEN 0 AND 1),
            CONSTRAINT CK_fpn_copay CHECK (copay_pct IS NULL
                                           OR copay_pct BETWEEN 0 AND 100),
            CONSTRAINT CK_fpn_months CHECK (months_inforce IS NULL
                                            OR months_inforce >= 0),
            CONSTRAINT CK_fpn_care_delay CHECK (care_delay_days IS NULL
                                                OR care_delay_days >= 0),
            CONSTRAINT CK_fpn_rider_premium_before CHECK (rider_premium_before_sgd IS NULL
                                                          OR rider_premium_before_sgd >= 0),
            CONSTRAINT CK_fpn_rider_premium_after CHECK (rider_premium_after_sgd IS NULL
                                                         OR rider_premium_after_sgd >= 0),
            CONSTRAINT CK_fpn_deductible CHECK (deductible_sgd IS NULL
                                                OR deductible_sgd >= 0),
            CONSTRAINT CK_fpn_term CHECK (policy_term_years IS NULL
                                          OR policy_term_years > 0)
        );
    END


GO
/* ========================================================================
   9. FACT: CLAIMS
   Grain: one row per claim record.
   ======================================================================== */
IF OBJECT_ID('prudential.fact_claim', 'U') IS NULL
    BEGIN
        CREATE TABLE prudential.fact_claim (
            claim_key                   BIGINT          IDENTITY (1, 1) NOT NULL,
            claim_id                    VARCHAR (30)    NOT NULL,
            policy_key                  BIGINT          NOT NULL,
            claim_date_key              INT             NULL,
            claim_type_key              INT             NULL,
            assessor_key                INT             NULL,
            claim_date                  DATE            NULL,
            claim_amount_sgd            DECIMAL (18, 2) NOT NULL,
            claim_status                VARCHAR (40)    NULL,
            claim_decline_reason        VARCHAR (200)   NULL,
            claim_tat_days              INT             NULL,
            disputed_flag               BIT             NULL,
            complaint_flag              BIT             NULL,
            customer_sentiment_score    DECIMAL (6, 3)  NULL,
            nps_score                   INT             NULL,
            approval_above_limit        BIT             NULL,
            sod_breach                  BIT             NULL,
            fraud_risk_score            DECIMAL (5, 3)  NULL,
            assessor_approval_limit_sgd DECIMAL (18, 2) NULL,
            dq_row_status               VARCHAR (20)    DEFAULT 'OK' NOT NULL,
            load_ts                     DATETIME2 (0)   DEFAULT SYSDATETIME() NOT NULL,
            CONSTRAINT PK_fact_claim PRIMARY KEY (claim_key),
            CONSTRAINT UQ_fact_claim_claim_id UNIQUE (claim_id),
            CONSTRAINT FK_fc_policy FOREIGN KEY (policy_key) REFERENCES prudential.fact_policy_new_business (policy_key),
            CONSTRAINT FK_fc_date FOREIGN KEY (claim_date_key) REFERENCES prudential.dim_date (date_key),
            CONSTRAINT FK_fc_type FOREIGN KEY (claim_type_key) REFERENCES prudential.dim_claim_type (claim_type_key),
            CONSTRAINT FK_fc_assessor FOREIGN KEY (assessor_key) REFERENCES prudential.dim_assessor (assessor_key),
            CONSTRAINT CK_fc_amount CHECK (claim_amount_sgd >= 0),
            CONSTRAINT CK_fc_tat CHECK (claim_tat_days IS NULL
                                        OR claim_tat_days >= 0),
            CONSTRAINT CK_fc_sentiment CHECK (customer_sentiment_score IS NULL
                                              OR customer_sentiment_score BETWEEN -1 AND 1),
            CONSTRAINT CK_fc_nps CHECK (nps_score IS NULL
                                        OR nps_score BETWEEN -100 AND 100),
            CONSTRAINT CK_fc_fraud CHECK (fraud_risk_score IS NULL
                                          OR fraud_risk_score BETWEEN 0 AND 1),
            CONSTRAINT CK_fc_assessor_limit CHECK (assessor_approval_limit_sgd IS NULL
                                                   OR assessor_approval_limit_sgd >= 0)
        );
    END


GO
/* ========================================================================
   10. SCENARIO RUN HEADER
    Supports Monte Carlo simulation governance.
   ======================================================================== */
IF OBJECT_ID('prudential.scenario_run', 'U') IS NULL
    BEGIN
        CREATE TABLE prudential.scenario_run (
            run_id        BIGINT        IDENTITY (1, 1) NOT NULL,
            scenario_code VARCHAR (10)  NOT NULL,
            scenario_name VARCHAR (200) NOT NULL,
            run_by        VARCHAR (100) NULL,
            run_ts        DATETIME2 (0) DEFAULT SYSDATETIME() NOT NULL,
            iterations    INT           NOT NULL,
            random_seed   INT           NOT NULL,
            status        VARCHAR (20)  DEFAULT 'COMPLETED' NOT NULL,
            CONSTRAINT PK_scenario_run PRIMARY KEY (run_id),
            CONSTRAINT CK_scenario_iterations CHECK (iterations > 0)
        );
    END


GO
/* ========================================================================
   11. SCENARIO PARAMETERS
   Explicitly stores the business levers used for reproducibility.
   ======================================================================== */
IF OBJECT_ID('prudential.scenario_parameter', 'U') IS NULL
    BEGIN
        CREATE TABLE prudential.scenario_parameter (
            parameter_key   BIGINT          IDENTITY (1, 1) NOT NULL,
            run_id          BIGINT          NOT NULL,
            parameter_name  VARCHAR (100)   NOT NULL,
            parameter_value DECIMAL (20, 6) NULL,
            parameter_unit  VARCHAR (40)    NULL,
            source_note     VARCHAR (500)   NULL,
            CONSTRAINT PK_scenario_parameter PRIMARY KEY (parameter_key),
            CONSTRAINT FK_scenario_parameter_run FOREIGN KEY (run_id) REFERENCES prudential.scenario_run (run_id)
        );
    END


GO
/* ========================================================================
   12. SCENARIO RESULTS
   P05 / P50 / P95 are intentionally stored rather than a single estimate.
   ======================================================================== */
IF OBJECT_ID('prudential.scenario_result', 'U') IS NULL
    BEGIN
        CREATE TABLE prudential.scenario_result (
            result_key         BIGINT          IDENTITY (1, 1) NOT NULL,
            run_id             BIGINT          NOT NULL,
            metric_name        VARCHAR (100)   NOT NULL,
            p05                DECIMAL (20, 6) NULL,
            p50                DECIMAL (20, 6) NULL,
            p95                DECIMAL (20, 6) NULL,
            baseline           DECIMAL (20, 6) NULL,
            delta_vs_base      DECIMAL (20, 6) NULL,
            result_unit        VARCHAR (40)    NULL,
            central_impact_bps DECIMAL (20, 6) NULL,
            nbp_at_risk_sgd    DECIMAL (20, 2) NULL,
            CONSTRAINT PK_scenario_result PRIMARY KEY (result_key),
            CONSTRAINT FK_scenario_result_run FOREIGN KEY (run_id) REFERENCES prudential.scenario_run (run_id)
        );
    END


GO
/* ========================================================================
   13. PERFORMANCE INDEXES
    These align with KPI and Power BI filtering patterns.
   ======================================================================== */
IF NOT EXISTS (SELECT 1
               FROM   sys.indexes
               WHERE  name = 'IX_fact_policy_date'
                      AND object_id = OBJECT_ID('prudential.fact_policy_new_business'))
    BEGIN
        CREATE INDEX IX_fact_policy_date
            ON prudential.fact_policy_new_business(start_date_key);
    END


GO
IF NOT EXISTS (SELECT 1
               FROM   sys.indexes
               WHERE  name = 'IX_fact_policy_product'
                      AND object_id = OBJECT_ID('prudential.fact_policy_new_business'))
    BEGIN
        CREATE INDEX IX_fact_policy_product
            ON prudential.fact_policy_new_business(product_key);
    END


GO
IF NOT EXISTS (SELECT 1
               FROM   sys.indexes
               WHERE  name = 'IX_fact_policy_channel'
                      AND object_id = OBJECT_ID('prudential.fact_policy_new_business'))
    BEGIN
        CREATE INDEX IX_fact_policy_channel
            ON prudential.fact_policy_new_business(channel_key);
    END


GO
IF NOT EXISTS (SELECT 1
               FROM   sys.indexes
               WHERE  name = 'IX_fact_policy_agent'
                      AND object_id = OBJECT_ID('prudential.fact_policy_new_business'))
    BEGIN
        CREATE INDEX IX_fact_policy_agent
            ON prudential.fact_policy_new_business(agent_key);
    END


GO
IF NOT EXISTS (SELECT 1
               FROM   sys.indexes
               WHERE  name = 'IX_fact_claim_policy'
                      AND object_id = OBJECT_ID('prudential.fact_claim'))
    BEGIN
        CREATE INDEX IX_fact_claim_policy
            ON prudential.fact_claim(policy_key);
    END


GO
IF NOT EXISTS (SELECT 1
               FROM   sys.indexes
               WHERE  name = 'IX_fact_claim_date'
                      AND object_id = OBJECT_ID('prudential.fact_claim'))
    BEGIN
        CREATE INDEX IX_fact_claim_date
            ON prudential.fact_claim(claim_date_key);
    END


GO
IF NOT EXISTS (SELECT 1
               FROM   sys.indexes
               WHERE  name = 'IX_fact_claim_assessor'
                      AND object_id = OBJECT_ID('prudential.fact_claim'))
    BEGIN
        CREATE INDEX IX_fact_claim_assessor
            ON prudential.fact_claim(assessor_key);
    END


GO
IF NOT EXISTS (SELECT 1
               FROM   sys.indexes
               WHERE  name = 'IX_scenario_result_run'
                      AND object_id = OBJECT_ID('prudential.scenario_result'))
    BEGIN
        CREATE INDEX IX_scenario_result_run
            ON prudential.scenario_result(run_id);
    END


GO
/* ========================================================================
   14. OPTIONAL DEFAULT / UNKNOWN DIMENSION MEMBERS
   These prevent fact-load failures for legitimate "Not Applicable" cases.
   ======================================================================== */
IF NOT EXISTS (SELECT 1
               FROM   prudential.dim_product
               WHERE  product_key = 1)
    BEGIN
        SET IDENTITY_INSERT prudential.dim_product ON;
        INSERT  INTO prudential.dim_product (
            product_key,
            product_name,
            product_category,
            is_low_margin,
            valid_from,
            valid_to,
            is_current
        )
        VALUES                             (1, 'Unknown / Not Available', 'Unknown', 0, CONVERT (DATE, '1900-01-01'), CONVERT (DATE, '9999-12-31'), 1);
        SET IDENTITY_INSERT prudential.dim_product OFF;
    END


GO
IF NOT EXISTS (SELECT 1
               FROM   prudential.dim_channel
               WHERE  channel_key = 1)
    BEGIN
        SET IDENTITY_INSERT prudential.dim_channel ON;
        INSERT  INTO prudential.dim_channel (
            channel_key,
            channel_name,
            channel_group,
            is_agent_led,
            is_digital,
            valid_from,
            valid_to,
            is_current
        )
        VALUES                             (1, 'Unknown / Not Available', 'Unknown', 0, 0, CONVERT (DATE, '1900-01-01'), CONVERT (DATE, '9999-12-31'), 1);
        SET IDENTITY_INSERT prudential.dim_channel OFF;
    END


GO
/* ========================================================================
15. WAREHOUSE VALIDATION CHECKS
    Run after loading data as well. They prove the warehouse design is intact.
   ======================================================================== */
/* A. Objects created */
SELECT   s.name AS schema_name,
         t.name AS table_name
FROM     sys.tables AS t
         INNER JOIN
         sys.schemas AS s
         ON s.schema_id = t.schema_id
WHERE    s.name = 'prudential'
         AND t.name IN ('dim_date', 'dim_customer', 'dim_product', 'dim_channel', 'dim_agent', 'dim_assessor', 'dim_claim_type', 'fact_policy_new_business', 'fact_claim', 'scenario_run', 'scenario_parameter', 'scenario_result')
ORDER BY CASE WHEN t.name LIKE 'dim_%' THEN 1 WHEN t.name LIKE 'fact_%' THEN 2 ELSE 3 END, t.name;


GO
/* B. Foreign-key inventory */
SELECT   fk.name AS foreign_key_name,
         OBJECT_NAME(fk.parent_object_id) AS child_table,
         COL_NAME(fkc.parent_object_id, fkc.parent_column_id) AS child_column,
         OBJECT_NAME(fk.referenced_object_id) AS parent_table,
         COL_NAME(fkc.referenced_object_id, fkc.referenced_column_id) AS parent_column
FROM     sys.foreign_keys AS fk
         INNER JOIN
         sys.foreign_key_columns AS fkc
         ON fk.object_id = fkc.constraint_object_id
         INNER JOIN
         sys.schemas AS s
         ON s.schema_id = fk.schema_id
WHERE    s.name = 'prudential'
ORDER BY child_table, foreign_key_name;


GO
/* C. Key business constraints */
SELECT 'fact_policy_new_business policy_id uniqueness' AS check_name,
       COUNT(*) AS duplicate_count
FROM   (SELECT   policy_id
        FROM     prudential.fact_policy_new_business
        GROUP BY policy_id
        HAVING   COUNT(*) > 1) AS x;


GO
SELECT 'fact_claim claim_id uniqueness' AS check_name,
       COUNT(*) AS duplicate_count
FROM   (SELECT   claim_id
        FROM     prudential.fact_claim
        GROUP BY claim_id
        HAVING   COUNT(*) > 1) AS x;


GO
/* D. Orphan checks after data load */
SELECT 'Policy records with missing product dimension' AS check_name,
       COUNT(*) AS issue_count
FROM   prudential.fact_policy_new_business AS f
       LEFT OUTER JOIN
       prudential.dim_product AS d
       ON d.product_key = f.product_key
WHERE  d.product_key IS NULL;


GO
SELECT 'Policy records with missing channel dimension' AS check_name,
       COUNT(*) AS issue_count
FROM   prudential.fact_policy_new_business AS f
       LEFT OUTER JOIN
       prudential.dim_channel AS d
       ON d.channel_key = f.channel_key
WHERE  d.channel_key IS NULL;


GO
SELECT 'Claims with missing policy dimension' AS check_name,
       COUNT(*) AS issue_count
FROM   prudential.fact_claim AS c
       LEFT OUTER JOIN
       prudential.fact_policy_new_business AS p
       ON p.policy_key = c.policy_key
WHERE  p.policy_key IS NULL;


GO
/* E. Financial integrity checks */
SELECT 'Negative annual premium' AS check_name,
       COUNT(*) AS issue_count
FROM   prudential.fact_policy_new_business
WHERE  annual_premium_sgd <= 0;


GO
SELECT 'NBP margin outside 0-1' AS check_name,
       COUNT(*) AS issue_count
FROM   prudential.fact_policy_new_business
WHERE  nbp_margin_pct IS NOT NULL
       AND (nbp_margin_pct < 0
            OR nbp_margin_pct > 1);


/* ========================================================================
   16. SOURCE COLUMN -> STAR SCHEMA MAPPING
   ========================================================================

   Source CSV: prudential_sg_cleaned_imputed.csv

   CUSTOMER DIMENSION
   ------------------
   customer_id         -> dim_customer.customer_id
   customer_name       -> dim_customer.customer_name
   gender              -> dim_customer.gender
   date_of_birth       -> dim_customer.date_of_birth
   age                 -> dim_customer.age
   marital_status      -> dim_customer.marital_status
   residency_status    -> dim_customer.residency_status
   occupation_group    -> dim_customer.occupation_group
   annual_income_sgd   -> dim_customer.annual_income_sgd
   income_band         -> dim_customer.income_band

   PRODUCT DIMENSION
   -----------------
   product_name        -> dim_product.product_name
   product_category    -> dim_product.product_category
   is_low_margin       -> derived business rule

   CHANNEL DIMENSION
   -----------------
   distribution_channel -> dim_channel.channel_name

   AGENT DIMENSION
   ---------------
   agent_id                    -> dim_agent.agent_id
   agent_name                  -> dim_agent.agent_name
   agent_tier                  -> dim_agent.agent_tier
   agent_tenure_months         -> dim_agent.agent_tenure_months
   agency_unit                 -> dim_agent.agency_unit
   agent_status                -> dim_agent.agent_status
   agent_productivity_score    -> dim_agent.agent_productivity_score
   agent_attrition_risk        -> dim_agent.agent_attrition_risk

   ASSESSOR DIMENSION
   ------------------
   assessor_id                  -> dim_assessor.assessor_id
   assessor_approval_limit_sgd  -> dim_assessor.assessor_approval_limit_sgd

   CLAIM TYPE DIMENSION
   --------------------
   claim_type          -> dim_claim_type.claim_type

   POLICY FACT
   -----------
   record_id                   -> fact_policy_new_business.record_id
   policy_id                   -> fact_policy_new_business.policy_id
   quote_date                  -> fact_policy_new_business.quote_date
   policy_start_date           -> fact_policy_new_business.policy_start_date
   policy_term_years           -> fact_policy_new_business.policy_term_years
   payment_frequency           -> fact_policy_new_business.payment_frequency
   policy_status               -> fact_policy_new_business.policy_status
   annual_premium_sgd          -> fact_policy_new_business.annual_premium_sgd
   sum_assured_sgd             -> fact_policy_new_business.sum_assured_sgd
   ape_sgd                     -> fact_policy_new_business.ape_sgd
   nbp_margin_pct              -> fact_policy_new_business.nbp_margin_pct
   nbp_sgd                     -> COMPUTED: ape_sgd * nbp_margin_pct
   months_inforce              -> fact_policy_new_business.months_inforce
   lapse_flag                  -> fact_policy_new_business.lapse_flag
   persistency_13m_flag        -> fact_policy_new_business.persistency_13m
   chronic_condition_flag      -> fact_policy_new_business.chronic_condition_flag
   ip_plan_tier                -> fact_policy_new_business.ip_plan_tier
   rider_attached_flag         -> fact_policy_new_business.rider_attached_flag
   rider_premium_before_sgd    -> fact_policy_new_business.rider_premium_before_sgd
   rider_premium_after_sgd     -> fact_policy_new_business.rider_premium_after_sgd
   copay_pct                   -> fact_policy_new_business.copay_pct
   deductible_sgd              -> fact_policy_new_business.deductible_sgd
   care_delay_days             -> fact_policy_new_business.care_delay_days
   dq_row_status               -> fact_policy_new_business.dq_row_status

   CLAIM FACT
   ----------
   claim_id                    -> fact_claim.claim_id
   claim_date                  -> fact_claim.claim_date
   claim_amount_sgd            -> fact_claim.claim_amount_sgd
   claim_status                -> fact_claim.claim_status
   claim_decline_reason        -> fact_claim.claim_decline_reason
   claim_tat_days              -> fact_claim.claim_tat_days
   disputed_flag               -> fact_claim.disputed_flag
   complaint_flag              -> fact_claim.complaint_flag
   customer_sentiment_score    -> fact_claim.customer_sentiment_score
   nps_score                   -> fact_claim.nps_score
   approval_above_limit_flag   -> fact_claim.approval_above_limit
   sod_breach_flag             -> fact_claim.sod_breach
   fraud_risk_score            -> fact_claim.fraud_risk_score
   assessor_approval_limit_sgd -> fact_claim.assessor_approval_limit_sgd

   DIMENSION / FACT RELATIONSHIPS
   ------------------------------
    customer_id + policy_id relationships are resolved during the load process.
   policy_id is the business key; policy_key is the warehouse surrogate key.
   claim records link to policy through policy_key.
   Dates link through integer date_key values.

   ========================================================================
    END OF WAREHOUSE DDL
   ========================================================================
*/