"""
Point d'entrée léger sessions.
- sans argument : rafraîchit la fenêtre récente (onglet « Sessions » dynamique).
- argument "archive" : backfill one-shot de l'historique (onglet archive 720 j).
"""
import sys
from ingestion import sessions_sheet

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "archive":
        print({"archive": sessions_sheet.backfill_archive()})
    else:
        print({"sessions": sessions_sheet.refresh_from_sheet()})
