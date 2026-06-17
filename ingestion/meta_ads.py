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

from ingestion import bq_io

META_TOKEN   = os.environ["META_ACCESS_TOKEN"].strip()
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
    n = bq_io.load_replace_window(client, table, rows, since, until)
    print(f"[meta] {n} lignes campagne x jour écrites")
    return n


def refresh(days: int = 14) -> int:
    today = datetime.now(timezone.utc).date()
    since = (today - timedelta(days=days)).isoformat()
    return ingest(since, today.isoformat())


def backfill(days: int = 760, chunk: int = 90) -> int:
    """Historique Meta par tranches (l'API refuse une trop grande plage d'un coup)."""
    today = datetime.now(timezone.utc).date()
    ws, total = today - timedelta(days=days), 0
    while ws < today:
        we = min(ws + timedelta(days=chunk), today)
        print(f"=== Meta {ws} -> {we} ===", flush=True)
        total += ingest(ws.isoformat(), we.isoformat())
        ws = we + timedelta(days=1)
    print(f"[meta] BACKFILL terminé : {total} lignes campagne x jour")
    return total


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "backfill":
        backfill(int(sys.argv[2]) if len(sys.argv) > 2 else 760)
    else:
        refresh(int(sys.argv[1]) if len(sys.argv) > 1 else 14)
