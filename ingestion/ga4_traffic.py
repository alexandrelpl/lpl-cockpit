"""
Sessions & visiteurs depuis Google Analytics 4 -> BigQuery (shopify_traffic_daily).

GA4 filtre nativement les bots connus. Le CVR est ensuite calculé côté appli
(commandes LPL ÷ sessions GA4).

Auth : le compte de service du Job doit avoir un accès « Lecteur » à la propriété GA4
(GA4 Admin -> Gestion des accès à la propriété). API GA4 Data activée.

Config : GA4_PROPERTY_ID (le numéro de propriété, ex. 123456789).
"""

from __future__ import annotations
import os
from datetime import date, datetime, timedelta, timezone

import requests
import google.auth
from google.auth.transport.requests import Request as GAuthRequest
from google.cloud import bigquery

from ingestion import bq_io

BQ_PROJECT = os.environ["BQ_PROJECT"]
BQ_DATASET = os.environ.get("BQ_DATASET", "lpl_cockpit")
GA4_PID    = os.environ.get("GA4_PROPERTY_ID", "")


def _fetch(since: str, until: str) -> list[dict]:
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/analytics.readonly"])
    creds.refresh(GAuthRequest())
    url = f"https://analyticsdata.googleapis.com/v1beta/properties/{GA4_PID}:runReport"
    body = {
        "dateRanges": [{"startDate": since, "endDate": until}],
        "dimensions": [{"name": "date"}],
        "metrics": [{"name": "sessions"}, {"name": "totalUsers"}],
        "limit": 100000,
    }
    r = requests.post(url, json=body, headers={"Authorization": f"Bearer {creds.token}"}, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"GA4 Data API {r.status_code}: {r.text[:200]}")
    out = []
    for row in r.json().get("rows", []):
        d = row["dimensionValues"][0]["value"]          # "YYYYMMDD"
        iso = f"{d[0:4]}-{d[4:6]}-{d[6:8]}"
        sessions = int(float(row["metricValues"][0]["value"]))
        users = int(float(row["metricValues"][1]["value"]))
        out.append({"date": iso, "sessions": sessions, "visitors": users,
                    "conversion_rate": None})
    return out


def ingest(since: str, until: str) -> int:
    if not GA4_PID:
        raise RuntimeError("GA4_PROPERTY_ID non configuré")
    now = datetime.now(timezone.utc).isoformat()
    rows = [dict(r, updated_at=now) for r in _fetch(since, until)]
    client = bigquery.Client(project=BQ_PROJECT)
    table = f"{BQ_PROJECT}.{BQ_DATASET}.shopify_traffic_daily"
    n = bq_io.load_replace_window(client, table, rows, since, until)
    print(f"[ga4] {n} jours de trafic écrits dans shopify_traffic_daily")
    return n


def refresh(days: int = 40) -> int:
    today = date.today()
    return ingest((today - timedelta(days=days)).isoformat(), today.isoformat())


def backfill(days: int = 420) -> int:
    today = date.today()
    return ingest((today - timedelta(days=days)).isoformat(), today.isoformat())


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "backfill":
        backfill(int(sys.argv[2]) if len(sys.argv) > 2 else 420)
    else:
        refresh(int(sys.argv[1]) if len(sys.argv) > 1 else 40)
