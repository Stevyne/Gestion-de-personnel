"""Module Compétences — référentiel et niveaux par employé (Phase 6).

Revision ID: 20260817_competences
Revises: 20260814_recrutement
Create Date: 2026-08-17
"""

from alembic import op

from services.phase6_schema import appliquer_schema_phase6


revision = '20260817_competences'
down_revision = '20260814_recrutement'
branch_labels = None
depends_on = None


class _AlembicCursor:
    def execute(self, statement, params=None):
        if params:
            raise RuntimeError(
                'La migration competences ne doit pas utiliser de paramètres.')
        return op.execute(statement)


def upgrade():
    appliquer_schema_phase6(_AlembicCursor())


def downgrade():
    op.execute('DROP TABLE IF EXISTS employe_competences CASCADE')
    op.execute('DROP TABLE IF EXISTS competences CASCADE')
