"""
Catalogue produit + stock par emplacement -> BigQuery (snapshots remplacés à chaque run).

Un seul crawl Shopify produit deux tables :
  - product_catalog  : 1 ligne par VARIANTE (clé = shopify_variant_id) avec métadonnées
                       (product_type -> Optique/Solaire, genre depuis les tags Homme/Femme,
                        prix, tags, tags "datés" de collection, statut, publié Online Store).
  - product_stock    : agrégats de stock par variante (warehouse web + OOS retail).

Clé de jointure avec le web et le retail = shopify_variant_id (présent des deux côtés).

Règles OOS (spec Alexandre) :
  - WEB : indisponible si NON (status ACTIVE ET publié Online Store ET stock warehouse > 0).
          Warehouse web = « 155 Charonne - Warehouse ». « Publié Online Store » = publishedAt
          non nul (même signal que shopify_inventory, déjà fiable en prod).
  - RETAIL : nombre de boutiques où le stock est à 0. On EXCLUT toujours J and J,
          Nice - Cap 3000, Blagnac - Centre Commercial, Logecom — et le warehouse (= web).
"""

from __future__ import annotations
import os
import re
import time
from datetime import datetime, timezone

import requests
from google.cloud import bigquery

from ingestion import bq_io

SHOP_URL     = os.environ["SHOPIFY_SHOP_URL"]
ACCESS_TOKEN = os.environ["SHOPIFY_ACCESS_TOKEN"].strip()
# `InventoryLevel.available` a été retiré ; `quantities(names:["available"])` est GA depuis
# 2024-04. On épingle ce module sur une version récente (indépendamment du reste du repo).
API_VERSION  = os.environ.get("PRODUCTS_API_VERSION", "2025-01")
BQ_PROJECT   = os.environ["BQ_PROJECT"]
BQ_DATASET   = os.environ.get("BQ_DATASET", "lpl_cockpit")

GRAPHQL_URL = f"https://{SHOP_URL}/admin/api/{API_VERSION}/graphql.json"
_SESSION = requests.Session()

WAREHOUSE_WEB = "155 Charonne - Warehouse"
# Stock e-commerce (web) = somme de ces emplacements de fulfilment web.
WEB_LOCATIONS = {"155 Charonne - Warehouse", "Logecom"}
# Emplacements JAMAIS comptés dans l'OOS retail (les 2 web + 3 boutiques exclues).
RETAIL_EXCLUDE = {"J and J", "Nice - Cap 3000", "Blagnac - Centre Commercial", "Logecom"}

# Bloc stock : nécessite le scope read_inventory (+ read_locations pour location.name).
# Si le scope manque, on retombe sur une requête SANS stock (catalogue seul).
_INVENTORY_BLOCK = """
          inventoryItem {
            inventoryLevels(first: 50) {
              nodes {
                location { name }
                quantities(names: ["available"]) { name quantity }
              }
            }
          }"""


def _query(with_inventory: bool) -> str:
    inv = _INVENTORY_BLOCK if with_inventory else ""
    return f"""
query ($cursor: String) {{
  products(first: 25, after: $cursor) {{
    pageInfo {{ hasNextPage endCursor }}
    nodes {{
      id title status productType tags publishedAt
      variants(first: 25) {{
        nodes {{
          id sku price{inv}
        }}
      }}
    }}
  }}
}}"""


class _ScopeMissing(Exception):
    """Le scope read_inventory manque : on bascule sur le catalogue seul."""


def _graphql(cursor, with_inventory):
    for attempt in range(6):
        r = _SESSION.post(GRAPHQL_URL,
                          json={"query": _query(with_inventory), "variables": {"cursor": cursor}},
                          headers={"X-Shopify-Access-Token": ACCESS_TOKEN}, timeout=90)
        data = r.json()
        if data.get("errors"):
            errs = data["errors"]
            if any((e.get("extensions") or {}).get("code") == "THROTTLED" for e in errs):
                time.sleep(2 * (attempt + 1)); continue
            if with_inventory and any((e.get("extensions") or {}).get("code") == "ACCESS_DENIED"
                                      for e in errs):
                raise _ScopeMissing()
            raise RuntimeError(f"Shopify GraphQL: {errs}")
        return data["data"]["products"]
    raise RuntimeError("Shopify GraphQL: throttling persistant")


def _gid_num(gid: str) -> str:
    """gid://shopify/ProductVariant/123 -> '123'."""
    return (gid or "").rsplit("/", 1)[-1]


def _gender(tags: list[str]) -> str:
    """Genre produit depuis les tags 'Homme/*' et 'Femme/*'. Les deux => Mixte."""
    low = [t.lower() for t in tags]
    has_h = any(t.startswith("homme") for t in low)
    has_f = any(t.startswith("femme") for t in low)
    if has_h and has_f:
        return "Mixte"
    if has_h:
        return "Homme"
    if has_f:
        return "Femme"
    return "Indéterminé"


def _category(product_type: str) -> str:
    pt = (product_type or "").strip().lower()
    if "solaire" in pt:
        return "Solaire"
    if "monture optique" in pt or pt == "optique":
        return "Optique"
    return "Autre"


# Tag "daté" de collection/drop : contient une séquence d'au moins 4 chiffres
# (ex. LPLXTB0424, lepetitlunetier0823, new29-1102025) OU un mois FR/EN écrit.
_MONTHS = ("janv|févr|fevr|mars|avril|avr|mai|juin|juil|août|aout|sept|oct|nov|déc|dec"
           "|jan|feb|mar|apr|jun|jul|aug|sep|nov|dec")
_DATE_TAG = re.compile(rf"\d{{4,}}|({_MONTHS})", re.IGNORECASE)


def _date_tags(tags: list[str]) -> list[str]:
    out = []
    for t in tags:
        tl = t.lower()
        # on écarte les tags d'attributs structurés « CLÉ|valeur » et les tailles en mm
        if "|" in t or "mm" in tl:
            continue
        if _DATE_TAG.search(t):
            out.append(t)
    return out


def refresh() -> dict:
    now = datetime.now(timezone.utc).isoformat()
    cat_rows, stock_rows = [], []
    cursor, has_next = None, True
    with_inventory = True
    while has_next:
        try:
            p = _graphql(cursor, with_inventory)
        except _ScopeMissing:
            # 1re page sans le scope : on repart au début en mode catalogue seul.
            print("[products] scope read_inventory absent -> catalogue seul (stock ignoré). "
                  "Ajouter read_inventory + read_locations à l'app pour l'OOS.", flush=True)
            with_inventory = False
            cat_rows, stock_rows = [], []
            cursor, has_next = None, True
            continue
        for prod in p["nodes"]:
            tags = prod.get("tags") or []
            gender = _gender(tags)
            category = _category(prod.get("productType"))
            dtags = _date_tags(tags)
            status = prod.get("status")
            published = prod.get("publishedAt") is not None
            for v in prod["variants"]["nodes"]:
                vid = _gid_num(v.get("id"))
                levels = (v.get("inventoryItem") or {}).get("inventoryLevels", {}).get("nodes", [])
                by_loc = {}
                for lv in levels:
                    name = (lv.get("location") or {}).get("name")
                    if name is None:
                        continue
                    # API récente : available via quantities(names:["available"])
                    avail = 0
                    for qn in (lv.get("quantities") or []):
                        if qn.get("name") == "available":
                            avail = qn.get("quantity") or 0
                    by_loc[name] = avail
                warehouse = sum(by_loc.get(n, 0) for n in WEB_LOCATIONS)  # stock web = Charonne + Logecom
                total = sum(by_loc.values())
                # Boutiques retail = tout sauf emplacements web + boutiques exclues.
                retail_locs = {n: q for n, q in by_loc.items()
                               if n not in WEB_LOCATIONS and n not in RETAIL_EXCLUDE}
                retail_oos = sum(1 for q in retail_locs.values() if q <= 0)
                web_available = bool(status == "ACTIVE" and published and warehouse > 0)

                cat_rows.append({
                    "shopify_variant_id": vid,
                    "shopify_product_id": _gid_num(prod.get("id")),
                    "sku": v.get("sku") or None,
                    "title": prod.get("title"),
                    "product_type": prod.get("productType"),
                    "category": category,
                    "gender": gender,
                    "price": float(v["price"]) if v.get("price") not in (None, "") else None,
                    "status": status,
                    "published_online": published,
                    "tags": tags,
                    "date_tags": dtags,
                    "updated_at": now,
                })
                if with_inventory:
                    stock_rows.append({
                        "shopify_variant_id": vid,
                        "warehouse_available": warehouse,
                        "total_inventory": total,
                        "retail_loc_total": len(retail_locs),
                        "retail_loc_oos": retail_oos,
                        "web_available": web_available,
                        "updated_at": now,
                    })
        has_next = p["pageInfo"]["hasNextPage"]
        cursor = p["pageInfo"]["endCursor"]
        time.sleep(0.3)

    client = bigquery.Client(project=BQ_PROJECT)
    n1 = bq_io.load_replace_all(client, f"{BQ_PROJECT}.{BQ_DATASET}.product_catalog", cat_rows)
    # Stock : on ne réécrit product_stock QUE si le scope read_inventory est présent
    # (sinon on garderait un stock nul = tout en rupture, ce qui serait faux).
    if with_inventory:
        n2 = bq_io.load_replace_all(client, f"{BQ_PROJECT}.{BQ_DATASET}.product_stock", stock_rows)
    else:
        n2 = 0
    print(f"[products] {n1} variantes catalogue, {n2} lignes stock"
          f"{'' if with_inventory else ' (SCOPE read_inventory MANQUANT — stock ignoré)'}")
    return {"catalog": n1, "stock": n2, "inventory_scope": with_inventory}


if __name__ == "__main__":
    print(refresh())
