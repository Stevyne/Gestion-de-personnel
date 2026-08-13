"""Configuration commune du scheduler, hors processus web en production."""

from __future__ import annotations

import json
import logging
import os
import socket
from uuid import uuid4

from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_EXECUTED, EVENT_JOB_MISSED
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.blocking import BlockingScheduler


logger = logging.getLogger("gestion_personnel.scheduler")


def _instance_id() -> str:
    explicit = os.environ.get("RENDER_INSTANCE_ID") or os.environ.get("HOSTNAME")
    return f"{explicit or socket.gethostname()}-{os.getpid()}-{uuid4().hex[:8]}"[:160]


def build_scheduler(*, jobs, db_cursor, app_config, blocking=False):
    """Crée APScheduler avec les jobs métier et un heartbeat PostgreSQL."""
    scheduler_class = BlockingScheduler if blocking else BackgroundScheduler
    scheduler = scheduler_class(
        timezone=os.environ.get("SCHEDULER_TIMEZONE", "Indian/Antananarivo"),
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 900},
    )
    instance_id = _instance_id()

    def heartbeat():
        metadata = json.dumps(
            {"pid": os.getpid(), "hostname": socket.gethostname()},
            separators=(",", ":"),
        )
        with db_cursor(commit=True) as (_conn, cur):
            cur.execute(
                """
                INSERT INTO scheduler_heartbeats
                    (instance_id, service_name, started_at, last_seen, metadata)
                VALUES (%s, 'scheduler', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, %s)
                ON CONFLICT (instance_id) DO UPDATE
                  SET last_seen=CURRENT_TIMESTAMP, metadata=EXCLUDED.metadata
                """,
                (instance_id, metadata),
            )
            # Nettoyage des anciennes instances et des anciens garde-fous.
            cur.execute(
                "DELETE FROM scheduler_heartbeats "
                "WHERE last_seen < CURRENT_TIMESTAMP - INTERVAL '7 days'"
            )

    scheduler.add_job(
        heartbeat,
        "interval",
        seconds=max(15, int(os.environ.get("SCHEDULER_HEARTBEAT_SECONDS", "60"))),
        id="scheduler_heartbeat",
        replace_existing=True,
    )
    scheduler.add_job(
        jobs["generation_absences"], "cron", hour=1, minute=0,
        id="generation_absences_quotidienne", replace_existing=True,
    )
    scheduler.add_job(
        jobs["alertes_documents"], "cron", hour=1, minute=30,
        id="alertes_expiration_documents", replace_existing=True,
    )
    scheduler.add_job(
        jobs["recalcul_soldes"], "cron", hour=2, minute=0,
        id="recalcul_soldes_conges", replace_existing=True,
    )
    scheduler.add_job(
        jobs["alertes_contrats"], "cron", hour=2, minute=30,
        id="alertes_contrats", replace_existing=True,
    )
    scheduler.add_job(
        jobs["purge_sessions"], "cron", hour=3, minute=0,
        id="purge_sessions_actives", replace_existing=True,
    )
    scheduler.add_job(
        jobs["validation_maintenances"], "cron", hour=3, minute=30,
        id="validation_auto_maintenances", replace_existing=True,
    )
    if app_config.get("EMAIL_ENABLED"):
        scheduler.add_job(
            jobs["email_outbox"],
            "interval",
            seconds=max(15, int(app_config.get("EMAIL_POLL_SECONDS", 60))),
            id="traiter_file_emails",
            replace_existing=True,
        )

    def log_event(event):
        if event.code == EVENT_JOB_ERROR:
            logger.error(
                "job scheduler en échec",
                extra={
                    "job_id": event.job_id,
                    "scheduled_run_time": str(event.scheduled_run_time),
                    "error": repr(event.exception),
                    "traceback": event.traceback,
                },
            )
            try:
                import sentry_sdk
                sentry_sdk.capture_exception(event.exception)
            except ImportError:
                pass
        elif event.code == EVENT_JOB_MISSED:
            logger.warning(
                "job scheduler manqué",
                extra={"job_id": event.job_id, "scheduled_run_time": str(event.scheduled_run_time)},
            )
        else:
            logger.info("job scheduler terminé", extra={"job_id": event.job_id})

    scheduler.add_listener(
        log_event, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR | EVENT_JOB_MISSED
    )
    scheduler.phase4_heartbeat = heartbeat
    scheduler.phase4_instance_id = instance_id
    return scheduler
