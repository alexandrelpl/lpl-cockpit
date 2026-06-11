"""
Ingestion trafic & conversion Shopify -> BigQuery (table shopify_traffic_daily).

Utilise ShopifyQL via l'API Admin GraphQL (shopifyqlQuery). Les 'sessions' Shopify
sont déjà filtrées des bots nativement.

NB : selon la version d'API / le plan, le champ shopifyqlQuery peut nécessiter un
ajustement de forme de réponse. Testé sur 2024-01 (Shopify Plus). Voir DEPLOY.md.
"""

from __future__ import annotations
import os
import time
from datetime import datetime, timedelta, timezone

import requests
from google.cloud import bigquery

SHOP_URL     = os.environ["SHOPIFY_SHOP_URL"]
ACCESS_TOKEN = os.environ["SHOPIFY_ACCESS_TOKEN"]
API_VERSION  = os.environ.get("SHOPIFY_API_VERSION", "2024-01")
BQ_PROJECT   = os.environ["BQ_PROJECT"]
BQ_DATASET   = os.environ.get("BQ_DATASET", "lpl_cockpit")

GRAPHQL_URL = f"https://{SHOP_URL}/admin/api/{API_VERSION}/graphql.json"

WRAPPER = """
query ($q: String!) {
  shopifyqlQuery(query: $q) {
    __typename
    ... on TableResponse {
      tableData { columns { name } rowData }
    }
    parseErrors { code message }
  }
}
"""


def _fetch(since: str, until: str) -> list[list]:
    sql = (f"FROM sessions SHOW sessions, online_store_visitors, conversion_rate "
           f"TIMESERIES day SINCE {since} UNTIL {until}")
    r = requests.post(
        GRAPHQL_URL,
        json={"query": WRAPPER, "variables": {"q": sql}},
        headers={"X-Shopify-Access-Token": ACCESS_TOKEN, "Content-Type": "application/json"},
        timeout=60,
    )
    data = r.json()["data"]["shopifyqlQuery"]
    if data.get("parseErrors"):
        raise RuntimeError(f"ShopifyQL parse errors: {data['parseErrors']}")
    return data["tableData"]["rowData"]


def ingest(since: str, until: str) -> int:
    rows_raw = _fetch(since, until)
    now = datetime.now(timezone.utc).isoformat()
    rows = []
    for row in rows_raw:
        # ordre colonnes : day, sessions, online_store_visitors, conversion_rate
        rows.append({
            "date": row[0][:10],
            "sessions": int(float(row[1])) if row[1] not in (None, "") else None,
            "visitors": int(float(row[2])) if row[2] not in (None, "") else None,
            "conversion_rate": float(row[3]) if row[3] not in (None, "") else None,
            "updated_at": now,
        })

    client = bigquery.Client(project=BQ_PROJECT)
    table = f"{BQ_PROJECT}.{BQ_DATASET}.shopify_traffic_daily"
    client.query(
        f"DELETE FROM `{table}` WHERE date BETWEEN @s AND @u",
        job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("s", "DATE", since),
            bigquery.ScalarQueryParameter("u", "DATE", until),
        ]),
    ).result()
    if rows:
        errors = client.insert_rows_json(table, rows)
        if errors:
            raise RuntimeError(f"BigQuery insert errors: {errors}")
    print(f"[traffic] {len(rows)} jours écrits")
    return len(rows)


def refresh(days: int = 40) -> int:
    today = datetime.now(timezone.utc).date()
    since = (today - timedelta(days=days)).isoformat()
    return ingest(since, today.isoformat())


if __name__ == "__main__":
    import sys
    refresh(int(sys.argv[1]) if len(sys.argv) > 1 else 40)
