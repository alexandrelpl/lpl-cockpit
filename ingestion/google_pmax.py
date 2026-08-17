"""
Ingestion PMax (Google Ads) -> BigQuery. Sources validées par `google_pmax_probe` (API v24).

4 sources, chacune isolée (une panne n'arrête pas les autres) :
  1. search_cat  -> google_pmax_search_cat   : catégories de requêtes par campagne PMax
                    (l'API IMPOSE de filtrer une seule campagne à la fois -> on boucle)
  2. products    -> google_pmax_products     : perf par produit réel (shopping_performance_view)
  3. assets      -> google_pmax_assets       : perf + primary_status par asset
  4. meta        -> google_pmax_asset_groups : ad_strength / statut (snapshot)
                    google_pmax_campaigns    : stratégie d'enchère, tROAS cible, budget (snapshot)

⚠️ PIEGE — les métriques par ASSET ne sont PAS additives : une même impression implique
plusieurs assets (titre + image + description), chacun se voit attribuer le coût complet.
La somme des coûts d'assets DEPASSE le coût réel de la campagne. Ne jamais sommer les assets
pour reconstituer un total : les utiliser uniquement en COMPARATIF entre assets de même
field_type au sein d'un même asset group.

`performance_label` n'existe plus en v24 : remplacé par `primary_status` + vraies métriques.
"""

from __future__ import annotations
import os
from datetime import date, datetime, timedelta, timezone

from google.cloud import bigquery

from ingestion import bq_io
from ingestion.google_ads import _client, CUSTOMER_ID

BQ_PROJECT = os.environ["BQ_PROJECT"]
BQ_DATASET = os.environ.get("BQ_DATASET", "lpl_cockpit")


def _svc():
    return _client().get_service("GoogleAdsService")


def _cid() -> str:
    return CUSTOMER_ID.replace("-", "")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _table(name: str) -> str:
    return f"{BQ_PROJECT}.{BQ_DATASET}.{name}"


def _pmax_campaigns(service) -> list[tuple[str, str]]:
    """Campagnes PMax activées : [(id, name)]."""
    out = []
    for batch in service.search_stream(customer_id=_cid(), query="""
            SELECT campaign.id, campaign.name FROM campaign
            WHERE campaign.advertising_channel_type = 'PERFORMANCE_MAX'
              AND campaign.status = 'ENABLED'"""):
        for r in batch.results:
            out.append((str(r.campaign.id), r.campaign.name))
    return out


# ---------------------------------------------------------------- 1. catégories de requêtes
def ingest_search_cat(since: str, until: str) -> int:
    """
    Catégories de requêtes par campagne PMax. Sert à mesurer la cannibalisation marque.

    ⚠️ DEUX contraintes d'API (constatées empiriquement via google_pmax_probe) :
      1. `campaign_search_term_insight` IMPOSE de filtrer sur UNE campagne à la fois
         (sinon REQUIRES_FILTER_BY_SINGLE_RESOURCE) -> on boucle.
      2. `segments.date` est FILTRABLE mais PAS SÉLECTIONNABLE sur cette ressource.
         Donc PAS de granularité journalière possible : le résultat est un AGRÉGAT
         sur la période. -> table en SNAPSHOT (period_start/period_end), remplacée
         intégralement à chaque run.
    Ne pas remettre `segments.date` dans le SELECT : la requête échoue silencieusement
    (l'erreur est avalée par refresh(), on se retrouve avec 0 ligne).
    """
    service = _svc()
    rows = []
    now = _now()
    for cmp_id, cmp_name in _pmax_campaigns(service):
        q = f"""SELECT campaign_search_term_insight.category_label,
                       campaign_search_term_insight.id,
                       metrics.impressions, metrics.clicks,
                       metrics.conversions, metrics.conversions_value
                FROM campaign_search_term_insight
                WHERE campaign_search_term_insight.campaign_id = {cmp_id}
                  AND segments.date BETWEEN '{since}' AND '{until}'"""
        for batch in service.search_stream(customer_id=_cid(), query=q):
            for r in batch.results:
                ins = r.campaign_search_term_insight
                rows.append({
                    "period_start": since,
                    "period_end": until,
                    "campaign_id": cmp_id,
                    "campaign_name": cmp_name,
                    # '' = requêtes non catégorisées (anonymisées par Google, faible volume)
                    "category_label": ins.category_label or "(non catégorisé)",
                    "impressions": r.metrics.impressions,
                    "clicks": r.metrics.clicks,
                    "conversions": r.metrics.conversions,
                    "conversion_value": r.metrics.conversions_value,
                    "updated_at": now,
                })
    bq = bigquery.Client(project=BQ_PROJECT)
    n = bq_io.load_replace_all(bq, _table("google_pmax_search_cat"), rows)
    print(f"[pmax-search] {n} catégories écrites (agrégat {since} -> {until})")
    return n


# ---------------------------------------------------------------- 2. produits
def ingest_products(since: str, until: str) -> int:
    """Perf par produit réel — concentration du spend / produits zombies."""
    service = _svc()
    q = f"""SELECT campaign.id, campaign.name,
                   segments.date, segments.product_item_id, segments.product_title,
                   metrics.cost_micros, metrics.impressions, metrics.clicks,
                   metrics.conversions, metrics.conversions_value
            FROM shopping_performance_view
            WHERE segments.date BETWEEN '{since}' AND '{until}'"""
    rows = []
    now = _now()
    for batch in service.search_stream(customer_id=_cid(), query=q):
        for r in batch.results:
            item = r.segments.product_item_id
            if not item:
                continue
            rows.append({
                "date": str(r.segments.date),
                "campaign_id": str(r.campaign.id),
                "campaign_name": r.campaign.name,
                "product_item_id": item,
                "product_title": r.segments.product_title,
                "cost": r.metrics.cost_micros / 1_000_000,
                "impressions": r.metrics.impressions,
                "clicks": r.metrics.clicks,
                "conversions": r.metrics.conversions,
                "conversion_value": r.metrics.conversions_value,
                "updated_at": now,
            })
    bq = bigquery.Client(project=BQ_PROJECT)
    n = bq_io.load_replace_window(bq, _table("google_pmax_products"), rows, since, until)
    print(f"[pmax-products] {n} lignes produit x jour écrites")
    return n


# ---------------------------------------------------------------- 3. assets
def ingest_assets(since: str, until: str) -> int:
    """
    Perf + statut de diffusion par asset.
    ⚠️ NON ADDITIF (cf. en-tête) : usage comparatif uniquement, entre assets de même
    field_type dans un même asset group.
    """
    service = _svc()
    q = f"""SELECT campaign.id, campaign.name,
                   asset_group.id, asset_group.name,
                   asset_group_asset.asset, asset_group_asset.field_type,
                   asset_group_asset.status, asset_group_asset.primary_status,
                   segments.date,
                   metrics.cost_micros, metrics.impressions, metrics.clicks,
                   metrics.conversions, metrics.conversions_value
            FROM asset_group_asset
            WHERE segments.date BETWEEN '{since}' AND '{until}'
              AND asset_group_asset.status != 'REMOVED'"""
    rows = []
    now = _now()
    for batch in service.search_stream(customer_id=_cid(), query=q):
        for r in batch.results:
            a = r.asset_group_asset
            rows.append({
                "date": str(r.segments.date),
                "campaign_id": str(r.campaign.id),
                "campaign_name": r.campaign.name,
                "asset_group_id": str(r.asset_group.id),
                "asset_group_name": r.asset_group.name,
                "asset_resource": a.asset,
                "field_type": a.field_type.name if hasattr(a.field_type, "name") else str(a.field_type),
                "status": a.status.name if hasattr(a.status, "name") else str(a.status),
                "primary_status": (a.primary_status.name
                                   if hasattr(a.primary_status, "name") else str(a.primary_status)),
                "cost": r.metrics.cost_micros / 1_000_000,
                "impressions": r.metrics.impressions,
                "clicks": r.metrics.clicks,
                "conversions": r.metrics.conversions,
                "conversion_value": r.metrics.conversions_value,
                "updated_at": now,
            })
    bq = bigquery.Client(project=BQ_PROJECT)
    n = bq_io.load_replace_window(bq, _table("google_pmax_assets"), rows, since, until)
    print(f"[pmax-assets] {n} lignes asset x jour écrites")
    return n


# ---------------------------------------------------------------- 4. snapshots config
def ingest_meta() -> dict:
    """Snapshots (remplacés à chaque run) : ad_strength par asset group, config par campagne."""
    service = _svc()
    now = _now()
    bq = bigquery.Client(project=BQ_PROJECT)

    ag_rows = []
    for batch in service.search_stream(customer_id=_cid(), query="""
            SELECT campaign.id, campaign.name, campaign.status,
                   asset_group.id, asset_group.name, asset_group.status,
                   asset_group.ad_strength
            FROM asset_group"""):
        for r in batch.results:
            ag_rows.append({
                "campaign_id": str(r.campaign.id),
                "campaign_name": r.campaign.name,
                "campaign_status": r.campaign.status.name,
                "asset_group_id": str(r.asset_group.id),
                "asset_group_name": r.asset_group.name,
                "asset_group_status": r.asset_group.status.name,
                "ad_strength": r.asset_group.ad_strength.name,
                "updated_at": now,
            })
    n1 = bq_io.load_replace_all(bq, _table("google_pmax_asset_groups"), ag_rows)

    cmp_rows = []
    for batch in service.search_stream(customer_id=_cid(), query="""
            SELECT campaign.id, campaign.name, campaign.status,
                   campaign.advertising_channel_type,
                   campaign.bidding_strategy_type,
                   campaign.maximize_conversion_value.target_roas,
                   campaign_budget.amount_micros
            FROM campaign
            WHERE campaign.status = 'ENABLED'"""):
        for r in batch.results:
            cmp_rows.append({
                "campaign_id": str(r.campaign.id),
                "campaign_name": r.campaign.name,
                "channel_type": r.campaign.advertising_channel_type.name,
                "bidding_strategy": r.campaign.bidding_strategy_type.name,
                "target_roas": r.campaign.maximize_conversion_value.target_roas or None,
                "daily_budget": (r.campaign_budget.amount_micros / 1_000_000
                                 if r.campaign_budget.amount_micros else None),
                "updated_at": now,
            })
    n2 = bq_io.load_replace_all(bq, _table("google_pmax_campaigns"), cmp_rows)
    print(f"[pmax-meta] {n1} asset groups, {n2} campagnes")
    return {"asset_groups": n1, "campaigns": n2}


# ---------------------------------------------------------------- orchestration
def refresh(days: int = 40) -> dict:
    today = date.today()
    since = (today - timedelta(days=days)).isoformat()
    until = today.isoformat()
    out: dict[str, object] = {}
    for name, fn in [
        ("search_cat", lambda: ingest_search_cat(since, until)),
        ("products",   lambda: ingest_products(since, until)),
        ("assets",     lambda: ingest_assets(since, until)),
        ("meta",       lambda: ingest_meta()),
    ]:
        try:
            out[name] = fn()
        except Exception as e:  # noqa: BLE001
            out[name] = f"ERROR: {e}"
            import traceback
            traceback.print_exc()
    return out


if __name__ == "__main__":
    import sys
    print(refresh(int(sys.argv[1]) if len(sys.argv) > 1 else 40))
