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
    for stmt in [s.strip() for s in sql.split(";") if s.strip() and not s.strip().startswith("--")]:
        client.query(stmt).result()
    print("[bq] tables + vue cockpit_daily créées")


if __name__ == "__main__":
    run()
