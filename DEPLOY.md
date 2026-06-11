# Déploiement — LPL Cockpit (socle données BigQuery)

Guide pas à pas. Tu exécutes les commandes ; je suis dispo pour débloquer chaque étape.
Objectif de cette phase : **alimenter BigQuery** (CA Shopify ré-daté sur 24 mois +
Meta, et Google dès l'accès Basic), rafraîchi tous les jours automatiquement.

Architecture de cette phase :

```
Cloud Scheduler (tous les jours 6h)
        │  HTTP
        ▼
Cloud Run  (service "lpl-cockpit-ingest")
        │  rejoue ta logique CA Shopify + tire Meta/Google
        ▼
BigQuery   dataset "lpl_cockpit"  ──►  vue cockpit_daily  (socle du futur dashboard)
        ▲
Secret Manager  (tokens Shopify / Meta / Google)
```

---

## 0. Prérequis (une fois)

- Un projet Google Cloud (le même que pour l'API Google Ads, c'est plus simple).
- Le SDK `gcloud` installé et connecté : `gcloud auth login` puis `gcloud config set project TON_PROJET`.
- **⚠️ Régénérer le token Shopify divulgué** (celui collé dans le chat). Shopify → Paramètres
  → Applications et canaux → Développer des applications → ton app → *API credentials* →
  *Revoke* puis nouveau token. Scopes nécessaires : `read_orders`, `read_analytics`.

Définis une variable locale pour la suite :

```bash
export PROJECT=$(gcloud config get-value project)
export REGION=europe-west1
```

## 1. Activer les API

```bash
gcloud services enable run.googleapis.com cloudscheduler.googleapis.com \
  bigquery.googleapis.com secretmanager.googleapis.com artifactregistry.googleapis.com \
  cloudbuild.googleapis.com
```

## 2. Stocker les secrets dans Secret Manager

```bash
# Shopify (NOUVEAU token régénéré)
printf 'shpat_LE_NOUVEAU_TOKEN' | gcloud secrets create SHOPIFY_ACCESS_TOKEN --data-file=-
# Meta (token long-lived)
printf 'EAAxxx' | gcloud secrets create META_ACCESS_TOKEN --data-file=-
```

(On ajoutera les secrets Google Ads quand l'accès Basic sera accordé.)

## 3. Compte de service du job

```bash
gcloud iam service-accounts create lpl-cockpit-ingest --display-name="LPL Cockpit ingest"
export SA=lpl-cockpit-ingest@$PROJECT.iam.gserviceaccount.com

# Droits BigQuery (écrire les données + lancer des requêtes)
gcloud projects add-iam-policy-binding $PROJECT --member="serviceAccount:$SA" --role="roles/bigquery.dataEditor"
gcloud projects add-iam-policy-binding $PROJECT --member="serviceAccount:$SA" --role="roles/bigquery.jobUser"
# Lecture des secrets
gcloud secrets add-iam-policy-binding SHOPIFY_ACCESS_TOKEN --member="serviceAccount:$SA" --role="roles/secretmanager.secretAccessor"
gcloud secrets add-iam-policy-binding META_ACCESS_TOKEN     --member="serviceAccount:$SA" --role="roles/secretmanager.secretAccessor"
```

## 4. Déployer le service sur Cloud Run

Depuis le dossier `lpl-cockpit/` (contenant le Dockerfile) :

```bash
gcloud run deploy lpl-cockpit-ingest \
  --source . \
  --region $REGION \
  --service-account $SA \
  --no-allow-unauthenticated \
  --memory 512Mi --cpu 1 --timeout 900 \
  --set-env-vars BQ_PROJECT=$PROJECT,BQ_DATASET=lpl_cockpit,BQ_LOCATION=EU,SHOPIFY_SHOP_URL=test-store20.myshopify.com,SHOPIFY_API_VERSION=2024-01,TIMEZONE=Europe/Paris,META_ACCOUNT_ID=305450184,META_API_VERSION=v21.0,SHOPIFY_REFRESH_DAYS=40,ADS_REFRESH_DAYS=14 \
  --set-secrets SHOPIFY_ACCESS_TOKEN=SHOPIFY_ACCESS_TOKEN:latest,META_ACCESS_TOKEN=META_ACCESS_TOKEN:latest

export URL=$(gcloud run services describe lpl-cockpit-ingest --region $REGION --format='value(status.url)')
```

## 5. Bootstrap BigQuery + backfill 24 mois (une fois)

```bash
# jeton d'identité pour appeler le service privé
TOK() { gcloud auth print-identity-token; }

# crée dataset + tables + vue
curl -s -X POST -H "Authorization: Bearer $(TOK)" "$URL/setup"

# historique Shopify 24 mois (peut prendre plusieurs minutes)
curl -s -X POST -H "Authorization: Bearer $(TOK)" "$URL/backfill?months=24"

# première charge Meta (14 j ; relance avec un backfill plus large si besoin)
curl -s -X POST -H "Authorization: Bearer $(TOK)" "$URL/refresh"
```

Vérifie dans BigQuery :

```sql
SELECT * FROM `TON_PROJET.lpl_cockpit.cockpit_daily` ORDER BY date DESC LIMIT 14;
```

## 6. Planifier le refresh quotidien

```bash
gcloud scheduler jobs create http lpl-cockpit-daily \
  --location $REGION \
  --schedule "0 6 * * *" --time-zone "Europe/Paris" \
  --uri "$URL/refresh" --http-method POST \
  --oidc-service-account-email $SA
```

---

## Quand l'accès Basic Google Ads est accordé

1. Génère un refresh token OAuth pour l'API Google Ads (compte qui a accès au MCC).
2. Ajoute les secrets :
   ```bash
   printf 'DEV_TOKEN'      | gcloud secrets create GOOGLE_ADS_DEVELOPER_TOKEN --data-file=-
   printf 'CLIENT_ID'      | gcloud secrets create GOOGLE_ADS_CLIENT_ID --data-file=-
   printf 'CLIENT_SECRET'  | gcloud secrets create GOOGLE_ADS_CLIENT_SECRET --data-file=-
   printf 'REFRESH_TOKEN'  | gcloud secrets create GOOGLE_ADS_REFRESH_TOKEN --data-file=-
   ```
   (+ `secretAccessor` pour le SA sur chacun, cf. étape 3)
3. Redeploy en ajoutant aux `--set-env-vars` : `GOOGLE_ADS_LOGIN_CUSTOMER_ID=...,GOOGLE_ADS_CUSTOMER_ID=...`
   et aux `--set-secrets` les 4 secrets ci-dessus.
4. Le `/refresh` remplira automatiquement `google_daily`.

## Tester en local (optionnel, avant Cloud Run)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # remplir, puis :
set -a && source .env && set +a
export GOOGLE_APPLICATION_CREDENTIALS=/chemin/cle-sa.json   # un SA avec accès BigQuery
python -m ingestion.main setup
python -m ingestion.main backfill 24
```

## Coûts (ordre de grandeur)
Cloud Run quasi nul (1 exécution/jour), BigQuery quelques centimes (volumes faibles),
Scheduler gratuit. Budget mensuel attendu : négligeable (< quelques €).
