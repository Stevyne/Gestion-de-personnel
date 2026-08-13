import app as application


def _creer_employe_sans_compte(email='nouveau.compte@example.test'):
    with application.db_cursor(commit=True) as (conn, cur):
        cur.execute("""
            INSERT INTO employes
                (nom, prenom, poste, departement, email, date_embauche)
            VALUES ('Compte', 'Nouveau', 'Analyste', 'Informatique', %s, CURRENT_DATE)
            RETURNING id
        """, (email,))
        return cur.fetchone()['id']


def _donnees_valides(**surcharge):
    data = {
        'username': 'nouveau.compte',
        'password': 'Secret12!',
        'confirm_password': 'Secret12!',
        'role': 'employe',
        'employe_id': '',
    }
    data.update(surcharge)
    return data


def test_formulaire_creation_affiche_role_et_employes_disponibles(admin_client):
    employe_id = _creer_employe_sans_compte()
    response = admin_client.get('/register')
    assert response.status_code == 200
    assert b'name="csrf_token"' in response.data
    assert b'name="role"' in response.data
    assert b'name="employe_id"' in response.data
    assert f'value="{employe_id}"'.encode() in response.data
    # Jean Dupont (id=1) possède déjà le compte "employe" : il ne doit pas
    # pouvoir être lié à un deuxième compte depuis le formulaire.
    assert b'value="1"' not in response.data


def test_lien_nouvel_utilisateur_ouvre_popup(admin_client):
    response = admin_client.get('/utilisateurs')
    assert response.status_code == 200
    assert b'href="/register" class="js-modal-form btn btn-primary"' in response.data
    assert b'data-modal-title="Cr\xc3\xa9er un compte utilisateur"' in response.data


def test_rendu_modal_ne_contient_pas_navigation(admin_client):
    response = admin_client.get('/register?modal=1', headers={
        'X-Requested-With': 'XMLHttpRequest',
    })
    assert response.status_code == 200
    assert b'id="registerForm"' in response.data
    assert b'<!DOCTYPE html>' not in response.data
    assert b'class="navbar"' not in response.data
    assert b'<footer>' not in response.data


def test_erreur_validation_reste_dans_layout_modal(admin_client):
    response = admin_client.post('/register?modal=1', data=_donnees_valides(
        password='court', confirm_password='court'), headers={
        'X-Requested-With': 'XMLHttpRequest',
    })
    assert response.status_code == 200
    assert 'entre 8 et 128 caractères'.encode('utf-8') in response.data
    assert b'id="registerForm"' in response.data
    assert b'class="navbar"' not in response.data


def test_creation_popup_renvoie_redirection_ajax(admin_client):
    employe_id = _creer_employe_sans_compte()
    response = admin_client.post('/register?modal=1', data=_donnees_valides(
        employe_id=str(employe_id)), headers={
        'X-Requested-With': 'XMLHttpRequest',
    }, follow_redirects=False)
    assert response.status_code == 204
    assert response.headers['X-Redirect-To'].endswith('/utilisateurs')


def test_creation_enregistre_role_liaison_audit_et_notification(admin_client):
    employe_id = _creer_employe_sans_compte()
    response = admin_client.post('/register', data=_donnees_valides(
        username='Nouveau.Compte', role='manager', employe_id=str(employe_id),
    ), follow_redirects=False)
    assert response.status_code in (301, 302)
    assert response.headers['Location'].endswith('/utilisateurs')

    with application.db_cursor() as (conn, cur):
        cur.execute("SELECT id, username, role, employe_id FROM users "
                    "WHERE username = 'nouveau.compte'")
        user = cur.fetchone()
        cur.execute("SELECT COUNT(*) AS n FROM audit_logs "
                    "WHERE action = 'CREATE_USER' AND entity_id = %s", (user['id'],))
        audit = cur.fetchone()['n']
        cur.execute("SELECT COUNT(*) AS n FROM notifications "
                    "WHERE user_id = %s AND title = 'Votre compte a été créé'", (user['id'],))
        notification = cur.fetchone()['n']
    assert user['role'] == 'manager'
    assert user['employe_id'] == employe_id
    assert audit == 1
    assert notification == 1


def test_employe_simple_ne_peut_pas_creer_compte(employe_client):
    response = employe_client.get('/register', follow_redirects=False)
    assert response.status_code in (301, 302)
    assert response.headers['Location'].endswith('/')


def test_rh_ne_peut_pas_creer_administrateur(client):
    client.post('/login', data={'username': 'rh', 'password': 'rh123'})
    response = client.post('/register', data=_donnees_valides(role='admin'),
                           follow_redirects=True)
    assert response.status_code == 200
    assert 'Seul un administrateur'.encode('utf-8') in response.data
    with application.db_cursor() as (conn, cur):
        cur.execute("SELECT COUNT(*) AS n FROM users WHERE username = 'nouveau.compte'")
        assert cur.fetchone()['n'] == 0


def test_employe_deja_lie_est_refuse(admin_client):
    response = admin_client.post('/register', data=_donnees_valides(employe_id='1'),
                                 follow_redirects=True)
    assert response.status_code == 200
    assert 'déjà lié au compte'.encode('utf-8') in response.data
    with application.db_cursor() as (conn, cur):
        cur.execute("SELECT COUNT(*) AS n FROM users WHERE username = 'nouveau.compte'")
        assert cur.fetchone()['n'] == 0


def test_identifiant_trop_long_ne_divulgue_pas_erreur_sql(admin_client):
    response = admin_client.post('/register', data=_donnees_valides(username='x' * 81),
                                 follow_redirects=True)
    assert response.status_code == 200
    assert '3 à 80 caractères'.encode('utf-8') in response.data
    assert b'value too long' not in response.data


def test_doublon_est_insensible_a_la_casse(admin_client):
    response = admin_client.post('/register', data=_donnees_valides(username='ADMIN'),
                                 follow_redirects=True)
    assert response.status_code == 200
    assert "déjà utilisé".encode('utf-8') in response.data


def test_mot_de_passe_minimum_huit_caracteres(admin_client):
    response = admin_client.post('/register', data=_donnees_valides(
        password='Abc123!', confirm_password='Abc123!'), follow_redirects=True)
    assert response.status_code == 200
    assert 'entre 8 et 128 caractères'.encode('utf-8') in response.data
