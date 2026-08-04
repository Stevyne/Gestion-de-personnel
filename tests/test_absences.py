from datetime import date, timedelta

import app as application


def _dernier_jour_ouvre_passe():
    """Retourne le plus récent jour ouvré (lun-ven) strictement avant aujourd'hui."""
    d = date.today() - timedelta(days=1)
    while d.weekday() >= 5:  # 5=samedi, 6=dimanche
        d -= timedelta(days=1)
    return d


def _preparer_periodes(cur, employe_id, embauche, autres_embauche):
    """Fixe les dates d'embauche pour limiter la portée du calcul et nettoie
    les données liées : tous les autres employés sont 'embauchés' aujourd'hui
    (hors période) pour ne pas polluer le test."""
    cur.execute("UPDATE employes SET date_embauche = %s", (autres_embauche,))
    cur.execute("UPDATE employes SET date_embauche = %s WHERE id = %s", (embauche, employe_id))
    cur.execute("DELETE FROM presences WHERE employe_id = %s", (employe_id,))
    cur.execute("DELETE FROM conges WHERE employe_id = %s", (employe_id,))
    cur.execute("DELETE FROM permissions WHERE employe_id = %s", (employe_id,))
    cur.execute("DELETE FROM absences")


def test_jour_sans_presence_devient_absence(admin_client):
    hier = _dernier_jour_ouvre_passe()
    today = date.today()
    with application.db_cursor(commit=True) as (conn, cur):
        _preparer_periodes(cur, 1, hier, today)
        nb = application.generer_absences_automatiques(cur, date_jusqua=hier)
    assert nb == 1
    with application.db_cursor() as (conn, cur):
        cur.execute("SELECT COUNT(*) AS n FROM absences WHERE employe_id = 1")
        assert cur.fetchone()['n'] == 1


def test_presence_empeche_absence(admin_client):
    hier = _dernier_jour_ouvre_passe()
    today = date.today()
    with application.db_cursor(commit=True) as (conn, cur):
        _preparer_periodes(cur, 1, hier, today)
        # l'employé a pointé ce jour-là -> pas d'absence
        cur.execute(
            "INSERT INTO presences (employe_id, date, heure_arrivee, statut) VALUES (%s, %s, '08:30', 'présent')",
            (1, hier),
        )
        nb = application.generer_absences_automatiques(cur, date_jusqua=hier)
    assert nb == 0
    with application.db_cursor() as (conn, cur):
        cur.execute("SELECT COUNT(*) AS n FROM absences WHERE employe_id = 1")
        assert cur.fetchone()['n'] == 0


def test_conge_approuve_empeche_absence(admin_client):
    hier = _dernier_jour_ouvre_passe()
    today = date.today()
    with application.db_cursor(commit=True) as (conn, cur):
        _preparer_periodes(cur, 1, hier, today)
        cur.execute(
            "INSERT INTO conges (employe_id, type_conge, date_debut, date_fin, nombre_jours, statut) "
            "VALUES (1, 'congé payé', %s, %s, 1, 'approuvé')",
            (hier, hier),
        )
        nb = application.generer_absences_automatiques(cur, date_jusqua=hier)
    assert nb == 0
    with application.db_cursor() as (conn, cur):
        cur.execute("SELECT COUNT(*) AS n FROM absences WHERE employe_id = 1")
        assert cur.fetchone()['n'] == 0


def test_permission_approuvee_empeche_absence(admin_client):
    """Lien entre les deux modules : une permission approuvée couvre le jour,
    donc il n'est pas marqué comme absence non justifiée."""
    hier = _dernier_jour_ouvre_passe()
    today = date.today()
    with application.db_cursor(commit=True) as (conn, cur):
        _preparer_periodes(cur, 1, hier, today)
        cur.execute(
            "INSERT INTO permissions (employe_id, motif, date_debut, date_fin, nombre_jours, statut) "
            "VALUES (1, 'démarche', %s, %s, 1, 'approuvé')",
            (hier, hier),
        )
        nb = application.generer_absences_automatiques(cur, date_jusqua=hier)
    assert nb == 0


def test_generation_est_idempotente(admin_client):
    hier = _dernier_jour_ouvre_passe()
    today = date.today()
    with application.db_cursor(commit=True) as (conn, cur):
        _preparer_periodes(cur, 1, hier, today)
        n1 = application.generer_absences_automatiques(cur, date_jusqua=hier)
        n2 = application.generer_absences_automatiques(cur, date_jusqua=hier)
    assert n1 == 1
    assert n2 == 0  # déjà créé -> rien de nouveau


def test_absences_page_interdite_aux_employes(employe_client):
    resp = employe_client.get('/absences', follow_redirects=False)
    assert resp.status_code in (301, 302)
