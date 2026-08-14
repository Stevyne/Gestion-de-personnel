"""Module complet de recrutement.

Revision ID: 20260814_recrutement
Revises: 20260813_phase4
Create Date: 2026-08-14
"""

from alembic import op

from services.phase5_schema import appliquer_schema_phase5


revision = '20260814_recrutement'
down_revision = '20260813_phase4'
branch_labels = None
depends_on = None


class _AlembicCursor:
    def execute(self, statement, params=None):
        if params:
            raise RuntimeError('La migration recrutement ne doit pas utiliser de paramètres.')
        return op.execute(statement)


def upgrade():
    appliquer_schema_phase5(_AlembicCursor())


def downgrade():
    op.execute("""DO $$ BEGIN
      IF EXISTS (SELECT 1 FROM recrutement_candidats WHERE employe_id IS NOT NULL) THEN
        RAISE EXCEPTION 'Downgrade refusé : des candidats ont déjà été embauchés';
      END IF;
    END $$""")
    for table in (
        'recrutement_entretien_notes', 'recrutement_entretiens',
        'recrutement_evaluations', 'recrutement_criteres',
        'recrutement_candidatures', 'recrutement_candidats',
        'recrutement_offres', 'recrutement_demandes',
    ):
        op.execute(f'DROP TABLE IF EXISTS {table} CASCADE')
