"""
Point d'entrée orchestration.

Deux usages :
  - CLI (one-shot, en local) :
      python -m ingestion.main setup            # crée dataset + tables BigQuery
      python -m ingestion.main backfill 24      # historique 24 mois (Shopify)
      python -m ingestion.main refresh          # refresh quotidien toutes sources
  - HTTP (Cloud Run, appelé par Cloud Scheduler) :
      GET/POST /refresh   -> refresh quotidien
      GET/POST /setup     -> bootstrap BigQuery (à n'appeler qu'une fois)

Le refresh quotidien :
  - Shopify CA   : fenêtre glissante 40 j (rattrape les remboursements tardifs)
  - Shopify trafic : 40 j
  - Meta         : 14 j
  - Google       : 14 j (silencieux tant que developer token non approuvé)
"""

from __future__ import annotations
import os
import traceback

from ingestion import (shopify_orders, shopify_traffic, meta_ads, google_ads, google_asset_groups,
                       google_sheet, ga4_traffic, sessions_sheet, bq_setup, bq_io)

SHOPIFY_REFRESH_DAYS = int(os.environ.get("SHOPIFY_REFRESH_DAYS", "40"))
ADS_REFRESH_DAYS     = int(os.environ.get("ADS_REFRESH_DAYS", "14"))


def daily_refresh() -> dict:
    results: dict[str, object] = {}
    # Chaque source est isolée : une panne (ex. Google non approuvé) n'arrête pas les autres.
    # Sources rapides d'abord (ads), Shopify (lent, ~25 min) en dernier.
    for name, fn in [
        ("meta",            lambda: meta_ads.refresh(ADS_REFRESH_DAYS)),
        # Google Ads via l'API officielle (GAQL) — accès Basic obtenu.
        # (l'ancienne lecture via Sheet `google_sheet` reste dispo en secours si besoin)
        ("google",          lambda: google_ads.refresh(ADS_REFRESH_DAYS)),
        ("google_assets",   lambda: google_asset_groups.refresh(ADS_REFRESH_DAYS)),
        # Sessions via GA4 (source autonome). Fenêtre 3 j seulement -> l'historique plus ancien
        # reste figé en base = continuité conservée même après la rétention 14 mois de GA4.
        ("sessions",        lambda: ga4_traffic.refresh(3)),
        ("shopify_orders",  lambda: shopify_orders.refresh(SHOPIFY_REFRESH_DAYS)),
    ]:
        try:
            results[name] = fn()
        except Exception as e:  # noqa: BLE001
            results[name] = f"ERROR: {e}"
            traceback.print_exc()
    return results


# ----- HTTP (Cloud Run) -----
try:
    from flask import Flask, jsonify, request
    app = Flask(__name__)

    @app.route("/", methods=["GET"])
    def health():
        return "lpl-cockpit ingestion OK", 200

    @app.route("/refresh", methods=["GET", "POST"])
    def http_refresh():
        return jsonify(daily_refresh()), 200

    @app.route("/setup", methods=["GET", "POST"])
    def http_setup():
        bq_setup.run()
        return jsonify({"setup": "ok"}), 200

    @app.route("/backfill", methods=["POST"])
    def http_backfill():
        months = int(request.args.get("months", "24"))
        return jsonify({"shopify_backfill_days": shopify_orders.backfill(months)}), 200
except ImportError:
    app = None  # Flask absent en mode CLI pur


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "refresh"
    if cmd == "setup":
        bq_setup.run()
    elif cmd == "flush":
        bq_io.flush_default()
    elif cmd == "backfill":
        shopify_orders.backfill(int(sys.argv[2]) if len(sys.argv) > 2 else 24)
    elif cmd == "refresh":
        print(daily_refresh())
    else:
        print(f"Commande inconnue: {cmd}")
