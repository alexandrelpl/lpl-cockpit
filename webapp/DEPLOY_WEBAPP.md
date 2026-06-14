# Déploiement — Cockpit web (façade)

Appli web Cloud Run, connexion Google restreinte à `@lepetitlunetier.com`, branchée sur
BigQuery (`cockpit_daily`, `meta_daily`) + alertes Meta en direct.

On réutilise le projet (`shopify-data-ltv`), la région (`europe-west1`) et le robot
(`lpl-cockpit-ingest`) déjà en place. Variables de session supposées encore définies :
`$PROJECT`, `$REGION`, `$SA`. (Sinon : `export PROJECT=shopify-data-ltv REGION=europe-west1
SA=lpl-cockpit-ingest@shopify-data-ltv.iam.gserviceaccount.com`)

---

## 1. Client OAuth « Web » (5 min, dans la console)

Le client OAuth utilisé pour l'API Google Ads était de type **Desktop** ; ici il faut un
client de type **Application Web**. Sur [console.cloud.google.com](https://console.cloud.google.com) :

1. **API et services → Écran de consentement OAuth** : type **Interne** (réservé à ton
   organisation Workspace `lepetitlunetier.com`) → enregistre. Cela limite déjà la connexion
   à ton domaine, au niveau de Google.
2. **API et services → Identifiants → Créer des identifiants → ID client OAuth** :
   - Type : **Application Web**
   - Nom : `LPL Cockpit Web`
   - (les URIs de redirection seront ajoutées à l'étape 4, une fois l'URL connue)
   - Crée, puis note l'**ID client** et le **secret client**.

## 2. Secrets (clé de session + secret OAuth)

```bash
# clé de session aléatoire
python3 -c "import secrets;print(secrets.token_hex(32))" | gcloud secrets create COCKPIT_SECRET_KEY --data-file=-
# secret du client OAuth Web
printf '%s' 'COLLE_LE_SECRET_CLIENT_OAUTH' | gcloud secrets create GOOGLE_OAUTH_CLIENT_SECRET --data-file=-

# accès du robot aux secrets (META_ACCESS_TOKEN lui est déjà accordé)
gcloud secrets add-iam-policy-binding COCKPIT_SECRET_KEY --member="serviceAccount:$SA" --role="roles/secretmanager.secretAccessor"
gcloud secrets add-iam-policy-binding GOOGLE_OAUTH_CLIENT_SECRET --member="serviceAccount:$SA" --role="roles/secretmanager.secretAccessor"
```

(Le robot a déjà `bigquery.dataEditor` + `jobUser`, donc il peut lire BigQuery.)

## 3. Déployer l'appli (depuis le dossier `webapp/`)

```bash
cd ~/Documents/GitHub/meta-ads-analyzer/lpl-cockpit/webapp

gcloud run deploy lpl-cockpit-web \
  --source . --region $REGION --service-account $SA \
  --allow-unauthenticated \
  --memory 512Mi --cpu 1 \
  --set-env-vars BQ_PROJECT=$PROJECT,BQ_DATASET=lpl_cockpit,BQ_LOCATION=EU,ALLOWED_DOMAIN=lepetitlunetier.com,ROI_FLOOR=2,META_ACCOUNT_ID=305450184,META_API_VERSION=v21.0,GOOGLE_OAUTH_CLIENT_ID=TON_ID_CLIENT_OAUTH.apps.googleusercontent.com \
  --set-secrets SECRET_KEY=COCKPIT_SECRET_KEY:latest,GOOGLE_OAUTH_CLIENT_SECRET=GOOGLE_OAUTH_CLIENT_SECRET:latest,META_ACCESS_TOKEN=META_ACCESS_TOKEN:latest

export WEB=$(gcloud run services describe lpl-cockpit-web --region $REGION --format='value(status.url)')
echo $WEB
```

> Note : `--allow-unauthenticated` ouvre l'accès réseau, mais l'appli **bloque tout** sauf
> les comptes `@lepetitlunetier.com` (la connexion Google est gérée dans le code). La page
> n'affiche rien sans login valide.

## 4. Enregistrer l'URL de redirection dans le client OAuth

Avec l'URL affichée (`$WEB`), retourne dans **Identifiants → ton client OAuth Web** et ajoute :

- **Origines JavaScript autorisées** : `https://lpl-cockpit-web-….run.app`
- **URI de redirection autorisés** : `https://lpl-cockpit-web-….run.app/auth/callback`

Enregistre (la prise en compte peut prendre 1-2 min).

## 5. Tester

Ouvre `$WEB` dans ton navigateur → tu dois être redirigé vers la connexion Google →
connecte-toi en `@lepetitlunetier.com` → le cockpit s'affiche (onglet COS par défaut).

Teste aussi avec un compte hors domaine (ou navigation privée) : tu dois voir « Accès réservé ».

---

## Dépannage

- **`redirect_uri_mismatch`** : l'URI de l'étape 4 ne correspond pas exactement (https, pas de
  slash final, bon sous-domaine). Recopie l'URL exacte de `$WEB` + `/auth/callback`.
- **Page « Accès réservé » alors que tu es bien du domaine** : vérifie que l'écran de
  consentement est en **Interne** et que tu utilises bien ton adresse `@lepetitlunetier.com`.
- **Onglet Meta « Alertes indisponibles »** : le token Meta a peut-être expiré (~60 j) — mets
  à jour le secret `META_ACCESS_TOKEN` (nouvelle version) et redéploie.
- **Mettre à jour le code plus tard** : `gcloud run deploy lpl-cockpit-web --source . --region $REGION`.

## Partage à l'équipe

Une fois validé, partage simplement l'URL `$WEB` à l'équipe : chacun se connecte avec son
compte `@lepetitlunetier.com`. Aucun autre réglage par utilisateur.
