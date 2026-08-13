"""Commandes Flask de migration progressive des BYTEA vers S3."""

from __future__ import annotations

from dataclasses import dataclass
import logging

import click

from services.object_storage import ObjectStorageError


logger = logging.getLogger("gestion_personnel.storage.migration")


@dataclass(frozen=True)
class FileTable:
    table: str
    content: str
    key: str
    etag: str
    sha256: str
    filename: str
    category: str


FILE_TABLES = (
    FileTable("documents", "contenu", "storage_key", "storage_etag",
              "storage_sha256", "nom_fichier", "documents"),
    FileTable("contrats", "contenu", "storage_key", "storage_etag",
              "storage_sha256", "nom_fichier", "contrats"),
    FileTable("absences", "justificatif_contenu", "justificatif_storage_key",
              "justificatif_storage_etag", "justificatif_storage_sha256",
              "justificatif_nom", "justificatifs-absences"),
    FileTable("messages", "piece_jointe_contenu", "piece_jointe_storage_key",
              "piece_jointe_storage_etag", "piece_jointe_storage_sha256",
              "piece_jointe_nom", "pieces-jointes-messagerie"),
)


def register_storage_cli(app, db_cursor, object_storage):
    @app.cli.group("storage")
    def storage_group():
        """Inspecter et migrer le stockage hybride PostgreSQL/S3."""

    @storage_group.command("status")
    def storage_status():
        """Affiche le nombre et le volume de fichiers par backend."""
        click.echo(
            f"backend={object_storage.backend_name} "
            f"threshold={object_storage.threshold_bytes} bucket={object_storage.bucket or '-'}"
        )
        with db_cursor() as (_conn, cur):
            for spec in FILE_TABLES:
                cur.execute(f"""
                    SELECT
                      COUNT(*) FILTER (WHERE {spec.content} IS NOT NULL) AS in_database,
                      COALESCE(SUM(octet_length({spec.content})), 0) AS database_bytes,
                      COUNT(*) FILTER (WHERE {spec.key} IS NOT NULL) AS in_object_storage
                    FROM {spec.table}
                """)
                row = cur.fetchone()
                click.echo(
                    f"{spec.table}: postgresql={row['in_database']} "
                    f"({row['database_bytes']} octets), s3={row['in_object_storage']}"
                )

    @storage_group.command("migrate")
    @click.option("--batch-size", default=100, type=click.IntRange(1, 1000), show_default=True)
    @click.option("--max-batches", default=100, type=click.IntRange(1, 10000), show_default=True)
    @click.option("--all-files", is_flag=True,
                  help="Migre aussi les fichiers inférieurs au seuil configuré.")
    @click.option("--keep-source", is_flag=True,
                  help="Conserve le BYTEA après vérification S3 (transition temporaire).")
    @click.option("--dry-run", is_flag=True, help="Compte sans envoyer ni modifier.")
    def migrate_storage(batch_size, max_batches, all_files, keep_source, dry_run):
        """Migre par lots, avec checksum SHA-256 et reprise idempotente."""
        if not object_storage.enabled:
            raise click.ClickException(
                "Activez OBJECT_STORAGE_ENABLED et configurez S3 avant la migration."
            )
        total = 0
        for spec in FILE_TABLES:
            migrated = 0
            threshold_clause = "" if all_files else f"AND octet_length({spec.content}) >= %s"
            params = [] if all_files else [object_storage.threshold_bytes]
            if dry_run:
                with db_cursor() as (_conn, cur):
                    cur.execute(f"""SELECT COUNT(*) AS total FROM {spec.table}
                        WHERE {spec.content} IS NOT NULL AND {spec.key} IS NULL
                        {threshold_clause}""", params)
                    migrated = cur.fetchone()["total"]
                total += migrated
                click.echo(f"{spec.table}: éligibles={migrated}")
                continue
            for _batch in range(max_batches):
                with db_cursor() as (_conn, cur):
                    cur.execute(f"""
                        SELECT id, {spec.content} AS file_content,
                               {spec.filename} AS file_name
                          FROM {spec.table}
                         WHERE {spec.content} IS NOT NULL
                           AND {spec.key} IS NULL
                           {threshold_clause}
                         ORDER BY id
                         LIMIT %s
                    """, params + [batch_size])
                    rows = cur.fetchall()
                if not rows:
                    break
                for row in rows:
                    stored = None
                    try:
                        stored = object_storage.store(
                            spec.category,
                            bytes(row["file_content"]),
                            row.get("file_name") or f"{spec.table}-{row['id']}",
                            force_external=True,
                        )
                        if not stored.external:
                            raise ObjectStorageError(
                                "S3 n'a pas confirmé l'externalisation du fichier."
                            )
                        with db_cursor(commit=True) as (_conn, cur):
                            source_assignment = (
                                "" if keep_source else f", {spec.content}=NULL"
                            )
                            cur.execute(f"""
                                UPDATE {spec.table}
                                   SET {spec.key}=%s, {spec.etag}=%s, {spec.sha256}=%s
                                       {source_assignment}
                                 WHERE id=%s AND {spec.key} IS NULL
                                RETURNING id
                            """, (stored.storage_key, stored.storage_etag,
                                  stored.storage_sha256, row["id"]))
                            if cur.fetchone() is None:
                                object_storage.delete(stored.storage_key)
                                continue
                        migrated += 1
                    except Exception:
                        if stored and stored.external:
                            object_storage.delete(stored.storage_key)
                        logger.exception(
                            "Migration S3 interrompue",
                            extra={"table": spec.table, "row_id": row["id"]},
                        )
                        raise
            total += migrated
            click.echo(
                f"{spec.table}: {'éligibles' if dry_run else 'migrés'}={migrated}"
            )
        click.echo(f"total={'éligibles' if dry_run else 'migrés'}={total}")

    return storage_group
