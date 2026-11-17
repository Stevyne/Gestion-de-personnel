"""Préchargement des seuls panneaux autorisés, URLs et snapshots privés."""
from html.parser import HTMLParser
from pathlib import Path

import pytest

import app as application
import services.dashboard_tabs as dashboard_service
from test_dashboard_scope import _seed_dashboard


URLS = {
    'general': '/', 'rh': '/dashboard/rh',
    'parc': '/dashboard/parc', 'direction': '/dashboard/direction',
}


class DashboardHTML(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.panels = {}
        self.tabs = {}
        self.ids = []
        self.cards = {}
        self.panel = None
        self.card = None
        self.field = None
        self.feed(html.decode())

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if 'id' in attrs:
            self.ids.append(attrs['id'])
        if 'data-dashboard-panel' in attrs:
            self.panel = attrs['data-dashboard-panel']
            self.panels[self.panel] = attrs
            self.cards[self.panel] = {}
        if 'data-dashboard-tab' in attrs:
            self.tabs[attrs['data-dashboard-tab']] = attrs
        if tag == 'section' and 'role-dashboard-card' in attrs.get('class', '').split():
            self.card = {'label': '', 'value': ''}
        if self.card is not None and tag in ('h3', 'strong'):
            self.field = 'label' if tag == 'h3' else 'value'

    def handle_endtag(self, tag):
        if tag in ('h3', 'strong'):
            self.field = None
        if tag == 'section' and self.card is not None:
            self.cards[self.panel][self.card['label'].strip()] = self.card['value'].strip()
            self.card = None

    def handle_data(self, data):
        if self.card is not None and self.field:
            self.card[self.field] += data


def login(client, username='admin', password='admin123'):
    return client.post('/login', data={'username': username, 'password': password})


@pytest.mark.parametrize('username,password', [('admin', 'admin123'), ('rh', 'rh123')])
@pytest.mark.parametrize('active,url', URLS.items())
def test_chaque_url_charge_tous_les_onglets_et_selectionne_la_bonne_vue(client, username, password, active, url):
    _seed_dashboard()
    login(client, username, password)
    response = client.get(url)
    assert response.status_code == 200
    assert response.headers['Cache-Control'] == 'private, no-store'
    html = DashboardHTML(response.data)
    assert list(html.tabs) == list(URLS)
    assert list(html.panels) == list(URLS)
    assert len(html.ids) == len(set(html.ids)), 'Chaque panneau et graphique a un id unique'
    for name, path in URLS.items():
        assert html.tabs[name]['href'] == path
        assert html.tabs[name]['aria-controls'] == html.panels[name]['id']
        assert html.panels[name]['aria-labelledby'] == html.tabs[name]['id']
        assert ('hidden' not in html.panels[name]) == (name == active)
        assert (html.tabs[name].get('aria-current') == 'page') == (name == active)
    assert len(html.cards['rh']) == 11
    assert len(html.cards['parc']) == 10
    assert len(html.cards['direction']) == 10
    assert html.cards['rh']['Employés actifs'] == '4'
    assert html.cards['parc']['Articles'] == '3'
    assert html.cards['direction']['Effectif actif'] == '4'
    assert b'data-kpi="employees"' in response.data
    assert b'dashboard-tabs.js' in response.data
    assert b'data-dashboard-refresh' in response.data


@pytest.mark.parametrize('username,password', [('manager', 'manager123'), ('employe', 'user123')])
def test_les_panneaux_interdits_ne_sont_pas_dans_le_html_meme_masques(client, monkeypatch, username, password):
    _seed_dashboard()

    def interdit(*args):
        pytest.fail('Une requête spécialisée interdite ne doit jamais être exécutée')

    monkeypatch.setattr(dashboard_service, '_CHARGEURS', dict.fromkeys(('rh', 'parc', 'direction'), interdit))
    login(client, username, password)
    response = client.get('/')
    html = DashboardHTML(response.data)
    assert list(html.panels) == ['general']
    assert list(html.tabs) == ['general']
    assert b'data-kpi="salary"' not in response.data
    assert 'Masse salariale'.encode() not in response.data
    assert 'Sophie Martin'.encode() not in response.data
    for path in list(URLS.values())[1:]:
        denied = client.get(path)
        assert denied.status_code == 302
        assert denied.headers['Location'] == '/'


def devenir_technicien(employe_id=1):
    with application.db_cursor(commit=True) as (conn, cur):
        cur.execute("UPDATE users SET role='technicien', employe_id=%s WHERE id=4", (employe_id,))


@pytest.mark.parametrize('url', ['/', '/dashboard/parc'])
def test_technicien_precharge_parc_uniquement_dans_son_departement(client, url):
    _seed_dashboard()
    devenir_technicien()
    login(client, 'employe', 'user123')
    response = client.get(url)
    assert response.status_code == 200
    html = DashboardHTML(response.data)
    assert list(html.tabs) == ['general', 'parc']
    assert list(html.panels) == ['general', 'parc']
    cards = html.cards['parc']
    assert cards['Articles'] == '1'
    assert cards['Stock disponible'] == '3'
    assert cards['Exemplaires'] == '1'
    assert cards['Maintenances ouvertes'] == '1'
    assert cards['Inventaires ouverts'] == '1'
    assert 'Masse salariale'.encode() not in response.data
    assert 'Sophie Martin'.encode() not in response.data
    for path in ('/dashboard/rh', '/dashboard/direction'):
        assert client.get(path).status_code == 302


def test_technicien_sans_departement_recoit_un_parc_vide_pas_le_parc_global(client):
    _seed_dashboard()
    devenir_technicien(None)
    login(client, 'employe', 'user123')
    response = client.get('/dashboard/parc')
    html = DashboardHTML(response.data)
    assert list(html.panels) == ['general', 'parc']
    assert set(html.cards['parc'].values()) == {'0', '0 Ar'}
    assert 'Aucun département'.encode() in response.data
    assert b'data-kpi="salary"' not in response.data


def test_retrogradation_en_base_revoque_le_prechargement_des_panneaux_sensibles(client):
    _seed_dashboard()
    login(client)
    assert list(DashboardHTML(client.get('/').data).panels) == list(URLS)
    with application.db_cursor(commit=True) as (conn, cur):
        cur.execute("UPDATE users SET role='employe', employe_id=1 WHERE id=1")
    response = client.get('/')
    assert list(DashboardHTML(response.data).panels) == ['general']
    assert b'data-kpi="salary"' not in response.data
    assert client.get('/dashboard/direction').status_code == 302


def test_promotion_en_base_charge_les_nouveaux_onglets_a_la_requete_suivante(client):
    login(client, 'manager', 'manager123')
    assert list(DashboardHTML(client.get('/').data).panels) == ['general']
    with application.db_cursor(commit=True) as (conn, cur):
        cur.execute("UPDATE users SET role='rh' WHERE id=3")
    assert list(DashboardHTML(client.get('/dashboard/rh').data).panels) == list(URLS)


def test_pas_de_cache_partage_entre_comptes(app):
    _seed_dashboard()
    admin = app.test_client()
    employe = app.test_client()
    login(admin)
    login(employe, 'employe', 'user123')
    assert list(DashboardHTML(admin.get('/dashboard/direction').data).panels) == list(URLS)
    assert list(DashboardHTML(employe.get('/').data).panels) == ['general']
    assert list(DashboardHTML(admin.get('/').data).panels) == list(URLS)


@pytest.mark.parametrize('url', URLS.values())
def test_dashboard_toujours_authentifie(client, url):
    response = client.get(url)
    assert response.status_code == 302
    assert '/login' in response.headers['Location']
    assert b'data-dashboard-panel=' not in response.data


@pytest.mark.parametrize('role,global_scope,expected', [
    ('admin', True, list(URLS)), ('rh', True, list(URLS)),
    ('manager', False, ['general']), ('employe', False, ['general']),
    ('technicien', False, ['general', 'parc']), ('inconnu', False, ['general']),
    ('rh', False, ['general', 'parc']),
])
def test_matrice_de_prechargement_combine_role_acl_et_portee(role, global_scope, expected):
    assert [tab['name'] for tab in dashboard_service.onglets_autorises(role, global_scope)] == expected


def test_definitions_des_onglets_non_mutables_par_un_rendu():
    first = dashboard_service.onglets_autorises('admin', True)
    first[0]['label'] = 'altéré'
    assert dashboard_service.onglets_autorises('admin', True)[0]['label'] == 'Général'


def test_scripts_onglets_non_chargees_sur_les_autres_pages(admin_client):
    response = admin_client.get('/employes')
    assert response.status_code == 200
    assert b'dashboard-tabs.js' not in response.data
    assert b'dashboard-tabs.css' not in response.data
    source = (Path(application.app.static_folder) / 'dashboard-tabs.js').read_text()
    assert 'fetch(' not in source
    assert 'XMLHttpRequest' not in source
    assert 'innerHTML' not in source
