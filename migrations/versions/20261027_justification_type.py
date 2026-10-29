"""Nature de l'absence choisie lors du dépôt d'un justificatif.

Une absence justifiée était requalifiée systématiquement en « congé
maladie ». Toute pièce — convocation, attestation, décès — produisait
donc un arrêt maladie, ce qui faussait les calendriers et les rapports
RH.

On mémorise désormais la nature que l'employé propose en déposant sa
pièce (`absences.justification_type`). Les RH la voient au moment de la
décision, peuvent la corriger, et peuvent aussi retenir « aucun » :
l'absence est alors excusée sans créer le moindre congé.

Colonne ajoutée, jamais supprimée : un retour arrière reste possible.
"""
from alembic import op

revision = "20261027_justification_type"
down_revision = "20261027_facture_entree"
branch_labels = None
depends_on = None


def upgrade():
    op.execute(
        "ALTER TABLE absences ADD COLUMN IF NOT EXISTS justification_type VARCHAR(30)")


def downgrade():
    op.execute("ALTER TABLE absences DROP COLUMN IF EXISTS justification_type")
