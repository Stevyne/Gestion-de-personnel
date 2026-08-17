"""Création et mise à niveau idempotente du schéma historique PostgreSQL.

Alembic versionne les migrations de production ; cette fonction conserve le
bootstrap compatible avec les installations existantes et la suite de tests.
"""

from datetime import datetime

from services.bootstrap_seed import appliquer_seed_initial
from services.phase1_schema import appliquer_contraintes_phase1
from services.phase2_schema import appliquer_schema_phase2
from services.phase3_schema import appliquer_schema_phase3
from services.phase4_schema import appliquer_schema_phase4
from services.phase5_schema import appliquer_schema_phase5
from services.phase6_schema import appliquer_schema_phase6


def initialiser_schema(get_db, get_cursor, logger, calculer_jours_acquis_prorata):
    conn = get_db()
    cur = get_cursor(conn)
    cur.execute("SELECT pg_advisory_xact_lock(hashtext('gestion_personnel_init_db'))")

    cur.execute('''CREATE TABLE IF NOT EXISTS departements (
        id SERIAL PRIMARY KEY,
        nom VARCHAR(100) UNIQUE,
        description TEXT,
        responsable VARCHAR(150)
    )''')
    # Migration : ajoute les colonnes si la table existait déjà sans elles
    cur.execute("ALTER TABLE departements ADD COLUMN IF NOT EXISTS description TEXT")
    cur.execute("ALTER TABLE departements ADD COLUMN IF NOT EXISTS responsable VARCHAR(150)")
    cur.execute('''CREATE TABLE IF NOT EXISTS employes (id SERIAL PRIMARY KEY, nom VARCHAR(100) NOT NULL, prenom VARCHAR(100) NOT NULL, poste VARCHAR(150), departement VARCHAR(100), email VARCHAR(150), telephone VARCHAR(20), date_embauche DATE, salaire NUMERIC(10,2))''')
    cur.execute('''CREATE TABLE IF NOT EXISTS presences (id SERIAL PRIMARY KEY, employe_id INTEGER REFERENCES employes(id), date DATE, heure_arrivee TIME, heure_depart TIME, statut VARCHAR(30) DEFAULT 'présent', commentaire TEXT, UNIQUE(employe_id, date))''')
    cur.execute('''CREATE TABLE IF NOT EXISTS conges (id SERIAL PRIMARY KEY, employe_id INTEGER REFERENCES employes(id), type_conge VARCHAR(50), date_debut DATE, date_fin DATE, nombre_jours INTEGER, motif TEXT, statut VARCHAR(20) DEFAULT 'en attente', date_demande DATE DEFAULT CURRENT_DATE)''')

    # ==================== TABLE SOLDES_CONGES (CRITIQUE) ====================
    cur.execute('''CREATE TABLE IF NOT EXISTS soldes_conges (
        id SERIAL PRIMARY KEY,
        employe_id INTEGER REFERENCES employes(id) ON DELETE CASCADE,
        annee INTEGER NOT NULL,
        jours_acquis NUMERIC(5,1) DEFAULT 25,
        jours_utilises NUMERIC(5,1) DEFAULT 0,
        UNIQUE(employe_id, annee)
    )''')
    cur.execute("CREATE INDEX IF NOT EXISTS idx_soldes_employe_annee ON soldes_conges(employe_id, annee)")
    # Si RH fixe manuellement jours_acquis (ex: jours de congé exceptionnels
    # accordés), le job de recalcul mensuel automatique ne doit PAS l'écraser.
    cur.execute("ALTER TABLE soldes_conges ADD COLUMN IF NOT EXISTS jours_acquis_manuel BOOLEAN DEFAULT FALSE")

    cur.execute('''CREATE TABLE IF NOT EXISTS users (id SERIAL PRIMARY KEY, username VARCHAR(80) UNIQUE, password_hash VARCHAR(255), role VARCHAR(20) DEFAULT 'employe', employe_id INTEGER REFERENCES employes(id))''')
    # Absences non justifiées : jours d'absence qui ne relèvent ni d'un congé
    # ni d'une permission approuvés (ex. absence non signalée, no-show).
    cur.execute('''CREATE TABLE IF NOT EXISTS absences (
        id SERIAL PRIMARY KEY,
        employe_id INTEGER REFERENCES employes(id) ON DELETE CASCADE,
        date DATE NOT NULL,
        motif TEXT,
        enregistre_par INTEGER REFERENCES users(id),
        date_enregistrement TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(employe_id, date)
    )''')
    # Circuit de justification : les anciennes lignes deviennent explicitement
    # « non justifiées ». Le binaire reste en BYTEA, comme les documents RH et
    # avatars, afin de survivre aux redémarrages du disque éphémère Render.
    for col, typ in (
        ('statut', 'VARCHAR(30) DEFAULT \'non_justifiee\''),
        ('justificatif_nom', 'VARCHAR(255)'),
        ('justificatif_type', 'VARCHAR(20)'),
        ('justificatif_taille', 'INTEGER'),
        ('justificatif_contenu', 'BYTEA'),
        ('date_depot_justificatif', 'TIMESTAMP'),
        ('justification_commentaire', 'TEXT'),
        ('decide_par', 'INTEGER REFERENCES users(id) ON DELETE SET NULL'),
        ('decide_le', 'TIMESTAMP'),
        ('motif_refus', 'TEXT'),
        ('conge_id', 'INTEGER REFERENCES conges(id) ON DELETE SET NULL'),
    ):
        cur.execute(f"ALTER TABLE absences ADD COLUMN IF NOT EXISTS {col} {typ}")
    cur.execute("UPDATE absences SET statut = 'non_justifiee' WHERE statut IS NULL")
    cur.execute("ALTER TABLE absences ALTER COLUMN statut SET DEFAULT 'non_justifiee'")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_absences_statut ON absences(statut)")
    # Absences supprimées manuellement : on mémorise les couples (employe_id, date)
    # à NE PAS régénérer automatiquement. Sans cela, la génération auto recréerait
    # immédiatement toute absence supprimée (le jour reste sans présence) et la
    # suppression semblait ne pas fonctionner.
    cur.execute('''CREATE TABLE IF NOT EXISTS absences_exclues (
        employe_id INTEGER REFERENCES employes(id) ON DELETE CASCADE,
        date DATE NOT NULL,
        PRIMARY KEY (employe_id, date)
    )''')
    # Garde-fou d'idempotence pour les jobs planifiés (scheduler) : empêche un
    # job de tourner deux fois le même jour (redémarrage du dyno, plusieurs
    # workers gunicorn...).
    cur.execute('''CREATE TABLE IF NOT EXISTS scheduler_runs (
        job_name VARCHAR(100) NOT NULL,
        run_date DATE NOT NULL,
        executed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (job_name, run_date)
    )''')
    # ==================== TABLE PERMISSIONS (MODULE SÉPARÉ) ====================
    # Une permission fonctionne COMME un congé (demande → approbation/refus),
    # mais c'est une entité à part entière : elle NE fait PAS partie des congés
    # et ne déduit JAMAIS de jours du solde de congés (soldes_conges).
    cur.execute('''CREATE TABLE IF NOT EXISTS permissions (
        id SERIAL PRIMARY KEY,
        employe_id INTEGER REFERENCES employes(id) ON DELETE CASCADE,
        motif TEXT,
        date_debut DATE NOT NULL,
        date_fin DATE NOT NULL,
        nombre_jours INTEGER NOT NULL DEFAULT 1,
        statut VARCHAR(20) DEFAULT 'en attente',
        date_demande DATE DEFAULT CURRENT_DATE
    )''')
    cur.execute("CREATE INDEX IF NOT EXISTS idx_permissions_employe ON permissions(employe_id)")

    # ==================== WORKFLOWS RH (validation à deux niveaux) ==========
    # Congés et permissions suivent le même circuit : l'employé dépose sa
    # demande, le manager de son département donne un avis, le RH tranche.
    # Colonnes nullables : les demandes déjà en base restent valides.
    for table in ('conges', 'permissions'):
        for col, typ in [
            ('demande_par_id',   'INTEGER'),      # qui a déposé (self-service)
            ('avis_manager',     'VARCHAR(15)'),  # favorable / defavorable
            ('avis_manager_par', 'VARCHAR(80)'),
            ('avis_manager_le',  'DATE'),
            ('avis_commentaire', 'TEXT'),
            ('decide_par',       'VARCHAR(80)'),  # décision finale RH
            ('decide_le',        'DATE'),
            ('motif_refus',      'TEXT'),         # refus expliqué
            ('annule_par',       'VARCHAR(80)'),
        ]:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {typ}")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_conges_statut ON conges(statut)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_conges_demandeur ON conges(demande_par_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_perms_statut ON permissions(statut)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_perms_demandeur ON permissions(demande_par_id)")
    cur.execute('''CREATE TABLE IF NOT EXISTS audit_logs (id SERIAL PRIMARY KEY, user_id INTEGER, username VARCHAR(80), action VARCHAR(100), entity_type VARCHAR(50), entity_id INTEGER, details TEXT, ip_address VARCHAR(45), timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    cur.execute("CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_logs(timestamp DESC)")

    # ---- Registre des sessions actives -------------------------------------
    # Flask stocke la session dans un cookie signé côté navigateur : le serveur
    # ne sait donc pas qui est connecté et ne peut pas « reprendre » un cookie
    # déjà émis. Ce registre comble ce manque : chaque connexion y inscrit un
    # identifiant de session, que l'on peut révoquer à distance. Chaque requête
    # vérifie que la session présentée est toujours valide (voir session_active).
    cur.execute('''CREATE TABLE IF NOT EXISTS sessions_actives (
        id SERIAL PRIMARY KEY,
        sid VARCHAR(64) UNIQUE NOT NULL,
        user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
        username VARCHAR(80),
        ip_address VARCHAR(45),
        user_agent VARCHAR(300),
        login_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        revoked_at TIMESTAMP,
        revoked_by VARCHAR(80)
    )''')
    cur.execute("CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions_actives(user_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_sessions_seen ON sessions_actives(last_seen DESC)")

    # Table documents
    cur.execute('''CREATE TABLE IF NOT EXISTS documents (
        id SERIAL PRIMARY KEY,
        employe_id INTEGER REFERENCES employes(id) ON DELETE CASCADE,
        titre VARCHAR(255) NOT NULL,
        nom_fichier VARCHAR(255) NOT NULL,
        chemin_fichier VARCHAR(500) NOT NULL,
        type_fichier VARCHAR(50),
        taille INTEGER,
        date_upload TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        description TEXT
    )''')
    cur.execute("CREATE INDEX IF NOT EXISTS idx_documents_employe ON documents(employe_id)")
    # Date d'expiration optionnelle (CDD, visa, certification, contrat...) pour
    # les alertes automatiques avant échéance.
    cur.execute("ALTER TABLE documents ADD COLUMN IF NOT EXISTS date_expiration DATE")
    # Le contenu du fichier est stocké EN BASE (persistant), pas sur le disque
    # local du service (éphémère sur Render : perdu après une inactivité
    # prolongée ou un redéploiement). Les documents uploadés avant ce
    # correctif n'ont pas de `contenu` (colonne NULL) : leur fichier disque
    # est probablement déjà perdu, voir download_document().
    cur.execute("ALTER TABLE documents ADD COLUMN IF NOT EXISTS contenu BYTEA")
    # Photo de profil : portée par le COMPTE et non par la fiche employé, afin
    # que les comptes sans employé lié puissent aussi en avoir une.
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS photo VARCHAR(255)")
    # Contenu de la photo stocké EN BASE (persistant), pas sur le disque local
    # du service (éphémère sur Render : perdu après une inactivité prolongée
    # ou un redéploiement — c'était la cause des photos cassées).
    cur.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS photo_contenu BYTEA")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_documents_expiration ON documents(date_expiration)")
    # Empêche de renvoyer la même alerte d'expiration chaque jour : une seule
    # notif/email par document et par type d'alerte ('bientot' / 'expire').
    cur.execute('''CREATE TABLE IF NOT EXISTS documents_alertes (
        document_id INTEGER REFERENCES documents(id) ON DELETE CASCADE,
        type_alerte VARCHAR(20) NOT NULL,
        envoye_le DATE DEFAULT CURRENT_DATE,
        PRIMARY KEY (document_id, type_alerte)
    )''')

    # Table notifications (multi-utilisateur)
    cur.execute('''CREATE TABLE IF NOT EXISTS notifications (
        id SERIAL PRIMARY KEY,
        user_id INTEGER,
        title VARCHAR(200) NOT NULL,
        message TEXT,
        type VARCHAR(30) DEFAULT 'info',
        is_read BOOLEAN DEFAULT FALSE,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    cur.execute("CREATE INDEX IF NOT EXISTS idx_notif_user ON notifications(user_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_notif_unread ON notifications(user_id, is_read)")

    # Outbox SMTP persistante : la requête web ne dépend jamais de la latence
    # du fournisseur e-mail. Une clé d'événement optionnelle évite les doublons.
    cur.execute('''CREATE TABLE IF NOT EXISTS email_outbox (
        id SERIAL PRIMARY KEY,
        destinataire VARCHAR(320) NOT NULL,
        sujet VARCHAR(255) NOT NULL,
        corps_texte TEXT,
        corps_html TEXT,
        cle_evenement VARCHAR(200) UNIQUE,
        statut VARCHAR(20) NOT NULL DEFAULT 'en_attente',
        tentatives INTEGER NOT NULL DEFAULT 0,
        disponible_le TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        verrouille_le TIMESTAMP,
        date_creation TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        envoye_le TIMESTAMP,
        derniere_erreur TEXT
    )''')
    cur.execute("CREATE INDEX IF NOT EXISTS idx_outbox_a_envoyer ON email_outbox(statut, disponible_le)")

    # ==================== MATÉRIELS (stock par département) ====================
    # Un matériel appartient à un département (papiers, stylos, classeurs...).
    # `quantite` = stock actuel, recalculé à partir des mouvements.
    cur.execute('''CREATE TABLE IF NOT EXISTS materiels (
        id SERIAL PRIMARY KEY,
        nom VARCHAR(150) NOT NULL,
        categorie VARCHAR(50) DEFAULT 'fourniture',
        departement_id INTEGER REFERENCES departements(id) ON DELETE CASCADE,
        quantite INTEGER NOT NULL DEFAULT 0,
        seuil_alerte INTEGER NOT NULL DEFAULT 0,
        unite VARCHAR(30) DEFAULT 'unité',
        description TEXT,
        date_creation TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    cur.execute("CREATE INDEX IF NOT EXISTS idx_materiels_dept ON materiels(departement_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_materiels_nom ON materiels(nom)")

    # Historique des mouvements : toute entrée/sortie est tracée (auditable).
    # type_mouvement : 'entree' (approvisionnement) | 'sortie' (consommation)
    cur.execute('''CREATE TABLE IF NOT EXISTS materiels_mouvements (
        id SERIAL PRIMARY KEY,
        materiel_id INTEGER REFERENCES materiels(id) ON DELETE CASCADE,
        type_mouvement VARCHAR(10) NOT NULL,
        quantite INTEGER NOT NULL,
        employe_id INTEGER REFERENCES employes(id) ON DELETE SET NULL,
        motif TEXT,
        user_id INTEGER,
        username VARCHAR(80),
        date_mouvement TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    cur.execute("CREATE INDEX IF NOT EXISTS idx_mvt_materiel ON materiels_mouvements(materiel_id, date_mouvement DESC)")

    # Attributions durables (PC, téléphone, clés...) : remise à un employé
    # puis retour éventuel. Une attribution active a date_retour IS NULL.
    cur.execute('''CREATE TABLE IF NOT EXISTS materiels_attributions (
        id SERIAL PRIMARY KEY,
        materiel_id INTEGER REFERENCES materiels(id) ON DELETE CASCADE,
        employe_id INTEGER REFERENCES employes(id) ON DELETE CASCADE,
        quantite INTEGER NOT NULL DEFAULT 1,
        date_attribution DATE DEFAULT CURRENT_DATE,
        date_retour DATE,
        commentaire TEXT
    )''')
    cur.execute("CREATE INDEX IF NOT EXISTS idx_attrib_materiel ON materiels_attributions(materiel_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_attrib_employe ON materiels_attributions(employe_id)")
    # Accusé de réception : l'employé confirme avoir reçu le matériel, ce qui
    # évite les « je n'ai jamais eu ce PC » lors des inventaires ou des soldes
    # de tout compte.
    for col, typ in [
        ('accuse_reception', 'BOOLEAN DEFAULT FALSE'),
        ('accuse_le',        'DATE'),
        ('accuse_par',       'VARCHAR(80)'),
        ('conteste_motif',   'TEXT'),
        ('attribue_par',     'VARCHAR(80)'),
    ]:
        cur.execute(f"ALTER TABLE materiels_attributions ADD COLUMN IF NOT EXISTS {col} {typ}")
    # Évite de renvoyer l'alerte de stock bas en boucle : une seule notif tant
    # que le stock n'est pas repassé au-dessus du seuil (remis à FALSE alors).
    cur.execute("ALTER TABLE materiels ADD COLUMN IF NOT EXISTS alerte_envoyee BOOLEAN DEFAULT FALSE")

    # --- Inventaire physique -------------------------------------------------
    # Une campagne fige, à un instant T, la liste des articles d'un département
    # et leur stock théorique ; on y saisit ensuite le comptage réel. Le stock
    # n'est corrigé qu'à la clôture, via un mouvement d'ajustement tracé.
    # statut : 'en_cours' | 'cloture' | 'annule'
    cur.execute('''CREATE TABLE IF NOT EXISTS inventaires (
        id SERIAL PRIMARY KEY,
        reference VARCHAR(40),
        departement_id INTEGER REFERENCES departements(id) ON DELETE CASCADE,
        statut VARCHAR(15) NOT NULL DEFAULT 'en_cours',
        commentaire TEXT,
        date_creation TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        date_cloture TIMESTAMP,
        cree_par INTEGER,
        cree_par_nom VARCHAR(80),
        cloture_par INTEGER,
        cloture_par_nom VARCHAR(80)
    )''')
    cur.execute("CREATE INDEX IF NOT EXISTS idx_inventaires_dept ON inventaires(departement_id, date_creation DESC)")

    # Une ligne par article inventorié. quantite_theorique est figée à
    # l'ouverture (photo du stock) ; quantite_comptee est NULL tant que
    # l'article n'a pas été compté — un écart de 0 n'est PAS la même chose
    # qu'un article non compté.
    cur.execute('''CREATE TABLE IF NOT EXISTS inventaire_lignes (
        id SERIAL PRIMARY KEY,
        inventaire_id INTEGER REFERENCES inventaires(id) ON DELETE CASCADE,
        materiel_id INTEGER REFERENCES materiels(id) ON DELETE CASCADE,
        quantite_theorique INTEGER NOT NULL DEFAULT 0,
        quantite_comptee INTEGER,
        commentaire TEXT,
        date_comptage TIMESTAMP,
        compte_par_nom VARCHAR(80),
        UNIQUE (inventaire_id, materiel_id)
    )''')
    cur.execute("CREATE INDEX IF NOT EXISTS idx_inv_lignes_inv ON inventaire_lignes(inventaire_id)")
    # Permet de distinguer, dans l'historique d'un matériel, une entrée/sortie
    # saisie à la main d'un ajustement issu d'un inventaire physique.
    cur.execute("ALTER TABLE materiels_mouvements ADD COLUMN IF NOT EXISTS origine VARCHAR(20) DEFAULT 'manuel'")

    # --- Gestion de parc : patrimoine, exemplaires, maintenance --------------
    # Informations patrimoniales portées par l'ARTICLE (valables pour tout le
    # lot : marque, modèle, fournisseur...). Ce qui est propre à une unité
    # précise (n° de série, garantie, état) vit dans `materiel_exemplaires`.
    for col, typ in (
        ('marque',            'VARCHAR(80)'),
        ('modele',            'VARCHAR(120)'),
        ('fournisseur',       'VARCHAR(150)'),
        ('prix_acquisition',  'NUMERIC(14,2)'),
        ('date_acquisition',  'DATE'),
        ('duree_garantie_mois', 'INTEGER'),
        # Un article « suivi à l'unité » génère des exemplaires numérotés
        # (PC, mobilier) ; les consommables restent gérés en quantité.
        ('suivi_unitaire',    'BOOLEAN DEFAULT FALSE'),
        ('prefixe_inventaire', 'VARCHAR(12)'),
    ):
        cur.execute(f"ALTER TABLE materiels ADD COLUMN IF NOT EXISTS {col} {typ}")

    # Un exemplaire = une unité physique identifiable, étiquetable, réparable.
    # etat : 'bon' | 'usage' | 'panne' | 'reparation' | 'rebut'
    cur.execute('''CREATE TABLE IF NOT EXISTS materiel_exemplaires (
        id SERIAL PRIMARY KEY,
        materiel_id INTEGER REFERENCES materiels(id) ON DELETE CASCADE,
        numero_inventaire VARCHAR(40) UNIQUE NOT NULL,
        numero_serie VARCHAR(120),
        etat VARCHAR(15) NOT NULL DEFAULT 'bon',
        employe_id INTEGER REFERENCES employes(id) ON DELETE SET NULL,
        date_acquisition DATE,
        prix_acquisition NUMERIC(14,2),
        fournisseur VARCHAR(150),
        garantie_fin DATE,
        emplacement VARCHAR(150),
        commentaire TEXT,
        date_creation TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    cur.execute("CREATE INDEX IF NOT EXISTS idx_exemplaires_materiel ON materiel_exemplaires(materiel_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_exemplaires_num ON materiel_exemplaires(numero_inventaire)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_exemplaires_etat ON materiel_exemplaires(etat)")

    # Circuit de réparation : panne → envoi → retour (réparé ou irréparable).
    # statut : 'signale' | 'envoye' | 'repare' | 'irreparable' | 'annule'
    cur.execute('''CREATE TABLE IF NOT EXISTS materiel_maintenances (
        id SERIAL PRIMARY KEY,
        exemplaire_id INTEGER REFERENCES materiel_exemplaires(id) ON DELETE CASCADE,
        statut VARCHAR(15) NOT NULL DEFAULT 'signale',
        panne TEXT NOT NULL,
        technicien VARCHAR(150),
        date_signalement DATE DEFAULT CURRENT_DATE,
        date_envoi DATE,
        date_retour DATE,
        cout NUMERIC(14,2),
        diagnostic TEXT,
        signale_par VARCHAR(80),
        cloture_par VARCHAR(80),
        date_creation TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    cur.execute("CREATE INDEX IF NOT EXISTS idx_maint_exemplaire ON materiel_maintenances(exemplaire_id, date_creation DESC)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_maint_statut ON materiel_maintenances(statut)")

    # --- Workflow d'assignation à 4 acteurs -------------------------------
    # Ajouté après coup : les colonnes sont nullables pour que les
    # interventions déjà enregistrées restent valides sans reprise de données.
    for col, typ in [
        # Qui a signalé : on garde l'id en plus du username, pour pouvoir
        # notifier le demandeur et lui demander de valider le retour.
        ('signale_par_id',    'INTEGER'),
        # Assignation : soit un utilisateur interne, soit un prestataire.
        ('assigne_user_id',   'INTEGER'),
        ('prestataire_id',    'INTEGER'),
        ('date_assignation',  'DATE'),
        ('assigne_par',       'VARCHAR(80)'),
        # Retour d'atelier saisi par l'exécutant, avant validation.
        ('date_execution',    'DATE'),
        ('execute_par',       'VARCHAR(80)'),
        # Validation par le demandeur.
        ('valide_par',        'VARCHAR(80)'),
        ('date_validation',   'DATE'),
        ('motif_refus',       'TEXT'),
        ('validation_forcee', 'BOOLEAN DEFAULT FALSE'),
    ]:
        cur.execute(f"ALTER TABLE materiel_maintenances ADD COLUMN IF NOT EXISTS {col} {typ}")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_maint_assigne ON materiel_maintenances(assigne_user_id, statut)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_maint_demandeur ON materiel_maintenances(signale_par_id, statut)")

    # Annuaire des prestataires externes : remplace le champ texte libre, pour
    # que « Atelier Info+ » soit la même entité d'une intervention à l'autre.
    cur.execute('''CREATE TABLE IF NOT EXISTS prestataires (
        id SERIAL PRIMARY KEY,
        nom VARCHAR(150) NOT NULL,
        contact VARCHAR(150),
        telephone VARCHAR(40),
        email VARCHAR(150),
        specialite VARCHAR(100),
        actif BOOLEAN DEFAULT TRUE,
        date_creation TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    # Compteurs des numéros d'inventaire : une séquence par préfixe et par
    # année (PC-2026-001, PC-2026-002...). Table dédiée plutôt que MAX()+1,
    # qui réattribuerait un numéro après suppression d'un exemplaire.
    cur.execute('''CREATE TABLE IF NOT EXISTS materiel_compteurs (
        prefixe VARCHAR(12) NOT NULL,
        annee INTEGER NOT NULL,
        dernier INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (prefixe, annee)
    )''')

    # ==================== MESSAGERIE INTERNE ====================
    # Messages privés, discussions de groupe, annonces RH. Voir
    # blueprints/messagerie.py pour la logique.
    cur.execute('''CREATE TABLE IF NOT EXISTS conversations (
        id SERIAL PRIMARY KEY,
        type VARCHAR(20) NOT NULL DEFAULT 'prive',
        titre VARCHAR(200),
        cible_role VARCHAR(20),
        cree_par INTEGER REFERENCES users(id),
        date_creation TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    cur.execute('''CREATE TABLE IF NOT EXISTS conversation_membres (
        conversation_id INTEGER REFERENCES conversations(id) ON DELETE CASCADE,
        user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
        dernier_message_lu_id INTEGER,
        date_ajout TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (conversation_id, user_id)
    )''')
    # Colonne BYTEA historique ; la Phase 4 ajoute les clés S3 hybrides.
    # Aucun upload récent ne dépend du disque local éphémère.
    cur.execute('''CREATE TABLE IF NOT EXISTS messages (
        id SERIAL PRIMARY KEY,
        conversation_id INTEGER REFERENCES conversations(id) ON DELETE CASCADE,
        sender_id INTEGER REFERENCES users(id),
        contenu TEXT,
        piece_jointe_nom VARCHAR(255),
        piece_jointe_type VARCHAR(50),
        piece_jointe_taille INTEGER,
        piece_jointe_contenu BYTEA,
        date_envoi TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')
    cur.execute("CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id, id)")
    # La suppression d'un compte ne doit pas casser toute la gestion des
    # utilisateurs dès qu'il a écrit un message. On conserve l'historique en
    # anonymisant l'auteur (SET NULL) ; membres et lectures, eux, sont supprimés
    # par leurs clés étrangères CASCADE.
    cur.execute("""
        DO $$ BEGIN
          IF EXISTS (
              SELECT 1 FROM pg_constraint
               WHERE conname='conversations_cree_par_fkey' AND confdeltype <> 'n'
          ) THEN
            ALTER TABLE conversations DROP CONSTRAINT conversations_cree_par_fkey;
            ALTER TABLE conversations ADD CONSTRAINT conversations_cree_par_fkey
              FOREIGN KEY (cree_par) REFERENCES users(id) ON DELETE SET NULL;
          END IF;
          IF EXISTS (
              SELECT 1 FROM pg_constraint
               WHERE conname='messages_sender_id_fkey' AND confdeltype <> 'n'
          ) THEN
            ALTER TABLE messages DROP CONSTRAINT messages_sender_id_fkey;
            ALTER TABLE messages ADD CONSTRAINT messages_sender_id_fkey
              FOREIGN KEY (sender_id) REFERENCES users(id) ON DELETE SET NULL;
          END IF;
        END $$;
    """)
    # Suivi de lecture des annonces : pas de ligne de membre par destinataire
    # potentiel (pourrait être tous les employés), juste une marque de lecture.
    cur.execute('''CREATE TABLE IF NOT EXISTS annonce_lues (
        conversation_id INTEGER REFERENCES conversations(id) ON DELETE CASCADE,
        user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
        lu_le TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        PRIMARY KEY (conversation_id, user_id)
    )''')

    appliquer_contraintes_phase1(cur, logger)
    appliquer_schema_phase2(cur)
    appliquer_schema_phase3(cur)
    appliquer_schema_phase4(cur)
    appliquer_schema_phase5(cur)
    appliquer_schema_phase6(cur)

    # Les identifiants publics de démonstration sont strictement réservés au
    # développement/tests ; la production exige un secret de bootstrap.
    appliquer_seed_initial(cur, conn)

    # Seed soldes congés (maintenant possible car la table existe)
    annee_courante = datetime.now().year
    cur.execute("SELECT COUNT(*) FROM soldes_conges WHERE annee = %s", (annee_courante,))
    if cur.fetchone()['count'] == 0:
        cur.execute("SELECT id, date_embauche FROM employes")
        for emp in cur.fetchall():
            acquis_initial = calculer_jours_acquis_prorata(emp.get('date_embauche'), annee_courante)
            cur.execute("""
                INSERT INTO soldes_conges (employe_id, annee, jours_acquis, jours_utilises)
                VALUES (%s, %s, %s, 0)
                ON CONFLICT (employe_id, annee) DO NOTHING
            """, (emp['id'], annee_courante, acquis_initial))

    conn.commit()
    cur.close()
    conn.close()
    logger.info("Base PostgreSQL initialisée (Self-Service + Exports + Emails HTML + Soldes Congés)")
