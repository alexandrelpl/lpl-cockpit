"""
Sessions GA4 filtrées Pays-Bas (countryId = NL) -> table ga4_nl_daily (date, sessions).
Sert au CVR du segment « NEDERLAND TEST ». Réutilise l'OAuth/propriété de ga4_traffic.
"""

from __future__ import annotations
from datetime import date, datetime, timedelta, timezone

import requests
from google.cloud import bigquery

from ingestion import bq_io
from ingestion.ga4_traffic import _token, GA4_PID, BQ_PROJECT, BQ_DATASET


def _fetch(since: str, until: str) -> list[dict]:
    url = f"https://analyticsdata.googleapis.com/v1beta/properties/{GA4_PID}:runReport"
    body = {
        "dateRanges": [{"startDate": since, "endDate": until}],
        "dimensions": [{"name": "date"}],
        "metrics": [{"name": "sessions"}],
        "dimensionFilter": {"filter": {"fieldName": "countryId",
                                       "stringFilter": {"value": "NL"}}},
        "limit": 100000,
    }
    r = requests.post(url, json=body, headers={"Authorization": f"Bearer {_token()}"}, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"GA4 Data API {r.status_code}: {r.text[:200]}")
    now = datetime.now(timezone.utc).isoformat()
    out = []
    for row in r.json().get("rows", []):
        d = row["dimensionValues"][0]["value"]
        out.append({"date": f"{d[0:4]}-{d[4:6]}-{d[6:8]}",
                    "sessions": int(float(row["metricValues"][0]["value"])), "updated_at": now})
    return out


def ingest(since: str, until: str) -> int:
    if not GA4_PID:
        raise RuntimeError("GA4_PROPERTY_ID non configuré")
    rows = _fetch(since, until)
    client = bigquery.Client(project=BQ_PROJECT)
    n = bq_io.load_replace_window(client, f"{BQ_PROJECT}.{BQ_DATASET}.ga4_nl_daily", rows, since, until)
    print(f"[ga4-nl] {n} jours de sessions NL écrits")
    return n


def refresh(days: int = 40) -> int:
    today = date.today()
    return ingest((today - timedelta(days=days)).isoformat(), today.isoformat())


def backfill(days: int = 90) -> int:
    today = date.today()
    return ingest((today - timedelta(days=days)).isoformat(), today.isoformat())


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "backfill":
        backfill(int(sys.argv[2]) if len(sys.argv) > 2 else 90)
    else:
        refresh(int(sys.argv[1]) if len(sys.argv) > 1 else 40)
