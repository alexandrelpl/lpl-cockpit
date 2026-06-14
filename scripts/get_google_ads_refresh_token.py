"""
Génère un refresh token OAuth pour l'API Google Ads (scope adwords).
À lancer UNE fois en local. Utilise TON client OAuth (type « Desktop » de préférence).

Usage :
  pip install google-auth-oauthlib
  export GOOGLE_ADS_CLIENT_ID='xxx.apps.googleusercontent.com'
  export GOOGLE_ADS_CLIENT_SECRET='xxx'
  python scripts/get_google_ads_refresh_token.py

Un navigateur s'ouvre -> connecte-toi avec le compte Google qui a accès au MCC Google Ads,
autorise -> le refresh token s'affiche dans le terminal. Garde-le pour Secret Manager.
"""
import os
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/adwords"]

cid = os.environ["GOOGLE_ADS_CLIENT_ID"]
csec = os.environ["GOOGLE_ADS_CLIENT_SECRET"]

flow = InstalledAppFlow.from_client_config(
    {"installed": {
        "client_id": cid,
        "client_secret": csec,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost"],
    }},
    scopes=SCOPES,
)
creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")
print("\n=================  REFRESH TOKEN  =================")
print(creds.refresh_token)
print("==================================================")
print("Garde-le précieusement (il n'expire pas) — on le mettra dans Secret Manager.")
