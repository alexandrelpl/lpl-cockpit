"""
Ventes WEB par produit -> BigQuery (table web_sales_daily), par jour x variante.

Crawl des lignes de commande Shopify (fenêtre glissante). Net des remises (discountedTotal)
et des remboursements (refundLineItems). Mêmes exclusions que shopify_orders (tags
alan/wholesale/b2b, VOIDED, sourceName hors {web, just, 5448991}).

Clé = shopify_variant_id (jointure avec le catalogue et le retail).
"""

from __future__ import annotations
import os
import socket
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from google.cloud import bigquery

from ingestion import bq_io

socket.setdefaulttimeout(120)

SHOP_URL     = os.environ["SHOPIFY_SHOP_URL"]
ACCESS_TOKEN = os.environ["SHOPIFY_ACCESS_TOKEN"].strip()
API_VERSION  = os.environ.get("SHOPIFY_API_VERSION", "2024-01")
BQ_PROJECT   = os.environ["BQ_PROJECT"]
BQ_DATASET   = os.environ.get("BQ_DATASET", "lpl_cockpit")
LOCAL_TZ     = ZoneInfo(os.environ.get("TIMEZONE", "Europe/Paris"))

GRAPHQL_URL = f"https://{SHOP_URL}/admin/api/{API_VERSION}/graphql.json"
ALLOWED_SOURCES = {"web", "just", "5448991"}
_SESSION = requests.Session()

QUERY = """
query ($query: String!, $cursor: String) {
  orders(first: 20, query: $query, after: $cursor, sortKey: CREATED_AT) {
    pageInfo { hasNextPage endCursor }
    nodes {
      createdAt displayFinancialStatus tags sourceName
      lineItems(first: 50) {
        nodes {
          quantity sku
          variant { id }
          discountedTotalSet { shopMoney { amount } }
        }
      }
      refunds {
        refundLineItems(first: 50) {
          nodes {
            quantity
            lineItem { sku variant { id } }
            subtotalSet { shopMoney { amount } }
          }
        }
      }
    }
  }
}
"""


def _graphql(query_string, cursor):
    payload = {"query": QUERY, "variables": {"query": query_string, "cursor": cursor}}
    headers = {"X-Shopify-Access-Token": ACCESS_TOKEN, "Content-Type": "application/json"}
    for attempt in range(8):
        try:
            r = _SESSION.post(GRAPHQL_URL, json=payload, headers=headers, timeout=(15, 90))
        except requests.exceptions.RequestException as e:
            print(f"  [line-items] réseau (tentative {attempt+1}/8): {e}", flush=True)
            time.sleep(3 * (attempt + 1)); continue
        if r.status_code == 429:
            time.sleep(2 * (attempt + 1)); continue
        if r.status_code in (401, 403):
            raise RuntimeError(f"Shopify AUTH {r.status_code}: scope read_orders ? {r.text[:200]}")
        if r.status_code >= 500:
            time.sleep(3 * (attempt + 1)); continue
        data = r.json()
        errs = data.get("errors")
        if errs:
            if isinstance(errs, str):
                raise RuntimeError(f"Shopify GraphQL (token/scope ?): {errs}")
            if any((e.get("extensions") or {}).get("code") == "THROTTLED" for e in errs):
                time.sleep(2 * (attempt + 1)); continue
            raise RuntimeError(f"Shopify GraphQL errors: {errs}")
        return data["data"]["orders"]
    raise RuntimeError("Shopify GraphQL: échec après 8 tentatives")


def _vid(node):
    v = (node.get("variant") or {})
    return (v.get("id") or "").rsplit("/", 1)[-1] or None


def _excluded(order) -> bool:
    tags = order.get("tags") or ""
    tags = " ".join(tags).lower() if isinstance(tags, list) else str(tags).lower()
    if "alan" in tags or "wholesale" in tags or "b2b" in tags:
        return True
    if order.get("displayFinancialStatus") == "VOIDED":
        return True
    if (order.get("sourceName") or "").lower() not in ALLOWED_SOURCES:
        return True
    return False


def ingest(since_dt: str, until_dt: str, since_date: str, until_date: str) -> int:
    # agg[(date, vid)] = {"sku":..., "units":x, "revenue":y}
    agg: dict = defaultdict(lambda: {"sku": None, "units": 0.0, "revenue": 0.0})
    query_string = f"created_at:>='{since_dt}' created_at:<='{until_dt}'"
    cursor, has_next, npages = None, True, 0
    while has_next:
        o = _graphql(query_string, cursor)
        for order in o["nodes"]:
            if _excluded(order):
                continue
            d = datetime.fromisoformat(order["createdAt"].replace("Z", "+00:00")) \
                .astimezone(LOCAL_TZ).date().isoformat()
            for li in (order.get("lineItems") or {}).get("nodes", []):
                vid = _vid(li)
                if not vid:
                    continue
                amt = float((li.get("discountedTotalSet") or {}).get("shopMoney", {}).get("amount") or 0)
                k = (d, vid)
                agg[k]["units"] += (li.get("quantity") or 0)
                agg[k]["revenue"] += amt
                agg[k]["sku"] = agg[k]["sku"] or li.get("sku")
            for rf in (order.get("refunds") or []):
                for rli in (rf.get("refundLineItems") or {}).get("nodes", []):
                    vid = _vid(rli.get("lineItem") or {})
                    if not vid:
                        continue
                    amt = float((rli.get("subtotalSet") or {}).get("shopMoney", {}).get("amount") or 0)
                    k = (d, vid)
                    agg[k]["units"] -= (rli.get("quantity") or 0)
                    agg[k]["revenue"] -= amt
        has_next = o["pageInfo"]["hasNextPage"]
        cursor = o["pageInfo"]["endCursor"]
        npages += 1
        # cadence douce pour ménager le quota GraphQL
        time.sleep(0.25)

    now = datetime.now(tz=LOCAL_TZ).isoformat()
    rows = [{"date": d, "shopify_variant_id": vid, "sku": v["sku"],
             "units": round(v["units"], 3), "revenue": round(v["revenue"], 2), "updated_at": now}
            for (d, vid), v in agg.items()
            # on ne garde pas les lignes entièrement remboursées / nulles
            if v["units"] > 0 or v["revenue"] != 0]
    client = bigquery.Client(project=BQ_PROJECT)
    n = bq_io.load_replace_window(client, f"{BQ_PROJECT}.{BQ_DATASET}.web_sales_daily",
                                  rows, since_date, until_date)
    print(f"[web-sales] {n} lignes (jour x variante) sur {npages} page(s) de commandes")
    return n


def refresh(days: int = 40) -> int:
    today = date.today()
    since = today - timedelta(days=days)
    return ingest(since.isoformat() + "T00:00:00", today.isoformat() + "T23:59:59",
                  since.isoformat(), today.isoformat())


if __name__ == "__main__":
    import sys
    print(refresh(int(sys.argv[1]) if len(sys.argv) > 1 else 40))
