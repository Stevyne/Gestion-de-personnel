"""Schéma idempotent du module Compétences (Phase 6 — base RH).

Tables créées :
    * competences             — référentiel géré par admin/RH
    * employe_competences     — niveau d'un employé sur une compétence,
                                attribué par le manager ou les RH

Ce module est la brique de base qui servira ensuite aux évaluations,
aux formations et aux plans de développement.
"""


def appliquer_schema_phase6(cur):
    # Référentiel des compétences. Le champ `categorie` permet de regrouper
    # (ex: Technique, Soft skills, Management, Langues…) sans imposer de
    # vocabulaire fermé : les RH peuvent l'ajouter librement.
    cur.execute("""CREATE TABLE IF NOT EXISTS competences (
        id SERIAL PRIMARY KEY,
        nom VARCHAR(100) NOT NULL UNIQUE,
        description TEXT,
        categorie VARCHAR(80),
        active BOOLEAN NOT NULL DEFAULT TRUE,
        date_creation TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        date_modification TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_competences_nom ON competences(nom)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_competences_categorie ON competences(categorie)")

    # Niveau d'un employé sur une compétence (0..100). Le `niveau` représente
    # l'évaluation du manager/RH. `notes` est un commentaire libre sur le niveau
    # (points forts, axes d'amélioration, besoin de formation…). La politique
    # d'accès est gérée côté blueprint (admin/RH tous les employés, manager
    # uniquement son département, employé simple : lecture seule de son
    # propre profil, pas d'écriture).
    cur.execute("""CREATE TABLE IF NOT EXISTS employe_competences (
        id SERIAL PRIMARY KEY,
        employe_id INTEGER NOT NULL REFERENCES employes(id) ON DELETE CASCADE,
        competence_id INTEGER NOT NULL REFERENCES competences(id) ON DELETE CASCADE,
        niveau INTEGER NOT NULL CHECK (niveau BETWEEN 0 AND 100),
        notes TEXT,
        ajoute_par INTEGER REFERENCES users(id) ON DELETE SET NULL,
        modifie_par INTEGER REFERENCES users(id) ON DELETE SET NULL,
        date_creation TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        date_modification TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (employe_id, competence_id)
    )""")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_emp_comp_employe ON employe_competences(employe_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_emp_comp_competence ON employe_competences(competence_id)")
