"""
Sessions Shopify (humaines, hors bots) depuis Google Sheet -> shopify_traffic_daily.

Deux sources :
- onglet « Sessions » récent (rempli quotidiennement par le scraper) : rafraîchi
  souvent, met à jour la fenêtre récente UNIQUEMENT (sans toucher à l'historique).
- onglet archive (720 j, statique) : backfill one-shot de l'historique.

Écriture par FENÊTRE (load_replace_window) : on ne remplace que [min..max] des
lignes lues, donc l'archive et le récent coexistent sans s'effacer.

Le CVR est calculé côté appli (commandes LPL ÷ sessions).

Config :
  SESSIONS_SHEET_ID, SESSIONS_SHEET_TAB (défaut « Sessions »)
  SESSIONS_ARCHIVE_SHEET_ID, SESSIONS_ARCHIVE_TAB   (pour le backfill historique)
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

BQ_PROJECT     = os.environ["BQ_PROJECT"]
BQ_DATASET     = os.environ.get("BQ_DATASET", "lpl_cockpit")
SHEET_ID       = os.environ.get("SESSIONS_SHEET_ID", "")
SHEET_TAB      = os.environ.get("SESSIONS_SHEET_TAB", "Sessions")
ARCHIVE_ID     = os.environ.get("SESSIONS_ARCHIVE_SHEET_ID", "")
ARCHIVE_TAB    = os.environ.get("SESSIONS_ARCHIVE_TAB", "")

_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}")
_DMY = re.compile(r"^(\d{2})/(\d{2})/(\d{4})")
_EPOCH = date(1899, 12, 30)
_FR = {"janv": 1, "févr": 2, "fevr": 2, "mars": 3, "avr": 4, "mai": 5, "juin": 6,
       "juil": 7, "août": 8, "aout": 8, "sept": 9, "oct": 10, "nov": 11, "déc": 12, "dec": 12}


def _to_iso(v) -> str | None:
    if isinstance(v, (int, float)):
        return (_EPOCH + timedelta(days=int(v))).isoformat()
    s = str(v).strip()
    if _ISO.match(s):
        return s[:10]
    m = _DMY.match(s)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    m = re.match(r"^(\d{1,2})\s+([A-Za-zéûàÉ\.]+)\.?\s+(\d{4})", s)
    if m:
        mon = _FR.get(m.group(2).lower().strip(".")[:4])
        if mon:
            return f"{m.group(3)}-{mon:02d}-{int(m.group(1)):02d}"
    return None


def _read(sheet_id: str, tab: str) -> list[list]:
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"])
    creds.refresh(GAuthRequest())
    rng = requests.utils.quote(f"'{tab}'!A2:B", safe="")
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/{rng}"
    r = requests.get(url, params={"valueRenderOption": "UNFORMATTED_VALUE",
                                  "dateTimeRenderOption": "FORMATTED_STRING"},
                     headers={"Authorization": f"Bearer {creds.token}"}, timeout=45)
    if r.status_code != 200:
        raise RuntimeError(f"Sheets API {r.status_code}: {r.text[:160]}")
    return r.json().get("values", [])


def _parse(values: list[list]) -> list[dict]:
    now = datetime.now(timezone.utc).isoformat()
    rows, seen = [], set()
    for line in values:
        if len(line) < 2:
            continue
        iso = _to_iso(line[0])
        if not iso or iso in seen:
            continue
        try:
            sessions = int(round(float(line[1])))
        except (TypeError, ValueError):
            continue
        seen.add(iso)
        rows.append({"date": iso, "sessions": sessions, "visitors": None,
                     "conversion_rate": None, "updated_at": now})
    return rows


def _write(rows: list[dict]) -> int:
    if not rows:
        return 0
    dates = sorted(r["date"] for r in rows)
    client = bigquery.Client(project=BQ_PROJECT)
    table = f"{BQ_PROJECT}.{BQ_DATASET}.shopify_traffic_daily"
    return bq_io.load_replace_window(client, table, rows, dates[0], dates[-1])


def refresh_from_sheet() -> int:
    if not SHEET_ID:
        raise RuntimeError("SESSIONS_SHEET_ID non configuré")
    n = _write(_parse(_read(SHEET_ID, SHEET_TAB)))
    print(f"[sessions-sheet] {n} jours (récent) écrits dans shopify_traffic_daily")
    return n


def backfill_archive() -> int:
    if not (ARCHIVE_ID and ARCHIVE_TAB):
        raise RuntimeError("SESSIONS_ARCHIVE_SHEET_ID / SESSIONS_ARCHIVE_TAB non configurés")
    n = _write(_parse(_read(ARCHIVE_ID, ARCHIVE_TAB)))
    print(f"[sessions-archive] {n} jours d'historique écrits dans shopify_traffic_daily")
    return n


def refresh(days: int = 0) -> int:
    return refresh_from_sheet()


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "archive":
        backfill_archive()
    else:
        refresh_from_sheet()
