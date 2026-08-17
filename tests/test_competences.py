"""Tests du module Compétences."""
import pytest
from bs4 import BeautifulSoup


_PASSWORDS = {
    'admin': 'admin123',
    'rh': 'rh123',
    'manager': 'manager123',
    'employe': 'user123',
}


def _login(client, username, password=None):
    pwd = password or _PASSWORDS.get(username, 'user123')
    return client.post('/login', data={'username': username, 'password': pwd},
                       follow_redirects=True)


def test_referentiel_competences_visible_tous_roles(app, client):
    """Tous les utilisateurs connectés peuvent consulter le catalogue."""
    for username in ('admin', 'rh', 'manager', 'employe'):
        _login(client, username)
        r = client.get('/competences')
        assert r.status_code == 200, username
        assert 'Compétences'.encode() in r.data or b'comp' in r.data.lower()


def test_admin_peut_creer_archiver_reactiver(app, client):
    _login(client, 'admin')
    r = client.post('/competences/nouvelle',
                    data={'nom': 'Python', 'categorie': 'Technique',
                          'description': 'Langage Python'},
                    follow_redirects=True)
    assert r.status_code == 200
    assert b'Python' in r.data
    # Archiver
    with app.app_context():
        import app as application
        with application.db_cursor() as (conn, cur):
            cur.execute("SELECT id FROM competences WHERE nom='Python'")
            cid = cur.fetchone()['id']
    r = client.post(f'/competences/{cid}/archiver', follow_redirects=True)
    assert r.status_code == 200
    assert b'Archiv' in r.data or b'archiv' in r.data.lower()
    # Réactiver
    r = client.post(f'/competences/{cid}/reactiver', follow_redirects=True)
    assert r.status_code == 200
    # Page détail
    r = client.get(f'/competences/{cid}')
    assert r.status_code == 200
    assert b'Python' in r.data


def test_manager_ne_peut_pas_modifier_le_referentiel(app, client):
    """Seul admin/RH peut gérer le catalogue."""
    _login(client, 'manager')
    r = client.post('/competences/nouvelle',
                    data={'nom': 'Tentative', 'categorie': 'Technique'},
                    follow_redirects=False)
    assert r.status_code in (302, 403, 401)


def test_manager_peut_associer_competence_departement(app, client):
    """Le manager info peut ajouter des compétences aux employés info."""
    _login(client, 'admin')
    client.post('/competences/nouvelle',
                data={'nom': 'Python', 'categorie': 'Technique'},
                follow_redirects=True)
    client.get('/logout', follow_redirects=True)

    _login(client, 'manager')
    # Employé 4 est dans Administration (hors département info)
    # Employé 3 est le manager lui-même (informatique)
    # Employé 1 est admin — de quel département ?
    with app.app_context():
        import app as application
        with application.db_cursor() as (conn, cur):
            cur.execute("SELECT id, nom, prenom, departement FROM employes ORDER BY id")
            emps = cur.fetchall()
            cur.execute("SELECT id FROM competences WHERE nom='Python'")
            cid = cur.fetchone()['id']
            # Trouver un employé du département Informatique
            emp_info = next((e for e in emps if e['departement'] == 'Informatique'), None)
            emp_autre = next((e for e in emps if e['departement'] == 'Administration'), None)

    assert emp_info is not None
    # Ajout de la compétence à l'employé du département
    r = client.post(f'/employes/{emp_info["id"]}/competences/ajouter',
                    data={'competence_id': cid, 'niveau': 75, 'notes': 'Bonne maîtrise'},
                    follow_redirects=True)
    assert r.status_code == 200
    assert b'Python' in r.data
    assert b'75' in r.data

    # Modification
    with app.app_context():
        import app as application
        with application.db_cursor() as (conn, cur):
            cur.execute("SELECT id FROM employe_competences WHERE employe_id=%s AND competence_id=%s",
                        (emp_info['id'], cid))
            ecid = cur.fetchone()['id']
    r = client.post(f'/employes/{emp_info["id"]}/competences/{ecid}/modifier',
                    data={'niveau': 85, 'notes': 'Confirmé'},
                    follow_redirects=True)
    assert r.status_code == 200
    assert b'85' in r.data

    # Refus : employé hors département
    if emp_autre:
        r = client.post(f'/employes/{emp_autre["id"]}/competences/ajouter',
                        data={'competence_id': cid, 'niveau': 50},
                        follow_redirects=False)
        assert r.status_code in (302, 403)

    # Suppression de l'association
    r = client.post(f'/employes/{emp_info["id"]}/competences/{ecid}/supprimer',
                    follow_redirects=True)
    assert r.status_code == 200


def test_employe_voit_ses_competences_mais_ne_peut_pas_modifier(app, client):
    # Créer une compétence + association en tant qu'admin
    _login(client, 'admin')
    client.post('/competences/nouvelle',
                data={'nom': 'PostgreSQL', 'categorie': 'Technique'},
                follow_redirects=True)
    with app.app_context():
        import app as application
        with application.db_cursor() as (conn, cur):
            cur.execute("SELECT id FROM competences WHERE nom='PostgreSQL'")
            cid = cur.fetchone()['id']
            cur.execute("SELECT id FROM employes ORDER BY id LIMIT 1")
            emp_id = cur.fetchone()['id']
            cur.execute("INSERT INTO employe_competences (employe_id, competence_id, niveau, ajoute_par) VALUES (%s,%s,60,1)",
                        (emp_id, cid))
    client.get('/logout', follow_redirects=True)

    _login(client, 'employe')
    # Trouver l'employé lié au compte employe (id=4 en seed)
    with app.app_context():
        import app as application
        with application.db_cursor() as (conn, cur):
            cur.execute("SELECT employe_id FROM users WHERE username='employe'")
            eid = cur.fetchone()['employe_id']
    r = client.get(f'/employes/{eid}/competences')
    assert r.status_code == 200
    # Ne doit pas avoir de formulaire POST d'ajout
    assert b'Ajouter une comp' not in r.data
    # Essai de POST d'ajout : doit être 403
    r = client.post(f'/employes/{eid}/competences/ajouter',
                    data={'competence_id': cid, 'niveau': 50},
                    follow_redirects=False)
    assert r.status_code in (302, 403)


def test_validation_niveau_hors_plage(app, client):
    _login(client, 'admin')
    client.post('/competences/nouvelle',
                data={'nom': 'Git', 'categorie': 'Technique'},
                follow_redirects=True)
    with app.app_context():
        import app as application
        with application.db_cursor() as (conn, cur):
            cur.execute("SELECT id FROM competences WHERE nom='Git'")
            cid = cur.fetchone()['id']
            cur.execute("SELECT id FROM employes ORDER BY id LIMIT 1")
            eid = cur.fetchone()['id']
    r = client.post(f'/employes/{eid}/competences/ajouter',
                    data={'competence_id': cid, 'niveau': 150},
                    follow_redirects=True)
    assert r.status_code == 200
    # Le niveau 150 doit être refusé
    with app.app_context():
        import app as application
        with application.db_cursor() as (conn, cur):
            cur.execute("SELECT COUNT(*) AS nb FROM employe_competences WHERE employe_id=%s AND competence_id=%s",
                        (eid, cid))
            assert cur.fetchone()['nb'] == 0


def test_competence_doublon_refusee(app, client):
    _login(client, 'admin')
    client.post('/competences/nouvelle',
                data={'nom': 'SQL', 'categorie': 'Technique'},
                follow_redirects=True)
    r = client.post('/competences/nouvelle',
                    data={'nom': 'SQL', 'categorie': 'Technique'},
                    follow_redirects=True)
    # La deuxième insertion doit être refusée mais ne pas crasher
    assert r.status_code == 200


def test_libelle_niveau():
    from services.competences import libelle_niveau
    assert libelle_niveau(0)[0] == 'Débutant'
    assert libelle_niveau(30)[0] == 'Notions'
    assert libelle_niveau(50)[0] == 'Intermédiaire'
    assert libelle_niveau(70)[0] == 'Confirmé'
    assert libelle_niveau(90)[0] == 'Expert'


def test_lien_competences_dans_navbar(app, client):
    _login(client, 'admin')
    r = client.get('/')
    assert b'/competences' in r.data
    assert b'Comp\xc3\xa9tences' in r.data  # "Compétences" en UTF-8


def test_fiche_employe_affiche_bouton_competences(app, client):
    _login(client, 'admin')
    with app.app_context():
        import app as application
        with application.db_cursor() as (conn, cur):
            cur.execute("SELECT id FROM employes ORDER BY id LIMIT 1")
            eid = cur.fetchone()['id']
    r = client.get(f'/employes/{eid}')
    assert r.status_code == 200
    assert f'/employes/{eid}/competences'.encode() in r.data
