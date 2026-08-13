"""Phase 1 : rôles, contraintes PostgreSQL et cohérence du parc."""

import psycopg2
import pytest
from werkzeug.security import generate_password_hash

import app as application
from services.roles import ROLE_CODES


def _parc_unitaire():
    with application.db_cursor(commit=True) as (conn, cur):
        cur.execute("""INSERT INTO departements (nom) VALUES ('Parc Phase1')
                       ON CONFLICT (nom) DO UPDATE SET nom=EXCLUDED.nom RETURNING id""")
        departement_id = cur.fetchone()['id']
        cur.execute("""INSERT INTO materiels
            (nom,categorie,departement_id,quantite,seuil_alerte,suivi_unitaire)
            VALUES ('Portable Phase1','informatique',%s,2,0,TRUE) RETURNING id""",
                    (departement_id,))
        materiel_id = cur.fetchone()['id']
        cur.execute("""INSERT INTO materiel_exemplaires
            (materiel_id,numero_inventaire,etat)
            VALUES (%s,'PH1-001','bon'),(%s,'PH1-002','usage') RETURNING id""",
                    (materiel_id, materiel_id))
        exemplaires = [row['id'] for row in cur.fetchall()]
    return materiel_id, exemplaires


def test_github_actions_execute_postgresql_et_pytest():
    from pathlib import Path
    workflow = (Path(__file__).resolve().parents[1] / '.github' / 'workflows' / 'tests.yml')
    contenu = workflow.read_text(encoding='utf-8')
    assert 'postgres:17' in contenu
    assert 'pytest -q' in contenu
    assert 'pull_request:' in contenu


def test_role_technicien_est_officiel_et_accepte_par_postgresql(admin_client):
    assert 'technicien' in ROLE_CODES
    with application.db_cursor(commit=True) as (conn, cur):
        cur.execute("""INSERT INTO users (username,password_hash,role)
                       VALUES ('technicien-phase1',%s,'technicien') RETURNING id""",
                    (generate_password_hash('Secret12!'),))
        assert cur.fetchone()['id']
    response = admin_client.get('/register')
    assert b'value="technicien"' in response.data


def test_technicien_est_cloisonne_a_son_departement(app):
    with application.db_cursor(commit=True) as (conn, cur):
        cur.execute("""INSERT INTO employes (nom,prenom,departement)
                       VALUES ('VisibleTech','Test','Informatique') RETURNING id""")
        tech_emp = cur.fetchone()['id']
        cur.execute("""INSERT INTO employes (nom,prenom,departement)
                       VALUES ('SecretTech','Test','Ressources Humaines')""")
        cur.execute("""INSERT INTO users (username,password_hash,role,employe_id)
                       VALUES ('tech-phase1',%s,'technicien',%s)""",
                    (generate_password_hash('Secret12!'), tech_emp))
    client = app.test_client()
    client.post('/login', data={'username': 'tech-phase1', 'password': 'Secret12!'})
    response = client.get('/employes')
    assert b'VisibleTech' in response.data
    assert b'SecretTech' not in response.data


def test_role_inconnu_est_refuse_par_postgresql():
    with pytest.raises(psycopg2.errors.CheckViolation):
        with application.db_cursor(commit=True) as (conn, cur):
            cur.execute("""INSERT INTO users (username,password_hash,role)
                           VALUES ('role-invalide',%s,'superadmin')""",
                        (generate_password_hash('Secret12!'),))


def test_contraintes_postgresql_critiques_sont_installees():
    attendues = {
        'ck_users_role', 'ck_materiels_quantite', 'ck_mouvements_quantite',
        'ck_attributions_quantite', 'ck_exemplaires_etat',
        'ck_maintenances_statut', 'ck_conversations_type',
        'ck_messages_contenu', 'ck_permissions_dates', 'ck_conges_dates',
    }
    with application.db_cursor() as (conn, cur):
        cur.execute("SELECT conname FROM pg_constraint WHERE conname = ANY(%s)",
                    (list(attendues),))
        presentes = {row['conname'] for row in cur.fetchall()}
    assert presentes == attendues


@pytest.mark.parametrize('requete,parametres', (
    ("INSERT INTO materiels (nom,quantite,seuil_alerte) VALUES ('Négatif',-1,0)", ()),
    ("INSERT INTO materiels_mouvements (type_mouvement,quantite) VALUES ('entree',0)", ()),
    ("INSERT INTO materiel_exemplaires (materiel_id,numero_inventaire,etat) "
     "SELECT id,'INVALIDE-ETAT','cassé' FROM materiels LIMIT 1", ()),
))
def test_valeurs_metier_invalides_sont_refusees(requete, parametres):
    # Garantit qu'un matériel existe pour le cas exemplaire.
    with application.db_cursor(commit=True) as (conn, cur):
        cur.execute("INSERT INTO materiels (nom,quantite,seuil_alerte) VALUES ('Support',1,0)")
    with pytest.raises((psycopg2.errors.CheckViolation,
                        psycopg2.errors.NotNullViolation,
                        psycopg2.errors.ForeignKeyViolation)):
        with application.db_cursor(commit=True) as (conn, cur):
            cur.execute(requete, parametres)


def test_attribution_unitaire_lie_exemplaire_et_detenteur(admin_client):
    materiel_id, exemplaires = _parc_unitaire()
    response = admin_client.post(f'/materiels/{materiel_id}/attribuer', data={
        'employe_id': '1', 'exemplaire_id': str(exemplaires[0]),
        'quantite': '9', 'commentaire': 'Remise réelle',
    }, follow_redirects=True)
    assert response.status_code == 200

    with application.db_cursor() as (conn, cur):
        cur.execute("SELECT * FROM materiels_attributions WHERE materiel_id=%s",
                    (materiel_id,))
        attribution = cur.fetchone()
        cur.execute("SELECT employe_id FROM materiel_exemplaires WHERE id=%s",
                    (exemplaires[0],))
        detenteur = cur.fetchone()['employe_id']
        cur.execute("SELECT quantite FROM materiels WHERE id=%s", (materiel_id,))
        stock = cur.fetchone()['quantite']
    assert attribution['exemplaire_id'] == exemplaires[0]
    assert attribution['quantite'] == 1
    assert detenteur == 1
    assert stock == 1


def test_retour_unitaire_libere_exactement_exemplaire(admin_client):
    materiel_id, exemplaires = _parc_unitaire()
    admin_client.post(f'/materiels/{materiel_id}/attribuer', data={
        'employe_id': '1', 'exemplaire_id': str(exemplaires[0]), 'quantite': '1',
    })
    with application.db_cursor() as (conn, cur):
        cur.execute("SELECT id FROM materiels_attributions WHERE materiel_id=%s",
                    (materiel_id,))
        attribution_id = cur.fetchone()['id']

    response = admin_client.post(
        f'/materiels/attribution/{attribution_id}/retour',
        data={'materiel_id': materiel_id}, follow_redirects=True)
    assert response.status_code == 200
    with application.db_cursor() as (conn, cur):
        cur.execute("SELECT employe_id FROM materiel_exemplaires WHERE id=%s",
                    (exemplaires[0],))
        assert cur.fetchone()['employe_id'] is None
        cur.execute("SELECT quantite FROM materiels WHERE id=%s", (materiel_id,))
        assert cur.fetchone()['quantite'] == 2


def test_attribution_unitaire_sans_exemplaire_est_impossible(admin_client):
    materiel_id, _ = _parc_unitaire()
    response = admin_client.post(f'/materiels/{materiel_id}/attribuer', data={
        'employe_id': '1', 'quantite': '1',
    }, follow_redirects=True)
    assert 'exemplaire physique'.encode('utf-8') in response.data
    with application.db_cursor() as (conn, cur):
        cur.execute("SELECT COUNT(*) AS n FROM materiels_attributions WHERE materiel_id=%s",
                    (materiel_id,))
        assert cur.fetchone()['n'] == 0


def test_exemplaire_d_un_autre_article_est_refuse(admin_client):
    materiel_id, _ = _parc_unitaire()
    with application.db_cursor(commit=True) as (conn, cur):
        cur.execute("""INSERT INTO materiels (nom,quantite,suivi_unitaire)
                       VALUES ('Autre article',1,TRUE) RETURNING id""")
        autre = cur.fetchone()['id']
        cur.execute("""INSERT INTO materiel_exemplaires
                       (materiel_id,numero_inventaire,etat)
                       VALUES (%s,'AUTRE-001','bon') RETURNING id""", (autre,))
        autre_exemplaire = cur.fetchone()['id']
    admin_client.post(f'/materiels/{materiel_id}/attribuer', data={
        'employe_id': '1', 'exemplaire_id': autre_exemplaire, 'quantite': '1',
    })
    with application.db_cursor() as (conn, cur):
        cur.execute("SELECT COUNT(*) AS n FROM materiels_attributions WHERE materiel_id=%s",
                    (materiel_id,))
        assert cur.fetchone()['n'] == 0


def test_trigger_refuse_attribution_unitaire_sans_exemplaire():
    materiel_id, _ = _parc_unitaire()
    with pytest.raises(psycopg2.errors.RaiseException):
        with application.db_cursor(commit=True) as (conn, cur):
            cur.execute("""INSERT INTO materiels_attributions
                           (materiel_id,employe_id,quantite)
                           VALUES (%s,1,1)""", (materiel_id,))


def test_modification_directe_detenteur_est_interdite():
    _, exemplaires = _parc_unitaire()
    with pytest.raises(psycopg2.errors.RaiseException):
        with application.db_cursor(commit=True) as (conn, cur):
            cur.execute("UPDATE materiel_exemplaires SET employe_id=1 WHERE id=%s",
                        (exemplaires[0],))


def test_materiel_non_unitaire_garde_attribution_quantitative(admin_client):
    with application.db_cursor(commit=True) as (conn, cur):
        cur.execute("""INSERT INTO materiels (nom,categorie,quantite,suivi_unitaire)
                       VALUES ('Clés Phase1','autre',5,FALSE) RETURNING id""")
        materiel_id = cur.fetchone()['id']
    admin_client.post(f'/materiels/{materiel_id}/attribuer', data={
        'employe_id': '1', 'quantite': '2',
    })
    with application.db_cursor() as (conn, cur):
        cur.execute("""SELECT exemplaire_id,quantite FROM materiels_attributions
                       WHERE materiel_id=%s""", (materiel_id,))
        row = cur.fetchone()
    assert row['exemplaire_id'] is None
    assert row['quantite'] == 2
