"""Migrations et contraintes de la phase 1.

Séparées de ``app.py`` pour garder le fichier de composition compact. Toutes
les opérations sont idempotentes et compatibles avec une base existante.
"""

from services.roles import ROLE_CODES


def appliquer_contraintes_phase1(cur, logger):
    # Une attribution de matériel suivi à l'unité pointe désormais vers
    # l'exemplaire physique exact. Le lien est nullable pour préserver les
    # historiques antérieurs à cette migration.
    cur.execute("ALTER TABLE materiels_attributions ADD COLUMN IF NOT EXISTS exemplaire_id INTEGER")
    cur.execute("""
        DO $$ BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
             WHERE conname='materiels_attributions_exemplaire_id_fkey'
          ) THEN
            ALTER TABLE materiels_attributions
              ADD CONSTRAINT materiels_attributions_exemplaire_id_fkey
              FOREIGN KEY (exemplaire_id) REFERENCES materiel_exemplaires(id)
              ON DELETE RESTRICT;
          END IF;
        END $$;
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_attrib_exemplaire ON materiels_attributions(exemplaire_id)")
    cur.execute("""CREATE UNIQUE INDEX IF NOT EXISTS uq_attribution_exemplaire_active
                   ON materiels_attributions(exemplaire_id)
                   WHERE exemplaire_id IS NOT NULL AND date_retour IS NULL""")

    # Validation et synchronisation bidirectionnelle attribution ↔ détenteur.
    # Le trigger empêche qu'un exemplaire soit lié au mauvais article, attribué
    # en quantité > 1, indisponible ou déjà détenu par quelqu'un d'autre.
    cur.execute("""
        CREATE OR REPLACE FUNCTION verifier_attribution_exemplaire()
        RETURNS trigger AS $$
        DECLARE
          v_suivi BOOLEAN;
          v_materiel_id INTEGER;
          v_employe_id INTEGER;
          v_etat VARCHAR(15);
        BEGIN
          SELECT COALESCE(suivi_unitaire,FALSE) INTO v_suivi
            FROM materiels WHERE id=NEW.materiel_id;
          IF NOT FOUND THEN RAISE EXCEPTION 'Matériel introuvable'; END IF;

          IF NEW.exemplaire_id IS NOT NULL THEN
            SELECT materiel_id, employe_id, etat
              INTO v_materiel_id, v_employe_id, v_etat
              FROM materiel_exemplaires WHERE id=NEW.exemplaire_id FOR UPDATE;
            IF NOT FOUND THEN RAISE EXCEPTION 'Exemplaire introuvable'; END IF;
            IF v_materiel_id <> NEW.materiel_id THEN
              RAISE EXCEPTION 'L exemplaire ne correspond pas au matériel';
            END IF;
            IF NEW.quantite <> 1 THEN
              RAISE EXCEPTION 'Un exemplaire suivi à l unité impose une quantité de 1';
            END IF;
            IF NEW.date_retour IS NULL AND v_etat NOT IN ('bon','usage') THEN
              RAISE EXCEPTION 'Cet exemplaire est indisponible';
            END IF;
            IF NEW.date_retour IS NULL AND v_employe_id IS NOT NULL
               AND v_employe_id <> NEW.employe_id THEN
              RAISE EXCEPTION 'Cet exemplaire possède déjà un détenteur';
            END IF;
          ELSIF v_suivi AND NEW.date_retour IS NULL THEN
            RAISE EXCEPTION 'Un exemplaire est obligatoire pour un matériel suivi à l unité';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)
    cur.execute("DROP TRIGGER IF EXISTS trg_verifier_attribution_exemplaire ON materiels_attributions")
    cur.execute("""CREATE TRIGGER trg_verifier_attribution_exemplaire
                   BEFORE INSERT OR UPDATE ON materiels_attributions
                   FOR EACH ROW EXECUTE FUNCTION verifier_attribution_exemplaire()""")

    cur.execute("""
        CREATE OR REPLACE FUNCTION synchroniser_detenteur_exemplaire()
        RETURNS trigger AS $$
        BEGIN
          IF TG_OP IN ('UPDATE','DELETE')
             AND OLD.exemplaire_id IS NOT NULL AND OLD.date_retour IS NULL
             AND (TG_OP='DELETE' OR NEW.date_retour IS NOT NULL
                  OR NEW.exemplaire_id IS DISTINCT FROM OLD.exemplaire_id
                  OR NEW.employe_id IS DISTINCT FROM OLD.employe_id) THEN
            UPDATE materiel_exemplaires SET employe_id=NULL
             WHERE id=OLD.exemplaire_id AND employe_id=OLD.employe_id;
          END IF;
          IF TG_OP IN ('INSERT','UPDATE')
             AND NEW.exemplaire_id IS NOT NULL AND NEW.date_retour IS NULL THEN
            UPDATE materiel_exemplaires SET employe_id=NEW.employe_id
             WHERE id=NEW.exemplaire_id;
          END IF;
          RETURN CASE WHEN TG_OP='DELETE' THEN OLD ELSE NEW END;
        END;
        $$ LANGUAGE plpgsql;
    """)
    cur.execute("DROP TRIGGER IF EXISTS trg_synchroniser_detenteur_exemplaire ON materiels_attributions")
    cur.execute("""CREATE TRIGGER trg_synchroniser_detenteur_exemplaire
                   AFTER INSERT OR UPDATE OR DELETE ON materiels_attributions
                   FOR EACH ROW EXECUTE FUNCTION synchroniser_detenteur_exemplaire()""")

    # Toute modification directe du détenteur contournerait l'historique et le
    # stock. Elle est donc refusée ; le trigger de synchronisation ci-dessus est
    # le seul autorisé à changer employe_id (profondeur de trigger > 1).
    cur.execute("""
        CREATE OR REPLACE FUNCTION proteger_detenteur_exemplaire()
        RETURNS trigger AS $$
        BEGIN
          IF NEW.employe_id IS DISTINCT FROM OLD.employe_id
             AND pg_trigger_depth() <= 1 THEN
            RAISE EXCEPTION 'Le détenteur doit être modifié via une attribution';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
    """)
    cur.execute("DROP TRIGGER IF EXISTS trg_proteger_detenteur_exemplaire ON materiel_exemplaires")
    cur.execute("""CREATE TRIGGER trg_proteger_detenteur_exemplaire
                   BEFORE UPDATE OF employe_id ON materiel_exemplaires
                   FOR EACH ROW EXECUTE FUNCTION proteger_detenteur_exemplaire()""")

    # ==================== CONTRAINTES MÉTIER POSTGRESQL =====================
    # NOT VALID protège immédiatement toutes les nouvelles écritures sans
    # bloquer un déploiement à cause d'une ancienne ligne historique. Les
    # contraintes pourront être validées après nettoyage des éventuels écarts.
    roles_sql = ",".join("'%s'" % role for role in ROLE_CODES)
    check_constraints = [
        ('users', 'ck_users_role', f"role IN ({roles_sql})"),
        ('soldes_conges', 'ck_soldes_non_negatifs',
         'jours_acquis >= 0 AND jours_utilises >= 0'),
        ('conges', 'ck_conges_dates',
         'date_debut IS NULL OR date_fin IS NULL OR date_fin >= date_debut'),
        ('conges', 'ck_conges_jours', 'nombre_jours IS NULL OR nombre_jours > 0'),
        ('conges', 'ck_conges_statut',
         "statut IN ('en attente','en_attente','avis rendu','approuvé','refusé','annulé')"),
        ('permissions', 'ck_permissions_dates', 'date_fin >= date_debut'),
        ('permissions', 'ck_permissions_jours', 'nombre_jours > 0'),
        ('permissions', 'ck_permissions_statut',
         "statut IN ('en attente','en_attente','avis rendu','approuvé','refusé','annulé')"),
        ('absences', 'ck_absences_statut',
         "statut IN ('non_justifiee','justificatif_depose','acceptee','refusee')"),
        ('documents', 'ck_documents_taille', 'taille IS NULL OR taille >= 0'),
        ('materiels', 'ck_materiels_quantite', 'quantite >= 0'),
        ('materiels', 'ck_materiels_seuil', 'seuil_alerte >= 0'),
        ('materiels', 'ck_materiels_prix',
         'prix_acquisition IS NULL OR prix_acquisition >= 0'),
        ('materiels_mouvements', 'ck_mouvements_type',
         "type_mouvement IN ('entree','sortie')"),
        ('materiels_mouvements', 'ck_mouvements_quantite', 'quantite > 0'),
        ('materiels_attributions', 'ck_attributions_quantite', 'quantite > 0'),
        ('inventaires', 'ck_inventaires_statut',
         "statut IN ('en_cours','cloture','annule')"),
        ('inventaire_lignes', 'ck_inventaire_quantites',
         'quantite_theorique >= 0 AND (quantite_comptee IS NULL OR quantite_comptee >= 0)'),
        ('materiel_exemplaires', 'ck_exemplaires_etat',
         "etat IN ('bon','usage','panne','reparation','rebut')"),
        ('materiel_exemplaires', 'ck_exemplaires_prix',
         'prix_acquisition IS NULL OR prix_acquisition >= 0'),
        ('materiel_maintenances', 'ck_maintenances_statut',
         "statut IN ('signale','assigne','envoye','a_valider','repare','irreparable','annule')"),
        ('materiel_maintenances', 'ck_maintenances_cout', 'cout IS NULL OR cout >= 0'),
        ('conversations', 'ck_conversations_type',
         "type IN ('prive','groupe','annonce')"),
        ('conversations', 'ck_conversations_role',
         f"cible_role IS NULL OR cible_role IN ({roles_sql})"),
        ('messages', 'ck_messages_contenu',
         "COALESCE(length(btrim(contenu)),0) > 0 OR piece_jointe_contenu IS NOT NULL"),
        ('messages', 'ck_messages_taille',
         'piece_jointe_taille IS NULL OR piece_jointe_taille >= 0'),
        ('email_outbox', 'ck_email_outbox_statut',
         "statut IN ('en_attente','en_cours','envoye','echec')"),
    ]
    for table, nom, expression in check_constraints:
        cur.execute(f"""
            DO $$ BEGIN
              IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                 WHERE conname='{nom}' AND conrelid='{table}'::regclass
              ) THEN
                ALTER TABLE {table} ADD CONSTRAINT {nom}
                  CHECK ({expression}) NOT VALID;
              END IF;
            END $$;
        """)

    # Garde-fous de concurrence que les contrôles Flask seuls ne peuvent pas
    # garantir entre deux requêtes simultanées. Une base historique déjà
    # incohérente n'empêche pas le démarrage : l'anomalie est journalisée.
    unique_indexes = [
        ('uq_inventaire_ouvert_departement',
         "SELECT 1 FROM inventaires WHERE statut='en_cours' GROUP BY departement_id HAVING COUNT(*)>1 LIMIT 1",
         "CREATE UNIQUE INDEX uq_inventaire_ouvert_departement ON inventaires(departement_id) WHERE statut='en_cours'"),
        ('uq_maintenance_ouverte_exemplaire',
         "SELECT 1 FROM materiel_maintenances WHERE statut IN ('signale','assigne','envoye','a_valider') GROUP BY exemplaire_id HAVING COUNT(*)>1 LIMIT 1",
         "CREATE UNIQUE INDEX uq_maintenance_ouverte_exemplaire ON materiel_maintenances(exemplaire_id) WHERE statut IN ('signale','assigne','envoye','a_valider')"),
    ]
    for nom_index, detecter_doublon, creer_index in unique_indexes:
        cur.execute("SELECT 1 FROM pg_indexes WHERE schemaname=current_schema() AND indexname=%s",
                    (nom_index,))
        if cur.fetchone() is None:
            cur.execute(detecter_doublon)
            if cur.fetchone() is None:
                cur.execute(creer_index)
            else:
                logger.warning("Index %s non créé : doublons historiques à corriger.", nom_index)
