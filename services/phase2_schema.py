"""Migrations idempotentes de la phase 2 : départs, SLA et contrats."""


def appliquer_schema_phase2(cur):
    # ---------------------------------------------------------------- départs
    for colonne, type_sql in (
        ('actif', 'BOOLEAN NOT NULL DEFAULT TRUE'),
        ('statut_depart', "VARCHAR(20) NOT NULL DEFAULT 'aucun'"),
        ('date_depart_prevue', 'DATE'),
        ('date_depart_effective', 'DATE'),
        ('motif_depart', 'TEXT'),
        ('depart_initie_par', 'INTEGER'),
        ('depart_initie_le', 'TIMESTAMP'),
        ('depart_finalise_par', 'INTEGER'),
        ('depart_finalise_le', 'TIMESTAMP'),
    ):
        cur.execute(f"ALTER TABLE employes ADD COLUMN IF NOT EXISTS {colonne} {type_sql}")
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS actif BOOLEAN NOT NULL DEFAULT TRUE")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_employes_actif ON employes(actif, departement)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_employes_depart ON employes(statut_depart, date_depart_prevue)")
    cur.execute('''CREATE TABLE IF NOT EXISTS depart_employe_logs (
        id SERIAL PRIMARY KEY,
        employe_id INTEGER NOT NULL REFERENCES employes(id) ON DELETE CASCADE,
        evenement VARCHAR(30) NOT NULL,
        details TEXT,
        acteur_id INTEGER,
        acteur_nom VARCHAR(80),
        date_evenement TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
    )''')
    cur.execute("CREATE INDEX IF NOT EXISTS idx_depart_logs_employe ON depart_employe_logs(employe_id, date_evenement DESC)")

    # Même une écriture SQL directe ne peut archiver un employé qui détient
    # encore une attribution ou un exemplaire physique.
    cur.execute('''CREATE OR REPLACE FUNCTION verifier_depart_sans_materiel()
        RETURNS trigger AS $$
        BEGIN
          IF OLD.actif = TRUE AND NEW.actif = FALSE THEN
            IF EXISTS (SELECT 1 FROM materiels_attributions
                       WHERE employe_id=NEW.id AND date_retour IS NULL) THEN
              RAISE EXCEPTION 'Retour de tous les matériels obligatoire avant le départ';
            END IF;
            IF EXISTS (SELECT 1 FROM materiel_exemplaires WHERE employe_id=NEW.id) THEN
              RAISE EXCEPTION 'Un exemplaire est encore rattaché à cet employé';
            END IF;
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql''')
    cur.execute("DROP TRIGGER IF EXISTS trg_verifier_depart_sans_materiel ON employes")
    cur.execute('''CREATE TRIGGER trg_verifier_depart_sans_materiel
                   BEFORE UPDATE OF actif ON employes FOR EACH ROW
                   EXECUTE FUNCTION verifier_depart_sans_materiel()''')

    # Une fiche archivée reste consultable, mais aucune nouvelle opération
    # métier ne peut lui être rattachée.
    cur.execute('''CREATE OR REPLACE FUNCTION verifier_employe_actif_operation()
        RETURNS trigger AS $$
        DECLARE est_actif BOOLEAN;
        BEGIN
          SELECT actif INTO est_actif FROM employes WHERE id=NEW.employe_id;
          IF est_actif IS DISTINCT FROM TRUE THEN
            RAISE EXCEPTION 'Opération interdite pour un employé archivé';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql''')
    # Les triggers sont installés plus bas, après création de la table contrats.

    # ---------------------------------------------------------- maintenance SLA
    for colonne, type_sql in (
        ('reference', 'VARCHAR(40)'),
        ('priorite', "VARCHAR(10) NOT NULL DEFAULT 'normale'"),
        ('sla_echeance', 'TIMESTAMP'),
        ('date_prise_en_charge', 'TIMESTAMP'),
        ('date_resolution', 'TIMESTAMP'),
        ('sla_respecte', 'BOOLEAN'),
    ):
        cur.execute(f"ALTER TABLE materiel_maintenances ADD COLUMN IF NOT EXISTS {colonne} {type_sql}")
    cur.execute('''CREATE TABLE IF NOT EXISTS maintenance_compteurs (
        annee INTEGER PRIMARY KEY,
        dernier INTEGER NOT NULL DEFAULT 0
    )''')
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS uq_maintenance_reference ON materiel_maintenances(reference) WHERE reference IS NOT NULL")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_maintenance_sla ON materiel_maintenances(statut, sla_echeance)")
    cur.execute('''CREATE OR REPLACE FUNCTION preparer_ticket_maintenance()
        RETURNS trigger AS $$
        DECLARE numero INTEGER; annee_ticket INTEGER;
        BEGIN
          NEW.priorite := COALESCE(NEW.priorite, 'normale');
          IF NEW.reference IS NULL OR btrim(NEW.reference) = '' THEN
            annee_ticket := EXTRACT(YEAR FROM COALESCE(NEW.date_signalement, CURRENT_DATE));
            INSERT INTO maintenance_compteurs(annee,dernier) VALUES (annee_ticket,1)
            ON CONFLICT(annee) DO UPDATE SET dernier=maintenance_compteurs.dernier+1
            RETURNING dernier INTO numero;
            NEW.reference := 'MAINT-' || annee_ticket || '-' || lpad(numero::text,3,'0');
          END IF;
          IF NEW.sla_echeance IS NULL THEN
            NEW.sla_echeance := COALESCE(NEW.date_creation,CURRENT_TIMESTAMP) +
              CASE NEW.priorite
                WHEN 'critique' THEN INTERVAL '4 hours'
                WHEN 'haute' THEN INTERVAL '1 day'
                WHEN 'basse' THEN INTERVAL '7 days'
                ELSE INTERVAL '3 days'
              END;
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql''')
    cur.execute("DROP TRIGGER IF EXISTS trg_preparer_ticket_maintenance ON materiel_maintenances")
    cur.execute('''CREATE TRIGGER trg_preparer_ticket_maintenance
                   BEFORE INSERT OR UPDATE OF priorite ON materiel_maintenances
                   FOR EACH ROW EXECUTE FUNCTION preparer_ticket_maintenance()''')
    # Backfill des anciens tickets via le trigger, sans modifier leur priorité.
    cur.execute("UPDATE materiel_maintenances SET priorite=COALESCE(priorite,'normale') WHERE reference IS NULL OR sla_echeance IS NULL")

    # ---------------------------------------------------------------- contrats
    cur.execute('''CREATE TABLE IF NOT EXISTS contrats (
        id SERIAL PRIMARY KEY,
        employe_id INTEGER NOT NULL REFERENCES employes(id) ON DELETE CASCADE,
        type_contrat VARCHAR(20) NOT NULL,
        reference VARCHAR(80),
        date_debut DATE NOT NULL,
        date_fin DATE,
        statut VARCHAR(20) NOT NULL DEFAULT 'actif',
        notes TEXT,
        nom_fichier VARCHAR(255),
        type_fichier VARCHAR(20),
        taille INTEGER,
        contenu BYTEA,
        renouvelle_depuis INTEGER REFERENCES contrats(id) ON DELETE SET NULL,
        cree_par INTEGER,
        date_creation TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        date_modification TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
    )''')
    cur.execute("CREATE INDEX IF NOT EXISTS idx_contrats_employe ON contrats(employe_id, date_debut DESC)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_contrats_expiration ON contrats(statut, date_fin)")
    cur.execute('''CREATE TABLE IF NOT EXISTS contrats_alertes (
        contrat_id INTEGER REFERENCES contrats(id) ON DELETE CASCADE,
        type_alerte VARCHAR(20) NOT NULL,
        envoye_le DATE NOT NULL DEFAULT CURRENT_DATE,
        PRIMARY KEY (contrat_id, type_alerte)
    )''')
    for table in ('presences','conges','permissions','absences','documents',
                  'materiels_attributions','contrats'):
        trigger = f'trg_{table}_employe_actif'
        cur.execute(f'DROP TRIGGER IF EXISTS {trigger} ON {table}')
        cur.execute(f'''CREATE TRIGGER {trigger}
                       BEFORE INSERT OR UPDATE OF employe_id ON {table}
                       FOR EACH ROW EXECUTE FUNCTION verifier_employe_actif_operation()''')

    # ------------------------------------------------------------ checks phase2
    checks = (
        ('employes', 'ck_employes_statut_depart',
         "statut_depart IN ('aucun','preparation','finalise','annule')"),
        ('materiel_maintenances', 'ck_maintenance_priorite',
         "priorite IN ('basse','normale','haute','critique')"),
        ('materiel_maintenances', 'ck_maintenance_reference',
         "reference IS NULL OR reference ~ '^MAINT-[0-9]{4}-[0-9]{3,}$'"),
        ('contrats', 'ck_contrats_type',
         "type_contrat IN ('cdi','cdd','stage','consultant','autre')"),
        ('contrats', 'ck_contrats_statut',
         "statut IN ('actif','expire','resilie','renouvele')"),
        ('contrats', 'ck_contrats_dates',
         'date_fin IS NULL OR date_fin >= date_debut'),
        ('contrats', 'ck_contrats_taille', 'taille IS NULL OR taille >= 0'),
    )
    for table, nom, expression in checks:
        cur.execute(f'''DO $$ BEGIN
          IF NOT EXISTS (SELECT 1 FROM pg_constraint
                         WHERE conname='{nom}' AND conrelid='{table}'::regclass) THEN
            ALTER TABLE {table} ADD CONSTRAINT {nom} CHECK ({expression}) NOT VALID;
          END IF;
        END $$''')
