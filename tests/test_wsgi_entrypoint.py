import importlib
import sys
import unittest
from unittest.mock import patch


class WsgiEntrypointTests(unittest.TestCase):
    def test_initialize_app_reconciles_registration_state_before_serving(self):
        app = object()
        recovery = {
            "stopped_jobs": 5,
            "failed_qan8_assignments": 1,
            "completed_qan8_assignments": 0,
        }
        sys.modules.pop("wsgi", None)
        with patch("webui.app.create_app", return_value=app), patch(
            "core.registration_service.reconcile_interrupted_registration_jobs",
            return_value=recovery,
        ) as reconcile, patch(
            "core.codex_retry_service.reconcile_persisted_retrying_statuses",
            return_value={"reset": 0},
        ):
            wsgi = importlib.import_module("wsgi")
            self.assertIs(wsgi.app, app)

        reconcile.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
