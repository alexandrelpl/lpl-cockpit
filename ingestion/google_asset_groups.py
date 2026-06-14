"""
Ingestion Google Ads — performance par ASSET GROUP (groupes de composants PMax).

GAQL sur la ressource `asset_group` segmentée par jour -> table google_asset_group_daily.
Réutilise le client OAuth de google_ads.py (mêmes secrets/identifiants).
"""

from __future__ import annotations
import os
from datetime import datetime, timedelta, timezone

from google.cloud import bigquery

from ingestion import bq_io
from ingestion.google_ads import _client, CUSTOMER_ID

BQ_PROJECT = os.environ["BQ_PROJECT"]
BQ_DATASET = os.environ.get("BQ_DATASET", "lpl_cockpit")

GAQL = """
SELECT
  campaign.id, campaign.name,
  asset_group.id, asset_group.name,
  segments.date,
  metrics.cost_micros, metrics.conversions, metrics.conversions_value,
  metrics.impressions, metrics.clicks
FROM asset_group
WHERE segments.date BETWEEN '{since}' AND '{until}'
"""


def ingest(since: str, until: str) -> int:
    client = _client()
    service = client.get_service("GoogleAdsService")
    resp = service.search_stream(customer_id=CUSTOMER_ID.replace("-", ""),
                                 query=GAQL.format(since=since, until=until))
    now = datetime.now(timezone.utc).isoformat()
    rows = []
    for batch in resp:
        for r in batch.results:
            rows.append({
                "date": str(r.segments.date),
                "campaign_id": str(r.campaign.id),
                "campaign_name": r.campaign.name,
                "asset_group_id": str(r.asset_group.id),
                "asset_group_name": r.asset_group.name,
                "cost": r.metrics.cost_micros / 1_000_000,
                "conversions": r.metrics.conversions,
                "conversion_value": r.metrics.conversions_value,
                "impressions": r.metrics.impressions,
                "clicks": r.metrics.clicks,
                "updated_at": now,
            })
    bq = bigquery.Client(project=BQ_PROJECT)
    table = f"{BQ_PROJECT}.{BQ_DATASET}.google_asset_group_daily"
    n = bq_io.load_replace_window(bq, table, rows, since, until)
    print(f"[google-assets] {n} lignes asset group x jour écrites")
    return n


def refresh(days: int = 14) -> int:
    today = datetime.now(timezone.utc).date()
    return ingest((today - timedelta(days=days)).isoformat(), today.isoformat())


def backfill(days: int = 760, chunk: int = 180) -> int:
    today = datetime.now(timezone.utc).date()
    ws, total = today - timedelta(days=days), 0
    while ws < today:
        we = min(ws + timedelta(days=chunk), today)
        print(f"=== Asset groups {ws} -> {we} ===", flush=True)
        total += ingest(ws.isoformat(), we.isoformat())
        ws = we + timedelta(days=1)
    print(f"[google-assets] BACKFILL terminé : {total} lignes")
    return total


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "backfill":
        backfill(int(sys.argv[2]) if len(sys.argv) > 2 else 760)
    else:
        refresh(int(sys.argv[1]) if len(sys.argv) > 1 else 14)
