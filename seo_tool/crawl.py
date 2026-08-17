"""
Orchestrateur du diagnostic (read-only). Produit le snapshot complet :
  { generated_at, totals, scores, issues[] }
"""

from __future__ import annotations
from datetime import datetime, timezone

from seo_tool import config, detectors, scoring
from seo_tool.shopify_client import ShopifyClient, ShopifyError


def run(client: ShopifyClient | None = None, include_translations: bool = True,
        log=print) -> dict:
    client = client or ShopifyClient()

    log("· Crawl produits…")
    all_products = detectors.fetch_products(client)
    products = [p for p in all_products if config.product_in_scope(p)]
    log(f"  {len(all_products)} produits récupérés → {len(products)} dans le périmètre "
        f"(Solaires/Optique, hors inactif+non publié+pré-2021)")

    log("· Crawl collections…")
    collections = detectors.fetch_collections(client)
    log(f"  {len(collections)} collections")

    issues: list[dict] = []
    issues += list(detectors.detect_collection_seo(collections))
    issues += list(detectors.detect_image_alt(products))
    issues += list(detectors.detect_product_meta(products))

    translatable_units = 0
    translations_evaluated = False
    if include_translations:
        log("· Crawl traductions (produits + collections)…")
        try:
            for rtype in ("PRODUCT", "COLLECTION"):
                t_issues, units = detectors.detect_translations(client, rtype)
                issues += t_issues
                translatable_units += units
            translations_evaluated = True
            log(f"  {translatable_units} unités traduisibles analysées")
        except ShopifyError as e:
            # ex. scope read_translations absent -> on n'interrompt pas le diagnostic.
            log(f"  ⚠ traductions non évaluées : {e}")

    totals = {
        "products_fetched": len(all_products),
        "products_in_scope": len(products),
        "products_eligible": sum(1 for p in products
                                 if not config.is_low_value_product(p.get("handle", ""))),
        "images_total": sum(len([m for m in (p.get("media") or {}).get("nodes", [])
                                 if m and "id" in m]) for p in products),
        "collections_total": len(collections),
        "collections_indexable": sum(1 for c in collections
                                     if (c.get("productsCount") or {}).get("count", 0) > 0
                                     and not config.is_operational_collection(c.get("handle", ""))),
        "translatable_units": translatable_units,
    }

    scores = scoring.compute(issues, totals)
    issues.sort(key=lambda i: (-i["priority_score"], i["type"]))
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "shop": config.SHOP_URL,
        "totals": totals,
        "scores": scores,
        "issues": issues,
    }
