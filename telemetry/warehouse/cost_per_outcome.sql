-- R7: cost per outcome. Spend comes from gateway request spans (gov.cost.amount); outcomes are
-- KPI events (one event = one outcome). Joined by use case and calendar month (UTC).
CREATE OR REPLACE TABLE spend AS
SELECT use_case_id, business_unit, team, strftime(ts, '%Y-%m') AS period,
       SUM(cost) AS spend_usd, COUNT(*) AS requests
FROM spans_flat
WHERE name = 'gateway.request' AND cost IS NOT NULL AND use_case_id IS NOT NULL
GROUP BY ALL;

CREATE OR REPLACE TABLE outcomes AS
SELECT k.use_case_id, strftime(k.ts, '%Y-%m') AS period,
       COUNT(*) AS outcomes, SUM(k.kpi_value) AS kpi_value_sum, string_agg(DISTINCT k.kpi_name, ';') AS kpi_names
FROM kpi_events k
GROUP BY ALL;

CREATE OR REPLACE TABLE use_case_dim AS SELECT * FROM use_cases;

CREATE OR REPLACE TABLE joined AS
SELECT COALESCE(s.use_case_id, o.use_case_id) AS use_case_id,
       COALESCE(s.business_unit, d.business_unit) AS business_unit,
       COALESCE(s.team, d.team) AS team,
       COALESCE(s.period, o.period) AS period,
       COALESCE(s.spend_usd, 0) AS spend_usd,
       COALESCE(s.requests, 0) AS requests,
       COALESCE(o.outcomes, 0) AS outcomes,
       COALESCE(o.kpi_value_sum, 0) AS kpi_value_sum,
       o.kpi_names
FROM spend s
FULL OUTER JOIN outcomes o ON s.use_case_id = o.use_case_id AND s.period = o.period
LEFT JOIN use_case_dim d ON d.use_case_id = COALESCE(s.use_case_id, o.use_case_id);

CREATE OR REPLACE TABLE cost_per_outcome AS
SELECT 'use_case' AS level, use_case_id AS key, business_unit, team, period,
       spend_usd, requests, outcomes, kpi_names,
       CASE WHEN outcomes > 0 THEN spend_usd / outcomes END AS cost_per_outcome_usd
FROM joined
UNION ALL
SELECT 'team', team, business_unit, team, period, SUM(spend_usd), SUM(requests), SUM(outcomes), NULL,
       CASE WHEN SUM(outcomes) > 0 THEN SUM(spend_usd) / SUM(outcomes) END
FROM joined GROUP BY business_unit, team, period
UNION ALL
SELECT 'business_unit', business_unit, business_unit, NULL, period, SUM(spend_usd), SUM(requests), SUM(outcomes), NULL,
       CASE WHEN SUM(outcomes) > 0 THEN SUM(spend_usd) / SUM(outcomes) END
FROM joined GROUP BY business_unit, period
ORDER BY level, key, period;
