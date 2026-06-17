"""
LPL Cockpit — façade web.

Appli Flask sur Cloud Run :
- Connexion Google restreinte au domaine @lepetitlunetier.com (OAuth).
- Lit BigQuery (vue cockpit_daily + meta_daily) pour les onglets COS et Meta.
- Tire l'API Meta en direct pour les alertes adset/ad (non stockées en base).

Config (variables d'env / Secret Manager) :
  BQ_PROJECT, BQ_DATASET=lpl_cockpit, BQ_LOCATION=EU
  GOOGLE_OAUTH_CLIENT_ID, GOOGLE_OAUTH_CLIENT_SECRET   (client OAuth "Web")
  ALLOWED_DOMAIN=lepetitlunetier.com
  SECRET_KEY                                           (clé de session aléatoire)
  META_ACCESS_TOKEN, META_ACCOUNT_ID=305450184         (pour les alertes live)
  ROI_FLOOR=2
"""

from __future__ import annotations
import os
import time
import calendar
import functools
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta

import requests
import google.auth
from google.auth.transport.requests import Request as GAuthRequest
from flask import Flask, session, redirect, url_for, jsonify, request, abort, render_template
from authlib.integrations.flask_client import OAuth
from google.cloud import bigquery

# ---- config ----
BQ_PROJECT     = os.environ["BQ_PROJECT"]
BQ_DATASET     = os.environ.get("BQ_DATASET", "lpl_cockpit")
BQ_LOCATION    = os.environ.get("BQ_LOCATION", "EU")
ALLOWED_DOMAIN = os.environ.get("ALLOWED_DOMAIN", "lepetitlunetier.com")
ROI_FLOOR      = float(os.environ.get("ROI_FLOOR", "2"))
META_TOKEN     = os.environ.get("META_ACCESS_TOKEN", "").strip()
META_ACCOUNT   = os.environ.get("META_ACCOUNT_ID", "305450184")
META_API       = os.environ.get("META_API_VERSION", "v21.0")
# Objectifs (Google Sheet « Budget 2026 » : CA en ligne 8, dépense en ligne 15,
# une colonne par mois à partir de D=janvier).
BUDGET_SHEET_ID  = os.environ.get("BUDGET_SHEET_ID", "")
BUDGET_SHEET_TAB = os.environ.get("BUDGET_SHEET_TAB", "Budget 2026")
RUN_REGION       = os.environ.get("CLOUD_RUN_REGION", "europe-west1")
SESSIONS_JOB     = os.environ.get("SESSIONS_JOB", "lpl-cockpit-sessions")

app = Flask(__name__)
app.secret_key = os.environ["SECRET_KEY"]
app.config.update(SESSION_COOKIE_SECURE=True, SESSION_COOKIE_HTTPONLY=True,
                  SESSION_COOKIE_SAMESITE="Lax")

oauth = OAuth(app)
oauth.register(
    name="google",
    client_id=os.environ["GOOGLE_OAUTH_CLIENT_ID"],
    client_secret=os.environ["GOOGLE_OAUTH_CLIENT_SECRET"],
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={"scope": "openid email profile", "hd": ALLOWED_DOMAIN},
)

_bq = None
def bq() -> bigquery.Client:
    global _bq
    if _bq is None:
        _bq = bigquery.Client(project=BQ_PROJECT)
    return _bq

def q(sql: str, params: list | None = None):
    cfg = bigquery.QueryJobConfig(query_parameters=params or [])
    return [dict(r) for r in bq().query(sql, job_config=cfg, location=BQ_LOCATION).result()]

def T(name: str) -> str:
    return f"`{BQ_PROJECT}.{BQ_DATASET}.{name}`"


# ---- auth ----
def login_required(fn):
    @functools.wraps(fn)
    def wrapper(*a, **kw):
        if not session.get("user"):
            if request.path.startswith("/api/"):
                abort(401)
            return redirect(url_for("login"))
        return fn(*a, **kw)
    return wrapper


# ---- cache mémoire des endpoints BigQuery (la donnée ne change qu'1×/nuit) ----
_BQ_CACHE: dict = {}

def bq_cache(ttl=900):
    """Sert la réponse JSON en cache si < ttl s (clé = chemin + query string). ?force=1 bypass."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*a, **kw):
            if request.args.get("force"):
                return fn(*a, **kw)
            key = request.full_path
            ent = _BQ_CACHE.get(key)
            now = time.time()
            if ent and now - ent[0] < ttl:
                return app.response_class(ent[1], mimetype="application/json")
            out = fn(*a, **kw)
            resp = out[0] if isinstance(out, tuple) else out
            try:
                if getattr(resp, "status_code", 200) == 200:
                    _BQ_CACHE[key] = (now, resp.get_data(as_text=True))
            except Exception:  # noqa: BLE001
                pass
            return out
        return wrapper
    return deco


@app.route("/login")
def login():
    redirect_uri = url_for("auth_callback", _external=True, _scheme="https")
    return oauth.google.authorize_redirect(redirect_uri)


@app.route("/auth/callback")
def auth_callback():
    token = oauth.google.authorize_access_token()
    info = token.get("userinfo") or {}
    email = (info.get("email") or "").lower()
    verified = info.get("email_verified", False)
    if not (verified and email.endswith("@" + ALLOWED_DOMAIN)):
        return render_template("denied.html", email=email, domain=ALLOWED_DOMAIN), 403
    session["user"] = email
    return redirect(url_for("index"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/healthz")
def healthz():
    return "ok", 200


@app.route("/")
@login_required
def index():
    return render_template("dashboard.html", user=session["user"], roi_floor=ROI_FLOOR)


# ---- helpers métriques ----
def _trend(series, key, days):
    """Compare la moyenne des `days` derniers jours à celle des `days` précédents."""
    if len(series) < days * 2:
        return None
    recent = [r[key] for r in series[-days:] if r[key] is not None]
    prior = [r[key] for r in series[-days * 2:-days] if r[key] is not None]
    if not recent or not prior:
        return None
    a, b = sum(recent) / len(recent), sum(prior) / len(prior)
    if b == 0:
        return None
    return round((a - b) / b * 100, 1)


def _roas_window(series, spend_key, value_key, days=3):
    """ROAS agrégé (somme valeur / somme dépense) sur les `days` derniers jours,
    comparé aux `days` précédents. Plus juste qu'une moyenne de ratios quotidiens."""
    if len(series) < days * 2:
        return None
    rec, pri = series[-days:], series[-days * 2:-days]
    sr = sum(r[spend_key] or 0 for r in rec); vr = sum(r[value_key] or 0 for r in rec)
    sp = sum(r[spend_key] or 0 for r in pri); vp = sum(r[value_key] or 0 for r in pri)
    now = (vr / sr) if sr else None
    prev = (vp / sp) if sp else None
    pct = round((now - prev) / prev * 100, 1) if (now is not None and prev) else None
    return {"now": now, "prev": prev, "pct": pct, "days": days}


# ---- API : vue d'ensemble (COS) ----
@app.route("/api/overview")
@login_required
@bq_cache()
def api_overview():
    days = min(int(request.args.get("days", 30)), 400)
    fetch = max(days, 16)   # on tire au moins 16 j pour calculer les tendances 7j
    allrows = q(
        f"""SELECT date, ca_shopify, orders, meta_spend, google_spend, ad_spend_total,
                   cos_blended, roas_blended, sessions, conversion_rate
            FROM {T('cockpit_daily')}
            WHERE date BETWEEN DATE_SUB(CURRENT_DATE(), INTERVAL @d DAY) AND DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY)
            ORDER BY date""",
        [bigquery.ScalarQueryParameter("d", "INT64", fetch)],
    )
    for r in allrows:
        r["date"] = r["date"].isoformat()
        s = r.get("sessions")
        r["cvr"] = (r["orders"] / s) if s else None
    rows = allrows[-days:]   # série affichée (graphe + table)

    # Month-to-date (du 1er du mois à hier, jours complets)
    m = q(f"""SELECT COALESCE(SUM(ad_spend_total),0) spend, COALESCE(SUM(ca_shopify),0) ca,
                     COALESCE(SUM(orders),0) orders, COALESCE(SUM(sessions),0) sessions
              FROM {T('cockpit_daily')}
              WHERE date >= DATE_TRUNC(CURRENT_DATE(), MONTH) AND date < CURRENT_DATE()""")[0]
    mtd = {"spend": m["spend"], "ca": m["ca"], "orders": m["orders"],
           "sessions": m["sessions"] or None,
           "cos": (m["spend"] / m["ca"]) if m["ca"] else None,
           "roas": (m["ca"] / m["spend"]) if m["spend"] else None,
           "cvr": (m["orders"] / m["sessions"]) if m["sessions"] else None}

    def tr(field):
        return {"d3": _trend(allrows, field, 3), "d7": _trend(allrows, field, 7)}

    # Seuils quotidiens (objectif du mois ÷ jours) pour la heatmap du tableau.
    daily_targets = None
    try:
        td = date.today()
        ca_t, sp_t = _month_targets(td)
        dim = calendar.monthrange(td.year, td.month)[1]
        daily_targets = {"ca": ca_t / dim, "spend": sp_t / dim,
                         "cos": (sp_t / ca_t) if ca_t else None,
                         "roas": (ca_t / sp_t) if sp_t else None}
    except Exception:  # noqa: BLE001
        daily_targets = None

    return jsonify({
        "rows": rows,
        "mtd": mtd,
        "daily_targets": daily_targets,
        "trends": {"spend": tr("ad_spend_total"), "ca": tr("ca_shopify"),
                   "cos": tr("cos_blended"), "roas": tr("roas_blended"),
                   "orders": tr("orders"), "sessions": tr("sessions"), "cvr": tr("cvr")},
    })


# ---- API : détail par semaine / par mois (avec comparatif N-1 pro-rata) ----
@app.route("/api/periods")
@login_required
@bq_cache()
def api_periods():
    end = date.today() - timedelta(days=1)   # J-1 : on ignore le jour partiel
    rows = q(
        f"""SELECT date, COALESCE(ca_shopify,0) ca, COALESCE(orders,0) orders,
                   COALESCE(ad_spend_total,0) spend, sessions
            FROM {T('cockpit_daily')}
            WHERE date <= @end AND date >= DATE_SUB(@end, INTERVAL 800 DAY)""",
        [bigquery.ScalarQueryParameter("end", "DATE", end.isoformat())],
    )
    by = {r["date"]: r for r in rows}

    def agg(d0, d1):
        ca = o = sp = ses = 0
        has_ses = False
        dd = d0
        while dd <= d1:
            r = by.get(dd)
            if r:
                ca += r["ca"]; o += r["orders"]; sp += r["spend"]
                if r["sessions"] is not None:
                    ses += r["sessions"]; has_ses = True
            dd += timedelta(days=1)
        return {"ca": ca, "orders": o, "spend": sp,
                "sessions": ses if has_ses else None,
                "cvr": (o / ses) if (has_ses and ses) else None,
                "cos": (sp / ca) if ca else None}

    def pct(a, b):
        return ((a - b) / b * 100) if (a is not None and b) else None

    def pts(a, b):  # écart en points de % (pour CVR, COS)
        return ((a - b) * 100) if (a is not None and b is not None) else None

    def block(label, is_current, cur, prev):
        return {"label": label, "current": is_current,
                "sessions": cur["sessions"], "sessions_cmp": pct(cur["sessions"], prev["sessions"]),
                "cvr": cur["cvr"], "cvr_cmp": pts(cur["cvr"], prev["cvr"]),
                "orders": cur["orders"], "orders_cmp": pct(cur["orders"], prev["orders"]),
                "ca": cur["ca"], "ca_cmp": pct(cur["ca"], prev["ca"]),
                "spend": cur["spend"], "spend_cmp": pct(cur["spend"], prev["spend"]),
                "cos": cur["cos"], "cos_cmp": pts(cur["cos"], prev["cos"])}

    # --- Semaines (8 dernières, lundi->dimanche), vs semaine précédente ---
    weeks = []
    monday = end - timedelta(days=end.weekday())
    for i in range(8):
        ws = monday - timedelta(days=7 * i)
        we = end if i == 0 else ws + timedelta(days=6)
        ndays = (we - ws).days
        ps = ws - timedelta(days=7)
        weeks.append(block(ws.isoformat(), i == 0, agg(ws, we), agg(ps, ps + timedelta(days=ndays))))

    # --- Mois (depuis janvier), vs même mois N-1, pro-rata pour le mois courant ---
    months = []
    for mo in range(1, end.month + 1):
        ms = date(end.year, mo, 1)
        if mo == end.month:
            me = end
            ps, pe = date(end.year - 1, mo, 1), date(end.year - 1, mo, min(end.day, calendar.monthrange(end.year - 1, mo)[1]))
        else:
            me = date(end.year, mo, calendar.monthrange(end.year, mo)[1])
            ps, pe = date(end.year - 1, mo, 1), date(end.year - 1, mo, calendar.monthrange(end.year - 1, mo)[1])
        months.append(block(ms.isoformat(), mo == end.month, agg(ms, me), agg(ps, pe)))

    return jsonify({"weeks": weeks, "months": months})


# ---- API : Meta (campagnes, depuis BigQuery) ----
@app.route("/api/meta")
@login_required
@bq_cache()
def api_meta():
    days = min(int(request.args.get("days", 7)), 90)
    daily = q(
        f"""SELECT date, SUM(spend) spend, SUM(purchase_value) value, SUM(purchases) purchases,
                   SUM(impressions) impressions, SUM(clicks) clicks
            FROM {T('meta_daily')}
            WHERE campaign_id IS NOT NULL
              AND date BETWEEN DATE_SUB(CURRENT_DATE(), INTERVAL @d DAY) AND DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY)
            GROUP BY date ORDER BY date""",
        [bigquery.ScalarQueryParameter("d", "INT64", days)],
    )
    for r in daily:
        r["date"] = r["date"].isoformat()
        r["roas"] = (r["value"] / r["spend"]) if r["spend"] else None
        r["cpa"] = (r["spend"] / r["purchases"]) if r["purchases"] else None
        r["ctr"] = (r["clicks"] / r["impressions"]) if r["impressions"] else None
    camp = q(
        f"""SELECT campaign_name,
                   SUM(spend) spend, SUM(purchases) purchases,
                   SUM(purchase_value) value, SUM(impressions) impressions
            FROM {T('meta_daily')}
            WHERE campaign_id IS NOT NULL
              AND date BETWEEN DATE_SUB(CURRENT_DATE(), INTERVAL @d DAY) AND DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY)
            GROUP BY campaign_name HAVING spend > 0 ORDER BY spend DESC""",
        [bigquery.ScalarQueryParameter("d", "INT64", days)],
    )
    for r in camp:
        r["roas"] = (r["value"] / r["spend"]) if r["spend"] else 0
        r["cpa"] = (r["spend"] / r["purchases"]) if r["purchases"] else 0
    def trM(field):
        return {"d3": _trend(daily, field, 3), "d7": _trend(daily, field, 7)}
    return jsonify({
        "daily": daily, "campaigns": camp,
        "roas3": _roas_window(daily, "spend", "value", 3),
        "trends": {"spend": trM("spend"), "impressions": trM("impressions"), "ctr": trM("ctr"),
                   "purchases": trM("purchases"), "value": trM("value"), "cpa": trM("cpa"),
                   "roas": trM("roas")},
    })


# ---- API : Google (depuis BigQuery, vide tant que pas d'accès Basic) ----
@app.route("/api/google")
@login_required
@bq_cache()
def api_google():
    days = min(int(request.args.get("days", 7)), 90)
    win = (f"date BETWEEN DATE_SUB(CURRENT_DATE(), INTERVAL @d DAY) "
           f"AND DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY)")
    p = [bigquery.ScalarQueryParameter("d", "INT64", days)]

    daily = q(f"""SELECT date, SUM(cost) cost, SUM(conversion_value) value, SUM(conversions) conv
                  FROM {T('google_daily')} WHERE {win} GROUP BY date ORDER BY date""", p)
    for r in daily:
        r["date"] = r["date"].isoformat()
        r["roas"] = (r["value"] / r["cost"]) if r["cost"] else None
        r["cpa"] = (r["cost"] / r["conv"]) if r["conv"] else None

    camp = q(f"""SELECT campaign_name, campaign_type, SUM(cost) cost, SUM(conversions) conv,
                        SUM(conversion_value) value, SUM(impressions) impressions
                 FROM {T('google_daily')} WHERE {win}
                 GROUP BY campaign_name, campaign_type HAVING cost > 0 ORDER BY cost DESC""", p)
    by_type = q(f"""SELECT campaign_type, SUM(cost) cost, SUM(conversions) conv,
                           SUM(conversion_value) value
                    FROM {T('google_daily')} WHERE {win}
                    GROUP BY campaign_type HAVING cost > 0 ORDER BY cost DESC""", p)
    for r in camp + by_type:
        r["roas"] = (r["value"] / r["cost"]) if r["cost"] else 0
        r["cpa"] = (r["cost"] / r["conv"]) if r["conv"] else 0

    m = q(f"""SELECT COALESCE(SUM(cost),0) cost, COALESCE(SUM(conversions),0) conv,
                     COALESCE(SUM(conversion_value),0) value FROM {T('google_daily')}
              WHERE date >= DATE_TRUNC(CURRENT_DATE(), MONTH) AND date < CURRENT_DATE()""")[0]
    mtd = {"cost": m["cost"], "conv": m["conv"], "value": m["value"],
           "roas": (m["value"] / m["cost"]) if m["cost"] else None,
           "cpa": (m["cost"] / m["conv"]) if m["conv"] else None}

    return jsonify({"available": len(camp) > 0, "daily": daily, "campaigns": camp,
                    "by_type": by_type, "mtd": mtd,
                    "roas3": _roas_window(daily, "cost", "value", 3),
                    "trends": {"roas_3d": _trend(daily, "roas", 3),
                               "roas_7d": _trend(daily, "roas", 7)}})


@app.route("/api/google/asset-groups")
@login_required
@bq_cache()
def api_google_asset_groups():
    days = min(int(request.args.get("days", 30)), 90)
    try:
        rows = q(f"""SELECT campaign_name, asset_group_name,
                            SUM(cost) cost, SUM(conversions) conv,
                            SUM(conversion_value) value, SUM(impressions) impressions
                     FROM {T('google_asset_group_daily')}
                     WHERE date BETWEEN DATE_SUB(CURRENT_DATE(), INTERVAL @d DAY)
                       AND DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY)
                     GROUP BY campaign_name, asset_group_name HAVING cost > 0 ORDER BY cost DESC""",
                 [bigquery.ScalarQueryParameter("d", "INT64", days)])
        for r in rows:
            r["roas"] = (r["value"] / r["cost"]) if r["cost"] else 0
            r["cpa"] = (r["cost"] / r["conv"]) if r["conv"] else 0
        return jsonify({"ok": True, "rows": rows})
    except Exception as e:  # noqa: BLE001 (table peut ne pas encore exister)
        return jsonify({"ok": False, "error": str(e)}), 200


# ---- API : CRO (funnel GA4 + produits + canaux + disponibilité) ----
def _pct(a, b):
    return round((a - b) / b * 100, 1) if (a is not None and b) else None

def _pts(a, b):
    return round((a - b) * 100, 1) if (a is not None and b is not None) else None

# CRO : on ne pilote l'écoulement / la disponibilité que sur le cœur de gamme.
CRO_PRODUCT_TYPES = ["Monture Optique", "Solaires"]


def _norm(x):
    return (x or "").strip().lower()


def _rupture_reason(it):
    """Réalité métier : indisponible bien avant stock 0."""
    if it["status"] != "ACTIVE":
        return "Brouillon / archivée"
    if not it["published"]:
        return "Retirée de Boutique en ligne"
    if it["total_inventory"] is not None and it["total_inventory"] <= 0:
        return "Stock épuisé"
    return None


def _inventory_map():
    """(liste, map normalisée) du stock cœur de gamme."""
    inv = q(f"""SELECT product_title, total_inventory, status, published
                FROM {T('shopify_inventory')} WHERE product_type IN UNNEST(@types)""",
            [bigquery.ArrayQueryParameter("types", "STRING", CRO_PRODUCT_TYPES)])
    return inv, {_norm(r["product_title"]): r for r in inv}


def _oos_sellers(end, min_sales=5):
    """Produits du cœur de gamme qui ont vendu (>= min_sales sur 14 j) mais sont
    indisponibles maintenant. Matching GA4<->Shopify insensible casse/espaces."""
    sells = q(f"""SELECT item_name, SUM(purchases) pur, SUM(revenue) rev
                  FROM {T('ga4_items_daily')}
                  WHERE date >= DATE_SUB(@e, INTERVAL 14 DAY)
                  GROUP BY item_name HAVING pur >= @ms""",
              [bigquery.ScalarQueryParameter("e", "DATE", end.isoformat()),
               bigquery.ScalarQueryParameter("ms", "INT64", min_sales)])
    inv, stock = _inventory_map()
    oos = []
    for s in sells:
        it = stock.get(_norm(s["item_name"]))
        if not it:
            continue
        reason = _rupture_reason(it)
        if reason:
            oos.append({"product": s["item_name"], "purchases_14d": s["pur"],
                        "revenue_14d": round(s["rev"], 2), "stock": it["total_inventory"],
                        "reason": reason})
    oos.sort(key=lambda x: x["purchases_14d"], reverse=True)
    active = sum(1 for r in inv if _rupture_reason(r))
    return oos, active


def _product_movers(end, lookback, base, stock, min_delta=3, drop=0.70, surge=1.30, max_each=2):
    """Produits cœur de gamme qui bougent fort (hausses ET baisses) sur `lookback` jours
    vs les `lookback` jours précédents. Pour les baisses, on classe la cause via les vues
    GA4 + le stock : amont (pub catalogue/lien/merch home), disponibilité, ou fiche."""
    cs = (end - timedelta(days=lookback - 1)).isoformat(); ce = end.isoformat()
    ps = (end - timedelta(days=2 * lookback - 1)).isoformat(); pe = (end - timedelta(days=lookback)).isoformat()
    rows = q(f"""SELECT i.item_name,
                   SUM(IF(i.date BETWEEN @cs AND @ce, i.purchases, 0)) cp,
                   SUM(IF(i.date BETWEEN @ps AND @pe, i.purchases, 0)) pp,
                   SUM(IF(i.date BETWEEN @cs AND @ce, i.views, 0)) cv,
                   SUM(IF(i.date BETWEEN @ps AND @pe, i.views, 0)) pv
                 FROM {T('ga4_items_daily')} i
                 JOIN (SELECT DISTINCT product_title FROM {T('shopify_inventory')}
                       WHERE product_type IN UNNEST(@types)) s
                   ON LOWER(TRIM(i.item_name)) = LOWER(TRIM(s.product_title))
                 WHERE i.date BETWEEN @ps AND @ce GROUP BY i.item_name""",
             [bigquery.ScalarQueryParameter("cs", "DATE", cs), bigquery.ScalarQueryParameter("ce", "DATE", ce),
              bigquery.ScalarQueryParameter("ps", "DATE", ps), bigquery.ScalarQueryParameter("pe", "DATE", pe),
              bigquery.ArrayQueryParameter("types", "STRING", CRO_PRODUCT_TYPES)])
    down, up = [], []
    for r in rows:
        cp, pp, delta = r["cp"], r["pp"], r["cp"] - r["pp"]
        if max(cp, pp) < base or abs(delta) < min_delta:
            continue
        pct = round((cp - pp) / pp * 100, 1) if pp else None
        if pp and cp <= pp * drop:  # baisse marquée
            vdrop = ((r["cv"] - r["pv"]) / r["pv"]) if r["pv"] else None
            it = stock.get(_norm(r["item_name"]))
            reason = _rupture_reason(it) if it else None
            if reason:
                cause = f"indisponible ({reason.lower()})"
            elif vdrop is not None and vdrop <= -0.30:
                cause = f"vues −{abs(round(vdrop * 100))}% (amont : pub/merch)"
            else:
                cause = "conversion fiche en baisse"
            down.append({"product": r["item_name"], "cur": cp, "prev": pp, "pct": pct,
                         "delta": delta, "cause": cause})
        elif (pp == 0 and cp >= base) or (pp and cp >= pp * surge):  # hausse marquée ou nouveau
            up.append({"product": r["item_name"], "cur": cp, "prev": pp, "pct": pct,
                       "delta": delta, "new": pp == 0})
    down.sort(key=lambda x: x["delta"])
    up.sort(key=lambda x: -x["delta"])
    return down[:max_each], up[:max_each]


@app.route("/api/cro")
@login_required
@bq_cache()
def api_cro():
    end = date.today() - timedelta(days=1)
    rows = q(f"""SELECT date, COALESCE(sessions,0) s, COALESCE(add_to_carts,0) atc,
                        COALESCE(checkouts,0) co, COALESCE(purchases,0) pu, COALESCE(item_views,0) iv
                 FROM {T('ga4_funnel_daily')}
                 WHERE date <= @e AND date >= DATE_SUB(@e, INTERVAL 15 DAY)""",
             [bigquery.ScalarQueryParameter("e", "DATE", end.isoformat())])
    by = {r["date"]: r for r in rows}

    def agg(d0, d1):
        s = atc = co = pu = iv = 0
        dd = d0
        while dd <= d1:
            r = by.get(dd)
            if r:
                s += r["s"]; atc += r["atc"]; co += r["co"]; pu += r["pu"]; iv += r["iv"]
            dd += timedelta(days=1)
        return {"sessions": s, "atc": atc, "checkout": co, "orders": pu, "item_views": iv,
                "atc_rate": (atc / s) if s else None,
                "checkout_rate": (co / atc) if atc else None,
                "completion_rate": (pu / co) if co else None,
                "cvr": (pu / s) if s else None}

    def win(label, d):
        cur = agg(end - timedelta(days=d - 1), end)
        prev = agg(end - timedelta(days=2 * d - 1), end - timedelta(days=d))
        return {"label": label, "current": cur, "previous": prev,
                "cmp": {"sessions": _pct(cur["sessions"], prev["sessions"]),
                        "atc": _pct(cur["atc"], prev["atc"]),
                        "checkout": _pct(cur["checkout"], prev["checkout"]),
                        "orders": _pct(cur["orders"], prev["orders"]),
                        "atc_rate": _pts(cur["atc_rate"], prev["atc_rate"]),
                        "checkout_rate": _pts(cur["checkout_rate"], prev["checkout_rate"]),
                        "completion_rate": _pts(cur["completion_rate"], prev["completion_rate"]),
                        "cvr": _pts(cur["cvr"], prev["cvr"])}}

    windows = [win("Hier (J-1)", 1), win("3 derniers jours", 3), win("7 derniers jours", 7)]

    # ---- Diagnostic 2 horizons (3 j / 7 j), funnel + produits, hausses ET baisses ----
    rate_labels = [("atc_rate", "Sessions → ATC"), ("checkout_rate", "ATC → Checkout"),
                   ("completion_rate", "Checkout → Commande")]
    try:
        _, stock = _inventory_map()
    except Exception:  # noqa: BLE001
        stock = {}

    base_by_h = {1: 3, 3: 5, 7: 8}
    horizons = []
    for label, hw, win in [("Hier (J-1)", 1, windows[0]), ("3 derniers jours", 3, windows[1]),
                           ("7 derniers jours", 7, windows[2])]:
        cmp = win["cmp"]
        downs = [(l, cmp[k]) for k, l in rate_labels if cmp.get(k) is not None and cmp[k] <= -1]
        ups = [(l, cmp[k]) for k, l in rate_labels if cmp.get(k) is not None and cmp[k] >= 1]
        fdown = min(downs, key=lambda x: x[1]) if downs else None
        fup = max(ups, key=lambda x: x[1]) if ups else None
        try:
            md, mu = _product_movers(end, hw, base_by_h[hw], stock)
        except Exception:  # noqa: BLE001
            md, mu = [], []
        horizons.append({"label": label, "lookback": hw,
                         "cvr_pts": cmp.get("cvr"), "orders_pct": cmp.get("orders"),
                         "sessions_pct": cmp.get("sessions"),
                         "funnel_down": ({"step": fdown[0], "pts": fdown[1]} if fdown else None),
                         "funnel_up": ({"step": fup[0], "pts": fup[1]} if fup else None),
                         "movers_down": md, "movers_up": mu})

    # Contexte conservateur (calculé une fois, fenêtre 3 j).
    context = []
    try:
        cs3, ce3 = (end - timedelta(days=2)).isoformat(), end.isoformat()
        ps3, pe3 = (end - timedelta(days=5)).isoformat(), (end - timedelta(days=3)).isoformat()
        ch = q(f"""SELECT channel,
                     SUM(IF(date BETWEEN @cs AND @ce, sessions, 0)) cs,
                     SUM(IF(date BETWEEN @ps AND @pe, sessions, 0)) ps,
                     SUM(IF(date BETWEEN @cs AND @ce, purchases, 0)) cp
                   FROM {T('ga4_channels_daily')} WHERE date BETWEEN @ps AND @ce GROUP BY channel""",
               [bigquery.ScalarQueryParameter("cs", "DATE", cs3), bigquery.ScalarQueryParameter("ce", "DATE", ce3),
                bigquery.ScalarQueryParameter("ps", "DATE", ps3), bigquery.ScalarQueryParameter("pe", "DATE", pe3)])
        tc = sum(r["cs"] for r in ch) or 0
        tp = sum(r["ps"] for r in ch) or 0
        blended = (sum(r["cp"] for r in ch) / tc) if tc else None
        best = None
        for r in ch:
            if not (tc and tp and r["cs"]):
                continue
            gain = (r["cs"] / tc - r["ps"] / tp) * 100
            cvr = r["cp"] / r["cs"]
            if gain >= 5 and blended and cvr < blended * 0.8 and (best is None or gain > best[1]):
                best = (r["channel"], round(gain, 1))
        if best:
            context.append(f"Mix de trafic : « {best[0]} » (CVR plus faible) a gagné {best[1]} pts de part sur 3 j "
                           "— un repli de conversion peut venir d'un trafic moins qualifié, pas du site.")
    except Exception:  # noqa: BLE001
        pass
    try:
        so = len(_oos_sellers(end, 5)[0])
        if so:
            context.append(f"{so} produit(s) fort(s) vendeur(s) actuellement en rupture — voir « Produits en Rupture ».")
    except Exception:  # noqa: BLE001
        pass
    context.append("Facteurs externes non mesurés (saisonnalité, pré-soldes, météo, actualité) : à garder en tête.")

    return jsonify({"windows": windows, "diagnosis": {"horizons": horizons, "context": context}})


@app.route("/api/cro/products")
@login_required
@bq_cache()
def api_cro_products():
    d = max(1, min(int(request.args.get("days", 3)), 14))
    end = date.today() - timedelta(days=1)
    cs, ce = (end - timedelta(days=d - 1)).isoformat(), end.isoformat()
    ps, pe = (end - timedelta(days=2 * d - 1)).isoformat(), (end - timedelta(days=d)).isoformat()
    rows = q(f"""SELECT i.item_name,
                   SUM(IF(i.date BETWEEN @cs AND @ce, i.purchases, 0)) cur_pur,
                   SUM(IF(i.date BETWEEN @ps AND @pe, i.purchases, 0)) prev_pur,
                   SUM(IF(i.date BETWEEN @cs AND @ce, i.add_to_carts, 0)) cur_atc,
                   SUM(IF(i.date BETWEEN @ps AND @pe, i.add_to_carts, 0)) prev_atc,
                   SUM(IF(i.date BETWEEN @cs AND @ce, i.revenue, 0)) cur_rev
                 FROM {T('ga4_items_daily')} i
                 JOIN (SELECT DISTINCT product_title FROM {T('shopify_inventory')}
                       WHERE product_type IN UNNEST(@types)) s
                   ON LOWER(TRIM(i.item_name)) = LOWER(TRIM(s.product_title))
                 WHERE i.date BETWEEN @ps AND @ce
                 GROUP BY i.item_name""",
             [bigquery.ScalarQueryParameter("cs", "DATE", cs), bigquery.ScalarQueryParameter("ce", "DATE", ce),
              bigquery.ScalarQueryParameter("ps", "DATE", ps), bigquery.ScalarQueryParameter("pe", "DATE", pe),
              bigquery.ArrayQueryParameter("types", "STRING", CRO_PRODUCT_TYPES)])
    for r in rows:
        dp = _pct(r["cur_pur"], r["prev_pur"])
        r["delta_pct"] = dp
        r["flag"] = ("red" if dp is not None and dp <= -50
                     else "amber" if dp is not None and dp <= -20
                     else "green" if dp is not None and dp >= 50
                     else "lime" if dp is not None and dp >= 20
                     else "")
    top = sorted([r for r in rows if r["cur_pur"] > 0 or r["prev_pur"] > 0],
                 key=lambda r: r["cur_pur"], reverse=True)[:20]
    entrants = sorted([r for r in rows if r["cur_pur"] > 0 and r["prev_pur"] == 0],
                      key=lambda r: r["cur_pur"], reverse=True)[:10]
    tot_cur = sum(r["cur_pur"] for r in rows) or 0
    tot_prev = sum(r["prev_pur"] for r in rows) or 0
    top3_cur = sum(sorted((r["cur_pur"] for r in rows), reverse=True)[:3])
    top3_prev = sum(sorted((r["prev_pur"] for r in rows), reverse=True)[:3])
    allp = sorted([r for r in rows if r["cur_pur"] > 0 or r["prev_pur"] > 0 or r["cur_atc"] > 0],
                  key=lambda r: r["cur_pur"], reverse=True)[:500]
    return jsonify({"days": d, "top": top, "products": allp, "entrants": entrants,
                    "concentration": {"top3_share_cur": (top3_cur / tot_cur) if tot_cur else None,
                                      "top3_share_prev": (top3_prev / tot_prev) if tot_prev else None}})


@app.route("/api/cro/channels")
@login_required
@bq_cache()
def api_cro_channels():
    d = max(1, min(int(request.args.get("days", 7)), 30))
    end = date.today() - timedelta(days=1)
    cs, ce = (end - timedelta(days=d - 1)).isoformat(), end.isoformat()
    ps, pe = (end - timedelta(days=2 * d - 1)).isoformat(), (end - timedelta(days=d)).isoformat()
    rows = q(f"""SELECT channel,
                   SUM(IF(date BETWEEN @cs AND @ce, sessions, 0)) cur_s,
                   SUM(IF(date BETWEEN @ps AND @pe, sessions, 0)) prev_s,
                   SUM(IF(date BETWEEN @cs AND @ce, purchases, 0)) cur_pu,
                   SUM(IF(date BETWEEN @cs AND @ce, revenue, 0)) cur_rev
                 FROM {T('ga4_channels_daily')} WHERE date BETWEEN @ps AND @ce
                 GROUP BY channel HAVING cur_s > 0 OR prev_s > 0""",
             [bigquery.ScalarQueryParameter("cs", "DATE", cs), bigquery.ScalarQueryParameter("ce", "DATE", ce),
              bigquery.ScalarQueryParameter("ps", "DATE", ps), bigquery.ScalarQueryParameter("pe", "DATE", pe)])
    tot = sum(r["cur_s"] for r in rows) or 1
    for r in rows:
        r["cvr"] = (r["cur_pu"] / r["cur_s"]) if r["cur_s"] else None
        r["share"] = r["cur_s"] / tot
        r["sessions_cmp"] = _pct(r["cur_s"], r["prev_s"])
    rows.sort(key=lambda r: r["cur_s"], reverse=True)

    # Série quotidienne pour le graphe de tendance, top 5 canaux (fenêtre paramétrable).
    span = max(7, min(int(request.args.get("chart_days", 28)), 120))
    sd = end - timedelta(days=span - 1)
    sr = q(f"""SELECT date, channel, SUM(sessions) s FROM {T('ga4_channels_daily')}
               WHERE date BETWEEN @sd AND @e GROUP BY date, channel""",
           [bigquery.ScalarQueryParameter("sd", "DATE", sd.isoformat()),
            bigquery.ScalarQueryParameter("e", "DATE", end.isoformat())])
    dates = [sd + timedelta(days=i) for i in range((end - sd).days + 1)]
    totals, bykey = {}, {}
    for r in sr:
        totals[r["channel"]] = totals.get(r["channel"], 0) + (r["s"] or 0)
        bykey[(r["date"], r["channel"])] = r["s"] or 0
    top5 = [c for c, _ in sorted(totals.items(), key=lambda kv: kv[1], reverse=True)[:5]]
    series = {"dates": [dt.isoformat() for dt in dates], "span_days": span,
              "channels": [{"name": c, "sessions": [bykey.get((dt, c), 0) for dt in dates]} for c in top5],
              "provisional_days": 2}
    return jsonify({"days": d, "channels": rows, "series": series})


@app.route("/api/cro/availability")
@login_required
@bq_cache()
def api_cro_availability():
    end = date.today() - timedelta(days=1)
    min_sales = max(1, min(int(request.args.get("min_sales", 5)), 100))
    try:
        oos, active_oos = _oos_sellers(end, min_sales)
        return jsonify({"ok": True, "min_sales": min_sales,
                        "out_of_stock_sellers": oos[:20], "active_oos_total": active_oos})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 200


# ---- API : clients new vs returning (customer-level, cohorte, par grain) ----
@app.route("/api/customers")
@login_required
@bq_cache()
def api_customers():
    scope = request.args.get("scope", "global")
    if scope not in ("web", "global"):
        scope = "global"
    try:
        out = {}
        for grain, lim in [("day", 21), ("week", 10), ("month", 13)]:
            # On s'arrête à hier : le jour en cours (partiel) n'est pas affiché.
            cutoff = "AND period_start < CURRENT_DATE()" if grain == "day" else ""
            rows = q(f"""SELECT period, new_customers, returning_customers, new_brand
                         FROM {T('customers_period')}
                         WHERE scope = @s AND grain = @g {cutoff}
                         ORDER BY period_start DESC LIMIT @n""",
                     [bigquery.ScalarQueryParameter("s", "STRING", scope),
                      bigquery.ScalarQueryParameter("g", "STRING", grain),
                      bigquery.ScalarQueryParameter("n", "INT64", lim)])
            for r in rows:
                tot = (r["new_customers"] or 0) + (r["returning_customers"] or 0)
                r["pct_new"] = (r["new_customers"] / tot) if tot else None
            out[grain] = list(reversed(rows))   # ordre chronologique
        return jsonify({"ok": True, "scope": scope, **out})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 200


# ---- API : acquisition & valeur (CAC brut + ROPO) ----
@app.route("/api/acquisition")
@login_required
@bq_cache()
def api_acquisition():
    try:
        out = {}
        for grain, lim in [("day", 21), ("week", 10), ("month", 13)]:
            cutoff = "AND period_start < CURRENT_DATE()" if grain == "day" else ""
            rows = q(f"""SELECT period, ad_spend, new_web, cac FROM {T('acquisition_period')}
                         WHERE grain = @g {cutoff} ORDER BY period_start DESC LIMIT @n""",
                     [bigquery.ScalarQueryParameter("g", "STRING", grain),
                      bigquery.ScalarQueryParameter("n", "INT64", lim)])
            out[grain] = list(reversed(rows))
        ropo = q(f"""SELECT period, web_to_store, store_to_web, total_web, total_store FROM {T('ropo_month')}
                     WHERE period_start < DATE_TRUNC(CURRENT_DATE(), MONTH)
                     ORDER BY period_start DESC LIMIT 6""")
        out["ropo"] = list(reversed(ropo))
        return jsonify({"ok": True, **out})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 200


# ---- API : alertes Meta (live, niveau adset & ad) ----
def _meta_insights(level, since, until, extra_fields="", time_increment=None):
    if not META_TOKEN:
        raise RuntimeError("META_ACCESS_TOKEN absent")
    url = f"https://graph.facebook.com/{META_API}/act_{META_ACCOUNT}/insights"
    params = {
        "access_token": META_TOKEN, "level": level,
        "time_range": f'{{"since":"{since}","until":"{until}"}}',
        "fields": f"adset_id,adset_name,ad_id,ad_name,spend,impressions,actions,action_values{extra_fields}",
        "limit": 500,
    }
    if time_increment:
        params["time_increment"] = time_increment
    out = []
    while url:
        r = requests.get(url, params=params, timeout=60).json()
        if "error" in r:
            raise RuntimeError(r["error"].get("message", "Meta API error"))
        out.extend(r.get("data", []))
        url = r.get("paging", {}).get("next")
        params = None
    return out

def _active_ids(node):
    """IDs des entités réellement actives. Pour une ad, effective_status==ACTIVE
    implique que son adset ET sa campagne sont actifs (sinon ADSET_PAUSED /
    CAMPAIGN_PAUSED). Filtrer sur ACTIVE gère donc toute la chaîne parente."""
    url = f"https://graph.facebook.com/{META_API}/act_{META_ACCOUNT}/{node}"
    # Filtre côté API : on ne ramène QUE les entités actives (payload réduit, plus rapide).
    params = {"access_token": META_TOKEN, "fields": "id", "limit": 500,
              "filtering": '[{"field":"effective_status","operator":"IN","value":["ACTIVE"]}]'}
    active = set()
    while url:
        r = requests.get(url, params=params if url.endswith(node) else None, timeout=60).json()
        if "error" in r:
            raise RuntimeError(r["error"].get("message", "Meta API error"))
        for x in r.get("data", []):
            active.add(x["id"])
        url = r.get("paging", {}).get("next")
        params = None
    return active


def _av(items, t):
    for it in (items or []):
        if it.get("action_type") == t:
            return float(it["value"])
    return 0.0

def _roas(row):
    spend = float(row.get("spend", 0) or 0)
    val = _av(row.get("action_values"), "omni_purchase")
    return (val / spend) if spend else 0, spend


_ALERTS_CACHE: dict = {}   # par fenêtre : {win: {"ts":…, "data":…}}

def _bsig(bud, spend_of, end, cbo=False):
    """Signal budget générique (marche pour un adset ABO ou une campagne CBO).
    `bud` = {date_iso: budget}, `spend_of(date_iso)` -> dépense de ce niveau ce jour-là.

    Affichage immédiat : si la date exacte manque, on retombe sur le snapshot le plus récent
    (= budget courant). Les ALERTES (changé hier / over-under qui dure) exigent, elles, de
    l'historique réel pour ne pas inventer de signal."""
    snaps = sorted(bud)
    latest = bud[snaps[-1]] if snaps else None

    def bud_for(d):
        if d in bud:
            return bud[d]
        prior = [s for s in snaps if s <= d]
        return bud[prior[-1]] if prior else latest

    pre = "CBO · " if cbo else ""
    d0, d1 = end.isoformat(), (end - timedelta(days=1)).isoformat()
    bn = bud_for(d0)
    e0, e1 = bud.get(d0), bud.get(d1)   # changement : snapshots EXACTS uniquement
    changed = e0 is not None and e1 is not None and abs(e0 - e1) > 0.01
    chg = ((e0 - e1) / e1 * 100) if (changed and e1) else None
    last3 = [(end - timedelta(days=i)).isoformat() for i in range(3)]
    utils = [spend_of(dd) / bud_for(dd) for dd in last3 if bud_for(dd)]
    util3 = round(sum(utils) / len(utils) * 100) if utils else None
    real3 = [bud[dd] for dd in last3 if dd in bud]   # over/under : 3 snapshots RÉELS
    flat = len(real3) >= 3 and (max(real3) - min(real3) <= 0.01)
    status, note = None, None
    if changed:
        note = pre + f"budget {'+' if chg >= 0 else ''}{chg:.0f}% hier"
    elif flat and len(utils) >= 3:
        if all(u >= 0.90 for u in utils):
            status, note = "overspend", pre + "budget saturé ≥3 j (sans changement)"
        elif all(u <= 0.60 for u in utils):
            status, note = "underspend", pre + "sous-délivré ≥3 j (sans changement)"
    elif cbo and bn is not None:
        note = "CBO · budget campagne"
    return {"budget_now": (round(bn, 2) if bn is not None else None),
            "changed": changed, "chg_pct": (round(chg, 1) if chg is not None else None),
            "util_3d": util3, "status": status, "note": note, "cbo": cbo}


@app.route("/api/meta/alerts")
@login_required
def api_meta_alerts():
    # Cache 15 min par fenêtre (1/3/7 j) — sauf ?force=1.
    win = int(request.args.get("window", 3))
    if win not in (1, 3, 7):
        win = 3
    force = request.args.get("force")
    c = _ALERTS_CACHE.get(win)
    if not force and c and time.time() - c["ts"] < 900:
        return jsonify(c["data"])
    try:
        F = ROI_FLOOR
        gate = max(5.0, round(20.0 * win / 3.0))   # seuil de dépense proportionnel à la fenêtre (20 €/3 j)
        end = date.today() - timedelta(days=1)
        since = (end - timedelta(days=13)).isoformat()   # 14 j : fenêtre courante + précédente jusqu'à 7 j
        end_s = end.isoformat()

        with ThreadPoolExecutor(max_workers=4) as ex:
            f_ads_act = ex.submit(_active_ids, "ads")
            f_adset_act = ex.submit(_active_ids, "adsets")
            f_ads = ex.submit(_meta_insights, "ad", since, end_s, "", 1)
            f_adset_daily = ex.submit(_meta_insights, "adset", since, end_s, ",campaign_id,campaign_name", 1)
            active_ads = f_ads_act.result(); active_adsets = f_adset_act.result()
            ad_daily = f_ads.result(); adset_daily = f_adset_daily.result()

        # ---- Ads : fenêtre courante vs précédente (même longueur), regroupées par adset. ----
        adagg = {}
        for x in ad_daily:
            aid = x.get("ad_id")
            if not aid:
                continue
            e = adagg.setdefault(aid, {"ad": x.get("ad_name"), "adset_id": x.get("adset_id"),
                                       "adset": x.get("adset_name"), "days": {}})
            e["days"][x.get("date_start")] = (float(x.get("spend", 0) or 0),
                                              _av(x.get("action_values"), "omni_purchase"))
        ad_dates = sorted({d for e in adagg.values() for d in e["days"]})
        cur_w, prev_w = ad_dates[-win:], ad_dates[-2 * win:-win]

        def _sv(days, ds):
            return (sum(days.get(d, (0, 0))[0] for d in ds), sum(days.get(d, (0, 0))[1] for d in ds))

        ads_by_adset, top_ads, ads_to_cut, starved_ads, revived_ads, ignored_ads = {}, [], [], [], [], 0
        for aid, e in adagg.items():
            if aid not in active_ads or e["adset_id"] not in active_adsets:
                continue
            cs, cv = _sv(e["days"], cur_w)
            ps, pv = _sv(e["days"], prev_w)
            if cs < gate:
                ignored_ads += 1
                continue
            cr = cv / cs if cs else 0
            prr = pv / ps if ps else 0
            rec = {"ad": e["ad"], "adset": e["adset"], "spend": round(cs, 2), "value": cv,
                   "roas": round(cr, 2), "prev_spend": round(ps, 2), "prev_roas": round(prr, 2), "adset_note": ""}
            if cr >= F:
                rec["kind"] = "winner"
                top_ads.append({"ad": e["ad"], "adset": e["adset"], "spend": round(cs, 2), "roas": round(cr, 2)})
                if ps > 0 and cs >= ps * 1.5:   # regagne nettement du budget ET performe
                    revived_ads.append({"ad": e["ad"], "adset": e["adset"], "spend": round(cs, 2),
                                        "roas": round(cr, 2), "prev_spend": round(ps, 2), "prev_roas": round(prr, 2)})
            elif ps >= cs * 1.5 and prr >= F:
                # Performait quand il était alimenté (dépensait + et ROI OK) -> Meta ne le sert plus : PAS à couper.
                rec["kind"] = "starved"
                starved_ads.append({**rec,
                    "reco": "Performait quand il était alimenté → augmenter le budget de l'adset pour le ré-alimenter, "
                            "ou segmenter (sortir les ads récents dans un adset dédié) pour ne pas l'étouffer."})
            else:
                rec["kind"] = "cut"
                ads_to_cut.append(rec)
            ads_by_adset.setdefault(e["adset_id"], []).append(rec)

        # ---- Adsets : ROAS fenêtre courante vs précédente + budget (ABO, sinon repli CBO). ----
        byid, camp_of, camp_spend = {}, {}, {}
        for x in adset_daily:
            aid = x.get("adset_id")
            if not aid:
                continue
            e = byid.setdefault(aid, {"name": x.get("adset_name"), "days": {}})
            d = x.get("date_start")
            val = _av(x.get("action_values"), "omni_purchase"); sp = float(x.get("spend", 0) or 0)
            e["days"][d] = (val, sp)
            cid = x.get("campaign_id")
            if cid:
                camp_of[aid] = cid
                camp_spend.setdefault(cid, {})[d] = camp_spend.setdefault(cid, {}).get(d, 0) + sp
        all_dates = sorted({d for e in byid.values() for d in e["days"]})
        cur_a, prev_a = all_dates[-win:], all_dates[-2 * win:-win]

        def _load_bud(table, key):
            m = {}
            try:
                for r in q(f"""SELECT date, {key}, daily_budget FROM {T(table)}
                               WHERE date >= DATE_SUB(@e, INTERVAL 8 DAY) AND daily_budget IS NOT NULL""",
                           [bigquery.ScalarQueryParameter("e", "DATE", end.isoformat())]):
                    m.setdefault(r[key], {})[r["date"].isoformat()] = r["daily_budget"]
            except Exception:  # noqa: BLE001
                pass
            return m

        adset_bud = _load_bud("meta_adset_budget_daily", "adset_id")
        camp_bud = _load_bud("meta_campaign_budget_daily", "campaign_id")

        def wr(days, cur, pri):
            vr = sum(days.get(d, (0, 0))[0] for d in cur); sr = sum(days.get(d, (0, 0))[1] for d in cur)
            vp = sum(days.get(d, (0, 0))[0] for d in pri); sp = sum(days.get(d, (0, 0))[1] for d in pri)
            return (vr / sr if sr else 0), (vp / sp if sp else 0), sr, sp

        # ---- Moteur de décision : budgets adsets à moduler + ads à couper / affamés. ----
        adset_actions, rising = [], []
        for aid, e in byid.items():
            if aid not in active_adsets:
                continue
            rr, pr, sr, _ = wr(e["days"], cur_a, prev_a)
            abud = adset_bud.get(aid, {})
            if abud:
                sig = _bsig(abud, lambda d, ee=e: ee["days"].get(d, (0, 0))[1], end, cbo=False)
            else:
                cid = camp_of.get(aid)
                sig = _bsig(camp_bud.get(cid, {}), lambda d, c=camp_spend.get(cid, {}): c.get(d, 0), end, cbo=True)
            util = sig["util_3d"]; status = sig["status"]

            ads = ads_by_adset.get(aid, [])
            cut = [a for a in ads if a["kind"] == "cut"]         # vrais ads à couper (pas les affamés)
            winners = [a for a in ads if a["kind"] == "winner"]
            starved = [a for a in ads if a["kind"] == "starved"]
            vr = rr * sr
            s_excl = sr - sum(a["spend"] for a in cut)
            v_excl = vr - sum(a["value"] for a in cut)
            roas_excl = round(v_excl / s_excl, 2) if s_excl > 0.5 else None
            saturated = (status == "overspend") or (util is not None and util >= 90)
            under = (status == "underspend") or (util is not None and util <= 60)

            chg = ((rr - pr) / pr * 100) if pr > 0 else None
            if chg is not None and chg >= 20 and sr >= gate and rr >= F:
                rising.append({"adset": e["name"], "roas": round(rr, 2), "roas_prev": round(pr, 2),
                               "rise_pct": round(chg, 1), "budget": sig})

            action = reason = None
            if sr >= gate:
                if rr < F:
                    if cut and roas_excl is not None and roas_excl >= F:
                        action = "CUT_DRAINERS"
                        reason = (f"ROAS {rr:.2f} plombé par {len(cut)} ad(s) faible(s). "
                                  f"Sans eux ≈ {roas_excl:.2f} (≥ {F:.0f}) : couper, garder le budget.")
                    else:
                        action = "LOWER"
                        reason = f"ROAS {rr:.2f}, aucun ad fautif isolé → réduire le budget ~20 % ou mettre en pause."
                else:
                    if cut and roas_excl is not None and roas_excl > rr + 0.1:
                        action = "CUT_THEN_SCALE"
                        reason = (f"ROAS {rr:.2f} bridé par {len(cut)} ad(s) faible(s) (sans eux ≈ {roas_excl:.2f}) : "
                                  "couper, puis envisager de scaler.")
                    elif starved:
                        action = "FEED_STARVED"
                        reason = (f"ROAS {rr:.2f} OK mais {len(starved)} ad(s) qui performaient ne sont plus alimentés. "
                                  "Augmenter le budget de l'adset, ou segmenter pour les relancer.")
                    elif saturated and winners:
                        action = "SCALE"
                        reason = (f"ROAS {rr:.2f} ≥ {F:.0f} et budget saturé"
                                  f"{f' (util {util}%)' if util is not None else ''} → augmenter ~20 % / 3-4 j.")
                    elif under:
                        action = "REVIEW_UNDER"
                        reason = (f"ROAS {rr:.2f} bon mais sous-délivré"
                                  f"{f' (util {util}%)' if util is not None else ''} → budget trop haut ou ciblage trop étroit.")
            if action:
                adset_actions.append({
                    "adset": e["name"], "action": action, "reason": reason,
                    "roas": round(rr, 2), "roas_prev": round(pr, 2), "spend": round(sr, 2), "util_3d": util,
                    "budget_now": sig["budget_now"], "budget_note": sig["note"],
                    "drainers": [a["ad"] for a in cut][:4], "winners": [a["ad"] for a in winners][:4],
                    "starved": [a["ad"] for a in starved][:4]})

            for a in cut:   # impact d'un retrait, écrit sur l'objet partagé avec ads_to_cut
                a["adset_note"] = (f"l'adset repasse à ≈ {roas_excl:.2f} sans cet ad"
                                   if (roas_excl is not None and rr < F and winners)
                                   else f"draine le budget (ROAS adset {rr:.2f})")

        order = {"LOWER": 0, "CUT_DRAINERS": 1, "FEED_STARVED": 2, "CUT_THEN_SCALE": 3, "SCALE": 4, "REVIEW_UNDER": 5}
        adset_actions.sort(key=lambda x: (order.get(x["action"], 9), -x["spend"]))
        ads_to_cut.sort(key=lambda x: -x["spend"])
        starved_ads.sort(key=lambda x: -x["prev_spend"])
        revived_ads.sort(key=lambda x: -x["spend"])
        rising.sort(key=lambda x: -x["rise_pct"])
        top_ads.sort(key=lambda x: -x["roas"])
        result = {"ok": True, "roi_floor": ROI_FLOOR, "window": win, "gate": gate,
                  "adset_actions": adset_actions[:30], "ads_to_cut": ads_to_cut[:30],
                  "starved_ads": starved_ads[:20], "revived_ads": revived_ads[:20],
                  "rising_adsets": rising[:12], "top_ads": top_ads[:12], "ignored_ads": ignored_ads}
        _ALERTS_CACHE[win] = {"ts": time.time(), "data": result}
        return jsonify(result)
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 200


# ---- API : objectifs du mois (Google Sheet) + MTD + projection + reco ----
def _sheet_cell(a1: str) -> float:
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"])
    creds.refresh(GAuthRequest())
    rng = requests.utils.quote(a1, safe="")
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{BUDGET_SHEET_ID}/values/{rng}"
    r = requests.get(url, params={"valueRenderOption": "UNFORMATTED_VALUE"},
                     headers={"Authorization": f"Bearer {creds.token}"}, timeout=30)
    if r.status_code != 200:
        raise RuntimeError(f"Sheets API {r.status_code}: {r.text[:160]}")
    vals = r.json().get("values") or []
    if not vals or not vals[0]:
        raise RuntimeError(f"Cellule {a1} vide")
    return float(vals[0][0])


_TARGETS_CACHE: dict = {}   # (année, mois) -> (ts, ca, dépense) — le Sheet change rarement

def _month_targets(d: date):
    """(objectif CA, objectif dépense) du mois de `d`, lus dans le Sheet Budget (cache 1 h)."""
    key = (d.year, d.month)
    c = _TARGETS_CACHE.get(key)
    if c and time.time() - c[0] < 3600:
        return c[1], c[2]
    col = chr(ord("D") + (d.month - 1))   # D = janvier ... O = décembre
    ca = _sheet_cell(f"'{BUDGET_SHEET_TAB}'!{col}8")
    sp = _sheet_cell(f"'{BUDGET_SHEET_TAB}'!{col}15")
    _TARGETS_CACHE[key] = (time.time(), ca, sp)
    return ca, sp


@app.route("/api/targets")
@login_required
def api_targets():
    try:
        if not BUDGET_SHEET_ID:
            return jsonify({"ok": False, "error": "BUDGET_SHEET_ID non configuré"}), 200
        today = date.today()
        ca_target, spend_target = _month_targets(today)

        dim = calendar.monthrange(today.year, today.month)[1]
        elapsed = today.day - 1            # jours complets écoulés (hors aujourd'hui)
        left = dim - elapsed               # jours restants (aujourd'hui inclus)

        row = q(f"""SELECT COALESCE(SUM(ca_shopify),0) ca, COALESCE(SUM(ad_spend_total),0) spend
                    FROM {T('cockpit_daily')}
                    WHERE date >= DATE_TRUNC(CURRENT_DATE(), MONTH) AND date < CURRENT_DATE()""")[0]

        def block(target, mtd):
            return {
                "target": target, "mtd": mtd,
                "projected": (mtd / elapsed * dim) if elapsed else None,
                "reco_daily": ((target - mtd) / left) if left > 0 else None,
                "pace_pct": (mtd / (target * elapsed / dim)) if (target and elapsed) else None,
            }
        ca_mtd, spend_mtd = row["ca"], row["spend"]
        ca_rem, spend_rem = ca_target - ca_mtd, spend_target - spend_mtd
        cos = {
            "target": (spend_target / ca_target) if ca_target else None,
            "mtd": (spend_mtd / ca_mtd) if ca_mtd else None,
            # COS à tenir sur le CA restant pour finir le mois pile sur les 2 objectifs
            "needed_remaining": (spend_rem / ca_rem) if ca_rem > 0 else None,
        }
        return jsonify({"ok": True, "month": today.strftime("%Y-%m"),
                        "days": {"in_month": dim, "elapsed": elapsed, "left": left},
                        "ca": block(ca_target, ca_mtd),
                        "spend": block(spend_target, spend_mtd),
                        "cos": cos})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 200


# ---- API : santé / fraîcheur des données ----
@app.route("/api/health")
@login_required
def api_health():
    end = date.today() - timedelta(days=1)   # on attend des données jusqu'à hier

    # Une seule requête (UNION ALL) au lieu de 4 -> 4× moins d'allers-retours BigQuery.
    raw = q(f"""
        SELECT 'orders'   k, MIN(date) mn, MAX(date) mx, COUNT(DISTINCT date) nd FROM {T('shopify_orders_daily')}
        UNION ALL SELECT 'meta',     MIN(date), MAX(date), COUNT(DISTINCT date) FROM {T('meta_daily')}
        UNION ALL SELECT 'google',   MIN(date), MAX(date), COUNT(DISTINCT date) FROM {T('google_daily')}
        UNION ALL SELECT 'sessions', MIN(date), MAX(date), COUNT(DISTINCT date) FROM {T('shopify_traffic_daily')}
        UNION ALL SELECT 'customers', MIN(period_start), MAX(period_start), COUNT(DISTINCT period_start)
                  FROM {T('customers_period')} WHERE scope = 'web' AND grain = 'day'
    """)
    by = {r["k"]: r for r in raw}
    # tolerate = retard toléré (jours) ; check_gaps = signaler les trous d'historique.
    # Clients : pas de check_gaps (un jour sans commande = pas de ligne, ce n'est pas un trou).
    spec = [("orders", "CA / Commandes", 1, True), ("meta", "Dépense Meta", 1, True),
            ("google", "Dépense Google", 1, True), ("sessions", "Sessions", 2, True),
            ("customers", "Clients", 1, False)]

    def info(k, label, tolerate, check_gaps=True):
        r = by.get(k, {})
        mx = r.get("mx")
        if mx is None:
            return {"label": label, "last": None, "days_behind": None, "gaps": None, "status": "empty"}
        days_behind = max(0, (end - mx).days)
        gaps = (mx - r["mn"]).days + 1 - r["nd"] if check_gaps else 0
        status = "ok"
        if days_behind > tolerate or gaps > 0:
            status = "warn"
        if days_behind >= 7:
            status = "stale"
        return {"label": label, "last": mx.isoformat(), "days_behind": days_behind,
                "gaps": gaps, "status": status}

    # Sessions : on regarde le DERNIER jour avec une vraie valeur (>0). Si le script
    # ne s'est pas lancé pendant N jours, on l'indique explicitement (rouge + nb de jours).
    lr = q(f"SELECT MAX(date) m FROM {T('shopify_traffic_daily')} WHERE sessions > 0")[0]["m"]
    sess_behind = max(0, (end - lr).days) if lr else None

    sources = []
    for k, label, tol, cg in spec:
        s = info(k, label, tol, cg)
        if k == "sessions":
            if sess_behind is None or sess_behind >= 1:
                s["status"] = "stale"
                s["days_behind"] = sess_behind
                jours = "plusieurs" if sess_behind is None else str(sess_behind)
                last_txt = lr.isoformat() if lr else "—"
                s["note"] = (f"Sessions plus à jour depuis {jours} jour(s) (dernière donnée : {last_txt}) — "
                             f"le script qui alimente le Google Sheet ne s'est probablement pas lancé. "
                             f"Relance-le côté Mac, ou clique « Rafraîchir les sessions ».")
            else:
                s["days_behind"] = 0
        sources.append(s)
    overall = "ok"
    for s in sources:
        if s["status"] == "stale":
            overall = "stale"
        elif s["status"] in ("warn", "empty") and overall == "ok":
            overall = "warn"
    return jsonify({"sources": sources, "overall": overall, "as_of": end.isoformat()})


# ---- API : déclencher manuellement le rafraîchissement des sessions ----
@app.route("/api/refresh-sessions", methods=["POST"])
@login_required
def refresh_sessions():
    try:
        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        creds.refresh(GAuthRequest())
        url = f"https://run.googleapis.com/v2/projects/{BQ_PROJECT}/locations/{RUN_REGION}/jobs/{SESSIONS_JOB}:run"
        r = requests.post(url, headers={"Authorization": f"Bearer {creds.token}"}, timeout=30)
        ok = r.status_code in (200, 201)
        return jsonify({"ok": ok, "status": r.status_code,
                        "detail": "" if ok else r.text[:200]}), 200
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)), debug=True)
