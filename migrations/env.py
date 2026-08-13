from logging.config import fileConfig

from alembic import context
from flask import current_app


config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def get_engine():
    try:
        return current_app.extensions["migrate"].db.get_engine()
    except (TypeError, AttributeError):
        return current_app.extensions["migrate"].db.engine


def get_engine_url():
    try:
        return get_engine().url.render_as_string(hide_password=False).replace("%", "%%")
    except AttributeError:
        return str(get_engine().url).replace("%", "%%")


config.set_main_option("sqlalchemy.url", get_engine_url())
target_db = current_app.extensions["migrate"].db


def include_object(obj, name, type_, reflected, compare_to):
    # Les tables historiques n'ont pas encore de modèles SQLAlchemy. Sans ce
    # filtre, `flask db migrate` proposerait dangereusement de toutes les
    # supprimer parce qu'elles existent en base mais pas dans MetaData.
    if type_ == "table" and reflected and compare_to is None:
        return False
    return True


def process_revision_directives(context_, revision, directives):
    if getattr(config.cmd_opts, "autogenerate", False):
        script = directives[0]
        if script.upgrade_ops.is_empty():
            directives[:] = []
            current_app.logger.info("Aucun changement de schéma détecté.")


def run_migrations_offline():
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_db.metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online():
    conf_args = current_app.extensions["migrate"].configure_args
    conf_args.setdefault("process_revision_directives", process_revision_directives)
    conf_args.setdefault("include_object", include_object)
    with get_engine().connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_db.metadata,
            **conf_args,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
