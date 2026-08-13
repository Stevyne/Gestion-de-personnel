"""Parc matériel, inventaires physiques et maintenance.

Extrait du monolithe historique. La fabrique reçoit explicitement les services
partagés afin d'éviter les imports circulaires et de garder ce module testable.
Les URLs publiques sont inchangées ; seuls les noms d'endpoints Flask portent
le préfixe ``parc.``.
"""

import calendar
from datetime import date, datetime
import io
import os
import re
from urllib.parse import urlencode

from flask import (Blueprint, flash, redirect, render_template, request,
                   session, url_for)
from markupsafe import Markup, escape
import psycopg2

MATERIEL_CATEGORIES = [
    ('fourniture', '✏️ Fourniture de bureau'),
    ('papeterie', '📄 Papeterie'),
    ('mobilier', '🪑 Mobilier'),
    ('informatique', '💻 Informatique'),
    ('entretien', '🧹 Entretien'),
    ('autre', '📦 Autre'),
]
MATERIEL_CATEGORIES_DICT = dict(MATERIEL_CATEGORIES)

# Catégories dont les articles sont attribuables durablement à un employé
# (un PC se remet à quelqu'un ; une ramette de papier se consomme).
MATERIEL_CAT_ATTRIBUABLES = {'informatique', 'mobilier', 'autre'}

# ---------------------------------------------------------------------------
# GESTION DE PARC : numéros d'inventaire, états, maintenance, QR
# ---------------------------------------------------------------------------

# Préfixe par défaut du numéro d'inventaire, selon la catégorie.
MATERIEL_PREFIXES = {
    'informatique': 'PC',
    'mobilier':     'MOB',
    'papeterie':    'PAP',
    'fourniture':   'FOU',
    'entretien':    'ENT',
    'autre':        'MAT',
}

EXEMPLAIRE_ETATS = [
    ('bon',        'Bon état'),
    ('usage',      'Usagé'),
    ('panne',      'En panne'),
    ('reparation', 'En réparation'),
    ('rebut',      'Mis au rebut'),
]
EXEMPLAIRE_ETATS_DICT = dict(EXEMPLAIRE_ETATS)

# États dans lesquels l'exemplaire n'est pas utilisable.
EXEMPLAIRE_ETATS_INDISPONIBLES = {'panne', 'reparation', 'rebut'}

MAINTENANCE_STATUTS = [
    ('signale',     'Panne signalée'),
    ('assigne',     'Assignée'),
    ('envoye',      'En réparation'),
    ('a_valider',   'Retour à valider'),
    ('repare',      'Réparé'),
    ('irreparable', 'Irréparable'),
    ('annule',      'Annulé'),
]
MAINTENANCE_STATUTS_DICT = dict(MAINTENANCE_STATUTS)
# Une intervention est « en cours » tant qu'elle n'est pas close. `a_valider`
# en fait partie : le matériel est revenu mais le demandeur n'a pas encore
# confirmé que la panne est réellement résolue.
MAINTENANCE_OUVERTS = ('signale', 'assigne', 'envoye', 'a_valider')
# Étapes où le matériel est physiquement indisponible pour son utilisateur.
MAINTENANCE_INDISPONIBLE = ('signale', 'assigne', 'envoye')
# Délai au-delà duquel un retour sans réponse du demandeur est réputé validé.
MAINTENANCE_VALIDATION_JOURS = int(os.environ.get('MAINTENANCE_VALIDATION_JOURS', 7))
GLOBAL_DATA_ROLES = ('admin', 'rh')


def creer_blueprint_parc(deps):
    """Construit le Blueprint du parc avec ses dépendances applicatives."""
    bp = Blueprint('parc', __name__)
    db_cursor = deps['db_cursor']
    login_required = deps['login_required']
    role_required = deps['role_required']
    create_notification = deps['create_notification']
    _notifier_roles = deps['notifier_roles']
    log_action = deps['log_action']
    get_current_employee = deps['get_current_employee']
    _user_id_de_employe = deps['user_id_de_employe']
    pagination_info = deps['pagination_info']
    page_list = deps['page_list']
    department_scope_sql = deps['department_scope_sql']
    get_department_scope = deps['get_department_scope']
    _department_access_denied = deps['department_access_denied']
    logger = deps['logger']

    def _prefixe_materiel(materiel):
        """Préfixe du numéro d'inventaire : celui saisi, sinon celui de la catégorie."""
        perso = (materiel.get('prefixe_inventaire') or '').strip().upper()
        if perso:
            return re.sub(r'[^A-Z0-9]', '', perso)[:12] or 'MAT'
        return MATERIEL_PREFIXES.get(materiel.get('categorie'), 'MAT')


    def _generer_numero_inventaire(cur, materiel, annee=None):
        """Réserve et renvoie le prochain numéro (ex. PC-2026-001).

        Le compteur est incrémenté en base sous verrou (UPDATE ... RETURNING),
        ce qui garantit l'unicité même si deux utilisateurs créent des
        exemplaires en même temps.
        """
        prefixe = _prefixe_materiel(materiel)
        annee = annee or date.today().year
        cur.execute("""
            INSERT INTO materiel_compteurs (prefixe, annee, dernier)
            VALUES (%s, %s, 1)
            ON CONFLICT (prefixe, annee)
            DO UPDATE SET dernier = materiel_compteurs.dernier + 1
            RETURNING dernier
        """, (prefixe, annee))
        n = cur.fetchone()['dernier']
        return f"{prefixe}-{annee}-{n:03d}"


    def _garantie_fin(date_acq, duree_mois):
        """Date de fin de garantie, sans dépendance externe (pas de dateutil)."""
        if not date_acq or not duree_mois:
            return None
        mois = date_acq.month - 1 + int(duree_mois)
        an = date_acq.year + mois // 12
        mois = mois % 12 + 1
        # 31 janvier + 1 mois → 28/29 février
        dernier_jour = calendar.monthrange(an, mois)[1]
        return date(an, mois, min(date_acq.day, dernier_jour))


    def _qr_svg(donnee, taille=6):
        """QR code en SVG inline (aucun fichier écrit, aucun binaire requis).

        Renvoie None si la bibliothèque `qrcode` n'est pas installée : le module
        reste alors pleinement fonctionnel, seul le QR disparaît de l'affichage.
        """
        try:
            import qrcode
            import qrcode.image.svg
        except ImportError:
            return None
        qr = qrcode.QRCode(version=None, box_size=taille, border=2,
                           error_correction=qrcode.constants.ERROR_CORRECT_M)
        qr.add_data(donnee)
        qr.make(fit=True)
        img = qr.make_image(image_factory=qrcode.image.svg.SvgPathImage)
        flux = io.BytesIO()
        img.save(flux)
        svg = flux.getvalue().decode('utf-8')
        # On retire la déclaration XML : le SVG est inséré dans du HTML.
        return re.sub(r'<\?xml[^>]*\?>\s*', '', svg)


    def _champs_patrimoine(form):
        """Lit les champs patrimoniaux du formulaire matériel.

        Renvoie (valeurs, erreur). Les montants acceptent « 3 500 000 » ou
        « 3500000,50 » : espaces et virgule décimale sont normalisés.
        """
        marque = (form.get('marque') or '').strip()
        modele = (form.get('modele') or '').strip()
        fournisseur = (form.get('fournisseur') or '').strip()
        prefixe = re.sub(r'[^A-Za-z0-9]', '', (form.get('prefixe_inventaire') or '')).upper()[:12]
        date_acq = (form.get('date_acquisition') or '').strip() or None
        duree = form.get('duree_garantie_mois', type=int)
        prix_brut = (form.get('prix_acquisition') or '').strip().replace(' ', '').replace('\u202f', '').replace(',', '.')

        prix = None
        if prix_brut:
            try:
                prix = float(prix_brut)
            except ValueError:
                return None, "Le prix d'acquisition doit être un nombre."
            if prix < 0:
                return None, "Le prix d'acquisition ne peut pas être négatif."
        if duree is not None and duree < 0:
            return None, "La durée de garantie ne peut pas être négative."

        return {
            'marque': marque or None, 'modele': modele or None,
            'fournisseur': fournisseur or None, 'prefixe_inventaire': prefixe or None,
            'date_acquisition': date_acq, 'duree_garantie_mois': duree,
            'prix_acquisition': prix,
        }, None


    def _peut_gerer_materiels():
        return session.get('role') in ('admin', 'rh', 'manager')


    # ==================== WORKFLOW DE MAINTENANCE (4 acteurs) ====================
    # Rôles pilotes : ils assignent, arbitrent et peuvent forcer une clôture.
    MAINTENANCE_PILOTES = ('admin', 'rh', 'manager')


    def _est_pilote_maintenance():
        return session.get('role') in MAINTENANCE_PILOTES


    def _notifier_pilotes(cur, titre, message, type_='warning', sauf=None,
                         departement=None):
        """Prévient admin/RH et uniquement les managers du département concerné."""
        cur.execute("""
            SELECT u.id FROM users u LEFT JOIN employes e ON e.id=u.employe_id
             WHERE u.role IN ('admin','rh')
                OR (u.role='manager' AND e.departement=%s)
        """, (departement,))
        for row in cur.fetchall():
            if sauf and row['id'] == sauf:
                continue
            create_notification(row['id'], titre, message, type_)


    def _acteurs_intervention(mt):
        """Qui a le droit de faire quoi sur cette intervention, pour l'utilisateur courant."""
        uid = session.get('user_id')
        pilote = _est_pilote_maintenance()
        est_assigne = bool(mt.get('assigne_user_id')) and mt['assigne_user_id'] == uid
        est_demandeur = bool(mt.get('signale_par_id')) and mt['signale_par_id'] == uid
        return {
            # Assigner / réassigner : uniquement les pilotes.
            'peut_assigner': pilote and mt['statut'] in ('signale', 'assigne'),
            # Démarrer : l'assigné interne lui-même, ou un pilote (cas prestataire).
            'peut_demarrer': mt['statut'] == 'assigne' and (pilote or est_assigne),
            # Saisir le retour d'atelier : l'exécutant interne, ou un pilote qui
            # retranscrit le retour d'un prestataire externe. Autorisé dès
            # l'assignation : un technicien peut diagnostiquer un matériel mort
            # sur place, sans passer par un départ en atelier.
            'peut_executer': mt['statut'] in ('assigne', 'envoye') and (pilote or est_assigne),
            # Valider : le demandeur. Un pilote peut forcer (employé absent/parti).
            'peut_valider': mt['statut'] == 'a_valider' and est_demandeur,
            'peut_forcer': mt['statut'] == 'a_valider' and pilote,
            'peut_annuler': pilote and mt['statut'] in MAINTENANCE_OUVERTS,
            'est_demandeur': est_demandeur,
            'est_assigne': est_assigne,
            'est_pilote': pilote,
        }


    @bp.app_context_processor
    def inject_materiel_perms():
        return {'peut_gerer_materiels': _peut_gerer_materiels()}


    def _notifier_stock_bas(cur, materiel_id):
        """Notifie les gestionnaires quand un stock passe sous son seuil.

        Anti-spam : `alerte_envoyee` empêche de renvoyer la notification tant que
        le stock n'est pas repassé au-dessus du seuil.
        """
        try:
            cur.execute("""
                SELECT m.id, m.nom, m.quantite, m.seuil_alerte, m.unite,
                       m.alerte_envoyee, COALESCE(d.nom, 'Sans département') AS dept
                FROM materiels m
                LEFT JOIN departements d ON d.id = m.departement_id
                WHERE m.id = %s
            """, (materiel_id,))
            m = cur.fetchone()
            if not m or not m['seuil_alerte']:
                return

            en_alerte = m['quantite'] <= m['seuil_alerte']

            if en_alerte and not m['alerte_envoyee']:
                cur.execute("""SELECT u.id FROM users u
                               LEFT JOIN employes e ON e.id=u.employe_id
                               WHERE u.role IN ('admin','rh')
                                  OR (u.role='manager' AND e.departement=%s)""",
                            (m['dept'],))
                for u in cur.fetchall():
                    create_notification(
                        u['id'],
                        f"Stock bas : {m['nom']}",
                        f"Il reste {m['quantite']} {m['unite']} de « {m['nom']} » "
                        f"({m['dept']}), seuil d'alerte : {m['seuil_alerte']}.",
                        "warning",
                    )
                cur.execute("UPDATE materiels SET alerte_envoyee = TRUE WHERE id = %s", (materiel_id,))
            elif not en_alerte and m['alerte_envoyee']:
                # Stock réapprovisionné : on réarme l'alerte pour la prochaine fois.
                cur.execute("UPDATE materiels SET alerte_envoyee = FALSE WHERE id = %s", (materiel_id,))
        except Exception as e:
            logger.error("Erreur alerte stock matériel %s: %s", materiel_id, e, exc_info=True)


    def _enregistrer_mouvement(cur, materiel_id, type_mouvement, quantite,
                               employe_id=None, motif=None, origine='manuel'):
        """Écrit un mouvement et met à jour le stock. Retourne le nouveau stock.

        Lève ValueError si une sortie dépasse le stock disponible.
        """
        cur.execute("SELECT quantite FROM materiels WHERE id = %s FOR UPDATE", (materiel_id,))
        row = cur.fetchone()
        if not row:
            raise ValueError("Matériel introuvable")

        stock = row['quantite']
        if type_mouvement == 'sortie':
            if quantite > stock:
                raise ValueError(f"Stock insuffisant : il ne reste que {stock} unité(s)")
            nouveau = stock - quantite
        else:
            nouveau = stock + quantite

        cur.execute("UPDATE materiels SET quantite = %s WHERE id = %s", (nouveau, materiel_id))
        cur.execute("""
            INSERT INTO materiels_mouvements
                (materiel_id, type_mouvement, quantite, employe_id, motif, user_id,
                 username, origine)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, (materiel_id, type_mouvement, quantite, employe_id, motif or None,
              session.get('user_id'), session.get('username'), origine))

        _notifier_stock_bas(cur, materiel_id)
        return nouveau


    @bp.route('/materiels')
    @login_required
    def materiels():
        page = max(1, request.args.get('page', 1, type=int))
        per_page = 10
        search = (request.args.get('search') or '').strip()
        dept_id = request.args.get('departement', type=int)
        categorie = (request.args.get('categorie') or '').strip()
        etat = (request.args.get('etat') or '').strip()  # '' | 'alerte' | 'rupture'

        where, params = [], []
        if search:
            where.append("m.nom ILIKE %s")
            params.append(f"%{search}%")
        if dept_id:
            where.append("m.departement_id = %s")
            params.append(dept_id)
        if categorie:
            where.append("m.categorie = %s")
            params.append(categorie)
        if etat == 'alerte':
            where.append("m.seuil_alerte > 0 AND m.quantite <= m.seuil_alerte")
        elif etat == 'rupture':
            where.append("m.quantite = 0")
        with db_cursor() as (conn, cur):
            dept_scope, dept_params = department_scope_sql('d', 'nom', cur)
            where.append(dept_scope)
            params.extend(dept_params)
            clause = "WHERE " + " AND ".join(where)
            cur.execute(f"""SELECT COUNT(*) AS total FROM materiels m
                            LEFT JOIN departements d ON d.id = m.departement_id
                            {clause}""", params)
            total = cur.fetchone()['total'] or 0
            pag = pagination_info(total, page, per_page)

            cur.execute(f"""
                SELECT m.*, COALESCE(d.nom, '—') AS departement_nom,
                       COALESCE((
                           SELECT SUM(a.quantite) FROM materiels_attributions a
                           WHERE a.materiel_id = m.id AND a.date_retour IS NULL
                       ), 0) AS nb_attribues
                FROM materiels m
                LEFT JOIN departements d ON d.id = m.departement_id
                {clause}
                ORDER BY (m.seuil_alerte > 0 AND m.quantite <= m.seuil_alerte) DESC,
                         d.nom NULLS LAST, m.nom
                LIMIT %s OFFSET %s
            """, params + [per_page, (pag['page'] - 1) * per_page])
            liste = cur.fetchall()

            cur.execute(f"""
                SELECT COUNT(*) AS nb_articles,
                       COALESCE(SUM(m.quantite), 0) AS stock_total,
                       COUNT(*) FILTER (WHERE m.seuil_alerte > 0 AND m.quantite <= m.seuil_alerte) AS nb_alertes,
                       COUNT(*) FILTER (WHERE m.quantite = 0) AS nb_ruptures
                FROM materiels m LEFT JOIN departements d ON d.id = m.departement_id
                WHERE {dept_scope}
            """, dept_params)
            stats = cur.fetchone()

            cur.execute(f"SELECT d.id, d.nom FROM departements d WHERE {dept_scope} ORDER BY d.nom",
                        dept_params)
            depts = cur.fetchall()

        filters = {'search': search, 'departement': dept_id or '',
                   'categorie': categorie, 'etat': etat}
        return render_template('materiels.html',
                               materiels=liste,
                               stats=stats,
                               departements=depts,
                               categories=MATERIEL_CATEGORIES,
                               categories_dict=MATERIEL_CATEGORIES_DICT,
                               pg=pag,
                               page_items=page_list(pag['page'], pag['pages']),
                               base_qs=urlencode({k: v for k, v in filters.items() if v}),
                               filters=filters)


    @bp.route('/materiels/add', methods=['GET', 'POST'])
    @login_required
    @role_required('rh', 'manager')
    def add_materiel():
        if request.method == 'POST':
            nom = (request.form.get('nom') or '').strip()
            categorie = (request.form.get('categorie') or 'fourniture').strip()
            dept_id = request.form.get('departement_id', type=int)
            quantite = request.form.get('quantite', type=int) or 0
            seuil = request.form.get('seuil_alerte', type=int) or 0
            unite = (request.form.get('unite') or 'unité').strip()
            description = (request.form.get('description') or '').strip()
            patrimoine, err_patrimoine = _champs_patrimoine(request.form)

            erreur = None
            if not nom:
                erreur = "Le nom du matériel est obligatoire"
            elif not dept_id:
                erreur = "Le département est obligatoire"
            elif quantite < 0 or seuil < 0:
                erreur = "Les quantités ne peuvent pas être négatives"
            elif err_patrimoine:
                erreur = err_patrimoine

            if erreur:
                flash(erreur, "danger")
            else:
                try:
                    with db_cursor(commit=True) as (conn, cur):
                        cur.execute("""
                            INSERT INTO materiels
                                (nom, categorie, departement_id, quantite, seuil_alerte,
                                 unite, description, marque, modele, fournisseur,
                                 prix_acquisition, date_acquisition, duree_garantie_mois,
                                 prefixe_inventaire)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            RETURNING id
                        """, (nom, categorie, dept_id, quantite, seuil, unite, description or None,
                              patrimoine['marque'], patrimoine['modele'], patrimoine['fournisseur'],
                              patrimoine['prix_acquisition'], patrimoine['date_acquisition'],
                              patrimoine['duree_garantie_mois'], patrimoine['prefixe_inventaire']))
                        new_id = cur.fetchone()['id']
                        # Stock initial = premier mouvement d'entrée, pour que
                        # l'historique reste cohérent avec le stock affiché.
                        if quantite > 0:
                            cur.execute("""
                                INSERT INTO materiels_mouvements
                                    (materiel_id, type_mouvement, quantite, motif, user_id, username)
                                VALUES (%s, 'entree', %s, 'Stock initial', %s, %s)
                            """, (new_id, quantite, session.get('user_id'), session.get('username')))
                        _notifier_stock_bas(cur, new_id)
                    log_action(session.get('user_id'), session.get('username'),
                               "Création matériel", "materiel", new_id, nom)
                    flash(f"Matériel « {nom} » ajouté avec succès", "success")
                    return redirect(url_for('parc.materiels'))
                except Exception as e:
                    logger.error("Erreur ajout matériel: %s", e, exc_info=True)
                    flash(f"Erreur : {e}", "danger")

        with db_cursor() as (conn, cur):
            dept_scope, dept_params = department_scope_sql('d', 'nom', cur)
            cur.execute(f"SELECT d.id, d.nom FROM departements d WHERE {dept_scope} ORDER BY d.nom",
                        dept_params)
            depts = cur.fetchall()
        return render_template('materiel_form.html', materiel=None, departements=depts,
                               categories=MATERIEL_CATEGORIES, title="Nouveau matériel")


    @bp.route('/materiels/edit/<int:id>', methods=['GET', 'POST'])
    @login_required
    @role_required('rh', 'manager')
    def edit_materiel(id):
        with db_cursor() as (conn, cur):
            cur.execute("SELECT * FROM materiels WHERE id = %s", (id,))
            materiel = cur.fetchone()
            dept_scope, dept_params = department_scope_sql('d', 'nom', cur)
            cur.execute(f"SELECT d.id, d.nom FROM departements d WHERE {dept_scope} ORDER BY d.nom",
                        dept_params)
            depts = cur.fetchall()

        if not materiel:
            flash("Matériel introuvable", "danger")
            return redirect(url_for('parc.materiels'))

        if request.method == 'POST':
            nom = (request.form.get('nom') or '').strip()
            categorie = (request.form.get('categorie') or 'fourniture').strip()
            dept_id = request.form.get('departement_id', type=int)
            seuil = request.form.get('seuil_alerte', type=int) or 0
            unite = (request.form.get('unite') or 'unité').strip()
            description = (request.form.get('description') or '').strip()
            patrimoine, err_patrimoine = _champs_patrimoine(request.form)

            if not nom or not dept_id:
                flash("Le nom et le département sont obligatoires", "danger")
            elif err_patrimoine:
                flash(err_patrimoine, "danger")
            else:
                try:
                    with db_cursor(commit=True) as (conn, cur):
                        # La quantité n'est volontairement pas modifiable ici :
                        # elle ne change que via les mouvements (traçabilité).
                        cur.execute("""
                            UPDATE materiels
                            SET nom = %s, categorie = %s, departement_id = %s,
                                seuil_alerte = %s, unite = %s, description = %s,
                                marque = %s, modele = %s, fournisseur = %s,
                                prix_acquisition = %s, date_acquisition = %s,
                                duree_garantie_mois = %s, prefixe_inventaire = %s
                            WHERE id = %s
                        """, (nom, categorie, dept_id, seuil, unite, description or None,
                              patrimoine['marque'], patrimoine['modele'], patrimoine['fournisseur'],
                              patrimoine['prix_acquisition'], patrimoine['date_acquisition'],
                              patrimoine['duree_garantie_mois'], patrimoine['prefixe_inventaire'], id))
                        _notifier_stock_bas(cur, id)
                    log_action(session.get('user_id'), session.get('username'),
                               "Modification matériel", "materiel", id, nom)
                    flash("Matériel mis à jour", "success")
                    return redirect(url_for('parc.materiels'))
                except Exception as e:
                    logger.error("Erreur édition matériel: %s", e, exc_info=True)
                    flash(f"Erreur : {e}", "danger")

        return render_template('materiel_form.html', materiel=materiel, departements=depts,
                               categories=MATERIEL_CATEGORIES, title="Modifier le matériel")


    @bp.route('/materiels/<int:id>')
    @login_required
    def view_materiel(id):
        with db_cursor() as (conn, cur):
            cur.execute("""
                SELECT m.*, COALESCE(d.nom, '—') AS departement_nom
                FROM materiels m
                LEFT JOIN departements d ON d.id = m.departement_id
                WHERE m.id = %s
            """, (id,))
            materiel = cur.fetchone()
            if not materiel:
                flash("Matériel introuvable", "danger")
                return redirect(url_for('parc.materiels'))
            emp_scope, emp_params = department_scope_sql('e', 'departement', cur)

            cur.execute(f"""
                SELECT mv.*, e.nom AS emp_nom, e.prenom AS emp_prenom
                FROM materiels_mouvements mv
                LEFT JOIN employes e ON e.id = mv.employe_id AND {emp_scope}
                WHERE mv.materiel_id = %s
                ORDER BY mv.date_mouvement DESC, mv.id DESC
                LIMIT 50
            """, emp_params + [id])
            mouvements = cur.fetchall()

            cur.execute(f"""
                SELECT a.*, e.nom AS emp_nom, e.prenom AS emp_prenom
                FROM materiels_attributions a
                JOIN employes e ON e.id = a.employe_id
                WHERE a.materiel_id = %s AND {emp_scope}
                ORDER BY a.date_retour NULLS FIRST, a.date_attribution DESC
            """, [id] + emp_params)
            attributions = cur.fetchall()

            # Employés visibles dans la portée courante.
            cur.execute(f"""
                SELECT e.id, e.nom, e.prenom
                FROM employes e
                LEFT JOIN departements d ON d.nom = e.departement
                WHERE {emp_scope}
                ORDER BY (d.id = %s) DESC NULLS LAST, e.nom, e.prenom
            """, emp_params + [materiel['departement_id']])
            employes = cur.fetchall()

            # Exemplaires numérotés (gestion de parc) et leur intervention en cours.
            cur.execute(f"""
                SELECT ex.*, emp.nom AS emp_nom, emp.prenom AS emp_prenom,
                       mt.id AS maintenance_id, mt.statut AS maintenance_statut
                FROM materiel_exemplaires ex
                LEFT JOIN employes emp ON emp.id = ex.employe_id AND
                     {emp_scope.replace('e.', 'emp.')}
                LEFT JOIN LATERAL (
                    SELECT id, statut FROM materiel_maintenances
                    WHERE exemplaire_id = ex.id AND statut IN %s
                    ORDER BY date_creation DESC LIMIT 1
                ) mt ON TRUE
                WHERE ex.materiel_id = %s
                ORDER BY ex.numero_inventaire
            """, emp_params + [MAINTENANCE_OUVERTS, id])
            exemplaires = cur.fetchall()

            cur.execute("""
                SELECT COUNT(*) AS total,
                       COUNT(*) FILTER (WHERE etat IN ('bon','usage'))       AS disponibles,
                       COUNT(*) FILTER (WHERE etat IN ('panne','reparation')) AS indisponibles,
                       COUNT(*) FILTER (WHERE etat = 'rebut')                AS rebuts
                FROM materiel_exemplaires WHERE materiel_id = %s
            """, (id,))
            stats_ex = cur.fetchone()

        return render_template('materiel_detail.html',
                               exemplaires=exemplaires, stats_ex=stats_ex,
                               etats_dict=EXEMPLAIRE_ETATS_DICT,
                               prefixe_propose=_prefixe_materiel(materiel),
                               aujourdhui_date=date.today(),
                               annee_courante=date.today().year,
                               materiel=materiel,
                               mouvements=mouvements,
                               attributions=attributions,
                               employes=employes,
                               categories_dict=MATERIEL_CATEGORIES_DICT,
                               attribuable=materiel['categorie'] in MATERIEL_CAT_ATTRIBUABLES)


    @bp.route('/materiels/<int:id>/mouvement', methods=['POST'])
    @login_required
    @role_required('rh', 'manager')
    def add_mouvement_materiel(id):
        type_mvt = (request.form.get('type_mouvement') or '').strip()
        quantite = request.form.get('quantite', type=int) or 0
        motif = (request.form.get('motif') or '').strip()
        employe_id = request.form.get('employe_id', type=int)

        if type_mvt not in ('entree', 'sortie'):
            flash("Type de mouvement invalide", "danger")
        elif quantite <= 0:
            flash("La quantité doit être supérieure à zéro", "danger")
        else:
            try:
                with db_cursor(commit=True) as (conn, cur):
                    nouveau = _enregistrer_mouvement(cur, id, type_mvt, quantite,
                                                     employe_id or None, motif)
                libelle = "Entrée" if type_mvt == 'entree' else "Sortie"
                log_action(session.get('user_id'), session.get('username'),
                           f"{libelle} stock", "materiel", id, f"{quantite}")
                flash(f"{libelle} de {quantite} enregistrée — nouveau stock : {nouveau}", "success")
            except ValueError as e:
                flash(str(e), "danger")
            except Exception as e:
                logger.error("Erreur mouvement matériel: %s", e, exc_info=True)
                flash(f"Erreur : {e}", "danger")

        return redirect(url_for('parc.view_materiel', id=id))


    @bp.route('/materiels/<int:id>/attribuer', methods=['POST'])
    @login_required
    @role_required('rh', 'manager')
    def attribuer_materiel(id):
        employe_id = request.form.get('employe_id', type=int)
        quantite = request.form.get('quantite', type=int) or 1
        commentaire = (request.form.get('commentaire') or '').strip()

        if not employe_id:
            flash("Veuillez sélectionner un employé", "danger")
        elif quantite <= 0:
            flash("La quantité doit être supérieure à zéro", "danger")
        else:
            try:
                with db_cursor(commit=True) as (conn, cur):
                    cur.execute("SELECT nom, prenom FROM employes WHERE id = %s", (employe_id,))
                    emp = cur.fetchone()
                    if not emp:
                        raise ValueError("Employé introuvable")
                    nom_complet = f"{emp['prenom']} {emp['nom']}"
                    # L'attribution retire physiquement l'article du stock.
                    _enregistrer_mouvement(cur, id, 'sortie', quantite, employe_id,
                                           f"Attribution à {nom_complet}")
                    cur.execute("""
                        INSERT INTO materiels_attributions
                            (materiel_id, employe_id, quantite, commentaire, attribue_par)
                        VALUES (%s, %s, %s, %s, %s) RETURNING id
                    """, (id, employe_id, quantite, commentaire or None,
                          session.get('username')))
                    cur.fetchone()  # consomme le RETURNING id

                    # L'attribution n'est acquise qu'une fois l'employé l'ayant
                    # confirmée : on l'invite à accuser réception.
                    cur.execute("SELECT nom FROM materiels WHERE id = %s", (id,))
                    mrow = cur.fetchone()
                    libelle = mrow['nom'] if mrow else "matériel"
                    uid = _user_id_de_employe(cur, employe_id)
                    if uid:
                        create_notification(
                            uid, "Matériel à réceptionner",
                            "%s (x%s) vous a été attribué. Merci d'accuser réception "
                            "depuis votre espace, ou de contester si l'équipement "
                            "ne vous a pas été remis." % (libelle, quantite), 'info')
                log_action(session.get('user_id'), session.get('username'),
                           "Attribution matériel", "materiel", id, nom_complet)
                flash(f"Matériel attribué à {nom_complet} — en attente de son accusé de réception",
                      "success")
            except ValueError as e:
                flash(str(e), "danger")
            except Exception as e:
                logger.error("Erreur attribution matériel: %s", e, exc_info=True)
                flash(f"Erreur : {e}", "danger")

        return redirect(url_for('parc.view_materiel', id=id))


    @bp.route('/materiels/attribution/<int:attribution_id>/retour', methods=['POST'])
    @login_required
    @role_required('rh', 'manager')
    def retour_materiel(attribution_id):
        materiel_id = request.form.get('materiel_id', type=int)
        try:
            with db_cursor(commit=True) as (conn, cur):
                cur.execute("""
                    SELECT a.*, e.nom AS emp_nom, e.prenom AS emp_prenom
                    FROM materiels_attributions a
                    JOIN employes e ON e.id = a.employe_id
                    WHERE a.id = %s
                """, (attribution_id,))
                attr = cur.fetchone()
                if not attr:
                    raise ValueError("Attribution introuvable")
                if attr['date_retour']:
                    raise ValueError("Ce matériel a déjà été retourné")

                materiel_id = attr['materiel_id']
                # Le retour rouvre l'accusé de réception : l'employé confirme
                # cette fois avoir bien restitué l'équipement.
                cur.execute("""UPDATE materiels_attributions
                                  SET date_retour = CURRENT_DATE, accuse_reception = FALSE,
                                      accuse_le = NULL, accuse_par = NULL, conteste_motif = NULL
                                WHERE id = %s""", (attribution_id,))
                uid = _user_id_de_employe(cur, attr['employe_id'])
                if uid:
                    create_notification(
                        uid, "Restitution à confirmer",
                        "Le retour du matériel a été enregistré. Merci de le confirmer "
                        "depuis votre espace, ou de le contester.", 'info')
                # Le retour réintègre l'article dans le stock.
                _enregistrer_mouvement(
                    cur, materiel_id, 'entree', attr['quantite'], attr['employe_id'],
                    f"Retour de {attr['emp_prenom']} {attr['emp_nom']}")
            flash("Retour enregistré, stock réintégré", "success")
        except ValueError as e:
            flash(str(e), "danger")
        except Exception as e:
            logger.error("Erreur retour matériel: %s", e, exc_info=True)
            flash(f"Erreur : {e}", "danger")

        return redirect(url_for('parc.view_materiel', id=materiel_id) if materiel_id
                        else url_for('parc.materiels'))


    @bp.route('/materiels/attribution/<int:attribution_id>/accuser', methods=['POST'])
    @login_required
    def accuser_attribution(attribution_id):
        """L'employé confirme la remise (ou la restitution) de l'équipement."""
        conteste = request.form.get('action') == 'contester'
        motif = (request.form.get('conteste_motif') or '').strip()
        retour = request.form.get('retour') or url_for('self_service_materiels')

        if conteste and not motif:
            flash("Merci de préciser ce qui ne correspond pas.", "danger")
            return redirect(retour)

        with db_cursor(commit=True) as (conn, cur):
            cur.execute("""
                SELECT a.*, m.nom AS materiel_nom
                  FROM materiels_attributions a
                  JOIN materiels m ON m.id = a.materiel_id
                 WHERE a.id = %s
            """, (attribution_id,))
            attr = cur.fetchone()
            if not attr:
                flash("Attribution introuvable.", "danger")
                return redirect(retour)

            moi = get_current_employee()
            if not (moi and moi['id'] == attr['employe_id']):
                flash("Seul le détenteur du matériel peut accuser réception.", "danger")
                return redirect(retour)
            if attr['accuse_reception']:
                flash("Cette ligne a déjà été confirmée.", "warning")
                return redirect(retour)

            etape = "la restitution" if attr['date_retour'] else "la réception"
            if conteste:
                cur.execute("""UPDATE materiels_attributions
                                  SET conteste_motif = %s WHERE id = %s""",
                            (motif, attribution_id))
                _notifier_roles(cur, ('admin', 'rh', 'manager'),
                                "Attribution contestée : %s" % attr['materiel_nom'],
                                "%s %s conteste %s : %s"
                                % (moi['prenom'], moi['nom'], etape, motif), 'warning')
                log_action(session.get('user_id'), session.get('username'),
                           "Contestation attribution", "materiel",
                           attr['materiel_id'], motif[:120])
                flash("Contestation transmise au gestionnaire du parc.", "info")
            else:
                cur.execute("""UPDATE materiels_attributions
                                  SET accuse_reception = TRUE, accuse_le = CURRENT_DATE,
                                      accuse_par = %s, conteste_motif = NULL
                                WHERE id = %s""",
                            (session.get('username'), attribution_id))
                _notifier_roles(cur, ('admin', 'rh', 'manager'),
                                "Accusé de réception : %s" % attr['materiel_nom'],
                                "%s %s a confirmé %s."
                                % (moi['prenom'], moi['nom'], etape), 'success')
                log_action(session.get('user_id'), session.get('username'),
                           "Accusé de réception", "materiel", attr['materiel_id'], etape)
                flash("Merci, votre confirmation est enregistrée.", "success")

        return redirect(retour)


    @bp.route('/materiels/attribution/<int:attribution_id>/relancer', methods=['POST'])
    @login_required
    @role_required('admin', 'rh', 'manager')
    def relancer_accuse(attribution_id):
        """Relance l'employé qui n'a pas encore accusé réception."""
        with db_cursor(commit=True) as (conn, cur):
            cur.execute("""
                SELECT a.*, m.nom AS materiel_nom
                  FROM materiels_attributions a
                  JOIN materiels m ON m.id = a.materiel_id
                 WHERE a.id = %s
            """, (attribution_id,))
            attr = cur.fetchone()
            if not attr:
                flash("Attribution introuvable.", "danger")
                return redirect(url_for('parc.materiels'))
            uid = _user_id_de_employe(cur, attr['employe_id'])
            if uid:
                create_notification(uid, "Rappel : matériel à confirmer",
                                    "Merci de confirmer la %s de « %s » depuis votre espace."
                                    % ("restitution" if attr['date_retour'] else "réception",
                                       attr['materiel_nom']), 'warning')
                flash("Relance envoyée.", "success")
            else:
                flash("Cet employé n'a pas de compte utilisateur : relance impossible.",
                      "warning")
        return redirect(url_for('parc.view_materiel', id=attr['materiel_id']))


    @bp.route('/materiels/delete/<int:id>', methods=['POST'])
    @login_required
    @role_required('rh')
    def delete_materiel(id):
        try:
            with db_cursor(commit=True) as (conn, cur):
                cur.execute("SELECT nom FROM materiels WHERE id = %s", (id,))
                row = cur.fetchone()
                # ON DELETE CASCADE supprime mouvements et attributions liés.
                cur.execute("DELETE FROM materiels WHERE id = %s", (id,))
            log_action(session.get('user_id'), session.get('username'),
                       "Suppression matériel", "materiel", id, row['nom'] if row else None)
            flash("Matériel supprimé", "success")
        except Exception as e:
            logger.error("Erreur suppression matériel: %s", e, exc_info=True)
            flash(f"Erreur : {e}", "danger")
        return redirect(url_for('parc.materiels'))


    # =============================================================================
    # INVENTAIRE PHYSIQUE
    # =============================================================================
    # Le stock (table `materiels.quantite`) est une valeur *théorique* : elle
    # découle des mouvements saisis. L'inventaire physique confronte cette théorie
    # au terrain (« 10 PC en base, 9 trouvés »), constate les écarts, puis corrige
    # le stock à la clôture par un mouvement d'ajustement traçable.

    def _peut_saisir_inventaire():
        return session.get('role') in ('admin', 'rh', 'manager')


    def _peut_cloturer_inventaire():
        # La clôture modifie le stock : réservée aux profils les plus habilités.
        return session.get('role') in ('admin', 'rh')


    def _inventaire_stats(cur, inventaire_id):
        """Compteurs d'une campagne : comptés, restants, écarts, valeur des écarts."""
        cur.execute("""
            SELECT
                COUNT(*)                                              AS nb_lignes,
                COUNT(quantite_comptee)                               AS nb_comptes,
                COUNT(*) FILTER (WHERE quantite_comptee IS NOT NULL
                                 AND quantite_comptee <> quantite_theorique) AS nb_ecarts,
                COALESCE(SUM(quantite_theorique), 0)                  AS total_theorique,
                COALESCE(SUM(quantite_comptee), 0)                    AS total_compte,
                COALESCE(SUM(quantite_comptee - quantite_theorique)
                         FILTER (WHERE quantite_comptee IS NOT NULL), 0) AS ecart_net
            FROM inventaire_lignes WHERE inventaire_id = %s
        """, (inventaire_id,))
        st = dict(cur.fetchone() or {})
        st['nb_restants'] = (st.get('nb_lignes', 0) or 0) - (st.get('nb_comptes', 0) or 0)
        return st


    @bp.route('/inventaires')
    @login_required
    def inventaires():
        """Liste des campagnes d'inventaire, avec l'avancement de chacune."""
        page = max(1, request.args.get('page', 1, type=int))
        per_page = 10
        statut = (request.args.get('statut') or '').strip()
        dept_id = request.args.get('departement', type=int)

        where, params = [], []
        if statut in ('en_cours', 'cloture', 'annule'):
            where.append("i.statut = %s")
            params.append(statut)
        if dept_id:
            where.append("i.departement_id = %s")
            params.append(dept_id)
        with db_cursor() as (conn, cur):
            dept_scope, dept_params = department_scope_sql('d', 'nom', cur)
            where.append(dept_scope)
            params.extend(dept_params)
            clause = "WHERE " + " AND ".join(where)
            cur.execute(f"""SELECT COUNT(*) AS n FROM inventaires i
                            LEFT JOIN departements d ON d.id = i.departement_id
                            {clause}""", params)
            total = cur.fetchone()['n']
            cur.execute(f"""
                SELECT i.*, d.nom AS departement_nom,
                       COUNT(l.id)                    AS nb_lignes,
                       COUNT(l.quantite_comptee)      AS nb_comptes,
                       COUNT(*) FILTER (WHERE l.quantite_comptee IS NOT NULL
                            AND l.quantite_comptee <> l.quantite_theorique) AS nb_ecarts
                FROM inventaires i
                LEFT JOIN departements d ON d.id = i.departement_id
                LEFT JOIN inventaire_lignes l ON l.inventaire_id = i.id
                {clause}
                GROUP BY i.id, d.nom
                ORDER BY i.date_creation DESC
                LIMIT %s OFFSET %s
            """, params + [per_page, (page - 1) * per_page])
            campagnes = cur.fetchall()

            cur.execute(f"SELECT d.id, d.nom FROM departements d WHERE {dept_scope} ORDER BY d.nom",
                        dept_params)
            departements = cur.fetchall()
            cur.execute(f"""
                SELECT COUNT(*) FILTER (WHERE i.statut = 'en_cours') AS en_cours,
                       COUNT(*) FILTER (WHERE i.statut = 'cloture')  AS clotures,
                       COUNT(*)                                      AS total
                FROM inventaires i LEFT JOIN departements d ON d.id = i.departement_id
                WHERE {dept_scope}
            """, dept_params)
            stats = cur.fetchone()

        pg = pagination_info(total, page, per_page)
        filters = {'statut': statut, 'departement': dept_id}
        return render_template('inventaires.html',
                               campagnes=campagnes, departements=departements,
                               stats=stats, filters=filters,
                               pg=pg, page_items=page_list(pg['page'], pg['pages']),
                               base_qs=urlencode({k: v for k, v in filters.items() if v}),
                               peut_saisir=_peut_saisir_inventaire(),
                               peut_cloturer=_peut_cloturer_inventaire())


    @bp.route('/inventaires/nouveau', methods=['GET', 'POST'])
    @login_required
    @role_required('admin', 'rh', 'manager')
    def add_inventaire():
        """Ouvre une campagne : fige la liste des articles du département et leur
        stock théorique à cet instant."""
        with db_cursor() as (conn, cur):
            dept_scope, dept_params = department_scope_sql('d', 'nom', cur)
            cur.execute(f"SELECT d.id, d.nom FROM departements d WHERE {dept_scope} ORDER BY d.nom",
                        dept_params)
            departements = cur.fetchall()

        if request.method == 'POST':
            dept_id = request.form.get('departement_id', type=int)
            commentaire = (request.form.get('commentaire') or '').strip()

            if not dept_id:
                flash("Veuillez choisir un département", "danger")
                return render_template(
                    'inventaire_form.html', departements=departements,
                    layout='_modal_layout.html' if request.args.get('modal') == '1' else 'base.html')
            try:
                with db_cursor(commit=True) as (conn, cur):
                    # Une seule campagne ouverte par département : sinon deux
                    # comptages concurrents ajusteraient le stock deux fois.
                    cur.execute("""SELECT id FROM inventaires
                                   WHERE departement_id = %s AND statut = 'en_cours'""", (dept_id,))
                    existante = cur.fetchone()
                    if existante:
                        flash("Une campagne est déjà en cours pour ce département.", "warning")
                        return redirect(url_for('parc.view_inventaire', id=existante['id']))

                    cur.execute("SELECT nom FROM departements WHERE id = %s", (dept_id,))
                    drow = cur.fetchone()
                    if not drow:
                        flash("Département introuvable", "danger")
                        return redirect(url_for('parc.inventaires'))

                    cur.execute("""
                        INSERT INTO inventaires (departement_id, statut, commentaire,
                                                 cree_par, cree_par_nom)
                        VALUES (%s, 'en_cours', %s, %s, %s) RETURNING id
                    """, (dept_id, commentaire or None,
                          session.get('user_id'), session.get('username')))
                    inv_id = cur.fetchone()['id']
                    cur.execute("UPDATE inventaires SET reference = %s WHERE id = %s",
                                (f"INV-{datetime.now().strftime('%Y%m%d')}-{inv_id}", inv_id))

                    # Photo du stock : les quantités sont copiées, pas référencées.
                    cur.execute("""
                        INSERT INTO inventaire_lignes (inventaire_id, materiel_id, quantite_theorique)
                        SELECT %s, id, quantite FROM materiels WHERE departement_id = %s
                    """, (inv_id, dept_id))
                    nb = cur.rowcount

                log_action(session.get('user_id'), session.get('username'),
                           "Ouverture inventaire", "inventaire", inv_id, drow['nom'])
                if nb == 0:
                    flash("Campagne ouverte, mais ce département ne contient aucun article.", "warning")
                else:
                    flash(f"Campagne d'inventaire ouverte — {nb} article(s) à compter.", "success")
                return redirect(url_for('parc.view_inventaire', id=inv_id))
            except Exception as e:
                logger.error("Erreur ouverture inventaire: %s", e, exc_info=True)
                flash(f"Erreur : {e}", "danger")

        layout = '_modal_layout.html' if request.args.get('modal') == '1' else 'base.html'
        return render_template('inventaire_form.html', departements=departements, layout=layout)


    @bp.route('/inventaires/<int:id>')
    @login_required
    def view_inventaire(id):
        """Feuille de comptage : théorique vs compté, écart calculé par ligne."""
        with db_cursor() as (conn, cur):
            cur.execute("""
                SELECT i.*, d.nom AS departement_nom
                FROM inventaires i
                LEFT JOIN departements d ON d.id = i.departement_id
                WHERE i.id = %s
            """, (id,))
            inv = cur.fetchone()
            if not inv:
                flash("Inventaire introuvable", "danger")
                return redirect(url_for('parc.inventaires'))

            cur.execute("""
                SELECT l.*, m.nom AS materiel_nom, m.unite, m.categorie,
                       m.quantite AS stock_actuel,
                       (l.quantite_comptee - l.quantite_theorique) AS ecart
                FROM inventaire_lignes l
                JOIN materiels m ON m.id = l.materiel_id
                WHERE l.inventaire_id = %s
                ORDER BY m.nom
            """, (id,))
            lignes = cur.fetchall()
            stats = _inventaire_stats(cur, id)

        return render_template('inventaire_detail.html', inv=inv, lignes=lignes,
                               stats=stats,
                               peut_saisir=_peut_saisir_inventaire(),
                               peut_cloturer=_peut_cloturer_inventaire())


    @bp.route('/inventaires/<int:id>/compter', methods=['POST'])
    @login_required
    @role_required('admin', 'rh', 'manager')
    def compter_inventaire(id):
        """Enregistre le comptage d'une ligne. Ne touche jamais au stock."""
        ligne_id = request.form.get('ligne_id', type=int)
        brut = (request.form.get('quantite_comptee') or '').strip()
        commentaire = (request.form.get('commentaire') or '').strip()

        try:
            with db_cursor(commit=True) as (conn, cur):
                cur.execute("SELECT statut FROM inventaires WHERE id = %s", (id,))
                inv = cur.fetchone()
                if not inv:
                    flash("Inventaire introuvable", "danger")
                    return redirect(url_for('parc.inventaires'))
                if inv['statut'] != 'en_cours':
                    flash("Cette campagne est clôturée : le comptage n'est plus modifiable.", "warning")
                    return redirect(url_for('parc.view_inventaire', id=id))

                if brut == '':
                    # Champ vidé : on repasse la ligne à « non comptée ».
                    cur.execute("""UPDATE inventaire_lignes
                                   SET quantite_comptee = NULL, date_comptage = NULL,
                                       compte_par_nom = NULL, commentaire = %s
                                   WHERE id = %s AND inventaire_id = %s""",
                                (commentaire or None, ligne_id, id))
                    flash("Comptage effacé pour cette ligne.", "info")
                else:
                    try:
                        qte = int(brut)
                    except ValueError:
                        flash("La quantité comptée doit être un nombre entier.", "danger")
                        return redirect(url_for('parc.view_inventaire', id=id))
                    if qte < 0:
                        flash("La quantité comptée ne peut pas être négative.", "danger")
                        return redirect(url_for('parc.view_inventaire', id=id))

                    cur.execute("""UPDATE inventaire_lignes
                                   SET quantite_comptee = %s, commentaire = %s,
                                       date_comptage = CURRENT_TIMESTAMP, compte_par_nom = %s
                                   WHERE id = %s AND inventaire_id = %s""",
                                (qte, commentaire or None, session.get('username'), ligne_id, id))
                    if cur.rowcount == 0:
                        flash("Ligne introuvable dans cette campagne.", "danger")
                    else:
                        flash("Comptage enregistré.", "success")
        except Exception as e:
            logger.error("Erreur comptage inventaire: %s", e, exc_info=True)
            flash(f"Erreur : {e}", "danger")

        return redirect(url_for('parc.view_inventaire', id=id))


    @bp.route('/inventaires/<int:id>/cloturer', methods=['POST'])
    @login_required
    @role_required('admin', 'rh')
    def cloturer_inventaire(id):
        """Clôture : aligne le stock théorique sur le comptage réel.

        Chaque écart produit un mouvement d'ajustement (entrée ou sortie) portant
        l'origine 'inventaire', pour que la correction reste auditable. Les lignes
        non comptées sont ignorées : absence de comptage ≠ stock nul.
        """
        try:
            with db_cursor(commit=True) as (conn, cur):
                cur.execute("SELECT * FROM inventaires WHERE id = %s FOR UPDATE", (id,))
                inv = cur.fetchone()
                if not inv:
                    flash("Inventaire introuvable", "danger")
                    return redirect(url_for('parc.inventaires'))
                if inv['statut'] != 'en_cours':
                    flash("Cette campagne est déjà clôturée.", "warning")
                    return redirect(url_for('parc.view_inventaire', id=id))

                cur.execute("""
                    SELECT l.*, m.nom AS materiel_nom
                    FROM inventaire_lignes l
                    JOIN materiels m ON m.id = l.materiel_id
                    WHERE l.inventaire_id = %s AND l.quantite_comptee IS NOT NULL
                      AND l.quantite_comptee <> l.quantite_theorique
                """, (id,))
                ecarts = cur.fetchall()

                nb_ajustes = 0
                for lg in ecarts:
                    # On se cale sur le stock RÉEL du moment : entre l'ouverture de
                    # la campagne et sa clôture, des mouvements ont pu avoir lieu.
                    cur.execute("SELECT quantite FROM materiels WHERE id = %s FOR UPDATE",
                                (lg['materiel_id'],))
                    row = cur.fetchone()
                    if not row:
                        continue
                    delta = lg['quantite_comptee'] - row['quantite']
                    if delta == 0:
                        continue
                    type_mvt = 'entree' if delta > 0 else 'sortie'
                    motif = (f"Ajustement inventaire {inv['reference'] or id} : "
                             f"théorique {lg['quantite_theorique']}, compté {lg['quantite_comptee']}")
                    if lg['commentaire']:
                        motif += f" — {lg['commentaire']}"
                    _enregistrer_mouvement(cur, lg['materiel_id'], type_mvt, abs(delta),
                                           None, motif, origine='inventaire')
                    nb_ajustes += 1

                cur.execute("""UPDATE inventaires
                               SET statut = 'cloture', date_cloture = CURRENT_TIMESTAMP,
                                   cloture_par = %s, cloture_par_nom = %s
                               WHERE id = %s""",
                            (session.get('user_id'), session.get('username'), id))

            log_action(session.get('user_id'), session.get('username'),
                       "Clôture inventaire", "inventaire", id,
                       f"{nb_ajustes} ajustement(s)")
            if nb_ajustes:
                flash(f"Inventaire clôturé — {nb_ajustes} article(s) ajusté(s) dans le stock.", "success")
            else:
                flash("Inventaire clôturé — aucun écart à corriger.", "success")
        except Exception as e:
            logger.error("Erreur clôture inventaire: %s", e, exc_info=True)
            flash(f"Erreur : {e}", "danger")

        return redirect(url_for('parc.view_inventaire', id=id))


    @bp.route('/inventaires/<int:id>/annuler', methods=['POST'])
    @login_required
    @role_required('admin', 'rh')
    def annuler_inventaire(id):
        """Abandonne une campagne sans toucher au stock."""
        try:
            with db_cursor(commit=True) as (conn, cur):
                cur.execute("SELECT statut FROM inventaires WHERE id = %s", (id,))
                inv = cur.fetchone()
                if not inv:
                    flash("Inventaire introuvable", "danger")
                    return redirect(url_for('parc.inventaires'))
                if inv['statut'] != 'en_cours':
                    flash("Seule une campagne en cours peut être annulée.", "warning")
                    return redirect(url_for('parc.view_inventaire', id=id))
                cur.execute("""UPDATE inventaires
                               SET statut = 'annule', date_cloture = CURRENT_TIMESTAMP,
                                   cloture_par = %s, cloture_par_nom = %s
                               WHERE id = %s""",
                            (session.get('user_id'), session.get('username'), id))
            log_action(session.get('user_id'), session.get('username'),
                       "Annulation inventaire", "inventaire", id, None)
            flash("Campagne annulée — le stock n'a pas été modifié.", "info")
        except Exception as e:
            logger.error("Erreur annulation inventaire: %s", e, exc_info=True)
            flash(f"Erreur : {e}", "danger")
        return redirect(url_for('parc.view_inventaire', id=id))


    # =============================================================================
    # GESTION DE PARC : exemplaires, maintenance, étiquettes QR
    # =============================================================================

    def _exemplaire_complet(cur, exemplaire_id):
        """Exemplaire enrichi sans révéler un détenteur hors département."""
        emp_scope, emp_params = department_scope_sql('emp', 'departement', cur)
        cur.execute(f"""
            SELECT e.*, m.nom AS materiel_nom, m.categorie, m.marque, m.modele,
                   m.id AS materiel_id, d.nom AS departement_nom,
                   emp.nom AS emp_nom, emp.prenom AS emp_prenom
            FROM materiel_exemplaires e
            JOIN materiels m ON m.id = e.materiel_id
            LEFT JOIN departements d ON d.id = m.departement_id
            LEFT JOIN employes emp ON emp.id = e.employe_id AND {emp_scope}
            WHERE e.id = %s
        """, emp_params + [exemplaire_id])
        return cur.fetchone()


    @bp.route('/materiels/<int:id>/exemplaires/add', methods=['POST'])
    @login_required
    @role_required('rh', 'manager')
    def add_exemplaire(id):
        """Crée un ou plusieurs exemplaires numérotés pour un article."""
        nombre = request.form.get('nombre', type=int) or 1
        numero_serie = (request.form.get('numero_serie') or '').strip()
        emplacement = (request.form.get('emplacement') or '').strip()
        numero_manuel = (request.form.get('numero_inventaire') or '').strip().upper()

        if nombre < 1 or nombre > 100:
            flash("Le nombre d'exemplaires doit être compris entre 1 et 100.", "danger")
            return redirect(url_for('parc.view_materiel', id=id))
        if numero_manuel and nombre > 1:
            flash("Un numéro d'inventaire imposé ne peut concerner qu'un seul exemplaire.", "danger")
            return redirect(url_for('parc.view_materiel', id=id))

        try:
            with db_cursor(commit=True) as (conn, cur):
                cur.execute("SELECT * FROM materiels WHERE id = %s", (id,))
                mat = cur.fetchone()
                if not mat:
                    flash("Matériel introuvable", "danger")
                    return redirect(url_for('parc.materiels'))

                garantie = _garantie_fin(mat['date_acquisition'], mat['duree_garantie_mois'])
                crees = []
                for _ in range(nombre):
                    numero = numero_manuel or _generer_numero_inventaire(cur, mat)
                    cur.execute("""
                        INSERT INTO materiel_exemplaires
                            (materiel_id, numero_inventaire, numero_serie, etat,
                             date_acquisition, prix_acquisition, fournisseur,
                             garantie_fin, emplacement)
                        VALUES (%s, %s, %s, 'bon', %s, %s, %s, %s, %s)
                        RETURNING numero_inventaire
                    """, (id, numero, numero_serie or None, mat['date_acquisition'],
                          mat['prix_acquisition'], mat['fournisseur'], garantie,
                          emplacement or None))
                    crees.append(cur.fetchone()['numero_inventaire'])

                # L'article devient « suivi à l'unité » dès son premier exemplaire.
                cur.execute("UPDATE materiels SET suivi_unitaire = TRUE WHERE id = %s", (id,))

            log_action(session.get('user_id'), session.get('username'),
                       "Création exemplaires", "materiel", id, ", ".join(crees))
            flash(f"{len(crees)} exemplaire(s) créé(s) : {', '.join(crees)}", "success")
        except psycopg2.errors.UniqueViolation:
            flash("Ce numéro d'inventaire existe déjà.", "danger")
        except Exception as e:
            logger.error("Erreur création exemplaire: %s", e, exc_info=True)
            flash(f"Erreur : {e}", "danger")
        return redirect(url_for('parc.view_materiel', id=id))


    @bp.route('/exemplaires/<int:id>')
    @login_required
    def view_exemplaire(id):
        """Fiche d'un exemplaire : identité, garantie, QR, historique des pannes.

        C'est la page ouverte en scannant l'étiquette QR collée sur le matériel.
        """
        with db_cursor() as (conn, cur):
            ex = _exemplaire_complet(cur, id)
            if not ex:
                flash("Exemplaire introuvable", "danger")
                return redirect(url_for('parc.materiels'))

            cur.execute("""
                SELECT * FROM materiel_maintenances
                WHERE exemplaire_id = %s
                ORDER BY date_creation DESC
            """, (id,))
            maintenances = cur.fetchall()

            cur.execute("""
                SELECT COALESCE(SUM(cout), 0) AS total_repare,
                       COUNT(*) FILTER (WHERE statut IN ('repare','irreparable')) AS nb_closes
                FROM materiel_maintenances WHERE exemplaire_id = %s
            """, (id,))
            stats = cur.fetchone()

            emp_scope, emp_params = department_scope_sql('e', 'departement', cur)
            cur.execute(f"""SELECT id, nom, prenom FROM employes e
                            WHERE {emp_scope} ORDER BY nom, prenom""", emp_params)
            employes = cur.fetchall()

            # Cibles d'assignation limitées au même département.
            cur.execute(f"""
                SELECT u.id, u.username, u.role,
                       TRIM(COALESCE(e.prenom,'') || ' ' || COALESCE(e.nom,'')) AS nom_complet
                  FROM users u
                  LEFT JOIN employes e ON e.id = u.employe_id
                 WHERE {emp_scope}
                 ORDER BY CASE u.role WHEN 'technicien' THEN 0 ELSE 1 END, u.username
            """, emp_params)
            assignables = cur.fetchall()
            if get_department_scope(cur)['is_global']:
                cur.execute("""SELECT id, nom, specialite FROM prestataires
                               WHERE actif ORDER BY nom""")
                prestataires = cur.fetchall()
            else:
                prestataires = []

            # Droits sur l'intervention en cours, s'il y en a une.
            ouverte = next((m for m in maintenances if m['statut'] in MAINTENANCE_OUVERTS), None)
            droits = _acteurs_intervention(ouverte) if ouverte else {}

        url_fiche = url_for('parc.view_exemplaire', id=id, _external=True)
        return render_template('exemplaire_detail.html', ex=ex,
                               maintenances=maintenances, stats=stats,
                               employes=employes,
                               assignables=assignables, prestataires=prestataires,
                               droits=droits,
                               etats=EXEMPLAIRE_ETATS,
                               etats_dict=EXEMPLAIRE_ETATS_DICT,
                               statuts_dict=MAINTENANCE_STATUTS_DICT,
                               maintenance_ouverts=MAINTENANCE_OUVERTS,
                               maintenance_indisponible=MAINTENANCE_INDISPONIBLE,
                               validation_jours=MAINTENANCE_VALIDATION_JOURS,
                               qr_svg=_qr_svg(url_fiche), url_fiche=url_fiche,
                               aujourdhui=date.today())


    @bp.route('/exemplaires/<int:id>/modifier', methods=['POST'])
    @login_required
    @role_required('rh', 'manager')
    def edit_exemplaire(id):
        """Met à jour l'identité d'un exemplaire (série, état, détenteur...)."""
        numero_serie = (request.form.get('numero_serie') or '').strip()
        emplacement = (request.form.get('emplacement') or '').strip()
        commentaire = (request.form.get('commentaire') or '').strip()
        etat = (request.form.get('etat') or '').strip()
        employe_id = request.form.get('employe_id', type=int)
        garantie_fin = (request.form.get('garantie_fin') or '').strip()
        prix = (request.form.get('prix_acquisition') or '').strip()

        if etat and etat not in EXEMPLAIRE_ETATS_DICT:
            flash("État invalide.", "danger")
            return redirect(url_for('parc.view_exemplaire', id=id))

        try:
            with db_cursor(commit=True) as (conn, cur):
                # Une intervention ouverte pilote l'état : on ne le force pas à la main.
                cur.execute("""SELECT COUNT(*) AS n FROM materiel_maintenances
                               WHERE exemplaire_id = %s AND statut IN %s""",
                            (id, MAINTENANCE_OUVERTS))
                bloque = cur.fetchone()['n'] > 0

                cur.execute("""
                    UPDATE materiel_exemplaires
                    SET numero_serie = %s, emplacement = %s, commentaire = %s,
                        employe_id = %s, garantie_fin = %s, prix_acquisition = %s,
                        etat = CASE WHEN %s THEN etat ELSE COALESCE(NULLIF(%s,''), etat) END
                    WHERE id = %s
                """, (numero_serie or None, emplacement or None, commentaire or None,
                      employe_id or None, garantie_fin or None,
                      prix.replace(' ', '').replace(',', '.') or None,
                      bloque, etat, id))
            if bloque and etat:
                flash("Exemplaire mis à jour. L'état reste piloté par l'intervention en cours.", "info")
            else:
                flash("Exemplaire mis à jour.", "success")
            log_action(session.get('user_id'), session.get('username'),
                       "Modification exemplaire", "exemplaire", id, None)
        except Exception as e:
            logger.error("Erreur modification exemplaire: %s", e, exc_info=True)
            flash(f"Erreur : {e}", "danger")
        return redirect(url_for('parc.view_exemplaire', id=id))


    @bp.route('/exemplaires/<int:id>/supprimer', methods=['POST'])
    @login_required
    @role_required('rh')
    def delete_exemplaire(id):
        try:
            with db_cursor(commit=True) as (conn, cur):
                cur.execute("""SELECT e.numero_inventaire, e.materiel_id
                               FROM materiel_exemplaires e WHERE e.id = %s""", (id,))
                row = cur.fetchone()
                if not row:
                    flash("Exemplaire introuvable", "danger")
                    return redirect(url_for('parc.materiels'))
                cur.execute("DELETE FROM materiel_exemplaires WHERE id = %s", (id,))
            log_action(session.get('user_id'), session.get('username'),
                       "Suppression exemplaire", "exemplaire", id, row['numero_inventaire'])
            flash(f"Exemplaire {row['numero_inventaire']} supprimé.", "success")
            return redirect(url_for('parc.view_materiel', id=row['materiel_id']))
        except Exception as e:
            logger.error("Erreur suppression exemplaire: %s", e, exc_info=True)
            flash(f"Erreur : {e}", "danger")
            return redirect(url_for('parc.view_exemplaire', id=id))


    # --- Circuit de maintenance -------------------------------------------------

    @bp.route('/exemplaires/<int:id>/panne', methods=['POST'])
    @login_required
    def signaler_panne(id):
        """Signale une panne. Ouvert à tous : celui qui constate n'est pas
        forcément gestionnaire du parc."""
        panne = (request.form.get('panne') or '').strip()
        if not panne:
            flash("Veuillez décrire la panne.", "danger")
            return redirect(url_for('parc.view_exemplaire', id=id))
        try:
            with db_cursor(commit=True) as (conn, cur):
                cur.execute("SELECT etat FROM materiel_exemplaires WHERE id = %s", (id,))
                ex = cur.fetchone()
                if not ex:
                    flash("Exemplaire introuvable", "danger")
                    return redirect(url_for('parc.materiels'))
                if ex['etat'] == 'rebut':
                    flash("Cet exemplaire est au rebut : aucune intervention possible.", "warning")
                    return redirect(url_for('parc.view_exemplaire', id=id))
                cur.execute("""SELECT COUNT(*) AS n FROM materiel_maintenances
                               WHERE exemplaire_id = %s AND statut IN %s""",
                            (id, MAINTENANCE_OUVERTS))
                if cur.fetchone()['n'] > 0:
                    flash("Une intervention est déjà en cours pour cet exemplaire.", "warning")
                    return redirect(url_for('parc.view_exemplaire', id=id))

                cur.execute("""
                    INSERT INTO materiel_maintenances
                        (exemplaire_id, statut, panne, signale_par, signale_par_id)
                    VALUES (%s, 'signale', %s, %s, %s) RETURNING id
                """, (id, panne, session.get('username'), session.get('user_id')))
                cur.fetchone()  # consomme le RETURNING id
                cur.execute("UPDATE materiel_exemplaires SET etat = 'panne' WHERE id = %s", (id,))

                # Étape 1 du workflow : prévenir les gestionnaires qu'il y a une
                # intervention à assigner.
                cur.execute("""SELECT ex.numero_inventaire, m.nom,
                                      d.nom AS departement_nom
                               FROM materiel_exemplaires ex
                               JOIN materiels m ON m.id = ex.materiel_id
                               LEFT JOIN departements d ON d.id=m.departement_id
                               WHERE ex.id = %s""", (id,))
                info = cur.fetchone()
                _notifier_pilotes(
                    cur, "Panne signalée : %s" % info['numero_inventaire'],
                    "%s — %s (signalé par %s). À assigner."
                    % (info['nom'], panne[:120], session.get('username')),
                    'warning', sauf=session.get('user_id'),
                    departement=info.get('departement_nom'))

            log_action(session.get('user_id'), session.get('username'),
                       "Signalement panne", "exemplaire", id, panne[:120])
            flash("Panne signalée. Un gestionnaire va assigner l'intervention.", "success")
        except Exception as e:
            logger.error("Erreur signalement panne: %s", e, exc_info=True)
            flash(f"Erreur : {e}", "danger")
        return redirect(url_for('parc.view_exemplaire', id=id))


    @bp.route('/maintenances/<int:id>/assigner', methods=['POST'])
    @login_required
    @role_required('rh', 'manager')
    def assigner_maintenance(id):
        """Étape 2 : le gestionnaire confie l'intervention à un exécutant.

        Deux cas : un utilisateur interne (qui traitera lui-même dans
        l'application) ou un prestataire externe (le retour sera retranscrit par
        un gestionnaire).
        """
        cible = (request.form.get('cible') or '').strip()   # 'interne' | 'externe'
        user_id = request.form.get('assigne_user_id', type=int)
        prestataire_id = request.form.get('prestataire_id', type=int)
        if cible == 'externe' and session.get('role') not in GLOBAL_DATA_ROLES:
            return _department_access_denied()

        try:
            with db_cursor(commit=True) as (conn, cur):
                cur.execute("SELECT * FROM materiel_maintenances WHERE id = %s", (id,))
                mt = cur.fetchone()
                if not mt:
                    flash("Intervention introuvable", "danger")
                    return redirect(url_for('parc.maintenances'))
                if mt['statut'] not in ('signale', 'assigne'):
                    flash("Cette intervention ne peut plus être assignée.", "warning")
                    return redirect(url_for('parc.view_exemplaire', id=mt['exemplaire_id']))

                technicien = None
                if cible == 'interne':
                    if not user_id:
                        flash("Veuillez choisir la personne à qui assigner l'intervention.", "danger")
                        return redirect(url_for('parc.view_exemplaire', id=mt['exemplaire_id']))
                    cur.execute("SELECT id, username FROM users WHERE id = %s", (user_id,))
                    u = cur.fetchone()
                    if not u:
                        flash("Utilisateur introuvable.", "danger")
                        return redirect(url_for('parc.view_exemplaire', id=mt['exemplaire_id']))
                    technicien, prestataire_id = u['username'], None
                elif cible == 'externe':
                    if not prestataire_id:
                        flash("Veuillez choisir un prestataire.", "danger")
                        return redirect(url_for('parc.view_exemplaire', id=mt['exemplaire_id']))
                    cur.execute("SELECT id, nom FROM prestataires WHERE id = %s", (prestataire_id,))
                    p = cur.fetchone()
                    if not p:
                        flash("Prestataire introuvable.", "danger")
                        return redirect(url_for('parc.view_exemplaire', id=mt['exemplaire_id']))
                    technicien, user_id = p['nom'], None
                else:
                    flash("Veuillez préciser à qui assigner l'intervention.", "danger")
                    return redirect(url_for('parc.view_exemplaire', id=mt['exemplaire_id']))

                cur.execute("""UPDATE materiel_maintenances
                               SET statut = 'assigne', technicien = %s,
                                   assigne_user_id = %s, prestataire_id = %s,
                                   date_assignation = CURRENT_DATE, assigne_par = %s
                               WHERE id = %s""",
                            (technicien, user_id, prestataire_id,
                             session.get('username'), id))

                cur.execute("""SELECT ex.numero_inventaire, m.nom,
                                      d.nom AS departement_nom
                               FROM materiel_exemplaires ex
                               JOIN materiels m ON m.id = ex.materiel_id
                               LEFT JOIN departements d ON d.id=m.departement_id
                               WHERE ex.id = %s""", (mt['exemplaire_id'],))
                info = cur.fetchone()
                if user_id:
                    create_notification(
                        user_id, "Intervention assignée : %s" % info['numero_inventaire'],
                        "%s — %s. Vous êtes chargé de cette réparation."
                        % (info['nom'], (mt['panne'] or '')[:120]), 'info')
                if mt.get('signale_par_id'):
                    create_notification(
                        mt['signale_par_id'],
                        "Panne prise en charge : %s" % info['numero_inventaire'],
                        "Votre signalement a été assigné à %s." % technicien, 'info')

            log_action(session.get('user_id'), session.get('username'),
                       "Assignation maintenance", "maintenance", id, technicien)
            flash("Intervention assignée à %s." % technicien, "success")
            return redirect(url_for('parc.view_exemplaire', id=mt['exemplaire_id']))
        except Exception as e:
            logger.error("Erreur assignation maintenance: %s", e, exc_info=True)
            flash(f"Erreur : {e}", "danger")
            return redirect(url_for('parc.maintenances'))


    @bp.route('/maintenances/<int:id>/envoyer', methods=['POST'])
    @login_required
    def envoyer_maintenance(id):
        """Étape 3a : l'exécutant démarre la réparation (départ à l'atelier).

        Accessible au technicien assigné lui-même, ou à un gestionnaire lorsque le
        matériel part chez un prestataire externe.
        """
        date_envoi = (request.form.get('date_envoi') or '').strip()
        try:
            with db_cursor(commit=True) as (conn, cur):
                cur.execute("SELECT * FROM materiel_maintenances WHERE id = %s", (id,))
                mt = cur.fetchone()
                if not mt:
                    flash("Intervention introuvable", "danger")
                    return redirect(url_for('parc.maintenances'))
                droits = _acteurs_intervention(mt)
                if mt['statut'] == 'signale':
                    flash("Cette intervention doit d'abord être assignée.", "warning")
                    return redirect(url_for('parc.view_exemplaire', id=mt['exemplaire_id']))
                if not droits['peut_demarrer']:
                    flash("Vous n'êtes pas en charge de cette intervention.", "danger")
                    return redirect(url_for('parc.view_exemplaire', id=mt['exemplaire_id']))

                cur.execute("""UPDATE materiel_maintenances
                               SET statut = 'envoye',
                                   date_envoi = COALESCE(NULLIF(%s,'')::date, CURRENT_DATE)
                               WHERE id = %s""", (date_envoi, id))
                cur.execute("""UPDATE materiel_exemplaires SET etat = 'reparation'
                               WHERE id = %s AND etat <> 'rebut'""", (mt['exemplaire_id'],))

                if mt.get('signale_par_id'):
                    cur.execute("""SELECT ex.numero_inventaire FROM materiel_exemplaires ex
                                   WHERE ex.id = %s""", (mt['exemplaire_id'],))
                    num = cur.fetchone()['numero_inventaire']
                    create_notification(
                        mt['signale_par_id'], "Réparation en cours : %s" % num,
                        "Le matériel est parti en réparation chez %s."
                        % (mt['technicien'] or 'le technicien'), 'info')

            log_action(session.get('user_id'), session.get('username'),
                       "Départ en réparation", "maintenance", id, mt['technicien'])
            flash("Matériel parti en réparation.", "success")
            return redirect(url_for('parc.view_exemplaire', id=mt['exemplaire_id']))
        except Exception as e:
            logger.error("Erreur envoi maintenance: %s", e, exc_info=True)
            flash(f"Erreur : {e}", "danger")
            return redirect(url_for('parc.maintenances'))


    @bp.route('/maintenances/<int:id>/cloturer', methods=['POST'])
    @login_required
    def cloturer_maintenance(id):
        """Étape 3b : l'exécutant rend son retour d'atelier.

        Si le matériel est réparé, l'intervention n'est PAS close : elle passe en
        « retour à valider » et c'est le demandeur qui confirmera que la panne est
        réellement résolue. Le cas irréparable, lui, est terminal (rien à valider :
        le matériel part au rebut).
        """
        resultat = (request.form.get('resultat') or '').strip()
        cout = (request.form.get('cout') or '').strip().replace(' ', '').replace(',', '.')
        diagnostic = (request.form.get('diagnostic') or '').strip()
        date_retour = (request.form.get('date_retour') or '').strip()
        etat_retour = (request.form.get('etat_retour') or 'bon').strip()

        if resultat not in ('repare', 'irreparable'):
            flash("Résultat invalide.", "danger")
            return redirect(url_for('parc.maintenances'))
        if cout:
            try:
                if float(cout) < 0:
                    raise ValueError
            except ValueError:
                flash("Le coût doit être un nombre positif.", "danger")
                return redirect(url_for('parc.maintenances'))

        try:
            with db_cursor(commit=True) as (conn, cur):
                cur.execute("SELECT * FROM materiel_maintenances WHERE id = %s", (id,))
                mt = cur.fetchone()
                if not mt:
                    flash("Intervention introuvable", "danger")
                    return redirect(url_for('parc.maintenances'))
                droits = _acteurs_intervention(mt)
                if mt['statut'] not in MAINTENANCE_OUVERTS:
                    flash("Cette intervention est déjà close.", "warning")
                    return redirect(url_for('parc.view_exemplaire', id=mt['exemplaire_id']))
                if not droits['peut_executer']:
                    flash("Vous n'êtes pas en charge de cette intervention.", "danger")
                    return redirect(url_for('parc.view_exemplaire', id=mt['exemplaire_id']))

                # Réparé → validation par le demandeur ; irréparable → terminal.
                nouveau_statut = 'a_valider' if resultat == 'repare' else 'irreparable'
                cur.execute("""UPDATE materiel_maintenances
                               SET statut = %s, cout = %s, diagnostic = %s,
                                   date_retour = COALESCE(NULLIF(%s,'')::date, CURRENT_DATE),
                                   date_execution = CURRENT_DATE, execute_par = %s
                               WHERE id = %s""",
                            (nouveau_statut, cout or None, diagnostic or None,
                             date_retour, session.get('username'), id))

                if resultat == 'repare':
                    etat = etat_retour if etat_retour in ('bon', 'usage') else 'bon'
                else:
                    etat = 'rebut'
                cur.execute("UPDATE materiel_exemplaires SET etat = %s WHERE id = %s",
                            (etat, mt['exemplaire_id']))

                cur.execute("""SELECT ex.numero_inventaire, m.nom,
                                      d.nom AS departement_nom
                               FROM materiel_exemplaires ex
                               JOIN materiels m ON m.id = ex.materiel_id
                               LEFT JOIN departements d ON d.id=m.departement_id
                               WHERE ex.id = %s""", (mt['exemplaire_id'],))
                info = cur.fetchone()
                if resultat == 'repare' and mt.get('signale_par_id'):
                    create_notification(
                        mt['signale_par_id'],
                        "Matériel réparé, à valider : %s" % info['numero_inventaire'],
                        "%s est revenu de réparation. Merci de confirmer que la panne "
                        "est bien résolue (sans réponse sous %d jours, la validation "
                        "sera automatique)." % (info['nom'], MAINTENANCE_VALIDATION_JOURS),
                        'success')
                elif resultat == 'irreparable':
                    if mt.get('signale_par_id'):
                        create_notification(
                            mt['signale_par_id'],
                            "Matériel irréparable : %s" % info['numero_inventaire'],
                            "%s a été déclaré irréparable et mis au rebut." % info['nom'],
                            'danger')
                    _notifier_pilotes(
                        cur, "Mise au rebut : %s" % info['numero_inventaire'],
                        "%s déclaré irréparable par %s." % (info['nom'], session.get('username')),
                        'danger', sauf=session.get('user_id'),
                        departement=info.get('departement_nom'))

            log_action(session.get('user_id'), session.get('username'),
                       "Retour d'intervention", "maintenance", id,
                       f"{resultat}, coût {cout or 0}")
            flash("Retour enregistré. En attente de validation par le demandeur."
                  if resultat == 'repare'
                  else "Matériel déclaré irréparable et mis au rebut.", "success")
            return redirect(url_for('parc.view_exemplaire', id=mt['exemplaire_id']))
        except Exception as e:
            logger.error("Erreur clôture maintenance: %s", e, exc_info=True)
            flash(f"Erreur : {e}", "danger")
            return redirect(url_for('parc.maintenances'))


    @bp.route('/maintenances/<int:id>/valider', methods=['POST'])
    @login_required
    def valider_maintenance(id):
        """Étape 4 : le demandeur confirme — ou non — que la panne est résolue.

        - Validation → l'intervention est close, le matériel reste disponible.
        - Refus → l'intervention repart en réparation chez le même exécutant,
          avec le motif du refus : c'est tout l'intérêt de l'étape.
        Un gestionnaire peut forcer la validation (demandeur absent ou parti).
        """
        decision = (request.form.get('decision') or '').strip()
        motif = (request.form.get('motif_refus') or '').strip()
        if decision not in ('valider', 'refuser'):
            flash("Décision invalide.", "danger")
            return redirect(url_for('parc.maintenances'))

        try:
            with db_cursor(commit=True) as (conn, cur):
                cur.execute("SELECT * FROM materiel_maintenances WHERE id = %s", (id,))
                mt = cur.fetchone()
                if not mt:
                    flash("Intervention introuvable", "danger")
                    return redirect(url_for('parc.maintenances'))
                if mt['statut'] != 'a_valider':
                    flash("Cette intervention n'est pas en attente de validation.", "warning")
                    return redirect(url_for('parc.view_exemplaire', id=mt['exemplaire_id']))

                droits = _acteurs_intervention(mt)
                forcee = False
                if not droits['peut_valider']:
                    if droits['peut_forcer']:
                        forcee = True   # clôture administrative
                    else:
                        flash("Seul le demandeur peut valider ce retour.", "danger")
                        return redirect(url_for('parc.view_exemplaire', id=mt['exemplaire_id']))

                cur.execute("""SELECT ex.numero_inventaire, m.nom,
                                      d.nom AS departement_nom
                               FROM materiel_exemplaires ex
                               JOIN materiels m ON m.id = ex.materiel_id
                               LEFT JOIN departements d ON d.id=m.departement_id
                               WHERE ex.id = %s""", (mt['exemplaire_id'],))
                info = cur.fetchone()

                if decision == 'valider':
                    cur.execute("""UPDATE materiel_maintenances
                                   SET statut = 'repare', valide_par = %s,
                                       date_validation = CURRENT_DATE,
                                       validation_forcee = %s, cloture_par = %s
                                   WHERE id = %s""",
                                (session.get('username'), forcee,
                                 session.get('username'), id))
                    _notifier_pilotes(
                        cur, "Intervention clôturée : %s" % info['numero_inventaire'],
                        "%s — retour validé par %s%s."
                        % (info['nom'], session.get('username'),
                           " (clôture forcée)" if forcee else ""),
                        'success', sauf=session.get('user_id'),
                        departement=info.get('departement_nom'))
                    log_action(session.get('user_id'), session.get('username'),
                               "Validation retour", "maintenance", id,
                               "forcée" if forcee else None)
                    flash("Retour validé : l'intervention est close.", "success")
                else:
                    if not motif:
                        flash("Merci d'indiquer pourquoi le retour n'est pas satisfaisant.", "danger")
                        return redirect(url_for('parc.view_exemplaire', id=mt['exemplaire_id']))
                    # Retour en réparation chez le même exécutant.
                    cur.execute("""UPDATE materiel_maintenances
                                   SET statut = 'envoye', motif_refus = %s,
                                       date_retour = NULL, date_execution = NULL
                                   WHERE id = %s""", (motif, id))
                    cur.execute("""UPDATE materiel_exemplaires SET etat = 'reparation'
                                   WHERE id = %s AND etat <> 'rebut'""", (mt['exemplaire_id'],))
                    if mt.get('assigne_user_id'):
                        create_notification(
                            mt['assigne_user_id'],
                            "Retour refusé : %s" % info['numero_inventaire'],
                            "%s — le demandeur signale que la panne persiste : %s"
                            % (info['nom'], motif[:150]), 'danger')
                    _notifier_pilotes(
                        cur, "Retour refusé : %s" % info['numero_inventaire'],
                        "%s — panne non résolue selon %s : %s"
                        % (info['nom'], session.get('username'), motif[:150]),
                        'danger', sauf=session.get('user_id'),
                        departement=info.get('departement_nom'))
                    log_action(session.get('user_id'), session.get('username'),
                               "Refus de retour", "maintenance", id, motif[:120])
                    flash("Retour refusé : l'intervention repart en réparation.", "warning")

            return redirect(url_for('parc.view_exemplaire', id=mt['exemplaire_id']))
        except Exception as e:
            logger.error("Erreur validation maintenance: %s", e, exc_info=True)
            flash(f"Erreur : {e}", "danger")
            return redirect(url_for('parc.maintenances'))


    @bp.route('/maintenances/<int:id>/annuler', methods=['POST'])
    @login_required
    @role_required('rh', 'manager')
    def annuler_maintenance(id):
        """Fausse alerte : on referme sans réparation."""
        try:
            with db_cursor(commit=True) as (conn, cur):
                cur.execute("SELECT * FROM materiel_maintenances WHERE id = %s", (id,))
                mt = cur.fetchone()
                if not mt:
                    flash("Intervention introuvable", "danger")
                    return redirect(url_for('parc.maintenances'))
                if mt['statut'] not in MAINTENANCE_OUVERTS:
                    flash("Cette intervention est déjà close.", "warning")
                    return redirect(url_for('parc.view_exemplaire', id=mt['exemplaire_id']))
                cur.execute("""UPDATE materiel_maintenances
                               SET statut = 'annule', cloture_par = %s,
                                   date_retour = CURRENT_DATE
                               WHERE id = %s""", (session.get('username'), id))
                cur.execute("""UPDATE materiel_exemplaires SET etat = 'bon'
                               WHERE id = %s AND etat <> 'rebut'""", (mt['exemplaire_id'],))
            log_action(session.get('user_id'), session.get('username'),
                       "Annulation maintenance", "maintenance", id, None)
            flash("Intervention annulée : le matériel redevient disponible.", "info")
            return redirect(url_for('parc.view_exemplaire', id=mt['exemplaire_id']))
        except Exception as e:
            logger.error("Erreur annulation maintenance: %s", e, exc_info=True)
            flash(f"Erreur : {e}", "danger")
            return redirect(url_for('parc.maintenances'))


    @bp.route('/maintenances')
    @login_required
    def maintenances():
        """Tableau de bord des réparations, avec filtres et coûts."""
        page = max(1, request.args.get('page', 1, type=int))
        per_page = 12
        statut = (request.args.get('statut') or '').strip()
        dept_id = request.args.get('departement', type=int)
        portee = (request.args.get('portee') or '').strip()

        where, params = [], []
        if statut in MAINTENANCE_STATUTS_DICT:
            where.append("mt.statut = %s")
            params.append(statut)
        elif statut == 'ouvert':
            where.append("mt.statut IN %s")
            params.append(MAINTENANCE_OUVERTS)
        if dept_id:
            where.append("m.departement_id = %s")
            params.append(dept_id)
        # « Mes interventions » : ce que je dois traiter (assigné) ou valider
        # (demandeur). C'est la file de travail du technicien.
        if portee == 'moi':
            where.append("(mt.assigne_user_id = %s OR mt.signale_par_id = %s)")
            params.extend([session.get('user_id'), session.get('user_id')])
        with db_cursor() as (conn, cur):
            dept_scope, dept_params = department_scope_sql('d', 'nom', cur)
            where.append(dept_scope)
            params.extend(dept_params)
            clause = "WHERE " + " AND ".join(where)
            cur.execute(f"""
                SELECT COUNT(*) AS n FROM materiel_maintenances mt
                JOIN materiel_exemplaires e ON e.id = mt.exemplaire_id
                JOIN materiels m ON m.id = e.materiel_id
                LEFT JOIN departements d ON d.id = m.departement_id {clause}
            """, params)
            total = cur.fetchone()['n']

            cur.execute(f"""
                SELECT mt.*, e.numero_inventaire, e.id AS exemplaire_id,
                       m.nom AS materiel_nom, d.nom AS departement_nom
                FROM materiel_maintenances mt
                JOIN materiel_exemplaires e ON e.id = mt.exemplaire_id
                JOIN materiels m ON m.id = e.materiel_id
                LEFT JOIN departements d ON d.id = m.departement_id
                {clause}
                ORDER BY CASE WHEN mt.statut IN ('signale','assigne','envoye','a_valider')
                               THEN 0 ELSE 1 END,
                         mt.date_creation DESC
                LIMIT %s OFFSET %s
            """, params + [per_page, (page - 1) * per_page])
            interventions = cur.fetchall()

            cur.execute(f"""
                SELECT COUNT(*) FILTER (WHERE mt.statut = 'signale')     AS signalees,
                       COUNT(*) FILTER (WHERE mt.statut = 'assigne')     AS assignees,
                       COUNT(*) FILTER (WHERE mt.statut = 'envoye')      AS en_cours,
                       COUNT(*) FILTER (WHERE mt.statut = 'a_valider')   AS a_valider,
                       COUNT(*) FILTER (WHERE mt.statut = 'repare')      AS reparees,
                       COUNT(*) FILTER (WHERE mt.statut = 'irreparable') AS rebuts,
                       COALESCE(SUM(mt.cout), 0)                         AS cout_total
                FROM materiel_maintenances mt
                JOIN materiel_exemplaires e ON e.id=mt.exemplaire_id
                JOIN materiels m ON m.id=e.materiel_id
                LEFT JOIN departements d ON d.id=m.departement_id
                WHERE {dept_scope}
            """, dept_params)
            stats = cur.fetchone()
            cur.execute(f"SELECT d.id, d.nom FROM departements d WHERE {dept_scope} ORDER BY d.nom",
                        dept_params)
            departements = cur.fetchall()

            # Ce que l'utilisateur courant doit traiter personnellement, dans sa portée.
            cur.execute(f"""
                SELECT COUNT(*) FILTER (WHERE mt.assigne_user_id = %s
                                          AND mt.statut IN ('assigne','envoye')) AS a_traiter,
                       COUNT(*) FILTER (WHERE mt.signale_par_id = %s
                                          AND mt.statut = 'a_valider')           AS a_valider_moi
                  FROM materiel_maintenances mt
                  JOIN materiel_exemplaires e ON e.id=mt.exemplaire_id
                  JOIN materiels m ON m.id=e.materiel_id
                  LEFT JOIN departements d ON d.id=m.departement_id
                 WHERE {dept_scope}
            """, [session.get('user_id'), session.get('user_id')] + dept_params)
            mes_taches = cur.fetchone()

        pg = pagination_info(total, page, per_page)
        filters = {'statut': statut, 'departement': dept_id, 'portee': portee}
        return render_template('maintenances.html',
                               interventions=interventions, stats=stats,
                               departements=departements, filters=filters,
                               statuts=MAINTENANCE_STATUTS,
                               statuts_dict=MAINTENANCE_STATUTS_DICT,
                               mes_taches=mes_taches,
                               pg=pg, page_items=page_list(pg['page'], pg['pages']),
                               base_qs=urlencode({k: v for k, v in filters.items() if v}),
                               peut_gerer=_peut_gerer_materiels())


    @bp.route('/prestataires', methods=['GET', 'POST'])
    @login_required
    @role_required('rh')
    def prestataires_page():
        """Annuaire des prestataires externes de réparation."""
        if request.method == 'POST':
            nom = (request.form.get('nom') or '').strip()
            if not nom:
                flash("Le nom du prestataire est obligatoire.", "danger")
                return redirect(url_for('parc.prestataires_page'))
            try:
                with db_cursor(commit=True) as (conn, cur):
                    cur.execute("""INSERT INTO prestataires
                                     (nom, contact, telephone, email, specialite)
                                   VALUES (%s,%s,%s,%s,%s) RETURNING id""",
                                (nom[:150],
                                 (request.form.get('contact') or '').strip()[:150] or None,
                                 (request.form.get('telephone') or '').strip()[:40] or None,
                                 (request.form.get('email') or '').strip()[:150] or None,
                                 (request.form.get('specialite') or '').strip()[:100] or None))
                    pid = cur.fetchone()['id']
                log_action(session.get('user_id'), session.get('username'),
                           "Création prestataire", "prestataire", pid, nom)
                flash("Prestataire ajouté.", "success")
            except Exception as e:
                logger.error("Erreur création prestataire: %s", e, exc_info=True)
                flash(f"Erreur : {e}", "danger")
            return redirect(url_for('parc.prestataires_page'))

        with db_cursor() as (conn, cur):
            cur.execute("""
                SELECT p.*,
                       COUNT(mt.id) AS nb_interventions,
                       COALESCE(SUM(mt.cout), 0) AS cout_total
                  FROM prestataires p
                  LEFT JOIN materiel_maintenances mt ON mt.prestataire_id = p.id
                 GROUP BY p.id
                 ORDER BY p.actif DESC, p.nom
            """)
            liste = cur.fetchall()
        return render_template('prestataires.html', prestataires=liste)


    @bp.route('/prestataires/<int:id>/basculer', methods=['POST'])
    @login_required
    @role_required('rh')
    def basculer_prestataire(id):
        """Active / désactive un prestataire (on ne supprime pas : historique)."""
        try:
            with db_cursor(commit=True) as (conn, cur):
                cur.execute("""UPDATE prestataires SET actif = NOT actif
                               WHERE id = %s RETURNING nom, actif""", (id,))
                row = cur.fetchone()
            if row:
                flash("%s %s." % (row['nom'], "réactivé" if row['actif'] else "désactivé"),
                      "success")
        except Exception as e:
            logger.error("Erreur bascule prestataire: %s", e, exc_info=True)
            flash(f"Erreur : {e}", "danger")
        return redirect(url_for('parc.prestataires_page'))


    def _rendre_etiquettes(exemplaires, titre, sous_titre, retour_url):
        """Fabrique une planche d'étiquettes QR (rendu commun aux deux planches)."""
        etiquettes = [{
            'ex': ex,
            'qr': _qr_svg(url_for('parc.view_exemplaire', id=ex['id'], _external=True), taille=4),
        } for ex in exemplaires]
        return render_template(
            'etiquettes.html', etiquettes=etiquettes, titre=titre,
            sous_titre=sous_titre, retour_url=retour_url,
            qr_indisponible=bool(etiquettes) and etiquettes[0]['qr'] is None)


    @bp.route('/materiels/<int:id>/etiquettes')
    @login_required
    def etiquettes_materiel(id):
        """Planche d'étiquettes QR imprimables pour les exemplaires d'un article."""
        # (le rendu commun est délégué à _rendre_etiquettes, voir plus bas)
        with db_cursor() as (conn, cur):
            cur.execute("""SELECT m.*, d.nom AS departement_nom FROM materiels m
                           LEFT JOIN departements d ON d.id = m.departement_id
                           WHERE m.id = %s""", (id,))
            mat = cur.fetchone()
            if not mat:
                flash("Matériel introuvable", "danger")
                return redirect(url_for('parc.materiels'))
            cur.execute("""SELECT ex.*, m.nom AS materiel_nom, m.marque, m.modele
                           FROM materiel_exemplaires ex
                           JOIN materiels m ON m.id = ex.materiel_id
                           WHERE ex.materiel_id = %s
                           ORDER BY ex.numero_inventaire""", (id,))
            exemplaires = cur.fetchall()

        sous_titre = None
        if mat['departement_nom']:
            sous_titre = Markup("Département : <strong>%s</strong>."
                                % escape(mat['departement_nom']))
        return _rendre_etiquettes(exemplaires, titre=mat['nom'], sous_titre=sous_titre,
                                  retour_url=url_for('parc.view_materiel', id=id))


    @bp.route('/departements/<int:id>/etiquettes')
    @login_required
    def etiquettes_departement(id):
        """Planche d'étiquettes de tout le parc d'un département.

        C'est le cas d'usage d'un inventaire : on imprime une planche unique pour
        tous les exemplaires du service, puis on colle en une seule passe.
        """
        with db_cursor() as (conn, cur):
            cur.execute("SELECT * FROM departements WHERE id = %s", (id,))
            dept = cur.fetchone()
            if not dept:
                flash("Département introuvable", "danger")
                return redirect(url_for('parc.materiels'))
            cur.execute("""SELECT ex.*, m.nom AS materiel_nom, m.marque, m.modele
                           FROM materiel_exemplaires ex
                           JOIN materiels m ON m.id = ex.materiel_id
                           WHERE m.departement_id = %s AND ex.etat <> 'rebut'
                           ORDER BY m.nom, ex.numero_inventaire""", (id,))
            exemplaires = cur.fetchall()

        sous_titre = Markup(
            "Département : <strong>%s</strong> · %d exemplaire(s). "
            "Le matériel mis au rebut est exclu." % (escape(dept['nom']), len(exemplaires)))
        return _rendre_etiquettes(exemplaires, titre=dept['nom'], sous_titre=sous_titre,
                                  retour_url=url_for('parc.materiels', departement=id))


    @bp.route('/departements/<int:id>/materiels')
    @login_required
    def materiels_departement(id):
        """Raccourci : stock filtré sur un département."""
        return redirect(url_for('parc.materiels', departement=id))

    return bp, {'notifier_stock_bas': _notifier_stock_bas}
