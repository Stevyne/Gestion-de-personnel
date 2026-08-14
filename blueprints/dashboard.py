"""Tableau de bord général, global ou cloisonné par département."""

from datetime import date

from flask import Blueprint, render_template


def creer_blueprint_dashboard(deps):
    bp = Blueprint('dashboard', __name__)
    db_cursor = deps['db_cursor']
    login_required = deps['login_required']
    get_department_scope = deps['get_department_scope']
    department_scope_sql = deps['department_scope_sql']
    calculer_retard = deps['calculer_retard']

    @bp.route('/')
    @login_required
    def dashboard():
        """Tableau de bord global pour admin/RH, départemental pour les autres.

        Le cloisonnement est appliqué dans CHAQUE requête SQL. Un compte non
        privilégié sans fiche employé ou sans département voit volontairement des
        compteurs vides : il ne doit jamais basculer implicitement sur la vue
        globale.
        """
        with db_cursor() as (conn, cur):
            data_scope = get_department_scope(cur)
            vue_globale = data_scope['is_global']
            departement = data_scope.get('department')
            scope_vide = data_scope['is_empty']
            dashboard_scope = {
                **data_scope,
                'label': "Toute l'entreprise" if vue_globale else
                         (departement or 'Aucun département rattaché'),
            }

            def scope_employe(alias='e'):
                return department_scope_sql(alias, 'departement', cur)

            def scope_departement(alias='d'):
                return department_scope_sql(alias, 'nom', cur)

            today = date.today()
            annee = today.year
            emp_where, emp_params = scope_employe('e')

            # === Personnel ======================================================
            cur.execute(f"""
                SELECT COUNT(*) AS total,
                       COALESCE(AVG(e.salaire), 0) AS salaire_moyen,
                       COALESCE(AVG(CURRENT_DATE - e.date_embauche), 0) AS anciennete_jours
                  FROM employes e WHERE {emp_where} AND e.actif
            """, emp_params)
            personnel = cur.fetchone()
            total_employes = personnel['total'] or 0
            salaire_moyen = personnel['salaire_moyen'] or 0
            anciennete_moyenne = round(float(personnel['anciennete_jours'] or 0) / 365.25, 1)

            if vue_globale:
                cur.execute("""
                    SELECT COUNT(*) AS total FROM (
                        SELECT nom FROM departements WHERE nom IS NOT NULL
                        UNION
                        SELECT departement FROM employes
                         WHERE departement IS NOT NULL AND departement <> ''
                    ) x
                """)
                total_departements = cur.fetchone()['total'] or 0
            else:
                total_departements = 0 if scope_vide else 1

            # === Présences et temps ============================================
            cur.execute(f"""
                SELECT p.*, e.nom, e.prenom
                  FROM presences p JOIN employes e ON e.id = p.employe_id
                 WHERE p.date = %s AND {emp_where} AND e.actif
            """, [today] + emp_params)
            presences_today = cur.fetchall()
            teletravail = 0
            presents = 0
            retards_aujourdhui = []
            total_retards_minutes = 0
            for presence in presences_today:
                statut = (presence.get('statut') or 'présent').lower()
                if statut == 'télétravail':
                    teletravail += 1
                elif statut != 'absent':
                    presents += 1
                retard = calculer_retard(presence.get('heure_arrivee'))
                if retard > 0 and statut != 'télétravail':
                    presence['retard_minutes'] = retard
                    retards_aujourdhui.append(presence)
                    total_retards_minutes += retard

            total_presences_aujourdhui = presents + teletravail
            presences_stat = {
                'present': presents,
                'absent': max(0, total_employes - presents - teletravail),
                'teletravail': teletravail,
            }
            taux_presence = round(
                total_presences_aujourdhui / total_employes * 100, 1
            ) if total_employes else 0

            cur.execute(f"""
                SELECT COALESCE(SUM(EXTRACT(EPOCH FROM
                           (p.heure_depart - p.heure_arrivee)) / 3600), 0) AS heures
                  FROM presences p JOIN employes e ON e.id = p.employe_id
                 WHERE p.heure_arrivee IS NOT NULL AND p.heure_depart IS NOT NULL
                   AND DATE_TRUNC('month', p.date) = DATE_TRUNC('month', CURRENT_DATE)
                   AND {emp_where} AND e.actif
            """, emp_params)
            heures_totales = round(float(cur.fetchone()['heures'] or 0), 1)

            # === Congés, permissions et soldes ================================
            cur.execute(f"""
                SELECT COUNT(*) FILTER (WHERE c.statut IN ('en attente','en_attente','avis rendu')) AS en_attente,
                       COUNT(*) FILTER (WHERE c.statut = 'approuvé'
                             AND EXTRACT(YEAR FROM c.date_debut) = %s) AS approuve,
                       COUNT(*) FILTER (WHERE c.statut = 'refusé'
                             AND EXTRACT(YEAR FROM c.date_debut) = %s) AS refuse,
                       COALESCE(SUM(c.nombre_jours) FILTER (WHERE c.statut = 'approuvé'
                             AND EXTRACT(YEAR FROM c.date_debut) = %s), 0) AS jours_approuves
                  FROM conges c JOIN employes e ON e.id = c.employe_id
                 WHERE {emp_where} AND e.actif
            """, [annee, annee, annee] + emp_params)
            conges_row = cur.fetchone()
            conges_stat = {
                'en_attente': conges_row['en_attente'] or 0,
                'approuve': conges_row['approuve'] or 0,
                'refuse': conges_row['refuse'] or 0,
                'jours_approuves': float(conges_row['jours_approuves'] or 0),
            }

            cur.execute(f"""
                SELECT COUNT(*) FILTER (WHERE p.statut IN ('en attente','en_attente','avis rendu')) AS en_attente,
                       COUNT(*) FILTER (WHERE p.statut = 'approuvé'
                             AND EXTRACT(YEAR FROM p.date_debut) = %s) AS approuve,
                       COUNT(*) FILTER (WHERE p.statut = 'refusé'
                             AND EXTRACT(YEAR FROM p.date_debut) = %s) AS refuse
                  FROM permissions p JOIN employes e ON e.id = p.employe_id
                 WHERE {emp_where} AND e.actif
            """, [annee, annee] + emp_params)
            permission_row = cur.fetchone()
            permissions_stat = {
                'en_attente': permission_row['en_attente'] or 0,
                'approuve': permission_row['approuve'] or 0,
                'refuse': permission_row['refuse'] or 0,
            }

            cur.execute(f"""
                SELECT COALESCE(SUM(s.jours_acquis - s.jours_utilises), 0) AS restant
                  FROM soldes_conges s JOIN employes e ON e.id = s.employe_id
                 WHERE s.annee = %s AND {emp_where} AND e.actif
            """, [annee] + emp_params)
            solde_conges_restant = round(float(cur.fetchone()['restant'] or 0), 1)

            # === Absences et justificatifs =====================================
            cur.execute(f"""
                SELECT COUNT(*) FILTER (WHERE EXTRACT(YEAR FROM a.date) = %s) AS total,
                       COUNT(*) FILTER (WHERE EXTRACT(YEAR FROM a.date) = %s
                           AND COALESCE(a.statut,'non_justifiee') IN ('non_justifiee','refusee')) AS a_regulariser,
                       COUNT(*) FILTER (WHERE a.statut = 'justificatif_depose') AS justificatifs_attente,
                       COUNT(*) FILTER (WHERE EXTRACT(YEAR FROM a.date) = %s
                           AND a.statut = 'acceptee') AS acceptees
                  FROM absences a JOIN employes e ON e.id = a.employe_id
                 WHERE {emp_where} AND e.actif
            """, [annee, annee, annee] + emp_params)
            absence_row = cur.fetchone()
            absences_stat = {
                'total': absence_row['total'] or 0,
                'a_regulariser': absence_row['a_regulariser'] or 0,
                'justificatifs_attente': absence_row['justificatifs_attente'] or 0,
                'acceptees': absence_row['acceptees'] or 0,
            }

            # === Documents =====================================================
            cur.execute(f"""
                SELECT COUNT(*) AS total,
                       COUNT(*) FILTER (WHERE d.date_expiration < CURRENT_DATE) AS expires,
                       COUNT(*) FILTER (WHERE d.date_expiration BETWEEN CURRENT_DATE
                                          AND CURRENT_DATE + INTERVAL '30 days') AS expirent_bientot
                  FROM documents d LEFT JOIN employes e ON e.id = d.employe_id
                 WHERE {emp_where} AND e.actif
            """, emp_params)
            document_row = cur.fetchone()
            documents_stat = {
                'total': document_row['total'] or 0,
                'expires': document_row['expires'] or 0,
                'expirent_bientot': document_row['expirent_bientot'] or 0,
            }

            # === Matériels, parc et attributions ===============================
            dept_where, dept_params = scope_departement('d')
            cur.execute(f"""
                SELECT COUNT(*) AS articles,
                       COALESCE(SUM(m.quantite), 0) AS stock_total,
                       COUNT(*) FILTER (WHERE m.seuil_alerte > 0
                                          AND m.quantite <= m.seuil_alerte) AS alertes_stock,
                       COALESCE(SUM(
                         CASE WHEN COALESCE(m.suivi_unitaire, FALSE) THEN
                           COALESCE((SELECT SUM(COALESCE(ex.prix_acquisition,
                                                        m.prix_acquisition, 0))
                                       FROM materiel_exemplaires ex
                                      WHERE ex.materiel_id = m.id AND ex.etat <> 'rebut'), 0)
                         ELSE COALESCE(m.prix_acquisition, 0) * m.quantite END
                       ), 0) AS valeur_parc
                  FROM materiels m LEFT JOIN departements d ON d.id = m.departement_id
                 WHERE {dept_where}
            """, dept_params)
            materiel_row = cur.fetchone()
            cur.execute(f"""
                SELECT COUNT(*) AS total
                  FROM materiels_attributions a
                  JOIN materiels m ON m.id = a.materiel_id
                  LEFT JOIN departements d ON d.id = m.departement_id
                 WHERE a.date_retour IS NULL AND {dept_where}
            """, dept_params)
            attributions_actives = cur.fetchone()['total'] or 0
            materiels_stat = {
                'articles': materiel_row['articles'] or 0,
                'stock_total': materiel_row['stock_total'] or 0,
                'alertes_stock': materiel_row['alertes_stock'] or 0,
                'valeur_parc': float(materiel_row['valeur_parc'] or 0),
                'attributions_actives': attributions_actives,
            }

            # === Maintenance ===================================================
            cur.execute(f"""
                SELECT COUNT(*) FILTER (WHERE mt.statut IN ('signale','assigne','envoye','a_valider')) AS ouvertes,
                       COUNT(*) FILTER (WHERE mt.statut = 'a_valider') AS a_valider,
                       COALESCE(SUM(mt.cout) FILTER (
                           WHERE EXTRACT(YEAR FROM mt.date_signalement) = %s), 0) AS cout_annee
                  FROM materiel_maintenances mt
                  JOIN materiel_exemplaires ex ON ex.id = mt.exemplaire_id
                  JOIN materiels m ON m.id = ex.materiel_id
                  LEFT JOIN departements d ON d.id = m.departement_id
                 WHERE {dept_where}
            """, [annee] + dept_params)
            maintenance_row = cur.fetchone()
            maintenances_stat = {
                'ouvertes': maintenance_row['ouvertes'] or 0,
                'a_valider': maintenance_row['a_valider'] or 0,
                'cout_annee': float(maintenance_row['cout_annee'] or 0),
            }

            # === Inventaires ===================================================
            cur.execute(f"""
                SELECT COUNT(DISTINCT i.id) FILTER (WHERE i.statut = 'en_cours') AS en_cours,
                       COUNT(il.id) FILTER (WHERE il.quantite_comptee IS NOT NULL
                           AND il.quantite_comptee <> il.quantite_theorique) AS ecarts
                  FROM inventaires i
                  LEFT JOIN inventaire_lignes il ON il.inventaire_id = i.id
                  LEFT JOIN departements d ON d.id = i.departement_id
                 WHERE {dept_where}
            """, dept_params)
            inventaire_row = cur.fetchone()
            inventaires_stat = {
                'en_cours': inventaire_row['en_cours'] or 0,
                'ecarts': inventaire_row['ecarts'] or 0,
            }

            # === Accès, audit et messagerie (vue globale uniquement) ===========
            systeme_stat = None
            if vue_globale:
                cur.execute("""
                    SELECT (SELECT COUNT(*) FROM users) AS utilisateurs,
                           (SELECT COUNT(*) FROM sessions_actives
                             WHERE revoked_at IS NULL
                               AND last_seen > CURRENT_TIMESTAMP - INTERVAL '1 hour') AS sessions_actives,
                           (SELECT COUNT(*) FROM audit_logs
                             WHERE timestamp::date = CURRENT_DATE) AS actions_audit,
                           (SELECT COUNT(*) FROM notifications
                             WHERE is_read = FALSE) AS notifications_non_lues,
                           (SELECT COUNT(*) FROM email_outbox
                             WHERE statut = 'en_attente') AS emails_attente,
                           (SELECT COUNT(*) FROM email_outbox
                             WHERE statut = 'echec') AS emails_echec
                """)
                systeme_stat = cur.fetchone()

            # === Répartition et activité récente ===============================
            cur.execute(f"""
                SELECT COALESCE(NULLIF(e.departement,''), 'Sans département') AS nom,
                       COUNT(*) AS nb_employes
                  FROM employes e WHERE {emp_where} AND e.actif
                 GROUP BY COALESCE(NULLIF(e.departement,''), 'Sans département')
                 ORDER BY nb_employes DESC LIMIT 8
            """, emp_params)
            dept_stats = []
            for row in cur.fetchall():
                pct = round(row['nb_employes'] / total_employes * 100, 1) if total_employes else 0
                dept_stats.append({'nom': row['nom'], 'nb_employes': row['nb_employes'],
                                   'pourcentage': pct})

            cur.execute(f"""
                SELECT p.*, e.nom, e.prenom FROM presences p
                JOIN employes e ON e.id = p.employe_id
                WHERE {emp_where} AND e.actif ORDER BY p.date DESC, p.id DESC LIMIT 5
            """, emp_params)
            recent_presences = cur.fetchall()
            cur.execute(f"""
                SELECT c.*, e.nom, e.prenom FROM conges c
                JOIN employes e ON e.id = c.employe_id
                WHERE {emp_where} AND e.actif ORDER BY c.date_demande DESC, c.id DESC LIMIT 5
            """, emp_params)
            recent_conges = cur.fetchall()

            # === Graphiques ====================================================
            if vue_globale:
                tendance_expr, tendance_params = 'COUNT(p.id)', []
            elif scope_vide:
                tendance_expr, tendance_params = '0', []
            else:
                tendance_expr = 'COUNT(p.id) FILTER (WHERE e.departement = %s)'
                tendance_params = [departement]
            cur.execute(f"""
                SELECT serie::date AS jour, {tendance_expr} AS nb
                  FROM generate_series(CURRENT_DATE - INTERVAL '29 days', CURRENT_DATE,
                                       INTERVAL '1 day') AS serie
                  LEFT JOIN presences p ON p.date = serie::date
                  LEFT JOIN employes e ON e.id = p.employe_id
                 GROUP BY serie ORDER BY serie
            """, tendance_params)
            tendance_rows = cur.fetchall()

        chart_tendance = {
            'labels': [r['jour'].strftime('%d/%m') for r in tendance_rows],
            'valeurs': [r['nb'] for r in tendance_rows],
        }
        chart_presences_jour = {
            'labels': ['Présent', 'Absent', 'Télétravail'],
            'valeurs': [presences_stat['present'], presences_stat['absent'],
                        presences_stat['teletravail']],
        }
        chart_conges = {
            'labels': ['En attente', 'Approuvés', 'Refusés'],
            'valeurs': [conges_stat['en_attente'], conges_stat['approuve'],
                        conges_stat['refuse']],
        }
        chart_departements = {
            'labels': [d['nom'] for d in dept_stats],
            'valeurs': [d['nb_employes'] for d in dept_stats],
        }

        return render_template(
            'dashboard.html', dashboard_scope=dashboard_scope,
            peut_voir_salaires=vue_globale, total_employes=total_employes,
            total_departements=total_departements, salaire_moyen=salaire_moyen,
            anciennete_moyenne=anciennete_moyenne,
            total_presences_aujourdhui=total_presences_aujourdhui,
            today=today, presences_stat=presences_stat, taux_presence=taux_presence,
            retards_aujourdhui=retards_aujourdhui, nb_retards=len(retards_aujourdhui),
            total_retards_minutes=total_retards_minutes, heures_totales=heures_totales,
            conges_stat=conges_stat, permissions_stat=permissions_stat,
            solde_conges_restant=solde_conges_restant, absences_stat=absences_stat,
            documents_stat=documents_stat, materiels_stat=materiels_stat,
            maintenances_stat=maintenances_stat, inventaires_stat=inventaires_stat,
            systeme_stat=systeme_stat,
            dept_stats=dept_stats, recent_presences=recent_presences,
            recent_conges=recent_conges, chart_tendance=chart_tendance,
            chart_presences_jour=chart_presences_jour, chart_conges=chart_conges,
            chart_departements=chart_departements,
            modules_couverts=12 if vue_globale else 9,
        )

    return bp
