"""Navigation privée : mêmes routes/droits que le HTML, jamais de préchargement."""
import json
from pathlib import Path
import re

from flask import url_for
import pytest

import app as application
from services.navigation import READ_ENDPOINTS, extraire_fragment, shell_version, url_rule_pattern
from test_dashboard_scope import _seed_dashboard


def _config(response):
    match = re.search(rb'<script id="appNavigationConfig"[^>]*>(.*?)</script>', response.data, re.S)
    assert match
    return json.loads(match[1])


def _headers(client):
    config = _config(client.get('/'))
    return {'X-App-Navigation': '1', 'X-App-Identity': config['identity'], 'X-App-Version': config['version']}


def _seed_navigation():
    """Un exemplaire réel de chaque page de détail, y compris les formulaires."""
    _seed_dashboard()
    ids = {}
    with application.db_cursor(commit=True) as (_conn, cur):
        cur.execute("SELECT id FROM departements WHERE nom='Informatique'")
        ids['departement'] = cur.fetchone()['id']
        for table, key in [('materiels', 'materiel'), ('materiel_exemplaires', 'exemplaire'), ('inventaires', 'inventaire')]:
            cur.execute(f'SELECT MIN(id) AS id FROM {table}')
            ids[key] = cur.fetchone()['id']
        cur.execute("""INSERT INTO absences (employe_id,date,motif,statut)
                       VALUES (4,CURRENT_DATE-2,'Navigation admin','non_justifiee') RETURNING id""")
        ids['absence_id'] = cur.fetchone()['id']
        cur.execute("INSERT INTO competences (nom,categorie) VALUES ('Python navigation','Technique') RETURNING id")
        ids['cid'] = cur.fetchone()['id']
        cur.execute("""INSERT INTO employe_competences (employe_id,competence_id,niveau,ajoute_par)
                       VALUES (1,%s,65,1)""", (ids['cid'],))
        cur.execute("""INSERT INTO objectifs (employe_id,titre,priorite,statut,progression,cree_par,cree_par_role)
                       VALUES (1,'Objectif navigation','normale','en_cours',25,1,'admin') RETURNING id""")
        ids['oid'] = cur.fetchone()['id']
        cur.execute("""INSERT INTO contrats (employe_id,type_contrat,date_debut,date_fin,reference)
                       VALUES (1,'cdd',CURRENT_DATE-30,CURRENT_DATE+365,'NAV-CONTRAT') RETURNING id""")
        ids['contrat'] = cur.fetchone()['id']
        cur.execute("""INSERT INTO recrutement_demandes
                       (reference,poste,departement_id,type_contrat,motif,demandeur_user_id)
                       VALUES ('NAV-DEM-001','Développeur',%s,'cdi','Test',1) RETURNING id""", (ids['departement'],))
        ids['demande_id'] = cur.fetchone()['id']
        cur.execute("""INSERT INTO recrutement_offres
                       (reference,titre,description,departement_id,poste,type_contrat,cree_par)
                       VALUES ('NAV-OFF-001','Offre navigation','Description',%s,'Développeur','cdi',1) RETURNING id""",
                    (ids['departement'],))
        ids['offre_id'] = cur.fetchone()['id']
        cur.execute("""INSERT INTO recrutement_criteres (offre_id,libelle,poids)
                       VALUES (%s,'Technique',60),(%s,'Communication',40)""", (ids['offre_id'], ids['offre_id']))
        cur.execute("""INSERT INTO recrutement_candidats (nom,prenom,email,cree_par)
                       VALUES ('Navigation','Candidat','navigation@example.test',1) RETURNING id""")
        ids['candidat_id'] = cur.fetchone()['id']
        cur.execute("""INSERT INTO recrutement_candidatures (candidat_id,offre_id,statut)
                       VALUES (%s,%s,'acceptee') RETURNING id""", (ids['candidat_id'], ids['offre_id']))
        ids['candidature_id'] = cur.fetchone()['id']
        cur.execute("""INSERT INTO recrutement_entretiens
                       (candidature_id,date_entretien,heure_entretien,type_entretien,evaluateur_user_id)
                       VALUES (%s,CURRENT_DATE,'10:00','presentiel',1) RETURNING id""", (ids['candidature_id'],))
        ids['entretien_id'] = cur.fetchone()['id']
        cur.execute("INSERT INTO conversations (type,titre,cree_par) VALUES ('prive','Navigation',1) RETURNING id")
        ids['conversation'] = cur.fetchone()['id']
        cur.execute("""INSERT INTO conversation_membres (conversation_id,user_id) VALUES (%s,1),(%s,4)""",
                    (ids['conversation'], ids['conversation']))
        cur.execute("INSERT INTO messages (conversation_id,sender_id,contenu) VALUES (%s,4,'Bonjour navigation !')",
                    (ids['conversation'],))
    return ids


def _navigation_urls(ids):
    result = []
    with application.app.test_request_context():
        for rule in application.app.url_map.iter_rules():
            if rule.endpoint not in READ_ENDPOINTS or 'GET' not in rule.methods:
                continue
            values = {name: ids.get(name, 1) for name in rule.arguments}
            if 'id' in values:
                endpoint = rule.endpoint
                values['id'] = (
                    ids['departement'] if endpoint.startswith('departements.') or endpoint == 'parc.materiels_departement'
                    else ids['contrat'] if endpoint.startswith('contrats.')
                    else ids['conversation'] if endpoint == 'messagerie.messagerie_voir'
                    else ids['inventaire'] if endpoint == 'parc.view_inventaire'
                    else ids['exemplaire'] if endpoint == 'parc.view_exemplaire'
                    else ids['materiel'] if endpoint.startswith('parc.') else 1
                )
            result.append((rule.endpoint, url_for(rule.endpoint, **values)))
    return list(dict.fromkeys(result))


def test_toutes_les_routes_de_lecture_rendent_un_vrai_fragment_avec_donnees(admin_client):
    ids = _seed_navigation()
    headers = _headers(admin_client)
    urls = _navigation_urls(ids)
    assert {endpoint for endpoint, _url in urls} == READ_ENDPOINTS
    for endpoint, url in urls:
        response = admin_client.get(url, headers=headers)
        if endpoint in ('parc.materiels_departement', 'add_employee_alt'):
            assert response.status_code == 204
            target = response.headers['X-App-Redirect']
            assert '/materiels?' in target if endpoint == 'parc.materiels_departement' else target == '/employes'
            continue
        assert response.status_code == 200, (endpoint, url, response.data[:300])
        assert response.mimetype == 'application/json', (endpoint, url)
        payload = response.json
        assert payload['main'].startswith('<main id="appMain"'), (endpoint, url)
        assert payload['main'].endswith('</main>')
        assert payload['title'] and payload['nav']
        assert 'class="navbar"' not in payload['main']
        assert payload['version'] == headers['X-App-Version']
        assert payload['identity'] == headers['X-App-Identity']
        assert response.headers['Cache-Control'] == 'private, no-store'
        assert {'Cookie', 'X-App-Navigation', 'X-App-Identity', 'X-App-Version'} <= set(response.vary)


def test_le_contrat_envoie_moins_que_le_layout_complet_et_conserve_styles_et_scripts(admin_client):
    headers = _headers(admin_client)
    for url in ('/employes', '/competences', '/messages', '/'):
        full = admin_client.get(url)
        assert full.headers['Cache-Control'] == 'private, no-store'
        assert 'X-App-Navigation' in full.vary
        fast = admin_client.get(url, headers=headers)
        expected = extraire_fragment(full.get_data(as_text=True))
        assert fast.json['main'] == expected['main']
        assert fast.json['styles'] == expected['styles']
        assert fast.json['scripts'] == expected['scripts']
        assert fast.json['badges'] == expected['badges']
        assert len(fast.data) < len(full.data)
        assert 'unsafe-eval' not in fast.headers.get('Content-Security-Policy', '')
        assert fast.headers.get('X-Frame-Options') == 'DENY'


def test_html_classique_et_urls_sans_js_restent_disponibles(admin_client, client):
    response = admin_client.get('/employes')
    assert response.mimetype == 'text/html'
    assert _config(response)['enabled']
    assert response.data.count(b'id="appMain"') == 1
    assert b'href="/employes"' in response.data
    admin_client.get('/logout')
    assert not _config(client.get('/login'))['enabled']


@pytest.mark.parametrize('changes', [
    {'X-App-Version': 'ancienne'}, {'X-App-Identity': '4:employe'},
    {'X-App-Version': ''}, {'X-App-Identity': ''},
])
def test_une_coque_perimee_ou_un_autre_compte_force_le_rechargement_sans_rendu(admin_client, monkeypatch, changes):
    headers = {**_headers(admin_client), **changes}
    def interdit():
        pytest.fail('Le rendu de la page ne doit pas avoir lieu avec une coque périmée')
    monkeypatch.setitem(application.app.view_functions, 'index', interdit)
    response = admin_client.get('/employes', headers=headers)
    assert response.status_code == 204
    assert response.headers['X-App-Native-Navigation'] == '/employes'
    assert not response.data


@pytest.mark.parametrize('url', [
    '/logout', '/login', '/export/presences/pdf', '/export/materiels/excel',
    '/messages/non-lus', '/api/recherche?q=abc', '/materiels/1/etiquettes',
    '/departements/1/etiquettes', '/employes/1/fiche-de-paie/pdf',
    '/documents/file/1', '/avatar/inconnu.png', '/health/live',
    '/employes?modal=1', '/notifications?panel=1', '/employes?print=1',
    '/employes?download=1', '/employes?attachment=1',
])
def test_routes_a_effet_de_bord_exports_et_fragments_separes_non_interceptes(admin_client, url):
    response = admin_client.get(url, headers=_headers(admin_client))
    assert response.status_code == 204
    assert response.headers['X-App-Native-Navigation'] == url
    with admin_client.session_transaction() as state:
        assert state.get('user_id') == 1, 'Un GET rapide /logout ne doit pas déconnecter'


def test_session_expiree_renvoie_un_repli_sans_donnees_privees(client):
    response = client.get('/employes', headers={'X-App-Navigation': '1'})
    assert response.status_code == 204
    assert response.headers['X-App-Native-Navigation'] == '/employes'
    assert not response.data


def test_changement_de_role_en_base_rejette_l_ancienne_coque(admin_client):
    headers = _headers(admin_client)
    with application.db_cursor(commit=True) as (_conn, cur):
        cur.execute("UPDATE users SET role='employe',employe_id=1 WHERE id=1")
    response = admin_client.get('/', headers=headers)
    assert response.status_code == 204
    assert response.headers['X-App-Native-Navigation'] == '/'
    assert not response.data


@pytest.mark.parametrize('username,password', [('manager', 'manager123'), ('employe', 'user123')])
def test_fragments_cloisonnes_meme_acl_et_masquage_que_le_html(client, username, password):
    _seed_navigation()
    client.post('/login', data={'username': username, 'password': password})
    headers = _headers(client)
    employee = client.get('/employes' if username == 'manager' else '/self-service', headers=headers).json
    assert 'Sophie Martin' not in employee['main']
    assert 'href="/add_employee"' not in employee['main']
    dashboard = client.get('/', headers=headers).json
    assert 'dashboard-panel-rh' not in dashboard['main']
    assert 'dashboard-panel-parc' not in dashboard['main']
    assert 'Masse salariale' not in dashboard['main']
    assert 'href="/utilisateurs"' not in dashboard['main']
    denied = client.get('/dashboard/parc', headers=headers)
    assert denied.status_code == 204
    assert denied.headers['X-App-Redirect'] == '/'
    redirected = client.get('/', headers=headers).json
    assert 'Accès refusé' in redirected['main']
    assert 'alert-danger' in redirected['main']


def test_technicien_conserve_uniquement_parc_de_son_departement(client):
    _seed_navigation()
    with application.db_cursor(commit=True) as (_conn, cur):
        cur.execute("UPDATE users SET role='technicien',employe_id=1 WHERE id=4")
    client.post('/login', data={'username': 'employe', 'password': 'user123'})
    # Le catalogue employés n'est pas nécessairement dans son périmètre ACL.
    config = _config(client.get('/'))
    headers = {'X-App-Navigation': '1', 'X-App-Version': config['version'], 'X-App-Identity': config['identity']}
    payload = client.get('/dashboard/parc', headers=headers).json
    assert 'dashboard-panel-parc' in payload['main']
    assert 'dashboard-panel-rh' not in payload['main']
    assert 'Papier RH' not in client.get('/materiels', headers=headers).json['main']


def test_redirection_ne_consomme_pas_le_flash_et_post_reste_natif(admin_client):
    headers = _headers(admin_client)
    with application.db_cursor(commit=True) as (_conn, cur):
        cur.execute("DELETE FROM departements WHERE nom='Navigation POST'")
    try:
        response = admin_client.post('/departements/add', data={'nom': 'Navigation POST'}, headers=headers)
        assert response.status_code == 302
        assert response.headers['Location'] == '/departements'
        payload = admin_client.get('/departements', headers=headers).json
        assert 'créé avec succès' in payload['main']
        assert 'data-dismiss-after="10000"' in payload['main']
    finally:
        with application.db_cursor(commit=True) as (_conn, cur):
            cur.execute("DELETE FROM departements WHERE nom='Navigation POST'")


def test_query_string_qui_se_termine_par_point_interrogation_n_est_pas_tronquee(admin_client):
    response = admin_client.get('/employes?search=Question?', headers=_headers(admin_client))
    assert response.json['url'] == '/employes?search=Question?'


@pytest.mark.parametrize('rule,url,expected', [
    ('/employes', '/employes', True), ('/employes', '/employes/1', False),
    ('/employes/<int:id>', '/employes/24', True), ('/employes/<int:id>', '/employes/a', False),
    ('/avatar/<path:filename>', '/avatar/a/b.png', True),
    ('/score/<float:value>', '/score/1.5', True), ('/score/<float:value>', '/score/abc', False),
    ('/fichier/<name>', '/fichier/a.pdf', True), ('/fichier/<name>', '/fichier/a/b', False),
])
def test_conversion_regles_flask_ancrees(rule, url, expected):
    assert bool(re.match(url_rule_pattern(rule), url)) is expected


def test_fragments_incomplets_ou_ambigus_replient_sans_exception():
    assert extraire_fragment('<main>incomplet</main>') is None
    sample = ''.join(f'<!-- app-navigation:{name}:start --><div>Test</div><!-- app-navigation:{name}:end -->'
                     for name in ('main', 'styles', 'scripts'))
    assert extraire_fragment(sample)['main'] == '<div>Test</div>'
    assert extraire_fragment(sample + '<!-- app-navigation:main:start -->') is None
    assert extraire_fragment(sample.replace('main:start', 'main:TEMP').replace('main:end', 'main:start').replace('main:TEMP', 'main:end')) is None
    assert extraire_fragment(sample + '<script id="appNavigationCounts">invalide</script>') is None
    assert extraire_fragment(sample + '<script id="appNavigationCounts">[]</script>') is None


def test_version_change_avec_templates_et_assets_mais_pas_avec_les_donnees(tmp_path):
    for name in ('templates/base.html', 'static/a.js', 'static/css/b.css'):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('initial')
    first = shell_version(tmp_path)
    assert first == shell_version(str(tmp_path))
    (tmp_path / 'donnees.json').write_text('privé')
    assert first == shell_version(tmp_path)
    (tmp_path / 'static/a.js').write_text('modifié')
    assert first != shell_version(tmp_path)
    assert all(Path(application.app.root_path, 'static', name).exists()
               for name in ('navigation.js', 'navigation-policy.js', 'navigation.css', 'page-lifecycle.js'))
