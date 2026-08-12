import app as application


def _creer_exemplaire():
    with application.db_cursor(commit=True) as (conn, cur):
        cur.execute("""
            INSERT INTO departements (nom) VALUES ('Maintenance Test')
            ON CONFLICT (nom) DO UPDATE SET nom = EXCLUDED.nom RETURNING id
        """)
        dept_id = cur.fetchone()['id']
        cur.execute("""
            INSERT INTO materiels (nom, categorie, departement_id, quantite,
                                   suivi_unitaire)
            VALUES ('Portable test', 'informatique', %s, 0, TRUE)
            RETURNING id
        """, (dept_id,))
        materiel_id = cur.fetchone()['id']
        cur.execute("""
            INSERT INTO materiel_exemplaires
                (materiel_id, numero_inventaire, etat)
            VALUES (%s, 'TEST-2026-001', 'bon') RETURNING id
        """, (materiel_id,))
        return cur.fetchone()['id']


def _maintenance(absence=False):
    with application.db_cursor() as (conn, cur):
        cur.execute("SELECT * FROM materiel_maintenances ORDER BY id DESC LIMIT 1")
        return cur.fetchone()


def test_workflow_maintenance_complet_quatre_etapes(admin_client):
    exemplaire_id = _creer_exemplaire()

    response = admin_client.post(f'/exemplaires/{exemplaire_id}/panne',
                                 data={'panne': 'Écran noir'},
                                 follow_redirects=True)
    assert response.status_code == 200
    maintenance = _maintenance()
    assert maintenance['statut'] == 'signale'

    # L'administrateur est ici à la fois pilote, exécutant assigné et demandeur.
    admin_client.post(f"/maintenances/{maintenance['id']}/assigner", data={
        'cible': 'interne', 'assigne_user_id': '1',
    })
    maintenance = _maintenance()
    assert maintenance['statut'] == 'assigne'
    assert maintenance['assigne_user_id'] == 1

    admin_client.post(f"/maintenances/{maintenance['id']}/envoyer",
                      data={'date_envoi': ''})
    maintenance = _maintenance()
    assert maintenance['statut'] == 'envoye'

    admin_client.post(f"/maintenances/{maintenance['id']}/cloturer", data={
        'resultat': 'repare', 'cout': '125000',
        'diagnostic': 'Nappe remplacée', 'date_retour': '',
        'etat_retour': 'bon',
    })
    maintenance = _maintenance()
    assert maintenance['statut'] == 'a_valider'

    admin_client.post(f"/maintenances/{maintenance['id']}/valider",
                      data={'decision': 'valider'})
    maintenance = _maintenance()
    assert maintenance['statut'] == 'repare'
    assert maintenance['valide_par'] == 'admin'
    with application.db_cursor() as (conn, cur):
        cur.execute("SELECT etat FROM materiel_exemplaires WHERE id = %s",
                    (exemplaire_id,))
        assert cur.fetchone()['etat'] == 'bon'


def test_une_seule_maintenance_ouverte_par_exemplaire(admin_client):
    exemplaire_id = _creer_exemplaire()
    admin_client.post(f'/exemplaires/{exemplaire_id}/panne',
                      data={'panne': 'Première panne'})
    admin_client.post(f'/exemplaires/{exemplaire_id}/panne',
                      data={'panne': 'Doublon'})
    with application.db_cursor() as (conn, cur):
        cur.execute("""SELECT COUNT(*) AS n FROM materiel_maintenances
                       WHERE exemplaire_id = %s AND statut IN %s""",
                    (exemplaire_id, application.MAINTENANCE_OUVERTS))
        assert cur.fetchone()['n'] == 1
