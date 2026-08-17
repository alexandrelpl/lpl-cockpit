"""
Les 4 détecteurs d'issues v1 (read-only). Chacun produit des enregistrements `make_issue`.

Stratégie de crawl :
- Les produits sont récupérés UNE fois (champs meta + media) et alimentent 2 détecteurs
  (alt-text + meta produit).
- Les collections une fois.
- Les traductions via l'API translatableResources (PRODUCT puis COLLECTION).
"""

from __future__ import annotations

from seo_tool import config, market_data
from seo_tool.issues import make_issue, empty
from seo_tool.shopify_client import ShopifyClient

# --------- Requêtes ---------

PRODUCTS_QUERY = """
query($cursor: String) {
  products(first: 50, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id handle title productType tags status createdAt publishedAt
      seo { title description }
      media(first: 25) { nodes { ... on MediaImage { id image { url altText } } } }
    }
  }
}
"""

COLLECTIONS_QUERY = """
query($cursor: String) {
  collections(first: 50, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id handle title
      seo { title description }
      descriptionHtml
      productsCount { count }
    }
  }
}
"""

TRANSLATIONS_QUERY = """
query($cursor: String, $locale: String!) {
  translatableResources(first: 50, after: $cursor, resourceType: %s) {
    pageInfo { hasNextPage endCursor }
    nodes {
      resourceId
      translatableContent { key value digest locale }
      translations(locale: $locale) { key value outdated }
    }
  }
}
"""


def fetch_products(client: ShopifyClient) -> list[dict]:
    return list(client.paginate(PRODUCTS_QUERY, "products"))


def fetch_collections(client: ShopifyClient) -> list[dict]:
    return list(client.paginate(COLLECTIONS_QUERY, "collections"))


# --------- Détecteurs ---------

def detect_collection_seo(collections: list[dict]):
    for c in collections:
        handle = c.get("handle", "")
        count = (c.get("productsCount") or {}).get("count", 0)
        # Opérationnelle/technique OU vide -> relève de l'index bloat, pas de l'enrichissement SEO.
        if config.is_operational_collection(handle) or count == 0:
            continue
        seo = c.get("seo") or {}
        missing = []
        if empty(seo.get("title")):
            missing.append("seo.title")
        if empty(seo.get("description")):
            missing.append("seo.description")
        if empty(c.get("descriptionHtml")):
            missing.append("descriptionHtml")
        if not missing:
            continue
        url = f"https://lepetitlunetier.com/collections/{handle}"
        mkt = market_data.market_info(handle)
        traffic = market_data.collection_traffic(handle)
        tier = market_data.collection_tier(handle)
        ctx = {"title": c.get("title"), "products_count": count, "missing": missing, "url": url,
               "organic_traffic": traffic, "is_market": mkt is not None, "tier": tier,
               "market_label": (mkt or {}).get("label"), "target_kw": (mkt or {}).get("kw"),
               "search_volume": (mkt or {}).get("vol")}
        for field in missing:
            yield make_issue(
                "collection_seo", "high", "collection", c["id"], handle, field,
                current_value="", context=ctx,
                # priorité = marché + trafic prouvé > marché (opportunité) > reste.
                priority_score=market_data.collection_priority(handle),
            )


def detect_image_alt(products: list[dict]):
    for p in products:
        for m in (p.get("media") or {}).get("nodes", []):
            if not m or "id" not in m:
                continue
            img = m.get("image") or {}
            if empty(img.get("altText")):
                yield make_issue(
                    "image_alt", "high", "image", m["id"], p.get("handle"), "media.alt",
                    current_value="",
                    context={"product_id": p["id"], "product_title": p.get("title"),
                             "product_type": p.get("productType"), "image_url": img.get("url")},
                    priority_score=1,
                )


def detect_product_meta(products: list[dict]):
    for p in products:
        if config.is_low_value_product(p.get("handle", "")):
            continue
        seo = p.get("seo") or {}
        missing = [f for f, v in (("seo.title", seo.get("title")),
                                  ("seo.description", seo.get("description"))) if empty(v)]
        for field in missing:
            yield make_issue(
                "product_meta", "medium", "product", p["id"], p.get("handle"), field,
                current_value="",
                context={"title": p.get("title"), "product_type": p.get("productType"),
                         "tags": p.get("tags"), "missing": missing},
                priority_score=1,
            )


def detect_translations(client: ShopifyClient, resource_type: str, locale: str | None = None):
    """resource_type = 'PRODUCT' ou 'COLLECTION'. Issue si une clé SEO n'a pas de
    traduction dans la locale cible (ou est marquée outdated).
    Retourne (issues, units_total) — units_total = nb de clés traduisibles (source non vide),
    qui sert de dénominateur au score traductions."""
    locale = locale or config.TARGET_LOCALE
    q = TRANSLATIONS_QUERY % resource_type
    obj_type = resource_type.lower()
    issues, units = [], 0
    for node in client.paginate(q, "translatableResources", {"locale": locale}):
        rid = node["resourceId"]
        src = {c["key"]: c for c in node.get("translatableContent", [])}
        trans = {t["key"]: t for t in node.get("translations", [])}
        handle = (src.get("handle") or {}).get("value", "")
        for key in config.TRANSLATABLE_KEYS:
            c = src.get(key)
            if not c or empty(c.get("value")):
                continue  # rien à traduire si la source est vide
            units += 1
            t = trans.get(key)
            if t and not t.get("outdated"):
                continue  # déjà traduit et à jour
            issues.append(make_issue(
                "translation_missing", "medium", obj_type, rid, handle,
                f"translation.{locale}.{key}",
                current_value="(outdated)" if t else "",
                context={"key": key, "locale": locale, "digest": c.get("digest"),
                         "source_present": True, "outdated": bool(t)},
                priority_score=1,
            ))
    return issues, units
