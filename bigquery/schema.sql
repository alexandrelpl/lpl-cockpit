-- ============================================================================
-- LPL Cockpit — Schémas BigQuery
-- Dataset cible : `${PROJECT}.lpl_cockpit`
-- Exécuter une fois (les CREATE ... IF NOT EXISTS sont idempotents).
-- Remplacer ${PROJECT} par l'ID de ton projet Google Cloud avant exécution,
-- ou laisser le script Python create_dataset_and_tables() le faire.
-- ============================================================================

-- 1) CA Shopify ré-attribué à la DATE DE COMMANDE (logique Apps Script V5.7).
--    net_sales = total commande - remboursements réussis, commande bucketée
--    sur sa date de création (timezone Europe/Paris). Refresh glissant 40 j
--    => les remboursements tardifs sont rattrapés et re-datés correctement.
CREATE TABLE IF NOT EXISTS `lpl_cockpit.shopify_orders_daily` (
  date              DATE      NOT NULL,
  net_sales         FLOAT64   NOT NULL,   -- Total Net Sales (col B du Sheet)
  orders            INT64     NOT NULL,   -- Total Orders inclus (col C)
  comptoir_new      INT64,
  comptoir_existing INT64,
  optique_new       INT64,
  optique_existing  INT64,
  mm_new            INT64,
  mm_existing       INT64,
  others_new        INT64,
  others_existing   INT64,
  updated_at        TIMESTAMP NOT NULL
)
PARTITION BY date
OPTIONS (description = "CA net Shopify par date de commande, logique métier LPL (exclusions b2b/wholesale/alan/voided/non-web).");

-- 2) Trafic & conversion Shopify (déjà filtré des bots par Shopify).
CREATE TABLE IF NOT EXISTS `lpl_cockpit.shopify_traffic_daily` (
  date             DATE      NOT NULL,
  sessions         INT64,                 -- sessions hors bots (natif Shopify)
  visitors         INT64,
  conversion_rate  FLOAT64,               -- 0..1
  updated_at       TIMESTAMP NOT NULL
)
PARTITION BY date
OPTIONS (description = "Trafic & taux de conversion Shopify (ShopifyQL sessions, bots exclus).");

-- 3) Dépenses & résultats Meta Ads (niveau campagne ; account = agrégat).
CREATE TABLE IF NOT EXISTS `lpl_cockpit.meta_daily` (
  date            DATE      NOT NULL,
  campaign_id     STRING,                 -- NULL pour la ligne account
  campaign_name   STRING,                 -- 'ACCOUNT' pour l'agrégat
  spend           FLOAT64,
  purchases       FLOAT64,                -- omni_purchase (count)
  purchase_value  FLOAT64,                -- omni_purchase (valeur)
  impressions     INT64,
  clicks          INT64,
  link_clicks     INT64,
  updated_at      TIMESTAMP NOT NULL
)
PARTITION BY date
CLUSTER BY campaign_id
OPTIONS (description = "Meta Ads quotidien, métriques standardisées (omni_purchase).");

-- 4) Dépenses & résultats Google Ads (par campagne).
CREATE TABLE IF NOT EXISTS `lpl_cockpit.google_daily` (
  date              DATE      NOT NULL,
  campaign_id       STRING,
  campaign_name     STRING,
  campaign_type     STRING,               -- SEARCH, PERFORMANCE_MAX, DEMAND_GEN...
  cost              FLOAT64,
  conversions       FLOAT64,
  conversion_value  FLOAT64,
  impressions       INT64,
  clicks            INT64,
  updated_at        TIMESTAMP NOT NULL
)
PARTITION BY date
CLUSTER BY campaign_id
OPTIONS (description = "Google Ads quotidien (GAQL). Vide tant que l'accès Basic n'est pas accordé.");

-- 4b) Asset groups (groupes de composants) des campagnes Performance Max.
CREATE TABLE IF NOT EXISTS `lpl_cockpit.google_asset_group_daily` (
  date              DATE      NOT NULL,
  campaign_id       STRING,
  campaign_name     STRING,
  asset_group_id    STRING,
  asset_group_name  STRING,
  cost              FLOAT64,
  conversions       FLOAT64,
  conversion_value  FLOAT64,
  impressions       INT64,
  clicks            INT64,
  updated_at        TIMESTAMP NOT NULL
)
PARTITION BY date
CLUSTER BY asset_group_id
OPTIONS (description = "Performance quotidienne par asset group (PMax), via GAQL.");

-- ============================================================================
-- VUE D'ENSEMBLE — un point par jour, prête pour le front / Looker Studio.
-- COS blended = (dépense Meta + Google) / CA net Shopify.
-- ============================================================================
CREATE OR REPLACE VIEW `lpl_cockpit.cockpit_daily` AS
WITH meta_acc AS (   -- agrégat compte = somme des campagnes (robuste si pas de ligne ACCOUNT)
  SELECT date, SUM(spend) AS meta_spend, SUM(purchase_value) AS meta_value,
         SUM(purchases) AS meta_purchases
  FROM `lpl_cockpit.meta_daily`
  WHERE campaign_id IS NOT NULL
  GROUP BY date
),
google_acc AS (
  SELECT date, SUM(cost) AS google_spend, SUM(conversion_value) AS google_value,
         SUM(conversions) AS google_conv
  FROM `lpl_cockpit.google_daily`
  GROUP BY date
)
SELECT
  s.date,
  s.net_sales                                   AS ca_shopify,
  s.orders                                      AS orders,
  COALESCE(m.meta_spend, 0)                     AS meta_spend,
  COALESCE(g.google_spend, 0)                   AS google_spend,
  COALESCE(m.meta_spend, 0) + COALESCE(g.google_spend, 0) AS ad_spend_total,
  COALESCE(m.meta_value, 0)                     AS meta_value,
  COALESCE(g.google_value, 0)                   AS google_value,
  SAFE_DIVIDE(COALESCE(m.meta_spend,0)+COALESCE(g.google_spend,0), s.net_sales) AS cos_blended,
  SAFE_DIVIDE(s.net_sales, COALESCE(m.meta_spend,0)+COALESCE(g.google_spend,0)) AS roas_blended,
  t.sessions,
  t.visitors,
  t.conversion_rate
FROM `lpl_cockpit.shopify_orders_daily` s
LEFT JOIN meta_acc   m USING (date)
LEFT JOIN google_acc g USING (date)
LEFT JOIN `lpl_cockpit.shopify_traffic_daily` t USING (date)
ORDER BY s.date;
