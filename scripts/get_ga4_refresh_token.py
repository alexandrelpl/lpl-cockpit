"""
Génère un refresh token OAuth pour l'API GA4 Data (scope analytics.readonly).
À lancer UNE fois en local, avec TON client OAuth « Desktop » (le même que Google Ads).

  pip install google-auth-oauthlib
  export GOOGLE_ADS_CLIENT_ID='xxx.apps.googleusercontent.com'   # réutilise le client Desktop
  export GOOGLE_ADS_CLIENT_SECRET='xxx'
  python scripts/get_ga4_refresh_token.py

Connecte-toi avec le compte Google qui a accès à la propriété GA4. Le refresh token
s'affiche -> on le mettra dans Secret Manager (GA4_REFRESH_TOKEN).
"""
import os
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/analytics.readonly"]

flow = InstalledAppFlow.from_client_config(
    {"installed": {
        "client_id": os.environ["GOOGLE_ADS_CLIENT_ID"],
        "client_secret": os.environ["GOOGLE_ADS_CLIENT_SECRET"],
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost"],
    }},
    scopes=SCOPES,
)
creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")
print("\n=================  GA4 REFRESH TOKEN  =================")
print(creds.refresh_token)
print("======================================================")
