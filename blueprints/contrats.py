"""Contrats des employés, renouvellements et alertes d'expiration."""

from datetime import date, timedelta
import io

from flask import Blueprint, abort, flash, redirect, render_template, request, send_file, session, url_for
import psycopg2
from werkzeug.utils import secure_filename

TYPE_CONTRATS = (
    ('cdi', 'CDI'), ('cdd', 'CDD'), ('stage', 'Stage'),
    ('consultant', 'Consultant / prestation'), ('autre', 'Autre'),
)
STATUTS_CONTRATS = ('actif', 'expire', 'resilie', 'renouvele')
MAX_CONTRAT_BYTES = 8 * 1024 * 1024


def creer_blueprint_contrats(deps):
    bp = Blueprint('contrats', __name__)
    db_cursor = deps['db_cursor']
    login_required = deps['login_required']
    role_required = deps['role_required']
    get_current_employee = deps['get_current_employee']
    detect_file_type = deps['detect_file_type']
    create_notification = deps['create_notification']
    queue_email = deps['queue_email']
    log_action = deps['log_action']

    def _lire_fichier(fichier):
        if not fichier or not fichier.filename:
            return None, None
        fichier.stream.seek(0, 2)
        taille = fichier.stream.tell()
        fichier.stream.seek(0)
        if taille <= 0:
            return None, "Le fichier est vide."
        if taille > MAX_CONTRAT_BYTES:
            return None, "Le fichier dépasse 8 Mo."
        extension = detect_file_type(fichier)
        if extension is None:
            return None, "Le contenu du fichier ne correspond pas à son extension."
        fichier.stream.seek(0)
        contenu = fichier.stream.read()
        return {'nom': secure_filename(fichier.filename)[:255], 'type': extension,
                'taille': len(contenu), 'contenu': contenu}, None

    def _peut_voir(contrat):
        if session.get('role') in ('admin', 'rh'):
            return True
        employe = get_current_employee()
        return bool(employe and employe['id'] == contrat['employe_id'])

    @bp.route('/contrats')
    @login_required
    def contrats_liste():
        employe = get_current_employee()
        statut = (request.args.get('statut') or '').strip()
        employe_id = request.args.get('employe_id', type=int)
        where, params = [], []
        if session.get('role') not in ('admin', 'rh'):
            where.append('c.employe_id=%s')
            params.append(employe['id'] if employe else -1)
        elif employe_id:
            where.append('c.employe_id=%s')
            params.append(employe_id)
        if statut in STATUTS_CONTRATS:
            where.append('c.statut=%s')
            params.append(statut)
        clause = ('WHERE ' + ' AND '.join(where)) if where else ''
        with db_cursor() as (conn, cur):
            cur.execute(f"""SELECT c.*, e.nom, e.prenom, e.departement
                            FROM contrats c JOIN employes e ON e.id=c.employe_id
                            {clause}
                            ORDER BY CASE c.statut WHEN 'actif' THEN 0 ELSE 1 END,
                                     c.date_fin NULLS LAST, c.date_debut DESC""", params)
            contrats = cur.fetchall()
            if session.get('role') in ('admin', 'rh'):
                cur.execute("SELECT id,nom,prenom FROM employes WHERE actif ORDER BY nom,prenom")
                employes = cur.fetchall()
            else:
                employes = []
        return render_template('contrats.html', contrats=contrats, employes=employes,
                               type_contrats=TYPE_CONTRATS, today=date.today(),
                               bientot=date.today() + timedelta(days=30),
                               selected_statut=statut, selected_employe=employe_id)

    @bp.route('/employes/<int:employe_id>/contrats/nouveau', methods=['GET', 'POST'])
    @login_required
    @role_required('rh')
    def contrat_nouveau(employe_id):
        with db_cursor() as (conn, cur):
            cur.execute("SELECT id,nom,prenom FROM employes WHERE id=%s AND actif", (employe_id,))
            employe = cur.fetchone()
        if not employe:
            abort(404)
        if request.method == 'POST':
            type_contrat = (request.form.get('type_contrat') or '').strip()
            reference = (request.form.get('reference') or '').strip()[:80] or None
            debut = request.form.get('date_debut')
            fin = request.form.get('date_fin') or None
            notes = (request.form.get('notes') or '').strip()
            if type_contrat not in dict(TYPE_CONTRATS) or not debut:
                flash("Type et date de début obligatoires.", 'danger')
                return redirect(request.url)
            if fin and fin < debut:
                flash("La fin du contrat ne peut pas précéder son début.", 'danger')
                return redirect(request.url)
            fichier, erreur = _lire_fichier(request.files.get('fichier'))
            if erreur:
                flash(erreur, 'danger')
                return redirect(request.url)
            with db_cursor(commit=True) as (conn, cur):
                cur.execute("""INSERT INTO contrats
                    (employe_id,type_contrat,reference,date_debut,date_fin,notes,
                     nom_fichier,type_fichier,taille,contenu,cree_par)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                            (employe_id, type_contrat, reference, debut, fin, notes or None,
                             fichier['nom'] if fichier else None,
                             fichier['type'] if fichier else None,
                             fichier['taille'] if fichier else None,
                             psycopg2.Binary(fichier['contenu']) if fichier else None,
                             session.get('user_id')))
                contrat_id = cur.fetchone()['id']
            log_action(session.get('user_id'), session.get('username'), 'CREATE_CONTRAT',
                       'contrat', contrat_id, f"employe={employe_id}, type={type_contrat}")
            flash("Contrat créé.", 'success')
            return redirect(url_for('contrats.contrat_voir', id=contrat_id))
        return render_template('contrat_form.html', employe=employe,
                               type_contrats=TYPE_CONTRATS, contrat=None)

    @bp.route('/contrats/<int:id>')
    @login_required
    def contrat_voir(id):
        with db_cursor() as (conn, cur):
            cur.execute("""SELECT c.*,e.nom,e.prenom,e.departement
                           FROM contrats c JOIN employes e ON e.id=c.employe_id
                           WHERE c.id=%s""", (id,))
            contrat = cur.fetchone()
        if not contrat:
            abort(404)
        if not _peut_voir(contrat):
            abort(403)
        return render_template('contrat_detail.html', contrat=contrat,
                               type_contrats=dict(TYPE_CONTRATS), today=date.today())

    @bp.route('/contrats/<int:id>/fichier')
    @login_required
    def contrat_fichier(id):
        with db_cursor() as (conn, cur):
            cur.execute("SELECT * FROM contrats WHERE id=%s", (id,))
            contrat = cur.fetchone()
        if not contrat or contrat.get('contenu') is None:
            abort(404)
        if not _peut_voir(contrat):
            abort(403)
        response = send_file(io.BytesIO(bytes(contrat['contenu'])), as_attachment=True,
                             download_name=contrat['nom_fichier'] or 'contrat')
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Cache-Control'] = 'private, no-store'
        return response

    @bp.route('/contrats/<int:id>/renouveler', methods=['GET', 'POST'])
    @login_required
    @role_required('rh')
    def contrat_renouveler(id):
        with db_cursor() as (conn, cur):
            cur.execute("SELECT c.*,e.nom,e.prenom FROM contrats c JOIN employes e ON e.id=c.employe_id WHERE c.id=%s AND e.actif", (id,))
            ancien = cur.fetchone()
        if not ancien:
            abort(404)
        if request.method == 'POST':
            debut = request.form.get('date_debut')
            fin = request.form.get('date_fin') or None
            type_contrat = (request.form.get('type_contrat') or ancien['type_contrat']).strip()
            if not debut or type_contrat not in dict(TYPE_CONTRATS) or (fin and fin < debut):
                flash("Informations de renouvellement invalides.", 'danger')
                return redirect(request.url)
            fichier, erreur = _lire_fichier(request.files.get('fichier'))
            if erreur:
                flash(erreur, 'danger')
                return redirect(request.url)
            with db_cursor(commit=True) as (conn, cur):
                cur.execute("UPDATE contrats SET statut='renouvele',date_modification=CURRENT_TIMESTAMP WHERE id=%s", (id,))
                cur.execute("""INSERT INTO contrats
                    (employe_id,type_contrat,reference,date_debut,date_fin,notes,
                     nom_fichier,type_fichier,taille,contenu,renouvelle_depuis,cree_par)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                            (ancien['employe_id'], type_contrat,
                             (request.form.get('reference') or '').strip()[:80] or ancien.get('reference'),
                             debut, fin, (request.form.get('notes') or '').strip() or None,
                             fichier['nom'] if fichier else None,
                             fichier['type'] if fichier else None,
                             fichier['taille'] if fichier else None,
                             psycopg2.Binary(fichier['contenu']) if fichier else None,
                             id, session.get('user_id')))
                nouveau_id = cur.fetchone()['id']
            log_action(session.get('user_id'), session.get('username'), 'RENEW_CONTRAT',
                       'contrat', nouveau_id, f"ancien={id}")
            flash("Contrat renouvelé.", 'success')
            return redirect(url_for('contrats.contrat_voir', id=nouveau_id))
        return render_template('contrat_form.html', employe=ancien,
                               type_contrats=TYPE_CONTRATS, contrat=ancien, renouvellement=True)

    @bp.route('/contrats/<int:id>/resilier', methods=['POST'])
    @login_required
    @role_required('rh')
    def contrat_resilier(id):
        motif = (request.form.get('motif') or '').strip()
        if not motif:
            flash("Le motif de résiliation est obligatoire.", 'danger')
            return redirect(url_for('contrats.contrat_voir', id=id))
        with db_cursor(commit=True) as (conn, cur):
            cur.execute("""UPDATE contrats SET statut='resilie',notes=CONCAT_WS(E'\n',notes,%s),
                           date_modification=CURRENT_TIMESTAMP WHERE id=%s RETURNING employe_id""",
                        (f"Résiliation : {motif}", id))
            row = cur.fetchone()
        if not row:
            abort(404)
        log_action(session.get('user_id'), session.get('username'), 'TERMINATE_CONTRAT',
                   'contrat', id, motif[:120])
        flash("Contrat résilié.", 'success')
        return redirect(url_for('contrats.contrat_voir', id=id))

    def job_alertes_contrats():
        """Alertes idempotentes à J-30, J-7 et après expiration."""
        with db_cursor(commit=True) as (conn, cur):
            cur.execute("""INSERT INTO scheduler_runs(job_name,run_date)
                           VALUES ('alertes_contrats',CURRENT_DATE)
                           ON CONFLICT DO NOTHING RETURNING job_name""")
            if cur.fetchone() is None:
                return 0
            cur.execute("""UPDATE contrats SET statut='expire',date_modification=CURRENT_TIMESTAMP
                           WHERE statut='actif' AND date_fin<CURRENT_DATE""")
            cur.execute("""SELECT c.*,e.nom,e.prenom,e.email,u.id AS user_id
                           FROM contrats c JOIN employes e ON e.id=c.employe_id
                           LEFT JOIN users u ON u.employe_id=e.id
                           WHERE e.actif AND c.statut IN ('actif','expire') AND c.date_fin IS NOT NULL
                             AND c.date_fin<=CURRENT_DATE+INTERVAL '30 days'""")
            candidats = cur.fetchall()
            cur.execute("""SELECT u.id,e.email FROM users u LEFT JOIN employes e ON e.id=u.employe_id
                           WHERE u.role IN ('admin','rh')""")
            responsables = cur.fetchall()
            envoyees = 0
            for contrat in candidats:
                jours = (contrat['date_fin'] - date.today()).days
                type_alerte = 'expire' if jours < 0 else ('j7' if jours <= 7 else 'j30')
                cur.execute("""INSERT INTO contrats_alertes(contrat_id,type_alerte)
                               VALUES (%s,%s) ON CONFLICT DO NOTHING RETURNING contrat_id""",
                            (contrat['id'], type_alerte))
                if cur.fetchone() is None:
                    continue
                envoyees += 1
                nom = f"{contrat['prenom']} {contrat['nom']}"
                message = (f"Contrat {contrat['type_contrat']} de {nom} "
                           f"{'expiré' if jours < 0 else 'expire'} le {contrat['date_fin']}.")
                destinataires = {r['id']: r.get('email') for r in responsables}
                if contrat.get('user_id'):
                    destinataires[contrat['user_id']] = contrat.get('email')
                for user_id, email in destinataires.items():
                    create_notification(user_id, "Échéance de contrat", message, 'warning', cur=cur)
                    queue_email(email, "Échéance de contrat", message, cur=cur,
                                event_key=f"contrat:{contrat['id']}:{type_alerte}:{user_id}")
            return envoyees

    return bp, {'job_alertes_contrats': job_alertes_contrats}
