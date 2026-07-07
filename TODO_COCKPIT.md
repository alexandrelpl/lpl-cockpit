# TODO cockpit — chantiers en cours (à exécuter séquentiellement)
*Créé le 07/07/2026. Ordre proposé : 1 → 2 → 4 → 3 → 5 (du plus simple au plus lourd).*

## 1. ATV dans « Performance & COS »
Ajouter une colonne **ATV = CA net ÷ nombre de commandes**, juste après CA, avec couleur conditionnelle
(gradient comme les autres colonnes). Dans les 3 grains (daily/weekly/monthly).
→ Donnée dispo (`cockpit_daily.ca_shopify`, `orders`). Pas de nouvelle ingestion.

## 2. Bloc Clients — ajustements
- Défaut du toggle = **Web** (au lieu de Global).
- Renommer « dont X LPL » → « **dont X New to LPL** ».
- (Explication dimanche 05/07 : voir plus bas — ce n'est PAS un bug.)

## 3. Module estimation genre ✅ FAIT (déploiement à lancer)
Modules `first_names.py` (dico embarqué FR/EN/AR) + `shopify_gender.py` (crawl web → dico → gender-guesser
→ cache → Claude si ANTHROPIC_API_KEY → sinon Indéterminé). Tables `gender_daily` + `gender_name_cache`.
Endpoint `/api/gender`, section autonome « Genre estimé (web) » + rail.
Claude = OPTIONNEL : sans clé, dico + gender-guesser couvrent l'essentiel ; le reste = Indéterminé.

### (historique de la décision)
Nouveau module : lire les prénoms, estimer H/F (modèles pour prénoms FR / EN / AR),
afficher day/week/month + couleur conditionnelle. Bucket « Indéterminé » pour les inconnus/unisexes.
→ **DÉCIDÉ** : méthode = **hybride** (dictionnaire FR/EN/AR d'abord, Claude sur les prénoms non résolus, résultats mis en cache) ; champ = **prénom de livraison (shipping)**. Bucket « Indéterminé ».

## 4. Bloc « Comptoir · Solaires vs Optiques » ✅ FAIT
% Solaires vs Optiques par day/week/month + couleur. « Comptoir » = lunettes NON ajustées à la vue.
→ **DÉCIDÉ** : strict `product_type` (Solaires vs Monture Optique). Dumb Lens / Offre Santé / Atelier exclus (= à-la-vue).
→ Table `frame_type_daily` (repliée dans le crawl `shopify_price_mix`), endpoint `/api/comptoir`, section autonome + rail.

### 4-bis (PLUS TARD) — « Comptoir vs À la vue »
Comptoir = product_type Monture Optique/Solaires **non** montées ; À la vue = commandes où un `package_id`
contient à la fois un **DUMB** (monture) ET des **Verres** (ou Mix and Match). Nécessite de lire les package_id
des lignes de commande. À spécifier quand on l'attaque.

## 5. Segment NEDERLAND TEST ✅ FAIT (déploiement à lancer)
Sortir NL du COS principal (à partir du **03/07/2026**) et créer un tableau « NEDERLAND TEST » au-dessus.
- **Meta** : campagnes `NL - CVR - DABA` + `NL - CVR - PRO - TESTING`.
- **Google Ads** : campagne `LPL - NL - PMax - Solaires`.
- **Shopify** : commandes `shipping_country = NL` depuis le 03/07/2026 (colonnes `net_sales_nl`/`orders_nl`).
- Sessions NL via GA4 (`ga4_nl_daily`), **retirées** du principal (`GREATEST(0, sessions - sess_nl)`).
- Isolation aussi sur onglets **Meta** et **Google** : principal = hors NL, section « 🇳🇱 Nederland test » en bas
  (prédicat `NL_PRED` dans app.py = regex mot « NL » ET `date >= 2026-07-03`, cohérent avec la vue).
- Colonnes du tableau NL = celles de Performance & COS : Sessions, CVR, Cmd, CA, ATV, Meta, Google, Dépense, COS.

### Refactor UX associé (✅ FAIT) — sélecteur de grain global
Ancien « Détails comparés » (sous-onglets Jour/Semaine/Mois + toggles par bloc) remplacé par un
**sélecteur unique Jour/Semaine/Mois sticky** (`.grainbar`, `#grain-global`, défaut = Jour) qui pilote
TOUS les blocs temporels (Performance, Clients, Acquisition, Gammes, Comptoir, Genre, NL). Chaque analyse
= sa propre section titrée (chapitre du rail). État JS = `_grain` + `setGrain()` + `renderPerf/renderCust/
renderAcq/renderMix/renderComptoir/renderGender/renderNl`.

### Déploiement restant (une seule passe)
1. **Webapp** (`lpl-cockpit-web`) : embarque le refactor + isolation Meta/Google (bas risque, à lancer en 1er).
2. **BigQuery `setup`** : rebuild vue `cockpit_daily` + tables `ga4_nl_daily`/`gender_*`/`frame_type_daily` + colonnes NL.
3. **Rebuild Job** `lpl-cockpit-job` (`shopify_orders` NL, `ga4_nl`, `shopify_gender`, `shopify_price_mix`, `main`).
   ⚠️ RE-INCLURE `--command=python --args=job.py --service-account $SA` + TOUS les env + secrets,
   avec **`SHOPIFY_ACCESS_TOKEN=SHOPIFY_ACCESS_TOKEN:1`** (JAMAIS `:latest`). Sinon CA prod cassé (cf. CLAUDE.md).
4. **Backfills** : `shopify_orders` (colonnes NL depuis 03/07) + `ga4_nl` (sessions NL) + gender/price_mix.
→ **DÉCIDÉ** : NL uniquement à partir du 03/07/2026, sorti du principal ; avant le 03/07 rien ne change.

---
## Explication écart Web vs Web+Retail (dimanche 05/07/2026)
Web : 116 N / 23 F (139). Web+Retail : 111 N / 28 F (139). **Même total (139)** → aucune vente retail ce jour-là.
Les 5 clients passés de « Nouveau » à « Fidèle » sont **nouveaux sur le web mais déjà clients en boutique** dans les 3 ans :
en portée « global » (historique tous canaux), ils comptent comme fidèles. **C'est le comportement attendu, pas un bug.**
C'est justement ce que mesure « New to LPL » (vrais nouveaux marque).
