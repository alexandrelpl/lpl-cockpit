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

from ingestion import shopify_orders, shopify_traffic, meta_ads, google_ads, bq_setup, bq_io

SHOPIFY_REFRESH_DAYS = int(os.environ.get("SHOPIFY_REFRESH_DAYS", "40"))
ADS_REFRESH_DAYS     = int(os.environ.get("ADS_REFRESH_DAYS", "14"))


def daily_refresh() -> dict:
    results: dict[str, object] = {}
    # Chaque source est isolée : une panne (ex. Google non approuvé) n'arrête pas les autres.
    for name, fn in [
        ("shopify_orders",  lambda: shopify_orders.refresh(SHOPIFY_REFRESH_DAYS)),
        ("shopify_traffic", lambda: shopify_traffic.refresh(SHOPIFY_REFRESH_DAYS)),
        ("meta",            lambda: meta_ads.refresh(ADS_REFRESH_DAYS)),
        ("google",          lambda: google_ads.refresh(ADS_REFRESH_DAYS)),
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
