"""
Source PROVISOIRE pour Google Ads : lit les coûts quotidiens depuis un Google Sheet
(rempli chaque jour par un script Google Ads) et les déverse dans `google_daily`.

Onglet « GoogleAds » : col A = date, col B = coût (€). En-tête en ligne 1.

But : avoir un COS *blended* juste (Meta + Google) en attendant l'accès Basic de l'API
Google Ads. Dès que Basic est accordé, on remplace cette source par `google_ads.py`
(même table `google_daily`, rien d'autre à changer).

Accès : le Sheet doit être partagé (Lecteur) avec le compte de service du Job.
"""

from __future__ import annotations
import os
import re
from datetime import date, datetime, timedelta, timezone

import requests
import google.auth
from google.auth.transport.requests import Request as GAuthRequest
from google.cloud import bigquery

from ingestion import bq_io

BQ_PROJECT = os.environ["BQ_PROJECT"]
BQ_DATASET = os.environ.get("BQ_DATASET", "lpl_cockpit")
SHEET_ID   = os.environ.get("GOOGLE_COST_SHEET_ID", "")
SHEET_TAB  = os.environ.get("GOOGLE_COST_SHEET_TAB", "GoogleAds")

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")
_EPOCH = date(1899, 12, 30)   # base des numéros de série Google Sheets


def _to_iso(v) -> str | None:
    if isinstance(v, (int, float)):
        return (_EPOCH + timedelta(days=int(v))).isoformat()
    s = str(v).strip()
    if _DATE_RE.match(s):
        return s[:10]
    # tolère JJ/MM/AAAA
    m = re.match(r"^(\d{2})/(\d{2})/(\d{4})", s)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    return None


def _read_sheet() -> list[list]:
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"])
    creds.refresh(GAuthRequest())
    rng = requests.utils.quote(f"'{SHEET_TAB}'!A2:B", safe="")
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values/{rng}"
    r = requests.get(url, params={"valueRenderOption": "UNFORMATTED_VALUE",
                                  "dateTimeRenderOption": "FORMATTED_STRING"},
                     headers={"Authorization": f"Bearer {creds.token}"}, timeout=45)
    if r.status_code != 200:
        raise RuntimeError(f"Sheets API {r.status_code}: {r.text[:160]}")
    return r.json().get("values", [])


def refresh_from_sheet() -> int:
    if not SHEET_ID:
        raise RuntimeError("GOOGLE_COST_SHEET_ID non configuré")
    now = datetime.now(timezone.utc).isoformat()
    rows, seen = [], set()
    for line in _read_sheet():
        if len(line) < 2:
            continue
        iso = _to_iso(line[0])
        if not iso or iso in seen:
            continue
        try:
            cost = float(line[1])
        except (TypeError, ValueError):
            continue
        seen.add(iso)
        rows.append({
            "date": iso, "campaign_id": None,
            "campaign_name": "Google Ads (total, sheet)", "campaign_type": "ALL",
            "cost": round(cost, 2), "conversions": None, "conversion_value": None,
            "impressions": None, "clicks": None, "updated_at": now,
        })
    client = bigquery.Client(project=BQ_PROJECT)
    table = f"{BQ_PROJECT}.{BQ_DATASET}.google_daily"
    n = bq_io.load_replace_all(client, table, rows)
    print(f"[google-sheet] {n} jours de coûts Google écrits dans google_daily")
    return n


def refresh(days: int = 0) -> int:   # signature compatible avec l'orchestrateur
    return refresh_from_sheet()


if __name__ == "__main__":
    refresh_from_sheet()
