"""
Point d'entrée pour Cloud Run Job (traitement par lots quotidien).

Lance le refresh de toutes les sources, sans serveur web ni limite de requête.
Utilisé par : gcloud run jobs deploy ... --command=python --args=job.py
"""
from ingestion.main import daily_refresh

if __name__ == "__main__":
    print(daily_refresh())
