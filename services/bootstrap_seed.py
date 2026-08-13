"""Données initiales de développement et premier administrateur de production."""

import os

from werkzeug.security import generate_password_hash


def appliquer_seed_initial(cur, conn):
    """Crée les démos hors production ou un admin secret sur base prod vide."""
    seed_demo = os.environ.get(
        "SEED_DEMO_DATA",
        "false" if os.environ.get("FLASK_ENV") == "production" else "true",
    ).lower() == "true"

    cur.execute("SELECT COUNT(*) FROM employes")
    if cur.fetchone()["count"] == 0:
        if seed_demo:
            cur.executemany(
                """INSERT INTO employes
                    (nom,prenom,poste,departement,email,telephone,date_embauche,salaire)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""",
                [
                    ("Dupont", "Jean", "Développeur", "Informatique",
                     "jean.dupont@entreprise.fr", "0612345678", "2023-01-15", 52000),
                    ("Martin", "Sophie", "Responsable RH", "Ressources Humaines",
                     "sophie.martin@entreprise.fr", "0698765432", "2022-06-01", 58000),
                    ("Bernard", "Pierre", "Chef de projet", "Informatique",
                     "pierre.bernard@entreprise.fr", "0678912345", "2021-09-10", 61000),
                    ("Administrateur", "Système", "Administrateur Système",
                     "Administration", "admin@entreprise.fr", "0600000001",
                     "2022-01-01", 72000),
                ],
            )
        else:
            cur.execute(
                """INSERT INTO employes
                    (nom,prenom,poste,departement,email,date_embauche,salaire)
                    VALUES ('Administrateur','Production','Administrateur Système',
                            'Administration',%s,CURRENT_DATE,0)""",
                (os.environ.get("BOOTSTRAP_ADMIN_EMAIL", "admin@localhost"),),
            )

    cur.execute("SELECT COUNT(*) FROM users")
    if cur.fetchone()["count"] != 0:
        return
    if seed_demo:
        for username, password, role, employe_id in (
            ("admin", "admin123", "admin", 4),
            ("rh", "rh123", "rh", 2),
            ("manager", "manager123", "manager", 3),
            ("employe", "user123", "employe", 1),
        ):
            cur.execute(
                """INSERT INTO users(username,password_hash,role,employe_id)
                   VALUES (%s,%s,%s,%s)""",
                (username, generate_password_hash(password), role, employe_id),
            )
        return

    password = os.environ.get("BOOTSTRAP_ADMIN_PASSWORD", "")
    if len(password) < 12:
        conn.rollback()
        cur.close()
        conn.close()
        raise RuntimeError(
            "BOOTSTRAP_ADMIN_PASSWORD (12 caractères minimum) est obligatoire "
            "pour initialiser une base de production vide."
        )
    cur.execute(
        """SELECT id FROM employes WHERE poste='Administrateur Système'
           ORDER BY id LIMIT 1"""
    )
    admin_employee = cur.fetchone()
    if not admin_employee:
        cur.execute(
            """INSERT INTO employes
                (nom,prenom,poste,departement,email,date_embauche,salaire)
                VALUES ('Administrateur','Production','Administrateur Système',
                        'Administration',%s,CURRENT_DATE,0) RETURNING id""",
            (os.environ.get("BOOTSTRAP_ADMIN_EMAIL", "admin@localhost"),),
        )
        admin_employee = cur.fetchone()
    cur.execute(
        """INSERT INTO users(username,password_hash,role,employe_id,actif)
           VALUES (%s,%s,'admin',%s,TRUE)""",
        (
            os.environ.get("BOOTSTRAP_ADMIN_USERNAME", "admin"),
            generate_password_hash(password),
            admin_employee["id"],
        ),
    )
