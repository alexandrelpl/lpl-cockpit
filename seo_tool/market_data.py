"""
Données de priorisation des collections :
- COLLECTION_ORGANIC : trafic organique mensuel par page collection (snapshot Semrush, base FR).
  Sert de signal « trafic notable en 2026 ».
- MARKET_CATEGORIES : whitelist des catégories CLÉS du marché de la lunette (evergreen),
  avec le mot-clé cible et, quand connu, le volume de recherche Semrush.

À rafraîchir périodiquement (idéalement automatiquement via l'API Semrush — voir spec_outil_seo.md).
Snapshot pris en juin 2026.
"""

# handle -> trafic organique mensuel estimé (Semrush domain_organic_unique, db fr)
COLLECTION_ORGANIC = {
    "lumiere-bleue": 3456,
    "lunettes-de-soleil-homme": 2790,
    "lunettes-de-soleil-femme": 1843,
    "lunettes-de-soleil": 1347,
    "descente-givree": 292,          # capsule/saisonnier -> hors marché evergreen
    "accessoires": 15,
    "lunettes-papillon": 8,
    "lunettes-de-vue-femme": 8,
    "last-chance": 2,
    "verre-a-la-vue": 3,
}

# handle -> {label lisible, mot-clé cible, volume mensuel Semrush si connu}
MARKET_CATEGORIES = {
    "lunettes-de-soleil-homme": {"label": "Solaires homme", "kw": "lunettes de soleil homme", "vol": 27100},
    "lunettes-de-soleil-femme": {"label": "Solaires femme", "kw": "lunettes de soleil femme", "vol": 18100},
    "lunettes-de-soleil":       {"label": "Solaires (parent)", "kw": "lunettes de soleil", "vol": 27100},
    "optiques":                 {"label": "Optique / vue (parent)", "kw": "lunettes de vue", "vol": None},
    "optique":                  {"label": "Optique / vue (doublon)", "kw": "lunettes de vue", "vol": None},
    "lunettes-de-vue-femme":    {"label": "Vue femme", "kw": "lunettes de vue femme", "vol": None},
    "lunettes-de-vue-homme":    {"label": "Vue homme", "kw": "lunettes de vue homme", "vol": None},
    "lumiere-bleue":            {"label": "Anti-lumière bleue", "kw": "lunettes anti lumière bleue", "vol": 1900},
    "lumiere-bleue-copy":       {"label": "Lumière bleue femme", "kw": "lunettes lumière bleue femme", "vol": None},
    "lumiere-bleue-femme-copy": {"label": "Lumière bleue homme", "kw": "lunettes lumière bleue homme", "vol": None},
    "lunettes-pantos":          {"label": "Forme pantos", "kw": "lunettes pantos", "vol": None},
    "lunettes-hexagonale":      {"label": "Forme hexagonale", "kw": "lunettes hexagonales", "vol": None},
    "lunettes-papillon":        {"label": "Forme papillon", "kw": "lunettes papillon", "vol": None},
    "lunettes-oversize":        {"label": "Forme oversize", "kw": "lunettes oversize", "vol": None},
    "lunettes-couleur-ecaille": {"label": "Écaille", "kw": "lunettes écaille", "vol": None},
    "visages-fins":             {"label": "Visage fin", "kw": "lunettes visage fin", "vol": None},
    "visages-larges":           {"label": "Visage large/rond", "kw": "lunettes visage large", "vol": None},
    "lunettes-taille-large":    {"label": "Grande taille", "kw": "lunettes grande taille", "vol": None},
}


def collection_traffic(handle: str) -> int:
    return COLLECTION_ORGANIC.get(handle, 0)


def market_info(handle: str) -> dict | None:
    return MARKET_CATEGORIES.get(handle)


def collection_tier(handle: str) -> int:
    """3 = marché + trafic prouvé ; 2 = marché (opportunité à capter) ; 1 = autre."""
    is_market = handle in MARKET_CATEGORIES
    traffic = collection_traffic(handle)
    if is_market and traffic > 0:
        return 3
    if is_market:
        return 2
    return 1


def collection_priority(handle: str) -> int:
    """Score de tri : marché+trafic en haut, puis marché (opportunité), puis le reste."""
    tier = collection_tier(handle)
    traffic = collection_traffic(handle)
    base = {3: 1_000_000, 2: 100_000, 1: 0}[tier]
    return base + traffic
