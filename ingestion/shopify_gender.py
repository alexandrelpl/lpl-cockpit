"""
Estimation Homme / Femme à partir du PRÉNOM DE LIVRAISON des commandes web.

Chaîne de résolution (hybride, décidé avec Alexandre) :
  1. dictionnaire embarqué FR/EN/AR (first_names.local_gender) — déterministe
  2. gender-guesser (large base) si installé
  3. cache BigQuery (gender_name_cache) — prénoms déjà résolus (gg / Claude)
  4. Claude (si ANTHROPIC_API_KEY) sur les prénoms restants, résultats mis en cache
  5. sinon -> 'U' (Indéterminé)

Sortie : gender_daily (date, gender ∈ {H,F,U}, units).
C'est une ESTIMATION statistique (le prénom ne détermine pas le genre réel).
"""

from __future__ import annotations
import json
import os
import socket
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from google.cloud import bigquery

from ingestion import bq_io
from ingestion.first_names import local_gender, normalize

socket.setdefaulttimeout(120)

SHOP_URL     = os.environ["SHOPIFY_SHOP_URL"]
ACCESS_TOKEN = os.environ["SHOPIFY_ACCESS_TOKEN"].strip()
API_VERSION  = os.environ.get("SHOPIFY_API_VERSION", "2024-01")
BQ_PROJECT   = os.environ["BQ_PROJECT"]
BQ_DATASET   = os.environ.get("BQ_DATASET", "lpl_cockpit")
LOCAL_TZ     = ZoneInfo(os.environ.get("TIMEZONE", "Europe/Paris"))
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_API_KEY", "")
CLAUDE_MODEL  = os.environ.get("GENDER_CLAUDE_MODEL", "claude-haiku-4-5-20251001")

GRAPHQL_URL = f"https://{SHOP_URL}/admin/api/{API_VERSION}/graphql.json"
ALLOWED_SOURCES = {"web"}
EXCLUDE_ORDER_TAGS = {"alan", "wholesale", "b2b"}
CACHE_TABLE = f"{BQ_PROJECT}.{BQ_DATASET}.gender_name_cache"

_SESSION = requests.Session()
_THROTTLE: dict = {}
_LAST_COST = 40

QUERY = """
query ($query: String!, $cursor: String) {
  orders(first: 100, query: $query, after: $cursor, sortKey: CREATED_AT) {
    pageInfo { hasNextPage endCursor }
    nodes {
      createdAt
      displayFinancialStatus
      sourceName
      tags
      shippingAddress { firstName }
      customer { firstName }
    }
  }
}
"""


def _graphql(query_string, cursor):
    payload = {"query": QUERY, "variables": {"query": query_string, "cursor": cursor}}
    headers = {"X-Shopify-Access-Token": ACCESS_TOKEN, "Content-Type": "application/json"}
    last_err = None
    for attempt in range(8):
        try:
            r = _SESSION.post(GRAPHQL_URL, json=payload, headers=headers, timeout=(15, 90))
        except requests.exceptions.RequestException as e:
            last_err = e; time.sleep(3 * (attempt + 1)); continue
        if r.status_code == 429:
            time.sleep(2 * (attempt + 1)); continue
        if r.status_code in (401, 403):
            raise RuntimeError(f"Shopify AUTH {r.status_code} : token/scope. {r.text[:200]}")
        if r.status_code >= 500:
            time.sleep(3 * (attempt + 1)); continue
        data = r.json()
        errs = data.get("errors")
        if errs:
            if any((e.get("extensions", {}) or {}).get("code") == "THROTTLED" for e in errs
                   if isinstance(e, dict)):
                time.sleep(2 * (attempt + 1)); continue
            raise RuntimeError(f"Shopify GraphQL errors: {errs}")
        global _THROTTLE, _LAST_COST
        cost = data.get("extensions", {}).get("cost", {})
        _THROTTLE = cost.get("throttleStatus", _THROTTLE)
        _LAST_COST = cost.get("requestedQueryCost", _LAST_COST)
        return data["data"]["orders"]
    raise RuntimeError(f"Shopify GraphQL: échec après 8 tentatives. {last_err}")


def _included(node) -> bool:
    if (node.get("displayFinancialStatus") or "").upper() == "VOIDED":
        return False
    if (node.get("sourceName") or "").lower() not in ALLOWED_SOURCES:
        return False
    tags = [t.strip().lower() for t in (node.get("tags") or [])]
    return not any(x in tags for x in EXCLUDE_ORDER_TAGS)


def _first_name(node) -> str:
    return ((node.get("shippingAddress") or {}).get("firstName")
            or (node.get("customer") or {}).get("firstName") or "")


def _load_cache(client) -> dict:
    try:
        rows = client.query(f"SELECT name, gender FROM `{CACHE_TABLE}`",
                            location=os.environ.get("BQ_LOCATION", "EU")).result()
        return {r["name"]: r["gender"] for r in rows}
    except Exception:  # noqa: BLE001 (table absente au 1er run)
        return {}


def _gg_detector():
    try:
        import gender_guesser.detector as gd
        return gd.Detector(case_sensitive=False)
    except Exception:  # noqa: BLE001
        return None


def _claude_classify(names: list[str]) -> tuple[dict, dict]:
    """Retourne ({name_normalisé: 'H'/'F'/'U'}, usage). Vide si pas de clé.
    usage = {names, input_tokens, output_tokens, calls}."""
    usage = {"names": len(names), "input_tokens": 0, "output_tokens": 0, "calls": 0}
    if not ANTHROPIC_KEY or not names:
        return {}, usage
    out = {}
    for i in range(0, len(names), 80):
        batch = names[i:i + 80]
        prompt = ("Classe chaque prénom comme H (homme), F (femme) ou U (incertain/mixte). "
                  "Prénoms francophones, anglophones et arabophones (translittérés inclus). "
                  "Réponds UNIQUEMENT un JSON objet {\"prenom\":\"H|F|U\"}.\nPrénoms : "
                  + ", ".join(batch))
        try:
            r = _SESSION.post("https://api.anthropic.com/v1/messages",
                headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": CLAUDE_MODEL, "max_tokens": 2000,
                      "messages": [{"role": "user", "content": prompt}]}, timeout=90)
            data = r.json()
            u = data.get("usage", {}) or {}
            usage["input_tokens"] += int(u.get("input_tokens", 0))
            usage["output_tokens"] += int(u.get("output_tokens", 0))
            usage["calls"] += 1
            txt = data["content"][0]["text"]
            txt = txt[txt.find("{"): txt.rfind("}") + 1]
            for k, v in json.loads(txt).items():
                g = str(v).strip().upper()
                out[normalize(k)] = g if g in ("H", "F") else "U"
        except Exception as e:  # noqa: BLE001
            print(f"[gender] Claude batch échoué : {e}")
    return out, usage


def _log_claude_usage(client, usage: dict):
    if usage.get("calls", 0) <= 0:
        return
    row = [{"ts": datetime.now(timezone.utc).isoformat(), "task": "gender",
            "names": usage["names"], "input_tokens": usage["input_tokens"],
            "output_tokens": usage["output_tokens"]}]
    cfg = bigquery.LoadJobConfig(write_disposition="WRITE_APPEND")
    client.load_table_from_json(row, f"{BQ_PROJECT}.{BQ_DATASET}.claude_usage",
                                job_config=cfg).result()


def ingest(since: str, until: str) -> int:
    query_string = f"created_at:>={since}T00:00:00Z AND created_at:<={until}T23:59:59Z"
    now = datetime.now(timezone.utc).isoformat()
    client = bigquery.Client(project=BQ_PROJECT)
    cache = _load_cache(client)
    gg = _gg_detector()

    # 1) crawl -> (day, prénom normalisé)
    pairs: list[tuple[str, str]] = []
    cursor, has_next = None, True
    while has_next:
        orders = _graphql(query_string, cursor)
        for node in orders["nodes"]:
            if not _included(node):
                continue
            n = normalize(_first_name(node))
            day = datetime.fromisoformat(node["createdAt"].replace("Z", "+00:00")) \
                .astimezone(LOCAL_TZ).strftime("%Y-%m-%d")
            pairs.append((day, n))
        has_next = orders["pageInfo"]["hasNextPage"]
        cursor = orders["pageInfo"]["endCursor"]
        avail, restore = _THROTTLE.get("currentlyAvailable"), _THROTTLE.get("restoreRate")
        time.sleep(min(2.0, (_LAST_COST - avail) / restore) if (avail is not None and restore and avail < _LAST_COST) else 0.2)

    # 2) résolution des prénoms uniques
    uniq = {n for _, n in pairs if n}
    resolved: dict = {}
    new_cache: dict = {}
    to_claude = []
    for n in uniq:
        g = local_gender(n)
        if g:
            resolved[n] = g; continue
        if n in cache:
            resolved[n] = cache[n]; continue
        if gg:
            r = gg.get_gender(n.capitalize())
            if r in ("male", "mostly_male"):
                resolved[n] = new_cache[n] = "H"; continue
            if r in ("female", "mostly_female"):
                resolved[n] = new_cache[n] = "F"; continue
        to_claude.append(n)
    claude_map, usage = _claude_classify(to_claude)
    for n, g in claude_map.items():
        resolved[n] = new_cache[n] = g
    _log_claude_usage(client, usage)

    # 3) agrégation date x genre
    counts: dict = defaultdict(int)
    for day, n in pairs:
        counts[(day, resolved.get(n, "U") if n else "U")] += 1
    rows = [{"date": d, "gender": g, "units": u, "updated_at": now}
            for (d, g), u in counts.items()]
    bq_io.load_replace_window(client, f"{BQ_PROJECT}.{BQ_DATASET}.gender_daily", rows, since, until)

    # 4) cache : réécriture complète (existant + nouveaux)
    if new_cache:
        merged = dict(cache); merged.update(new_cache)
        bq_io.load_replace_all(client, CACHE_TABLE,
                               [{"name": k, "gender": v, "updated_at": now} for k, v in merged.items()])
    src = "dico+gg+cache" + ("+claude" if ANTHROPIC_KEY else "")
    print(f"[gender] {len(pairs)} commandes web, {len(uniq)} prénoms uniques, {len(to_claude)} envoyés à Claude ({src})")
    return len(rows)


def refresh(days: int = 45) -> int:
    today = datetime.now(LOCAL_TZ).date()
    return ingest((today - timedelta(days=days)).isoformat(), today.isoformat())


def backfill(months: int = 24) -> int:
    today = datetime.now(LOCAL_TZ).date()
    ws, total = today - timedelta(days=int(months * 30.5)), 0
    while ws <= today:
        we = min(ws + timedelta(days=30), today)
        print(f"=== Gender {ws} -> {we} ===", flush=True)
        total += ingest(ws.isoformat(), we.isoformat())
        ws = we + timedelta(days=1)
    print("[gender] BACKFILL terminé")
    return total


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "backfill":
        backfill(int(sys.argv[2]) if len(sys.argv) > 2 else 24)
    else:
        refresh(int(sys.argv[2]) if len(sys.argv) > 2 else 45)
