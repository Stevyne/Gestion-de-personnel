"""Garde-fous structurels pour empêcher le retour au monolithe app.py."""

from pathlib import Path

import app as application


ROOT = Path(__file__).resolve().parents[1]


def test_app_py_reste_sous_5600_lignes():
    lignes = (ROOT / 'app.py').read_text(encoding='utf-8').count('\n') + 1
    assert lignes < 5600, f'app.py est remonté à {lignes} lignes'


def test_blueprints_metier_sont_enregistres(app):
    assert {'parc', 'documents', 'departements', 'absence_justifications',
            'messagerie'} <= set(app.blueprints)


def test_urls_publiques_des_modules_extraits_sont_inchangees(app):
    attendu = {
        '/materiels': 'parc.materiels',
        '/inventaires': 'parc.inventaires',
        '/maintenances': 'parc.maintenances',
        '/documents': 'documents.documents',
        '/departements': 'departements.departements',
    }
    regles = {rule.rule: rule.endpoint for rule in app.url_map.iter_rules()}
    for url, endpoint in attendu.items():
        assert regles[url] == endpoint


def test_routes_extraites_ne_sont_plus_definies_dans_app_py():
    source = (ROOT / 'app.py').read_text(encoding='utf-8')
    for definition in ('def materiels():', 'def inventaires():',
                       'def maintenances():', 'def documents():',
                       'def departements():'):
        assert definition not in source


def test_palette_de_recherche_reference_des_endpoints_existants(app):
    for _libelle, endpoint, _roles, _icone in application.RECHERCHE_PAGES:
        assert endpoint in app.view_functions, endpoint


def test_constantes_compatibles_reexportees():
    # Les intégrations historiques qui importent ces constantes depuis app
    # continuent de fonctionner malgré leur déplacement dans le Blueprint.
    assert application.MAINTENANCE_OUVERTS
    assert application.MAINTENANCE_VALIDATION_JOURS > 0
