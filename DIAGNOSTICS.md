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
Cause : le **scraper Mac n'a pas alimenté le Sheet** (Mac éteint / vacances) — le job ne peut
pas inventer la donnée.
1. Vérifier la dernière date du Sheet « Sessions » (onglet dynamique).
2. Si le Sheet est à jour mais pas BigQuery : relancer la lecture →
   `gcloud run jobs execute lpl-cockpit-sessions --region europe-west1` (ou bouton dans l'appli).
3. Si le Sheet n'est pas à jour : relancer le scraper côté Mac. Le bandeau de l'appli prévient
   déjà l'utilisateur que les sessions sont incomplètes.

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
**Règle :** tout redéploiement de job avec `--source .` doit ré-inclure `--command=python --args=…`
(et garder les env). Préférer `gcloud run jobs update` pour un simple changement de config (pas de rebuild).

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

---

## Inventaire de référence (pour ne pas chercher)

| Élément | Valeur |
|---|---|
| Projet GCP | `shopify-data-ltv` |
| Dataset BQ | `lpl_cockpit` (EU) |
| Compte de service | `lpl-cockpit-ingest@shopify-data-ltv.iam.gserviceaccount.com` |
| Job nuit (tout) | `lpl-cockpit-job` — Scheduler `lpl-cockpit-nightly` (6h Paris) |
| Job sessions | `lpl-cockpit-sessions` — Scheduler `lpl-cockpit-sessions-hourly` |
| Appli web | service Cloud Run `lpl-cockpit-web` |
| Secrets | `META_ACCESS_TOKEN`, `SHOPIFY_ACCESS_TOKEN`, `COCKPIT_SECRET_KEY`, `GOOGLE_OAUTH_CLIENT_SECRET` |
| Sheet objectifs | `1Ct23…` onglet `Budget 2026` (CA ligne 8, dépense ligne 15) |
| Sheet coûts Google | `1ZrKs7…` onglet `GoogleAds` |
| Sheet sessions récent | `1uHl3…` onglet `Sessions` (dynamique, scraper Mac) |
| Sheet sessions archive | `1ZrKs7…` onglet `Sessions Shopify Archive 720j …` (figé) |
