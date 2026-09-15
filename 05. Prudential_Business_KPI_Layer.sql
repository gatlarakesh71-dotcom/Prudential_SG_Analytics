/*==========================================================================
  PRUDENTIAL SINGAPORE ANALYTICS PROJECT
  BUSINESS KPI LAYER

  PURPOSE
  -------
  Create one governed KPI layer for Prudential Singapore so that Power BI,
  analysts and downstream analytics consume the same business definitions.

  DEPENDS ON THE PRUDENTIAL WAREHOUSE TABLES
  ----------------------------
  prudential.dim_date
  prudential.dim_customer
  prudential.dim_product
  prudential.dim_channel
  prudential.dim_agent
  prudential.dim_assessor
  prudential.dim_claim_type
  prudential.fact_policy_new_business
  prudential.fact_claim

  KPI VIEWS CREATED
  -----------------
  1. vw_nbp_margin
  2. vw_health_gap
  3. vw_ip_rider_repricing
  4. vw_claims_trust
  5. vw_assessor_alerts
  6. vw_agent_attrition
  7. vw_executive_kpis

  IMPORTANT
  ---------
  - Only rows with dq_row_status = 'OK' feed headline policy KPIs.
  - Claims are aggregated independently before joining to policy metrics so
    policies with multiple claims do not double-count APE.
  - SQL Server syntax is used throughout.
  - This script creates KPI views only; it does not load or modify fact data.
==========================================================================*/
SET NOCOUNT ON;


GO
/*==========================================================================
    0. SAFETY CHECKS - FAIL EARLY IF WAREHOUSE OBJECTS ARE MISSING
==========================================================================*/
  IF OBJECT_ID('prudential.fact_policy_new_business', 'U') IS NULL
    THROW 50001, 'Missing prudential.fact_policy_new_business. Run the warehouse DDL and loader first.', 1;

  IF OBJECT_ID('prudential.fact_claim', 'U') IS NULL
    THROW 50002, 'Missing prudential.fact_claim. Run the warehouse DDL and loader first.', 1;

  IF OBJECT_ID('prudential.dim_product', 'U') IS NULL
    THROW 50003, 'Missing prudential.dim_product. Run the warehouse DDL and loader first.', 1;

  IF OBJECT_ID('prudential.dim_channel', 'U') IS NULL
    THROW 50004, 'Missing prudential.dim_channel. Run the warehouse DDL and loader first.', 1;

  IF OBJECT_ID('prudential.dim_agent', 'U') IS NULL
    THROW 50005, 'Missing prudential.dim_agent. Run the warehouse DDL and loader first.', 1;

  IF OBJECT_ID('prudential.dim_assessor', 'U') IS NULL
    THROW 50006, 'Missing prudential.dim_assessor. Run the warehouse DDL and loader first.', 1;

  IF OBJECT_ID('prudential.dim_claim_type', 'U') IS NULL
    THROW 50007, 'Missing prudential.dim_claim_type. Run the warehouse DDL and loader first.', 1;

  IF OBJECT_ID('prudential.dim_date', 'U') IS NULL
    THROW 50008, 'Missing prudential.dim_date. Run the warehouse DDL and loader first.', 1;


GO
/*==========================================================================
  1. BO-1 - NEW BUSINESS MARGIN BY PRODUCT / CHANNEL / MONTH

  Business question:
  Are rising Savings & Wealth / Investment-Linked volumes compressing NBP?
==========================================================================*/
CREATE OR ALTER VIEW prudential.vw_nbp_margin
AS
SELECT   d.year_month,
         p.product_category,
         p.product_name,
         c.channel_name,
         COUNT_BIG(*) AS policy_count,
         SUM(f.ape_sgd) AS ape_sgd,
         SUM(f.nbp_sgd) AS nbp_sgd,
         CAST (SUM(f.nbp_sgd) / NULLIF (SUM(f.ape_sgd), 0) AS DECIMAL (18, 6)) AS blended_margin,
         CAST (SUM(CASE WHEN p.is_low_margin = 1 THEN f.ape_sgd ELSE 0 END) / NULLIF (SUM(f.ape_sgd), 0) AS DECIMAL (18, 6)) AS low_margin_mix,
         CAST (SUM(CASE WHEN f.lapse_flag = 1 THEN 1 ELSE 0 END) * 1.0 / NULLIF (COUNT_BIG(*), 0) AS DECIMAL (18, 6)) AS lapse_rate,
         CAST (SUM(CASE WHEN f.persistency_13m = 0 THEN 1 ELSE 0 END) * 1.0 / NULLIF (COUNT_BIG(*), 0) AS DECIMAL (18, 6)) AS persistency_failure_rate
FROM     prudential.fact_policy_new_business AS f
         INNER JOIN
         prudential.dim_date AS d
         ON d.date_key = f.start_date_key
         INNER JOIN
         prudential.dim_product AS p
         ON p.product_key = f.product_key
         INNER JOIN
         prudential.dim_channel AS c
         ON c.channel_key = f.channel_key
WHERE    f.dq_row_status = 'OK'
GROUP BY d.year_month, p.product_category, p.product_name, c.channel_name;


GO
/*==========================================================================
  2. BO-2 - HEALTH GAP

  Business question:
  Does longer care delay correspond to higher claim severity / loss ratio?

  Design:
  - Policy exposure is calculated once per policy band.
  - Claim severity is calculated separately.
  - This avoids multiplying APE when one policy has multiple claims.
==========================================================================*/
CREATE OR ALTER VIEW prudential.vw_health_gap
AS
WITH   policy_band
AS     (SELECT f.policy_key,
               CASE WHEN ISNULL(f.care_delay_days, 0) = 0 THEN '0 - no delay' WHEN f.care_delay_days <= 30 THEN '1 - up to 1 month' WHEN f.care_delay_days <= 90 THEN '2 - 1 to 3 months' WHEN f.care_delay_days <= 180 THEN '3 - 3 to 6 months' ELSE '4 - over 6 months' END AS delay_band,
               CASE WHEN ISNULL(f.care_delay_days, 0) = 0 THEN 0 WHEN f.care_delay_days <= 30 THEN 1 WHEN f.care_delay_days <= 90 THEN 2 WHEN f.care_delay_days <= 180 THEN 3 ELSE 4 END AS delay_sort,
               f.ape_sgd
        FROM   prudential.fact_policy_new_business AS f
        WHERE  f.dq_row_status = 'OK'),
       policy_exposure
AS     (SELECT   delay_band,
                 delay_sort,
                 COUNT_BIG(*) AS policies,
                 SUM(ape_sgd) AS ape_sgd
        FROM     policy_band
        GROUP BY delay_band, delay_sort),
       claim_detail
AS     (SELECT pb.delay_band,
               pb.delay_sort,
               cl.claim_key,
               cl.claim_amount_sgd,
               cl.claim_tat_days,
               cl.claim_status
        FROM   policy_band AS pb
               INNER JOIN
               prudential.fact_claim AS cl
               ON cl.policy_key = pb.policy_key
         WHERE  cl.dq_row_status = 'OK'
           AND cl.claim_amount_sgd IS NOT NULL),
       claim_window
AS     (SELECT delay_band,
               delay_sort,
               claim_key,
               claim_amount_sgd,
               claim_tat_days,
               claim_status,
               PERCENTILE_CONT(0.90) WITHIN GROUP (ORDER BY claim_amount_sgd) OVER (PARTITION BY delay_band) AS p90_severity
        FROM   claim_detail),
       claim_summary
AS     (SELECT   delay_band,
                 delay_sort,
                 COUNT_BIG(*) AS claims,
                 AVG(claim_amount_sgd) AS avg_severity,
                 MAX(p90_severity) AS p90_severity,
                 SUM(claim_amount_sgd) AS claim_cost_sgd,
                 AVG(CAST (claim_tat_days AS DECIMAL (18, 4))) AS avg_claim_tat_days,
                 SUM(CASE WHEN claim_status = 'Declined' THEN 1 ELSE 0 END) AS declined_claims
        FROM     claim_window
        GROUP BY delay_band, delay_sort)
SELECT pe.delay_band,
       pe.policies,
       pe.ape_sgd,
       ISNULL(cs.claims, 0) AS claims,
       cs.avg_severity,
       cs.p90_severity,
       cs.claim_cost_sgd,
       CAST (cs.claim_cost_sgd / NULLIF (pe.ape_sgd, 0) AS DECIMAL (18, 6)) AS loss_ratio,
       cs.avg_claim_tat_days,
       ISNULL(cs.declined_claims, 0) AS declined_claims,
       CAST (ISNULL(cs.declined_claims, 0) * 1.0 / NULLIF (cs.claims, 0) AS DECIMAL (18, 6)) AS decline_rate
FROM   policy_exposure AS pe
       LEFT OUTER JOIN
       claim_summary AS cs
       ON cs.delay_band = pe.delay_band
          AND cs.delay_sort = pe.delay_sort;


GO
/*==========================================================================
  3. BO-3 - IP RIDER REPRICING / PERSISTENCY

  Business question:
  Where is rider repricing creating premium reduction and customer churn?
==========================================================================*/
CREATE OR ALTER VIEW prudential.vw_ip_rider_repricing
AS
SELECT   d.year_month,
         p.product_name,
         p.product_category,
         f.ip_plan_tier,
         f.copay_pct,
         COUNT_BIG(*) AS rider_policy_count,
         AVG(f.rider_premium_before_sgd) AS avg_rider_premium_before_sgd,
         AVG(f.rider_premium_after_sgd) AS avg_rider_premium_after_sgd,
         CAST (AVG(CASE WHEN f.rider_premium_before_sgd > 0 THEN (f.rider_premium_before_sgd - f.rider_premium_after_sgd) / f.rider_premium_before_sgd END) AS DECIMAL (18, 6)) AS avg_premium_reduction_rate,
         CAST (SUM(CASE WHEN f.lapse_flag = 1 THEN 1 ELSE 0 END) * 1.0 / NULLIF (COUNT_BIG(*), 0) AS DECIMAL (18, 6)) AS lapse_rate,
         CAST (SUM(CASE WHEN f.persistency_13m = 0 THEN 1 ELSE 0 END) * 1.0 / NULLIF (COUNT_BIG(*), 0) AS DECIMAL (18, 6)) AS persistency_failure_rate,
         SUM(ISNULL(f.rider_premium_before_sgd, 0) - ISNULL(f.rider_premium_after_sgd, 0)) AS premium_reduction_sgd
FROM     prudential.fact_policy_new_business AS f
         INNER JOIN
         prudential.dim_date AS d
         ON d.date_key = f.start_date_key
         INNER JOIN
         prudential.dim_product AS p
         ON p.product_key = f.product_key
WHERE    f.dq_row_status = 'OK'
         AND f.rider_attached_flag = 1
GROUP BY d.year_month, p.product_name, p.product_category, f.ip_plan_tier, f.copay_pct;


GO
/*==========================================================================
  4. BO-4 - CLAIMS & CUSTOMER TRUST

  Business question:
  Where are turnaround time, disputes, complaints and negative sentiment
  converging into a customer-trust problem?
==========================================================================*/
CREATE OR ALTER VIEW prudential.vw_claims_trust
AS
SELECT   d.year_month,
         ct.claim_type,
         cl.claim_status,
         cl.claim_decline_reason,
         COUNT_BIG(*) AS claims,
         SUM(cl.claim_amount_sgd) AS claim_amount_sgd,
         AVG(cl.claim_amount_sgd) AS avg_claim_amount_sgd,
         AVG(CAST (cl.claim_tat_days AS DECIMAL (18, 4))) AS avg_claim_tat_days,
         MAX(cl.claim_tat_days) AS max_claim_tat_days,
         SUM(CASE WHEN cl.claim_tat_days > 30 THEN 1 ELSE 0 END) AS claims_over_30_days,
         CAST (SUM(CASE WHEN cl.claim_tat_days > 30 THEN 1 ELSE 0 END) * 1.0 / NULLIF (COUNT_BIG(*), 0) AS DECIMAL (18, 6)) AS sla_risk_rate,
         SUM(CASE WHEN cl.disputed_flag = 1 THEN 1 ELSE 0 END) AS disputed_claims,
         CAST (SUM(CASE WHEN cl.disputed_flag = 1 THEN 1 ELSE 0 END) * 1.0 / NULLIF (COUNT_BIG(*), 0) AS DECIMAL (18, 6)) AS dispute_rate,
         SUM(CASE WHEN cl.complaint_flag = 1 THEN 1 ELSE 0 END) AS complaints,
         CAST (SUM(CASE WHEN cl.complaint_flag = 1 THEN 1 ELSE 0 END) * 1.0 / NULLIF (COUNT_BIG(*), 0) AS DECIMAL (18, 6)) AS complaint_rate,
         AVG(cl.customer_sentiment_score) AS avg_sentiment_score,
         AVG(CAST (cl.nps_score AS DECIMAL (18, 4))) AS avg_nps
FROM     prudential.fact_claim AS cl
         INNER JOIN
         prudential.dim_date AS d
         ON d.date_key = cl.claim_date_key
         LEFT OUTER JOIN
         prudential.dim_claim_type AS ct
         ON ct.claim_type_key = cl.claim_type_key
WHERE    cl.dq_row_status = 'OK'
GROUP BY d.year_month, ct.claim_type, cl.claim_status, cl.claim_decline_reason;


GO
/*==========================================================================
  5. BO-5 - ASSESSOR FRAUD / CONDUCT ALERTS

  Business question:
  Which assessors behave materially differently from their peer group?

  A z-score above +3 is treated as a strong outlier signal in the guide.
  Minimum 20 decisions reduces small-sample false positives.
==========================================================================*/
CREATE OR ALTER VIEW prudential.vw_assessor_alerts
AS
WITH   per_assessor
AS     (SELECT   a.assessor_id,
                 COUNT_BIG(*) AS decisions,
                 AVG(cl.claim_amount_sgd) AS avg_amount,
                 SUM(CASE WHEN cl.approval_above_limit = 1 THEN 1 ELSE 0 END) AS above_limit,
                 SUM(CASE WHEN cl.sod_breach = 1 THEN 1 ELSE 0 END) AS sod_breaches,
                 AVG(cl.fraud_risk_score) AS avg_fraud_score
        FROM     prudential.fact_claim AS cl
                 INNER JOIN
                 prudential.dim_assessor AS a
                 ON a.assessor_key = cl.assessor_key
              WHERE    cl.dq_row_status = 'OK'
        GROUP BY a.assessor_id),
       scored
AS     (SELECT pa.*,
               CAST (pa.above_limit * 1.0 / NULLIF (pa.decisions, 0) AS DECIMAL (18, 6)) AS above_limit_rate,
               CAST ((pa.avg_fraud_score - AVG(pa.avg_fraud_score) OVER ()) / NULLIF (STDEV(pa.avg_fraud_score) OVER (), 0) AS DECIMAL (18, 6)) AS z_score
        FROM   per_assessor AS pa
        WHERE  pa.decisions >= 20)
SELECT assessor_id,
       decisions,
       avg_amount,
       above_limit,
       sod_breaches,
       avg_fraud_score,
       above_limit_rate,
       z_score,
       CASE WHEN z_score >= 3
                 OR above_limit_rate >= 0.10
                 OR (sod_breaches * 1.0 / NULLIF (decisions, 0)) >= 0.05 THEN 'HIGH RISK' WHEN z_score >= 2 THEN 'WATCH' ELSE 'NORMAL' END AS alert_status
FROM   scored;


GO
/*==========================================================================
  6. BO-6 - AGENCY PERFORMANCE / ATTRITION

  Business question:
  Which agents / agency units have the highest premium at risk if attrition
  materialises?
==========================================================================*/
CREATE OR ALTER VIEW prudential.vw_agent_attrition
AS
SELECT   a.agent_id,
         a.agent_name,
         a.agent_tier,
         a.agency_unit,
         a.agent_status,
         a.agent_tenure_months,
         a.agent_productivity_score,
         a.agent_attrition_risk,
         COUNT_BIG(DISTINCT f.policy_key) AS policy_count,
         SUM(f.ape_sgd) AS total_ape_sgd,
         SUM(CASE WHEN ISNULL(a.agent_attrition_risk, 0) >= 0.70 THEN f.ape_sgd ELSE 0 END) AS premium_at_risk_sgd,
         CAST (SUM(CASE WHEN f.lapse_flag = 1 THEN 1 ELSE 0 END) * 1.0 / NULLIF (COUNT_BIG(DISTINCT f.policy_key), 0) AS DECIMAL (18, 6)) AS agent_policy_lapse_rate,
         CASE WHEN ISNULL(a.agent_attrition_risk, 0) >= 0.70
                   AND SUM(f.ape_sgd) >= 250000 THEN 'CRITICAL' WHEN ISNULL(a.agent_attrition_risk, 0) >= 0.70 THEN 'HIGH' WHEN ISNULL(a.agent_attrition_risk, 0) >= 0.50 THEN 'WATCH' ELSE 'NORMAL' END AS attrition_risk_status
FROM     prudential.dim_agent AS a
         LEFT OUTER JOIN
         prudential.fact_policy_new_business AS f
         ON f.agent_key = a.agent_key
            AND f.dq_row_status = 'OK'
WHERE    a.agent_key <> -1
 GROUP BY a.agent_id, a.agent_name, a.agent_tier, a.agency_unit, a.agent_status, a.agent_tenure_months, a.agent_productivity_score, a.agent_attrition_risk;


GO
/*==========================================================================
  7. EXECUTIVE KPI VIEW

  One governed row for the executive cockpit.
  This view intentionally uses independent aggregates to prevent fact_claim
  from multiplying policy-level measures.
==========================================================================*/
CREATE OR ALTER VIEW prudential.vw_executive_kpis
AS
WITH   policy_kpi
AS     (SELECT COUNT_BIG(*) AS total_policies,
               COUNT(DISTINCT f.customer_key) AS total_customers,
               SUM(f.ape_sgd) AS total_ape_sgd,
               SUM(f.nbp_sgd) AS total_nbp_sgd,
               CAST (SUM(f.nbp_sgd) / NULLIF (SUM(f.ape_sgd), 0) AS DECIMAL (18, 6)) AS blended_margin,
               CAST (SUM(CASE WHEN p.is_low_margin = 1 THEN f.ape_sgd ELSE 0 END) / NULLIF (SUM(f.ape_sgd), 0) AS DECIMAL (18, 6)) AS low_margin_mix,
               CAST (SUM(CASE WHEN f.lapse_flag = 1 THEN 1 ELSE 0 END) * 1.0 / NULLIF (COUNT_BIG(*), 0) AS DECIMAL (18, 6)) AS lapse_rate,
               CAST (SUM(CASE WHEN f.persistency_13m = 0 THEN 1 ELSE 0 END) * 1.0 / NULLIF (COUNT_BIG(*), 0) AS DECIMAL (18, 6)) AS persistency_failure_rate,
               SUM(CASE WHEN ISNULL(f.rider_attached_flag, 0) = 1 THEN ISNULL(f.rider_premium_before_sgd, 0) - ISNULL(f.rider_premium_after_sgd, 0) ELSE 0 END) AS rider_premium_reduction_sgd
        FROM   prudential.fact_policy_new_business AS f
               INNER JOIN
               prudential.dim_product AS p
               ON p.product_key = f.product_key
        WHERE  f.dq_row_status = 'OK'),
       claim_kpi
AS     (SELECT COUNT_BIG(*) AS total_claims,
               SUM(cl.claim_amount_sgd) AS total_claim_amount_sgd,
               AVG(cl.claim_amount_sgd) AS avg_claim_severity_sgd,
               AVG(CAST (cl.claim_tat_days AS DECIMAL (18, 4))) AS avg_claim_tat_days,
               CAST (SUM(CASE WHEN cl.disputed_flag = 1 THEN 1 ELSE 0 END) * 1.0 / NULLIF (COUNT_BIG(*), 0) AS DECIMAL (18, 6)) AS dispute_rate,
               CAST (SUM(CASE WHEN cl.complaint_flag = 1 THEN 1 ELSE 0 END) * 1.0 / NULLIF (COUNT_BIG(*), 0) AS DECIMAL (18, 6)) AS complaint_rate,
               CAST (SUM(CASE WHEN cl.approval_above_limit = 1
                                   OR cl.sod_breach = 1 THEN 1 ELSE 0 END) * 1.0 / NULLIF (COUNT_BIG(*), 0) AS DECIMAL (18, 6)) AS control_breach_rate,
               AVG(cl.fraud_risk_score) AS avg_fraud_risk_score
        FROM   prudential.fact_claim AS cl
        WHERE  cl.dq_row_status = 'OK'),
       agency_kpi
AS     (SELECT COUNT(DISTINCT a.agent_id) AS agent_population,
               SUM(CASE WHEN ISNULL(a.agent_attrition_risk, 0) >= 0.70 THEN ISNULL(f.ape_sgd, 0) ELSE 0 END) AS premium_at_high_attrition_risk_sgd
             FROM   prudential.dim_agent AS a
               LEFT OUTER JOIN
               prudential.fact_policy_new_business AS f
               ON f.agent_key = a.agent_key
                  AND f.dq_row_status = 'OK'
        WHERE  a.agent_key <> -1)
SELECT pk.total_policies,
       pk.total_customers,
       pk.total_ape_sgd,
       pk.total_nbp_sgd,
       pk.blended_margin,
       pk.low_margin_mix,
       pk.lapse_rate,
       pk.persistency_failure_rate,
       pk.rider_premium_reduction_sgd,
       ck.total_claims,
       ck.total_claim_amount_sgd,
       ck.avg_claim_severity_sgd,
       ck.avg_claim_tat_days,
       ck.dispute_rate,
       ck.complaint_rate,
       ck.control_breach_rate,
       ck.avg_fraud_risk_score,
       ak.agent_population,
       ak.premium_at_high_attrition_risk_sgd
FROM   policy_kpi AS pk CROSS JOIN claim_kpi AS ck CROSS JOIN agency_kpi AS ak;


GO
/*==========================================================================
  8. KPI VALIDATION / PROOF QUERIES

  Run these after the views are created. These are diagnostic checks, not
  additional source tables.
==========================================================================*/
/* 8.1 View inventory */
SELECT   v.name AS view_name,
         v.create_date,
         v.modify_date
FROM     sys.views AS v
WHERE    v.name IN ('vw_nbp_margin', 'vw_health_gap', 'vw_ip_rider_repricing', 'vw_claims_trust', 'vw_assessor_alerts', 'vw_agent_attrition', 'vw_executive_kpis')
ORDER BY v.name;


GO
/* 8.2 Executive KPI snapshot */
SELECT *
FROM   prudential.vw_executive_kpis;


GO
/* 8.3 Margin diagnostic - worst segments first */
SELECT   TOP (20) year_month,
                  product_category,
                  product_name,
                  channel_name,
                  policy_count,
                  ape_sgd,
                  nbp_sgd,
                  blended_margin,
                  low_margin_mix,
                  lapse_rate
FROM     prudential.vw_nbp_margin
ORDER BY blended_margin ASC, ape_sgd DESC;


GO
/* 8.4 Health-gap diagnostic */
SELECT   delay_band,
         policies,
         claims,
         avg_severity,
         p90_severity,
         loss_ratio,
         avg_claim_tat_days,
         decline_rate
FROM     prudential.vw_health_gap
ORDER BY delay_band;


GO
/* 8.5 Repricing / churn hotspots */
SELECT   TOP (20) year_month,
                  product_name,
                  ip_plan_tier,
                  copay_pct,
                  rider_policy_count,
                  avg_premium_reduction_rate,
                  lapse_rate,
                  persistency_failure_rate,
                  premium_reduction_sgd
FROM     prudential.vw_ip_rider_repricing
ORDER BY lapse_rate DESC, avg_premium_reduction_rate DESC;


GO
/* 8.6 Claims trust hotspots */
SELECT   TOP (20) year_month,
                  claim_type,
                  claim_status,
                  claim_decline_reason,
                  claims,
                  avg_claim_tat_days,
                  sla_risk_rate,
                  dispute_rate,
                  complaint_rate,
                  avg_sentiment_score,
                  avg_nps
FROM     prudential.vw_claims_trust
ORDER BY dispute_rate DESC, complaint_rate DESC, sla_risk_rate DESC;


GO
/* 8.7 Assessor alert queue */
SELECT   assessor_id,
         decisions,
         avg_amount,
         above_limit,
         sod_breaches,
         avg_fraud_score,
         above_limit_rate,
         z_score,
         alert_status
FROM     prudential.vw_assessor_alerts
ORDER BY CASE alert_status WHEN 'HIGH RISK' THEN 1 WHEN 'WATCH' THEN 2 ELSE 3 END, z_score DESC;


GO
/* 8.8 Agency premium-at-risk queue */
SELECT   TOP (20) agent_id,
                  agent_name,
                  agency_unit,
                  agent_status,
                  agent_productivity_score,
                  agent_attrition_risk,
                  policy_count,
                  total_ape_sgd,
                  premium_at_risk_sgd,
                  attrition_risk_status
FROM     prudential.vw_agent_attrition
ORDER BY premium_at_risk_sgd DESC, agent_attrition_risk DESC;


GO
/*==========================================================================
  9. RECONCILIATION CHECKS
==========================================================================*/
/* The KPI view should reconcile directly to the clean policy fact table. */
WITH   base
AS     (SELECT SUM(ape_sgd) AS expected_ape,
               SUM(nbp_sgd) AS expected_nbp
        FROM   prudential.fact_policy_new_business
        WHERE  dq_row_status = 'OK'),
       kpi
AS     (SELECT total_ape_sgd AS actual_ape,
               total_nbp_sgd AS actual_nbp
        FROM   prudential.vw_executive_kpis)
SELECT b.expected_ape,
       k.actual_ape,
       b.expected_ape - k.actual_ape AS ape_variance,
       b.expected_nbp,
       k.actual_nbp,
       b.expected_nbp - k.actual_nbp AS nbp_variance,
       CASE WHEN ABS(ISNULL(b.expected_ape, 0) - ISNULL(k.actual_ape, 0)) < 0.01
                 AND ABS(ISNULL(b.expected_nbp, 0) - ISNULL(k.actual_nbp, 0)) < 0.01 THEN 'PASS' ELSE 'FAIL' END AS reconciliation_status
FROM   base AS b CROSS JOIN kpi AS k;


GO
/*==========================================================================
  10. KPI DEFINITION / BUSINESS OWNERSHIP REFERENCE

  BO-1  Margin       -> vw_nbp_margin
  BO-2  Health Gap   -> vw_health_gap
  BO-3  Repricing    -> vw_ip_rider_repricing
  BO-4  Trust        -> vw_claims_trust
  BO-5  Fraud        -> vw_assessor_alerts
  BO-6  Agency       -> vw_agent_attrition
  BO-7  Governance   -> vw_executive_kpis + reconciliation checks

  These views are the certified SQL interface for the Power BI reporting layer.
==========================================================================*/
PRINT 'BUSINESS KPI LAYER CREATED SUCCESSFULLY.';

PRINT 'Scenario simulation tables are available in the prudential schema.';