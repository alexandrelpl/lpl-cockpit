"""
Répartition des ventes de montures par gamme de prix (29 / 49 / 69 / 89 € + Autre),
en UNITÉS, par jour, par portée (optique / solaire).

- Gamme = `originalUnitPriceSet` (prix AVANT remise) -> stable même en promo (1 achetée=1 offerte,
  soldes), conforme au choix « gamme standard de la monture ».
- Portée : Solaires* -> 'solaire' ; Monture Optique / Dumb Lens / Optiques* -> 'optique'.
  (« Dumb Lens » = monture de vue vendue via le configurateur de verres.)
- Exclus : verres (Verre à la vue / Verre solaire), « Mont client », lignes à 0 € d'origine,
  accessoires / cartes cadeaux.
La portée « optique + solaire » est recalculée à la lecture (somme), pas stockée.
"""

from __future__ import annotations
import os
import socket
import time
from collections import defaultdict
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
ALLOWED_SOURCES = {"web", "pos", "just", "5448991"}
EXCLUDE_ORDER_TAGS = {"alan", "wholesale", "b2b"}

TIERS = {29: "29", 49: "49", 69: "69", 89: "89"}

_SESSION = requests.Session()
_THROTTLE: dict = {}
_LAST_COST = 60

QUERY = """
query ($query: String!, $cursor: String) {
  orders(first: 100, query: $query, after: $cursor, sortKey: CREATED_AT) {
    pageInfo { hasNextPage endCursor }
    nodes {
      createdAt
      displayFinancialStatus
      sourceName
      tags
      lineItems(first: 50) {
        nodes {
          quantity
          originalUnitPriceSet { shopMoney { amount } }
          product { productType }
        }
      }
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
            last_err = e; time.sleep(3 * (attempt + 1)); continue
        if r.status_code == 429:
            time.sleep(2 * (attempt + 1)); continue
        if r.status_code in (401, 403):
            raise RuntimeError(f"Shopify AUTH {r.status_code} : token/scope read_orders. {r.text[:200]}")
        if r.status_code >= 500:
            time.sleep(3 * (attempt + 1)); continue
        data = r.json()
        errs = data.get("errors")
        if errs:
            if any((e.get("extensions", {}) or {}).get("code") == "THROTTLED" for e in errs
                   if isinstance(e, dict)):
                time.sleep(2 * (attempt + 1)); continue
            raise RuntimeError(f"Shopify GraphQL errors: {errs}")
        global _THROTTLE, _LAST_COST
        cost = data.get("extensions", {}).get("cost", {})
        _THROTTLE = cost.get("throttleStatus", _THROTTLE)
        _LAST_COST = cost.get("requestedQueryCost", _LAST_COST)
        return data["data"]["orders"]
    raise RuntimeError(f"Shopify GraphQL: échec après 8 tentatives. {last_err}")


def _scope(product_type: str) -> str | None:
    t = (product_type or "").strip().lower()
    if t.startswith("solaires"):
        return "solaire"
    if t.startswith("optiques") or t in ("monture optique", "dumb lens"):
        return "optique"
    return None   # verres, accessoires, etc.


def _tier(amount: float) -> str:
    return TIERS.get(int(round(amount)), "autre")


def _strict_type(product_type: str) -> str | None:
    # « Comptoir » = lunettes NON ajustées à la vue. product_type strict :
    #   Solaires -> solaire ; Monture Optique -> optique.
    # (Les montures montées avec verres correcteurs passent en « Dumb Lens » -> exclues ici.)
    t = (product_type or "").strip().lower()
    if t == "solaires":
        return "solaire"
    if t == "monture optique":
        return "optique"
    return None


def _order_included(node) -> bool:
    if (node.get("displayFinancialStatus") or "").upper() == "VOIDED":
        return False
    if (node.get("sourceName") or "").lower() not in ALLOWED_SOURCES:
        return False
    tags = [t.strip().lower() for t in (node.get("tags") or [])]
    if any(x in tags for x in EXCLUDE_ORDER_TAGS):
        return False
    return True


def ingest(since: str, until: str) -> int:
    query_string = f"created_at:>={since}T00:00:00Z AND created_at:<={until}T23:59:59Z"
    now = datetime.now(timezone.utc).isoformat()
    # counts[(date, scope, tier)] = units (mix par gamme)
    counts: dict = defaultdict(int)
    # type_counts[(date, type)] = units (Comptoir strict : solaire / optique)
    type_counts: dict = defaultdict(int)
    cursor, has_next = None, True
    while has_next:
        orders = _graphql(query_string, cursor)
        for node in orders["nodes"]:
            if not _order_included(node):
                continue
            day = datetime.fromisoformat(node["createdAt"].replace("Z", "+00:00")) \
                .astimezone(LOCAL_TZ).strftime("%Y-%m-%d")
            for li in node["lineItems"]["nodes"]:
                ptype = (li.get("product") or {}).get("productType")
                qty = int(li.get("quantity") or 0)
                if qty <= 0:
                    continue
                # Comptoir (strict) : compte les unités quel que soit le prix (promo/1+1 incluses).
                st = _strict_type(ptype)
                if st:
                    type_counts[(day, st)] += qty
                # Mix par gamme : portée large + prix d'origine > 0.
                scope = _scope(ptype)
                if not scope:
                    continue
                amount = float((li.get("originalUnitPriceSet") or {}).get("shopMoney", {}).get("amount") or 0)
                if amount <= 0:
                    continue   # « Mont client », lignes offertes sans prix d'origine
                counts[(day, scope, _tier(amount))] += qty
        has_next = orders["pageInfo"]["hasNextPage"]
        cursor = orders["pageInfo"]["endCursor"]
        avail, restore = _THROTTLE.get("currentlyAvailable"), _THROTTLE.get("restoreRate")
        if avail is not None and restore and avail < _LAST_COST:
            time.sleep(min(2.0, (_LAST_COST - avail) / restore))
        else:
            time.sleep(0.2)

    client = bigquery.Client(project=BQ_PROJECT)
    rows = [{"date": d, "scope": s, "tier": t, "units": u, "updated_at": now}
            for (d, s, t), u in counts.items()]
    n = bq_io.load_replace_window(client, f"{BQ_PROJECT}.{BQ_DATASET}.frame_price_mix_daily",
                                  rows, since, until)
    type_rows = [{"date": d, "type": ty, "units": u, "updated_at": now}
                 for (d, ty), u in type_counts.items()]
    bq_io.load_replace_window(client, f"{BQ_PROJECT}.{BQ_DATASET}.frame_type_daily",
                              type_rows, since, until)
    print(f"[price_mix] {len(rows)} lignes gamme + {len(type_rows)} lignes comptoir écrites ({since} -> {until})")
    return n


def refresh(days: int = 45) -> int:
    today = datetime.now(LOCAL_TZ).date()
    return ingest((today - timedelta(days=days)).isoformat(), today.isoformat())


def backfill(months: int = 24) -> int:
    today = datetime.now(LOCAL_TZ).date()
    ws, total = today - timedelta(days=int(months * 30.5)), 0
    while ws <= today:
        we = min(ws + timedelta(days=30), today)
        print(f"=== Price mix {ws} -> {we} ===", flush=True)
        total += ingest(ws.isoformat(), we.isoformat())
        ws = we + timedelta(days=1)
    print("[price_mix] BACKFILL terminé")
    return total


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "backfill":
        backfill(int(sys.argv[2]) if len(sys.argv) > 2 else 24)
    else:
        refresh(int(sys.argv[2]) if len(sys.argv) > 2 else 45)
