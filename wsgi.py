"""Gunicorn entrypoint for the production WebUI."""
import logging

from core import codex_retry_service, registration_service
from webui.app import create_app

logger = logging.getLogger(__name__)

def _initialize_app():
    app = create_app()
    registration_recovery = registration_service.reconcile_interrupted_registration_jobs()
    if any(registration_recovery.values()):
        logger.warning(
            "Reconciled stale registration state: stopped_jobs=%s failed_qan8_assignments=%s completed_qan8_assignments=%s",
            registration_recovery["stopped_jobs"],
            registration_recovery["failed_qan8_assignments"],
            registration_recovery["completed_qan8_assignments"],
        )
    retry_recovery = codex_retry_service.reconcile_persisted_retrying_statuses()
    if retry_recovery["reset"]:
        logger.warning(
            "Reset %s persisted Codex retrying states without active workers",
            retry_recovery["reset"],
        )
    return app


app = _initialize_app()
