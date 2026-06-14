"""
Point d'entrée léger « sessions ».
- sans argument : rafraîchit les sessions via GA4 (fenêtre 3 j) -> source autonome.
- argument "archive" : recharge l'archive historique du scraper Shopify (one-shot, rare).
"""
import sys

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "archive":
        from ingestion import sessions_sheet
        print({"archive": sessions_sheet.backfill_archive()})
    else:
        from ingestion import ga4_traffic
        print({"ga4_sessions": ga4_traffic.refresh(3)})
