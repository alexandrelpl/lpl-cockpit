"""
Ingestion Shopify -> BigQuery : CA net ré-attribué à la DATE DE COMMANDE.

Port fidèle de l'Apps Script "VOLUME V5.7" d'Alexandre :
  net_sales = total commande - somme des remboursements RÉUSSIS (kind=REFUND, status=SUCCESS)
  La commande est comptée sur sa date de CRÉATION (timezone Europe/Paris),
  donc un remboursement, même tardif, est re-daté sur la commande d'origine.

Exclusions (identiques au script) :
  - tags contenant 'alan', 'wholesale' ou 'b2b'
  - displayFinancialStatus == 'VOIDED'
  - total commande == 0
  - sourceName hors {'web', 'just', '5448991'}

Segmentation : NEW vs EXISTING (1re commande du client) x catégorie
  (COMPTOIR / OPTIQUE / M&M / OTHERS) d'après les tags.

Modes :
  backfill(months=24)   -> historique complet
  refresh(days=40)      -> fenêtre glissante quotidienne (rattrape les refunds tardifs)

Le token Shopify est lu depuis la variable d'env SHOPIFY_ACCESS_TOKEN
(injectée par Secret Manager en prod). JAMAIS en dur dans le code.
"""

from __future__ import annotations
import os
import socket
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from google.cloud import bigquery

from ingestion import bq_io

# Garde-fou : impose un délai max à TOUTE opération socket (y compris la
# résolution DNS, que le timeout de `requests` ne couvre pas). Sans ça, un appel
# réseau peut « pendre » indéfiniment au lieu d'échouer (cas observé sur Cloud Run).
socket.setdefaulttimeout(120)

SHOP_URL      = os.environ["SHOPIFY_SHOP_URL"]            # ex: test-store20.myshopify.com (= PROD LPL)
ACCESS_TOKEN  = os.environ["SHOPIFY_ACCESS_TOKEN"].strip()   # .strip() : un \n parasite casse l'en-tête HTTP
API_VERSION   = os.environ.get("SHOPIFY_API_VERSION", "2024-01")
BQ_PROJECT    = os.environ["BQ_PROJECT"]
BQ_DATASET    = os.environ.get("BQ_DATASET", "lpl_cockpit")
LOCAL_TZ      = ZoneInfo(os.environ.get("TIMEZONE", "Europe/Paris"))

GRAPHQL_URL = f"https://{SHOP_URL}/admin/api/{API_VERSION}/graphql.json"
ALLOWED_SOURCES = {"web", "just", "5448991"}
# Segment « Test Europe » : commandes livrées dans ces pays (isolées du COS principal).
# (Champs stockés en *_nl pour raisons historiques.)
EUROPE_TEST_CC = {"AT", "DE", "ES", "IT", "NL", "PT"}
# Braderie = canal Syncio OU email thebradery (les deux en OR). Suivi HORS CA principal.
BRADERIE_EMAIL  = "logistique@thebradery.com"
BRADERIE_APP    = "syncio multi store sync"
BRADERIE_SOURCE = "1615469"

_SESSION = requests.Session()   # connexion HTTP persistante (keep-alive)
_THROTTLE: dict = {}            # dernier throttleStatus GraphQL renvoyé par Shopify
_LAST_COST = 50                 # coût de la dernière requête (pour la cadence adaptative)

ORDERS_QUERY = """
query ($query: String!, $cursor: String) {
  orders(first: 50, query: $query, after: $cursor, sortKey: CREATED_AT) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id
      createdAt
      totalPriceSet { shopMoney { amount } }
      displayFinancialStatus
      tags
      sourceName
      email
      app { name }
      shippingAddress { countryCodeV2 }
      customer { numberOfOrders }
      refunds { transactions(first: 5) { nodes { kind status amountSet { shopMoney { amount } } } } }
    }
  }
}
"""

CATEGORY_KEYS = [
    "comptoir_new", "comptoir_existing", "optique_new", "optique_existing",
    "mm_new", "mm_existing", "others_new", "others_existing",
]


def _graphql(query_string: str, cursor: str | None) -> dict:
    """Un appel GraphQL robuste : reprise sur incident réseau + gestion du throttling."""
    payload = {"query": ORDERS_QUERY, "variables": {"query": query_string, "cursor": cursor}}
    headers = {"X-Shopify-Access-Token": ACCESS_TOKEN, "Content-Type": "application/json"}
    last_err = None
    for attempt in range(8):
        try:
            # session persistante ; timeout=(connexion, lecture) ; socket par défaut (120s) couvre le DNS
            r = _SESSION.post(GRAPHQL_URL, json=payload, headers=headers, timeout=(15, 90))
        except requests.exceptions.RequestException as e:
            last_err = e
            print(f"  [shopify] incident réseau (tentative {attempt + 1}/8) : {e}", flush=True)
            time.sleep(3 * (attempt + 1))
            continue
        if r.status_code == 429:
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code in (401, 403):
            raise RuntimeError(f"Shopify AUTH {r.status_code} : token invalide ou scope read_orders "
                               f"manquant -> mettre à jour le secret SHOPIFY_ACCESS_TOKEN. {r.text[:200]}")
        if r.status_code >= 500:
            print(f"  [shopify] HTTP {r.status_code} (tentative {attempt + 1}/8)", flush=True)
            time.sleep(3 * (attempt + 1))
            continue
        data = r.json()
        errs = data.get("errors")
        if errs:
            if isinstance(errs, str):   # 401/403 renvoient souvent errors=str -> message clair
                raise RuntimeError(f"Shopify GraphQL (token/scope ?) : {errs}")
            if any(e.get("extensions", {}).get("code") == "THROTTLED" for e in errs):
                time.sleep(2 * (attempt + 1))
                continue
            raise RuntimeError(f"Shopify GraphQL errors: {errs}")
        cost = data.get("extensions", {}).get("cost", {})
        global _THROTTLE, _LAST_COST
        _THROTTLE = cost.get("throttleStatus", _THROTTLE)
        _LAST_COST = cost.get("requestedQueryCost", _LAST_COST)
        return data["data"]["orders"]
    raise RuntimeError(f"Shopify GraphQL: échec après 8 tentatives. Dernière erreur réseau : {last_err}")


def _analyze_order(order: dict) -> dict | None:
    """Réplique analyzeOrderVolumeUnified. Retourne None si la commande est EXCLUE."""
    original_total = float((order.get("totalPriceSet") or {}).get("shopMoney", {}).get("amount") or 0)

    refunded = 0.0
    for rf in (order.get("refunds") or []):
        for trx in ((rf.get("transactions") or {}).get("nodes") or []):
            if trx.get("kind") == "REFUND" and trx.get("status") == "SUCCESS":
                refunded += float(trx["amountSet"]["shopMoney"]["amount"])
    net_sales = original_total - refunded

    tags = (order.get("tags") or "")
    tags = " ".join(tags).lower() if isinstance(tags, list) else str(tags).lower()
    source = (order.get("sourceName") or "").lower()
    email = (order.get("email") or "").strip().lower()
    app_name = ((order.get("app") or {}).get("name") or "").strip().lower()

    # Garde-fou commun aux deux canaux : commande annulée ou à 0 -> ignorée.
    if order.get("displayFinancialStatus") == "VOIDED" or original_total == 0:
        return None

    # Braderie (canal Syncio OU email thebradery) : sortie du CA principal, suivie à part.
    if email == BRADERIE_EMAIL or app_name == BRADERIE_APP or source == BRADERIE_SOURCE:
        return {"channel": "braderie", "net_sales": net_sales}

    # Canal principal : mêmes exclusions qu'avant (tags B2B, source autorisée).
    if ("alan" in tags or "wholesale" in tags or "b2b" in tags
            or source not in ALLOWED_SOURCES):
        return None

    # Segmentation client allégée via le scalaire numberOfOrders (au lieu de la sous-requête
    # customer.orders, très coûteuse en GraphQL) : « new » = client à 1 commande au total,
    # « existing » = client récurrent. NB : définition « one-time vs repeat » (état actuel du
    # client), légèrement différente de l'ancienne « 1re commande exacte » par commande.
    cust = order.get("customer") or {}
    n = cust.get("numberOfOrders")
    try:
        customer_type = "existing" if (n is not None and int(n) > 1) else "new"
    except (TypeError, ValueError):
        customer_type = "new"

    if "mix & match" in tags:
        cat = "mm"
    elif "optique" in tags or "avec verres a la vue" in tags:
        cat = "optique"
    elif "comptoir" in tags or "sans verres a la vue" in tags:
        cat = "comptoir"
    else:
        cat = "others"

    cc = ((order.get("shippingAddress") or {}).get("countryCodeV2") or "").upper()
    return {"channel": "main", "net_sales": net_sales, "cc": cc,
            "bucket": f"{cat}_{customer_type}", "is_nl": cc in EUROPE_TEST_CC}


def _fetch_range(since: str, until: str) -> dict[str, dict]:
    """Agrège les commandes incluses par date locale (Europe/Paris)."""
    query_string = f"created_at:>={since}T00:00:00Z AND created_at:<={until}T23:59:59Z"
    stats: dict[str, dict] = {}
    braderie: dict[str, dict] = {}   # jour -> {orders, net_sales} (canal Braderie, hors CA principal)
    country: dict[tuple, dict] = {}  # (jour, pays) -> {orders, ca} (Test Europe, détail par pays)
    cursor, has_next, page = None, True, 0
    while has_next:
        orders = _graphql(query_string, cursor)
        page += 1
        if page % 20 == 0:
            done = sum(d["orders"] for d in stats.values())
            print(f"  ... {page} pages lues, {done} commandes incluses", flush=True)
        for order in orders["nodes"]:
            res = _analyze_order(order)
            if res is None:
                continue
            day = datetime.fromisoformat(order["createdAt"].replace("Z", "+00:00")) \
                .astimezone(LOCAL_TZ).strftime("%Y-%m-%d")
            if res["channel"] == "braderie":
                b = braderie.setdefault(day, {"net_sales": 0.0, "orders": 0})
                b["net_sales"] += res["net_sales"]
                b["orders"]    += 1
                continue
            d = stats.setdefault(day, {"net_sales": 0.0, "orders": 0, "net_sales_nl": 0.0,
                                       "orders_nl": 0, **{k: 0 for k in CATEGORY_KEYS}})
            d["net_sales"] += res["net_sales"]
            d["orders"]    += 1
            d[res["bucket"]] += 1
            if res["is_nl"]:
                d["net_sales_nl"] += res["net_sales"]
                d["orders_nl"]    += 1
                ck = country.setdefault((day, res["cc"]), {"orders": 0, "ca": 0.0})
                ck["orders"] += 1
                ck["ca"]     += res["net_sales"]
        has_next = orders["pageInfo"]["hasNextPage"]
        cursor   = orders["pageInfo"]["endCursor"]
        # Cadence adaptative : on n'attend QUE si le budget GraphQL Shopify est bas.
        avail, restore = _THROTTLE.get("currentlyAvailable"), _THROTTLE.get("restoreRate")
        if avail is not None and restore:
            if avail < _LAST_COST:
                time.sleep(min(2.0, (_LAST_COST - avail) / restore))
        else:
            time.sleep(0.2)
    return stats, braderie, country


def _write_bq(stats: dict[str, dict], since: str, until: str) -> int:
    """Remplace la fenêtre [since, until] via load job (cf. bq_io)."""
    client = bigquery.Client(project=BQ_PROJECT)
    table = f"{BQ_PROJECT}.{BQ_DATASET}.shopify_orders_daily"
    now = datetime.now(timezone.utc).isoformat()

    rows = []
    for day, d in sorted(stats.items()):
        rows.append({
            "date": day,
            "net_sales": round(d["net_sales"], 2),
            "orders": d["orders"],
            "net_sales_nl": round(d.get("net_sales_nl", 0.0), 2),
            "orders_nl": d.get("orders_nl", 0),
            "comptoir_new": d["comptoir_new"], "comptoir_existing": d["comptoir_existing"],
            "optique_new": d["optique_new"], "optique_existing": d["optique_existing"],
            "mm_new": d["mm_new"], "mm_existing": d["mm_existing"],
            "others_new": d["others_new"], "others_existing": d["others_existing"],
            "updated_at": now,
        })
    return bq_io.load_replace_window(client, table, rows, since, until)


def _write_braderie(braderie: dict[str, dict], since: str, until: str) -> int:
    """Remplace la fenêtre [since, until] de braderie_daily (canal Syncio/thebradery)."""
    client = bigquery.Client(project=BQ_PROJECT)
    table = f"{BQ_PROJECT}.{BQ_DATASET}.braderie_daily"
    now = datetime.now(timezone.utc).isoformat()
    rows = [{"date": day, "orders": d["orders"],
             "net_sales": round(d["net_sales"], 2), "updated_at": now}
            for day, d in sorted(braderie.items())]
    return bq_io.load_replace_window(client, table, rows, since, until)


def _write_country(country: dict[tuple, dict], since: str, until: str) -> int:
    """Remplace la fenêtre [since, until] de shopify_country_daily (détail Test Europe par pays)."""
    client = bigquery.Client(project=BQ_PROJECT)
    table = f"{BQ_PROJECT}.{BQ_DATASET}.shopify_country_daily"
    now = datetime.now(timezone.utc).isoformat()
    rows = [{"date": day, "country": cc, "orders": d["orders"],
             "ca": round(d["ca"], 2), "updated_at": now}
            for (day, cc), d in sorted(country.items())]
    return bq_io.load_replace_window(client, table, rows, since, until)


def ingest(since: str, until: str) -> int:
    print(f"[shopify] fetch {since} -> {until}")
    stats, braderie, country = _fetch_range(since, until)
    n = _write_bq(stats, since, until)
    nb = _write_braderie(braderie, since, until)
    nc = _write_country(country, since, until)
    print(f"[shopify] {n} jours · {nb} jours Braderie · {nc} lignes pays (Test Europe)")
    return n


def refresh(days: int = 40) -> int:
    today = datetime.now(LOCAL_TZ).date()
    since = (today - timedelta(days=days)).isoformat()
    return ingest(since, today.isoformat())


def backfill(months: int = 24) -> int:
    """Charge l'historique par tranches mensuelles (observable et repartable)."""
    today = datetime.now(LOCAL_TZ).date()
    start = today - timedelta(days=int(months * 30.5))
    total, window_start, chunk = 0, start, 0
    while window_start <= today:
        window_end = min(window_start + timedelta(days=30), today)
        chunk += 1
        print(f"=== Tranche {chunk} : {window_start} -> {window_end} ===", flush=True)
        total += ingest(window_start.isoformat(), window_end.isoformat())
        window_start = window_end + timedelta(days=1)
    print(f"[shopify] BACKFILL TERMINÉ : {total} jours écrits au total")
    return total


if __name__ == "__main__":
    import sys
    mode = sys.argv[1] if len(sys.argv) > 1 else "refresh"
    if mode == "backfill":
        backfill(int(sys.argv[2]) if len(sys.argv) > 2 else 24)
    else:
        refresh(int(sys.argv[2]) if len(sys.argv) > 2 else 40)
