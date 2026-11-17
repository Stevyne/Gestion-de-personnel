"""Régressions d'intégration du chargement, sans dépendance front-end."""
from html.parser import HTMLParser
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]


class Links(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.links = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            self.links.append(dict(attrs))


def test_login_inclut_un_seul_indicateur_local_et_accessible(client):
    response = client.get('/login')
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert html.count('id="appLoading"') == 1
    assert html.count('id="appLoadingStatus"') == 1
    assert 'role="progressbar"' in html
    assert 'aria-label="Chargement en cours" hidden' in html
    assert 'aria-live="polite" aria-atomic="true"' in html
    assert '/static/loading.css' in html
    assert '/static/loading.js' in html


def test_navigation_et_exports_ne_sont_pas_confondus(admin_client):
    response = admin_client.get('/self-service')
    assert response.status_code == 200
    links = Links(response.get_data(as_text=True)).links
    exports = [a for a in links if a.get('href', '').startswith('/export/')]
    assert exports
    assert all('data-no-loading' in a for a in exports)
    audit = [a for a in links if a.get('href') == '/audit']
    assert audit
    assert all('data-no-loading' not in a for a in audit)


def test_chargement_sans_cdn_et_mouvement_reduit(client):
    for name in ('loading.js', 'loading.css'):
        response = client.get('/static/' + name)
        assert response.status_code == 200
        source = response.get_data(as_text=True)
        assert 'https://' not in source and 'http://' not in source
    css = (ROOT / 'static/loading.css').read_text(encoding='utf-8')
    assert 'prefers-reduced-motion: reduce' in css
    assert 'animation: none' in css
    assert 'pointer-events: none' in css
    assert '@media print' in css


def test_les_telechargements_existants_sont_exclus_explicitement():
    endpoints = (
        'export_presences_', 'export_conges_', 'rapports_parc.export_',
        'audit_export_excel', 'documents.download_document',
        'absence_justifications.telecharger_justificatif',
        'messagerie.messagerie_piece_jointe', 'parc.telecharger_facture',
        'recrutement.candidat_fichier', 'fiches_paie.pdf',
    )
    count = 0
    for template in (ROOT / 'templates').rglob('*.html'):
        for tag in re.findall(r'<a\b[^>]*>', template.read_text(encoding='utf-8')):
            calls = re.findall(r"url_for\(['\"]([^'\"]+)", tag)
            if any(endpoint.startswith(prefix) for endpoint in calls for prefix in endpoints):
                assert 'data-no-loading' in tag, str(template)
                count += 1
    assert count >= 20


def test_les_soumissions_programmatiques_emettent_submit():
    for name in ('base.html', 'objectifs/objectifs_liste.html', 'soldes_conges.html'):
        source = (ROOT / 'templates' / name).read_text(encoding='utf-8')
        assert '.submit()' not in source
        assert '.requestSubmit()' in source


def test_le_polling_du_badge_reste_silencieux():
    source = (ROOT / 'static/activity-panel.js').read_text(encoding='utf-8')
    polling = source.split('async function refreshMessageBadge()', 1)[1].split('function normalize', 1)[0]
    assert 'await fetch(' in polling
    assert 'AppLoading' not in polling


def test_voile_unique_et_modal_sans_blocage_si_javascript_absent(client):
    response = client.get('/login')
    html = response.get_data(as_text=True)
    assert html.count('id="appLoadingOverlay"') == 1
    assert '<dialog id="appLoadingOverlay"' in html
    assert 'aria-labelledby="appLoadingTitle"' in html
    assert 'aria-describedby="appLoadingDescription"' in html
    assert 'tabindex="-1"' in html
    assert 'id="appLoadingTitle"' in html
    # Sans attribut open, le navigateur masque la modale sans JavaScript.
    dialog_tag = re.search(r'<dialog\b[^>]*>', html).group(0)
    assert ' open' not in dialog_tag


def test_voile_sombre_blocage_et_liberation_sont_configures():
    css = (ROOT / 'static/loading.css').read_text(encoding='utf-8')
    js = (ROOT / 'static/loading.js').read_text(encoding='utf-8')
    assert '.app-loading-overlay::backdrop' in css
    assert 'rgb(8 15 28 / .68)' in css
    assert 'overflow: hidden !important' in css
    assert 'touch-action: none' in css
    assert 'overlay.showModal()' in js
    assert 'overlay.close()' in js
    assert "overlay.addEventListener('cancel'" in js
    assert 'capture: true, passive: false' in js


def test_les_fragments_ajax_n_ajoutent_pas_de_deuxieme_voile(admin_client):
    for url in ('/departements/add?modal=1', '/notifications?panel=1'):
        response = admin_client.get(url)
        assert response.status_code == 200
        assert b'id="appLoadingOverlay"' not in response.data
