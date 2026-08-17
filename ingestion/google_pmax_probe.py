"""
PROBE PMax — valide quelles requêtes GAQL passent réellement sur notre compte/version d'API.

But : ne PAS construire l'ingestion PMax sur des noms de champs devinés. Les ressources PMax
(asset_group_asset, asset_group_product_group_view, asset_group_signal,
campaign_search_term_insight...) ont beaucoup évolué selon les versions de l'API.

Ce script n'écrit RIEN dans BigQuery. Il exécute chaque requête avec une petite fenêtre,
affiche OK/ERREUR + un échantillon de lignes, puis un récapitulatif.

Usage (Cloud Run Job, en override d'args) :
    python -m ingestion.google_pmax_probe
"""

from __future__ import annotations
import traceback
from datetime import date, timedelta

from ingestion.google_ads import _client, CUSTOMER_ID

SINCE = (date.today() - timedelta(days=30)).isoformat()
UNTIL = (date.today() - timedelta(days=1)).isoformat()

# Chaque entrée : (clé, description, GAQL, champs à afficher pour l'échantillon)
PROBES: list[tuple[str, str, str, list[str]]] = [
    (
        "asset_group_perf",
        "Performance par asset group (déjà utilisé — sert de témoin)",
        f"""SELECT campaign.name, asset_group.id, asset_group.name, asset_group.status,
                   metrics.cost_micros, metrics.conversions, metrics.conversions_value
            FROM asset_group
            WHERE segments.date BETWEEN '{SINCE}' AND '{UNTIL}'
            LIMIT 5""",
        ["campaign.name", "asset_group.name", "asset_group.status", "metrics.cost_micros"],
    ),
    (
        "asset_metrics",
        "Métriques REELLES par asset (v24 : performance_label supprimé, remplacé par metrics)",
        f"""SELECT asset_group.name, asset_group_asset.asset,
                   asset_group_asset.field_type, asset_group_asset.status,
                   metrics.cost_micros, metrics.impressions, metrics.clicks,
                   metrics.conversions, metrics.conversions_value
            FROM asset_group_asset
            WHERE segments.date BETWEEN '{SINCE}' AND '{UNTIL}'
              AND asset_group_asset.status = 'ENABLED'
            LIMIT 8""",
        ["asset_group.name", "asset_group_asset.field_type",
         "metrics.cost_micros", "metrics.conversions"],
    ),
    (
        "asset_primary_status",
        "primary_status / raisons de non-diffusion d'un asset",
        """SELECT asset_group.name, asset_group_asset.asset,
                  asset_group_asset.field_type,
                  asset_group_asset.primary_status,
                  asset_group_asset.primary_status_reasons
           FROM asset_group_asset
           WHERE asset_group_asset.status != 'REMOVED'
           LIMIT 8""",
        ["asset_group.name", "asset_group_asset.field_type",
         "asset_group_asset.primary_status"],
    ),
    (
        "asset_by_network",
        "Breakdown par canal (Search/Display/YouTube...) au niveau asset",
        f"""SELECT asset_group.name, asset_group_asset.field_type,
                   segments.ad_network_type,
                   metrics.cost_micros, metrics.conversions, metrics.conversions_value
            FROM asset_group_asset
            WHERE segments.date BETWEEN '{SINCE}' AND '{UNTIL}'
              AND asset_group_asset.status = 'ENABLED'
            LIMIT 8""",
        ["asset_group.name", "segments.ad_network_type", "metrics.cost_micros"],
    ),
    (
        "ad_strength",
        "Ad strength par asset group (GOOD/EXCELLENT/POOR)",
        """SELECT campaign.name, asset_group.name, asset_group.ad_strength,
                  asset_group.status
           FROM asset_group
           WHERE asset_group.status = 'ENABLED'
           LIMIT 10""",
        ["campaign.name", "asset_group.name", "asset_group.ad_strength"],
    ),
    (
        "asset_text",
        "Contenu texte des assets (pour nommer les créas à remplacer)",
        """SELECT asset.id, asset.type, asset.text_asset.text
           FROM asset
           WHERE asset.type = 'TEXT'
           LIMIT 5""",
        ["asset.id", "asset.text_asset.text"],
    ),
    (
        "listing_group",
        "Listing group / produits (concentration du spend, produits zombies)",
        f"""SELECT campaign.name, asset_group.name,
                   asset_group_listing_group_filter.id,
                   asset_group_listing_group_filter.type,
                   metrics.cost_micros, metrics.conversions, metrics.conversions_value,
                   metrics.impressions
            FROM asset_group_product_group_view
            WHERE segments.date BETWEEN '{SINCE}' AND '{UNTIL}'
            LIMIT 5""",
        ["asset_group.name", "asset_group_listing_group_filter.type", "metrics.cost_micros"],
    ),
    (
        "shopping_product",
        "Perf par produit réel (item_id) — alternative au listing group",
        f"""SELECT campaign.name, segments.product_item_id, segments.product_title,
                   metrics.cost_micros, metrics.conversions, metrics.conversions_value,
                   metrics.impressions
            FROM shopping_performance_view
            WHERE segments.date BETWEEN '{SINCE}' AND '{UNTIL}'
            LIMIT 5""",
        ["segments.product_item_id", "segments.product_title", "metrics.cost_micros"],
    ),
    (
        "signals",
        "Search themes / signaux d'audience par asset group",
        """SELECT campaign.name, asset_group.name,
                  asset_group_signal.audience.audience,
                  asset_group_signal.search_theme.text
           FROM asset_group_signal
           LIMIT 10""",
        ["asset_group.name", "asset_group_signal.search_theme.text"],
    ),
    (
        "conv_lag",
        "Conversion lag (maturité des données avant de juger un tROAS)",
        f"""SELECT campaign.name, segments.conversion_lag_bucket,
                   metrics.conversions, metrics.conversions_value
            FROM campaign
            WHERE segments.date BETWEEN '{SINCE}' AND '{UNTIL}'
            LIMIT 5""",
        ["campaign.name", "segments.conversion_lag_bucket", "metrics.conversions"],
    ),
    (
        "campaign_bidding",
        "Stratégie d'enchère + tROAS cible configuré + budget",
        """SELECT campaign.name, campaign.advertising_channel_type,
                  campaign.bidding_strategy_type,
                  campaign.maximize_conversion_value.target_roas,
                  campaign_budget.amount_micros
           FROM campaign
           WHERE campaign.status = 'ENABLED'
           LIMIT 10""",
        ["campaign.name", "campaign.bidding_strategy_type",
         "campaign.maximize_conversion_value.target_roas", "campaign_budget.amount_micros"],
    ),
]


def _get(obj, path: str):
    """Résout 'a.b.c' sur un objet proto, en tolérant les champs absents."""
    cur = obj
    for part in path.split("."):
        cur = getattr(cur, part, None)
        if cur is None:
            return None
    return cur


def run() -> dict:
    client = _client()
    service = client.get_service("GoogleAdsService")
    cid = CUSTOMER_ID.replace("-", "")
    results: dict[str, str] = {}

    for key, label, gaql, fields in PROBES:
        print("\n" + "=" * 78)
        print(f"### {key} — {label}")
        try:
            resp = service.search_stream(customer_id=cid, query=gaql)
            n = 0
            for batch in resp:
                for r in batch.results:
                    n += 1
                    vals = []
                    for f in fields:
                        v = _get(r, f)
                        if hasattr(v, "name"):      # enum proto
                            v = v.name
                        vals.append(f"{f.split('.')[-1]}={v}")
                    print("   " + " | ".join(vals))
            results[key] = f"OK ({n} ligne(s))"
            print(f"--> OK — {n} ligne(s)")
        except Exception as e:  # noqa: BLE001
            msg = str(e).split("\n")[0][:220]
            results[key] = f"ERREUR: {msg}"
            print(f"--> ERREUR: {msg}")
            traceback.print_exc()

    # --- search_insight : l'API impose de filtrer UNE seule campagne à la fois ---
    print("\n" + "=" * 78)
    print("### search_insight — Search term insights PMax (1 campagne à la fois)")
    try:
        camps = []
        for batch in service.search_stream(customer_id=cid, query="""
                SELECT campaign.id, campaign.name FROM campaign
                WHERE campaign.advertising_channel_type = 'PERFORMANCE_MAX'
                  AND campaign.status = 'ENABLED'"""):
            for r in batch.results:
                camps.append((str(r.campaign.id), r.campaign.name))
        print(f"   {len(camps)} campagne(s) PMax activée(s) : {[c[1] for c in camps]}")
        tot = 0
        for cmp_id, cmp_name in camps:
            q = f"""SELECT campaign_search_term_insight.category_label,
                           campaign_search_term_insight.id,
                           metrics.impressions, metrics.clicks,
                           metrics.conversions, metrics.conversions_value
                    FROM campaign_search_term_insight
                    WHERE campaign_search_term_insight.campaign_id = {cmp_id}
                      AND segments.date BETWEEN '{SINCE}' AND '{UNTIL}'
                    ORDER BY metrics.impressions DESC
                    LIMIT 8"""
            n = 0
            for batch in service.search_stream(customer_id=cid, query=q):
                for r in batch.results:
                    n += 1
                    tot += 1
                    ins = r.campaign_search_term_insight
                    print(f"   [{cmp_name}] cat='{ins.category_label}' "
                          f"impr={r.metrics.impressions} clics={r.metrics.clicks} "
                          f"conv={r.metrics.conversions}")
            print(f"   --> {cmp_name} : {n} catégorie(s)")
        results["search_insight"] = f"OK ({tot} ligne(s) sur {len(camps)} campagne(s))"
    except Exception as e:  # noqa: BLE001
        msg = str(e).split("\n")[0][:220]
        results["search_insight"] = f"ERREUR: {msg}"
        print(f"--> ERREUR: {msg}")

    print("\n" + "#" * 78)
    print("RECAPITULATIF PROBE PMAX")
    print("#" * 78)
    for k, v in results.items():
        print(f"  {k:24s} : {v}")
    return results


if __name__ == "__main__":
    run()
