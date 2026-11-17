"""Navigation progressive : contrat privé HTML→fragment, sans modifier les vues.

Les décorateurs, l'ACL et le masquage des liens continuent de s'appliquer. Le
transformateur doit être enregistré AVANT le masqueur (after_request inversés).
Aucune page privée n'est préchargée ou mise en cache par ce service.
"""
from functools import lru_cache
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re

from flask import jsonify, make_response, request, session

from services.acl import endpoint_autorise


READ_ENDPOINTS = frozenset('''
    dashboard.dashboard dashboards_roles.dashboard_rh dashboards_roles.dashboard_parc
    dashboards_roles.dashboard_direction index view_employee add_employee add_employee_alt edit_employee
    self_service self_service_presences self_service_conges self_service_materiels
    permissions add_permission soldes_conges_page audit rapports calendrier_conges
    absence_justifications.self_service_absences absence_justifications.formulaire_justificatif
    absences.absences absences.add_absence auth.mon_profil
    competences.competences_liste competences.competence_ajouter competences.competence_detail
    competences.competence_modifier competences.employe_competences
    conges.conges conges.add_conge contrats.contrats_liste contrats.contrat_nouveau
    contrats.contrat_voir contrats.contrat_renouveler departements.departements
    departements.add_departement departements.edit_departement departs.departs_liste
    documents.documents fiches_paie.fiche_de_paie fiches_paie.apercu
    messagerie.messagerie_inbox messagerie.messagerie_nouveau messagerie.messagerie_voir
    notifications.notifications objectifs.objectifs_liste objectifs.objectif_nouveau
    objectifs.objectif_detail objectifs.objectif_modifier
    parc.materiels parc.add_materiel parc.edit_materiel parc.view_materiel
    parc.inventaires parc.add_inventaire parc.view_inventaire parc.view_exemplaire
    parc.maintenances parc.prestataires_page parc.materiels_departement
    presences.presences presences.add_presence presences.historique recherche.recherche_page
    recrutement.tableau_recrutement recrutement.demandes recrutement.demande_nouvelle
    recrutement.demande_detail recrutement.offres recrutement.offre_nouvelle recrutement.offre_detail
    recrutement.candidats recrutement.candidat_nouveau recrutement.candidat_detail
    recrutement.candidature_detail recrutement.candidature_evaluation recrutement.entretien_nouveau
    recrutement.entretien_evaluer recrutement.comparer recrutement.embaucher
    utilisateurs.utilisateurs_page utilisateurs.register
'''.split())
NATIVE_QUERY_KEYS = frozenset({'modal', 'panel', 'print', 'download', 'attachment'})
MARKERS = {name: (f'<!-- app-navigation:{name}:start -->', f'<!-- app-navigation:{name}:end -->')
           for name in ('main', 'styles', 'scripts')}


def navigation_identity():
    return f"{session.get('user_id', '')}:{session.get('role', '')}"


def url_rule_pattern(rule):
    """Expression sans capture, issue uniquement du plan d'URL de Flask."""
    parts, position = [], 0
    for match in re.finditer(r'<(?:(\w+):)?\w+>', rule):
        parts.append(re.escape(rule[position:match.start()]))
        parts.append({'int': r'\d+', 'path': '.+', 'float': r'\d+(?:\.\d+)?'}.get(match[1], '[^/]+'))
        position = match.end()
    parts.append(re.escape(rule[position:]))
    return '^' + ''.join(parts) + '$'


def shell_version(root):
    """Un déploiement de templates/JS/CSS invalide l'ancienne coque cliente."""
    root = Path(root)
    digest = hashlib.sha256(b'teamsphere-navigation-v1')
    files = [*root.glob('templates/**/*.html'), *root.glob('static/**/*.js'), *root.glob('static/**/*.css')]
    for path in sorted(files):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:20]


class NavigationMetadata(HTMLParser):
    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.title = ''
        self.in_title = False
        self.body_class = ''
        self.nav = []
        self.in_counts = False
        self.counts_text = ''
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'title':
            self.in_title = True
        elif tag == 'body':
            self.body_class = attrs.get('class', '')
        if tag == 'script' and attrs.get('id') == 'appNavigationCounts':
            self.in_counts = True
        if {'nav-link', 'nav-dropdown-link', 'nav-notif'}.intersection(attrs.get('class', '').split()):
            self.nav.append({'tag': tag, 'href': attrs.get('href'),
                             'active': 'active' in attrs['class'].split()})

    def handle_endtag(self, tag):
        if tag == 'title':
            self.in_title = False
        if tag == 'script':
            self.in_counts = False

    def handle_data(self, text):
        if self.in_title:
            self.title += text
        if self.in_counts:
            self.counts_text += text


def extraire_fragment(html):
    sections = {}
    for name, (start, end) in MARKERS.items():
        if html.count(start) != 1 or html.count(end) != 1 or html.index(start) >= html.index(end):
            return None
        sections[name] = html.split(start, 1)[1].split(end, 1)[0].strip()
    metadata = NavigationMetadata(html)
    try:
        counts = json.loads(metadata.counts_text or '{}')
    except (TypeError, ValueError):
        return None
    if not isinstance(counts, dict):
        return None
    return {**sections, 'title': metadata.title, 'bodyClass': metadata.body_class, 'nav': metadata.nav,
            'badges': counts}


def configurer_navigation(app):
    version = shell_version(app.root_path)

    @lru_cache(maxsize=16)
    def routes(role):
        # Évaluation différée : tous les blueprints sont enregistrés à la
        # première requête, pas nécessairement lors de cet appel de config.
        # Cette liste est une amélioration d'interface, jamais une ACL. Comme
        # dans le menu, le journal d'audit reste invisible hors administrateur
        # même si le rôle RH contourne le filtre ACL général.
        return sorted({url_rule_pattern(rule.rule) for rule in app.url_map.iter_rules()
                       if rule.endpoint in READ_ENDPOINTS and 'GET' in rule.methods
                       and endpoint_autorise(role, rule.endpoint)
                       and (rule.endpoint != 'audit' or role == 'admin')})

    @app.context_processor
    def navigation_context():
        enabled = bool(session.get('user_id'))
        return {'app_navigation_config': {
            'version': version, 'identity': navigation_identity(),
            'enabled': enabled, 'routes': routes(session.get('role', 'employe')) if enabled else [],
        }}

    def fast_request():
        return request.method == 'GET' and request.headers.get('X-App-Navigation') == '1'

    def current_url():
        return request.full_path if request.query_string else request.path

    def native_response(target):
        response = make_response('', 204)
        response.headers['X-App-Native-Navigation'] = target
        return response

    @app.before_request
    def navigation_guard():
        if not fast_request():
            return None
        if (not session.get('user_id') or request.endpoint not in READ_ENDPOINTS
                or NATIVE_QUERY_KEYS.intersection(request.args)
                or request.headers.get('X-App-Identity') != navigation_identity()
                or request.headers.get('X-App-Version') != version):
            return native_response(current_url())
        return None

    @app.after_request
    def navigation_fragment(response):
        if not fast_request():
            if request.method == 'GET' and request.endpoint in READ_ENDPOINTS:
                # Même URL, deux représentations : le HTML natif ne doit pas
                # devenir un cache de données privées ni masquer le contrat JSON.
                response.headers['Cache-Control'] = 'private, no-store'
                response.vary.update(('X-App-Navigation', 'X-App-Identity', 'X-App-Version', 'Cookie'))
            return response
        # Ne pas suivre une redirection dans fetch : les messages flash
        # doivent rester disponibles pour la vraie page cible.
        if response.status_code in (301, 302, 303, 307, 308) and response.headers.get('Location'):
            target = response.headers['Location']
            response = make_response('', 204)
            response.headers['X-App-Redirect'] = target
        elif response.status_code == 200 and response.mimetype == 'text/html':
            fragment = extraire_fragment(response.get_data(as_text=True))
            if fragment is None:
                response = native_response(current_url())
            else:
                response = jsonify({**fragment, 'version': version, 'identity': navigation_identity(),
                                    'url': current_url()})
                response.headers['X-App-Navigation'] = version
        response.headers['Cache-Control'] = 'private, no-store'
        response.vary.update(('X-App-Navigation', 'X-App-Identity', 'X-App-Version', 'Cookie'))
        return response
