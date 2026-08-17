"""
Webapp minimale (volet 1) : sert le dashboard de diagnostic sur Cloud Run.

Routes :
  GET  /                -> dashboard si un snapshot existe, sinon bouton « Lancer »
  POST /run             -> lance un crawl synchrone (~1-2 min) puis redirige vers /
  GET  /api/issues.json -> snapshot brut (consommé par le volet 2)
  GET  /healthz         -> health check

Auth : à ce stade aucune (read-only). En prod, déployer derrière un contrôle d'accès
(Cloud Run --no-allow-unauthenticated + IAP, ou réutiliser l'OAuth du cockpit).
"""

from __future__ import annotations
import json
import os

from flask import Flask, Response, redirect

from seo_tool import config, crawl, report

app = Flask(__name__)
SNAP_PATH = os.path.join(config.OUTPUT_DIR, "seo_issues.json")
_state = {"snapshot": None, "error": None}


def _load():
    if _state["snapshot"] is None and os.path.exists(SNAP_PATH):
        try:
            _state["snapshot"] = json.load(open(SNAP_PATH, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass


_load()


def _page(title, body):
    return (f"<!DOCTYPE html><html lang=fr><head><meta charset=utf-8>"
            f"<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{title}</title><style>body{{font-family:-apple-system,sans-serif;"
            f"max-width:640px;margin:60px auto;padding:0 20px;color:#1d2126}}"
            f"button{{font-size:1rem;font-weight:600;color:#fff;background:#2E8FA6;border:0;"
            f"border-radius:9px;padding:12px 22px;cursor:pointer}}</style></head>"
            f"<body><h1>{title}</h1>{body}</body></html>")


@app.route("/")
def index():
    if _state["snapshot"]:
        return Response(report.render_dashboard_html(_state["snapshot"]), mimetype="text/html")
    err = (f"<p style='color:#c2415e'>Dernière erreur : {_state['error']}</p>"
           if _state["error"] else "")
    return _page("Diagnostic SEO — Le Petit Lunetier",
                 f"{err}<p>Aucun diagnostic en mémoire. Lance un crawl (≈ 1-2 min).</p>"
                 f"<form method=post action=/run><button>Lancer le diagnostic</button></form>")


@app.route("/run", methods=["POST", "GET"])
def run():
    try:
        snap = crawl.run(include_translations=True, log=lambda *a: None)
        report.write_json(snap)
        _state["snapshot"] = snap
        _state["error"] = None
    except Exception as e:  # noqa: BLE001
        _state["error"] = str(e)
    return redirect("/")


@app.route("/api/issues.json")
def api_issues():
    return Response(json.dumps(_state["snapshot"] or {}, ensure_ascii=False),
                    mimetype="application/json")


@app.route("/healthz")
def healthz():
    return "ok", 200
