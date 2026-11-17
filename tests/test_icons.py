"""Icônes d'interface : catalogue unique, rendu serveur et absence de conversion des saisies."""
from html.parser import HTMLParser
from pathlib import Path
import re
from xml.etree import ElementTree

from jinja2 import Environment, FileSystemLoader

import app as application
from blueprints.parc import MATERIEL_CATEGORIES
from services.objectifs import STATUT_LABELS


ROOT = Path(__file__).resolve().parents[1]
EMOJI = re.compile(r'[\U0001F000-\U0001FAFF\u2600-\u27BF\u23f0-\u23ff]')


def _catalogue():
    env = Environment(loader=FileSystemLoader(ROOT / 'templates'), autoescape=True)
    return env.get_template('_icons.html').module


class Structure(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.icons = []
        self.native_svg = []
        self.context = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag in ('title', 'option', 'textarea'):
            self.context.append(tag)
        if tag == 'svg':
            values = dict(attrs)
            if self.context:
                self.native_svg.append(list(self.context))
            if values.get('data-icon'):
                self.icons.append(values)

    def handle_endtag(self, tag):
        if self.context and self.context[-1] == tag:
            self.context.pop()


def test_catalogue_reutilise_le_trait_de_la_navbar_et_svg_valides():
    module = _catalogue()
    assert len(module.paths) >= 75
    for name in module.paths:
        svg = ElementTree.fromstring(module.icon(name))
        assert svg.tag == 'svg'
        assert svg.attrib['data-icon'] == name
        assert svg.attrib['viewBox'] == '0 0 24 24'
        assert svg.attrib['stroke'] == 'currentColor'
        assert svg.attrib['stroke-width'] == '1.9'
        assert svg.attrib['aria-hidden'] == 'true'
        assert svg.attrib['focusable'] == 'false'
        assert len(svg) > 0
        assert all(node.tag in ('path', 'rect', 'circle', 'ellipse', 'line', 'polyline', 'polygon') for node in svg)


def test_catalogue_inconnu_et_taille_injectee_ne_deviennent_pas_du_html():
    module = _catalogue()
    svg = ElementTree.fromstring(module.icon('<script>alert(1)</script>', '18" onload="evil'))
    assert svg.attrib['data-icon'] == 'info'
    assert svg.attrib['width'] == '18'
    assert 'onload' not in svg.attrib
    assert ElementTree.fromstring(module.icon('eye', '1em')).attrib['width'] == '1em'


def test_banque_couvre_tous_les_etats_javascript_et_confirmations():
    module = _catalogue()
    bank = Structure(module.icon_bank())
    names = {svg['data-icon'] for svg in bank.icons}
    assert {'menu', 'x', 'eye', 'eye-off', 'check', 'paperclip', 'bell', 'message', 'alert'} <= names
    for path in (ROOT / 'templates').rglob('*.html'):
        for name in re.findall(r'data-confirm-icon="([^"]+)"', path.read_text()):
            assert name in names, (path, name)


def test_tous_les_templates_compilent_avec_les_filtres_flask(app):
    for name in app.jinja_env.list_templates():
        app.jinja_env.get_template(name)


def test_pas_d_emojis_decoratifs_codes_en_dur_dans_le_frontend():
    files = list((ROOT / 'templates').rglob('*.html'))
    files += [p for p in (ROOT / 'static').glob('*') if p.suffix in ('.js', '.css')]
    for path in files:
        text = path.read_text()
        assert not EMOJI.search(text), str(path)
        assert not EMOJI.search(re.sub(r'&#x([0-9a-fA-F]+);', lambda m: chr(int(m[1], 16)), text)), str(path)


def test_libelles_natifs_sans_emoji_et_codes_metier_inchanges():
    assert [code for code, _label in MATERIEL_CATEGORIES] == [
        'fourniture', 'papeterie', 'mobilier', 'informatique', 'entretien', 'autre',
    ]
    for _code, label in MATERIEL_CATEGORIES:
        assert not EMOJI.search(label)
        assert '<' not in label
    assert STATUT_LABELS['atteint'] == 'Atteint (à valider)'
    assert STATUT_LABELS['non_atteint'] == 'Non atteint'


def test_pages_et_fragments_reutilisent_les_icones_sans_dupliquer_la_banque(admin_client):
    for url in ('/', '/dashboard/rh', '/dashboard/parc', '/dashboard/direction', '/employes',
                '/departements', '/competences', '/objectifs', '/materiels', '/notifications',
                '/messages', '/messages/nouveau', '/utilisateurs'):
        response = admin_client.get(url)
        assert response.status_code == 200, url
        html = response.get_data(as_text=True)
        assert html.count('id="appIconTemplates"') == 1, url
        assert '/static/icons.css' in html and '/static/icons.js' in html
        parsed = Structure(html)
        assert len(parsed.icons) > 10, url
        assert not parsed.native_svg, (url, parsed.native_svg)
        assert '&lt;svg' not in html, url
    for url in ('/register?modal=1', '/departements/add?modal=1', '/notifications?panel=1', '/messages/nouveau?panel=1'):
        response = admin_client.get(url)
        assert response.status_code == 200, url
        html = response.get_data(as_text=True)
        assert 'id="appIconTemplates"' not in html
        if not url.startswith('/departements/add'):
            assert Structure(html).icons, url
        assert not Structure(html).native_svg, url


def test_flash_utilisateur_reste_echappe_et_ses_emojis_ne_sont_pas_transformes(client):
    message = 'Bravo 🎉 <img src=x onerror=alert(1)>'
    with client.session_transaction() as session:
        session['_flashes'] = [('success', message)]
    html = client.get('/login').get_data(as_text=True)
    assert 'Bravo 🎉 &lt;img src=x onerror=alert(1)&gt;' in html
    assert 'data-icon="check-circle"' in html
    assert 'data-dismiss-after="10000"' in html


def test_avatars_de_groupe_et_annonce_sont_des_svg_messages_intacts(admin_client):
    message = 'Texte libre 🎉 <strong>inchangé</strong>'
    response = admin_client.post('/messages/nouveau', data={
        'type': 'groupe', 'destinataires': ['2', '3'], 'titre': 'Groupe test SVG', 'contenu': message,
    })
    assert response.status_code == 302
    group_url = response.headers['Location']
    html = admin_client.get(group_url).get_data(as_text=True)
    assert 'Texte libre 🎉 &lt;strong&gt;inchangé&lt;/strong&gt;' in html
    assert re.search(r'msg-avatar--groupe[^>]*>\s*<span>\s*<svg[^>]*data-icon="users"', html)
    response = admin_client.post('/messages/nouveau', data={
        'type': 'annonce', 'titre': 'Annonce SVG', 'contenu': 'Information de test', 'cible_role': '',
    })
    assert response.status_code == 302
    html = admin_client.get(response.headers['Location']).get_data(as_text=True)
    assert re.search(r'msg-avatar--annonce[^>]*>\s*<span>\s*<svg[^>]*data-icon="megaphone"', html)
    with application.db_cursor() as (_conn, cur):
        cur.execute('SELECT contenu FROM messages WHERE contenu=%s', (message,))
        assert cur.fetchone()['contenu'] == message


def test_librairie_locale_et_ne_parcourt_pas_les_textes_saisis(client):
    for name in ('icons.css', 'icons.js'):
        response = client.get('/static/' + name)
        assert response.status_code == 200
        assert b'https://' not in response.data
    js = (ROOT / 'static/icons.js').read_text()
    assert 'createTextNode' in js
    assert '.innerHTML' not in js
    assert 'MutationObserver' not in js
    assert 'replaceChildren' in js
