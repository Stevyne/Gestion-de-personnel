"""Données des onglets préchargés : droits serveur et requêtes existantes.

Aucun cache partagé : seules les vues permises par le rôle ET l'ACL sont
calculées dans le curseur de la requête courante.
"""
from services.acl import endpoint_autorise
from services.roles import GLOBAL_DATA_ROLES, ROLE_CODES


_DEFINITIONS = (
    {'name': 'general', 'label': 'Général', 'title': 'Tableau de bord',
     'endpoint': 'dashboard.dashboard', 'roles': ROLE_CODES},
    {'name': 'rh', 'label': 'RH', 'title': 'Tableau de bord RH',
     'endpoint': 'dashboards_roles.dashboard_rh', 'roles': GLOBAL_DATA_ROLES},
    {'name': 'parc', 'label': 'Parc', 'title': 'Tableau de bord Parc',
     'endpoint': 'dashboards_roles.dashboard_parc',
     'roles': (*GLOBAL_DATA_ROLES, 'manager', 'technicien')},
    {'name': 'direction', 'label': 'Direction', 'title': 'Tableau de bord Direction',
     'endpoint': 'dashboards_roles.dashboard_direction', 'roles': GLOBAL_DATA_ROLES},
)


def onglets_autorises(role, scope_global):
    """Même périmètre que les routes, y compris le filet central de l'ACL."""
    return [dict(tab) for tab in _DEFINITIONS
            if (tab['name'] == 'general' or role in tab['roles'])
            and endpoint_autorise(role, tab['endpoint'])
            and (tab['name'] not in ('rh', 'direction') or scope_global)]


def _charger_rh(cur, scope, department_scope_sql):
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
    cur.execute("""SELECT
      (SELECT COUNT(*) FROM recrutement_demandes WHERE statut='en_attente') AS demandes_recrutement,
      (SELECT COUNT(*) FROM recrutement_offres WHERE statut='publiee') AS offres_ouvertes,
      (SELECT COUNT(*) FROM recrutement_candidatures WHERE statut='acceptee') AS candidats_acceptes""")
    recrutement = cur.fetchone()
    cards = [
        ('Employés actifs', personnel['actifs'], 'users'),
        ('Départs en préparation', personnel['departs'], 'logout'),
        ('Congés à traiter', alertes['conges'], 'palm'),
        ('Permissions à traiter', alertes['permissions'], 'receipt'),
        ('Absences à régulariser', alertes['absences'], 'ban'),
        ('Contrats à échéance ≤ 30 j', alertes['contrats_echeance'], 'file'),
        ('Documents expirés', alertes['documents_expires'], 'folder'),
        ('Demandes de recrutement', recrutement['demandes_recrutement'], 'briefcase'),
        ('Offres publiées', recrutement['offres_ouvertes'], 'megaphone'),
        ('Candidats acceptés', recrutement['candidats_acceptes'], 'star'),
        ('Masse salariale annuelle', f"{float(personnel['masse_salariale']):,.0f} Ar", 'wallet'),
    ]
    return {'description': 'Effectifs, workflows et échéances', 'cards': cards}


def _charger_parc(cur, scope, department_scope_sql):
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
    cards = [
        ('Articles', stock['articles'], 'box'), ('Stock disponible', stock['stock'], 'chart'),
        ('Alertes stock', stock['alertes'], 'alert'), ('Valeur du stock', f"{float(stock['valeur']):,.0f} Ar", 'wallet'),
        ('Exemplaires', exemplaires['total'], 'tag'), ('Exemplaires indisponibles', exemplaires['indisponibles'], 'ban'),
        ('Maintenances ouvertes', maintenance['ouvertes'], 'wrench'), ('SLA dépassés', maintenance['sla'], 'clock'),
        ('Tickets critiques', maintenance['critiques'], 'alert'), ('Inventaires ouverts', inventaires, 'clipboard'),
    ]
    portee = 'Tous les départements' if scope['is_global'] else (scope.get('department') or 'Aucun département')
    return {'description': f'Périmètre : {portee}', 'cards': cards}


def _charger_direction(cur, scope, department_scope_sql):
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
        ('Effectif actif', personnel['actifs'], 'users'), ('Départements', personnel['departements'], 'building'),
        ('Présence aujourd’hui', f'{taux} %', 'calendar'), ('Absences année', metier['absences'], 'ban'),
        ('Jours de congé approuvés', metier['conges'], 'palm'), ('Salaire moyen', f"{float(personnel['moyenne']):,.0f} Ar", 'wallet'),
        ('Masse salariale', f"{float(personnel['masse']):,.0f} Ar", 'chart'), ('Valeur du parc', f"{float(metier['valeur_parc']):,.0f} Ar", 'monitor'),
        ('Coût maintenance année', f"{float(metier['cout_maintenance']):,.0f} Ar", 'wrench'), ('Contrats à échéance', metier['contrats'], 'file'),
    ]
    return {'description': 'Vue consolidée de l’entreprise', 'cards': cards}


_CHARGEURS = {'rh': _charger_rh, 'parc': _charger_parc, 'direction': _charger_direction}


def charger_onglets_dashboard(cur, role, scope, department_scope_sql):
    onglets = onglets_autorises(role, scope['is_global'])
    for onglet in onglets:
        onglet['description'] = ''
        onglet['cards'] = []
        if onglet['name'] != 'general':
            onglet.update(_CHARGEURS[onglet['name']](cur, scope, department_scope_sql))
    return onglets
