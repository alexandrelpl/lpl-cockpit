"""
Calcul des scores de santé SEO à partir du snapshot d'issues + des totaux du crawl.

score d'une catégorie = 100 × (1 − objets_affectés / objets_total)  (100 = parfait).
score global = moyenne des catégories pondérée par la sévérité.
"""

from __future__ import annotations
from seo_tool.issues import SEVERITY_WEIGHT

CATEGORIES = {
    "collection_seo":      ("Pages collections", "collections_indexable", "high"),
    "image_alt":           ("Alt-text images", "images_total", "high"),
    "product_meta":        ("Meta produits", "products_eligible", "medium"),
    "translation_missing": ("Traductions", "translatable_units", "medium"),
}


def _affected_objects(issues, type_):
    return {i["object_id"] for i in issues if i["type"] == type_}


def compute(issues: list[dict], totals: dict) -> dict:
    cats = {}
    for type_, (label, total_key, severity) in CATEGORIES.items():
        total = max(0, int(totals.get(total_key, 0)))
        affected = len(_affected_objects(issues, type_))
        n_issues = sum(1 for i in issues if i["type"] == type_)
        # total == 0 -> catégorie NON évaluée (ex. scope manquant), score = None (pas 100).
        score = round(max(0.0, 100 * (1 - affected / total)), 1) if total else None
        cats[type_] = {
            "label": label, "severity": severity,
            "total": total, "affected": affected, "issues": n_issues,
            "to_fix": n_issues, "score": score,
            "evaluated": total > 0,
        }
    # score global pondéré par sévérité, sur les catégories réellement évaluées.
    scored = [c for c in cats.values() if c["score"] is not None]
    num = sum(c["score"] * SEVERITY_WEIGHT[c["severity"]] for c in scored)
    den = sum(SEVERITY_WEIGHT[c["severity"]] for c in scored) or 1
    return {"global": round(num / den, 1) if scored else None, "categories": cats,
            "total_issues": len(issues)}
