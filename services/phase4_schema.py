"""Compatibilité transitoire du schéma de production Phase 4.

Les mêmes opérations sont historisées par Alembic dans ``migrations/``. Cet
applicateur idempotent reste appelé par ``init_db`` en développement et dans la
suite de tests, pendant la transition depuis l'ancien bootstrap SQL. En
production, ``AUTO_INIT_DB=false`` et ``flask db upgrade`` font autorité.
"""


def appliquer_schema_phase4(cur):
    colonnes = {
        "documents": ("storage_key", "storage_etag", "storage_sha256"),
        "contrats": ("storage_key", "storage_etag", "storage_sha256"),
    }
    for table, noms in colonnes.items():
        cur.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {noms[0]} VARCHAR(1024)")
        cur.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {noms[1]} VARCHAR(128)")
        cur.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {noms[2]} VARCHAR(64)")

    for nom, type_sql in (
        ("justificatif_storage_key", "VARCHAR(1024)"),
        ("justificatif_storage_etag", "VARCHAR(128)"),
        ("justificatif_storage_sha256", "VARCHAR(64)"),
    ):
        cur.execute(f"ALTER TABLE absences ADD COLUMN IF NOT EXISTS {nom} {type_sql}")

    for nom, type_sql in (
        ("piece_jointe_storage_key", "VARCHAR(1024)"),
        ("piece_jointe_storage_etag", "VARCHAR(128)"),
        ("piece_jointe_storage_sha256", "VARCHAR(64)"),
    ):
        cur.execute(f"ALTER TABLE messages ADD COLUMN IF NOT EXISTS {nom} {type_sql}")

    indexes = (
        ("uq_documents_storage_key", "documents", "storage_key"),
        ("uq_contrats_storage_key", "contrats", "storage_key"),
        ("uq_absences_storage_key", "absences", "justificatif_storage_key"),
        ("uq_messages_storage_key", "messages", "piece_jointe_storage_key"),
    )
    for nom, table, colonne in indexes:
        cur.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS {nom} ON {table}({colonne}) "
            f"WHERE {colonne} IS NOT NULL"
        )

    # La contrainte historique n'acceptait qu'un BYTEA. Elle doit aussi accepter
    # une pièce jointe externalisée, tout en interdisant les messages vides.
    cur.execute("ALTER TABLE messages DROP CONSTRAINT IF EXISTS ck_messages_contenu")
    cur.execute("""
        ALTER TABLE messages ADD CONSTRAINT ck_messages_contenu CHECK (
            COALESCE(length(btrim(contenu)), 0) > 0
            OR piece_jointe_contenu IS NOT NULL
            OR piece_jointe_storage_key IS NOT NULL
        ) NOT VALID
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS scheduler_heartbeats (
            instance_id VARCHAR(160) PRIMARY KEY,
            service_name VARCHAR(40) NOT NULL DEFAULT 'scheduler',
            started_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_seen TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            metadata TEXT
        )
    """)
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_scheduler_heartbeat_seen "
        "ON scheduler_heartbeats(service_name, last_seen DESC)"
    )
    cur.execute("""
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
    cur.execute(
        "CREATE INDEX IF NOT EXISTS idx_backup_runs_completed "
        "ON backup_runs(completed_at DESC)"
    )
