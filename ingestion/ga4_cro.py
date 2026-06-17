"""
Ingestion GA4 pour l'onglet CRO : funnel, produits (item-level), canaux.

Réutilise l'OAuth utilisateur + la propriété de ga4_traffic. Trois tables :
- ga4_funnel_daily   : sessions, add_to_cart, checkout, achats, vues produit (par jour)
- ga4_items_daily    : par produit (vues, ATC, achats, CA)
- ga4_channels_daily : par canal d'acquisition (sessions, achats, CA)

Fenêtre courte au quotidien (l'historique plus ancien reste figé en base = continuité).
"""

from __future__ import annotations
from datetime import date, datetime, timedelta, timezone

import requests
from google.cloud import bigquery

from ingestion import bq_io
from ingestion.ga4_traffic import _token, GA4_PID, BQ_PROJECT, BQ_DATASET


def _report(dimensions: list[str], metrics: list[str], since: str, until: str) -> list[dict]:
    url = f"https://analyticsdata.googleapis.com/v1beta/properties/{GA4_PID}:runReport"
    body = {
        "dateRanges": [{"startDate": since, "endDate": until}],
        "dimensions": [{"name": d} for d in dimensions],
        "metrics": [{"name": m} for m in metrics],
        "limit": 200000,
    }
    r = requests.post(url, json=body, headers={"Authorization": f"Bearer {_token()}"}, timeout=120)
    if r.status_code != 200:
        raise RuntimeError(f"GA4 Data API {r.status_code}: {r.text[:300]}")
    return r.json().get("rows", [])


def _iso(d: str) -> str:
    return f"{d[0:4]}-{d[4:6]}-{d[6:8]}"


def _i(v) -> int:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _f(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _write(table: str, rows: list[dict], since: str, until: str):
    client = bigquery.Client(project=BQ_PROJECT)
    return bq_io.load_replace_window(client, f"{BQ_PROJECT}.{BQ_DATASET}.{table}", rows, since, until)


def funnel(since: str, until: str) -> int:
    now = datetime.now(timezone.utc).isoformat()
    rows = []
    for r in _report(["date"], ["sessions", "addToCarts", "checkouts", "ecommercePurchases", "itemsViewed"], since, until):
        v = r["metricValues"]
        rows.append({"date": _iso(r["dimensionValues"][0]["value"]),
                     "sessions": _i(v[0]["value"]), "add_to_carts": _i(v[1]["value"]),
                     "checkouts": _i(v[2]["value"]), "purchases": _i(v[3]["value"]),
                     "item_views": _i(v[4]["value"]), "updated_at": now})
    n = _write("ga4_funnel_daily", rows, since, until)
    print(f"[ga4-funnel] {n} jours écrits")
    return n


def items(since: str, until: str) -> int:
    now = datetime.now(timezone.utc).isoformat()
    rows = []
    for r in _report(["date", "itemName"], ["itemsViewed", "itemsAddedToCart", "itemsPurchased", "itemRevenue"], since, until):
        d = r["dimensionValues"]; v = r["metricValues"]
        rows.append({"date": _iso(d[0]["value"]), "item_name": d[1]["value"] or "(non défini)",
                     "views": _i(v[0]["value"]), "add_to_carts": _i(v[1]["value"]),
                     "purchases": _i(v[2]["value"]), "revenue": _f(v[3]["value"]), "updated_at": now})
    n = _write("ga4_items_daily", rows, since, until)
    print(f"[ga4-items] {n} lignes produit x jour écrites")
    return n


def channels(since: str, until: str) -> int:
    now = datetime.now(timezone.utc).isoformat()
    rows = []
    for r in _report(["date", "sessionDefaultChannelGroup"], ["sessions", "ecommercePurchases", "purchaseRevenue"], since, until):
        d = r["dimensionValues"]; v = r["metricValues"]
        rows.append({"date": _iso(d[0]["value"]), "channel": d[1]["value"] or "(other)",
                     "sessions": _i(v[0]["value"]), "purchases": _i(v[1]["value"]),
                     "revenue": _f(v[2]["value"]), "updated_at": now})
    n = _write("ga4_channels_daily", rows, since, until)
    print(f"[ga4-channels] {n} lignes canal x jour écrites")
    return n


def refresh(days: int = 10) -> dict:
    today = date.today()
    s, u = (today - timedelta(days=days)).isoformat(), today.isoformat()
    return {"funnel": funnel(s, u), "items": items(s, u), "channels": channels(s, u)}


def backfill(days: int = 90) -> dict:
    today = date.today()
    s, u = (today - timedelta(days=days)).isoformat(), today.isoformat()
    return {"funnel": funnel(s, u), "items": items(s, u), "channels": channels(s, u)}


if __name__ == "__main__":
    import sys
    print(backfill(int(sys.argv[2]) if len(sys.argv) > 2 else 90) if (len(sys.argv) > 1 and sys.argv[1] == "backfill")
          else refresh(int(sys.argv[1]) if len(sys.argv) > 1 else 10))
