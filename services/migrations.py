"""Initialisation SQLAlchemy et Flask-Migrate/Alembic."""

import os


def configurer_migrations(app, db, migrate, database_url):
    app.config.update(
        SQLALCHEMY_DATABASE_URI=database_url.replace(
            'postgres://', 'postgresql://', 1
        ),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SQLALCHEMY_ENGINE_OPTIONS={
            'pool_pre_ping': True,
            'pool_recycle': int(
                os.environ.get('SQLALCHEMY_POOL_RECYCLE', '300')
            ),
        },
    )
    db.init_app(app)
    migrate.init_app(app, db, directory='migrations')
