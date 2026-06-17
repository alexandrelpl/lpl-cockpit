"""
Métriques clients new vs returning, customer-level, fenêtre glissante 3 ans, deux portées :
  - web    : commandes Shopify uniquement
  - global : web + ventes boutique (rapprochées par email)

Tout se calcule en SQL (économe). Deux étapes :
  1. retail_purchases : ventes boutique dédupliquées (email, date) depuis le DWH retail
     (bascule de source opticbox<2023-12-20 / invoice_optimum>= pour éviter le double-comptage).
  2. customers_period : matérialise new/returning par (scope, grain, période).
     Returning = a déjà acheté dans les 3 ans AVANT le début de la période ; sinon Nouveau
     (1re commande, ou réactivation après >3 ans).
"""

from __future__ import annotations
import os

from google.cloud import bigquery

BQ_PROJECT   = os.environ["BQ_PROJECT"]
BQ_DATASET   = os.environ.get("BQ_DATASET", "lpl_cockpit")
RETAIL_TABLE = os.environ.get("RETAIL_TABLE",
                              "stable-splicer-294813.dwh_datasource_sales.transaction_details_visits")
RETAIL_CUTOVER = os.environ.get("RETAIL_CUTOVER", "2023-12-20")


def _t(name):
    return f"`{BQ_PROJECT}.{BQ_DATASET}.{name}`"


def _retail_sql():
    return f"""
CREATE OR REPLACE TABLE {_t('retail_purchases')} AS
SELECT DISTINCT
  LOWER(TRIM(customer_email)) AS cust,
  DATE(COALESCE(invoice_creation_datetime, visit_creation_datetime)) AS date
FROM `{RETAIL_TABLE}`
WHERE ttc_net_sale_price > 0
  AND customer_email IS NOT NULL AND TRIM(customer_email) != ''
  AND (
    (data_source = 'invoice_optimum'
       AND DATE(COALESCE(invoice_creation_datetime, visit_creation_datetime)) >= DATE('{RETAIL_CUTOVER}'))
    OR (data_source = 'opticbox'
       AND DATE(COALESCE(invoice_creation_datetime, visit_creation_datetime)) < DATE('{RETAIL_CUTOVER}'))
  )
"""


def _period_sql():
    # new = aucun achat (même portée) dans les 3 ans avant la période ; sinon returning.
    # new_brand = nouveau ET aucun achat TOUS CANAUX (global) dans les 3 ans -> vrai nouveau LPL.
    return f"""
CREATE OR REPLACE TABLE {_t('customers_period')} AS
WITH web AS (
  SELECT DISTINCT LOWER(TRIM(email)) AS cust, date
  FROM {_t('shopify_customer_orders')}
  WHERE email IS NOT NULL AND TRIM(email) != ''
),
retail AS (SELECT DISTINCT cust, date FROM {_t('retail_purchases')}),
glob AS (SELECT DISTINCT cust, date FROM (SELECT cust, date FROM web UNION ALL SELECT cust, date FROM retail)),
evd AS (
  SELECT 'web' AS scope, cust, date FROM web
  UNION ALL SELECT 'global', cust, date FROM glob
),
ap AS (
  SELECT scope, cust, 'day' AS grain, date AS pstart FROM evd
  UNION ALL SELECT scope, cust, 'week', DATE_TRUNC(date, ISOWEEK) FROM evd
  UNION ALL SELECT scope, cust, 'month', DATE_TRUNC(date, MONTH) FROM evd
),
apd AS (SELECT DISTINCT scope, cust, grain, pstart FROM ap),
flagged AS (
  SELECT a.scope, a.cust, a.grain, a.pstart,
    MAX(IF(p.cust IS NULL, 0, 1)) AS prior_scope,
    MAX(IF(g.cust IS NULL, 0, 1)) AS prior_global
  FROM apd a
  LEFT JOIN evd p ON p.scope = a.scope AND p.cust = a.cust
    AND p.date >= DATE_SUB(a.pstart, INTERVAL 3 YEAR) AND p.date < a.pstart
  LEFT JOIN glob g ON g.cust = a.cust
    AND g.date >= DATE_SUB(a.pstart, INTERVAL 3 YEAR) AND g.date < a.pstart
  GROUP BY 1, 2, 3, 4
)
SELECT scope, grain,
  CASE grain
    WHEN 'day' THEN FORMAT_DATE('%Y-%m-%d', pstart)
    WHEN 'week' THEN FORMAT_DATE('%G-W%V', pstart)
    ELSE FORMAT_DATE('%Y-%m', pstart) END AS period,
  MIN(pstart) AS period_start,
  COUNTIF(prior_scope = 0) AS new_customers,
  COUNTIF(prior_scope = 1) AS returning_customers,
  COUNTIF(prior_scope = 0 AND prior_global = 0) AS new_brand
FROM flagged GROUP BY 1, 2, 3
"""


def _acq_sql():
    # CAC web brut = dépense pub blended / nouveaux clients WEB (la pub stimule surtout le web ;
    # on évite la distorsion des jours sans retail, ex. dimanches boutiques fermées).
    return f"""
CREATE OR REPLACE TABLE {_t('acquisition_period')} AS
WITH sp AS (SELECT date, COALESCE(ad_spend_total, 0) AS spend FROM {_t('cockpit_daily')}),
spg AS (
  SELECT 'day' AS grain, FORMAT_DATE('%Y-%m-%d', date) AS period, date AS period_start, SUM(spend) AS ad_spend
  FROM sp GROUP BY 1, 2, 3
  UNION ALL SELECT 'week', FORMAT_DATE('%G-W%V', date), DATE_TRUNC(date, ISOWEEK), SUM(spend) FROM sp GROUP BY 1, 2, 3
  UNION ALL SELECT 'month', FORMAT_DATE('%Y-%m', date), DATE_TRUNC(date, MONTH), SUM(spend) FROM sp GROUP BY 1, 2, 3
),
nb AS (SELECT grain, period, new_customers AS new_web FROM {_t('customers_period')} WHERE scope = 'web')
SELECT g.grain, g.period, g.period_start, ROUND(g.ad_spend, 2) AS ad_spend,
  nb.new_web, ROUND(SAFE_DIVIDE(g.ad_spend, NULLIF(nb.new_web, 0)), 2) AS cac
FROM spg g LEFT JOIN nb USING (grain, period)
"""


def _ropo_sql():
    # ROPO mensuel : clients web qui ont ensuite acheté en boutique (et inverse).
    return f"""
CREATE OR REPLACE TABLE {_t('ropo_month')} AS
WITH web AS (
  SELECT DISTINCT LOWER(TRIM(email)) AS cust, date FROM {_t('shopify_customer_orders')}
  WHERE email IS NOT NULL AND TRIM(email) != ''
),
retail AS (SELECT DISTINCT cust, date FROM {_t('retail_purchases')}),
fw AS (SELECT cust, MIN(date) AS first_web FROM web GROUP BY cust),
fr AS (SELECT cust, MIN(date) AS first_retail FROM retail GROUP BY cust),
w2s AS (
  SELECT FORMAT_DATE('%Y-%m', DATE_TRUNC(r.date, MONTH)) AS period, COUNT(DISTINCT r.cust) AS web_to_store
  FROM retail r JOIN fw ON fw.cust = r.cust AND fw.first_web < DATE_TRUNC(r.date, MONTH) GROUP BY 1
),
s2w AS (
  SELECT FORMAT_DATE('%Y-%m', DATE_TRUNC(w.date, MONTH)) AS period, COUNT(DISTINCT w.cust) AS store_to_web
  FROM web w JOIN fr ON fr.cust = w.cust AND fr.first_retail < DATE_TRUNC(w.date, MONTH) GROUP BY 1
),
tmw AS (SELECT FORMAT_DATE('%Y-%m', DATE_TRUNC(date, MONTH)) AS period, COUNT(DISTINCT cust) AS total_web FROM web GROUP BY 1),
tms AS (SELECT FORMAT_DATE('%Y-%m', DATE_TRUNC(date, MONTH)) AS period, COUNT(DISTINCT cust) AS total_store FROM retail GROUP BY 1),
allp AS (SELECT period FROM tmw UNION DISTINCT SELECT period FROM tms)
SELECT ap.period, PARSE_DATE('%Y-%m-%d', CONCAT(ap.period, '-01')) AS period_start,
  COALESCE(w.web_to_store, 0) AS web_to_store, COALESCE(s.store_to_web, 0) AS store_to_web,
  COALESCE(tw.total_web, 0) AS total_web, COALESCE(ts.total_store, 0) AS total_store
FROM allp ap
LEFT JOIN w2s w USING (period) LEFT JOIN s2w s USING (period)
LEFT JOIN tmw tw USING (period) LEFT JOIN tms ts USING (period)
"""


def refresh() -> int:
    client = bigquery.Client(project=BQ_PROJECT)
    client.query(_retail_sql()).result()
    print("[customers] retail_purchases reconstruit")
    client.query(_period_sql()).result()
    print("[customers] customers_period matérialisé (web + global + new_brand, fenêtre 3 ans)")
    client.query(_acq_sql()).result()
    print("[customers] acquisition_period matérialisé (CAC brut)")
    client.query(_ropo_sql()).result()
    print("[customers] ropo_month matérialisé")
    return 1


if __name__ == "__main__":
    refresh()
