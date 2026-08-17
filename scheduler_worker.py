"""Processus APScheduler dédié à la production.

Lancer avec ``python scheduler_worker.py``. Il ne sert aucune requête HTTP et
ne doit donc pas être ajouté aux workers Gunicorn.
"""

import logging
import os
import time

# Évite tout scheduler embarqué pendant l'import de l'application.
os.environ["SCHEDULER_MODE"] = "worker"
os.environ.setdefault("SERVICE_NAME", "gestion-personnel-scheduler")

from app import (  # noqa: E402
    app,
    db_cursor,
    job_alertes_contrats,
    job_alertes_expiration_documents,
    job_generation_quotidienne_absences,
    job_purge_sessions,
    job_recalcul_soldes_conges,
    job_traiter_file_emails,
    job_validation_auto_maintenances,
)
from services.scheduler_runtime import build_scheduler  # noqa: E402


logger = logging.getLogger("gestion_personnel.scheduler.worker")
EXPECTED_REVISION = "20260817_objectifs"


def attendre_schema() -> None:
    """Attend le preDeploy web lors d'un premier déploiement parallèle Render."""
    timeout = max(30, int(os.environ.get("SCHEDULER_STARTUP_TIMEOUT", "900")))
    interval = max(2, int(os.environ.get("SCHEDULER_STARTUP_RETRY_SECONDS", "5")))
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            with db_cursor() as (_conn, cur):
                cur.execute("""
                    SELECT to_regclass('public.scheduler_heartbeats') IS NOT NULL
                             AS heartbeat_ready,
                           (SELECT version_num FROM alembic_version LIMIT 1)
                             AS revision
                """)
                state = cur.fetchone()
            if state["heartbeat_ready"] and state["revision"] == EXPECTED_REVISION:
                return
            last_error = RuntimeError(
                f"schéma incomplet (révision={state.get('revision')!r})"
            )
        except Exception as exc:
            last_error = exc
        logger.warning(
            "schéma PostgreSQL pas encore prêt; nouvelle tentative",
            extra={"retry_seconds": interval, "error": repr(last_error)},
        )
        time.sleep(interval)
    raise RuntimeError(
        "Le schéma PostgreSQL n'est pas prêt après le délai du scheduler."
    ) from last_error


def main() -> None:
    attendre_schema()
    jobs = {
        "generation_absences": job_generation_quotidienne_absences,
        "alertes_documents": job_alertes_expiration_documents,
        "recalcul_soldes": job_recalcul_soldes_conges,
        "alertes_contrats": job_alertes_contrats,
        "purge_sessions": job_purge_sessions,
        "validation_maintenances": job_validation_auto_maintenances,
        "email_outbox": job_traiter_file_emails,
    }
    scheduler = build_scheduler(
        jobs=jobs,
        db_cursor=db_cursor,
        app_config=app.config,
        blocking=True,
    )
    with app.app_context():
        scheduler.phase4_heartbeat()
    logger.info(
        "worker scheduler prêt",
        extra={"instance_id": scheduler.phase4_instance_id},
    )
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("arrêt du worker scheduler")


if __name__ == "__main__":
    main()
