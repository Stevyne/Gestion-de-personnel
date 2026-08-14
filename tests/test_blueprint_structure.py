"""Garde-fous structurels pour empêcher le retour au monolithe app.py."""

from pathlib import Path

import app as application


ROOT = Path(__file__).resolve().parents[1]


def test_app_py_reste_sous_3200_lignes():
    lignes = (ROOT / 'app.py').read_text(encoding='utf-8').count('\n') + 1
    assert lignes < 3200, f'app.py est remonté à {lignes} lignes'


def test_blueprints_metier_sont_enregistres(app):
    assert {'parc', 'documents', 'departements', 'presences', 'utilisateurs',
            'auth', 'departs', 'contrats', 'rapports_parc', 'dashboards_roles',
            'dashboard', 'recherche', 'conges', 'absences', 'notifications',
            'recrutement', 'absence_justifications', 'messagerie'} <= set(app.blueprints)


def test_urls_publiques_des_modules_extraits_sont_inchangees(app):
    attendu = {
        '/materiels': 'parc.materiels',
        '/inventaires': 'parc.inventaires',
        '/maintenances': 'parc.maintenances',
        '/documents': 'documents.documents',
        '/departements': 'departements.departements',
        '/presences': 'presences.presences',
        '/historique': 'presences.historique',
        '/utilisateurs': 'utilisateurs.utilisateurs_page',
        '/login': 'auth.login',
        '/mon-profil': 'auth.mon_profil',
        '/': 'dashboard.dashboard',
        '/recherche': 'recherche.recherche_page',
        '/api/recherche': 'recherche.api_recherche',
        '/conges': 'conges.conges',
        '/absences': 'absences.absences',
        '/notifications': 'notifications.notifications',
        '/recrutement': 'recrutement.tableau_recrutement',
        '/recrutement/demandes': 'recrutement.demandes',
        '/recrutement/offres': 'recrutement.offres',
        '/recrutement/candidats': 'recrutement.candidats',
        '/departs': 'departs.departs_liste',
        '/contrats': 'contrats.contrats_liste',
        '/export/materiels/pdf': 'rapports_parc.export_materiels_pdf',
        '/dashboard/rh': 'dashboards_roles.dashboard_rh',
        '/dashboard/parc': 'dashboards_roles.dashboard_parc',
        '/dashboard/direction': 'dashboards_roles.dashboard_direction',
    }
    regles = {rule.rule: rule.endpoint for rule in app.url_map.iter_rules()}
    for url, endpoint in attendu.items():
        assert regles[url] == endpoint


def test_routes_extraites_ne_sont_plus_definies_dans_app_py():
    source = (ROOT / 'app.py').read_text(encoding='utf-8')
    for definition in ('def materiels():', 'def inventaires():',
                       'def maintenances():', 'def documents():',
                       'def departements():', 'def presences():',
                       'def historique():', 'def utilisateurs_page():',
                       'def register():', 'def login():', 'def mon_profil():',
                       'def dashboard():', 'def recherche_globale(',
                       'def conges():', 'def add_conge():', 'def absences():',
                       'def notifications():'):
        assert definition not in source


def test_palette_de_recherche_reference_des_endpoints_existants(app):
    for _libelle, endpoint, _roles, _icone in application.RECHERCHE_PAGES:
        assert endpoint in app.view_functions, endpoint


def test_configuration_et_services_communs_sont_decouples():
    attendus = {
        'services/configuration.py', 'services/security.py',
        'services/database.py', 'services/schema.py', 'services/common.py',
        'services/migrations.py', 'services/notifications.py', 'migrations/env.py',
        'migrations/versions/20260813_phase4_production.py',
        'migrations/versions/20260814_recrutement.py',
    }
    assert all((ROOT / chemin).exists() for chemin in attendus)
    source = (ROOT / 'app.py').read_text(encoding='utf-8')
    assert 'def get_db():' not in source
    assert 'def get_cursor(' not in source
    assert 'def pagination_info(' not in source
    assert 'def create_notification(' not in source
    assert 'CREATE TABLE IF NOT EXISTS departements' not in source


def test_valeurs_metier_ne_sont_pas_renommees_comme_des_endpoints():
    presences = (ROOT / 'templates' / 'presences.html').read_text(encoding='utf-8')
    rapports = (ROOT / 'templates' / 'rapports.html').read_text(encoding='utf-8')
    assert 'value="clock_in"' in presences
    assert 'value="clock_out"' in presences
    assert 'value="presences"' in rapports
    assert "type_rapport == 'presences'" in rapports


def test_constantes_compatibles_reexportees():
    # Les intégrations historiques qui importent ces constantes depuis app
    # continuent de fonctionner malgré leur déplacement dans le Blueprint.
    assert application.MAINTENANCE_OUVERTS
    assert application.MAINTENANCE_VALIDATION_JOURS > 0
