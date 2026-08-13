import io

from werkzeug.security import generate_password_hash

import app as application


def _login(client, username, password):
    return client.post('/login', data={'username': username, 'password': password})


def _client_connecte(app, username, password):
    client = app.test_client()
    _login(client, username, password)
    return client


def _creer_prive(client, destinataire=3, contenu='Message privé test', **extra):
    data = {
        'type': 'prive', 'destinataires': str(destinataire),
        'contenu': contenu, 'titre': '',
    }
    data.update(extra)
    return client.post('/messages/nouveau', data=data,
                       content_type='multipart/form-data', follow_redirects=False)


def _id_conversation():
    with application.db_cursor() as (conn, cur):
        cur.execute('SELECT id FROM conversations ORDER BY id DESC LIMIT 1')
        row = cur.fetchone()
    return row['id'] if row else None


def test_routes_messagerie_exigent_connexion(client):
    for route in ('/messages', '/messages/nouveau'):
        response = client.get(route, follow_redirects=False)
        assert response.status_code in (301, 302)
        assert '/login' in response.headers['Location']


def test_destinataires_sont_cloisonnes_par_departement(app):
    employe = _client_connecte(app, 'employe', 'user123')
    response = employe.get('/messages/nouveau')
    assert response.status_code == 200
    assert b'(manager)' in response.data
    assert b'(rh)' not in response.data
    assert b'(admin)' not in response.data

    admin = _client_connecte(app, 'admin', 'admin123')
    response = admin.get('/messages/nouveau')
    assert b'(manager)' in response.data
    assert b'(rh)' in response.data
    assert b'(employe)' in response.data


def test_creation_message_prive_membres_lecture_notification_et_audit(app):
    employe = _client_connecte(app, 'employe', 'user123')
    response = _creer_prive(employe)
    assert response.status_code in (301, 302)
    conv_id = _id_conversation()
    assert response.headers['Location'].endswith(f'/messages/{conv_id}')

    with application.db_cursor() as (conn, cur):
        cur.execute('SELECT type, cree_par FROM conversations WHERE id=%s', (conv_id,))
        conv = cur.fetchone()
        cur.execute('SELECT user_id, dernier_message_lu_id FROM conversation_membres '
                    'WHERE conversation_id=%s ORDER BY user_id', (conv_id,))
        membres = cur.fetchall()
        cur.execute('SELECT id, contenu FROM messages WHERE conversation_id=%s', (conv_id,))
        message = cur.fetchone()
        cur.execute("SELECT COUNT(*) AS n FROM notifications WHERE user_id=3 "
                    "AND title LIKE 'Nouveau message%%'")
        notifications = cur.fetchone()['n']
        cur.execute("SELECT COUNT(*) AS n FROM audit_logs WHERE action='CREATE_CONVERSATION' "
                    "AND entity_id=%s", (conv_id,))
        audit = cur.fetchone()['n']
    assert conv['type'] == 'prive' and conv['cree_par'] == 4
    assert [m['user_id'] for m in membres] == [3, 4]
    assert next(m for m in membres if m['user_id'] == 4)['dernier_message_lu_id'] == message['id']
    assert next(m for m in membres if m['user_id'] == 3)['dernier_message_lu_id'] is None
    assert message['contenu'] == 'Message privé test'
    assert notifications == 1
    assert audit == 1


def test_email_nouveau_message_est_mis_en_file_sans_smtp_synchrone(app):
    app.config['EMAIL_ENABLED'] = True
    employe = _client_connecte(app, 'employe', 'user123')
    _creer_prive(employe, contenu='Notification par email')
    with application.db_cursor() as (conn, cur):
        cur.execute("""SELECT destinataire, sujet, statut FROM email_outbox
                       WHERE destinataire='pierre.bernard@entreprise.fr'""")
        email = cur.fetchone()
    assert email is not None
    assert 'Nouveau message' in email['sujet']
    assert email['statut'] == 'en_attente'


def test_destinataire_hors_departement_ou_invalide_est_refuse(app):
    for destinataire in ('2', '999999', 'abc', '4'):
        employe = _client_connecte(app, 'employe', 'user123')
        response = _creer_prive(employe, destinataire=destinataire)
        assert response.status_code == 403
        with application.db_cursor() as (conn, cur):
            cur.execute('SELECT COUNT(*) AS n FROM conversations')
            assert cur.fetchone()['n'] == 0


def test_plusieurs_destinataires_transforment_le_prive_en_groupe(app):
    admin = _client_connecte(app, 'admin', 'admin123')
    response = admin.post('/messages/nouveau', data={
        'type': 'prive', 'destinataires': ['3', '4'],
        'contenu': 'Groupe automatique', 'titre': 'Projet',
    })
    assert response.status_code in (301, 302)
    conv_id = _id_conversation()
    with application.db_cursor() as (conn, cur):
        cur.execute('SELECT type FROM conversations WHERE id=%s', (conv_id,))
        assert cur.fetchone()['type'] == 'groupe'
        cur.execute('SELECT COUNT(*) AS n FROM conversation_membres WHERE conversation_id=%s',
                    (conv_id,))
        assert cur.fetchone()['n'] == 3


def test_conversation_privee_et_piece_jointe_inaccessibles_aux_non_membres(app):
    employe = _client_connecte(app, 'employe', 'user123')
    response = employe.post('/messages/nouveau', data={
        'type': 'prive', 'destinataires': '3', 'contenu': '',
        'piece_jointe': (io.BytesIO(b'%PDF-1.4\npiece-test'), 'preuve.pdf'),
    }, content_type='multipart/form-data')
    assert response.status_code in (301, 302)
    conv_id = _id_conversation()
    with application.db_cursor() as (conn, cur):
        cur.execute('SELECT id, piece_jointe_contenu FROM messages WHERE conversation_id=%s',
                    (conv_id,))
        message = cur.fetchone()
    assert bytes(message['piece_jointe_contenu']).startswith(b'%PDF')

    manager = _client_connecte(app, 'manager', 'manager123')
    download = manager.get(f"/messages/piece-jointe/{message['id']}")
    assert download.status_code == 200
    assert download.data.startswith(b'%PDF')
    assert download.headers['Cache-Control'] == 'private, no-store'

    rh = _client_connecte(app, 'rh', 'rh123')
    assert rh.get(f'/messages/{conv_id}', follow_redirects=False).status_code in (301, 302)
    assert rh.post(f'/messages/{conv_id}/repondre', data={'contenu': 'intrusion'}).status_code == 403
    assert rh.get(f"/messages/piece-jointe/{message['id']}").status_code == 403


def test_lecture_et_reponse_met_a_jour_le_non_lu(app):
    employe = _client_connecte(app, 'employe', 'user123')
    _creer_prive(employe, contenu='À lire')
    conv_id = _id_conversation()

    manager = _client_connecte(app, 'manager', 'manager123')
    inbox = manager.get('/messages')
    assert b'background:#eff6ff' in inbox.data
    manager.get(f'/messages/{conv_id}')
    with application.db_cursor() as (conn, cur):
        cur.execute('SELECT dernier_message_lu_id FROM conversation_membres '
                    'WHERE conversation_id=%s AND user_id=3', (conv_id,))
        lu = cur.fetchone()['dernier_message_lu_id']
        cur.execute('SELECT MAX(id) AS dernier FROM messages WHERE conversation_id=%s', (conv_id,))
        assert lu == cur.fetchone()['dernier']

    response = manager.post(f'/messages/{conv_id}/repondre', data={'contenu': 'Réponse'})
    assert response.status_code in (301, 302)
    with application.db_cursor() as (conn, cur):
        cur.execute("SELECT COUNT(*) AS n FROM notifications WHERE user_id=4 "
                    "AND message='Réponse'")
        assert cur.fetchone()['n'] == 1


def test_annonce_ciblee_visible_par_cible_et_par_admin_rh(app):
    rh = _client_connecte(app, 'rh', 'rh123')
    response = rh.post('/messages/nouveau', data={
        'type': 'annonce', 'cible_role': 'manager',
        'titre': 'Annonce managers', 'contenu': 'Réunion managers',
    }, follow_redirects=True)
    assert response.status_code == 200
    assert 'Annonce managers'.encode() in response.data
    conv_id = _id_conversation()

    manager = _client_connecte(app, 'manager', 'manager123')
    assert 'Annonce managers'.encode() in manager.get('/messages').data
    employe = _client_connecte(app, 'employe', 'user123')
    assert 'Annonce managers'.encode() not in employe.get('/messages').data
    admin = _client_connecte(app, 'admin', 'admin123')
    assert 'Annonce managers'.encode() in admin.get('/messages').data
    assert admin.get(f'/messages/{conv_id}').status_code == 200

    with application.db_cursor() as (conn, cur):
        cur.execute("SELECT COUNT(*) AS n FROM notifications WHERE user_id=3 "
                    "AND title LIKE '%%Annonce%%'")
        assert cur.fetchone()['n'] == 1
        cur.execute("SELECT COUNT(*) AS n FROM notifications WHERE user_id=4 "
                    "AND title LIKE '%%Annonce%%'")
        assert cur.fetchone()['n'] == 0


def test_annonce_technicien_et_role_invalide(app):
    with application.db_cursor(commit=True) as (conn, cur):
        cur.execute("""INSERT INTO employes (nom,prenom,departement)
                       VALUES ('Tech','Test','Informatique') RETURNING id""")
        emp_id = cur.fetchone()['id']
        cur.execute("""INSERT INTO users (username,password_hash,role,employe_id)
                       VALUES ('tech-msg',%s,'technicien',%s) RETURNING id""",
                    (generate_password_hash('Secret12!'), emp_id))
        tech_id = cur.fetchone()['id']

    rh = _client_connecte(app, 'rh', 'rh123')
    ok = rh.post('/messages/nouveau', data={
        'type': 'annonce', 'cible_role': 'technicien',
        'titre': 'Annonce tech', 'contenu': 'Maintenance',
    })
    assert ok.status_code in (301, 302)
    with application.db_cursor() as (conn, cur):
        cur.execute('SELECT COUNT(*) AS n FROM notifications WHERE user_id=%s', (tech_id,))
        assert cur.fetchone()['n'] == 1

    # Une cible inconnue ne doit surtout pas devenir une diffusion globale.
    invalide = rh.post('/messages/nouveau', data={
        'type': 'annonce', 'cible_role': 'superadmin-invalide',
        'titre': 'Ne pas diffuser', 'contenu': 'Secret',
    }, follow_redirects=True)
    assert invalide.status_code == 200
    assert 'Rôle destinataire invalide'.encode() in invalide.data
    with application.db_cursor() as (conn, cur):
        cur.execute("SELECT COUNT(*) AS n FROM conversations WHERE titre='Ne pas diffuser'")
        assert cur.fetchone()['n'] == 0


def test_seuls_admin_rh_repondent_aux_annonces_et_la_cible_est_notifiee(app):
    rh = _client_connecte(app, 'rh', 'rh123')
    rh.post('/messages/nouveau', data={
        'type': 'annonce', 'cible_role': 'manager',
        'titre': 'Annonce évolutive', 'contenu': 'Version 1',
    })
    conv_id = _id_conversation()

    manager = _client_connecte(app, 'manager', 'manager123')
    assert manager.post(f'/messages/{conv_id}/repondre',
                        data={'contenu': 'interdit'}).status_code == 403

    admin = _client_connecte(app, 'admin', 'admin123')
    assert admin.post(f'/messages/{conv_id}/repondre',
                      data={'contenu': 'Version 2'}).status_code in (301, 302)
    with application.db_cursor() as (conn, cur):
        cur.execute("SELECT COUNT(*) AS n FROM notifications WHERE user_id=3 "
                    "AND title LIKE '%%mise à jour%%'")
        assert cur.fetchone()['n'] == 1
        cur.execute('SELECT COUNT(*) AS n FROM annonce_lues '
                    'WHERE conversation_id=%s AND user_id=1', (conv_id,))
        assert cur.fetchone()['n'] == 1


def test_quitter_groupe_exige_etre_membre(app):
    admin = _client_connecte(app, 'admin', 'admin123')
    admin.post('/messages/nouveau', data={
        'type': 'groupe', 'destinataires': ['3', '4'],
        'titre': 'Groupe sortie', 'contenu': 'Bonjour',
    })
    conv_id = _id_conversation()

    rh = _client_connecte(app, 'rh', 'rh123')
    assert rh.post(f'/messages/{conv_id}/quitter').status_code == 403

    manager = _client_connecte(app, 'manager', 'manager123')
    assert manager.post(f'/messages/{conv_id}/quitter').status_code in (301, 302)
    with application.db_cursor() as (conn, cur):
        cur.execute('SELECT COUNT(*) AS n FROM conversation_membres '
                    'WHERE conversation_id=%s AND user_id=3', (conv_id,))
        assert cur.fetchone()['n'] == 0


def test_employe_ne_peut_pas_creer_annonce(app):
    employe = _client_connecte(app, 'employe', 'user123')
    response = employe.post('/messages/nouveau', data={
        'type': 'annonce', 'titre': 'Interdit', 'contenu': 'Non',
    })
    assert response.status_code == 403


def test_suppression_compte_anonymise_messages_sans_casser_conversation(app):
    with application.db_cursor(commit=True) as (conn, cur):
        cur.execute("""INSERT INTO employes (nom,prenom,departement)
                       VALUES ('Ancien','Compte','Informatique') RETURNING id""")
        employe_id = cur.fetchone()['id']
        cur.execute("""INSERT INTO users (username,password_hash,role,employe_id)
                       VALUES ('ancien-msg',%s,'employe',%s) RETURNING id""",
                    (generate_password_hash('Secret12!'), employe_id))
        ancien_user_id = cur.fetchone()['id']

    ancien = _client_connecte(app, 'ancien-msg', 'Secret12!')
    _creer_prive(ancien, destinataire=3, contenu='Message à conserver')
    conv_id = _id_conversation()

    admin = _client_connecte(app, 'admin', 'admin123')
    response = admin.post(f'/utilisateurs/{ancien_user_id}/delete')
    assert response.status_code in (301, 302)
    with application.db_cursor() as (conn, cur):
        cur.execute('SELECT sender_id FROM messages WHERE conversation_id=%s', (conv_id,))
        assert cur.fetchone()['sender_id'] is None
        cur.execute('SELECT cree_par FROM conversations WHERE id=%s', (conv_id,))
        assert cur.fetchone()['cree_par'] is None

    manager = _client_connecte(app, 'manager', 'manager123')
    thread = manager.get(f'/messages/{conv_id}')
    assert thread.status_code == 200
    assert 'Utilisateur supprimé'.encode() in thread.data
    assert 'Message à conserver'.encode() in thread.data
