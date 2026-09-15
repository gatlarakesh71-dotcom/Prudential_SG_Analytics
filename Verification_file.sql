/******************************************************************************************
                                /* Dimension Tables */
******************************************************************************************/
SELECT *
FROM   [prudential].[dim_customer];

SELECT *
FROM   [prudential].[dim_agent];

SELECT *
FROM   [prudential].[dim_assessor];

SELECT *
FROM   [prudential].[dim_channel];

SELECT *
FROM   [prudential].[dim_claim_type];

SELECT *
FROM   [prudential].[dim_date];

SELECT *
FROM   [prudential].[dim_product];

/******************************************************************************************
                                /* Fact Tables */
******************************************************************************************/
SELECT *
FROM   [prudential].[fact_claim];

SELECT *
FROM   [prudential].[fact_policy_new_business];

/******************************************************************************************
                                /* Scenario Tables */
******************************************************************************************/
SELECT *
FROM   [prudential].[scenario_parameter];

SELECT *
FROM   [prudential].[scenario_result];

SELECT *
FROM   [prudential].[scenario_run];