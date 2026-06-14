"""
Écriture BigQuery par LOAD JOBS (et non par streaming insert).

Pourquoi : les insertions en flux (insert_rows_json) placent les lignes dans un
« streaming buffer » verrouillé ~90 min, pendant lequel tout DELETE/UPDATE/MERGE
sur ces lignes échoue. Les load jobs écrivent directement dans le stockage géré,
sans ce verrou -> les rafraîchissements par fenêtre (DELETE + INSERT) sont fiables.

Fonctions :
- load_replace_window : remplace [since, until] (staging chargé par load job,
  puis DELETE de la fenêtre + INSERT depuis le staging).
- flush_default : réécrit les tables historiques pour vider un buffer résiduel
  laissé par une ancienne version (one-shot de migration).
"""

from __future__ import annotations
import os

from google.cloud import bigquery


def _load(client: bigquery.Client, table: str, rows: list[dict],
          disposition: str, schema) -> None:
    job_config = bigquery.LoadJobConfig(
        schema=schema,
        write_disposition=disposition,
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
    )
    client.load_table_from_json(rows, table, job_config=job_config).result()


def load_replace_window(client: bigquery.Client, table: str, rows: list[dict],
                        since: str, until: str, date_field: str = "date") -> int:
    """Remplace proprement la fenêtre [since, until] de `table` par `rows`."""
    schema = client.get_table(table).schema  # réutilise le schéma défini de la table

    def _delete_window():
        client.query(
            f"DELETE FROM `{table}` WHERE {date_field} BETWEEN @s AND @u",
            job_config=bigquery.QueryJobConfig(query_parameters=[
                bigquery.ScalarQueryParameter("s", "DATE", since),
                bigquery.ScalarQueryParameter("u", "DATE", until),
            ]),
        ).result()

    if not rows:
        _delete_window()
        return 0

    staging = f"{table}__stg"
    _load(client, staging, rows, bigquery.WriteDisposition.WRITE_TRUNCATE, schema)
    _delete_window()
    client.query(f"INSERT INTO `{table}` SELECT * FROM `{staging}`").result()
    client.delete_table(staging, not_found_ok=True)
    return len(rows)


def load_replace_all(client: bigquery.Client, table: str, rows: list[dict]) -> int:
    """Remplace TOUT le contenu de la table par `rows` (load job WRITE_TRUNCATE)."""
    if not rows:
        return 0
    schema = client.get_table(table).schema
    _load(client, table, rows, bigquery.WriteDisposition.WRITE_TRUNCATE, schema)
    return len(rows)


def _flush(client: bigquery.Client, table: str, partition_by: str,
           cluster_by: str | None = None) -> None:
    cl = f" CLUSTER BY {cluster_by}" if cluster_by else ""
    client.query(
        f"CREATE OR REPLACE TABLE `{table}` PARTITION BY {partition_by}{cl} "
        f"AS SELECT * FROM `{table}`"
    ).result()
    print(f"[flush] {table} réécrite (streaming buffer vidé)")


def flush_default() -> None:
    """Vide le buffer résiduel des tables déjà alimentées par l'ancienne méthode."""
    project = os.environ["BQ_PROJECT"]
    dataset = os.environ.get("BQ_DATASET", "lpl_cockpit")
    client = bigquery.Client(project=project)
    _flush(client, f"{project}.{dataset}.shopify_orders_daily", "date")
    _flush(client, f"{project}.{dataset}.meta_daily", "date", "campaign_id")
