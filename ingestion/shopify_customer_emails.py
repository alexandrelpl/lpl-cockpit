"""
Map customer_id -> email depuis Shopify (clé de rapprochement web <-> retail).

Économe : on ne re-fetch pas les commandes, juste les clients (id, email). On joint ensuite
shopify_customer_orders.customer_id à cette map pour obtenir l'email par commande web.
NB : email = donnée client protégée -> l'app Shopify doit avoir l'accès "protected customer data".
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
  customers(first: 250, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    nodes { id email }
  }
}
"""


def _gql(cursor):
    for attempt in range(6):
        r = _SESSION.post(GRAPHQL_URL, json={"query": QUERY, "variables": {"cursor": cursor}},
                          headers={"X-Shopify-Access-Token": ACCESS_TOKEN}, timeout=60)
        data = r.json()
        errs = data.get("errors")
        if errs:
            if isinstance(errs, str):
                raise RuntimeError(f"Shopify customers (scope email ?) : {errs}")
            if any(e.get("extensions", {}).get("code") == "THROTTLED" for e in errs):
                time.sleep(2 * (attempt + 1)); continue
            raise RuntimeError(f"Shopify customers: {errs}")
        return data["data"]["customers"]
    raise RuntimeError("Shopify customers: throttling persistant")


def refresh() -> int:
    now = datetime.now(timezone.utc).isoformat()
    rows, cursor, has_next = [], None, True
    while has_next:
        c = _gql(cursor)
        for n in c["nodes"]:
            rows.append({"customer_id": n.get("id"), "email": n.get("email"), "updated_at": now})
        has_next = c["pageInfo"]["hasNextPage"]
        cursor = c["pageInfo"]["endCursor"]
        time.sleep(0.2)
    client = bigquery.Client(project=BQ_PROJECT)
    n = bq_io.load_replace_all(client, f"{BQ_PROJECT}.{BQ_DATASET}.customer_emails", rows)
    with_email = sum(1 for r in rows if r["email"])
    print(f"[cust-emails] {n} clients, dont {with_email} avec email")
    return n


if __name__ == "__main__":
    refresh()
