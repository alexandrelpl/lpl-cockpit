"""
Ingestion Meta Ads -> BigQuery (table meta_daily), niveau campagne, pas quotidien.

Appelle directement l'API Meta Marketing (Cloud Run n'a pas le serveur MCP local).
Token long-lived dans META_ACCESS_TOKEN (Secret Manager). Expire ~60 j -> à renouveler.
"""

from __future__ import annotations
import os
import time
from datetime import datetime, timedelta, timezone

import requests
from google.cloud import bigquery

META_TOKEN   = os.environ["META_ACCESS_TOKEN"]
META_ACCOUNT = os.environ.get("META_ACCOUNT_ID", "305450184")   # sans 'act_'
API_VERSION  = os.environ.get("META_API_VERSION", "v21.0")
BQ_PROJECT   = os.environ["BQ_PROJECT"]
BQ_DATASET   = os.environ.get("BQ_DATASET", "lpl_cockpit")

BASE = f"https://graph.facebook.com/{API_VERSION}/act_{META_ACCOUNT}/insights"
FIELDS = "campaign_id,campaign_name,spend,impressions,clicks,actions,action_values"


def _action_val(items, action_type):
    for it in (items or []):
        if it.get("action_type") == action_type:
            return float(it["value"])
    return 0.0


def _fetch(since: str, until: str) -> list[dict]:
    params = {
        "access_token": META_TOKEN,
        "level": "campaign",
        "time_increment": 1,
        "time_range": f'{{"since":"{since}","until":"{until}"}}',
        "fields": FIELDS,
        "limit": 500,
    }
    url, out = BASE, []
    while url:
        r = requests.get(url, params=params if url == BASE else None, timeout=60)
        data = r.json()
        if "error" in data:
            raise RuntimeError(f"Meta API error: {data['error']}")
        out.extend(data.get("data", []))
        url = data.get("paging", {}).get("next")
        params = None
        time.sleep(0.3)
    return out


def ingest(since: str, until: str) -> int:
    rows_raw = _fetch(since, until)
    now = datetime.now(timezone.utc).isoformat()
    rows = []
    for r in rows_raw:
        rows.append({
            "date": r["date_start"],
            "campaign_id": r.get("campaign_id"),
            "campaign_name": r.get("campaign_name"),
            "spend": float(r.get("spend", 0)),
            "purchases": _action_val(r.get("actions"), "omni_purchase"),
            "purchase_value": _action_val(r.get("action_values"), "omni_purchase"),
            "impressions": int(r.get("impressions", 0)),
            "clicks": int(r.get("clicks", 0)),
            "link_clicks": int(_action_val(r.get("actions"), "link_click")),
            "updated_at": now,
        })

    client = bigquery.Client(project=BQ_PROJECT)
    table = f"{BQ_PROJECT}.{BQ_DATASET}.meta_daily"
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
    print(f"[meta] {len(rows)} lignes campagne x jour écrites")
    return len(rows)


def refresh(days: int = 14) -> int:
    today = datetime.now(timezone.utc).date()
    since = (today - timedelta(days=days)).isoformat()
    return ingest(since, today.isoformat())


if __name__ == "__main__":
    import sys
    refresh(int(sys.argv[1]) if len(sys.argv) > 1 else 14)
