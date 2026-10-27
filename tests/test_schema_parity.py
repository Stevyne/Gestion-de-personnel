"""Parité de schéma entre le chemin applicatif et le chemin Alembic.

Deux chemins construisent le schéma de la base :

* ``init_db()`` — développement, tests, et ``flask bootstrap-db`` ;
* ``flask db upgrade`` — migrations Alembic versionnées (production).

En production les deux se cumulent (``preDeployCommand: flask bootstrap-db &&
flask db upgrade``). Si une évolution n'est ajoutée que d'un côté, les
environnements divergent : les tests passent, la production casse au
démarrage (``REQUIRE_ALEMBIC_CURRENT=true``).

Ce test construit les deux schémas dans deux bases temporaires et compare
tables, colonnes, index et contraintes. Il détecte la divergence au moment
du commit plutôt qu'au déploiement.

Il se met de lui-même en « skipped » si l'utilisateur de test n'a pas le
droit de créer des bases (certains hébergements mutualisés).
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest
import psycopg2

ROOT = Path(__file__).resolve().parents[1]

DB_A = None  # init_db() seul
DB_B = None  # init_db() + migrations Alembic


def _dsn_de_base():
    return (
        os.environ.get("TEST_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
        or ""
    )


def _maintenance_conn(dsn):
    """Connexion à une base de service pour pouvoir CREATE/DROP DATABASE."""
    base = dsn.rsplit("/", 1)[0]
    for candidate in (base + "/postgres", base + "/template1"):
        try:
            conn = psycopg2.connect(candidate)
            conn.autocommit = True
            return conn
        except psycopg2.Error:
            continue
    return None


def _creer_base(conn, nom):
    with conn.cursor() as cur:
        cur.execute('DROP DATABASE IF EXISTS "%s"' % nom)
        cur.execute('CREATE DATABASE "%s"' % nom)


SCHEMA_QUERIES = {
    "colonnes": """
        SELECT table_name, column_name, data_type, is_nullable,
               COALESCE(column_default, '') AS dflt
          FROM information_schema.columns
         WHERE table_schema = 'public' AND table_name <> 'alembic_version'
         ORDER BY table_name, column_name
    """,
    "index": """
        SELECT indexname, indexdef FROM pg_indexes
         WHERE schemaname = 'public' AND tablename <> 'alembic_version'
         ORDER BY indexname
    """,
    "contraintes": """
        SELECT c.conname, pg_get_constraintdef(c.oid) AS definition
          FROM pg_constraint c
          JOIN pg_class t ON t.oid = c.conrelid
          JOIN pg_namespace n ON n.oid = t.relnamespace
         WHERE n.nspname = 'public' AND t.relname <> 'alembic_version'
         ORDER BY c.conname
    """,
}


def _lire_schema(dsn):
    """Relève l'empreinte du schéma public (hors table de version Alembic)."""
    conn = psycopg2.connect(dsn)
    try:
        empreinte = {}
        with conn.cursor() as cur:
            for nom, sql in SCHEMA_QUERIES.items():
                cur.execute(sql)
                empreinte[nom] = {tuple(map(str, row)) for row in cur.fetchall()}
        return empreinte
    finally:
        conn.close()


def _executer(nom_base, commande):
    """Lance une commande Python/Flask contre une base précise."""
    dsn = _dsn_de_base().rsplit("/", 1)[0] + "/" + nom_base
    env = dict(os.environ)
    env.update(
        DATABASE_URL=dsn,
        AUTO_INIT_DB="true",
        SCHEDULER_MODE="disabled",
        RATELIMIT_ENABLED="false",
        EMAIL_ENABLED="false",
        LOG_FORMAT="text",
        SECRET_KEY=env.get("SECRET_KEY", "schema-parity-secret"),
        PYTHONPATH=str(ROOT),
    )
    resultat = subprocess.run(
        commande, cwd=str(ROOT), env=env, capture_output=True, text=True
    )
    if resultat.returncode != 0:
        pytest.fail(
            "Échec de %s :\n%s\n%s"
            % (" ".join(commande), resultat.stdout[-2000:], resultat.stderr[-2000:])
        )
    return dsn


@pytest.fixture(scope="module")
def schemas():
    """Construit les deux bases et renvoie leurs empreintes de schéma."""
    dsn = _dsn_de_base()
    if not dsn.startswith("postgres"):
        pytest.skip("parité de schéma : base PostgreSQL requise")

    conn = _maintenance_conn(dsn)
    if conn is None:
        pytest.skip("parité de schéma : impossible de créer une base de test")

    nom_a, nom_b = "gp_parity_initdb", "gp_parity_alembic"
    try:
        _creer_base(conn, nom_a)
        _creer_base(conn, nom_b)
        # Chemin A : init_db() seul (développement / tests / bootstrap-db).
        dsn_a = _executer(nom_a, [sys.executable, "-c", "import app"])
        # Chemin B : init_db() puis migrations Alembic (production).
        dsn_b = _executer(nom_b, [sys.executable, "-m", "flask", "db", "upgrade"])
        return _lire_schema(dsn_a), _lire_schema(dsn_b)
    finally:
        with conn.cursor() as cur:
            for nom in (nom_a, nom_b):
                cur.execute('DROP DATABASE IF EXISTS "%s"' % nom)
        conn.close()


def test_initdb_et_alembic_produisent_le_meme_schema(schemas):
    """Aucune table, colonne, index ou contrainte ne doit manquer d'un côté."""
    a, b = schemas

    assert a["colonnes"], "le relevé de colonnes est vide : le test ne vérifie rien"
    assert b["colonnes"], "le relevé de colonnes est vide : le test ne vérifie rien"

    for categorie in ("colonnes", "index", "contraintes"):
        manquants = b[categorie] - a[categorie]
        supplementaires = a[categorie] - b[categorie]
        assert not manquants, (
            "présent dans les migrations Alembic mais pas dans init_db() "
            "(cassera les tests) : %s" % sorted(manquants)[:10]
        )
        assert not supplementaires, (
            "présent dans init_db() mais pas dans les migrations Alembic "
            "(cassera la production) : %s" % sorted(supplementaires)[:10]
        )
