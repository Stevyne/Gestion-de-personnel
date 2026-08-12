import app as application


def _departement():
    with application.db_cursor(commit=True) as (conn, cur):
        cur.execute("""
            INSERT INTO departements (nom, description)
            VALUES ('Parc Test', 'Tests automatisés')
            ON CONFLICT (nom) DO UPDATE SET description = EXCLUDED.description
            RETURNING id
        """)
        return cur.fetchone()['id']


def test_creation_materiel_trace_stock_initial(admin_client):
    dept_id = _departement()
    response = admin_client.post('/materiels/add', data={
        'nom': 'Clavier test', 'categorie': 'informatique',
        'departement_id': str(dept_id), 'quantite': '5',
        'seuil_alerte': '1', 'unite': 'unité',
    }, follow_redirects=True)
    assert response.status_code == 200

    with application.db_cursor() as (conn, cur):
        cur.execute("SELECT id, quantite FROM materiels WHERE nom = 'Clavier test'")
        materiel = cur.fetchone()
        cur.execute("""SELECT type_mouvement, quantite, motif
                       FROM materiels_mouvements WHERE materiel_id = %s""",
                    (materiel['id'],))
        mouvement = cur.fetchone()
    assert materiel['quantite'] == 5
    assert mouvement['type_mouvement'] == 'entree'
    assert mouvement['quantite'] == 5
    assert mouvement['motif'] == 'Stock initial'


def test_sortie_superieure_au_stock_est_refusee(admin_client):
    dept_id = _departement()
    with application.db_cursor(commit=True) as (conn, cur):
        cur.execute("""
            INSERT INTO materiels (nom, categorie, departement_id, quantite)
            VALUES ('Souris test', 'informatique', %s, 2) RETURNING id
        """, (dept_id,))
        materiel_id = cur.fetchone()['id']

    response = admin_client.post(f'/materiels/{materiel_id}/mouvement', data={
        'type_mouvement': 'sortie', 'quantite': '3', 'motif': 'Test',
    }, follow_redirects=True)
    assert response.status_code == 200
    assert 'Stock insuffisant'.encode('utf-8') in response.data
    with application.db_cursor() as (conn, cur):
        cur.execute("SELECT quantite FROM materiels WHERE id = %s", (materiel_id,))
        assert cur.fetchone()['quantite'] == 2
