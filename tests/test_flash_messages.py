"""Temporisation des confirmations, en page complète et dans les fragments."""
from html.parser import HTMLParser
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


class FlashMessages(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.messages = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'div' and 'data-flash-message' in attrs:
            self.messages.append(attrs)


def _messages(response):
    assert response.status_code == 200
    return FlashMessages(response.get_data(as_text=True)).messages


@pytest.mark.parametrize('category,timed', [
    ('success', True), ('info', True), ('message', True),
    ('danger', False), ('error', False), ('warning', False), ('custom', False),
])
def test_seules_les_confirmations_et_informations_expirent(client, category, timed):
    with client.session_transaction() as session:
        session['_flashes'] = [(category, 'Message de vérification')]
    response = client.get('/login')
    messages = _messages(response)
    assert len(messages) == 1
    assert messages[0].get('data-dismiss-after') == ('10000' if timed else None)
    assert messages[0]['role'] == ('alert' if category in ('danger', 'error', 'warning') else 'status')
    html = response.get_data(as_text=True)
    assert '/static/flash-messages.js' in html
    assert '/static/flash-messages.css' in html


def test_deconnexion_confirmee_pendant_dix_secondes(admin_client):
    response = admin_client.get('/logout', follow_redirects=True)
    messages = _messages(response)
    assert 'Déconnecté.'.encode() in response.data
    assert len(messages) == 1
    assert messages[0]['data-dismiss-after'] == '10000'
    with admin_client.session_transaction() as session:
        assert 'user_id' not in session


def test_erreur_authentification_ne_disparait_pas(client):
    response = client.post('/login', data={'username': 'admin', 'password': 'invalide'})
    messages = _messages(response)
    assert len(messages) == 1
    assert 'alert-danger' in messages[0]['class']
    assert 'data-dismiss-after' not in messages[0]


def test_fragment_popup_partage_le_meme_rendu_sans_script(admin_client):
    with admin_client.session_transaction() as session:
        session['_flashes'] = [('success', 'Enregistrement confirmé')]
    response = admin_client.get('/departements/add?modal=1')
    assert _messages(response)[0]['data-dismiss-after'] == '10000'
    assert b'/static/flash-messages.js' not in response.data
    assert b'<!DOCTYPE html>' not in response.data


def test_fragment_panneau_partage_le_meme_timer(admin_client):
    with admin_client.session_transaction() as session:
        session['_flashes'] = [('info', 'Action du panneau confirmée')]
    response = admin_client.get('/notifications?panel=1')
    messages = _messages(response)
    assert len(messages) == 1
    assert messages[0]['data-dismiss-after'] == '10000'
    assert 'activity-panel-flash--info' in messages[0]['class']
    assert b'/static/flash-messages.js' not in response.data


def test_texte_echappe_et_commandes_cachees_sans_javascript(client):
    with client.session_transaction() as session:
        session['_flashes'] = [('success', '<img src=x onerror=alert(1)>')]
    html = client.get('/login').get_data(as_text=True)
    assert '&lt;img src=x onerror=alert(1)&gt;' in html
    assert '<img src=x onerror=alert(1)>' not in html
    assert 'aria-label="Fermer le message" hidden' in html
    assert 'data-flash-timer aria-hidden="true" hidden' in html


def test_assets_locaux_et_mouvement_reduit(client):
    for asset in ('flash-messages.js', 'flash-messages.css'):
        response = client.get('/static/' + asset)
        assert response.status_code == 200
        source = response.get_data(as_text=True)
        assert 'https://' not in source and 'http://' not in source
    css = (ROOT / 'static/flash-messages.css').read_text(encoding='utf-8')
    assert 'prefers-reduced-motion: reduce' in css
    assert '@media print' in css
