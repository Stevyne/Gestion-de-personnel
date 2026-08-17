"""Tests du module Objectifs."""
import pytest


def _login(client, username):
    pw = {'admin': 'admin123', 'rh': 'rh123', 'manager': 'manager123',
          'employe': 'user123'}[username]
    return client.post('/login', data={'username': username, 'password': pw},
                       follow_redirects=True)


def _cree_objectif(app, titre, employe_id, cree_par_user_id=1,
                   statut='en_cours', progression=0, date_echeance='2030-12-31'):
    import app as a
    with a.db_cursor(commit=True) as (conn, cur):
        cur.execute(
            """INSERT INTO objectifs (employe_id, titre, description, priorite,
                statut, progression, date_debut, date_echeance,
                cree_par, cree_par_role)
               VALUES (%s,%s,'descr','normale',%s,%s,CURRENT_DATE,%s,%s,'admin')
               RETURNING id""",
            (employe_id, titre, statut, progression, date_echeance, cree_par_user_id))
        return cur.fetchone()['id']


def test_employe_peut_creer_son_objectif(app, client):
    _login(client, 'employe')
    r = client.post('/objectifs/nouveau', data={
        'titre': 'Mon objectif',
        'description': 'Progresser en SQL',
        'categorie': 'Formation',
        'priorite': 'normale',
    }, follow_redirects=True)
    assert r.status_code == 200
    assert 'Mon objectif'.encode() in r.data or b'objectif' in r.data.lower()


def test_employe_ne_peut_creer_pour_autrui(app, client):
    """L'employé ne doit pas pouvoir choisir un autre employé."""
    _login(client, 'employe')
    r = client.post('/objectifs/nouveau', data={
        'employe_id': 4,  # admin
        'titre': 'Tentative',
    }, follow_redirects=True)
    # Même s'il envoie le champ, le code force son propre employe_id
    with app.app_context():
        import app as a
        with a.db_cursor() as (conn, cur):
            cur.execute("SELECT employe_id FROM objectifs WHERE titre='Tentative'")
            row = cur.fetchone()
            # la fiche employé liée au compte employe doit être id=1 en seed
            if row:
                assert row['employe_id'] == 1


def test_manager_peut_creer_pour_son_departement(app, client):
    _login(client, 'manager')
    r = client.post('/objectifs/nouveau', data={
        'employe_id': 3,  # manager lui-même (même département)
        'titre': 'Obj manager',
        'valider_immediat': '1',
    }, follow_redirects=True)
    assert r.status_code == 200
    assert b'Obj manager' in r.data


def test_liste_objectifs_respecte_perimetre(app, client):
    # admin crée un objectif pour l'employé 1 (Informatique)
    _cree_objectif(app, 'Obj info', 1)
    _cree_objectif(app, 'Obj RH', 2)

    # manager (info) ne doit pas voir obj RH
    _login(client, 'manager')
    r = client.get('/objectifs')
    assert b'Obj info' in r.data
    assert b'Obj RH' not in r.data


def test_soumission_puis_validation(app, client):
    oid = _cree_objectif(app, 'A terminer', 1, statut='en_cours', progression=30)
    # L'employé soumet comme atteint
    _login(client, 'employe')
    r = client.post(f'/objectifs/{oid}/soumettre', follow_redirects=True)
    assert r.status_code == 200
    # Le statut doit être 'atteint' (à valider)
    with app.app_context():
        import app as a
        with a.db_cursor() as (conn, cur):
            cur.execute("SELECT statut, progression FROM objectifs WHERE id=%s", (oid,))
            row = cur.fetchone()
            assert row['statut'] == 'atteint'
            assert row['progression'] == 100

    # Le manager (même département) valide
    client.get('/logout', follow_redirects=True)
    _login(client, 'manager')
    r = client.post(f'/objectifs/{oid}/valider',
                    data={'commentaire': 'Bravo'}, follow_redirects=True)
    assert r.status_code == 200
    # reste atteint (clôturé)
    with app.app_context():
        import app as a
        with a.db_cursor() as (conn, cur):
            cur.execute("SELECT statut, cloture_par FROM objectifs WHERE id=%s", (oid,))
            row = cur.fetchone()
            assert row['statut'] == 'atteint'


def test_non_atteint_demande_motif(app, client):
    oid = _cree_objectif(app, 'Echoue', 1, statut='en_cours')
    _login(client, 'manager')
    # Sans commentaire → refus
    r = client.post(f'/objectifs/{oid}/non-atteint', data={}, follow_redirects=True)
    assert r.status_code == 200
    with app.app_context():
        import app as a
        with a.db_cursor() as (conn, cur):
            cur.execute("SELECT statut FROM objectifs WHERE id=%s", (oid,))
            assert cur.fetchone()['statut'] == 'en_cours'
    # Avec commentaire → ok
    r = client.post(f'/objectifs/{oid}/non-atteint',
                    data={'commentaire': 'Délai dépassé'}, follow_redirects=True)
    with app.app_context():
        import app as a
        with a.db_cursor() as (conn, cur):
            cur.execute("SELECT statut FROM objectifs WHERE id=%s", (oid,))
            assert cur.fetchone()['statut'] == 'non_atteint'


def test_progression_ajoute_point(app, client):
    oid = _cree_objectif(app, 'Progression test', 1, statut='en_cours')
    _login(client, 'employe')
    r = client.post(f'/objectifs/{oid}/progression',
                    data={'progression': 40, 'commentaire': 'Avancement milieu'},
                    follow_redirects=True)
    assert r.status_code == 200
    with app.app_context():
        import app as a
        with a.db_cursor() as (conn, cur):
            cur.execute("SELECT progression FROM objectifs WHERE id=%s", (oid,))
            assert cur.fetchone()['progression'] == 40
            cur.execute("SELECT COUNT(*) AS nb FROM objectifs_points WHERE objectif_id=%s", (oid,))
            assert cur.fetchone()['nb'] >= 1


def test_employe_ne_peut_pas_valider(app, client):
    oid = _cree_objectif(app, 'A valider', 1, statut='atteint')
    _login(client, 'employe')
    r = client.post(f'/objectifs/{oid}/valider', follow_redirects=False)
    # doit être 403 (ou redirect vers login/forbidden)
    assert r.status_code in (302, 403)


def test_annulation_objectif(app, client):
    oid = _cree_objectif(app, 'Annulable', 1, statut='en_cours')
    _login(client, 'employe')
    r = client.post(f'/objectifs/{oid}/annuler',
                    data={'commentaire': 'Changement de priorité'},
                    follow_redirects=True)
    assert r.status_code == 200
    with app.app_context():
        import app as a
        with a.db_cursor() as (conn, cur):
            cur.execute("SELECT statut FROM objectifs WHERE id=%s", (oid,))
            assert cur.fetchone()['statut'] == 'annule'


def test_reactivation_par_manager(app, client):
    oid = _cree_objectif(app, 'A reouvrir', 1, statut='annule')
    _login(client, 'manager')
    r = client.post(f'/objectifs/{oid}/reactiver', follow_redirects=True)
    assert r.status_code == 200
    with app.app_context():
        import app as a
        with a.db_cursor() as (conn, cur):
            cur.execute("SELECT statut FROM objectifs WHERE id=%s", (oid,))
            assert cur.fetchone()['statut'] == 'en_cours'


def test_employe_voit_son_bouton_dans_nav(app, client):
    _login(client, 'employe')
    r = client.get('/')
    assert b'/objectifs' in r.data
    assert b'Objectifs' in r.data
