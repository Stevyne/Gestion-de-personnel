"""Tableaux de bord spécialisés RH, Parc et Direction."""

from flask import Blueprint, render_template


def creer_blueprint_dashboards_roles(deps):
    bp = Blueprint('dashboards_roles', __name__)
    db_cursor = deps['db_cursor']
    login_required = deps['login_required']
    role_required = deps['role_required']
    department_scope_sql = deps['department_scope_sql']
    get_department_scope = deps['get_department_scope']

    @bp.route('/dashboard/rh')
    @login_required
    @role_required('rh')
    def dashboard_rh():
        with db_cursor() as (conn, cur):
            cur.execute("""SELECT
              COUNT(*) FILTER(WHERE actif) AS actifs,
              COUNT(*) FILTER(WHERE statut_depart='preparation') AS departs,
              COALESCE(SUM(salaire) FILTER(WHERE actif),0) AS masse_salariale
              FROM employes""")
            personnel = cur.fetchone()
            cur.execute("""SELECT
              (SELECT COUNT(*) FROM conges WHERE statut IN ('en attente','avis rendu')) AS conges,
              (SELECT COUNT(*) FROM permissions WHERE statut IN ('en attente','avis rendu')) AS permissions,
              (SELECT COUNT(*) FROM absences WHERE statut IN ('non_justifiee','justificatif_depose','refusee')) AS absences,
              (SELECT COUNT(*) FROM documents WHERE date_expiration<CURRENT_DATE) AS documents_expires,
              (SELECT COUNT(*) FROM contrats WHERE statut='actif' AND date_fin<=CURRENT_DATE+INTERVAL '30 days') AS contrats_echeance""")
            alertes = cur.fetchone()
        cards = [
            ('Employés actifs', personnel['actifs'], '👥'),
            ('Départs en préparation', personnel['departs'], '🚪'),
            ('Congés à traiter', alertes['conges'], '🏖️'),
            ('Permissions à traiter', alertes['permissions'], '🧾'),
            ('Absences à régulariser', alertes['absences'], '🚫'),
            ('Contrats à échéance ≤ 30 j', alertes['contrats_echeance'], '📑'),
            ('Documents expirés', alertes['documents_expires'], '📁'),
            ('Masse salariale annuelle', f"{float(personnel['masse_salariale']):,.0f} Ar", '💰'),
        ]
        return render_template('dashboard_role.html', titre='Tableau de bord RH',
                               sous_titre='Effectifs, workflows et échéances', cards=cards,
                               dashboard_actif='rh')

    @bp.route('/dashboard/parc')
    @login_required
    @role_required('rh', 'manager', 'technicien')
    def dashboard_parc():
        with db_cursor() as (conn, cur):
            dept_scope, dept_params = department_scope_sql('d', 'nom', cur)
            cur.execute(f"""SELECT COUNT(*) AS articles,COALESCE(SUM(m.quantite),0) AS stock,
              COUNT(*) FILTER(WHERE m.seuil_alerte>0 AND m.quantite<=m.seuil_alerte) AS alertes,
              COALESCE(SUM(COALESCE(m.prix_acquisition,0)*m.quantite),0) AS valeur
              FROM materiels m LEFT JOIN departements d ON d.id=m.departement_id
              WHERE {dept_scope}""", dept_params)
            stock = cur.fetchone()
            cur.execute(f"""SELECT COUNT(*) AS total,
              COUNT(*) FILTER(WHERE ex.etat IN ('panne','reparation','rebut')) AS indisponibles
              FROM materiel_exemplaires ex JOIN materiels m ON m.id=ex.materiel_id
              LEFT JOIN departements d ON d.id=m.departement_id WHERE {dept_scope}""", dept_params)
            exemplaires = cur.fetchone()
            cur.execute(f"""SELECT
              COUNT(*) FILTER(WHERE mt.statut IN ('signale','assigne','envoye','a_valider')) AS ouvertes,
              COUNT(*) FILTER(WHERE mt.statut IN ('signale','assigne','envoye','a_valider') AND mt.sla_echeance<CURRENT_TIMESTAMP) AS sla,
              COUNT(*) FILTER(WHERE mt.priorite='critique' AND mt.statut IN ('signale','assigne','envoye','a_valider')) AS critiques,
              COALESCE(SUM(mt.cout),0) AS cout
              FROM materiel_maintenances mt JOIN materiel_exemplaires ex ON ex.id=mt.exemplaire_id
              JOIN materiels m ON m.id=ex.materiel_id LEFT JOIN departements d ON d.id=m.departement_id
              WHERE {dept_scope}""", dept_params)
            maintenance = cur.fetchone()
            cur.execute(f"""SELECT COUNT(*) AS n FROM inventaires i
              LEFT JOIN departements d ON d.id=i.departement_id
              WHERE i.statut='en_cours' AND {dept_scope}""", dept_params)
            inventaires = cur.fetchone()['n']
            scope = get_department_scope(cur)
        cards = [
            ('Articles', stock['articles'], '📦'), ('Stock disponible', stock['stock'], '📊'),
            ('Alertes stock', stock['alertes'], '⚠️'), ('Valeur du stock', f"{float(stock['valeur']):,.0f} Ar", '💰'),
            ('Exemplaires', exemplaires['total'], '🏷️'), ('Exemplaires indisponibles', exemplaires['indisponibles'], '🔴'),
            ('Maintenances ouvertes', maintenance['ouvertes'], '🔧'), ('SLA dépassés', maintenance['sla'], '⏱️'),
            ('Tickets critiques', maintenance['critiques'], '🚨'), ('Inventaires ouverts', inventaires, '📋'),
        ]
        portee = 'Tous les départements' if scope['is_global'] else (scope.get('department') or 'Aucun département')
        return render_template('dashboard_role.html', titre='Tableau de bord Parc',
                               sous_titre=f'Périmètre : {portee}', cards=cards,
                               dashboard_actif='parc')

    @bp.route('/dashboard/direction')
    @login_required
    @role_required('rh')
    def dashboard_direction():
        with db_cursor() as (conn, cur):
            cur.execute("""SELECT COUNT(*) FILTER(WHERE actif) AS actifs,
              COUNT(DISTINCT departement) FILTER(WHERE actif) AS departements,
              COALESCE(SUM(salaire) FILTER(WHERE actif),0) AS masse,
              COALESCE(AVG(salaire) FILTER(WHERE actif),0) AS moyenne FROM employes""")
            personnel = cur.fetchone()
            cur.execute("""SELECT
              (SELECT COUNT(DISTINCT employe_id) FROM presences WHERE date=CURRENT_DATE) AS presents,
              (SELECT COUNT(*) FROM absences WHERE EXTRACT(YEAR FROM date)=EXTRACT(YEAR FROM CURRENT_DATE)) AS absences,
              (SELECT COALESCE(SUM(nombre_jours),0) FROM conges WHERE statut='approuvé' AND EXTRACT(YEAR FROM date_debut)=EXTRACT(YEAR FROM CURRENT_DATE)) AS conges,
              (SELECT COALESCE(SUM(COALESCE(prix_acquisition,0)*quantite),0) FROM materiels) AS valeur_parc,
              (SELECT COALESCE(SUM(cout),0) FROM materiel_maintenances WHERE EXTRACT(YEAR FROM date_signalement)=EXTRACT(YEAR FROM CURRENT_DATE)) AS cout_maintenance,
              (SELECT COUNT(*) FROM contrats WHERE statut='actif' AND date_fin<=CURRENT_DATE+INTERVAL '30 days') AS contrats""")
            metier = cur.fetchone()
        taux = round(100 * metier['presents'] / personnel['actifs'], 1) if personnel['actifs'] else 0
        cards = [
            ('Effectif actif', personnel['actifs'], '👥'), ('Départements', personnel['departements'], '🏢'),
            ('Présence aujourd’hui', f'{taux} %', '📅'), ('Absences année', metier['absences'], '🚫'),
            ('Jours de congé approuvés', metier['conges'], '🏖️'), ('Salaire moyen', f"{float(personnel['moyenne']):,.0f} Ar", '💰'),
            ('Masse salariale', f"{float(personnel['masse']):,.0f} Ar", '📈'), ('Valeur du parc', f"{float(metier['valeur_parc']):,.0f} Ar", '💻'),
            ('Coût maintenance année', f"{float(metier['cout_maintenance']):,.0f} Ar", '🔧'), ('Contrats à échéance', metier['contrats'], '📑'),
        ]
        return render_template('dashboard_role.html', titre='Tableau de bord Direction',
                               sous_titre='Vue consolidée de l’entreprise', cards=cards,
                               dashboard_actif='direction')

    return bp
