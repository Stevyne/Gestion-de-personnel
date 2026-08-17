"""Schéma idempotent du module Objectifs (Phase 7).

Un objectif est une cible individuelle assignée (ou proposée) à un employé
pour une période donnée. Workflow collaboratif :

    * brouillon     : créé par l'employé ou le manager, pas encore visible par
                      l'autre partie comme « en cours » ;
    * en_cours      : validé par le manager, progression suivie ;
    * atteint        : l'employé l'a terminé, en attente de validation manager ;
    * non_atteint   : marqué par le manager à l'échéance ;
    * annule        : abandonné.

Seul le créateur initial peut modifier un brouillon ; une fois en_cours,
l'employé peut ajuster la progression (0-100 %) et ajouter des points de
situation, le manager peut clôturer, les RH ont un droit de vue global.
"""


def appliquer_schema_phase7(cur):
    cur.execute("""CREATE TABLE IF NOT EXISTS objectifs (
        id SERIAL PRIMARY KEY,
        employe_id INTEGER NOT NULL REFERENCES employes(id) ON DELETE CASCADE,
        titre VARCHAR(200) NOT NULL,
        description TEXT,
        categorie VARCHAR(80),
        priorite VARCHAR(10) NOT NULL DEFAULT 'normale',
        statut VARCHAR(20) NOT NULL DEFAULT 'brouillon',
        progression INTEGER NOT NULL DEFAULT 0 CHECK (progression BETWEEN 0 AND 100),
        date_debut DATE,
        date_echeance DATE,
        date_realisation DATE,
        -- Qui a créé / validé / clôturé
        cree_par INTEGER REFERENCES users(id) ON DELETE SET NULL,
        cree_par_role VARCHAR(20),
        valide_par INTEGER REFERENCES users(id) ON DELETE SET NULL,
        valide_le DATE,
        cloture_par INTEGER REFERENCES users(id) ON DELETE SET NULL,
        cloture_le DATE,
        cloture_commentaire TEXT,
        -- Lien optionnel vers une compétence (pour l'analyse des besoins)
        competence_id INTEGER REFERENCES competences(id) ON DELETE SET NULL,
        date_creation TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        date_modification TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_obj_employe ON objectifs(employe_id, statut)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_obj_echeance ON objectifs(date_echeance)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_obj_statut ON objectifs(statut)")

    # Points de situation (historique des mises à jour de progression)
    cur.execute("""CREATE TABLE IF NOT EXISTS objectifs_points (
        id SERIAL PRIMARY KEY,
        objectif_id INTEGER NOT NULL REFERENCES objectifs(id) ON DELETE CASCADE,
        auteur_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
        progression INTEGER NOT NULL CHECK (progression BETWEEN 0 AND 100),
        commentaire TEXT,
        date_creation TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_obj_points_obj ON objectifs_points(objectif_id, date_creation)")
