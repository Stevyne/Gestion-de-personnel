"""Recrutement : coup de cœur (favori) sur les candidats.

Revision ID: 20261027_candidat_favori
Revises: 20261027_stock_factures
Create Date: 2026-10-27
"""

from alembic import op

revision = "20261027_candidat_favori"
down_revision = "20261027_stock_factures"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "ALTER TABLE recrutement_candidats ADD COLUMN IF NOT EXISTS "
        "favori BOOLEAN NOT NULL DEFAULT FALSE"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_recrutement_candidat_favori "
        "ON recrutement_candidats(favori) WHERE favori"
    )


def downgrade():
    op.execute("DROP INDEX IF EXISTS idx_recrutement_candidat_favori")
    op.execute(
        "ALTER TABLE recrutement_candidats DROP COLUMN IF EXISTS favori"
    )
