from pathlib import Path

import app as application


def _login(client, username, password):
    client.post('/login', data={'username': username, 'password': password})


def _exemplaire(numero='P3-001', departement='Informatique'):
    with application.db_cursor(commit=True) as (conn, cur):
        cur.execute("INSERT INTO departements(nom) VALUES (%s) ON CONFLICT(nom) DO UPDATE SET nom=EXCLUDED.nom RETURNING id", (departement,))
        dept = cur.fetchone()['id']
        cur.execute("INSERT INTO materiels(nom,categorie,departement_id,quantite,suivi_unitaire) VALUES (%s,'informatique',%s,1,TRUE) RETURNING id", ('PC '+numero,dept))
        materiel = cur.fetchone()['id']
        cur.execute("INSERT INTO materiel_exemplaires(materiel_id,numero_inventaire,etat) VALUES (%s,%s,'bon') RETURNING id", (materiel,numero))
        return materiel, cur.fetchone()['id']


def test_ticket_cree_automatiquement_groupe_messagerie_et_assigne(client):
    _, exemplaire = _exemplaire()
    _login(client,'admin','admin123')
    client.post(f'/exemplaires/{exemplaire}/panne', data={'panne':'Panne réseau','priorite':'haute'})
    with application.db_cursor() as (conn, cur):
        cur.execute("SELECT id,conversation_id,reference FROM materiel_maintenances WHERE exemplaire_id=%s", (exemplaire,))
        ticket = cur.fetchone()
        cur.execute("SELECT contexte_type,contexte_id FROM conversations WHERE id=%s", (ticket['conversation_id'],))
        conversation = cur.fetchone()
    assert conversation['contexte_type']=='maintenance' and conversation['contexte_id']==ticket['id']

    client.post(f"/maintenances/{ticket['id']}/assigner", data={'cible':'interne','assigne_user_id':'3'})
    with application.db_cursor() as (conn, cur):
        cur.execute("SELECT user_id FROM conversation_membres WHERE conversation_id=%s", (ticket['conversation_id'],))
        membres={r['user_id'] for r in cur.fetchall()}
    assert {1,2,3} <= membres
    thread=client.get(f"/messages/{ticket['conversation_id']}")
    assert ticket['reference'].encode() in thread.data
    assert b'Discussion ouverte pour' in thread.data


def test_pagination_messages_charge_par_blocs_de_50(app):
    employe=app.test_client(); _login(employe,'employe','user123')
    employe.post('/messages/nouveau', data={'type':'prive','destinataires':'3','contenu':'Initial'})
    with application.db_cursor(commit=True) as (conn, cur):
        cur.execute("SELECT id FROM conversations ORDER BY id DESC LIMIT 1")
        conv=cur.fetchone()['id']
        for i in range(120):
            cur.execute("INSERT INTO messages(conversation_id,sender_id,contenu) VALUES (%s,4,%s)", (conv,f'Message paginé {i}'))
    manager=app.test_client(); _login(manager,'manager','manager123')
    page1=manager.get(f'/messages/{conv}')
    page2=manager.get(f'/messages/{conv}?page_messages=2')
    page3=manager.get(f'/messages/{conv}?page_messages=3')
    assert page1.data.count(b'data-message-id=')==50
    assert page2.data.count(b'data-message-id=')==100
    assert page3.data.count(b'data-message-id=')==121
    assert b'Charger 50 messages' in page1.data and b'Charger 50 messages' in page2.data
    assert b'Charger 50 messages' not in page3.data


def test_impression_etiquettes_qr_et_format_physique(admin_client):
    materiel, _ = _exemplaire('QR-P3-001')
    response=admin_client.get(f'/materiels/{materiel}/etiquettes?print=1')
    assert response.status_code==200
    assert b'QR-P3-001' in response.data
    assert b'<svg' in response.data
    assert b'window.print' in response.data
    css=(Path(application.app.static_folder)/'style.css').read_text(encoding='utf-8')
    assert 'grid-template-columns: repeat(3, 60mm)' in css
    assert '@page { size: A4 portrait' in css


def test_tableaux_specialises_respectent_les_roles(app):
    manager=app.test_client(); _login(manager,'manager','manager123')
    assert manager.get('/dashboard/parc').status_code==200
    assert manager.get('/dashboard/rh',follow_redirects=False).status_code in (301,302)
    assert manager.get('/dashboard/direction',follow_redirects=False).status_code in (301,302)
    rh=app.test_client(); _login(rh,'rh','rh123')
    for route in ('/dashboard/rh','/dashboard/parc','/dashboard/direction'):
        response=rh.get(route)
        assert response.status_code==200
        assert b'role-dashboard-grid' in response.data


def test_recherche_globale_exemplaire_par_numero_et_departement(app):
    _exemplaire('INV-IT-P3','Informatique')
    _exemplaire('INV-RH-SECRET','Ressources Humaines')
    manager=app.test_client(); _login(manager,'manager','manager123')
    response=manager.get('/api/recherche?q=INV')
    texte=str(response.get_json())
    assert 'INV-IT-P3' in texte
    assert 'INV-RH-SECRET' not in texte
    assert 'exemplaire' in texte
