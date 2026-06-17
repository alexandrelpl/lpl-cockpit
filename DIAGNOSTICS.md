# Runbook de diagnostic — LPL Cockpit

But : quand Alexandre signale « les données semblent fausses / une source est en retard »,
suivre cette séquence **dans l'ordre**, sans tâtonner.

## Étape 0 — Lancer le diagnostic automatique

```bash
cd ~/Documents/GitHub/meta-ads-analyzer/lpl-cockpit && source .venv/bin/activate
export BQ_PROJECT=shopify-data-ltv BQ_DATASET=lpl_cockpit BQ_LOCATION=EU CLOUD_RUN_REGION=europe-west1
python -m ingestion.diagnose
```

Le rapport donne, par source : fraîcheur (retard en jours), trous, cohérence inter-sources,
les 8 derniers jours, l'état des jobs/planificateurs. **Lire les drapeaux 🔴/🟠 puis appliquer
le bloc correspondant ci-dessous.**

Repères de fraîcheur attendus (vs hier) : CA & Meta = 0 j ; Google & Sessions ≤ 1-2 j.

---

## Arbre de décision symptôme → cause → correctif

### A. Meta en retard / à zéro
Cause la plus fréquente : **jeton long-lived expiré (~60 j)**.
1. Vérifier : `gcloud run jobs executions list --job lpl-cockpit-job --region europe-west1 --limit 3`
   puis les logs : `gcloud logging read 'resource.labels.job_name="lpl-cockpit-job" AND textPayload:"meta"' --freshness=2d --limit=20 --format='value(textPayload)'`
   → un message d'erreur 190 / OAuth = jeton expiré.
2. Correctif : régénérer un token (Graph API Explorer, scope `ads_read`), puis :
   ```bash
   printf '%s' 'EAA_NOUVEAU' | gcloud secrets versions add META_ACCESS_TOKEN --data-file=-
   gcloud run jobs execute lpl-cockpit-job --region europe-west1
   ```
   (Le secret est versionné ; `:latest` est lu automatiquement.)
3. Robustesse durable : passer à un **token System User** (n'expire pas) — cf. IMPROVEMENTS.md.

### B. Sessions en retard
Source = **GA4** (autonome, OAuth utilisateur), fenêtre 3 j. L'archive ancienne (avril 2024 →)
vient du scraper Shopify et reste figée en base.
1. Relancer : `gcloud run jobs execute lpl-cockpit-sessions --region europe-west1` (= bouton appli) ou attendre le job de nuit.
2. Si échec : logs du job sessions (souvent jeton GA4/échec OAuth, ou GA4_PROPERTY_ID manquant).
3. ⚠️ **Continuité 14 mois** : le rafraîchissement ne touche QUE les 3 derniers jours, donc
   l'historique GA4 s'accumule à vie en base. **Ne JAMAIS** lancer un backfill GA4 sur une plage
   plus ancienne que la rétention GA4 (~14 mois) — ça écraserait l'historique conservé par du vide.

### C. Google en retard
Cause : le **script Google Ads** n'a pas écrit son Sheet de coûts (onglet « GoogleAds »).
1. Vérifier la dernière date du Sheet de coûts.
2. Forcer la relecture : `gcloud run jobs execute lpl-cockpit-job --region europe-west1`.
3. Cible finale : remplacer par l'API Google Ads dès l'accès Basic (google_ads.py prêt).

### D-bis. Un job échoue avec « Worker failed to boot » (gunicorn) ou `KeyError: 'BQ_PROJECT'`/`'SHOPIFY_SHOP_URL'`
Cause : un redéploiement `gcloud run jobs deploy <job> --source .` **sans** ré-inclure
`--command`/`--args` (et parfois sans les env) a fait retomber le job sur le CMD du Dockerfile
(gunicorn) et/ou perdu ses variables. Correctif sans rebuild :
```bash
gcloud run jobs update lpl-cockpit-sessions --region europe-west1 --command=python --args=job_sessions.py \
  --set-env-vars BQ_PROJECT=shopify-data-ltv,BQ_DATASET=lpl_cockpit,BQ_LOCATION=EU,SESSIONS_SHEET_ID=1uHl3DRVfhqAQnVT6lpm1tfbBG5BTfWd7RuasCy4SJAs
gcloud run jobs update lpl-cockpit-job --region europe-west1 --command=python --args=job.py
```
**Règle :** tout redéploiement de job avec `--source .` doit ré-inclure `--command=python --args=…`,
`--service-account $SA` ET les env, sinon ils retombent aux défauts (gunicorn, SA compute, env vide).
`Permission denied on secret … for …-compute@…` = il manque `--service-account $SA`.
Préférer `gcloud run jobs update` pour un simple changement de config (pas de rebuild).

### D. CA en retard / trous, ou job de nuit échoué
1. `gcloud run jobs executions list --job lpl-cockpit-job --region europe-west1 --limit 3`
   → statut de la dernière exécution.
2. Logs : `gcloud logging read 'resource.labels.job_name="lpl-cockpit-job"' --freshness=2d --limit=40 --format='value(timestamp,textPayload)'`
3. Causes typiques : throttling Shopify (le run dépasse le timeout), erreur réseau, secret manquant.
   Relancer : `gcloud run jobs execute lpl-cockpit-job --region europe-west1`.

### E. Trou d'historique (recouvrement)
Le diagnostic liste les jours manquants. Relancer le backfill de la source concernée :
- Shopify : `python -m ingestion.shopify_orders backfill 24` (local).
- Meta : `python -m ingestion.meta_ads backfill 760` (local, par tranches de 90 j).
- Sessions archive : re-déployer le job jetable `lpl-cockpit-sessions-archive` (cf. historique).

### F. Bouton « Rafraîchir » ou appli en 403
Permission manquante : le compte de service doit avoir `roles/run.invoker` sur le job visé.
```bash
gcloud run jobs add-iam-policy-binding lpl-cockpit-sessions --region europe-west1 \
  --member="serviceAccount:lpl-cockpit-ingest@shopify-data-ltv.iam.gserviceaccount.com" --role="roles/run.invoker"
```

### G. Chiffres « bizarres » mais aucune source en retard
- COS/Dépense sous-estimés sur l'historique ancien = Meta non backfillé sur cette période → `meta_ads backfill`.
- CVR vide avant ~avril 2024 = hors fenêtre de l'archive sessions (normal).
- Écart CA vs Shopify Analytics = rappeler la logique LPL (exclusions b2b/wholesale/alan/non-web,
  remboursements re-datés à la commande).

### H. Clients (Nouveaux/Fidèles, CAC, ROPO) en retard ou faux
Pipeline : `shopify_customers.refresh(45)` écrit `shopify_customer_orders` (web, via `order.email`),
puis `customers_metrics.refresh()` reconstruit **4 tables dérivées** (`CREATE OR REPLACE`, dans cet ordre) :
`retail_purchases` (DWH boutique externe par email) → `customers_period` → `acquisition_period` → `ropo_month`.
- **Encart « État des données » → Clients en rouge** = `customers_period` (scope web, grain day) en retard.
  Cause quasi toujours = le job de nuit a échoué AVANT l'étape clients (voir D), ou `shopify_customer_orders`
  pas à jour. Vérifier : `SELECT MAX(period_start) FROM customers_period WHERE scope='web' AND grain='day'`.
- **`customers_metrics` en erreur dans les logs du job** : le plus souvent une requête refusée par BigQuery.
  Piège connu : **les sous-requêtes corrélées avec condition de plage (`date < pstart`) sont interdites** →
  toujours passer par un `LEFT JOIN` + `MAX(IF(...))` (déjà fait dans `_period_sql`/`_ropo_sql`). Ne pas réintroduire d'`EXISTS`.
- **Taux de Fidèles anormalement bas (~13 % au lieu de ~33-46 %)** = données boutique manquantes.
  Vérifier l'accès SA au DWH retail (`stable-splicer-294813...transaction_details_visits`, rôle dataViewer)
  et que `retail_purchases` n'est pas vide. Sans le retail, le scope global ≈ web.
- **CAC qui explose un jour précis (ex. dimanches)** = normal si on regardait le global (boutiques fermées →
  0 nouveau). Le CAC est volontairement **web-only** (dénominateur = nouveaux clients web). Ne pas « corriger ».
- **`retail_purchases` double-comptage** : la source bascule `opticbox` (<2023-12-20) / `invoice_optimum` (≥),
  réglé par `RETAIL_CUTOVER`. Ne pas élargir la fenêtre des deux sources en même temps.

### I. Appli lente à charger
La donnée ne change qu'1×/nuit → toute lenteur est de la **latence**, pas du volume (tables ~700 lignes).
Ordre des causes (de la plus probable à la moins) :
1. **Cold start Cloud Run** (`min-instances=0` → conteneur éteint quand inactif). 1er chargement de la
   journée = plusieurs secondes. Correctif n°1 : `--min-instances=1` (+`--cpu-boost`) sur `lpl-cockpit-web`.
2. **Cache mémoire vidé** : `_BQ_CACHE`/`_ALERTS_CACHE` sont par-process et meurent à chaque cold start.
   Mitigé par 1 worker (cache partagé) + min-instances=1 (instance qui reste chaude).
3. **Onglet Meta lent au 1er clic** : `/api/meta/alerts` appelle l'API Graph Meta **en direct** (pas BQ),
   donc tributaire de la latence Meta. Caché 15 min/fenêtre. Optimisation future : recalculer les alertes
   depuis `meta_daily` (déjà en base) au lieu de l'API live.
4. **`cockpit_daily` est une VUE** recomposée à chaque requête (overview/periods/acquisition). Coût faible
   (petites tables partitionnées) ; matérialisable en table le soir si besoin.

---

## Inventaire de référence (pour ne pas chercher)

| Élément | Valeur |
|---|---|
| Projet GCP | `shopify-data-ltv` |
| Dataset BQ | `lpl_cockpit` (EU) |
| Compte de service | `lpl-cockpit-ingest@shopify-data-ltv.iam.gserviceaccount.com` |
| Job nuit (tout) | `lpl-cockpit-job` — Scheduler `lpl-cockpit-nightly` (6h Paris) |
| Job sessions | `lpl-cockpit-sessions` — Scheduler `lpl-cockpit-sessions-hourly` |
| Appli web | service Cloud Run `lpl-cockpit-web` (gunicorn **1 worker** / 8 threads → cache mémoire partagé) |
| Tables clients | `shopify_customer_orders` (web+email) → dérivées : `retail_purchases`, `customers_period`, `acquisition_period`, `ropo_month` |
| DWH boutique (externe) | `stable-splicer-294813.dwh_datasource_sales.transaction_details_visits` (SA = dataViewer) |
| Secrets | `META_ACCESS_TOKEN`, `SHOPIFY_ACCESS_TOKEN` (épinglé `:1`), `COCKPIT_SECRET_KEY`, `GOOGLE_OAUTH_CLIENT_SECRET` |
| Sheet objectifs | `1Ct23…` onglet `Budget 2026` (CA ligne 8, dépense ligne 15) |
| Sheet coûts Google | `1ZrKs7…` onglet `GoogleAds` |
| Sheet sessions récent | `1uHl3…` onglet `Sessions` (dynamique, scraper Mac) |
| Sheet sessions archive | `1ZrKs7…` onglet `Sessions Shopify Archive 720j …` (figé) |
