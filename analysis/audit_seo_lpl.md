# Audit SEO — Le Petit Lunetier (lepetitlunetier.com)

*Réalisé via le MCP Shopify (on-page / on-store) + données GA4 12 mois. Juin 2026.*

## Périmètre & limites (à lire d'abord)

Ce que le MCP Shopify permet d'auditer : **SEO on-page / on-store** — balises meta, structure des URL, contenu des pages produits/collections, texte alternatif des images, contenu blog. C'est fait ci-dessous.

Ce qu'il **ne peut pas** voir, et qui nécessite un outil tiers : **backlinks, autorité de domaine, positions/mots-clés, concurrence, crawl technique (vitesse, erreurs, maillage)**. Pour ça → Google Search Console (gratuit, à connecter en priorité), Ahrefs ou Semrush.

Catalogue analysé : **1 459 produits actifs, 111 collections**. Analyse meta sur échantillon de 50 produits + 40 collections (les plus récentes).

---

## Ce qui va bien

- **URLs propres** : handles descriptifs, en français, en kebab-case (`lunettes-de-soleil-femme`, `lunettes-pantos`, `lumiere-bleue`). Bonne base.
- **Meta des solaires bien faites** : les lunettes de soleil ont des balises title/description custom de qualité (type + modèle + bénéfice + « anti-UV Cat. 3 »). C'est le bon gabarit à généraliser.
- **L'Organic Search est déjà ton moteur** (GA4) : 45 % des sessions, 54 % des achats, CVR au-dessus de la moyenne. Le SEO travaille — il est juste sous-exploité sur une grande partie du site.

---

## Problèmes classés par gravité

### 🔴 CRITIQUE 1 — Les pages collections (catégories) n'ont quasiment aucun SEO
Les pages catégories sont les **pages les plus stratégiques** pour le SEO e-commerce (elles ciblent les requêtes commerciales à fort volume). Or sur 40 collections inspectées, **quasiment toutes ont `seo` vide ET aucun texte de description** :

| Collection | Produits | Meta SEO | Texte page |
|---|---|---|---|
| Solaires Femmes (`lunettes-de-soleil-femme`) | 222 | ❌ vide | ❌ vide |
| Solaires Hommes | 110 | ❌ | ❌ |
| Optiques (`optiques`) | 264 | ❌ | ❌ |
| Lumière Bleue | 264 | ❌ | ❌ |
| Lunettes Pantos | 156 | ❌ | ❌ |
| Lunettes Hexagonale / Oversize / Écaille | 65–147 | ❌ | ❌ |

Ces pages ciblent « lunettes de soleil femme », « lunettes anti lumière bleue », « lunettes pantos »… et tournent sans balise meta ni paragraphe de contenu. **C'est le plus gros gisement de trafic qualifié du site.** Seule « Meilleures Ventes 6 mois » est correctement optimisée — preuve que c'est faisable.

### 🔴 CRITIQUE 2 — 0 % de texte alternatif sur les images
Sur **tous** les produits et **toutes** les images de l'échantillon, `altText` est vide. Pour une marque dont le produit est 100 % visuel, c'est :
- une perte sèche de trafic **Google Images** (canal organique gratuit, très pertinent en lunetterie) ;
- un problème d'**accessibilité** ;
- une perte de contexte sémantique pour Google sur chaque page.

### 🟠 IMPORTANT 3 — ~38 % des produits sans meta custom, concentrés sur l'optique
Environ **38 % des produits** (échantillon) n'ont ni title ni description SEO custom → ils retombent sur le gabarit par défaut du thème. Le manque est **concentré sur les montures optiques / lumière bleue** (les solaires, elles, sont faites). Tu as visiblement optimisé les solaires lors d'un projet passé sans finir l'optique.

### 🟠 IMPORTANT 4 — Index bloat : collections opérationnelles & produits de test indexables
Beaucoup de collections techniques / dupliquées qui n'ont rien à faire dans l'index Google et diluent le crawl :
`Tout sauf accessoires et verres` (+ 2 copies), `// KAT - Tous sauf accessoires` (1403), `Tous sauf Verres à la vue` (1343), `OrderlyEmails - Recommended Products` (834), `Product Feed` (655), `REELUP (DO NOT DELETE)`…
Plus des **produits de test actifs** (`Thème 2018 - Test Produit sans image`). → À passer en `noindex` / exclure du sitemap / dépublier.

### 🟠 IMPORTANT 5 — Cannibalisation entre collections quasi-dupliquées
Plusieurs collections visent le même mot-clé et se concurrencent :
- « Montures à 49€ » existe en 3 exemplaires (`montures-a-49-selection-2026`, `selection-de-montures-a-49`, `collection-ephemere`).
- « Lumière Bleue » / « Lumière Bleue Femme » / « Lumière Bleue Homme » : OK si hiérarchisées et meta distinctes, sinon cannibalisation.
→ Choisir une page canonique par intention, rediriger/dé-indexer les doublons.

### 🟡 MOYEN 6 — Contenu éditorial sous-développé et brouillon
Des blogs existent (`Style by LPL`, `Découvre nos lookbooks`) mais il y a des blogs par défaut vides (« Titre du site » ×2). Le contenu de fond (guides : « quelles lunettes pour un visage rond », « bien choisir ses lunettes anti-lumière bleue », tendances) est le levier organique haut-de-funnel le moins exploité. À structurer.

### 🟡 MOYEN 7 — Couleurs en produits séparés = pages quasi-dupliquées
Chaque coloris est un produit distinct (Hanna.B Silver / Gold / Rose Gold…) avec des descriptions quasi identiques → risque de contenu dupliqué et d'autorité diluée. À gérer via maillage interne et balises canoniques (ou regroupement en variantes).

---

## Off-site : backlinks & affiliation (ce que GA4 montre)

Le détail backlinks complet exige Ahrefs/Semrush, mais le trafic référent/affilié GA4 (12 mois) éclaire déjà :

- **L'affiliation = surtout du cashback / code promo** : `affilae`, `ma-reduc.com` (3,7 % CVR), `savoo.fr` (6,4 %), `poulpeo.com` (4,7 %), `influenceur.promo` (4,7 %). Ces sources « convertissent » fort **mais à faible incrémentalité** : elles captent des acheteurs déjà décidés qui cherchaient un code. Utile mais ne crée pas de demande.
- **Pépites à fort intent à amplifier** : le **quiz/typeform** (2,3 % CVR, fort panier), `fr.search.yahoo.com`, presse. Reproductibles.
- **Référents sociaux faibles en conversion directe** : `l.instagram.com` (0,4 %), `m.facebook.com` (0,4 %) — du clic, peu d'achat direct (logique haut de funnel).
- **Trafic AI émergent** : `chatgpt.com` commence à apparaître (petit). À surveiller (SEO/AEO).

Total référent + affiliation ≈ 30 000 sessions / 547 achats sur 12 mois (~2 % des achats) : marginal aujourd'hui, donc **le vrai levier reste le SEO organique**, pas l'affiliation.

---

## Off-site & positions (données Semrush, base FR)

**Profil de liens** : Authority Score **34**, **4 527 backlinks** depuis **1 421 domaines référents** (3 570 follow / 931 nofollow). Profil presse honorable : Marie Claire, Cosmopolitan, Grazia, Public, Fortune, + Pinterest/Yandex/Substack/Crunchbase. Beaucoup de domaines à 1-2 liens → profil large mais peu profond. AS 34 = correct, pas dominant.

**Organic : ~3 711 mots-clés, ~39 300 visites/mois estimées — MAIS 65 % du trafic va sur la home**, tiré par la marque (« le petit lunetier » 18 100 vol/mois, + dizaines de variantes et requêtes locales « le petit lunetier {ville} »). Autrement dit : **ton SEO récolte surtout la demande de marque existante, il n'acquiert pas encore beaucoup de demande nouvelle.**

**Le gisement (confirmé par les positions)** : les grosses requêtes **non-marque** sur lesquelles tu ranks en **page 1 basse (position 5-7)** — exactement les pages collections sans meta ni contenu :

| Requête | Volume/mois | Position | Page |
|---|---|---|---|
| lunette de soleil homme | 27 100 | **5** | `lunettes-de-soleil-homme` |
| lunette de soleil femme | 18 100 | **7** | `lunettes-de-soleil-femme` |
| lunettes de soleil homme | 12 100 | **6** | `lunettes-de-soleil-homme` |
| lunettes de soleil femme | 9 900 | **7** | `lunettes-de-soleil-femme` |
| lunette soleil homme | 6 600 | 5 | `lunettes-de-soleil-homme` |
| lunettes soleil femme | 3 600 | 3 | `lunettes-de-soleil-femme` |
| lunettes lumière bleue | 1 900 | **2** | `lumiere-bleue` |
| lunette ecran bleu | 1 300 | 2 | `lumiere-bleue` |

**Lecture** : passer ces pages de la position 5-7 au top 3 sur des volumes de 10 000-27 000 = un multiple du trafic qualifié actuel. **C'est LE levier.** La preuve que ça marche : `lumiere-bleue` (déjà position 1-2) génère ~9 % de tout ton trafic organique à elle seule — alors que les pages soleil, mieux dotées en volume, plafonnent en position 5-7 faute d'optimisation. Réplique la profondeur de `lumiere-bleue` sur soleil et optique.

**Ce qui marche déjà et est à étendre** : les guides de contenu rankent (`guide-sur-lessayage-virtuel` = 198 mots-clés, `guide-pour-bien-choisir-sa-monture` = 95) ; le SEO local des boutiques fonctionne (`le petit lunetier {ville}` en position 1).

---

## Plan de travail priorisé (impact × effort)

**Quick wins (fort impact, faisable vite) :**
1. **Optimiser les meta + texte des 15–20 collections stratégiques** (solaires F/H, optiques, lumière bleue, formes pantos/hexagonale/oversize, par visage). Title + description + 2 paragraphes de contenu ciblé par page. → plus gros ROI SEO du site.
2. **Générer les `altText` de toutes les images produits** (modèle + forme + couleur + « lunettes Le Petit Lunetier »). Faisable en masse.
3. **Compléter les meta des produits optique / lumière bleue** (~38 % du catalogue) sur le gabarit déjà utilisé pour les solaires.
4. **Nettoyer l'index** : dépublier/`noindex` les collections techniques & produits de test, exclure du sitemap.

**Moyen terme :**
5. Résoudre la cannibalisation (canoniques + redirections sur les doublons 49€ / lumière bleue).
6. Structurer le **contenu éditorial** (guides d'achat ciblant les requêtes informationnelles) + nettoyer les blogs par défaut.
7. Maillage interne entre coloris d'un même modèle + canoniques.

**Outillage (pour piloter le SEO dans la durée) :**
8. Connecter **Google Search Console** (gratuit) → requêtes, positions, pages, couverture d'index réelle.
9. Connecter **Ahrefs ou Semrush** → audit backlinks, autorité, gaps de mots-clés vs concurrents, crawl technique.

---

## Ce que je peux faire directement ensuite

- **Écrire les meta (title/description) et les altText en masse via le MCP Shopify** (mutations) — en commençant par les collections stratégiques puis l'optique. Je te soumets les textes pour validation avant écriture.
- **Lister précisément** les collections à dé-indexer et les produits de test à dépublier.
- Une fois GSC / Ahrefs connecté : audit backlinks + mots-clés réels.
