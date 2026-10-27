"""Stock : facture par article, destinataire des sorties, catégories prestataire.

Revision ID: 20261027_stock_factures
Revises: 20260817_objectifs
Create Date: 2026-10-27

Écrite pour être rejouable et insensible à l'ordre d'exécution : en production
`flask bootstrap-db` (init_db) peut déjà avoir appliqué tout ou partie de ces
changements avant que cette migration ne s'exécute.
"""

from alembic import op

revision = "20261027_stock_factures"
down_revision = "20260817_objectifs"
branch_labels = None
depends_on = None

# À garder synchronisé avec PRESTATAIRE_CATEGORIES (blueprints/parc.py).
CATEGORIES = ("comptable", "agent_distribution", "agent_saisie", "externe")

FACTURE_COLUMNS = (
    ("facture_nom", "VARCHAR(255)"),
    ("facture_contenu", "BYTEA"),
    ("facture_type", "VARCHAR(30)"),
    ("facture_taille", "INTEGER"),
    ("facture_date_upload", "TIMESTAMP"),
    ("facture_storage_key", "VARCHAR(1024)"),
    ("facture_storage_etag", "VARCHAR(128)"),
    ("facture_storage_sha256", "VARCHAR(64)"),
)


def upgrade():
    for col, type_sql in FACTURE_COLUMNS:
        op.execute(f"ALTER TABLE materiels ADD COLUMN IF NOT EXISTS {col} {type_sql}")

    op.execute(
        "ALTER TABLE materiels_mouvements ADD COLUMN IF NOT EXISTS "
        "departement_destinataire VARCHAR(120)"
    )

    # specialite (libre) -> categorie (imposée). L'ancienne colonne est lue
    # seulement si elle existe encore, puis abandonnée : init_db a pu passer
    # avant cette migration et l'avoir déjà supprimée.
    op.execute("ALTER TABLE prestataires ADD COLUMN IF NOT EXISTS categorie VARCHAR(30)")
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'prestataires'
                          AND column_name = 'specialite') THEN
                UPDATE prestataires SET categorie = 'comptable'
                 WHERE LOWER(COALESCE(specialite, '')) LIKE '%comptab%';
                UPDATE prestataires SET categorie = 'agent_distribution'
                 WHERE LOWER(COALESCE(specialite, '')) LIKE '%distribution%';
                UPDATE prestataires SET categorie = 'agent_saisie'
                 WHERE LOWER(COALESCE(specialite, '')) LIKE '%saisie%';
                ALTER TABLE prestataires DROP COLUMN specialite;
            END IF;
        END $$
        """
    )
    op.execute("ALTER TABLE prestataires ALTER COLUMN categorie SET DEFAULT 'externe'")
    op.execute("UPDATE prestataires SET categorie = 'externe' WHERE categorie IS NULL")
    op.execute("ALTER TABLE prestataires ALTER COLUMN categorie SET NOT NULL")
    op.execute(
        "ALTER TABLE prestataires DROP CONSTRAINT IF EXISTS ck_prestataire_categorie"
    )
    liste = ", ".join("'" + c + "'" for c in CATEGORIES)
    op.execute(
        "ALTER TABLE prestataires ADD CONSTRAINT ck_prestataire_categorie "
        "CHECK (categorie IN (" + liste + ")) NOT VALID"
    )


def downgrade():
    op.execute(
        "ALTER TABLE prestataires DROP CONSTRAINT IF EXISTS ck_prestataire_categorie"
    )
    op.execute(
        "ALTER TABLE prestataires ADD COLUMN IF NOT EXISTS specialite VARCHAR(100)"
    )
    op.execute("UPDATE prestataires SET specialite = categorie WHERE specialite IS NULL")
    op.execute("ALTER TABLE prestataires DROP COLUMN IF EXISTS categorie")
    op.execute(
        "ALTER TABLE materiels_mouvements DROP COLUMN IF EXISTS departement_destinataire"
    )
    for col, _ in FACTURE_COLUMNS:
        op.execute(f"ALTER TABLE materiels DROP COLUMN IF EXISTS {col}")
