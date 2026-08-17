# État des lieux — Outil SEO Le Petit Lunetier
*Analytics + Update Shopify. Vue complète : ce qui est construit, forces, faiblesses, next steps.*
*Màj : juin 2026. Code : `lpl-cockpit/seo_tool/`. Docs liées : `audit_seo_lpl.md`, `brief_seo_execution.md`, `spec_outil_seo.md`.*

---

## 1. Vision & périmètre

Un outil en **deux volets** sur une seule source de vérité (le snapshot `seo_issues`) :
- **Volet 1 — Diagnostic & analytics** : crawle Shopify, mesure les manques SEO (on-page + sémantique), score, priorise par trafic réel × pertinence marché, et affiche un dashboard. **Read-only.**
- **Volet 2 — Mise à jour Shopify** : génère les correctifs (API Claude), fait valider, puis pousse dans Shopify (mutations). **À construire.**

Principe directeur : **le code calcule, Claude rédige, rien ne s'écrit sans validation.**

---

## 2. Ce qui est construit (V1 — diagnostic, fonctionnel)

### Architecture (`seo_tool/`)
| Fichier | Rôle | État |
|---|---|---|
| `config.py` | env, denylists, **règle de périmètre produits** | ✅ |
| `shopify_client.py` | client Admin GraphQL (pagination + anti-throttle) | ✅ |
| `detectors.py` | 4 détecteurs + requêtes | ✅ |
| `issues.py` | modèle d'issue **idempotent** (hash type+objet+champ) | ✅ |
| `scoring.py` | scores par catégorie + global (gère « non évalué ») | ✅ |
| `market_data.py` | trafic organique Semrush + **whitelist catégories marché** | ✅ (snapshot) |
| `crawl.py` | orchestrateur → snapshot | ✅ |
| `report.py` | export `seo_issues.json` + dashboard HTML autonome | ✅ |
| `run_diagnostic.py` | CLI | ✅ |
| `webapp.py` + `Dockerfile` | webapp Flask déployable (Cloud Run) | ✅ (non testée en ligne) |

### Ce qu'il détecte (résultats réels du dernier run)
- **Périmètre produits** : 1 459 produits → **672 dans le périmètre** (Solaires*/Optiques*/Monture Optique, hors inactif+non publié+pré-2021). Règle validée par tests.
- **Alt-text images** : ~0,3/100 — quasi 100 % des images sans alt (sur les produits du périmètre).
- **Pages collections** : ~8/100 — la grande majorité des collections indexables sans meta ni texte.
- **Meta produits** : ~75/100 — ~25 % des produits éligibles sans meta custom.
- **Traductions FR→EN** : *non évaluées* tant que le token n'a pas `read_translations` (dégradation propre, pas de crash).

### Priorisation des collections (répond à « lesquelles ? »)
Signal = **trafic organique réel 2026 (Semrush)** × **pertinence marché lunetterie** :
- **Tier 1 (marché + trafic prouvé)** : `lumiere-bleue` (3 456/mois), `lunettes-de-soleil-homme` (2 790), `lunettes-de-soleil-femme` (1 843), `lunettes-de-soleil` (1 347), + `lunettes-de-vue-femme`, `lunettes-papillon`.
- **Tier 2 (marché, opportunité à capter)** : `optiques`, lumière bleue F/H, formes (pantos, hexagonale, oversize, écaille), visages fins/larges.
- **Hors priorité** : capsules saisonnières (descente-givree…), accessoires.

### Off-site déjà disponible (via Semrush, pas encore dans le dashboard)
Authority Score **34**, **4 527 backlinks / 1 421 domaines référents**, ~3 700 mots-clés, **trafic organique à 65 % sur la marque** (home), grosses requêtes génériques en position 5-7 = gisement.

---

## 3. Forces
- **Architecture saine et découplée** : une source de vérité, détection déterministe, modèle idempotent → ré-exécutable sans doublon, extensible (ajouter un détecteur = une fonction).
- **Périmètre métier juste** : filtre produits exactement calé sur la règle (familles lunetterie + exclusion inactif/non publié/ancien), validé par tests.
- **Priorisation pilotée par la donnée réelle** (trafic Semrush + volumes), pas par intuition — aligne le travail sur le ROI SEO.
- **Robustesse** : anti-throttle Shopify, dégradation propre si un scope manque (ne casse pas tout le diagnostic).
- **Sortie immédiatement exploitable** : dashboard autonome + `seo_issues.json` prêt pour le volet 2.
- **Sécurité by design** : read-only à ce stade, token dédié, écriture conditionnée à validation (prévu).

## 4. Faiblesses / limites actuelles (sans complaisance)
- **Volet 2 non construit** : aujourd'hui on *diagnostique*, on ne *corrige* pas encore. Pas de génération (alt/meta/traduction) ni de push.
- **Off-site pas dans le dashboard** : les données Semrush (backlinks, positions) existent mais ne sont pas affichées ni transformées en reco dans l'app — c'est manuel.
- **`market_data.py` est un snapshot statique** : trafic/volumes Semrush figés à la main → à rebrancher sur l'API Semrush pour rester à jour (et passer du **organique** au **tous-canaux** via GA4 si on veut la vérité 2026 complète).
- **Persistance faible** : le snapshot vit en mémoire / `/tmp` → perdu au redéploiement. Pas d'historique des scores (donc pas de courbe de progression).
- **Webapp non sécurisée et non testée en ligne** : pas d'auth (à mettre derrière IAP/OAuth), crawl **synchrone** (1-2 min/requête, pas de file d'attente) → ok pour 1 utilisateur, pas pour de la charge.
- **Traductions** : nécessite le scope `read_translations` (sinon non évaluées).
- **Index bloat & doublons** : détectés implicitement (denylist) mais **aucune action** générée (noindex/dépublication/canonical des collections « 49€ » en triple, produits de test actifs).
- **Pas de tests automatisés** ni de CI (seuls des smoke-tests manuels ont été passés).
- **Alt-text « depuis l'image »** : la détection est faite, mais la **génération par vision** (lire l'image) est à construire (volet 2).

---

## 5. Next steps vers l'outil « parfait » (analytics + update, complet et fonctionnel)

### Priorité 1 — Volet 2 (boucler la boucle : corriger + pousser)
1. `generate.py` (API Claude) : alt-text **vision** (image + nom → alt ≤125c), meta produit/collection (gabarits `brief_seo_execution.md`), traduction FR→EN. Sortie en `suggested_value`.
2. UI de **validation / dry-run** : diff current→suggested, approuver/éditer/passer, bulk par type après revue d'échantillon.
3. `push.py` (mutations vérifiées) : `productUpdate` (meta), `productUpdateMedia` (alt), `collectionUpdate` (meta+texte), `translationsRegister` (avec `digest`). Lecture des `userErrors`, statut par issue, logs d'audit + rollback.
4. Scopes d'écriture : ajouter `write_products`, `write_translations` au token dédié.

### Priorité 2 — Onglet analytics off-site dans le dashboard
5. Brancher **Semrush en direct** (API : `domain_rank`, `backlinks_overview`, `domain_organic`, `domain_organic_unique`) + **Ahrefs** (2ᵉ source backlinks + content gap).
6. **Moteur de reco** déterministe (règles : collection pos 4-10 + volume>1000 + meta vide → P1 ; trafic marque dominant → développer le hors-marque…), formulé par Claude.
7. **Auto-refresh** des volumes/trafic Semrush → priorisation toujours à jour (remplace le snapshot statique `market_data.py`).

### Priorité 3 — Industrialisation
8. **Persistance + historique** : stocker chaque snapshot (BigQuery ou table dédiée) → courbe de score dans le temps, mesure d'impact après chaque vague.
9. **Auth + scaling** : webapp derrière OAuth @lepetitlunetier.com (réutiliser le socle cockpit), crawl **asynchrone** (job/queue + statut), `min-instances` selon usage.
10. **Index bloat & doublons** : générer les actions (noindex/dépublication des collections techniques, canonical/redirect des doublons « 49€ », dépublication des produits de test).
11. **Tests + CI** : unit tests des détecteurs/scoring/scope, golden tests sur un mini-catalogue, CI GitHub.
12. **GA4 tous-canaux** (option) : trafic par page collection toutes sources, pour une priorisation au-delà de l'organique.

### Priorité 4 — Raffinements SEO avancés
13. Maillage interne (guides ↔ collections), données structurées (Product/FAQ schema), surveillance des positions post-modif, AEO (réponses aux requêtes pour les moteurs IA — on voit déjà du trafic `chatgpt.com`).

---

## 6. Décisions ouvertes
- **Cible de déploiement** : pousser sur le service `seo-scan` existant (= l'écraser) **ou** déployer sur un nouveau service (`seo-tool`) pour ne rien casser ? → recommandation : **nouveau service** tant que le volet 2 n'est pas stabilisé.
- **Auth** : IAP Cloud Run vs OAuth applicatif (réutiliser le cockpit).
- **Source de priorisation** : organique (Semrush, en place) suffit-il, ou on ajoute le tous-canaux GA4 ?
- **Cadence** : diagnostic à la demande vs nightly automatique.
