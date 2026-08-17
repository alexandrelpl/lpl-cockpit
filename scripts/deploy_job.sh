#!/usr/bin/env bash
#
# Déploiement du Cloud Run Job `lpl-cockpit-job` — SOURCE DE VÉRITÉ des env/secrets.
#
# Pourquoi ce script : `--set-env-vars` et `--set-secrets` REMPLACENT tout le bloc.
# Un redéploiement qui en oublie un le supprime silencieusement (déjà arrivé :
# BUDGET_SHEET_ID perdu, GOOGLE_OAUTH_CLIENT_ID écrasé). En plus, la commande complète
# dépasse 1500 caractères et se fait tronquer au copier-coller dans le terminal.
#
# ⚠️ SHOPIFY_ACCESS_TOKEN est ÉPINGLÉ sur :1 — JAMAIS :latest (sinon le CA prod casse,
#    cf. CLAUDE.md « Secrets Shopify — piège résolu »).
#
# Usage :
#   bash scripts/deploy_job.sh              # rebuild + déploie le job (entrée normale job.py)
#   bash scripts/deploy_job.sh setup        # rebuild puis lance le setup BigQuery
#   bash scripts/deploy_job.sh probe        # rebuild puis lance le probe PMax
#   bash scripts/deploy_job.sh pmax         # rebuild puis remplit les tables PMax (rapide)
#   bash scripts/deploy_job.sh refresh      # rebuild puis lance un refresh complet (~25 min)
#
set -euo pipefail

PROJECT=shopify-data-ltv
REGION=europe-west1
JOB=lpl-cockpit-job
SA=lpl-cockpit-ingest@${PROJECT}.iam.gserviceaccount.com

cd "$(dirname "$0")/.."

ENVS="BQ_PROJECT=${PROJECT}"
ENVS="${ENVS},BQ_DATASET=lpl_cockpit"
ENVS="${ENVS},BQ_LOCATION=EU"
ENVS="${ENVS},SHOPIFY_SHOP_URL=test-store20.myshopify.com"
ENVS="${ENVS},SHOPIFY_API_VERSION=2024-01"
ENVS="${ENVS},TIMEZONE=Europe/Paris"
ENVS="${ENVS},META_ACCOUNT_ID=305450184"
ENVS="${ENVS},META_API_VERSION=v21.0"
ENVS="${ENVS},SHOPIFY_REFRESH_DAYS=40"
ENVS="${ENVS},ADS_REFRESH_DAYS=40"
ENVS="${ENVS},GOOGLE_COST_SHEET_ID=1ZrKs7hArLNpxSau6DLZwLFo4Udrff7ZASwbB7xMSCS4"
ENVS="${ENVS},SESSIONS_SHEET_ID=1uHl3DRVfhqAQnVT6lpm1tfbBG5BTfWd7RuasCy4SJAs"
ENVS="${ENVS},INCOMING_SHEET_ID=1I7b3gey0QFC27NEiFZJKXgEwlR0vHLsI4xChB-qq-Vg"
ENVS="${ENVS},GOOGLE_ADS_LOGIN_CUSTOMER_ID=1143252152"
ENVS="${ENVS},GOOGLE_ADS_CUSTOMER_ID=1557825645"
ENVS="${ENVS},GA4_PROPERTY_ID=309711659"
ENVS="${ENVS},CRO_REFRESH_DAYS=35"

# ⚠️ Épinglé sur la version qui a les scopes read_orders/customers + read_inventory/locations.
# v4 = token régénéré le 20/07/2026 avec read_inventory + read_locations (pour l'OOS produit).
# v1 = token d'origine sans inventory ; v2/v3 = versions intermédiaires (secret partagé). JAMAIS :latest.
SECS="SHOPIFY_ACCESS_TOKEN=SHOPIFY_ACCESS_TOKEN:4"
SECS="${SECS},META_ACCESS_TOKEN=META_ACCESS_TOKEN:latest"
SECS="${SECS},GOOGLE_ADS_DEVELOPER_TOKEN=GOOGLE_ADS_DEVELOPER_TOKEN:latest"
SECS="${SECS},GOOGLE_ADS_CLIENT_ID=GOOGLE_ADS_CLIENT_ID:latest"
SECS="${SECS},GOOGLE_ADS_CLIENT_SECRET=GOOGLE_ADS_CLIENT_SECRET:latest"
SECS="${SECS},GOOGLE_ADS_REFRESH_TOKEN=GOOGLE_ADS_REFRESH_TOKEN:latest"
SECS="${SECS},GA4_REFRESH_TOKEN=GA4_REFRESH_TOKEN:latest"
SECS="${SECS},ANTHROPIC_API_KEY=ANTHROPIC_API_KEY:latest"

echo "==> Rebuild + déploiement du job ${JOB} (region ${REGION})"
gcloud run jobs deploy "${JOB}" \
  --source . \
  --region "${REGION}" \
  --project "${PROJECT}" \
  --service-account "${SA}" \
  --command=python \
  --args=job.py \
  --memory 512Mi --cpu 1 \
  --task-timeout=3600 \
  --max-retries=3 \
  --set-env-vars "${ENVS}" \
  --set-secrets "${SECS}"

MODE="${1:-}"
[ -z "${MODE}" ] && { echo "==> Terminé (job déployé, entrée = job.py)."; exit 0; }

case "${MODE}" in
  setup)   ARGS="^@^-m@ingestion.main@setup" ;;
  probe)   ARGS="^@^-m@ingestion.google_pmax_probe" ;;
  pmax)    ARGS="^@^-m@ingestion.google_pmax@40" ;;
  products) ARGS="^@^-m@ingestion.shopify_products" ;;
  websales) ARGS="^@^-m@ingestion.shopify_line_items@40" ;;
  retailsales) ARGS="^@^-m@ingestion.retail_sales" ;;
  incoming) ARGS="^@^-m@ingestion.product_incoming" ;;
  orders) ARGS="^@^-m@ingestion.shopify_orders@refresh@40" ;;
  ga4nl) ARGS="^@^-m@ingestion.ga4_nl@40" ;;
  refresh) ARGS="" ;;
  *) echo "Mode inconnu: ${MODE} (setup|probe|pmax|products|websales|retailsales|incoming|orders|ga4nl|refresh)"; exit 1 ;;
esac

if [ -n "${ARGS}" ]; then
  echo "==> Bascule temporaire de l'entrée -> ${MODE}"
  gcloud run jobs update "${JOB}" --region "${REGION}" --project "${PROJECT}" \
    --command=python "--args=${ARGS}"
fi

echo "==> Exécution (${MODE})"
set +e
gcloud run jobs execute "${JOB}" --region "${REGION}" --project "${PROJECT}" --wait
STATUS=$?
set -e

if [ -n "${ARGS}" ]; then
  echo "==> Restauration de l'entrée normale (job.py)"
  gcloud run jobs update "${JOB}" --region "${REGION}" --project "${PROJECT}" \
    --command=python --args=job.py
fi

# Logs de CETTE exécution uniquement, en ordre chronologique.
# (Piège corrigé : `gcloud logging read` trie du plus RÉCENT au plus ancien ; un `| tail`
#  affichait donc les lignes les plus VIEILLES — on voyait le run précédent.)
EXEC=$(gcloud run jobs executions list --job "${JOB}" --region "${REGION}" \
  --project "${PROJECT}" --limit 1 --format='value(metadata.name)')
echo "==> Logs de l'exécution ${EXEC} :"
gcloud logging read \
  "resource.labels.job_name=\"${JOB}\" AND labels.\"run.googleapis.com/execution_name\"=\"${EXEC}\"" \
  --order=asc --limit=500 --format='value(textPayload)' --project "${PROJECT}"

exit ${STATUS}
