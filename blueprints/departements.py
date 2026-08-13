"""Consultation et administration des départements."""

from flask import Blueprint, flash, redirect, render_template, request, url_for


def creer_blueprint_departements(deps):
    bp = Blueprint('departements', __name__)
    get_db = deps['get_db']
    get_cursor = deps['get_cursor']
    login_required = deps['login_required']
    role_required = deps['role_required']
    department_scope_sql = deps['department_scope_sql']

    @bp.route('/departements')
    @login_required
    def departements():
        conn = get_db()
        cur = get_cursor(conn)
        dept_scope, dept_params = department_scope_sql('d', 'nom', cur)
        emp_scope, emp_params = department_scope_sql('e', 'departement', cur)

        # Get departments with employee count
        cur.execute(f"""
            SELECT
                d.id,
                d.nom,
                COALESCE(d.description, '') as description,
                COALESCE(d.responsable, '') as responsable,
                COUNT(e.id) as nb_employes
            FROM departements d
            LEFT JOIN employes e ON e.departement = d.nom
            WHERE {dept_scope}
            GROUP BY d.id, d.nom, d.description, d.responsable
            ORDER BY d.nom
        """, dept_params)
        departements = cur.fetchall()

        # Get totals dans la même portée
        cur.execute(f"SELECT COUNT(*) as total FROM departements d WHERE {dept_scope}",
                    dept_params)
        total_depts = cur.fetchone()['total'] or 0

        cur.execute(f"SELECT COUNT(*) as total FROM employes e WHERE {emp_scope}", emp_params)
        total_employes = cur.fetchone()['total'] or 0

        cur.close()
        conn.close()

        return render_template('departements.html',
                              departements=departements,
                              total_depts=total_depts,
                              total_employes=total_employes)


    @bp.route('/departements/add', methods=['GET', 'POST'])
    @login_required
    @role_required('admin')
    def add_departement():
        if request.method == 'POST':
            nom = request.form.get('nom', '').strip()
            description = request.form.get('description', '').strip()
            responsable = request.form.get('responsable', '').strip()

            if not nom:
                flash("Le nom du département est obligatoire", "danger")
            else:
                conn = get_db()
                cur = get_cursor(conn)
                try:
                    cur.execute("""
                        INSERT INTO departements (nom, description, responsable)
                        VALUES (%s, %s, %s)
                    """, (nom, description or None, responsable or None))
                    conn.commit()
                    flash(f"Département '{nom}' créé avec succès", "success")
                    cur.close()
                    conn.close()
                    return redirect(url_for('departements.departements'))
                except Exception as e:
                    conn.rollback()
                    if "unique" in str(e).lower():
                        flash("Ce nom de département existe déjà", "danger")
                    else:
                        flash(f"Erreur : {str(e)}", "danger")
                    cur.close()
                    conn.close()

        return render_template('dept_form.html', dept=None, title="Nouveau département")

    @bp.route('/departements/edit/<int:id>', methods=['GET', 'POST'])
    @login_required
    @role_required('admin')
    def edit_departement(id):
        conn = get_db()
        cur = get_cursor(conn)

        if request.method == 'POST':
            nom = request.form.get('nom', '').strip()
            description = request.form.get('description', '').strip()
            responsable = request.form.get('responsable', '').strip()

            if not nom:
                flash("Le nom du département est obligatoire", "danger")
            else:
                try:
                    cur.execute("""
                        UPDATE departements
                        SET nom=%s, description=%s, responsable=%s
                        WHERE id=%s
                    """, (nom, description or None, responsable or None, id))
                    conn.commit()
                    flash("Département mis à jour", "success")
                    cur.close()
                    conn.close()
                    return redirect(url_for('departements.departements'))
                except Exception as e:
                    conn.rollback()
                    flash(f"Erreur : {str(e)}", "danger")

        # GET: load current department
        cur.execute("SELECT * FROM departements WHERE id = %s", (id,))
        dept = cur.fetchone()
        cur.close()
        conn.close()

        if not dept:
            flash("Département introuvable", "danger")
            return redirect(url_for('departements.departements'))

        return render_template('dept_form.html', dept=dept, title="Modifier le département")

    @bp.route('/departements/delete/<int:id>', methods=['POST'])
    @login_required
    @role_required('admin')
    def delete_departement(id):
        conn = get_db()
        cur = get_cursor(conn)
        try:
            cur.execute("DELETE FROM departements WHERE id = %s", (id,))
            conn.commit()
            flash("Département supprimé", "success")
        except Exception as e:
            conn.rollback()
            flash(f"Erreur lors de la suppression : {str(e)}", "danger")
        cur.close()
        conn.close()
        return redirect(url_for('departements.departements'))

    return bp
