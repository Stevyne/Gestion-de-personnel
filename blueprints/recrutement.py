"""Recrutement : demandes, offres, candidats, évaluations et embauche."""

from datetime import date
import os

from flask import (Blueprint, abort, flash, redirect, render_template, request,
                   session, url_for)
import psycopg2
from werkzeug.utils import secure_filename

from services.object_storage import ObjectStorageError
from services.recrutement import (
    CANDIDATURE_STATUTS, CRITERES_ENTRETIEN, DEMANDE_STATUTS,
    ENTRETIEN_STATUTS, OFFRE_STATUTS, TYPE_CONTRATS,
    calculer_score_global, calculer_score_pondere, lignes_texte, recommandation,
)

MAX_CANDIDAT_FILE_BYTES = 8 * 1024 * 1024
CANDIDAT_FILE_EXTENSIONS = {'pdf', 'doc', 'docx'}


def creer_blueprint_recrutement(deps):
    bp = Blueprint('recrutement', __name__)
    db_cursor = deps['db_cursor']
    login_required = deps['login_required']
    role_required = deps['role_required']
    get_department_scope = deps['get_department_scope']
    detect_file_type = deps['detect_file_type']
    object_storage = deps['object_storage']
    create_notification = deps['create_notification']
    queue_email = deps['queue_email']
    log_action = deps['log_action']

    def _est_rh():
        return session.get('role') in ('admin', 'rh')

    def _nombre_positif(valeur, defaut=None):
        if valeur in (None, ''):
            return defaut
        nombre = round(float(valeur), 2)
        if nombre < 0:
            raise ValueError
        return nombre

    def _departements_autorises(cur):
        scope = get_department_scope(cur)
        if scope['is_global']:
            cur.execute("SELECT id,nom FROM departements ORDER BY nom")
        elif scope['is_empty']:
            return []
        else:
            cur.execute("SELECT id,nom FROM departements WHERE nom=%s",
                        (scope['department'],))
        return cur.fetchall()

    def _departement_autorise(cur, departement_id):
        return int(departement_id) in {d['id'] for d in _departements_autorises(cur)}

    def _charger_demande(cur, demande_id):
        cur.execute("""SELECT r.*,d.nom AS departement,u.username AS demandeur
                       FROM recrutement_demandes r
                       JOIN departements d ON d.id=r.departement_id
                       LEFT JOIN users u ON u.id=r.demandeur_user_id
                       WHERE r.id=%s""", (demande_id,))
        demande = cur.fetchone()
        if not demande:
            abort(404)
        scope = get_department_scope(cur)
        if not scope['is_global'] and (
            scope['is_empty'] or demande['departement'] != scope['department']
        ):
            abort(403)
        return demande

    def _charger_candidature(cur, candidature_id, lock=False):
        suffix = ' FOR UPDATE OF ca,c' if lock else ''
        cur.execute("""SELECT ca.*,c.nom,c.prenom,c.email,c.telephone,c.diplome,
                              c.experience,c.experience_annees,c.competences,
                              c.cv_nom,c.lettre_nom,c.employe_id,
                              o.titre AS offre_titre,o.poste,o.type_contrat,
                              o.nombre_postes,o.departement_id,d.nom AS departement
                       FROM recrutement_candidatures ca
                       JOIN recrutement_candidats c ON c.id=ca.candidat_id
                       JOIN recrutement_offres o ON o.id=ca.offre_id
                       JOIN departements d ON d.id=o.departement_id
                       WHERE ca.id=%s""" + suffix, (candidature_id,))
        candidature = cur.fetchone()
        if not candidature:
            abort(404)
        return candidature

    def _lire_fichier(fichier, categorie):
        if not fichier or not fichier.filename:
            return None
        extension = fichier.filename.rsplit('.', 1)[-1].lower() if '.' in fichier.filename else ''
        if extension not in CANDIDAT_FILE_EXTENSIONS:
            raise ValueError("Seuls les fichiers PDF, DOC et DOCX sont acceptés.")
        fichier.stream.seek(0, os.SEEK_END)
        taille = fichier.stream.tell()
        fichier.stream.seek(0)
        if taille <= 0 or taille > MAX_CANDIDAT_FILE_BYTES:
            raise ValueError("Le fichier doit être non vide et ne pas dépasser 8 Mo.")
        detected = detect_file_type(fichier)
        if detected not in CANDIDAT_FILE_EXTENSIONS:
            raise ValueError("Le contenu réel du fichier ne correspond pas à son extension.")
        contenu = fichier.stream.read()
        nom = secure_filename(fichier.filename)[:255] or f'document.{extension}'
        try:
            stored = object_storage.store(
                f'recrutement-{categorie}', contenu, nom,
                content_type='application/pdf' if detected == 'pdf' else None,
            )
        except ObjectStorageError as exc:
            raise ValueError(str(exc)) from exc
        return {'nom': nom, 'type': detected, 'taille': len(contenu), 'stored': stored}

    def _colonnes_fichier(prefixe, fichier):
        if not fichier:
            return {}
        stored = fichier['stored']
        return {
            f'{prefixe}_nom': fichier['nom'], f'{prefixe}_type': fichier['type'],
            f'{prefixe}_taille': fichier['taille'],
            f'{prefixe}_contenu': psycopg2.Binary(stored.content)
            if stored.content is not None else None,
            f'{prefixe}_storage_key': stored.storage_key,
            f'{prefixe}_storage_etag': stored.storage_etag,
            f'{prefixe}_storage_sha256': stored.storage_sha256,
        }

    def _criteres_defaut(competences):
        libelles = lignes_texte(competences)[:4]
        for standard in ('Expérience', 'Diplôme', 'Communication'):
            if standard.casefold() not in {x.casefold() for x in libelles}:
                libelles.append(standard)
        libelles = libelles[:7] or ['Expérience', 'Compétences', 'Communication']
        base, reste = divmod(100, len(libelles))
        return [(libelle, base + (1 if index < reste else 0))
                for index, libelle in enumerate(libelles)]

    def _recalculer_scores(cur, candidature_id):
        cur.execute("""SELECT rc.id,rc.poids,re.note
                       FROM recrutement_criteres rc
                       LEFT JOIN recrutement_evaluations re
                         ON re.critere_id=rc.id AND re.candidature_id=%s
                       JOIN recrutement_candidatures ca ON ca.offre_id=rc.offre_id
                       WHERE ca.id=%s ORDER BY rc.ordre,rc.id""",
                    (candidature_id, candidature_id))
        lignes = cur.fetchall()
        score_dossier = calculer_score_pondere(
            lignes, {r['id']: r['note'] for r in lignes if r['note'] is not None}
        )
        cur.execute("""SELECT score FROM recrutement_entretiens
                       WHERE candidature_id=%s AND statut='realise' AND score IS NOT NULL""",
                    (candidature_id,))
        scores_entretiens = [r['score'] for r in cur.fetchall()]
        score_entretien, score_global = calculer_score_global(
            score_dossier, scores_entretiens
        )
        cur.execute("""UPDATE recrutement_candidatures
                       SET score_dossier=%s,score_entretien=%s,score_global=%s,
                           date_modification=CURRENT_TIMESTAMP
                       WHERE id=%s""",
                    (score_dossier, score_entretien, score_global, candidature_id))
        return score_dossier, score_entretien, score_global

    @bp.route('/recrutement')
    @login_required
    @role_required('rh', 'manager')
    def tableau_recrutement():
        with db_cursor() as (_conn, cur):
            scope = get_department_scope(cur)
            params = []
            clause = ''
            if not scope['is_global']:
                clause = ' WHERE d.nom=%s' if not scope['is_empty'] else ' WHERE FALSE'
                params = [scope.get('department')] if not scope['is_empty'] else []
            cur.execute(f"""SELECT
                COUNT(*) AS total,
                COUNT(*) FILTER (WHERE r.statut='en_attente') AS demandes_attente,
                COUNT(*) FILTER (WHERE r.statut IN ('validee','publiee')) AS demandes_validees
                FROM recrutement_demandes r JOIN departements d ON d.id=r.departement_id
                {clause}""", params)
            stats = cur.fetchone()
            if _est_rh():
                cur.execute("""SELECT
                    (SELECT COUNT(*) FROM recrutement_offres WHERE statut='publiee') AS offres,
                    (SELECT COUNT(*) FROM recrutement_candidatures WHERE statut NOT IN ('refusee','embauchee')) AS candidatures,
                    (SELECT COUNT(*) FROM recrutement_entretiens WHERE statut='planifie') AS entretiens,
                    (SELECT COUNT(*) FROM recrutement_candidatures WHERE statut='acceptee') AS a_embaucher""")
                stats.update(cur.fetchone())
                cur.execute("""SELECT ca.id,c.nom,c.prenom,o.titre,ca.statut,ca.score_global
                               FROM recrutement_candidatures ca
                               JOIN recrutement_candidats c ON c.id=ca.candidat_id
                               JOIN recrutement_offres o ON o.id=ca.offre_id
                               ORDER BY ca.date_modification DESC LIMIT 8""")
                recentes = cur.fetchall()
            else:
                recentes = []
        return render_template('recrutement_dashboard.html', stats=stats,
                               recentes=recentes, est_rh=_est_rh())

    @bp.route('/recrutement/demandes')
    @login_required
    @role_required('rh', 'manager')
    def demandes():
        statut = (request.args.get('statut') or '').strip()
        with db_cursor() as (_conn, cur):
            scope = get_department_scope(cur)
            where, params = [], []
            if not scope['is_global']:
                if scope['is_empty']:
                    where.append('FALSE')
                else:
                    where.append('d.nom=%s'); params.append(scope['department'])
            if statut in DEMANDE_STATUTS:
                where.append('r.statut=%s'); params.append(statut)
            clause = 'WHERE ' + ' AND '.join(where) if where else ''
            cur.execute(f"""SELECT r.*,d.nom AS departement,u.username AS demandeur
                            FROM recrutement_demandes r
                            JOIN departements d ON d.id=r.departement_id
                            LEFT JOIN users u ON u.id=r.demandeur_user_id
                            {clause} ORDER BY r.date_creation DESC""", params)
            lignes = cur.fetchall()
        return render_template('recrutement_demandes.html', demandes=lignes,
                               statuts=DEMANDE_STATUTS, filtre_statut=statut)

    @bp.route('/recrutement/demandes/nouvelle', methods=['GET', 'POST'])
    @login_required
    @role_required('rh', 'manager')
    def demande_nouvelle():
        with db_cursor(commit=request.method == 'POST') as (_conn, cur):
            departements = _departements_autorises(cur)
            if request.method == 'POST':
                departement_id = request.form.get('departement_id', type=int)
                poste = (request.form.get('poste') or '').strip()[:150]
                nombre = request.form.get('nombre_postes', type=int) or 1
                type_contrat = (request.form.get('type_contrat') or '').strip()
                try:
                    salaire_min = _nombre_positif(request.form.get('salaire_min'))
                    salaire_max = _nombre_positif(request.form.get('salaire_max'))
                except (TypeError, ValueError):
                    flash("Les montants de salaire doivent être des nombres positifs.", 'danger')
                    return render_template('recrutement_demande_form.html',
                                           departements=departements,
                                           types_contrats=TYPE_CONTRATS)
                motif = (request.form.get('motif') or '').strip()
                if (not poste or not motif or type_contrat not in TYPE_CONTRATS
                        or nombre < 1 or not departement_id
                        or not _departement_autorise(cur, departement_id)):
                    flash("Informations de demande invalides.", 'danger')
                elif salaire_min and salaire_max and float(salaire_max) < float(salaire_min):
                    flash("Le salaire maximum doit être supérieur au minimum.", 'danger')
                else:
                    cur.execute("""INSERT INTO recrutement_demandes
                        (poste,departement_id,nombre_postes,type_contrat,date_souhaitee,
                         salaire_min,salaire_max,motif,competences,demandeur_user_id)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                        (poste, departement_id, nombre, type_contrat,
                         request.form.get('date_souhaitee') or None,
                         salaire_min, salaire_max, motif,
                         '\n'.join(lignes_texte(request.form.get('competences'))),
                         session.get('user_id')))
                    demande_id = cur.fetchone()['id']
                    reference = f"REC-{date.today().year}-{demande_id:04d}"
                    cur.execute("UPDATE recrutement_demandes SET reference=%s WHERE id=%s",
                                (reference, demande_id))
                    cur.execute("SELECT id FROM users WHERE role IN ('admin','rh') AND actif")
                    for user in cur.fetchall():
                        create_notification(user['id'], "Demande de recrutement",
                                            f"{reference} — {poste} attend votre décision.",
                                            'info', cur=cur)
                    log_action(session.get('user_id'), session.get('username'),
                               'CREATE_RECRUITMENT_REQUEST', 'recrutement_demande',
                               demande_id, reference)
                    flash("Demande de recrutement transmise aux RH.", 'success')
                    return redirect(url_for('.demande_detail', demande_id=demande_id))
        return render_template('recrutement_demande_form.html', departements=departements,
                               types_contrats=TYPE_CONTRATS)

    @bp.route('/recrutement/demandes/<int:demande_id>')
    @login_required
    @role_required('rh', 'manager')
    def demande_detail(demande_id):
        with db_cursor() as (_conn, cur):
            demande = _charger_demande(cur, demande_id)
            cur.execute("SELECT id,reference,titre,statut FROM recrutement_offres WHERE demande_id=%s",
                        (demande_id,))
            offre = cur.fetchone()
        return render_template('recrutement_demande_detail.html', demande=demande,
                               offre=offre, statuts=DEMANDE_STATUTS,
                               types_contrats=TYPE_CONTRATS, est_rh=_est_rh())

    @bp.route('/recrutement/demandes/<int:demande_id>/decision', methods=['POST'])
    @login_required
    @role_required('rh')
    def demande_decision(demande_id):
        decision = (request.form.get('decision') or '').strip()
        motif = (request.form.get('motif_decision') or '').strip()
        if decision not in ('validee', 'refusee') or (decision == 'refusee' and not motif):
            flash("Décision ou motif invalide.", 'danger')
            return redirect(url_for('.demande_detail', demande_id=demande_id))
        with db_cursor(commit=True) as (_conn, cur):
            demande = _charger_demande(cur, demande_id)
            if demande['statut'] != 'en_attente':
                flash("Cette demande a déjà été traitée.", 'warning')
                return redirect(url_for('.demande_detail', demande_id=demande_id))
            cur.execute("""UPDATE recrutement_demandes SET statut=%s,decide_par=%s,
                           motif_decision=%s,date_decision=CURRENT_TIMESTAMP,
                           date_modification=CURRENT_TIMESTAMP WHERE id=%s""",
                        (decision, session.get('user_id'), motif or None, demande_id))
            if demande.get('demandeur_user_id'):
                create_notification(
                    demande['demandeur_user_id'], "Décision recrutement",
                    f"La demande {demande['reference']} a été {DEMANDE_STATUTS[decision].lower()}.",
                    'success' if decision == 'validee' else 'warning', cur=cur,
                )
        log_action(session.get('user_id'), session.get('username'),
                   'DECIDE_RECRUITMENT_REQUEST', 'recrutement_demande',
                   demande_id, decision)
        flash("Décision enregistrée.", 'success')
        return redirect(url_for('.demande_detail', demande_id=demande_id))

    @bp.route('/recrutement/offres')
    @login_required
    @role_required('rh')
    def offres():
        statut = (request.args.get('statut') or '').strip()
        with db_cursor() as (_conn, cur):
            params = []
            clause = ''
            if statut in OFFRE_STATUTS:
                clause = 'WHERE o.statut=%s'; params.append(statut)
            cur.execute(f"""SELECT o.*,d.nom AS departement,
                              COUNT(ca.id) AS nb_candidatures
                             FROM recrutement_offres o
                             JOIN departements d ON d.id=o.departement_id
                             LEFT JOIN recrutement_candidatures ca ON ca.offre_id=o.id
                             {clause} GROUP BY o.id,d.nom
                             ORDER BY o.date_creation DESC""", params)
            lignes = cur.fetchall()
        return render_template('recrutement_offres.html', offres=lignes,
                               statuts=OFFRE_STATUTS, filtre_statut=statut)

    @bp.route('/recrutement/offres/nouvelle', methods=['GET', 'POST'])
    @login_required
    @role_required('rh')
    def offre_nouvelle():
        demande_id = request.args.get('demande_id', type=int) or request.form.get('demande_id', type=int)
        with db_cursor(commit=request.method == 'POST') as (_conn, cur):
            cur.execute("SELECT id,nom FROM departements ORDER BY nom")
            departements = cur.fetchall()
            demande = None
            if demande_id:
                cur.execute("SELECT * FROM recrutement_demandes WHERE id=%s", (demande_id,))
                demande = cur.fetchone()
                if not demande or demande['statut'] != 'validee':
                    abort(400)
            if request.method == 'POST':
                titre = (request.form.get('titre') or '').strip()[:200]
                description = (request.form.get('description') or '').strip()
                departement_id = request.form.get('departement_id', type=int)
                type_contrat = (request.form.get('type_contrat') or '').strip()
                nombre = request.form.get('nombre_postes', type=int) or 1
                try:
                    salaire_min = _nombre_positif(request.form.get('salaire_min'))
                    salaire_max = _nombre_positif(request.form.get('salaire_max'))
                except (TypeError, ValueError):
                    flash("Les montants de salaire doivent être des nombres positifs.", 'danger')
                    return render_template('recrutement_offre_form.html', demande=demande,
                                           departements=departements,
                                           types_contrats=TYPE_CONTRATS)
                if (not titre or not description or not departement_id or nombre < 1
                        or type_contrat not in TYPE_CONTRATS):
                    flash("Titre, description, département et contrat sont obligatoires.", 'danger')
                elif salaire_min and salaire_max and float(salaire_max) < float(salaire_min):
                    flash("Fourchette salariale invalide.", 'danger')
                else:
                    cur.execute("""INSERT INTO recrutement_offres
                        (demande_id,titre,description,departement_id,poste,competences,
                         niveau_experience,diplome_requis,type_contrat,salaire_min,salaire_max,
                         localisation,date_publication,date_limite,nombre_postes,statut,cree_par)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'brouillon',%s)
                        RETURNING id""",
                        (demande_id, titre, description, departement_id,
                         (request.form.get('poste') or titre).strip()[:150],
                         '\n'.join(lignes_texte(request.form.get('competences'))),
                         (request.form.get('niveau_experience') or '').strip()[:100] or None,
                         (request.form.get('diplome_requis') or '').strip()[:150] or None,
                         type_contrat, salaire_min, salaire_max,
                         (request.form.get('localisation') or '').strip()[:180] or None,
                         request.form.get('date_publication') or None,
                         request.form.get('date_limite') or None, nombre,
                         session.get('user_id')))
                    offre_id = cur.fetchone()['id']
                    reference = f"OFF-{date.today().year}-{offre_id:04d}"
                    cur.execute("UPDATE recrutement_offres SET reference=%s WHERE id=%s",
                                (reference, offre_id))
                    for ordre, (libelle, poids) in enumerate(
                        _criteres_defaut(request.form.get('competences')), 1
                    ):
                        cur.execute("""INSERT INTO recrutement_criteres
                                       (offre_id,libelle,poids,ordre) VALUES (%s,%s,%s,%s)""",
                                    (offre_id, libelle, poids, ordre))
                    log_action(session.get('user_id'), session.get('username'),
                               'CREATE_JOB_OFFER', 'recrutement_offre', offre_id, reference)
                    flash("Offre créée en brouillon avec une grille initiale.", 'success')
                    return redirect(url_for('.offre_detail', offre_id=offre_id))
        return render_template('recrutement_offre_form.html', demande=demande,
                               departements=departements, types_contrats=TYPE_CONTRATS)

    @bp.route('/recrutement/offres/<int:offre_id>')
    @login_required
    @role_required('rh')
    def offre_detail(offre_id):
        with db_cursor() as (_conn, cur):
            cur.execute("""SELECT o.*,d.nom AS departement FROM recrutement_offres o
                           JOIN departements d ON d.id=o.departement_id WHERE o.id=%s""",
                        (offre_id,))
            offre = cur.fetchone()
            if not offre:
                abort(404)
            cur.execute("SELECT * FROM recrutement_criteres WHERE offre_id=%s ORDER BY ordre,id",
                        (offre_id,))
            criteres = cur.fetchall()
            cur.execute("""SELECT ca.id,ca.statut,ca.score_dossier,ca.score_entretien,
                                  ca.score_global,c.nom,c.prenom,c.email
                           FROM recrutement_candidatures ca
                           JOIN recrutement_candidats c ON c.id=ca.candidat_id
                           WHERE ca.offre_id=%s ORDER BY ca.score_global DESC NULLS LAST,
                           ca.date_candidature""", (offre_id,))
            candidatures = cur.fetchall()
        return render_template('recrutement_offre_detail.html', offre=offre,
                               criteres=criteres, candidatures=candidatures,
                               statuts=OFFRE_STATUTS,
                               candidature_statuts=CANDIDATURE_STATUTS,
                               recommandation=recommandation)

    @bp.route('/recrutement/offres/<int:offre_id>/statut', methods=['POST'])
    @login_required
    @role_required('rh')
    def offre_statut(offre_id):
        statut = (request.form.get('statut') or '').strip()
        if statut not in OFFRE_STATUTS:
            abort(400)
        with db_cursor(commit=True) as (_conn, cur):
            cur.execute("SELECT id,demande_id FROM recrutement_offres WHERE id=%s", (offre_id,))
            offre = cur.fetchone()
            if not offre: abort(404)
            if statut == 'publiee':
                cur.execute("SELECT COALESCE(SUM(poids),0) AS total FROM recrutement_criteres WHERE offre_id=%s",
                            (offre_id,))
                if abs(float(cur.fetchone()['total']) - 100) > 0.01:
                    flash("La grille doit totaliser 100 % avant publication.", 'danger')
                    return redirect(url_for('.offre_detail', offre_id=offre_id))
            cur.execute("""UPDATE recrutement_offres SET statut=%s,
                           date_publication=CASE WHEN %s='publiee' THEN COALESCE(date_publication,CURRENT_DATE) ELSE date_publication END,
                           date_modification=CURRENT_TIMESTAMP WHERE id=%s""",
                        (statut, statut, offre_id))
            if statut == 'publiee' and offre.get('demande_id'):
                cur.execute("""UPDATE recrutement_demandes SET statut='publiee',
                               date_modification=CURRENT_TIMESTAMP WHERE id=%s""",
                            (offre['demande_id'],))
        flash("Statut de l'offre mis à jour.", 'success')
        return redirect(url_for('.offre_detail', offre_id=offre_id))

    @bp.route('/recrutement/offres/<int:offre_id>/criteres', methods=['POST'])
    @login_required
    @role_required('rh')
    def offre_criteres(offre_id):
        libelles = request.form.getlist('critere_libelle')
        poids_bruts = request.form.getlist('critere_poids')
        criteres = []
        try:
            for libelle, poids in zip(libelles, poids_bruts):
                libelle = libelle.strip()[:150]
                if libelle:
                    criteres.append((libelle, round(float(poids), 2)))
        except (TypeError, ValueError):
            criteres = []
        if not criteres or any(p <= 0 or p > 100 for _, p in criteres) or abs(sum(p for _, p in criteres) - 100) > 0.01:
            flash("La grille doit contenir des critères positifs totalisant exactement 100 %.", 'danger')
            return redirect(url_for('.offre_detail', offre_id=offre_id))
        if len({l.casefold() for l, _ in criteres}) != len(criteres):
            flash("Chaque critère doit être unique.", 'danger')
            return redirect(url_for('.offre_detail', offre_id=offre_id))
        with db_cursor(commit=True) as (_conn, cur):
            cur.execute("SELECT 1 FROM recrutement_candidatures WHERE offre_id=%s AND score_dossier IS NOT NULL LIMIT 1",
                        (offre_id,))
            if cur.fetchone():
                flash("La grille ne peut plus être remplacée après une évaluation.", 'warning')
                return redirect(url_for('.offre_detail', offre_id=offre_id))
            cur.execute("DELETE FROM recrutement_criteres WHERE offre_id=%s", (offre_id,))
            for ordre, (libelle, poids) in enumerate(criteres, 1):
                cur.execute("INSERT INTO recrutement_criteres(offre_id,libelle,poids,ordre) VALUES (%s,%s,%s,%s)",
                            (offre_id, libelle, poids, ordre))
        flash("Grille d'évaluation enregistrée.", 'success')
        return redirect(url_for('.offre_detail', offre_id=offre_id))

    @bp.route('/recrutement/candidats')
    @login_required
    @role_required('rh')
    def candidats():
        recherche = (request.args.get('q') or '').strip().lower()
        with db_cursor() as (_conn, cur):
            params, clause = [], ''
            if recherche:
                clause = "WHERE LOWER(c.nom) LIKE %s OR LOWER(c.prenom) LIKE %s OR LOWER(c.email) LIKE %s"
                motif = f'%{recherche}%'; params = [motif, motif, motif]
            cur.execute(f"""SELECT c.*,
                (SELECT COUNT(*) FROM recrutement_candidatures ca WHERE ca.candidat_id=c.id) AS nb_candidatures
                FROM recrutement_candidats c {clause} ORDER BY c.date_creation DESC""", params)
            lignes = cur.fetchall()
        return render_template('recrutement_candidats.html', candidats=lignes, recherche=recherche)

    @bp.route('/recrutement/candidats/nouveau', methods=['GET', 'POST'])
    @login_required
    @role_required('rh')
    def candidat_nouveau():
        cv = lettre = None
        if request.method == 'POST':
            try:
                cv = _lire_fichier(request.files.get('cv'), 'cv')
                lettre = _lire_fichier(request.files.get('lettre'), 'lettres')
            except ValueError as exc:
                flash(str(exc), 'danger')
                return render_template('recrutement_candidat_form.html')
            nom = (request.form.get('nom') or '').strip()[:100]
            prenom = (request.form.get('prenom') or '').strip()[:100]
            email = (request.form.get('email') or '').strip().lower()[:320]
            if not nom or not prenom or '@' not in email:
                flash("Nom, prénom et email valide sont obligatoires.", 'danger')
            else:
                try:
                    experience_annees = _nombre_positif(
                        request.form.get('experience_annees'), 0
                    )
                except (TypeError, ValueError):
                    flash("Le nombre d'années d'expérience est invalide.", 'danger')
                    return render_template('recrutement_candidat_form.html')
                valeurs = {
                    'nom': nom, 'prenom': prenom, 'email': email,
                    'telephone': (request.form.get('telephone') or '').strip()[:30] or None,
                    'adresse': (request.form.get('adresse') or '').strip() or None,
                    'date_naissance': request.form.get('date_naissance') or None,
                    'diplome': (request.form.get('diplome') or '').strip()[:200] or None,
                    'experience': (request.form.get('experience') or '').strip() or None,
                    'experience_annees': experience_annees,
                    'competences': '\n'.join(lignes_texte(request.form.get('competences'))),
                }
                valeurs.update(_colonnes_fichier('cv', cv)); valeurs.update(_colonnes_fichier('lettre', lettre))
                colonnes = list(valeurs)
                try:
                    with db_cursor(commit=True) as (_conn, cur):
                        cur.execute(f"""INSERT INTO recrutement_candidats
                            ({','.join(colonnes)},cree_par) VALUES ({','.join(['%s']*len(colonnes))},%s)
                            RETURNING id""", [valeurs[c] for c in colonnes] + [session.get('user_id')])
                        candidat_id = cur.fetchone()['id']
                except psycopg2.errors.UniqueViolation:
                    for fichier in (cv, lettre):
                        if fichier and fichier['stored'].external:
                            object_storage.delete(fichier['stored'].storage_key)
                    flash("Un candidat avec cet email existe déjà.", 'warning')
                    return render_template('recrutement_candidat_form.html')
                except Exception:
                    for fichier in (cv, lettre):
                        if fichier and fichier['stored'].external:
                            object_storage.delete(fichier['stored'].storage_key)
                    raise
                log_action(session.get('user_id'), session.get('username'),
                           'CREATE_CANDIDATE', 'recrutement_candidat', candidat_id,
                           f'{prenom} {nom}')
                flash("Candidat créé. Il reste indépendant des employés.", 'success')
                return redirect(url_for('.candidat_detail', candidat_id=candidat_id))
        return render_template('recrutement_candidat_form.html')

    @bp.route('/recrutement/candidats/<int:candidat_id>')
    @login_required
    @role_required('rh')
    def candidat_detail(candidat_id):
        with db_cursor() as (_conn, cur):
            cur.execute("SELECT * FROM recrutement_candidats WHERE id=%s", (candidat_id,))
            candidat = cur.fetchone()
            if not candidat: abort(404)
            cur.execute("""SELECT ca.*,o.titre,o.reference AS offre_reference
                           FROM recrutement_candidatures ca JOIN recrutement_offres o ON o.id=ca.offre_id
                           WHERE ca.candidat_id=%s ORDER BY ca.date_candidature DESC""",
                        (candidat_id,))
            candidatures = cur.fetchall()
            cur.execute("""SELECT o.id,o.reference,o.titre FROM recrutement_offres o
                           WHERE o.statut IN ('brouillon','publiee')
                           AND NOT EXISTS (SELECT 1 FROM recrutement_candidatures ca
                                           WHERE ca.offre_id=o.id AND ca.candidat_id=%s)
                           ORDER BY o.date_creation DESC""", (candidat_id,))
            offres_disponibles = cur.fetchall()
        return render_template('recrutement_candidat_detail.html', candidat=candidat,
                               candidatures=candidatures, offres=offres_disponibles,
                               statuts=CANDIDATURE_STATUTS)

    @bp.route('/recrutement/candidats/<int:candidat_id>/fichier/<type_fichier>')
    @login_required
    @role_required('rh')
    def candidat_fichier(candidat_id, type_fichier):
        if type_fichier not in ('cv', 'lettre'): abort(404)
        with db_cursor() as (_conn, cur):
            cur.execute(f"""SELECT {type_fichier}_nom AS nom,
                {type_fichier}_contenu AS contenu,{type_fichier}_storage_key AS storage_key
                FROM recrutement_candidats WHERE id=%s""", (candidat_id,))
            fichier = cur.fetchone()
        if not fichier or (fichier['contenu'] is None and not fichier['storage_key']):
            abort(404)
        try:
            return object_storage.download_response(
                content=fichier['contenu'], storage_key=fichier['storage_key'],
                filename=fichier['nom'] or type_fichier,
            )
        except ObjectStorageError:
            abort(503)

    @bp.route('/recrutement/candidats/<int:candidat_id>/candidature', methods=['POST'])
    @login_required
    @role_required('rh')
    def candidature_nouvelle(candidat_id):
        offre_id = request.form.get('offre_id', type=int)
        if not offre_id: abort(400)
        try:
            with db_cursor(commit=True) as (_conn, cur):
                cur.execute("""INSERT INTO recrutement_candidatures(candidat_id,offre_id,notes)
                               VALUES (%s,%s,%s) RETURNING id""",
                            (candidat_id, offre_id, (request.form.get('notes') or '').strip() or None))
                candidature_id = cur.fetchone()['id']
        except psycopg2.errors.UniqueViolation:
            flash("Ce candidat a déjà postulé à cette offre.", 'warning')
            return redirect(url_for('.candidat_detail', candidat_id=candidat_id))
        flash("Candidature enregistrée.", 'success')
        return redirect(url_for('.candidature_detail', candidature_id=candidature_id))

    @bp.route('/recrutement/candidatures/<int:candidature_id>')
    @login_required
    @role_required('rh')
    def candidature_detail(candidature_id):
        with db_cursor() as (_conn, cur):
            candidature = _charger_candidature(cur, candidature_id)
            cur.execute("""SELECT rc.*,re.note,re.commentaire FROM recrutement_criteres rc
                           LEFT JOIN recrutement_evaluations re
                             ON re.critere_id=rc.id AND re.candidature_id=%s
                           WHERE rc.offre_id=%s ORDER BY rc.ordre,rc.id""",
                        (candidature_id, candidature['offre_id']))
            criteres = cur.fetchall()
            cur.execute("""SELECT e.*,u.username AS evaluateur FROM recrutement_entretiens e
                           LEFT JOIN users u ON u.id=e.evaluateur_user_id
                           WHERE e.candidature_id=%s ORDER BY e.date_entretien,e.heure_entretien""",
                        (candidature_id,))
            entretiens = cur.fetchall()
        return render_template('recrutement_candidature_detail.html', candidature=candidature,
                               criteres=criteres, entretiens=entretiens,
                               statuts=CANDIDATURE_STATUTS,
                               entretien_statuts=ENTRETIEN_STATUTS,
                               recommandation=recommandation)

    @bp.route('/recrutement/candidatures/<int:candidature_id>/statut', methods=['POST'])
    @login_required
    @role_required('rh')
    def candidature_statut(candidature_id):
        statut = (request.form.get('statut') or '').strip()
        if statut not in CANDIDATURE_STATUTS or statut == 'embauchee': abort(400)
        with db_cursor(commit=True) as (_conn, cur):
            candidature = _charger_candidature(cur, candidature_id)
            if candidature['statut'] == 'embauchee': abort(409)
            cur.execute("""UPDATE recrutement_candidatures SET statut=%s,decide_par=%s,
                           date_decision=CASE WHEN %s IN ('acceptee','refusee') THEN CURRENT_TIMESTAMP ELSE date_decision END,
                           date_modification=CURRENT_TIMESTAMP WHERE id=%s""",
                        (statut, session.get('user_id'), statut, candidature_id))
            if statut in ('acceptee', 'refusee'):
                queue_email(candidature['email'], f"Candidature — {candidature['offre_titre']}",
                            f"Votre candidature est désormais : {CANDIDATURE_STATUTS[statut]}.",
                            cur=cur, event_key=f"candidature:{candidature_id}:{statut}")
        flash("Statut de candidature mis à jour par décision humaine.", 'success')
        return redirect(url_for('.candidature_detail', candidature_id=candidature_id))

    @bp.route('/recrutement/candidatures/<int:candidature_id>/evaluation', methods=['GET', 'POST'])
    @login_required
    @role_required('rh')
    def candidature_evaluation(candidature_id):
        with db_cursor(commit=request.method == 'POST') as (_conn, cur):
            candidature = _charger_candidature(cur, candidature_id)
            cur.execute("""SELECT rc.*,re.note,re.commentaire
                           FROM recrutement_criteres rc
                           LEFT JOIN recrutement_evaluations re
                             ON re.critere_id=rc.id AND re.candidature_id=%s
                           WHERE rc.offre_id=%s ORDER BY rc.ordre,rc.id""",
                        (candidature_id, candidature['offre_id']))
            criteres = cur.fetchall()
            if request.method == 'POST':
                notes = {}
                try:
                    for critere in criteres:
                        note = float(request.form.get(f"note_{critere['id']}", ''))
                        if not 0 <= note <= 100: raise ValueError
                        notes[critere['id']] = note
                except (TypeError, ValueError):
                    flash("Toutes les notes doivent être comprises entre 0 et 100.", 'danger')
                else:
                    for critere in criteres:
                        cur.execute("""INSERT INTO recrutement_evaluations
                            (candidature_id,critere_id,note,commentaire,evaluateur_user_id)
                            VALUES (%s,%s,%s,%s,%s)
                            ON CONFLICT(candidature_id,critere_id) DO UPDATE SET
                              note=EXCLUDED.note,commentaire=EXCLUDED.commentaire,
                              evaluateur_user_id=EXCLUDED.evaluateur_user_id,
                              date_evaluation=CURRENT_TIMESTAMP""",
                            (candidature_id, critere['id'], notes[critere['id']],
                             (request.form.get(f"commentaire_{critere['id']}") or '').strip() or None,
                             session.get('user_id')))
                    scores = _recalculer_scores(cur, candidature_id)
                    cur.execute("""UPDATE recrutement_candidatures SET statut=CASE
                                   WHEN statut IN ('recue','preselectionnee') THEN 'evaluation' ELSE statut END
                                   WHERE id=%s""", (candidature_id,))
                    flash(f"Évaluation enregistrée — score dossier : {scores[0]:.2f}/100.", 'success')
                    return redirect(url_for('.candidature_detail', candidature_id=candidature_id))
        return render_template('recrutement_evaluation_form.html', candidature=candidature,
                               criteres=criteres)

    @bp.route('/recrutement/candidatures/<int:candidature_id>/entretiens/nouveau', methods=['GET', 'POST'])
    @login_required
    @role_required('rh')
    def entretien_nouveau(candidature_id):
        with db_cursor(commit=request.method == 'POST') as (_conn, cur):
            candidature = _charger_candidature(cur, candidature_id)
            cur.execute("""SELECT u.id,u.username FROM users u
                           WHERE u.actif AND u.role IN ('admin','rh','manager','technicien')
                           ORDER BY u.username""")
            evaluateurs = cur.fetchall()
            if request.method == 'POST':
                type_entretien = (request.form.get('type_entretien') or '').strip()
                date_entretien = request.form.get('date_entretien')
                heure = request.form.get('heure_entretien')
                if type_entretien not in ('presentiel','visio','telephone') or not date_entretien or not heure:
                    flash("Date, heure et type d'entretien sont obligatoires.", 'danger')
                else:
                    cur.execute("""INSERT INTO recrutement_entretiens
                        (candidature_id,date_entretien,heure_entretien,type_entretien,
                         lieu_ou_lien,evaluateur_user_id)
                        VALUES (%s,%s,%s,%s,%s,%s) RETURNING id""",
                        (candidature_id, date_entretien, heure, type_entretien,
                         (request.form.get('lieu_ou_lien') or '').strip()[:300] or None,
                         request.form.get('evaluateur_user_id', type=int)))
                    entretien_id = cur.fetchone()['id']
                    cur.execute("""UPDATE recrutement_candidatures SET statut='entretien',
                                   date_modification=CURRENT_TIMESTAMP
                                   WHERE id=%s AND statut NOT IN ('acceptee','refusee','embauchee')""",
                                (candidature_id,))
                    queue_email(candidature['email'], f"Entretien — {candidature['offre_titre']}",
                                f"Entretien planifié le {date_entretien} à {heure} ({type_entretien}).",
                                cur=cur, event_key=f"entretien:{entretien_id}:planifie")
                    flash("Entretien planifié et candidat informé.", 'success')
                    return redirect(url_for('.candidature_detail', candidature_id=candidature_id))
        return render_template('recrutement_entretien_form.html', candidature=candidature,
                               evaluateurs=evaluateurs, entretien=None,
                               criteres=CRITERES_ENTRETIEN)

    @bp.route('/recrutement/entretiens/<int:entretien_id>/evaluer', methods=['GET', 'POST'])
    @login_required
    @role_required('rh')
    def entretien_evaluer(entretien_id):
        with db_cursor(commit=request.method == 'POST') as (_conn, cur):
            cur.execute("""SELECT e.*,ca.offre_id,c.nom,c.prenom,o.titre AS offre_titre
                           FROM recrutement_entretiens e
                           JOIN recrutement_candidatures ca ON ca.id=e.candidature_id
                           JOIN recrutement_candidats c ON c.id=ca.candidat_id
                           JOIN recrutement_offres o ON o.id=ca.offre_id WHERE e.id=%s""",
                        (entretien_id,))
            entretien = cur.fetchone()
            if not entretien: abort(404)
            if request.method == 'POST':
                notes = []
                try:
                    for critere in CRITERES_ENTRETIEN:
                        note = float(request.form.get(f"note_{critere}", ''))
                        if not 0 <= note <= 100: raise ValueError
                        notes.append((critere, note))
                except (TypeError, ValueError):
                    flash("Chaque note d'entretien doit être comprise entre 0 et 100.", 'danger')
                else:
                    score = round(sum(n for _, n in notes) / len(notes), 2)
                    cur.execute("DELETE FROM recrutement_entretien_notes WHERE entretien_id=%s",
                                (entretien_id,))
                    for critere, note in notes:
                        cur.execute("""INSERT INTO recrutement_entretien_notes
                                       (entretien_id,critere,note) VALUES (%s,%s,%s)""",
                                    (entretien_id, critere, note))
                    cur.execute("""UPDATE recrutement_entretiens SET statut='realise',score=%s,
                                   notes=%s,date_modification=CURRENT_TIMESTAMP WHERE id=%s""",
                                (score, (request.form.get('notes') or '').strip() or None,
                                 entretien_id))
                    _recalculer_scores(cur, entretien['candidature_id'])
                    flash(f"Entretien évalué : {score:.2f}/100.", 'success')
                    return redirect(url_for('.candidature_detail',
                                            candidature_id=entretien['candidature_id']))
            cur.execute("SELECT critere,note FROM recrutement_entretien_notes WHERE entretien_id=%s",
                        (entretien_id,))
            notes_existantes = {r['critere']: r['note'] for r in cur.fetchall()}
        return render_template('recrutement_entretien_form.html', entretien=entretien,
                               candidature=entretien, evaluateurs=[],
                               criteres=CRITERES_ENTRETIEN,
                               notes_existantes=notes_existantes)

    @bp.route('/recrutement/offres/<int:offre_id>/comparer')
    @login_required
    @role_required('rh')
    def comparer(offre_id):
        with db_cursor() as (_conn, cur):
            cur.execute("SELECT * FROM recrutement_offres WHERE id=%s", (offre_id,))
            offre = cur.fetchone()
            if not offre: abort(404)
            cur.execute("SELECT * FROM recrutement_criteres WHERE offre_id=%s ORDER BY ordre,id",
                        (offre_id,))
            criteres = cur.fetchall()
            cur.execute("""SELECT ca.id,ca.statut,ca.score_dossier,ca.score_entretien,
                                  ca.score_global,c.nom,c.prenom,c.experience_annees,c.diplome
                           FROM recrutement_candidatures ca
                           JOIN recrutement_candidats c ON c.id=ca.candidat_id
                           WHERE ca.offre_id=%s ORDER BY ca.score_global DESC NULLS LAST""",
                        (offre_id,))
            candidats = cur.fetchall()
            for candidat in candidats:
                cur.execute("""SELECT critere_id,note FROM recrutement_evaluations
                               WHERE candidature_id=%s""", (candidat['id'],))
                candidat['notes'] = {r['critere_id']: r['note'] for r in cur.fetchall()}
        return render_template('recrutement_comparaison.html', offre=offre,
                               criteres=criteres, candidats=candidats,
                               recommandation=recommandation)

    @bp.route('/recrutement/candidatures/<int:candidature_id>/embaucher', methods=['GET', 'POST'])
    @login_required
    @role_required('rh')
    def embaucher(candidature_id):
        with db_cursor(commit=request.method == 'POST') as (_conn, cur):
            candidature = _charger_candidature(cur, candidature_id, lock=request.method == 'POST')
            cur.execute("SELECT id,nom FROM departements ORDER BY nom")
            departements = cur.fetchall()
            if request.method == 'POST':
                if candidature['statut'] != 'acceptee' or candidature.get('employe_id'):
                    abort(409)
                poste = (request.form.get('poste') or candidature['poste']).strip()[:150]
                departement_id = request.form.get('departement_id', type=int) or candidature['departement_id']
                date_embauche = request.form.get('date_embauche')
                try:
                    salaire = _nombre_positif(request.form.get('salaire'))
                except (TypeError, ValueError):
                    flash("Le salaire est invalide.", 'danger')
                    return render_template('recrutement_embauche_form.html',
                                           candidature=candidature,
                                           departements=departements,
                                           types_contrats=TYPE_CONTRATS,
                                           today=date.today())
                cur.execute("SELECT nom FROM departements WHERE id=%s", (departement_id,))
                dept = cur.fetchone()
                if not poste or not date_embauche or not dept:
                    flash("Poste, département et date d'embauche sont obligatoires.", 'danger')
                else:
                    cur.execute("""INSERT INTO employes
                        (nom,prenom,poste,departement,email,telephone,date_embauche,salaire,actif)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,TRUE) RETURNING id""",
                        (candidature['nom'], candidature['prenom'], poste, dept['nom'],
                         candidature['email'], candidature['telephone'], date_embauche, salaire))
                    employe_id = cur.fetchone()['id']
                    type_contrat = (request.form.get('type_contrat') or '').strip()
                    if type_contrat:
                        if type_contrat not in TYPE_CONTRATS:
                            abort(400)
                        cur.execute("""INSERT INTO contrats
                            (employe_id,type_contrat,reference,date_debut,date_fin,notes,cree_par)
                            VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                            (employe_id, type_contrat,
                             (request.form.get('reference_contrat') or '').strip()[:80] or None,
                             request.form.get('date_debut_contrat') or date_embauche,
                             request.form.get('date_fin_contrat') or None,
                             f"Créé depuis la candidature #{candidature_id}",
                             session.get('user_id')))
                    cur.execute("UPDATE recrutement_candidats SET employe_id=%s,date_modification=CURRENT_TIMESTAMP WHERE id=%s",
                                (employe_id, candidature['candidat_id']))
                    cur.execute("""UPDATE recrutement_candidatures SET statut='embauchee',
                                   date_embauche=CURRENT_TIMESTAMP,date_modification=CURRENT_TIMESTAMP
                                   WHERE id=%s""", (candidature_id,))
                    cur.execute("""SELECT COUNT(*) AS nb FROM recrutement_candidatures
                                   WHERE offre_id=%s AND statut='embauchee'""",
                                (candidature['offre_id'],))
                    if cur.fetchone()['nb'] >= candidature['nombre_postes']:
                        cur.execute("UPDATE recrutement_offres SET statut='pourvue',date_modification=CURRENT_TIMESTAMP WHERE id=%s",
                                    (candidature['offre_id'],))
                    log_action(session.get('user_id'), session.get('username'),
                               'HIRE_CANDIDATE', 'employe', employe_id,
                               f"candidature={candidature_id}")
                    flash("Candidat converti en employé. Le compte utilisateur reste optionnel.", 'success')
                    return redirect(url_for('view_employee', id=employe_id))
        return render_template('recrutement_embauche_form.html', candidature=candidature,
                               departements=departements, types_contrats=TYPE_CONTRATS,
                               today=date.today())

    return bp
