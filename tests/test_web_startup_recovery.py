import sys
import unittest
from unittest.mock import Mock, patch

import web
from core import registration_service


class WebStartupRecoveryTests(unittest.TestCase):
    def test_startup_reconciles_registration_jobs_before_accepting_requests(self):
        app = Mock()
        registration_recovery = {
            "stopped_jobs": 1,
            "failed_qan8_assignments": 1,
            "completed_qan8_assignments": 0,
        }
        with patch.object(sys, "argv", ["web.py", "--port", "5099"]), patch.object(
            web, "_assert_listen_address_available"
        ), patch.object(web, "_acquire_single_instance", return_value=Mock()), patch.object(
            web, "_release_single_instance"
        ), patch.object(web, "create_app", return_value=app), patch.object(
            web.codex_retry_service,
            "reconcile_persisted_retrying_statuses",
            return_value={"reset": 0},
        ), patch.object(web, "is_generated_code", return_value=False), patch.object(
            registration_service,
            "reconcile_interrupted_registration_jobs",
            return_value=registration_recovery,
            create=True,
        ) as reconcile:
            web.main()

        reconcile.assert_called_once_with()
        app.run.assert_called_once_with(host="127.0.0.1", port=5099, debug=False, threaded=True)


if __name__ == "__main__":
    unittest.main()
