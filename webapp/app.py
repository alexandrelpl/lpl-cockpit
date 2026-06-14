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
META_TOKEN     = os.environ.get("META_ACCESS_TOKEN", "")
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


_ALERTS_CACHE: dict = {"ts": 0.0, "data": None}

@app.route("/api/meta/alerts")
@login_required
def api_meta_alerts():
    # Cache 15 min (les alertes ne bougent pas à la minute, l'API Meta est lente) — sauf ?force=1.
    force = request.args.get("force")
    if not force and _ALERTS_CACHE["data"] and time.time() - _ALERTS_CACHE["ts"] < 900:
        return jsonify(_ALERTS_CACHE["data"])
    try:
        end = date.today() - timedelta(days=1)
        since = (end - timedelta(days=6)).isoformat()
        end_s = end.isoformat()

        # 4 appels Meta indépendants -> en parallèle (au lieu de 6 en série).
        with ThreadPoolExecutor(max_workers=4) as ex:
            f_ads_act = ex.submit(_active_ids, "ads")
            f_adset_act = ex.submit(_active_ids, "adsets")
            f_ads = ex.submit(_meta_insights, "ad", since, end_s)
            f_adset_daily = ex.submit(_meta_insights, "adset", since, end_s, "", 1)  # quotidien
            active_ads = f_ads_act.result(); active_adsets = f_adset_act.result()
            ads = f_ads.result(); adset_daily = f_adset_daily.result()

        # Ads actives : sous ROI plancher, ou actives mais sous-dépensières.
        # Double garde-fou : l'ad ET son adset doivent être actifs. (effective_status==ACTIVE
        # d'une ad implique déjà adset+campagne actifs ; on re-vérifie l'adset par sécurité.)
        low_roi, under_spend = [], []
        for a in ads:
            if a.get("ad_id") not in active_ads or a.get("adset_id") not in active_adsets:
                continue
            roas, spend = _roas(a)
            impr = int(a.get("impressions", 0) or 0)
            if spend > 0 and roas < ROI_FLOOR:
                low_roi.append({"ad": a.get("ad_name"), "adset": a.get("adset_name"),
                                "spend": round(spend, 2), "roas": round(roas, 2)})
            if impr > 0 and spend < 30:
                under_spend.append({"ad": a.get("ad_name"), "adset": a.get("adset_name"),
                                    "spend": round(spend, 2), "impressions": impr})

        # Adsets : ROAS 3 derniers jours vs 3 précédents, depuis l'UNIQUE appel quotidien.
        byid = {}
        for x in adset_daily:
            aid = x.get("adset_id")
            if not aid:
                continue
            e = byid.setdefault(aid, {"name": x.get("adset_name"), "days": {}})
            e["days"][x.get("date_start")] = (_av(x.get("action_values"), "omni_purchase"),
                                              float(x.get("spend", 0) or 0))
        all_dates = sorted({d for e in byid.values() for d in e["days"]})
        recent_d, prior_d = all_dates[-3:], all_dates[-6:-3]
        declining = []
        for aid, e in byid.items():
            if aid not in active_adsets:
                continue
            vr = sum(e["days"].get(d, (0, 0))[0] for d in recent_d)
            sr = sum(e["days"].get(d, (0, 0))[1] for d in recent_d)
            vp = sum(e["days"].get(d, (0, 0))[0] for d in prior_d)
            sp = sum(e["days"].get(d, (0, 0))[1] for d in prior_d)
            rr = (vr / sr) if sr else 0
            pr = (vp / sp) if sp else 0
            if pr > 0 and rr < pr:
                drop = (rr - pr) / pr * 100
                if drop <= -20:
                    declining.append({"adset": e["name"], "roas_3d": round(rr, 2),
                                      "roas_prev": round(pr, 2), "drop_pct": round(drop, 1)})
        declining.sort(key=lambda x: x["drop_pct"])
        low_roi.sort(key=lambda x: x["spend"], reverse=True)
        result = {"ok": True, "roi_floor": ROI_FLOOR,
                  "low_roi_ads": low_roi[:25], "under_spend_ads": under_spend[:25],
                  "declining_adsets": declining[:25]}
        _ALERTS_CACHE["ts"], _ALERTS_CACHE["data"] = time.time(), result
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
    """)
    by = {r["k"]: r for r in raw}
    spec = [("orders", "CA / Commandes", 1), ("meta", "Dépense Meta", 1),
            ("google", "Dépense Google", 1), ("sessions", "Sessions", 2)]

    def info(k, label, tolerate):
        r = by.get(k, {})
        mx = r.get("mx")
        if mx is None:
            return {"label": label, "last": None, "days_behind": None, "gaps": None, "status": "empty"}
        days_behind = max(0, (end - mx).days)
        gaps = (mx - r["mn"]).days + 1 - r["nd"]
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
    for k, label, tol in spec:
        s = info(k, label, tol)
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
