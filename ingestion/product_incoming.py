"""
Réassorts fournisseurs -> BigQuery (table product_incoming).

Lit l'onglet « Raw Data » du Google Sheet « Order register - Frames » :
  col B = SKU, col C = EAN, col K = Estimated delivery date (JJ/MM/AAAA ou n° de série).
On stocke 1 ligne par commande fournisseur (SKU, EAN, date de livraison estimée). Le filtre
« date dans le futur » et le choix de la PROCHAINE date se font côté endpoint (la date du jour
bouge). Join produit = SKU (v1) ; l'EAN est stocké pour un fallback futur.

Accès : le Sheet doit être partagé (Lecteur) avec le compte de service du Job
(lpl-cockpit-ingest@shopify-data-ltv.iam.gserviceaccount.com).
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
SHEET_ID   = os.environ.get("INCOMING_SHEET_ID", "")
SHEET_TAB  = os.environ.get("INCOMING_SHEET_TAB", "Raw Data")

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")
_EPOCH = date(1899, 12, 30)   # base des numéros de série Google Sheets


def _to_iso(v) -> str | None:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return (_EPOCH + timedelta(days=int(v))).isoformat()
    s = str(v).strip()
    if _DATE_RE.match(s):
        return s[:10]
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})", s)   # JJ/MM/AAAA
    if m:
        return f"{m.group(3)}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    return None


def _norm_ean(v) -> str | None:
    if v is None or v == "":
        return None
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if isinstance(v, int):
        return str(v)
    return str(v).strip() or None


def _read_sheet() -> list[list]:
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"])
    creds.refresh(GAuthRequest())
    rng = requests.utils.quote(f"'{SHEET_TAB}'!A2:L", safe="")
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET_ID}/values/{rng}"
    r = requests.get(url, params={"valueRenderOption": "UNFORMATTED_VALUE",
                                  "dateTimeRenderOption": "FORMATTED_STRING"},
                     headers={"Authorization": f"Bearer {creds.token}"}, timeout=45)
    if r.status_code != 200:
        raise RuntimeError(f"Sheets API {r.status_code}: {r.text[:160]}")
    return r.json().get("values", [])


def refresh(days: int = 0) -> int:   # signature compatible orchestrateur
    if not SHEET_ID:
        raise RuntimeError("INCOMING_SHEET_ID non configuré")
    now = datetime.now(timezone.utc).isoformat()
    rows = []
    for line in _read_sheet():
        sku = (str(line[1]).strip() if len(line) > 1 else "")
        ean = _norm_ean(line[2]) if len(line) > 2 else None
        iso = _to_iso(line[10]) if len(line) > 10 else None
        if not iso or (not sku and not ean):
            continue
        rows.append({"sku": sku or None, "ean": ean,
                     "delivery_date": iso, "updated_at": now})
    client = bigquery.Client(project=BQ_PROJECT)
    table = f"{BQ_PROJECT}.{BQ_DATASET}.product_incoming"
    n = bq_io.load_replace_all(client, table, rows)
    print(f"[product-incoming] {n} lignes de réassort (SKU/EAN x date) écrites")
    return n


if __name__ == "__main__":
    print(refresh())
