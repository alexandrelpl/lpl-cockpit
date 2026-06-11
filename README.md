# LPL Cockpit — socle données

Socle de données pour le dashboard de pilotage marketing de Le Petit Lunetier :
CA Shopify (ré-attribué à la date de commande), dépenses Meta & Google Ads, COS blended,
trafic & conversion — le tout dans BigQuery, rafraîchi quotidiennement.

C'est la **phase 1** (couche données). Le front (appli web custom ou Looker Studio)
viendra se brancher sur la vue `cockpit_daily`.

## Pourquoi cette couche

- **CA « marketing-juste »** : les remboursements sont ré-attribués à la date de la
  commande d'origine (et non à la date de remboursement), via une lecture commande par
  commande de l'API Admin Shopify. C'est le portage fidèle de l'Apps Script « Volume V5.7 »
  (mêmes exclusions b2b/wholesale/alan/voided/non-web, mêmes catégories Comptoir/Optique/
  M&M/Others et segmentation New/Existing).
- **24 mois d'historique** : un backfill unique, puis un refresh glissant de 40 jours
  chaque nuit pour rattraper les remboursements tardifs.
- **Une source unique de vérité** réutilisable par n'importe quelle façade.

## Structure

```
lpl-cockpit/
├── bigquery/schema.sql          # tables + vue cockpit_daily
├── ingestion/
│   ├── shopify_orders.py        # CA net par date de commande (logique LPL)  ← cœur
│   ├── shopify_traffic.py       # sessions (hors bots) + taux de conversion
│   ├── meta_ads.py              # Meta Ads quotidien par campagne
│   ├── google_ads.py            # Google Ads quotidien (prêt pour accès Basic)
│   ├── bq_setup.py              # création dataset + tables
│   └── main.py                  # orchestration CLI + HTTP (Cloud Run)
├── Dockerfile                   # image Cloud Run
├── requirements.txt
├── .env.example
└── DEPLOY.md                    # guide de déploiement pas à pas
```

## Données exposées — vue `cockpit_daily` (1 ligne/jour)

`date, ca_shopify, orders, meta_spend, google_spend, ad_spend_total, meta_value,
google_value, cos_blended, roas_blended, sessions, visitors, conversion_rate`

→ `cos_blended = ad_spend_total / ca_shopify` (ton pilotage au COS).

## Mise en route

Voir **DEPLOY.md**. En résumé : secrets dans Secret Manager → déploiement Cloud Run →
`/setup` → `/backfill?months=24` → Scheduler quotidien.

## Sécurité

- Aucun secret en dur dans le code (contrairement à l'Apps Script). Tout via Secret Manager.
- ⚠️ Le token Shopify divulgué dans le chat doit être **régénéré** (voir DEPLOY.md §0).
- `.gitignore` exclut `.env`, clés SA et credentials.

## Statut

| Source | État |
|---|---|
| Shopify CA (logique LPL) | ✅ implémenté, logique testée (10/10) |
| Shopify trafic/CVR | ✅ implémenté (ShopifyQL Admin — à valider en réel) |
| Meta Ads | ✅ implémenté |
| Google Ads | ✅ implémenté, inactif jusqu'à l'accès Basic |
| Front (web / Looker) | ⏳ phase 2 |
```
