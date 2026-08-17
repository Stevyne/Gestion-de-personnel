"""Module Objectifs (Phase 7) — suivi collaboratif des objectifs individuels.

Revision ID: 20260817_objectifs
Revises: 20260817_competences
Create Date: 2026-08-17
"""

from alembic import op

from services.phase7_schema import appliquer_schema_phase7


revision = '20260817_objectifs'
down_revision = '20260817_competences'
branch_labels = None
depends_on = None


class _AlembicCursor:
    def execute(self, statement, params=None):
        if params:
            raise RuntimeError(
                'La migration objectifs ne doit pas utiliser de paramètres.')
        return op.execute(statement)


def upgrade():
    appliquer_schema_phase7(_AlembicCursor())


def downgrade():
    op.execute('DROP TABLE IF EXISTS objectifs_points CASCADE')
    op.execute('DROP TABLE IF EXISTS objectifs CASCADE')
