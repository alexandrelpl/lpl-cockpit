"""
Snapshot quotidien des budgets Meta -> BigQuery, à deux niveaux :
  - meta_adset_budget_daily     : budget propre des adsets (ABO).
  - meta_campaign_budget_daily  : budget des campagnes (CBO / Advantage Campaign Budget).

L'API insights ne donne que la dépense réelle, jamais le budget configuré ni son historique.
Beaucoup d'adsets sont en CBO -> daily_budget null au niveau adset, budget au niveau campagne.
On capture donc les deux + le campaign_id de chaque adset pour faire le lien et le repli.
"""

from __future__ import annotations
import os
import time
from datetime import datetime, timezone

import requests
from google.cloud import bigquery

from ingestion import bq_io

META_TOKEN   = os.environ["META_ACCESS_TOKEN"].strip()
META_ACCOUNT = os.environ.get("META_ACCOUNT_ID", "305450184")
API_VERSION  = os.environ.get("META_API_VERSION", "v21.0")
BQ_PROJECT   = os.environ["BQ_PROJECT"]
BQ_DATASET   = os.environ.get("BQ_DATASET", "lpl_cockpit")

ROOT = f"https://graph.facebook.com/{API_VERSION}/act_{META_ACCOUNT}"


def _fetch(edge: str, fields: str) -> list[dict]:
    params = {"access_token": META_TOKEN, "fields": fields, "limit": 200}
    url, base, out = f"{ROOT}/{edge}", f"{ROOT}/{edge}", []
    while url:
        r = requests.get(url, params=params if url == base else None, timeout=60)
        data = r.json()
        if "error" in data:
            raise RuntimeError(f"Meta API error ({edge}): {data['error']}")
        out.extend(data.get("data", []))
        url = data.get("paging", {}).get("next")
        params = None
        time.sleep(0.3)
    return out


def _eur(v):
    return (float(v) / 100.0) if v not in (None, "") else None   # budgets Meta en centimes


def refresh() -> int:
    today = datetime.now(timezone.utc).date().isoformat()
    now = datetime.now(timezone.utc).isoformat()
    client = bigquery.Client(project=BQ_PROJECT)

    adsets = _fetch("adsets", "id,name,campaign_id,daily_budget,lifetime_budget,status,effective_status")
    arows = [{"date": today, "adset_id": a.get("id"), "adset_name": a.get("name"),
              "campaign_id": a.get("campaign_id"),
              "daily_budget": _eur(a.get("daily_budget")), "lifetime_budget": _eur(a.get("lifetime_budget")),
              "status": a.get("effective_status") or a.get("status"), "updated_at": now}
             for a in adsets]
    na = bq_io.load_replace_window(client, f"{BQ_PROJECT}.{BQ_DATASET}.meta_adset_budget_daily",
                                   arows, today, today)

    camps = _fetch("campaigns", "id,name,daily_budget,lifetime_budget,status,effective_status")
    crows = [{"date": today, "campaign_id": c.get("id"), "campaign_name": c.get("name"),
              "daily_budget": _eur(c.get("daily_budget")), "lifetime_budget": _eur(c.get("lifetime_budget")),
              "status": c.get("effective_status") or c.get("status"), "updated_at": now}
             for c in camps]
    nc = bq_io.load_replace_window(client, f"{BQ_PROJECT}.{BQ_DATASET}.meta_campaign_budget_daily",
                                   crows, today, today)
    print(f"[meta-budgets] {na} adsets + {nc} campagnes snapshotés ({today})")
    return na + nc


if __name__ == "__main__":
    refresh()
