# seo_tool — Volet 1 : Diagnostic SEO (read-only)

Crawle la boutique Shopify, détecte les manques SEO, calcule un score par catégorie,
et produit un dashboard autonome + un snapshot `seo_issues.json` (consommé ensuite par le
volet 2 « mise à jour Shopify »).

**Read-only** : aucune écriture sur Shopify à ce stade. C'est volontaire — on valide que le
diagnostic est juste avant d'autoriser le moindre push.

## Ce qu'il détecte (v1)
- `collection_seo` — pages collections **indexables** sans balise title/description ou sans texte.
- `image_alt` — images produit sans texte alternatif.
- `product_meta` — produits actifs sans title/description SEO custom (hors accessoires/cartes).
- `translation_missing` — clés SEO (title, body, meta) non traduites (ou périmées) dans la locale cible.

## Prérequis : un token Admin LECTURE dédié
Dans Shopify : **Paramètres → Applications et canaux de vente → Développer des applications →
Créer une application** (« LPL SEO Tool »). Onglet **Configuration → Admin API**, cocher :
`read_products`, `read_translations`, `read_online_store_pages`. **Installer**, puis copier le
**jeton d'accès Admin API** (`shpat_…`).
> Token DÉDIÉ à cet outil (≠ token du cockpit). Pour le volet 2, on ajoutera `write_products` + `write_translations`.

## Lancer
```
cd ~/Documents/GitHub/meta-ads-analyzer/lpl-cockpit
source .venv/bin/activate
export SHOPIFY_SHOP_URL=test-store20.myshopify.com
export SHOPIFY_ADMIN_TOKEN=shpat_xxxxxxxx
python -m seo_tool.run_diagnostic
```
Options : `--no-translations` pour un premier run plus rapide (saute le crawl des traductions).

Sorties (dans `seo_tool/`) :
- `seo_issues.json` — snapshot complet (id idempotent par issue).
- `seo_dashboard.html` — à ouvrir dans un navigateur (scores + backlog priorisé).

## Architecture (fichiers)
| Fichier | Rôle |
|---|---|
| `config.py` | env + denylists (collections opérationnelles, produits faible valeur) |
| `shopify_client.py` | client Admin GraphQL (pagination + throttle) |
| `detectors.py` | les 4 détecteurs + requêtes |
| `issues.py` | modèle d'issue idempotent |
| `scoring.py` | scores par catégorie + global |
| `crawl.py` | orchestrateur → snapshot |
| `report.py` | export JSON + dashboard HTML |
| `run_diagnostic.py` | point d'entrée CLI |

## Suite (à construire)
- Volet 2 : génération (API Claude : alt depuis l'image, meta, traduction) → validation → push (mutations Shopify).
- Off-site : branchement Semrush/Ahrefs dans le dashboard + moteur de reco.
Voir `../analysis/spec_outil_seo.md`.
