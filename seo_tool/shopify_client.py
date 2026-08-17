"""
Client Shopify Admin GraphQL — lecture seule, pagination, gestion du throttle.

Conçu pour crawler un catalogue de ~1500 produits sans se faire jeter :
- backoff sur 429 / THROTTLED,
- pause adaptative selon le coût de requête restant (extensions.cost.throttleStatus),
- pagination générique via un curseur.
"""

from __future__ import annotations
import time
from typing import Callable, Iterator

import requests

from seo_tool import config


class ShopifyError(RuntimeError):
    pass


class ShopifyClient:
    def __init__(self, shop_url: str | None = None, token: str | None = None,
                 api_version: str | None = None):
        self.url = (f"https://{shop_url}/admin/api/{api_version or config.API_VERSION}/graphql.json"
                    if shop_url else config.GRAPHQL_URL)
        self.token = (token or config.ADMIN_TOKEN)
        self.session = requests.Session()
        self._last_cost = 100
        self._throttle: dict = {}

    def execute(self, query: str, variables: dict | None = None) -> dict:
        headers = {"X-Shopify-Access-Token": self.token, "Content-Type": "application/json"}
        payload = {"query": query, "variables": variables or {}}
        for attempt in range(8):
            try:
                r = self.session.post(self.url, json=payload, headers=headers, timeout=(15, 90))
            except requests.RequestException as e:
                time.sleep(2 * (attempt + 1))
                if attempt == 7:
                    raise ShopifyError(f"réseau : {e}")
                continue
            if r.status_code == 429:
                time.sleep(2 * (attempt + 1)); continue
            if r.status_code in (401, 403):
                raise ShopifyError(f"AUTH {r.status_code} — vérifie le token/scopes (read_products, "
                                   f"read_translations). {r.text[:200]}")
            if r.status_code >= 500:
                time.sleep(3 * (attempt + 1)); continue
            data = r.json()
            errs = data.get("errors")
            if errs:
                if any((e.get("extensions", {}) or {}).get("code") == "THROTTLED" for e in errs
                       if isinstance(e, dict)):
                    time.sleep(2 * (attempt + 1)); continue
                raise ShopifyError(f"GraphQL: {errs}")
            cost = (data.get("extensions", {}) or {}).get("cost", {})
            self._last_cost = cost.get("requestedQueryCost", self._last_cost)
            self._throttle = cost.get("throttleStatus", self._throttle)
            self._respect_throttle()
            return data["data"]
        raise ShopifyError("échec après 8 tentatives")

    def _respect_throttle(self):
        avail = self._throttle.get("currentlyAvailable")
        restore = self._throttle.get("restoreRate")
        if avail is not None and restore and avail < self._last_cost:
            time.sleep(min(2.0, (self._last_cost - avail) / restore))
        else:
            time.sleep(0.15)

    def paginate(self, query: str, root: str, variables: dict | None = None) -> Iterator[dict]:
        """Itère tous les noeuds d'une connexion paginée.
        `query` doit exposer {root}(first:.., after:$cursor){ pageInfo{hasNextPage endCursor} nodes{..} }
        et déclarer `$cursor: String`."""
        cursor = None
        while True:
            v = dict(variables or {}); v["cursor"] = cursor
            data = self.execute(query, v)
            conn = data
            for part in root.split("."):
                conn = conn[part]
            for node in conn["nodes"]:
                yield node
            page = conn["pageInfo"]
            if not page["hasNextPage"]:
                break
            cursor = page["endCursor"]
