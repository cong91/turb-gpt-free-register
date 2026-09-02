"""Gunicorn entrypoint for the production WebUI."""
import logging

from core import codex_retry_service
from webui.app import create_app

logger = logging.getLogger(__name__)

app = create_app()
retry_recovery = codex_retry_service.reconcile_persisted_retrying_statuses()
if retry_recovery["reset"]:
    logger.warning(
        "Reset %s persisted Codex retrying states without active workers",
        retry_recovery["reset"],
    )
