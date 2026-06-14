# Pistes d'amélioration par source — robustesse, légèreté, vitesse

Classées par impact. ⭐ = recommandé en priorité.

## Shopify — CA / commandes (le plus lourd : ~25 min/run)

Le coût vient de la requête GraphQL : `orders(first:50)` avec **deux connexions imbriquées**
(`refunds.transactions` et `customer.orders`) qui démultiplient le coût → throttling sévère
depuis Cloud Run.

- ⭐ **Supprimer la sous-requête `customer.orders(first:1)`** (la plus chère). Elle ne sert
  qu'à classer NEW/EXISTING. Alternative : `customer { numberOfOrders }` (1 champ scalaire,
  quasi gratuit) — « new » si `numberOfOrders <= 1`. Approximation très proche, coût divisé.
- ⭐ **Session HTTP persistante (keep-alive)** au lieu d'une connexion par page : supprime
  l'essentiel de la lenteur observée sur Cloud Run.
- **Piloter sur le throttle réel** : lire `extensions.cost.throttleStatus.currentlyAvailable`
  et n'attendre que le nécessaire, au lieu d'un `sleep(0.4)` fixe + back-off aveugle.
- **Bulk Operations API** pour les backfills : asynchrone, pas de pagination ni de throttling
  → un export 24 mois fiable en une opération (vs 733 jours paginés).
- **Fenêtre nuit plus courte** : 7 j quotidiens + une passe hebdo 40-60 j pour rattraper les
  remboursements tardifs. Allège fortement le run quotidien.
- **(Refonte) stocker au niveau commande** (`order_id, created_date, net`) plutôt que des
  agrégats journaliers : permet l'incrémental vrai par `updated_at`, l'audit, et le recalcul
  exact d'un jour. C'est la base la plus propre à terme.

## Meta

- ⭐ **Token System User (n'expire pas)** depuis le Business Manager, au lieu du token
  long-lived (60 j) qui casse silencieusement. Supprime la panne la plus fréquente.
- À défaut : **rafraîchissement automatique** du long-lived avant expiration (cron + endpoint
  d'échange) plutôt qu'une régénération manuelle.
- **Fenêtre 14 j** suffisante au quotidien (les chiffres se stabilisent vite) ; backfill via
  **insights asynchrones** (évite « reduce the amount of data »).

## Google

- ⭐ **Basculer sur l'API Google Ads (GAQL)** dès l'accès Basic accordé — `google_ads.py` est
  prêt. Plus robuste et plus riche (conversions, valeur, par campagne) que le Sheet, et
  supprime la dépendance au script externe.
- En attendant : le Sheet est OK ; la santé des données alerte déjà s'il n'est pas à jour.

## Sessions (maillon le plus fragile)

- Aujourd'hui : dépend du **scraper Playwright sur le Mac** (doit être allumé). C'est la source
  la moins robuste.
- ⭐ **Re-tenter GA4** proprement : l'échec d'ajout du compte de service vient souvent d'une
  restriction Workspace ; contourner via un **Groupe Google** (ajouter le SA au groupe, donner
  l'accès GA4 au groupe). GA4 = source serveur fiable, sans Mac. `ga4_traffic.py` est déjà prêt.
- Sinon : porter le scraper sur Cloud Run (Playwright headless) — possible mais fragile
  (session Shopify liée à l'appareil/IP + 2FA → re-auth fréquentes). À n'envisager que si GA4
  reste bloqué.

## Transverse (architecture & exploitation)

- ⭐ **Alerte proactive** (Cloud Monitoring) : email/Slack si un job échoue ou si une table
  n'a pas été mise à jour > 24 h — au lieu d'attendre que quelqu'un ouvre le dashboard.
- **Isoler les sources en jobs séparés** : un échec Shopify ne doit pas retarder Meta/Google.
  (Déjà partiellement le cas : erreurs isolées par source ; aller plus loin = 1 job par source.)
- **Tests unitaires** sur le parsing/agrégation (la logique CA est déjà couverte à 10/10 ;
  étendre à meta/sessions).
- **Rotation des secrets** + rappel calendaire (token Meta, token Shopify).
- **Logs structurés** (déjà lisibles) ; ajouter un identifiant de run pour tracer chaque nuit.
- **Sortir `lpl-cockpit/` en dépôt autonome** et pousser régulièrement sur GitHub (actuellement
  en retard sur le déployé).
