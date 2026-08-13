import io
from datetime import date, timedelta

import pytest
from werkzeug.security import generate_password_hash

import app as application


MARK_IT = 'ScopeMarkerIT'
MARK_RH = 'ScopeMarkerHR'


def _seed_scope_data():
    today = date.today()
    with application.db_cursor(commit=True) as (conn, cur):
        for nom, description in (
            ('Informatique', f'{MARK_IT} Department'),
            ('Ressources Humaines', f'{MARK_RH} Department'),
        ):
            cur.execute("""INSERT INTO departements (nom, description)
                           VALUES (%s,%s) ON CONFLICT (nom) DO UPDATE
                           SET description=EXCLUDED.description RETURNING id""",
                        (nom, description))
            if nom == 'Informatique':
                dept_it = cur.fetchone()['id']
            else:
                dept_rh = cur.fetchone()['id']
        cur.execute("UPDATE employes SET departement='Informatique' WHERE id IN (1,3)")
        cur.execute("UPDATE employes SET departement='Ressources Humaines' WHERE id=2")

        cur.execute("""INSERT INTO employes
            (nom, prenom, poste, departement, email, date_embauche, salaire)
            VALUES (%s,'Visible','Test','Informatique','it.scope@test.local',CURRENT_DATE,1000)
            RETURNING id""", (MARK_IT,))
        emp_it = cur.fetchone()['id']
        cur.execute("""INSERT INTO employes
            (nom, prenom, poste, departement, email, date_embauche, salaire)
            VALUES (%s,'Secret','Test','Ressources Humaines','rh.scope@test.local',CURRENT_DATE,2000)
            RETURNING id""", (MARK_RH,))
        emp_rh = cur.fetchone()['id']

        cur.execute("""INSERT INTO presences
            (employe_id,date,heure_arrivee,heure_depart,statut)
            VALUES (%s,%s,'08:00','17:00','présent'),
                   (%s,%s,'08:00','17:00','présent')""",
                    (emp_it, today, emp_rh, today))
        for emp, marker in ((emp_it, MARK_IT), (emp_rh, MARK_RH)):
            cur.execute("""INSERT INTO conges
                (employe_id,type_conge,date_debut,date_fin,nombre_jours,motif,statut)
                VALUES (%s,'congé payé',%s,%s,1,%s,'approuvé') RETURNING id""",
                        (emp, today + timedelta(days=10), today + timedelta(days=10), marker))
            conge_id = cur.fetchone()['id']
            cur.execute("""INSERT INTO permissions
                (employe_id,motif,date_debut,date_fin,nombre_jours,statut)
                VALUES (%s,%s,%s,%s,1,'en attente') RETURNING id""",
                        (emp, marker, today + timedelta(days=5), today + timedelta(days=5)))
            permission_id = cur.fetchone()['id']
            cur.execute("""INSERT INTO absences (employe_id,date,motif,statut)
                           VALUES (%s,%s,%s,'non_justifiee') RETURNING id""",
                        (emp, today - timedelta(days=2), marker))
            absence_id = cur.fetchone()['id']
            cur.execute("""INSERT INTO soldes_conges
                (employe_id,annee,jours_acquis,jours_utilises)
                VALUES (%s,%s,20,2) ON CONFLICT (employe_id,annee) DO NOTHING""",
                        (emp, today.year))
            cur.execute("""INSERT INTO documents
                (employe_id,titre,nom_fichier,chemin_fichier,type_fichier,taille,contenu)
                VALUES (%s,%s,%s,%s,'pdf',4,%s) RETURNING id""",
                        (emp, marker, marker + '.pdf', marker + '.pdf', b'%PDF'))
            document_id = cur.fetchone()['id']
            if emp == emp_it:
                ids_it = {'conge': conge_id, 'permission': permission_id,
                          'absence': absence_id, 'document': document_id}
            else:
                ids_rh = {'conge': conge_id, 'permission': permission_id,
                          'absence': absence_id, 'document': document_id}

        def parc(marker, dept_id):
            cur.execute("""INSERT INTO materiels
                (nom,categorie,departement_id,quantite,seuil_alerte,suivi_unitaire)
                VALUES (%s,'informatique',%s,5,1,TRUE) RETURNING id""",
                        (marker + ' Material', dept_id))
            materiel_id = cur.fetchone()['id']
            cur.execute("""INSERT INTO materiel_exemplaires
                (materiel_id,numero_inventaire,etat)
                VALUES (%s,%s,'panne') RETURNING id""",
                        (materiel_id, marker + '-EX'))
            exemplaire_id = cur.fetchone()['id']
            cur.execute("""INSERT INTO materiel_maintenances
                (exemplaire_id,statut,panne) VALUES (%s,'signale',%s) RETURNING id""",
                        (exemplaire_id, marker))
            maintenance_id = cur.fetchone()['id']
            cur.execute("""INSERT INTO inventaires
                (reference,departement_id,statut) VALUES (%s,%s,'en_cours') RETURNING id""",
                        (marker + ' Inventory', dept_id))
            inventaire_id = cur.fetchone()['id']
            return {'materiel': materiel_id, 'exemplaire': exemplaire_id,
                    'maintenance': maintenance_id, 'inventaire': inventaire_id}

        ids_it.update(parc(MARK_IT, dept_it))
        ids_rh.update(parc(MARK_RH, dept_rh))
        cur.execute("UPDATE users SET photo='scope-rh.png', photo_contenu=%s WHERE username='rh'",
                    (b'fake-image',))

    return {'emp_it': emp_it, 'emp_rh': emp_rh, 'dept_it': dept_it,
            'dept_rh': dept_rh, 'it': ids_it, 'rh': ids_rh}


def _login_manager(client):
    client.post('/login', data={'username': 'manager', 'password': 'manager123'})


def test_toutes_les_listes_sont_limitees_au_departement(client):
    _seed_scope_data()
    _login_manager(client)
    routes = (
        '/employes', '/presences', '/conges', '/permissions', '/absences',
        '/soldes-conges', '/rapports?type=presences', '/documents', '/historique',
        '/departements', '/calendrier-conges', '/materiels', '/inventaires',
        '/maintenances', '/recherche?q=ScopeMarker',
    )
    for route in routes:
        response = client.get(route, follow_redirects=True)
        assert response.status_code == 200, route
        assert MARK_IT.encode() in response.data, route
        assert MARK_RH.encode() not in response.data, route


def test_api_recherche_ne_retourne_pas_autre_departement(client):
    _seed_scope_data()
    _login_manager(client)
    response = client.get('/api/recherche?q=ScopeMarker')
    assert response.status_code == 200
    payload = response.get_json()
    texte = str(payload)
    assert MARK_IT in texte
    assert MARK_RH not in texte


def test_selecteurs_et_formulaires_ne_proposent_que_le_departement(client):
    ids = _seed_scope_data()
    _login_manager(client)
    routes = (
        '/presences/add', '/conges/add', '/permissions/add', '/absences/add',
        '/materiels/add', '/inventaires/nouveau',
        f"/materiels/{ids['it']['materiel']}",
        f"/exemplaires/{ids['it']['exemplaire']}",
    )
    for route in routes:
        response = client.get(route, follow_redirects=True)
        assert response.status_code == 200, route
        assert MARK_RH.encode() not in response.data, route
        assert 'Ressources Humaines'.encode() not in response.data, route


def test_acces_direct_interdepartement_est_refuse(client):
    ids = _seed_scope_data()
    _login_manager(client)
    routes_interdites = (
        f"/employes/{ids['emp_rh']}",
        f"/materiels/{ids['rh']['materiel']}",
        f"/inventaires/{ids['rh']['inventaire']}",
        f"/exemplaires/{ids['rh']['exemplaire']}",
        f"/documents/file/{ids['rh']['document']}",
        f"/departements/{ids['dept_rh']}/materiels",
        '/avatar/scope-rh.png',
    )
    for route in routes_interdites:
        response = client.get(route, follow_redirects=False)
        assert response.status_code in (301, 302), route
        assert response.headers['Location'].endswith('/'), route

    # Une ressource du département courant reste accessible.
    assert client.get(f"/employes/{ids['emp_it']}").status_code == 200
    assert client.get(f"/materiels/{ids['it']['materiel']}").status_code == 200


def test_formulaires_forges_ne_peuvent_ecrire_hors_departement(client):
    ids = _seed_scope_data()
    _login_manager(client)
    future = date.today() + timedelta(days=40)

    response = client.post(f"/presences/clock_in/{ids['emp_rh']}",
                           data={'date': future.isoformat()}, follow_redirects=False)
    assert response.status_code in (301, 302)
    client.post('/conges/add', data={
        'employe_id': ids['emp_rh'], 'type_conge': 'congé payé',
        'date_debut': future.isoformat(), 'date_fin': future.isoformat(),
        'motif': 'FORGED-CROSS-DEPT',
    })
    client.post(f"/materiels/{ids['rh']['materiel']}/mouvement", data={
        'type_mouvement': 'sortie', 'quantite': 1, 'motif': 'FORGED-CROSS-DEPT',
    })

    with application.db_cursor() as (conn, cur):
        cur.execute("SELECT COUNT(*) AS n FROM presences WHERE employe_id=%s AND date=%s",
                    (ids['emp_rh'], future))
        assert cur.fetchone()['n'] == 0
        cur.execute("SELECT COUNT(*) AS n FROM conges WHERE motif='FORGED-CROSS-DEPT'")
        assert cur.fetchone()['n'] == 0
        cur.execute("SELECT quantite FROM materiels WHERE id=%s", (ids['rh']['materiel'],))
        assert cur.fetchone()['quantite'] == 5


@pytest.mark.parametrize('endpoint,factory_name', (
    ('/export/presences/pdf', 'create_presences_pdf'),
    ('/export/presences/excel', 'create_presences_excel'),
    ('/export/conges/pdf', 'create_conges_pdf'),
    ('/export/conges/excel', 'create_conges_excel'),
))
def test_exports_sont_limites_au_departement(client, monkeypatch, endpoint, factory_name):
    _seed_scope_data()
    _login_manager(client)
    captures = []

    def fake_factory(data, *args, **kwargs):
        captures.extend(data)
        return io.BytesIO(b'export-test')

    monkeypatch.setattr(application, factory_name, fake_factory)
    response = client.get(endpoint)
    assert response.status_code == 200
    noms = {row['nom'] for row in captures}
    assert MARK_IT in noms
    assert MARK_RH not in noms


def test_compte_sans_departement_a_une_portee_vide_partout(client):
    _seed_scope_data()
    with application.db_cursor(commit=True) as (conn, cur):
        cur.execute("""INSERT INTO users (username,password_hash,role,employe_id)
                       VALUES ('scope-empty',%s,'technicien',NULL)""",
                    (generate_password_hash('Secret12!'),))
    client.post('/login', data={'username': 'scope-empty', 'password': 'Secret12!'})
    for route in ('/employes', '/presences', '/documents', '/materiels', '/inventaires',
                  '/maintenances', '/recherche?q=ScopeMarker'):
        response = client.get(route, follow_redirects=True)
        assert response.status_code == 200
        assert MARK_IT.encode() not in response.data
        assert MARK_RH.encode() not in response.data


def test_admin_et_rh_conservent_acces_global(client):
    ids = _seed_scope_data()
    for username, password in (('admin', 'admin123'), ('rh', 'rh123')):
        client.get('/logout')
        client.post('/login', data={'username': username, 'password': password})
        response = client.get('/employes')
        assert MARK_IT.encode() in response.data
        assert MARK_RH.encode() in response.data
        assert client.get(f"/employes/{ids['emp_rh']}").status_code == 200
        assert client.get(f"/materiels/{ids['rh']['materiel']}").status_code == 200


def test_prestataires_globaux_reserves_admin_rh(client):
    _seed_scope_data()
    _login_manager(client)
    response = client.get('/prestataires', follow_redirects=False)
    assert response.status_code in (301, 302)
    assert response.headers['Location'].endswith('/')


def test_notifications_stock_ne_fuitent_pas_vers_autre_manager(client):
    ids = _seed_scope_data()
    with application.db_cursor(commit=True) as (conn, cur):
        cur.execute("""INSERT INTO users (username,password_hash,role,employe_id)
                       VALUES ('manager-rh-scope',%s,'manager',%s) RETURNING id""",
                    (generate_password_hash('Secret12!'), ids['emp_rh']))
        manager_rh = cur.fetchone()['id']
        cur.execute("""UPDATE materiels SET quantite=1, seuil_alerte=2,
                       alerte_envoyee=FALSE WHERE id=%s""", (ids['it']['materiel'],))
        application._notifier_stock_bas(cur, ids['it']['materiel'])
    with application.db_cursor() as (conn, cur):
        cur.execute("SELECT COUNT(*) AS n FROM notifications WHERE user_id=3 "
                    "AND title LIKE 'Stock bas%%'")
        assert cur.fetchone()['n'] == 1
        cur.execute("SELECT COUNT(*) AS n FROM notifications WHERE user_id=%s "
                    "AND title LIKE 'Stock bas%%'", (manager_rh,))
        assert cur.fetchone()['n'] == 0


def test_retrogradation_prend_effet_sans_reconnexion(client):
    _seed_scope_data()
    client.post('/login', data={'username': 'admin', 'password': 'admin123'})
    with application.db_cursor(commit=True) as (conn, cur):
        cur.execute("UPDATE users SET role='manager', employe_id=3 WHERE username='admin'")
    response = client.get('/employes')
    assert response.status_code == 200
    assert MARK_IT.encode() in response.data
    assert MARK_RH.encode() not in response.data
    with client.session_transaction() as sess:
        assert sess['role'] == 'manager'
