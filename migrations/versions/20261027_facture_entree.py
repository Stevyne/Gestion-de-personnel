"""Facture d'achat rattachée à l'entrée en stock.

La facture était portée par l'article (``materiels``), ce qui laissait
croire qu'elle concernait indifféremment les entrées et les sorties. Or
une sortie est un mouvement interne : il n'y a rien à facturer. La pièce
du fournisseur arrive avec la marchandise reçue, elle appartient donc au
mouvement d'**entrée**.

Conséquence utile : un article peut porter plusieurs factures, une par
réapprovisionnement, au lieu d'une seule qui s'écrasait à chaque import.

Les factures déjà importées sont recopiées sur la dernière entrée connue
de leur article, puis les colonnes de l'article sont vidées. Elles ne
sont volontairement pas supprimées du schéma : rien n'est détruit, un
retour arrière reste possible.
"""
from alembic import op

revision = "20261027_facture_entree"
down_revision = "20261027_candidat_favori"
branch_labels = None
depends_on = None

COLONNES = (
    ("facture_nom", "VARCHAR(255)"),
    ("facture_contenu", "BYTEA"),
    ("facture_type", "VARCHAR(10)"),
    ("facture_taille", "INTEGER"),
    ("facture_date_upload", "TIMESTAMP"),
    ("facture_storage_key", "VARCHAR(1024)"),
    ("facture_storage_etag", "VARCHAR(128)"),
    ("facture_storage_sha256", "VARCHAR(64)"),
)


def upgrade():
    for nom, type_sql in COLONNES:
        op.execute(
            "ALTER TABLE materiels_mouvements ADD COLUMN IF NOT EXISTS %s %s"
            % (nom, type_sql))

    # Reprise de l'existant : la facture de l'article part sur son entrée
    # la plus récente. Sans entrée enregistrée, il n'y a pas de mouvement
    # porteur : on laisse alors la facture où elle est plutôt que de la
    # perdre, et le message de fin le signale.
    op.execute("""
        UPDATE materiels_mouvements m
           SET facture_nom          = a.facture_nom,
               facture_contenu      = a.facture_contenu,
               facture_type         = a.facture_type,
               facture_taille       = a.facture_taille,
               facture_date_upload  = a.facture_date_upload,
               facture_storage_key   = a.facture_storage_key,
               facture_storage_etag  = a.facture_storage_etag,
               facture_storage_sha256 = a.facture_storage_sha256
          FROM materiels a
         WHERE m.materiel_id = a.id
           AND a.facture_nom IS NOT NULL
           AND m.type_mouvement = 'entree'
           AND m.id = (
                SELECT m2.id
                  FROM materiels_mouvements m2
                 WHERE m2.materiel_id = a.id
                   AND m2.type_mouvement = 'entree'
                 ORDER BY m2.date_mouvement DESC, m2.id DESC
                 LIMIT 1)
    """)

    # On ne vide que ce qui a bien été recopié.
    op.execute("""
        UPDATE materiels a
           SET facture_nom = NULL, facture_contenu = NULL,
               facture_type = NULL, facture_taille = NULL,
               facture_date_upload = NULL,
               facture_storage_key = NULL, facture_storage_etag = NULL,
               facture_storage_sha256 = NULL
         WHERE a.facture_nom IS NOT NULL
           AND EXISTS (
                SELECT 1 FROM materiels_mouvements m
                 WHERE m.materiel_id = a.id
                   AND m.type_mouvement = 'entree'
                   AND m.facture_nom IS NOT NULL)
    """)


def downgrade():
    # Remettre la dernière facture connue sur l'article, autant que faire
    # se peut : une facture par article, la plus récente.
    op.execute("""
        UPDATE materiels a
           SET facture_nom = m.facture_nom, facture_contenu = m.facture_contenu,
               facture_type = m.facture_type, facture_taille = m.facture_taille,
               facture_date_upload = m.facture_date_upload,
               facture_storage_key = m.facture_storage_key,
               facture_storage_etag = m.facture_storage_etag,
               facture_storage_sha256 = m.facture_storage_sha256
          FROM materiels_mouvements m
         WHERE m.materiel_id = a.id
           AND m.facture_nom IS NOT NULL
           AND m.id = (
                SELECT m2.id
                  FROM materiels_mouvements m2
                 WHERE m2.materiel_id = a.id
                   AND m2.facture_nom IS NOT NULL
                 ORDER BY m2.date_mouvement DESC, m2.id DESC
                 LIMIT 1)
    """)
    for nom, _ in reversed(COLONNES):
        op.execute(
            "ALTER TABLE materiels_mouvements DROP COLUMN IF EXISTS %s" % nom)
