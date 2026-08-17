"""
Ventes RETAIL par produit -> BigQuery (table retail_sales_daily), par jour x variante.

Source = DWH retail (transaction_details_visits), agrégé par shopify_variant_id.
Tout en SQL (économe). On ne garde QUE les lignes rattachées à une variante Shopify
(les verres/prestations sans shopify_variant_id sont hors périmètre « performance produit »).

CA retail = SUM(ttc_theoretical_price_adjusted) = « Adjusted Price » TTC du DWH : le net
réparti sur les articles du panier (remise 1+1 étalée), même base TTC que le CA web Shopify
(boutique en taxesIncluded=true). Remplace l'ancienne estimation « unités × prix catalogue ».

On COMPTE les lignes revenue_equal_0 = true : ce sont les paires OFFERTES du 1+1, de la vraie
demande/volume produit (comme côté web), et elles portent leur juste part en adjusted.
"""

from __future__ import annotations
import os

from google.cloud import bigquery

BQ_PROJECT   = os.environ["BQ_PROJECT"]
BQ_DATASET   = os.environ.get("BQ_DATASET", "lpl_cockpit")
BQ_LOCATION  = os.environ.get("BQ_LOCATION", "EU")
RETAIL_TABLE = os.environ.get("RETAIL_TABLE",
                              "stable-splicer-294813.dwh_datasource_sales.transaction_details_visits")
RETAIL_DAYS  = int(os.environ.get("RETAIL_PRODUCT_DAYS", "120"))


def refresh() -> int:
    sql = f"""
CREATE OR REPLACE TABLE `{BQ_PROJECT}.{BQ_DATASET}.retail_sales_daily` AS
SELECT
  DATE(package_detail_purchase_date) AS date,
  shopify_variant_id,
  SUM(product_quantity) AS units,
  ROUND(SUM(ttc_theoretical_price_adjusted), 2) AS revenue
FROM `{RETAIL_TABLE}`
WHERE shopify_variant_id IS NOT NULL
  AND shopify_variant_id NOT IN ('', '-')
  AND product_quantity > 0
  AND package_detail_purchase_date IS NOT NULL
  AND DATE(package_detail_purchase_date) >= DATE_SUB(CURRENT_DATE(), INTERVAL {RETAIL_DAYS} DAY)
GROUP BY 1, 2
"""
    client = bigquery.Client(project=BQ_PROJECT, location=BQ_LOCATION)
    client.query(sql).result()
    n = client.query(
        f"SELECT COUNT(*) c FROM `{BQ_PROJECT}.{BQ_DATASET}.retail_sales_daily`"
    ).result()
    cnt = list(n)[0]["c"]
    print(f"[retail-sales] {cnt} lignes (jour x variante) sur {RETAIL_DAYS} j")
    return cnt


if __name__ == "__main__":
    print(refresh())
