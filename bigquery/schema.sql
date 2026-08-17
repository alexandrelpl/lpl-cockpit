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
  net_sales_nl      FLOAT64,              -- dont CA livré aux Pays-Bas (test NL)
  orders_nl         INT64,                -- dont commandes livrées NL
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

-- Colonnes NL ajoutées après coup (test Pays-Bas) : idempotent pour la table existante.
ALTER TABLE `lpl_cockpit.shopify_orders_daily` ADD COLUMN IF NOT EXISTS net_sales_nl FLOAT64;
ALTER TABLE `lpl_cockpit.shopify_orders_daily` ADD COLUMN IF NOT EXISTS orders_nl INT64;

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

-- 5) CRO — funnel GA4 (par jour).
CREATE TABLE IF NOT EXISTS `lpl_cockpit.ga4_funnel_daily` (
  date DATE NOT NULL, sessions INT64, add_to_carts INT64, checkouts INT64,
  purchases INT64, item_views INT64, updated_at TIMESTAMP NOT NULL
) PARTITION BY date OPTIONS (description = "Funnel GA4 quotidien (CRO).");

-- 5b) CRO — produits GA4 (par jour x produit).
CREATE TABLE IF NOT EXISTS `lpl_cockpit.ga4_items_daily` (
  date DATE NOT NULL, item_name STRING, views INT64, add_to_carts INT64,
  purchases INT64, revenue FLOAT64, updated_at TIMESTAMP NOT NULL
) PARTITION BY date CLUSTER BY item_name OPTIONS (description = "GA4 item-level quotidien (CRO).");

-- 5c) CRO — canaux d'acquisition GA4 (par jour x canal).
CREATE TABLE IF NOT EXISTS `lpl_cockpit.ga4_channels_daily` (
  date DATE NOT NULL, channel STRING, sessions INT64, purchases INT64,
  revenue FLOAT64, updated_at TIMESTAMP NOT NULL
) PARTITION BY date OPTIONS (description = "GA4 par canal d'acquisition (CRO).");

-- 5d) CRO — snapshot du stock Shopify (remplacé à chaque run, pas de date).
CREATE TABLE IF NOT EXISTS `lpl_cockpit.shopify_inventory` (
  product_title STRING, status STRING, total_inventory INT64,
  product_type STRING, published BOOL, updated_at TIMESTAMP NOT NULL
) OPTIONS (description = "Stock courant Shopify (CRO / ruptures). published = publié sur Boutique en ligne.");

-- 5e) Meta — snapshot quotidien du budget paramétré par adset (suivi over/under-spend).
CREATE TABLE IF NOT EXISTS `lpl_cockpit.meta_adset_budget_daily` (
  date DATE NOT NULL, adset_id STRING, adset_name STRING, campaign_id STRING,
  daily_budget FLOAT64, lifetime_budget FLOAT64, status STRING, updated_at TIMESTAMP NOT NULL
) PARTITION BY date CLUSTER BY adset_id OPTIONS (description = "Budget adset Meta (ABO), snapshot/jour.");

-- 5f) Meta — snapshot quotidien du budget de campagne (CBO / Advantage Campaign Budget).
CREATE TABLE IF NOT EXISTS `lpl_cockpit.meta_campaign_budget_daily` (
  date DATE NOT NULL, campaign_id STRING, campaign_name STRING,
  daily_budget FLOAT64, lifetime_budget FLOAT64, status STRING, updated_at TIMESTAMP NOT NULL
) PARTITION BY date CLUSTER BY campaign_id OPTIONS (description = "Budget campagne Meta (CBO), snapshot/jour.");

-- 5g) Clients : (date locale, customer_id) par commande incluse. La 1re commande
--     de chaque client est déduite par MIN(date) -> new/returning figé à l'achat.
CREATE TABLE IF NOT EXISTS `lpl_cockpit.shopify_customer_orders` (
  date DATE NOT NULL, customer_id STRING, updated_at TIMESTAMP NOT NULL
) PARTITION BY date CLUSTER BY customer_id
OPTIONS (description = "Commandes-client (date, customer_id) pour le calcul new vs returning par cohorte.");

-- 5h) Répartition ventes montures par gamme de prix (unités), par jour / portée / gamme.
--     scope ∈ {optique, solaire} ; tier ∈ {29,49,69,89,autre} ; gamme = prix AVANT remise.
CREATE TABLE IF NOT EXISTS `lpl_cockpit.frame_price_mix_daily` (
  date DATE NOT NULL, scope STRING NOT NULL, tier STRING NOT NULL,
  units INT64 NOT NULL, updated_at TIMESTAMP NOT NULL
) PARTITION BY date CLUSTER BY scope
OPTIONS (description = "Ventes montures en unités par gamme de prix (29/49/69/89/autre) et portée (optique/solaire).");

-- 5i) Comptoir (lunettes NON ajustées à la vue) : unités Solaires vs Optiques, strict product_type.
CREATE TABLE IF NOT EXISTS `lpl_cockpit.frame_type_daily` (
  date DATE NOT NULL, type STRING NOT NULL, units INT64 NOT NULL, updated_at TIMESTAMP NOT NULL
) PARTITION BY date CLUSTER BY type
OPTIONS (description = "Comptoir : unités Solaires vs Monture Optique (strict product_type, hors à-la-vue).");

-- 5j) Estimation genre (H/F/U) par prénom de livraison, commandes web. (ESTIMATION)
CREATE TABLE IF NOT EXISTS `lpl_cockpit.gender_daily` (
  date DATE NOT NULL, gender STRING NOT NULL, units INT64 NOT NULL, updated_at TIMESTAMP NOT NULL
) PARTITION BY date CLUSTER BY gender
OPTIONS (description = "Estimation Homme/Femme (U=indéterminé) par prénom de livraison, commandes web.");

-- 5j-bis) Cache prénom -> genre (résolutions gender-guesser / Claude), pour ne pas recalculer.
CREATE TABLE IF NOT EXISTS `lpl_cockpit.gender_name_cache` (
  name STRING NOT NULL, gender STRING NOT NULL, updated_at TIMESTAMP NOT NULL
) OPTIONS (description = "Cache prénom normalisé -> H/F/U (gg + Claude).");

-- 5k) Sessions GA4 filtrées Pays-Bas (pour le CVR du segment NEDERLAND TEST).
-- ---------- PMax (Google Ads) : sources validées par google_pmax_probe (API v24) ----------
-- Catégories de requêtes par campagne PMax. Sert à mesurer la cannibalisation de marque
-- (part des conversions PMax venant de requêtes contenant le nom de marque).
-- ⚠️ SNAPSHOT, pas de granularité journalière : sur `campaign_search_term_insight`,
--    `segments.date` est filtrable mais PAS sélectionnable -> le résultat est un agrégat
--    sur [period_start, period_end]. Table remplacée intégralement à chaque run.
CREATE TABLE IF NOT EXISTS `lpl_cockpit.google_pmax_search_cat` (
  period_start     DATE,
  period_end       DATE,
  campaign_id      STRING,
  campaign_name    STRING,
  category_label   STRING,             -- '(non catégorisé)' = anonymisé par Google (faible volume)
  impressions      INT64,
  clicks           INT64,
  conversions      FLOAT64,
  conversion_value FLOAT64,
  updated_at       TIMESTAMP
) OPTIONS (description = "Catégories de requêtes PMax — agrégat période (API : pas de date sélectionnable).");

-- Perf par produit réel : concentration du spend, produits zombies.
CREATE TABLE IF NOT EXISTS `lpl_cockpit.google_pmax_products` (
  date             DATE,
  campaign_id      STRING,
  campaign_name    STRING,
  product_item_id  STRING,
  product_title    STRING,
  cost             FLOAT64,
  impressions      INT64,
  clicks           INT64,
  conversions      FLOAT64,
  conversion_value FLOAT64,
  updated_at       TIMESTAMP
) PARTITION BY date OPTIONS (description = "Perf produit PMax (shopping_performance_view).");

-- Perf + statut de diffusion par asset.
-- ⚠️ NON ADDITIF : une impression implique plusieurs assets, chacun porte le coût complet.
--    La somme des coûts d'assets DEPASSE le coût de la campagne. Usage COMPARATIF seulement
--    (entre assets de même field_type dans un même asset group).
CREATE TABLE IF NOT EXISTS `lpl_cockpit.google_pmax_assets` (
  date             DATE,
  campaign_id      STRING,
  campaign_name    STRING,
  asset_group_id   STRING,
  asset_group_name STRING,
  asset_resource   STRING,
  field_type       STRING,             -- HEADLINE / DESCRIPTION / MARKETING_IMAGE / ...
  status           STRING,
  primary_status   STRING,             -- NOT_ELIGIBLE / ELIGIBLE / LIMITED / ...
  cost             FLOAT64,
  impressions      INT64,
  clicks           INT64,
  conversions      FLOAT64,
  conversion_value FLOAT64,
  updated_at       TIMESTAMP
) PARTITION BY date OPTIONS (description = "Perf asset PMax. NON ADDITIF — comparatif uniquement.");

-- Snapshots (remplacés à chaque run).
CREATE TABLE IF NOT EXISTS `lpl_cockpit.google_pmax_asset_groups` (
  campaign_id        STRING,
  campaign_name      STRING,
  campaign_status    STRING,
  asset_group_id     STRING,
  asset_group_name   STRING,
  asset_group_status STRING,
  ad_strength        STRING,           -- POOR / AVERAGE / GOOD / EXCELLENT
  updated_at         TIMESTAMP
) OPTIONS (description = "Snapshot ad_strength / statut des asset groups PMax.");

CREATE TABLE IF NOT EXISTS `lpl_cockpit.google_pmax_campaigns` (
  campaign_id      STRING,
  campaign_name    STRING,
  channel_type     STRING,
  bidding_strategy STRING,
  target_roas      FLOAT64,            -- tROAS configuré (3.5 sur les PMax LPL)
  daily_budget     FLOAT64,
  updated_at       TIMESTAMP
) OPTIONS (description = "Snapshot config campagnes : stratégie, tROAS cible, budget.");

-- ---------- Performance produit (web + retail unifiés par shopify_variant_id) ----------
-- Catalogue : 1 ligne par variante. Snapshot remplacé à chaque run.
CREATE TABLE IF NOT EXISTS `lpl_cockpit.product_catalog` (
  shopify_variant_id  STRING,          -- clé de jointure web + retail
  shopify_product_id  STRING,
  sku                 STRING,
  title               STRING,
  product_type        STRING,
  category            STRING,           -- Optique / Solaire / Autre
  gender              STRING,           -- Homme / Femme / Mixte / Indéterminé (depuis tags)
  price               FLOAT64,
  status              STRING,           -- ACTIVE / ARCHIVED / DRAFT / UNLISTED
  published_online    BOOL,             -- publié sur « Online Store » (publishedAt non nul)
  tags                ARRAY<STRING>,
  date_tags           ARRAY<STRING>,    -- tags « datés » de collection/drop (filtres)
  updated_at          TIMESTAMP
) OPTIONS (description = "Catalogue produit par variante — métadonnées + genre + tags.");

-- Ventes « Braderies » (canal Syncio / email logistique@thebradery.com) : HORS CA principal,
-- suivies à part. CA net de remboursements, TTC, à la date de commande (Europe/Paris).
CREATE TABLE IF NOT EXISTS `lpl_cockpit.braderie_daily` (
  date        DATE,
  orders      INT64,               -- nb de commandes Braderie
  net_sales   FLOAT64,             -- CA net TTC (remboursements deduits)
  updated_at  TIMESTAMP
) PARTITION BY date OPTIONS (description = "Ventes Braderies (canal Syncio/thebradery), hors CA principal.");

-- Ventes web par produit (jour x variante), net remises + remboursements.
CREATE TABLE IF NOT EXISTS `lpl_cockpit.web_sales_daily` (
  date               DATE,
  shopify_variant_id STRING,
  sku                STRING,
  units              FLOAT64,
  revenue            FLOAT64,           -- CA net web réel
  updated_at         TIMESTAMP
) PARTITION BY date OPTIONS (description = "Ventes web par jour x variante (CA net réel).");

-- Ventes retail par produit (jour x variante). UNITÉS seulement — le CA se calcule
-- côté endpoint = units x prix catalogue (le DWH ne porte pas le CA net au niveau ligne).
CREATE TABLE IF NOT EXISTS `lpl_cockpit.retail_sales_daily` (
  date               DATE,
  shopify_variant_id STRING,
  units              FLOAT64
) OPTIONS (description = "Ventes retail par jour x variante (unites, CA = units x prix catalogue).");

-- Réassorts fournisseurs (Google Sheet « Order register »). Snapshot remplacé à chaque run.
CREATE TABLE IF NOT EXISTS `lpl_cockpit.product_incoming` (
  sku            STRING,
  ean            STRING,
  delivery_date  DATE,               -- Estimated delivery date (col K du sheet)
  updated_at     TIMESTAMP
) OPTIONS (description = "Reassorts fournisseurs : date de livraison estimee par SKU/EAN.");

-- Stock agrégé par variante (OOS web + retail). Snapshot remplacé à chaque run.
CREATE TABLE IF NOT EXISTS `lpl_cockpit.product_stock` (
  shopify_variant_id  STRING,
  warehouse_available INT64,            -- stock au 155 Charonne - Warehouse (web)
  total_inventory     INT64,
  retail_loc_total    INT64,            -- nb de boutiques suivies (hors warehouse + 4 exclues)
  retail_loc_oos      INT64,            -- nb de ces boutiques à stock 0
  web_available       BOOL,             -- ACTIVE ET publié Online Store ET warehouse > 0
  updated_at          TIMESTAMP
) OPTIONS (description = "Stock agrégé par variante : dispo web + OOS retail.");

CREATE TABLE IF NOT EXISTS `lpl_cockpit.ga4_nl_daily` (
  date DATE NOT NULL, sessions INT64, updated_at TIMESTAMP NOT NULL
) PARTITION BY date OPTIONS (description = "Sessions GA4 Test Europe (AT/DE/ES/IT/NL/PT agrégé), pour le CVR segment.");

-- Détail PAR PAYS du Test Europe (v1 « par pays » = demande). Sessions GA4 + commandes/CA Shopify.
CREATE TABLE IF NOT EXISTS `lpl_cockpit.ga4_country_daily` (
  date DATE, country STRING, sessions INT64, updated_at TIMESTAMP
) PARTITION BY date OPTIONS (description = "Sessions GA4 par pays (Test Europe AT/DE/ES/IT/NL/PT).");

CREATE TABLE IF NOT EXISTS `lpl_cockpit.shopify_country_daily` (
  date DATE, country STRING, orders INT64, ca FLOAT64, updated_at TIMESTAMP
) PARTITION BY date OPTIONS (description = "Commandes + CA net par pays de livraison (Test Europe).");

-- 5j-ter) Conso API Claude (module genre) — append-only, pour suivi coût sur le dashboard.
CREATE TABLE IF NOT EXISTS `lpl_cockpit.claude_usage` (
  ts TIMESTAMP NOT NULL, task STRING, names INT64, input_tokens INT64, output_tokens INT64
) PARTITION BY DATE(ts)
OPTIONS (description = "Tokens consommés par les appels Claude (estimation genre).");

-- Vue : new vs returning par grain (jour / semaine ISO / mois), customer-level, window-correct.
--   new      = clients dont la 1re commande tombe dans la période
--   returning = clients ayant commandé dans la période mais acquis AVANT la période
CREATE OR REPLACE VIEW `lpl_cockpit.shopify_customers_period` AS
WITH oc AS (
  SELECT DISTINCT date, customer_id FROM `lpl_cockpit.shopify_customer_orders`
  WHERE customer_id IS NOT NULL
),
fo AS (SELECT customer_id, MIN(date) AS first_date FROM oc GROUP BY customer_id),
j  AS (SELECT o.date, o.customer_id, f.first_date FROM oc o JOIN fo f USING (customer_id))
SELECT 'day' AS grain, FORMAT_DATE('%Y-%m-%d', date) AS period,
       COUNT(DISTINCT IF(date = first_date, customer_id, NULL)) AS new_customers,
       COUNT(DISTINCT IF(date > first_date, customer_id, NULL)) AS returning_customers
FROM j GROUP BY 2
UNION ALL
SELECT 'week', FORMAT_DATE('%G-W%V', date),
       COUNT(DISTINCT IF(DATE_TRUNC(date, ISOWEEK) = DATE_TRUNC(first_date, ISOWEEK), customer_id, NULL)),
       COUNT(DISTINCT IF(DATE_TRUNC(date, ISOWEEK) > DATE_TRUNC(first_date, ISOWEEK), customer_id, NULL))
FROM j GROUP BY 2
UNION ALL
SELECT 'month', FORMAT_DATE('%Y-%m', date),
       COUNT(DISTINCT IF(DATE_TRUNC(date, MONTH) = DATE_TRUNC(first_date, MONTH), customer_id, NULL)),
       COUNT(DISTINCT IF(DATE_TRUNC(date, MONTH) > DATE_TRUNC(first_date, MONTH), customer_id, NULL))
FROM j GROUP BY 2;

-- 5h) Clients — map customer_id -> email (clé de rapprochement web<->retail).
CREATE TABLE IF NOT EXISTS `lpl_cockpit.customer_emails` (
  customer_id STRING, email STRING, updated_at TIMESTAMP NOT NULL
) OPTIONS (description = "Map Shopify customer_id -> email.");
-- (retail_purchases et customers_period sont créées par CREATE OR REPLACE dans customers_metrics.)

-- ============================================================================
-- VUE D'ENSEMBLE — un point par jour, prête pour le front / Looker Studio.
-- COS blended = (dépense Meta + Google) / CA net Shopify.
-- ============================================================================
-- NOTE test NL : le PRINCIPAL est « hors Pays-Bas » à partir du 03/07/2026
--   (dépenses campagnes NL + CA livré NL sortis). Avant cette date, rien ne change.
--   Les colonnes *_nl exposent le segment NL pour le tableau « NEDERLAND TEST ».
CREATE OR REPLACE VIEW `lpl_cockpit.cockpit_daily` AS
WITH meta_acc AS (
  SELECT date, SUM(spend) AS meta_spend, SUM(purchase_value) AS meta_value,
    SUM(IF(REGEXP_CONTAINS(campaign_name, r'(^|[^A-Za-z])NL([^A-Za-z]|$)'), spend, 0)) AS meta_spend_nl
  FROM `lpl_cockpit.meta_daily` WHERE campaign_id IS NOT NULL GROUP BY date
),
google_acc AS (
  SELECT date, SUM(cost) AS google_spend, SUM(conversion_value) AS google_value,
    SUM(IF(REGEXP_CONTAINS(campaign_name, r'(^|[^A-Za-z])NL([^A-Za-z]|$)'), cost, 0)) AS google_spend_nl
  FROM `lpl_cockpit.google_daily` GROUP BY date
),
j AS (
  SELECT s.date, s.net_sales, s.orders,
    IF(s.date >= DATE '2026-07-03', COALESCE(s.net_sales_nl, 0), 0) AS ca_nl,
    IF(s.date >= DATE '2026-07-03', COALESCE(s.orders_nl, 0), 0)    AS ord_nl,
    COALESCE(m.meta_spend, 0)   AS meta_t,
    COALESCE(g.google_spend, 0) AS google_t,
    IF(s.date >= DATE '2026-07-03', COALESCE(m.meta_spend_nl, 0), 0)   AS meta_nl,
    IF(s.date >= DATE '2026-07-03', COALESCE(g.google_spend_nl, 0), 0) AS google_nl,
    COALESCE(m.meta_value, 0) AS meta_value, COALESCE(g.google_value, 0) AS google_value,
    t.sessions,
    IF(s.date >= DATE '2026-07-03', COALESCE(nlses.sessions, 0), 0) AS sess_nl,
    t.visitors, t.conversion_rate
  FROM `lpl_cockpit.shopify_orders_daily` s
  LEFT JOIN meta_acc   m USING (date)
  LEFT JOIN google_acc g USING (date)
  LEFT JOIN `lpl_cockpit.shopify_traffic_daily` t USING (date)
  LEFT JOIN `lpl_cockpit.ga4_nl_daily` nlses USING (date)
)
SELECT
  date,
  (net_sales - ca_nl)                       AS ca_shopify,
  (orders - ord_nl)                         AS orders,
  (meta_t - meta_nl)                        AS meta_spend,
  (google_t - google_nl)                    AS google_spend,
  (meta_t - meta_nl + google_t - google_nl) AS ad_spend_total,
  meta_value, google_value,
  SAFE_DIVIDE(meta_t - meta_nl + google_t - google_nl, net_sales - ca_nl) AS cos_blended,
  SAFE_DIVIDE(net_sales - ca_nl, meta_t - meta_nl + google_t - google_nl) AS roas_blended,
  GREATEST(0, sessions - sess_nl) AS sessions, visitors, conversion_rate,
  ca_nl                       AS ca_shopify_nl,
  ord_nl                      AS orders_nl,
  meta_nl                     AS meta_spend_nl,
  google_nl                   AS google_spend_nl,
  (meta_nl + google_nl)       AS ad_spend_nl,
  SAFE_DIVIDE(meta_nl + google_nl, ca_nl) AS cos_nl,
  SAFE_DIVIDE(ca_nl, meta_nl + google_nl) AS roas_nl
FROM j
ORDER BY date;
