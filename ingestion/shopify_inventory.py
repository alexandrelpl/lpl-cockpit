"""
Snapshot du stock Shopify -> BigQuery (table shopify_inventory, remplacée à chaque run).

Sert à détecter les ruptures réelles (à croiser avec l'écoulement produit dans l'onglet CRO :
un best-seller en rupture = priorité, vs un simple repli de trafic).
"""

from __future__ import annotations
import os
import time
from datetime import datetime, timezone

import requests
from google.cloud import bigquery

from ingestion import bq_io

SHOP_URL     = os.environ["SHOPIFY_SHOP_URL"]
ACCESS_TOKEN = os.environ["SHOPIFY_ACCESS_TOKEN"].strip()
API_VERSION  = os.environ.get("SHOPIFY_API_VERSION", "2024-01")
BQ_PROJECT   = os.environ["BQ_PROJECT"]
BQ_DATASET   = os.environ.get("BQ_DATASET", "lpl_cockpit")

GRAPHQL_URL = f"https://{SHOP_URL}/admin/api/{API_VERSION}/graphql.json"
_SESSION = requests.Session()

QUERY = """
query ($cursor: String) {
  products(first: 200, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    nodes { title status totalInventory productType publishedAt }
  }
}
"""


def _graphql(cursor):
    for attempt in range(6):
        r = _SESSION.post(GRAPHQL_URL, json={"query": QUERY, "variables": {"cursor": cursor}},
                          headers={"X-Shopify-Access-Token": ACCESS_TOKEN}, timeout=60)
        data = r.json()
        if data.get("errors"):
            if any(e.get("extensions", {}).get("code") == "THROTTLED" for e in data["errors"]):
                time.sleep(2 * (attempt + 1)); continue
            raise RuntimeError(f"Shopify GraphQL: {data['errors']}")
        return data["data"]["products"]
    raise RuntimeError("Shopify GraphQL: throttling persistant")


def refresh() -> int:
    now = datetime.now(timezone.utc).isoformat()
    rows, cursor, has_next = [], None, True
    while has_next:
        p = _graphql(cursor)
        for n in p["nodes"]:
            # publishedAt non nul <=> publié sur le canal "Boutique en ligne" (Online Store).
            rows.append({"product_title": n.get("title"), "status": n.get("status"),
                         "total_inventory": n.get("totalInventory"),
                         "product_type": n.get("productType"),
                         "published": n.get("publishedAt") is not None,
                         "updated_at": now})
        has_next = p["pageInfo"]["hasNextPage"]
        cursor = p["pageInfo"]["endCursor"]
        time.sleep(0.3)
    client = bigquery.Client(project=BQ_PROJECT)
    n = bq_io.load_replace_all(client, f"{BQ_PROJECT}.{BQ_DATASET}.shopify_inventory", rows)
    print(f"[inventory] {n} produits écrits dans shopify_inventory")
    return n


if __name__ == "__main__":
    refresh()
