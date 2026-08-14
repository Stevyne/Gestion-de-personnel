"""Schéma idempotent du module Recrutement (Phase 5)."""


def appliquer_schema_phase5(cur):
    cur.execute("""CREATE TABLE IF NOT EXISTS recrutement_demandes (
        id SERIAL PRIMARY KEY,
        reference VARCHAR(30) UNIQUE,
        poste VARCHAR(150) NOT NULL,
        departement_id INTEGER NOT NULL REFERENCES departements(id) ON DELETE RESTRICT,
        nombre_postes INTEGER NOT NULL DEFAULT 1,
        type_contrat VARCHAR(20) NOT NULL,
        date_souhaitee DATE,
        salaire_min NUMERIC(12,2),
        salaire_max NUMERIC(12,2),
        motif TEXT NOT NULL,
        competences TEXT,
        statut VARCHAR(20) NOT NULL DEFAULT 'en_attente',
        demandeur_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
        decide_par INTEGER REFERENCES users(id) ON DELETE SET NULL,
        motif_decision TEXT,
        date_decision TIMESTAMP,
        date_creation TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        date_modification TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_recrutement_demandes_dept ON recrutement_demandes(departement_id, statut)")

    cur.execute("""CREATE TABLE IF NOT EXISTS recrutement_offres (
        id SERIAL PRIMARY KEY,
        reference VARCHAR(30) UNIQUE,
        demande_id INTEGER UNIQUE REFERENCES recrutement_demandes(id) ON DELETE SET NULL,
        titre VARCHAR(200) NOT NULL,
        description TEXT NOT NULL,
        departement_id INTEGER NOT NULL REFERENCES departements(id) ON DELETE RESTRICT,
        poste VARCHAR(150) NOT NULL,
        competences TEXT,
        niveau_experience VARCHAR(100),
        diplome_requis VARCHAR(150),
        type_contrat VARCHAR(20) NOT NULL,
        salaire_min NUMERIC(12,2),
        salaire_max NUMERIC(12,2),
        localisation VARCHAR(180),
        date_publication DATE,
        date_limite DATE,
        nombre_postes INTEGER NOT NULL DEFAULT 1,
        statut VARCHAR(20) NOT NULL DEFAULT 'brouillon',
        cree_par INTEGER REFERENCES users(id) ON DELETE SET NULL,
        date_creation TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        date_modification TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_recrutement_offres_statut ON recrutement_offres(statut, date_limite)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_recrutement_offres_dept ON recrutement_offres(departement_id)")

    cur.execute("""CREATE TABLE IF NOT EXISTS recrutement_candidats (
        id SERIAL PRIMARY KEY,
        nom VARCHAR(100) NOT NULL,
        prenom VARCHAR(100) NOT NULL,
        email VARCHAR(320) NOT NULL,
        telephone VARCHAR(30),
        adresse TEXT,
        date_naissance DATE,
        diplome VARCHAR(200),
        experience TEXT,
        experience_annees NUMERIC(5,2) DEFAULT 0,
        competences TEXT,
        cv_nom VARCHAR(255),
        cv_type VARCHAR(20),
        cv_taille INTEGER,
        cv_contenu BYTEA,
        cv_storage_key VARCHAR(1024),
        cv_storage_etag VARCHAR(128),
        cv_storage_sha256 VARCHAR(64),
        lettre_nom VARCHAR(255),
        lettre_type VARCHAR(20),
        lettre_taille INTEGER,
        lettre_contenu BYTEA,
        lettre_storage_key VARCHAR(1024),
        lettre_storage_etag VARCHAR(128),
        lettre_storage_sha256 VARCHAR(64),
        employe_id INTEGER UNIQUE REFERENCES employes(id) ON DELETE SET NULL,
        cree_par INTEGER REFERENCES users(id) ON DELETE SET NULL,
        date_creation TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        date_modification TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""")
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_recrutement_candidat_email ON recrutement_candidats(LOWER(email))")

    cur.execute("""CREATE TABLE IF NOT EXISTS recrutement_candidatures (
        id SERIAL PRIMARY KEY,
        candidat_id INTEGER NOT NULL REFERENCES recrutement_candidats(id) ON DELETE CASCADE,
        offre_id INTEGER NOT NULL REFERENCES recrutement_offres(id) ON DELETE RESTRICT,
        date_candidature DATE NOT NULL DEFAULT CURRENT_DATE,
        statut VARCHAR(25) NOT NULL DEFAULT 'recue',
        notes TEXT,
        score_dossier NUMERIC(5,2),
        score_entretien NUMERIC(5,2),
        score_global NUMERIC(5,2),
        decide_par INTEGER REFERENCES users(id) ON DELETE SET NULL,
        date_decision TIMESTAMP,
        date_embauche TIMESTAMP,
        date_creation TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        date_modification TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(candidat_id, offre_id)
    )""")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_recrutement_candidatures_offre ON recrutement_candidatures(offre_id, statut, score_global DESC)")

    cur.execute("""CREATE TABLE IF NOT EXISTS recrutement_criteres (
        id SERIAL PRIMARY KEY,
        offre_id INTEGER NOT NULL REFERENCES recrutement_offres(id) ON DELETE CASCADE,
        libelle VARCHAR(150) NOT NULL,
        poids NUMERIC(5,2) NOT NULL,
        ordre INTEGER NOT NULL DEFAULT 0,
        UNIQUE(offre_id, libelle)
    )""")
    cur.execute("""CREATE TABLE IF NOT EXISTS recrutement_evaluations (
        id SERIAL PRIMARY KEY,
        candidature_id INTEGER NOT NULL REFERENCES recrutement_candidatures(id) ON DELETE CASCADE,
        critere_id INTEGER NOT NULL REFERENCES recrutement_criteres(id) ON DELETE CASCADE,
        note NUMERIC(5,2) NOT NULL,
        commentaire TEXT,
        evaluateur_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
        date_evaluation TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(candidature_id, critere_id)
    )""")

    cur.execute("""CREATE TABLE IF NOT EXISTS recrutement_entretiens (
        id SERIAL PRIMARY KEY,
        candidature_id INTEGER NOT NULL REFERENCES recrutement_candidatures(id) ON DELETE CASCADE,
        date_entretien DATE NOT NULL,
        heure_entretien TIME NOT NULL,
        type_entretien VARCHAR(20) NOT NULL,
        lieu_ou_lien VARCHAR(300),
        evaluateur_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
        statut VARCHAR(20) NOT NULL DEFAULT 'planifie',
        notes TEXT,
        score NUMERIC(5,2),
        date_creation TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        date_modification TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_recrutement_entretiens_date ON recrutement_entretiens(date_entretien, statut)")
    cur.execute("""CREATE TABLE IF NOT EXISTS recrutement_entretien_notes (
        id SERIAL PRIMARY KEY,
        entretien_id INTEGER NOT NULL REFERENCES recrutement_entretiens(id) ON DELETE CASCADE,
        critere VARCHAR(100) NOT NULL,
        note NUMERIC(5,2) NOT NULL,
        UNIQUE(entretien_id, critere)
    )""")

    checks = (
        ('recrutement_demandes', 'ck_recrutement_demande_nb', 'nombre_postes > 0'),
        ('recrutement_demandes', 'ck_recrutement_demande_salaires', 'salaire_min IS NULL OR salaire_max IS NULL OR salaire_max >= salaire_min'),
        ('recrutement_demandes', 'ck_recrutement_demande_statut', "statut IN ('en_attente','validee','refusee','annulee','publiee')"),
        ('recrutement_offres', 'ck_recrutement_offre_nb', 'nombre_postes > 0'),
        ('recrutement_offres', 'ck_recrutement_offre_dates', 'date_limite IS NULL OR date_publication IS NULL OR date_limite >= date_publication'),
        ('recrutement_offres', 'ck_recrutement_offre_salaires', 'salaire_min IS NULL OR salaire_max IS NULL OR salaire_max >= salaire_min'),
        ('recrutement_offres', 'ck_recrutement_offre_statut', "statut IN ('brouillon','publiee','fermee','suspendue','pourvue')"),
        ('recrutement_candidats', 'ck_recrutement_candidat_experience', 'experience_annees >= 0'),
        ('recrutement_candidats', 'ck_recrutement_candidat_cv_taille', 'cv_taille IS NULL OR cv_taille >= 0'),
        ('recrutement_candidats', 'ck_recrutement_candidat_lettre_taille', 'lettre_taille IS NULL OR lettre_taille >= 0'),
        ('recrutement_candidatures', 'ck_recrutement_candidature_statut', "statut IN ('recue','preselectionnee','entretien','evaluation','acceptee','refusee','embauchee')"),
        ('recrutement_candidatures', 'ck_recrutement_scores', '(score_dossier IS NULL OR score_dossier BETWEEN 0 AND 100) AND (score_entretien IS NULL OR score_entretien BETWEEN 0 AND 100) AND (score_global IS NULL OR score_global BETWEEN 0 AND 100)'),
        ('recrutement_criteres', 'ck_recrutement_critere_poids', 'poids > 0 AND poids <= 100'),
        ('recrutement_evaluations', 'ck_recrutement_evaluation_note', 'note BETWEEN 0 AND 100'),
        ('recrutement_entretiens', 'ck_recrutement_entretien_type', "type_entretien IN ('presentiel','visio','telephone')"),
        ('recrutement_entretiens', 'ck_recrutement_entretien_statut', "statut IN ('planifie','realise','annule')"),
        ('recrutement_entretiens', 'ck_recrutement_entretien_score', 'score IS NULL OR score BETWEEN 0 AND 100'),
        ('recrutement_entretien_notes', 'ck_recrutement_entretien_note', 'note BETWEEN 0 AND 100'),
    )
    for table, nom, expression in checks:
        cur.execute(f"""DO $$ BEGIN
          IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='{nom}' AND conrelid='{table}'::regclass) THEN
            ALTER TABLE {table} ADD CONSTRAINT {nom} CHECK ({expression}) NOT VALID;
          END IF;
        END $$""")
