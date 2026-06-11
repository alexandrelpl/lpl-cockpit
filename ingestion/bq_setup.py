"""Crée le dataset BigQuery et toutes les tables/vue à partir de bigquery/schema.sql."""
from __future__ import annotations
import os
import pathlib

from google.cloud import bigquery

BQ_PROJECT = os.environ["BQ_PROJECT"]
BQ_DATASET = os.environ.get("BQ_DATASET", "lpl_cockpit")
LOCATION   = os.environ.get("BQ_LOCATION", "EU")

SCHEMA = pathlib.Path(__file__).resolve().parent.parent / "bigquery" / "schema.sql"


def run() -> None:
    client = bigquery.Client(project=BQ_PROJECT)
    ds_ref = bigquery.Dataset(f"{BQ_PROJECT}.{BQ_DATASET}")
    ds_ref.location = LOCATION
    client.create_dataset(ds_ref, exists_ok=True)
    print(f"[bq] dataset {BQ_DATASET} ok ({LOCATION})")

    sql = SCHEMA.read_text(encoding="utf-8")
    # le schéma référence `lpl_cockpit.<table>` -> on qualifie avec le projet courant
    sql = sql.replace("`lpl_cockpit.", f"`{BQ_PROJECT}.{BQ_DATASET}.")
    # retirer TOUS les commentaires (-- jusqu'à la fin de ligne) AVANT de découper
    # sur ';', sinon un ';' présent dans un commentaire casse le découpage.
    no_comments = "\n".join(line.split("--", 1)[0] for line in sql.splitlines())
    statements = [s.strip() for s in no_comments.split(";") if s.strip()]
    for stmt in statements:
        client.query(stmt, location=LOCATION).result()
    print(f"[bq] {len(statements)} objets créés (tables + vue cockpit_daily)")


if __name__ == "__main__":
    run()
