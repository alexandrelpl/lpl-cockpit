"""
Ingestion légère pour la vue clients (new vs returning, cohorte).

On stocke seulement (date locale, customer_id) par commande INCLUSE (mêmes exclusions que le
CA). La date de 1re commande de chaque client est ensuite déduite par MIN(date) en SQL
(vue cockpit), donc PAS de sous-requête coûteuse ici. Le classement new/returning par grain
(jour / semaine ISO / mois) est figé à l'achat -> juste quelle que soit la fenêtre.

Exclusions (identiques à shopify_orders) :
  - pas de client rattaché (checkout invité sans compte)
  - tags alan / wholesale / b2b ; displayFinancialStatus VOIDED ; total == 0
  - sourceName hors {web, just, 5448991}
"""

from __future__ import annotations
import os
import socket
import time
from datetime import datetime, timedelta, timezone
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
# Pour le statut new/returning, on prend le parcours client réel = web + boutiques (POS),
# comme l'Apps Script clients. NB : c'est volontairement différent du filtre du CA (web only) :
# un client qui a d'abord acheté en boutique est un client FIDÈLE, pas un nouveau.
ALLOWED_SOURCES = {"web", "pos"}

_SESSION = requests.Session()
_THROTTLE: dict = {}
_LAST_COST = 30

QUERY = """
query ($query: String!, $cursor: String) {
  orders(first: 100, query: $query, after: $cursor, sortKey: CREATED_AT) {
    pageInfo { hasNextPage endCursor }
    nodes {
      createdAt
      email
      totalPriceSet { shopMoney { amount } }
      displayFinancialStatus
      tags
      sourceName
      customer { id }
    }
  }
}
"""


def _graphql(query_string, cursor):
    payload = {"query": QUERY, "variables": {"query": query_string, "cursor": cursor}}
    headers = {"X-Shopify-Access-Token": ACCESS_TOKEN, "Content-Type": "application/json"}
    last_err = None
    for attempt in range(8):
        try:
            r = _SESSION.post(GRAPHQL_URL, json=payload, headers=headers, timeout=(15, 90))
        except requests.exceptions.RequestException as e:
            last_err = e
            time.sleep(3 * (attempt + 1)); continue
        if r.status_code == 429:
            time.sleep(2 * (attempt + 1)); continue
        if r.status_code in (401, 403):
            raise RuntimeError(f"Shopify AUTH {r.status_code} : token/scope read_orders. {r.text[:200]}")
        if r.status_code >= 500:
            time.sleep(3 * (attempt + 1)); continue
        data = r.json()
        errs = data.get("errors")
        if errs:
            if isinstance(errs, str):
                raise RuntimeError(f"Shopify GraphQL (token/scope ?) : {errs}")
            if any(e.get("extensions", {}).get("code") == "THROTTLED" for e in errs):
                time.sleep(2 * (attempt + 1)); continue
            raise RuntimeError(f"Shopify GraphQL errors: {errs}")
        global _THROTTLE, _LAST_COST
        cost = data.get("extensions", {}).get("cost", {})
        _THROTTLE = cost.get("throttleStatus", _THROTTLE)
        _LAST_COST = cost.get("requestedQueryCost", _LAST_COST)
        return data["data"]["orders"]
    raise RuntimeError(f"Shopify GraphQL: échec après 8 tentatives. {last_err}")


def _included(node):
    # Logique Apps Script clients : client rattaché, total > 0, source web/pos. Pas d'autre exclusion.
    if not node.get("customer") or not node["customer"].get("id"):
        return None
    total = float((node.get("totalPriceSet") or {}).get("shopMoney", {}).get("amount") or 0)
    if total == 0 or (node.get("sourceName") or "").lower() not in ALLOWED_SOURCES:
        return None
    return node["customer"]["id"]


def ingest(since: str, until: str) -> int:
    query_string = f"created_at:>={since}T00:00:00Z AND created_at:<={until}T23:59:59Z"
    now = datetime.now(timezone.utc).isoformat()
    rows, cursor, has_next = [], None, True
    while has_next:
        orders = _graphql(query_string, cursor)
        for node in orders["nodes"]:
            cid = _included(node)
            if not cid:
                continue
            day = datetime.fromisoformat(node["createdAt"].replace("Z", "+00:00")) \
                .astimezone(LOCAL_TZ).strftime("%Y-%m-%d")
            email = (node.get("email") or "").strip().lower() or None
            rows.append({"date": day, "customer_id": cid, "email": email, "updated_at": now})
        has_next = orders["pageInfo"]["hasNextPage"]
        cursor = orders["pageInfo"]["endCursor"]
        avail, restore = _THROTTLE.get("currentlyAvailable"), _THROTTLE.get("restoreRate")
        if avail is not None and restore and avail < _LAST_COST:
            time.sleep(min(2.0, (_LAST_COST - avail) / restore))
        else:
            time.sleep(0.2)
    client = bigquery.Client(project=BQ_PROJECT)
    n = bq_io.load_replace_window(client, f"{BQ_PROJECT}.{BQ_DATASET}.shopify_customer_orders",
                                  rows, since, until)
    print(f"[customers] {len(rows)} commandes-client écrites ({since} -> {until})")
    return n


def refresh(days: int = 45) -> int:
    today = datetime.now(LOCAL_TZ).date()
    return ingest((today - timedelta(days=days)).isoformat(), today.isoformat())


def backfill(months: int = 24) -> int:
    today = datetime.now(LOCAL_TZ).date()
    start, total, ws = today - timedelta(days=int(months * 30.5)), 0, None
    ws = start
    while ws <= today:
        we = min(ws + timedelta(days=30), today)
        print(f"=== Clients {ws} -> {we} ===", flush=True)
        total += ingest(ws.isoformat(), we.isoformat())
        ws = we + timedelta(days=1)
    print(f"[customers] BACKFILL terminé")
    return total


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "backfill":
        backfill(int(sys.argv[2]) if len(sys.argv) > 2 else 24)
    else:
        refresh(int(sys.argv[2]) if len(sys.argv) > 2 else 45)
