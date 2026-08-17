# Spécification — Outil SEO Le Petit Lunetier (2 volets)
*Brique technique pour ton app SEO existante connectée à Shopify. Moteur : déterministe + API Claude ciblée.*
*Compagnon de `audit_seo_lpl.md` (diagnostic) et `brief_seo_execution.md` (contenus prêts).*

---

## 0. Principe directeur

Deux volets, une seule source de vérité (le **snapshot d'issues**) :

```
[Volet 1 — DIAGNOSTIC]                         [Volet 2 — UPDATE SHOPIFY]
Crawl Shopify (API)  ─┐                         lit issues "open"
Semrush API          ─┼─> snapshot seo_issues ─> génère correctifs (Claude API)
Ahrefs API           ─┘   + scores              ─> VALIDATION humaine (diff)
                                                 ─> push Shopify (mutations)
                                                 ─> re-crawl => mesure de l'impact
```

Règles non négociables :
- **Le code calcule, Claude rédige.** Détection/scoring = déterministe (API natives). Claude n'est appelé que pour **générer** (meta, alt, traduction, formulation de reco). Jamais Claude pour décider *quoi* écrire en base.
- **Aucune écriture Shopify sans validation** (au minimum un mode *dry-run* qui produit le diff avant push en masse).
- **Idempotence** : ne jamais ré-écrire un champ déjà conforme ; ré-exécuter le crawl ne doit pas recréer de doublons d'issues (clé = `type+object_id+field`).

---

## 1. Modèle de données — `seo_issues`

Un enregistrement par problème atomique :

| champ | type | description |
|---|---|---|
| `issue_id` | string | hash de `type + object_id + field` (idempotence) |
| `type` | enum | `collection_seo`, `image_alt`, `product_meta`, `translation_missing` |
| `severity` | enum | `high` / `medium` / `low` (pondère le score) |
| `object_type` | enum | `product` / `collection` / `image` |
| `object_id` | gid | ID Shopify (`gid://shopify/Product/...`) |
| `handle` | string | pour l'URL et le join Semrush |
| `field` | string | `seo.title`, `seo.description`, `media.alt`, `translation.en.title`… |
| `current_value` | text | valeur actuelle (souvent vide) |
| `context` | json | données utiles à la génération (titre produit, type, couleur, URL image, mot-clé cible, volume Semrush…) |
| `suggested_value` | text | rempli au volet 2 (génération) |
| `status` | enum | `open` → `generated` → `validated` → `pushed` / `skipped` / `error` |
| `priority_score` | int | pour trier (voir §3.5) |
| `detected_at` / `updated_at` | ts | |

**Scores du dashboard** (volet 1) — par catégorie : `score = 100 × items_conformes / items_total`. Score global = moyenne pondérée par sévérité. Afficher aussi le **nombre brut à corriger** par type (c'est l'« à corriger » que tu demandes).

---

## 2. Volet 1 — Détection (requêtes Shopify exactes)

> Pagination partout : `pageInfo { hasNextPage endCursor }`, repasser `endCursor` en `after`. Catalogue ≈ 1 459 produits / 111 collections.

### 2.1 SEO pages collections  *(severity high)*
```graphql
query($cursor:String){
  collections(first:50, after:$cursor){
    pageInfo{ hasNextPage endCursor }
    nodes{
      id handle title
      seo{ title description }
      descriptionHtml
      onlineStoreUrl          # null = non publiée -> NON indexable -> ignorer
      productsCount{ count }
    }
  }
}
```
**Issue si** : `onlineStoreUrl != null` (page publique) **ET** (`seo.title` vide **OU** `seo.description` vide **OU** `descriptionHtml` vide).
**Exclure** : collections opérationnelles (denylist de handles : `*tout-sauf*`, `*kat-*`, `product-feed`, `orderlyemails-*`, `reelup-*`, `*-copie*`). Celles-là relèvent de l'**index bloat** (à `noindex`, pas à enrichir).
`context` : `{ handle, productsCount }` + join Semrush (mot-clé cible + volume + position, cf. §3) pour `priority_score`.

### 2.2 Alt-text images  *(severity high, gros volume)*
```graphql
query($cursor:String){
  products(first:50, after:$cursor, query:"status:active"){
    pageInfo{ hasNextPage endCursor }
    nodes{
      id handle title productType tags
      media(first:20){ nodes{ ... on MediaImage { id image{ url altText } } } }
    }
  }
}
```
**Issue (une par image)** si `altText` vide/null. `object_id` = id du MediaImage, `context` = `{ product_title, productType, color (déduit du titre), image_url, form (déduit collection/tag) }`.

### 2.3 Meta produits manquantes  *(severity medium)*
Mêmes nœuds produits + `seo{ title description } status`.
**Issue si** `status = ACTIVE` **ET** (`seo.title` vide **OU** `seo.description` vide) **ET** `productType` ∉ {Carte cadeau, Accessoire…} (denylist par type/tag — étuis, chaînes, coffrets en priorité basse).

### 2.4 Traductions FR→EN manquantes  *(severity medium)*
Tu as des URL `/en/` → marché EN actif. Détection via l'API translations :
```graphql
query($cursor:String){
  translatableResources(first:50, after:$cursor, resourceType: PRODUCT){
    pageInfo{ hasNextPage endCursor }
    nodes{
      resourceId
      translatableContent{ key value digest locale }   # FR source + digest
      translations(locale:"en"){ key value outdated }   # EN existant
    }
  }
}
```
**Issue si** pour les `key` à traduire (`title`, `body_html`, `meta_title`, `meta_description`, `handle`) : aucune `translations(en)` correspondante **OU** `outdated = true`. Refaire avec `resourceType: COLLECTION`.
`context` doit stocker le **`digest`** de la clé (obligatoire pour écrire la traduction, cf. §5.3).

---

## 3. Off-site & sémantique — Semrush / Ahrefs (lecture)

### 3.1 Semrush (API REST, déjà validé)
| Donnée à afficher | Rapport |
|---|---|
| Authority Score, mots-clés organiques, trafic estimé | `domain_rank` (db `fr`) |
| Backlinks total / domaines référents / follow-nofollow | `backlinks_overview` |
| Top domaines référents (autorité) | `backlinks_refdomains` |
| Mots-clés + position + volume (split marque / hors-marque) | `domain_organic` |
| Pages qui captent l'organique | `domain_organic_unique` |
| Quick wins (positions 4-15 sur gros volume) | `domain_organic` + filtre position |

### 3.2 Ahrefs (si clé API) — équivalents : `site-explorer/backlinks`, `refdomains`, `organic-keywords`, `content-gap`. Sert de **2e source backlinks** (croiser pour fiabiliser) + content gap vs concurrents.

### 3.3 Join Semrush ↔ Shopify
Clé = **handle de collection** ↔ **URL Semrush** (`/collections/{handle}`). Permet de poser sur chaque issue `collection_seo` : `mot-clé cible`, `volume`, `position actuelle`.

### 3.4 Moteur de reco (déterministe + Claude pour la formulation)
Règles (code) :
- collection avec issue SEO **ET** position 4-10 **ET** volume > 1 000 → reco **« P1 — optimiser »** ;
- page qui ranke pos 1-3 mais sans meta → reco **« sécuriser/raffiner »** ;
- forte part de trafic sur la home (marque) → reco **« développer le hors-marque »** ;
- mot-clé où un concurrent te dépasse (content gap Ahrefs) → reco **« créer/renforcer contenu »**.
Claude ne fait que **mettre en phrase** la reco à partir des chiffres (pas de décision).

### 3.5 `priority_score` (tri du backlog)
`priority_score = volume_mensuel × poids_position × poids_sévérité`, avec `poids_position` max pour les positions 4-10 (gros gain à portée), faible pour pos 1-3 et > 30. → ton volet 2 attaque le backlog dans cet ordre.

---

## 4. Volet 1 — Dashboard (onglet 1)

Blocs à afficher :
1. **Score SEO global** + 4 jauges par catégorie (collections / alt / meta / traductions) avec le **nombre à corriger**.
2. **Backlog priorisé** (table triée par `priority_score`) : objet, type, valeur actuelle, mot-clé/volume/position, action recommandée.
3. **Off-site** : Authority Score, backlinks, domaines référents (tendance), split trafic marque vs hors-marque, top pages organiques.
4. **Quick wins** : pages en position 4-15 sur volume élevé + issue on-page ouverte (= effort faible, impact fort).

---

## 5. Volet 2 — Génération + Push (onglet 2)

### 5.1 Génération (API Claude, par type)
Prompts (gabarits, à garder courts et contraints) :

- **Alt-text** (Claude vision) : input = image (URL) + `product_title` + `form`. Sortie ≤ 125 car., FR, pattern `{modèle} {couleur} – {type} {forme} Le Petit Lunetier`, **décrire ce qui est visible**, pas de bourrage. Une variante par vue.
- **Meta produit** : input = `title, productType, color, bénéfice (lumière bleue / cat.3 / 100% Santé)`. Sortie = `{ title ≤60, description ≤155 }`, gabarit du `brief_seo_execution.md §1`.
- **Texte + meta collection** : input = `mot-clé cible, volume, nom collection`. Sortie = `{ title ≤60, meta ≤155, h1, intro_html (2 §, 80-120 mots) }`, règles `brief §1/§2`. Interdiction de keyword stuffing.
- **Traduction EN** : input = `key, value FR`. Sortie = traduction EN naturelle (pas mot-à-mot), respecter les balises HTML pour `body_html`.

Stocker la sortie dans `suggested_value`, passer `status = generated`.

### 5.2 Validation (obligatoire)
UI de diff **current → suggested**, par item, avec approuver / éditer / passer. Bulk-approve possible **par type** mais après revue d'un échantillon. `status = validated` avant tout push.

### 5.3 Push Shopify (mutations exactes, API 2024-01 — vérifiées)
- **Meta produit** :
  `productUpdate(input:{ id, seo:{ title, description } })`
- **Alt image** :
  `productUpdateMedia(productId:$pid, media:[{ id:$mediaId, alt:$alt }])`  *(UpdateMediaInput.alt confirmé)*
- **Collection (meta + texte)** :
  `collectionUpdate(input:{ id, seo:{ title, description }, descriptionHtml })`
- **Traduction** :
  `translationsRegister(resourceId:$rid, translations:[{ locale:"en", key:$key, value:$valueEN, translatableContentDigest:$digest }])`
  ⚠️ le `translatableContentDigest` vient du `translatableContent.digest` capté au §2.4 ; s'il a changé entre crawl et push, **re-fetch** avant d'écrire (sinon Shopify rejette).
- **Index bloat** (collections opérationnelles) : les dépublier de la publication Online Store (les retirer du canal) **ou** appliquer un template `noindex`. Ne pas supprimer (elles alimentent le back/feed).

Toujours lire le bloc `userErrors` de chaque mutation ; `status = pushed` seulement si vide, sinon `error` + message.

### 5.4 Robustesse
- **Coût de requête GraphQL** : batcher (≤ ~50 objets/requête), respecter le throttle (`extensions.cost.throttleStatus`), backoff sur 429/THROTTLED.
- **Reprise** : le `status` par issue rend le job ré-exécutable sans double-écriture.
- **Dry-run** : produire un export (CSV/JSON) du diff complet avant le premier push en masse.

---

## 6. Boucle de mesure
Après une vague : re-crawl Shopify → recalcul des scores (les issues `pushed` doivent disparaître). À J+14/J+28 : re-tirer `domain_organic` (Semrush) sur les mots-clés cibles pour mesurer le gain de position. Historiser les scores pour une courbe de progression dans le dashboard.

---

## 7. Scopes & sécurité
- **Shopify Admin API** : `read_products, write_products` (meta + media alt), `read_translations, write_translations` (traductions), `read_online_store_pages`/publications pour l'index bloat. (Pas besoin de `read_orders` ici → token distinct, principe d'un secret par app.)
- **Clés** : Semrush, Ahrefs, **Claude API** — en variables d'environnement / secret manager, jamais en dur.
- **Garde-fou** : validation humaine avant write ; logs d'audit (qui a poussé quoi, quand, ancienne/nouvelle valeur) pour pouvoir **rollback**.

---

## 8. Roadmap v1 (les 4 chantiers retenus)
1. **Socle** : crawl Shopify → `seo_issues` + scores + dashboard read-only (rien d'écrit). *Permet de valider le diagnostic avant toute écriture.*
2. **SEO collections** (P1 soleil H/F) : génération + validation + `collectionUpdate`. Plus gros levier.
3. **Alt-text images** en masse (Claude vision) : `productUpdateMedia`.
4. **Meta produits** manquantes : `productUpdate`.
5. **Traductions EN** : `translationsRegister`.
6. Brancher l'off-site (Semrush/Ahrefs) dans le dashboard + moteur de reco.

> Démarrer **read-only** (étape 1) est l'assurance que tout le pipeline de détection/scoring est juste avant d'autoriser la moindre écriture sur 1 459 produits.
