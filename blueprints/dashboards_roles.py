"""Anciennes URLs conservées, désormais points d'entrée du dashboard unifié."""
from flask import Blueprint

from blueprints.dashboard import rendre_dashboard


def creer_blueprint_dashboards_roles(deps):
    bp = Blueprint('dashboards_roles', __name__)
    login_required = deps['login_required']
    role_required = deps['role_required']

    @bp.route('/dashboard/rh')
    @login_required
    @role_required('rh')
    def dashboard_rh():
        return rendre_dashboard(deps, 'rh')

    @bp.route('/dashboard/parc')
    @login_required
    @role_required('rh', 'manager', 'technicien')
    def dashboard_parc():
        return rendre_dashboard(deps, 'parc')

    @bp.route('/dashboard/direction')
    @login_required
    @role_required('rh')
    def dashboard_direction():
        return rendre_dashboard(deps, 'direction')

    return bp
