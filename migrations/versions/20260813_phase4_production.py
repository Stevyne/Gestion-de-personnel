"""Socle de production Phase 4 : stockage objet et heartbeat scheduler.

Revision ID: 20260813_phase4
Revises: None
Create Date: 2026-08-13
"""

from alembic import op


revision = "20260813_phase4"
down_revision = None
branch_labels = None
depends_on = None


FILE_COLUMNS = (
    ("documents", "storage_key", "storage_etag", "storage_sha256"),
    ("contrats", "storage_key", "storage_etag", "storage_sha256"),
)


def upgrade():
    for table, key, etag, sha256 in FILE_COLUMNS:
        op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {key} VARCHAR(1024)")
        op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {etag} VARCHAR(128)")
        op.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {sha256} VARCHAR(64)")

    for name, sql_type in (
        ("justificatif_storage_key", "VARCHAR(1024)"),
        ("justificatif_storage_etag", "VARCHAR(128)"),
        ("justificatif_storage_sha256", "VARCHAR(64)"),
    ):
        op.execute(f"ALTER TABLE absences ADD COLUMN IF NOT EXISTS {name} {sql_type}")

    for name, sql_type in (
        ("piece_jointe_storage_key", "VARCHAR(1024)"),
        ("piece_jointe_storage_etag", "VARCHAR(128)"),
        ("piece_jointe_storage_sha256", "VARCHAR(64)"),
    ):
        op.execute(f"ALTER TABLE messages ADD COLUMN IF NOT EXISTS {name} {sql_type}")

    for name, table, column in (
        ("uq_documents_storage_key", "documents", "storage_key"),
        ("uq_contrats_storage_key", "contrats", "storage_key"),
        ("uq_absences_storage_key", "absences", "justificatif_storage_key"),
        ("uq_messages_storage_key", "messages", "piece_jointe_storage_key"),
    ):
        op.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS {name} ON {table}({column}) "
            f"WHERE {column} IS NOT NULL"
        )

    op.execute("ALTER TABLE messages DROP CONSTRAINT IF EXISTS ck_messages_contenu")
    op.execute("""
        ALTER TABLE messages ADD CONSTRAINT ck_messages_contenu CHECK (
            COALESCE(length(btrim(contenu)), 0) > 0
            OR piece_jointe_contenu IS NOT NULL
            OR piece_jointe_storage_key IS NOT NULL
        ) NOT VALID
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS scheduler_heartbeats (
            instance_id VARCHAR(160) PRIMARY KEY,
            service_name VARCHAR(40) NOT NULL DEFAULT 'scheduler',
            started_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_seen TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            metadata TEXT
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_scheduler_heartbeat_seen "
        "ON scheduler_heartbeats(service_name, last_seen DESC)"
    )
    op.execute("""
        CREATE TABLE IF NOT EXISTS backup_runs (
            id BIGSERIAL PRIMARY KEY,
            completed_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            object_key VARCHAR(1024) NOT NULL,
            size_bytes BIGINT NOT NULL,
            sha256 VARCHAR(64) NOT NULL,
            retention_days INTEGER NOT NULL,
            deleted_old_backups INTEGER NOT NULL DEFAULT 0
        )
    """)
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_backup_runs_completed "
        "ON backup_runs(completed_at DESC)"
    )


def downgrade():
    # Ne jamais rendre inaccessibles des fichiers déjà externalisés.
    op.execute("""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM documents WHERE storage_key IS NOT NULL)
             OR EXISTS (SELECT 1 FROM contrats WHERE storage_key IS NOT NULL)
             OR EXISTS (SELECT 1 FROM absences WHERE justificatif_storage_key IS NOT NULL)
             OR EXISTS (SELECT 1 FROM messages WHERE piece_jointe_storage_key IS NOT NULL)
          THEN
            RAISE EXCEPTION 'Downgrade refusé: des fichiers sont encore stockés dans S3';
          END IF;
        END $$
    """)
    op.execute("DROP TABLE IF EXISTS backup_runs")
    op.execute("DROP TABLE IF EXISTS scheduler_heartbeats")
    op.execute("ALTER TABLE messages DROP CONSTRAINT IF EXISTS ck_messages_contenu")
    op.execute("""
        ALTER TABLE messages ADD CONSTRAINT ck_messages_contenu CHECK (
            COALESCE(length(btrim(contenu)), 0) > 0
            OR piece_jointe_contenu IS NOT NULL
        ) NOT VALID
    """)
    for name in (
        "uq_documents_storage_key", "uq_contrats_storage_key",
        "uq_absences_storage_key", "uq_messages_storage_key",
    ):
        op.execute(f"DROP INDEX IF EXISTS {name}")
    for table, key, etag, sha256 in FILE_COLUMNS:
        for column in (sha256, etag, key):
            op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS {column}")
    for column in (
        "justificatif_storage_sha256", "justificatif_storage_etag",
        "justificatif_storage_key",
    ):
        op.execute(f"ALTER TABLE absences DROP COLUMN IF EXISTS {column}")
    for column in (
        "piece_jointe_storage_sha256", "piece_jointe_storage_etag",
        "piece_jointe_storage_key",
    ):
        op.execute(f"ALTER TABLE messages DROP COLUMN IF EXISTS {column}")
