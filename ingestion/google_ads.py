"""
Ingestion Google Ads -> BigQuery (table google_daily), par campagne, pas quotidien.

Prêt pour l'API réelle dès l'obtention de l'accès Basic du developer token.
Utilise la lib officielle google-ads (GAQL). Tant que Basic n'est pas accordé,
l'appel renverra DEVELOPER_TOKEN_NOT_APPROVED : c'est attendu.

Auth via variables d'env (Secret Manager) :
  GOOGLE_ADS_DEVELOPER_TOKEN, GOOGLE_ADS_CLIENT_ID, GOOGLE_ADS_CLIENT_SECRET,
  GOOGLE_ADS_REFRESH_TOKEN, GOOGLE_ADS_LOGIN_CUSTOMER_ID (MCC), GOOGLE_ADS_CUSTOMER_ID (LPL).
"""

from __future__ import annotations
import os
from datetime import datetime, timedelta, timezone

from google.cloud import bigquery

BQ_PROJECT  = os.environ["BQ_PROJECT"]
BQ_DATASET  = os.environ.get("BQ_DATASET", "lpl_cockpit")
CUSTOMER_ID = os.environ.get("GOOGLE_ADS_CUSTOMER_ID", "")  # compte LPL, sans tirets

GAQL = """
SELECT
  campaign.id,
  campaign.name,
  campaign.advertising_channel_type,
  segments.date,
  metrics.cost_micros,
  metrics.conversions,
  metrics.conversions_value,
  metrics.impressions,
  metrics.clicks
FROM campaign
WHERE segments.date BETWEEN '{since}' AND '{until}'
"""


def _client():
    from google.ads.googleads.client import GoogleAdsClient
    cfg = {
        "developer_token": os.environ["GOOGLE_ADS_DEVELOPER_TOKEN"],
        "client_id": os.environ["GOOGLE_ADS_CLIENT_ID"],
        "client_secret": os.environ["GOOGLE_ADS_CLIENT_SECRET"],
        "refresh_token": os.environ["GOOGLE_ADS_REFRESH_TOKEN"],
        "login_customer_id": os.environ["GOOGLE_ADS_LOGIN_CUSTOMER_ID"].replace("-", ""),
        "use_proto_plus": True,
    }
    return GoogleAdsClient.load_from_dict(cfg)


def ingest(since: str, until: str) -> int:
    client = _client()
    service = client.get_service("GoogleAdsService")
    response = service.search_stream(
        customer_id=CUSTOMER_ID.replace("-", ""),
        query=GAQL.format(since=since, until=until),
    )
    now = datetime.now(timezone.utc).isoformat()
    rows = []
    for batch in response:
        for r in batch.results:
            rows.append({
                "date": str(r.segments.date),
                "campaign_id": str(r.campaign.id),
                "campaign_name": r.campaign.name,
                "campaign_type": r.campaign.advertising_channel_type.name,
                "cost": r.metrics.cost_micros / 1_000_000,
                "conversions": r.metrics.conversions,
                "conversion_value": r.metrics.conversions_value,
                "impressions": r.metrics.impressions,
                "clicks": r.metrics.clicks,
                "updated_at": now,
            })

    bq = bigquery.Client(project=BQ_PROJECT)
    table = f"{BQ_PROJECT}.{BQ_DATASET}.google_daily"
    bq.query(
        f"DELETE FROM `{table}` WHERE date BETWEEN @s AND @u",
        job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("s", "DATE", since),
            bigquery.ScalarQueryParameter("u", "DATE", until),
        ]),
    ).result()
    if rows:
        errors = bq.insert_rows_json(table, rows)
        if errors:
            raise RuntimeError(f"BigQuery insert errors: {errors}")
    print(f"[google] {len(rows)} lignes campagne x jour écrites")
    return len(rows)


def refresh(days: int = 14) -> int:
    today = datetime.now(timezone.utc).date()
    since = (today - timedelta(days=days)).isoformat()
    return ingest(since, today.isoformat())


if __name__ == "__main__":
    import sys
    refresh(int(sys.argv[1]) if len(sys.argv) > 1 else 14)
