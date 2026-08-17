"""
Configuration de l'outil SEO (volet diagnostic, read-only).

Variables d'environnement requises :
  SHOPIFY_SHOP_URL      ex. test-store20.myshopify.com  (domaine technique de prod)
  SHOPIFY_ADMIN_TOKEN   token d'une app custom avec scopes LECTURE :
                        read_products, read_translations, read_online_store_pages
  SHOPIFY_API_VERSION   défaut 2024-01

NB : token DÉDIÉ à l'outil SEO (principe « un secret par app »), distinct de celui
du cockpit marketing. Pour le volet 2 (écriture), il faudra y ajouter
write_products + write_translations.
"""

from __future__ import annotations
import os

SHOP_URL    = os.environ.get("SHOPIFY_SHOP_URL", "")
ADMIN_TOKEN = os.environ.get("SHOPIFY_ADMIN_TOKEN", "").strip()
API_VERSION = os.environ.get("SHOPIFY_API_VERSION", "2024-01")

GRAPHQL_URL = f"https://{SHOP_URL}/admin/api/{API_VERSION}/graphql.json"

# Locale du marché secondaire à vérifier pour les traductions.
TARGET_LOCALE = os.environ.get("SEO_TARGET_LOCALE", "en")

# --- Collections opérationnelles / techniques : NON indexables -> relèvent de
# l'« index bloat », pas de l'enrichissement SEO. Détection par motif de handle. ---
OPERATIONAL_COLLECTION_PATTERNS = [
    # techniques / feed / doublons
    "tout-sauf", "tous-sauf", "kat-", "product-feed", "orderlyemails",
    "reelup", "-copie", "-copy", "createurs-de-contenu", "frontpage",
    # saisonnières / promo / capsules (pas de valeur SEO evergreen)
    "noel", "soldes", "black-friday", "french-days", "nouvelle-collection",
    "selection", "descente-givree", "sun-squad", "printemps-ete", "brazilian",
]

# --- Types/handles de produits à faible valeur SEO (priorité basse pour les meta). ---
LOW_VALUE_PRODUCT_HINTS = [
    "gift-card", "carte-cadeau", "coffret", "etui", "chaine", "support", "verres", "test",
]

# Clés traduisibles qui comptent pour le SEO (sur PRODUCT et COLLECTION).
TRANSLATABLE_KEYS = ["title", "body_html", "meta_title", "meta_description", "handle"]

# Limites éditoriales (pour le scoring qualitatif et, plus tard, la génération).
TITLE_MAX = 60
META_MAX = 155

OUTPUT_DIR = os.environ.get("SEO_OUTPUT_DIR", os.path.dirname(os.path.abspath(__file__)))


def check_env() -> list[str]:
    """Retourne la liste des variables manquantes (vide si tout est là)."""
    missing = []
    if not SHOP_URL:
        missing.append("SHOPIFY_SHOP_URL")
    if not ADMIN_TOKEN:
        missing.append("SHOPIFY_ADMIN_TOKEN")
    return missing


def is_operational_collection(handle: str) -> bool:
    h = (handle or "").lower()
    return any(p in h for p in OPERATIONAL_COLLECTION_PATTERNS)


def is_low_value_product(handle: str) -> bool:
    h = (handle or "").lower()
    return any(p in h for p in LOW_VALUE_PRODUCT_HINTS)


# --- Périmètre produits (règle Alexandre) ---
# On GARDE un produit si : type lunetterie (Solaires* / Optiques* / Monture Optique)
# ET on l'EXCLUT s'il est à la fois inactif (brouillon/archivé) ET non publié en ligne
# ET créé avant 2021.
EYEWEAR_TYPE_PREFIXES = ("solaires", "optiques")   # couvre "Solaires - Atelier", "Optiques - Offre Santé"…
EYEWEAR_TYPE_EXACT = ("monture optique",)
SCOPE_CUTOFF_DATE = "2021-01-01"


def _is_eyewear_type(product_type: str) -> bool:
    t = (product_type or "").strip().lower()
    return t in EYEWEAR_TYPE_EXACT or t.startswith(EYEWEAR_TYPE_PREFIXES)


def product_in_scope(p: dict) -> bool:
    if not _is_eyewear_type(p.get("productType")):
        return False
    inactive = (p.get("status") or "").upper() in ("DRAFT", "ARCHIVED")
    unpublished = not p.get("publishedAt")
    created = (p.get("createdAt") or "")[:10]
    pre_2021 = bool(created) and created < SCOPE_CUTOFF_DATE
    if inactive and unpublished and pre_2021:
        return False
    return True
