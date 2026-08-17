"""
Extraction GA4 — 12 mois, par canal et par source/medium, pour analyse du trafic qualifié.

Sort 2 CSV dans le dossier du script :
  - ga4_channel_daily_365.csv      : date, channel, sessions, engaged_sessions, purchases, revenue, users
  - ga4_sourcemedium_monthly.csv   : year_month, source_medium, sessions, engaged_sessions, purchases, revenue, users

Auth : réutilise l'OAuth utilisateur GA4 (mêmes variables que le Job).
Variables d'environnement requises :
  GA4_PROPERTY_ID, GA4_REFRESH_TOKEN, GA4_CLIENT_ID, GA4_CLIENT_SECRET
(GA4_CLIENT_ID/SECRET retombent sur GOOGLE_ADS_CLIENT_ID/SECRET si absents, comme ga4_traffic.py)

Usage :
  python analysis/ga4_channel_extract.py
"""

from __future__ import annotations
import csv
import os
import sys
from datetime import date, timedelta

import requests
from google.auth.transport.requests import Request as GAuthRequest
from google.oauth2.credentials import Credentials

GA4_PID    = os.environ.get("GA4_PROPERTY_ID", "")
REFRESH    = os.environ.get("GA4_REFRESH_TOKEN", "")
CLIENT_ID  = os.environ.get("GA4_CLIENT_ID") or os.environ.get("GOOGLE_ADS_CLIENT_ID", "")
CLIENT_SEC = os.environ.get("GA4_CLIENT_SECRET") or os.environ.get("GOOGLE_ADS_CLIENT_SECRET", "")
SCOPE = ["https://www.googleapis.com/auth/analytics.readonly"]
OUT_DIR = os.path.dirname(os.path.abspath(__file__))


def _check_env():
    missing = [k for k, v in {"GA4_PROPERTY_ID": GA4_PID, "GA4_REFRESH_TOKEN": REFRESH,
                              "GA4_CLIENT_ID": CLIENT_ID, "GA4_CLIENT_SECRET": CLIENT_SEC}.items() if not v]
    if missing:
        print("ERREUR : variables manquantes -> " + ", ".join(missing))
        print("Exporte-les (depuis Secret Manager ou ton .env) puis relance.")
        sys.exit(1)


def _token() -> str:
    c = Credentials(None, refresh_token=REFRESH, client_id=CLIENT_ID, client_secret=CLIENT_SEC,
                    token_uri="https://oauth2.googleapis.com/token", scopes=SCOPE)
    c.refresh(GAuthRequest())
    return c.token


def _report(dimensions, metrics, since, until, order_metric=None, limit=200000):
    url = f"https://analyticsdata.googleapis.com/v1beta/properties/{GA4_PID}:runReport"
    body = {
        "dateRanges": [{"startDate": since, "endDate": until}],
        "dimensions": [{"name": d} for d in dimensions],
        "metrics": [{"name": m} for m in metrics],
        "limit": limit,
    }
    if order_metric:
        body["orderBys"] = [{"metric": {"metricName": order_metric}, "desc": True}]
    tok = _token()
    r = requests.post(url, json=body, headers={"Authorization": f"Bearer {tok}"}, timeout=120)
    if r.status_code != 200:
        raise RuntimeError(f"GA4 Data API {r.status_code}: {r.text[:400]}")
    return r.json().get("rows", [])


def _i(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return 0


def _f(v):
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return 0.0


def main():
    _check_env()
    today = date.today()
    since = (today - timedelta(days=365)).isoformat()
    until = today.isoformat()
    metrics = ["sessions", "engagedSessions", "ecommercePurchases", "purchaseRevenue", "totalUsers"]

    # 1) canal x jour
    print(f"GA4 propriété {GA4_PID} — extraction {since} -> {until}")
    rows = _report(["date", "sessionDefaultChannelGroup"], metrics, since, until)
    p1 = os.path.join(OUT_DIR, "ga4_channel_daily_365.csv")
    with open(p1, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["date", "channel", "sessions", "engaged_sessions", "purchases", "revenue", "users"])
        for r in rows:
            d = r["dimensionValues"]; v = r["metricValues"]
            dt = d[0]["value"]
            w.writerow([f"{dt[0:4]}-{dt[4:6]}-{dt[6:8]}", d[1]["value"] or "(other)",
                        _i(v[0]["value"]), _i(v[1]["value"]), _i(v[2]["value"]),
                        _f(v[3]["value"]), _i(v[4]["value"])])
    dates = sorted({r["dimensionValues"][0]["value"] for r in rows})
    print(f"  -> {p1} ({len(rows)} lignes ; couverture {dates[0] if dates else '?'} -> {dates[-1] if dates else '?'})")

    # 2) source / medium x mois
    rows2 = _report(["yearMonth", "sessionSourceMedium"], metrics, since, until, order_metric="sessions")
    p2 = os.path.join(OUT_DIR, "ga4_sourcemedium_monthly.csv")
    with open(p2, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["year_month", "source_medium", "sessions", "engaged_sessions", "purchases", "revenue", "users"])
        for r in rows2:
            d = r["dimensionValues"]; v = r["metricValues"]
            w.writerow([d[0]["value"], d[1]["value"] or "(not set)",
                        _i(v[0]["value"]), _i(v[1]["value"]), _i(v[2]["value"]),
                        _f(v[3]["value"]), _i(v[4]["value"])])
    print(f"  -> {p2} ({len(rows2)} lignes)")
    print("OK. Préviens Claude que les 2 CSV sont prêts.")


if __name__ == "__main__":
    main()
