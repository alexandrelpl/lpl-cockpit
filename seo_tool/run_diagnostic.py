"""
Point d'entrée du diagnostic SEO (read-only).

Usage :
  python -m seo_tool.run_diagnostic                 # crawl complet + dashboard
  python -m seo_tool.run_diagnostic --no-translations   # plus rapide (saute les traductions)

Sorties dans seo_tool/ : seo_issues.json + seo_dashboard.html
"""

from __future__ import annotations
import sys

from seo_tool import config, crawl, report


def main(argv=None):
    argv = argv if argv is not None else sys.argv[1:]
    include_tr = "--no-translations" not in argv

    missing = config.check_env()
    if missing:
        print("ERREUR — variables manquantes : " + ", ".join(missing))
        print("Exporte au minimum SHOPIFY_SHOP_URL et SHOPIFY_ADMIN_TOKEN (token LECTURE dédié).")
        return 1

    print(f"Diagnostic SEO · boutique {config.SHOP_URL} · API {config.API_VERSION}")
    snap = crawl.run(include_translations=include_tr)

    pj = report.write_json(snap)
    pd = report.write_dashboard(snap)

    s = snap["scores"]
    print("\n=== SCORES ===")
    gv = s["global"]
    print(f"Global : {gv if gv is not None else '—'}/100")
    for k, c in s["categories"].items():
        sc = f"{c['score']:>5}/100" if c["score"] is not None else "non évalué"
        print(f"  {c['label']:<20} {sc:>11}   {c['to_fix']:>5} à corriger "
              f"({c['affected']}/{c['total']} objets)")
    print(f"\nTotal issues : {s['total_issues']}")
    print(f"→ {pj}")
    print(f"→ {pd}  (ouvre-le dans un navigateur)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
