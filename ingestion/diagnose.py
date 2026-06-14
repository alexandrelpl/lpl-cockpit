"""
Diagnostic systématique du socle de données LPL Cockpit.

But : en cas de doute sur la santé des données, lancer `python -m ingestion.diagnose`
et obtenir un rapport structuré (fraîcheur, trous, cohérence inter-sources, état des
jobs/planificateurs) pour orienter le correctif sans tâtonner.

Dépendances : ADC (BigQuery) ; `gcloud` pour l'état des jobs/scheduler (optionnel).
  export BQ_PROJECT=shopify-data-ltv BQ_DATASET=lpl_cockpit BQ_LOCATION=EU
  python -m ingestion.diagnose
"""

from __future__ import annotations
import os
import subprocess
from datetime import date, timedelta

from google.cloud import bigquery

PROJECT = os.environ.get("BQ_PROJECT", "shopify-data-ltv")
DATASET = os.environ.get("BQ_DATASET", "lpl_cockpit")
LOCATION = os.environ.get("BQ_LOCATION", "EU")
REGION = os.environ.get("CLOUD_RUN_REGION", "europe-west1")
_cli = bigquery.Client(project=PROJECT)


def _q(sql):
    return [dict(r) for r in _cli.query(sql, location=LOCATION).result()]


def T(t):
    return f"`{PROJECT}.{DATASET}.{t}`"


def line(c="-"):
    print(c * 72)


def section(title):
    line("=")
    print(title)
    line("=")


def source_health():
    section("1. FRAÎCHEUR & TROUS PAR SOURCE")
    end = date.today() - timedelta(days=1)
    tables = [
        ("shopify_orders_daily", "CA / Commandes", 1),
        ("meta_daily", "Dépense Meta", 1),
        ("google_daily", "Dépense Google", 1),
        ("shopify_traffic_daily", "Sessions", 2),
    ]
    for tbl, label, tol in tables:
        r = _q(f"SELECT MIN(date) mn, MAX(date) mx, COUNT(DISTINCT date) nd FROM {T(tbl)}")[0]
        if r["mx"] is None:
            print(f"  ❌ {label:18} : VIDE")
            continue
        behind = max(0, (end - r["mx"]).days)
        gaps = (r["mx"] - r["mn"]).days + 1 - r["nd"]
        flag = "✅" if (behind <= tol and gaps == 0) else ("🟠" if behind < 7 else "🔴")
        print(f"  {flag} {label:18} : {r['mn']} → {r['mx']}  ({r['nd']} j, retard {behind} j, {gaps} trou(s))")


def gaps_detail():
    section("2. TROUS RÉCENTS (90 derniers jours) — recouvrement & oublis")
    for tbl, col in [("meta_daily", None), ("google_daily", None), ("shopify_traffic_daily", None)]:
        rows = _q(f"""
            WITH d AS (
              SELECT day FROM UNNEST(GENERATE_DATE_ARRAY(DATE_SUB(CURRENT_DATE(), INTERVAL 90 DAY),
                                                         DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY))) day)
            SELECT day FROM d
            LEFT JOIN (SELECT DISTINCT date FROM {T(tbl)}) s ON s.date = d.day
            WHERE s.date IS NULL ORDER BY day""")
        missing = [r["day"].isoformat() for r in rows]
        if missing:
            print(f"  🟠 {tbl}: {len(missing)} jours manquants → {missing[:8]}{' …' if len(missing) > 8 else ''}")
        else:
            print(f"  ✅ {tbl}: aucun trou sur 90 j")


def cross_checks():
    section("3. COHÉRENCE INTER-SOURCES (60 derniers jours)")
    # jours avec du CA mais une dépense/sessions à zéro/absente -> source en retard ou trou
    rows = _q(f"""
      SELECT
        COUNTIF(ca_shopify > 0 AND meta_spend = 0)  AS ca_sans_meta,
        COUNTIF(ca_shopify > 0 AND google_spend = 0) AS ca_sans_google,
        COUNTIF(ca_shopify > 0 AND sessions IS NULL) AS ca_sans_sessions
      FROM {T('cockpit_daily')}
      WHERE date BETWEEN DATE_SUB(CURRENT_DATE(), INTERVAL 60 DAY) AND DATE_SUB(CURRENT_DATE(), INTERVAL 1 DAY)
    """)[0]
    for k, v in rows.items():
        flag = "✅" if v == 0 else "🟠"
        print(f"  {flag} {k.replace('_',' ')}: {v} jour(s)")
    print("  (un nombre > 0 sur 'ca_sans_meta' = Meta en retard/trou ; idem Google/sessions)")


def recent_dump():
    section("4. DERNIERS JOURS (cockpit_daily)")
    rows = _q(f"""SELECT date, ROUND(ca_shopify) ca, ROUND(meta_spend) meta, ROUND(google_spend) google,
                         ROUND(cos_blended,3) cos, sessions
                  FROM {T('cockpit_daily')}
                  WHERE date >= DATE_SUB(CURRENT_DATE(), INTERVAL 8 DAY) ORDER BY date DESC""")
    print(f"  {'date':12}{'ca':>8}{'meta':>8}{'google':>8}{'cos':>7}{'sessions':>10}")
    for r in rows:
        print(f"  {str(r['date']):12}{r['ca'] or 0:>8.0f}{r['meta'] or 0:>8.0f}"
              f"{r['google'] or 0:>8.0f}{(r['cos'] or 0):>7.2f}{(r['sessions'] or 0):>10}")


def jobs_state():
    section("5. JOBS & PLANIFICATEURS (gcloud)")
    def run(cmd):
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=30).stdout.strip()
        except Exception as e:  # noqa: BLE001
            return f"(gcloud indisponible: {e})"
    for job in ["lpl-cockpit-job", "lpl-cockpit-sessions"]:
        out = run(["gcloud", "run", "jobs", "executions", "list", "--job", job,
                   "--region", REGION, "--limit", "1",
                   "--format=value(name,lastAttemptResult.status,completionTime)"])
        print(f"  • {job}: {out or '(aucune exécution)'}")
    print("  Planificateurs :")
    print(run(["gcloud", "scheduler", "jobs", "list", "--location", REGION,
               "--format=table(name.basename(),schedule,state)"]) or "  (aucun)")


def verdict():
    section("6. PISTES DE CORRECTION (selon les drapeaux ci-dessus)")
    print("""  • Meta en retard (🔴/🟠) -> jeton long-lived expiré (~60 j). Régénérer + mettre à jour
    le secret META_ACCESS_TOKEN, puis relancer le job. (cf. DIAGNOSTICS.md §Meta)
  • Sessions en retard -> le scraper Mac n'a pas alimenté le Sheet (Mac éteint / vacances).
    Le job ne peut pas inventer la donnée ; relancer le scraper. Bouton « Rafraîchir » = relit le Sheet.
  • Google en retard -> le script Google Ads n'a pas écrit le Sheet de coûts.
  • CA en retard / trous -> le job de nuit a échoué : voir les logs du job (DIAGNOSTICS.md §Job).
  • Trou d'historique (recouvrement) -> relancer le backfill de la source concernée.
  Détail des commandes : voir DIAGNOSTICS.md.""")


def main():
    print(f"\nDIAGNOSTIC LPL COCKPIT — projet {PROJECT} / dataset {DATASET}\n")
    for fn in (source_health, gaps_detail, cross_checks, recent_dump, jobs_state, verdict):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            print(f"  ⚠️ étape interrompue: {e}")
        print()


if __name__ == "__main__":
    main()
