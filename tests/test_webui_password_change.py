"""WebUI route tests for the account password-change batch API."""
import json
import threading
import unittest
from unittest.mock import patch

from webui import email_change_api
from webui.app import create_app


def _block_batch_runner(*_args, **_kwargs):
    """Giữ batch ở trạng thái running mà không đụng browser/proxy thật."""
    return threading.Event().wait()


class PasswordChangeApiTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()

    def tearDown(self):
        with email_change_api._progress_lock:
            email_change_api._password_progress.clear()

    @staticmethod
    def _run_thread_target_immediately(thread_mock):
        def start_thread(*, target, args, **_kwargs):
            class ImmediateThread:
                def start(self):
                    target(*args)

            return ImmediateThread()

        thread_mock.side_effect = start_thread

    @patch("webui.email_change_api.threading.Thread")
    @patch("webui.email_change_api.run_password_change_batch")
    def test_change_password_route_returns_statuses_without_secrets(self, run_batch, thread):
        self._run_thread_target_immediately(thread)
        run_batch.return_value = [{
            "ok": True,
            "persisted": True,
            "email": "user@example.com",
            "account_id": 7,
            "mode": "post_login_password_reset",
            "new_password": "NEWSECRET-MUST-STAY-SERVER-SIDE",
            "access_token": "token-must-stay-server-side",
        }]

        response = self.client.post(
            "/api/accounts/change-password",
            json={"credentials": "user@example.com", "workers": 2},
            headers={"Origin": "http://localhost", "X-Auth-Code": "test-auth"},
        )

        self.assertEqual(response.status_code, 202, response.get_json())
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["submitted"], 1)
        self.assertEqual(payload["succeeded"], 1)
        self.assertEqual(payload["failed"], 0)
        self.assertEqual(payload["completed"], 1)
        self.assertRegex(payload["batch_id"], r"^[0-9a-f]{32}$")
        self.assertEqual(payload["results"][0]["index"], 0)
        self.assertEqual(payload["results"][0]["email"], "user@example.com")
        self.assertEqual(payload["results"][0]["status"], "success")
        self.assertNotIn("NEWSECRET-MUST-STAY-SERVER-SIDE", json.dumps(payload))
        self.assertNotIn("token-must-stay-server-side", json.dumps(payload))
        run_batch.assert_called_once()
        self.assertEqual(run_batch.call_args.kwargs["workers"], 2)
        with email_change_api._progress_lock:
            self.assertNotIn("items", email_change_api._password_progress[payload["batch_id"]])

    @patch("webui.email_change_api.run_password_change_batch", side_effect=_block_batch_runner)
    @patch("webui.email_change_api.threading.Thread")
    def test_change_password_route_returns_batch_for_progress_polling(self, thread, _run_batch):
        response = self.client.post(
            "/api/accounts/change-password",
            json={"credentials": "user@example.com"},
            headers={"Origin": "http://localhost", "X-Auth-Code": "test-auth"},
        )

        self.assertEqual(response.status_code, 202, response.get_json())
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "running")
        self.assertEqual(payload["pending"], 1)
        self.assertEqual(payload["results"][0]["status"], "queued")
        thread.assert_called_once()
        thread.return_value.start.assert_called_once_with()

        status_response = self.client.get(
            f"/api/accounts/change-password-status?batch_id={payload['batch_id']}",
            headers={"X-Auth-Code": "test-auth"},
        )
        self.assertEqual(status_response.status_code, 200, status_response.get_json())
        status_payload = status_response.get_json()
        self.assertEqual(status_payload["batch_id"], payload["batch_id"])
        self.assertEqual(status_payload["results"][0]["email"], "user@example.com")
        self.assertEqual(status_payload["results"][0]["status"], "queued")

        missing = self.client.get(
            "/api/accounts/change-password-status?batch_id=does-not-exist",
            headers={"X-Auth-Code": "test-auth"},
        )
        self.assertEqual(missing.status_code, 404, missing.get_json())

    @patch("webui.email_change_api.threading.Thread")
    @patch("webui.email_change_api.run_password_change_batch")
    def test_change_password_route_reports_already_set_as_success_with_warning(self, run_batch, thread):
        self._run_thread_target_immediately(thread)
        run_batch.return_value = [{
            "ok": True,
            "persisted": False,
            "already_set": True,
            "email": "user@example.com",
            "account_id": 7,
        }]

        response = self.client.post(
            "/api/accounts/change-password",
            json={"credentials": "user@example.com"},
            headers={"Origin": "http://localhost", "X-Auth-Code": "test-auth"},
        )

        payload = response.get_json()
        self.assertEqual(response.status_code, 202, payload)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["succeeded"], 1)
        self.assertEqual(payload["failed"], 0)
        self.assertEqual(payload["results"][0]["status"], "success")
        self.assertEqual(payload["results"][0]["warning"], "Mật khẩu đã tồn tại từ trước")

    @patch("webui.email_change_api.threading.Thread")
    @patch("webui.email_change_api.run_password_change_batch")
    def test_change_password_route_fails_when_local_persistence_missing(self, run_batch, thread):
        self._run_thread_target_immediately(thread)
        run_batch.return_value = [{
            "ok": True,
            "persisted": False,
            "email": "user@example.com",
            "account_id": 7,
            "retryable": False,
            "warning": "local password persistence was not updated",
        }]

        response = self.client.post(
            "/api/accounts/change-password",
            json={"credentials": "user@example.com"},
            headers={"Origin": "http://localhost", "X-Auth-Code": "test-auth"},
        )

        payload = response.get_json()
        self.assertEqual(response.status_code, 202, payload)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["succeeded"], 0)
        self.assertEqual(payload["failed"], 1)
        self.assertEqual(payload["results"][0]["status"], "failed")

    @patch("webui.email_change_api.threading.Thread")
    @patch("webui.email_change_api.run_password_change_batch")
    def test_change_password_manual_retry_reruns_failed_row(self, run_batch, thread):
        self._run_thread_target_immediately(thread)
        run_batch.side_effect = [
            [{
                "ok": False,
                "persisted": False,
                "email": "user@example.com",
                "error": "temporary login failure",
            }],
            [{
                "ok": True,
                "persisted": True,
                "email": "user@example.com",
                "account_id": 7,
                "access_token_saved": True,
                "new_password": "NEWSECRET-MUST-STAY-SERVER-SIDE",
            }],
        ]

        initial = self.client.post(
            "/api/accounts/change-password",
            json={"credentials": "user@example.com"},
            headers={"Origin": "http://localhost", "X-Auth-Code": "test-auth"},
        )
        self.assertEqual(initial.status_code, 202, initial.get_json())
        initial_payload = initial.get_json()
        self.assertEqual(initial_payload["failed"], 1)

        retry = self.client.post(
            "/api/accounts/change-password-retry",
            json={"batch_id": initial_payload["batch_id"], "index": 0},
            headers={"Origin": "http://localhost", "X-Auth-Code": "test-auth"},
        )

        self.assertEqual(retry.status_code, 202, retry.get_json())
        payload = retry.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["results"][0]["status"], "success")
        self.assertNotIn("NEWSECRET-MUST-STAY-SERVER-SIDE", json.dumps(payload))
        self.assertEqual(run_batch.call_count, 2)
        retried_items = run_batch.call_args_list[1].args[0]
        self.assertEqual(len(retried_items), 1)
        retried_item = retried_items[0]
        self.assertEqual(retried_item.email, "user@example.com")
        self.assertEqual(retried_item.current_password, "")
        self.assertIsNone(retried_item.totp_secret)

    @patch("webui.email_change_api.threading.Thread")
    @patch("webui.email_change_api.run_password_change_batch")
    def test_change_password_manual_retry_rejects_row_marked_not_retryable(self, run_batch, thread):
        self._run_thread_target_immediately(thread)
        run_batch.return_value = [{
            "ok": True,
            "persisted": False,
            "email": "user@example.com",
            "account_id": 7,
            "retryable": False,
            "warning": "local password persistence was not updated",
        }]

        initial = self.client.post(
            "/api/accounts/change-password",
            json={"credentials": "user@example.com"},
            headers={"Origin": "http://localhost", "X-Auth-Code": "test-auth"},
        )
        batch_id = initial.get_json()["batch_id"]
        retry = self.client.post(
            "/api/accounts/change-password-retry",
            json={"batch_id": batch_id, "index": 0},
            headers={"Origin": "http://localhost", "X-Auth-Code": "test-auth"},
        )

        self.assertEqual(retry.status_code, 409, retry.get_json())
        self.assertIn("đối soát", retry.get_json()["error"])
        run_batch.assert_called_once()

    @patch("webui.email_change_api.run_password_change_batch", side_effect=_block_batch_runner)
    @patch("webui.email_change_api.threading.Thread")
    def test_change_password_manual_retry_requires_completed_batch(self, _thread, _run_batch):
        response = self.client.post(
            "/api/accounts/change-password",
            json={"credentials": "user@example.com"},
            headers={"Origin": "http://localhost", "X-Auth-Code": "test-auth"},
        )
        self.assertEqual(response.status_code, 202, response.get_json())
        batch_id = response.get_json()["batch_id"]

        retry = self.client.post(
            "/api/accounts/change-password-retry",
            json={"batch_id": batch_id, "index": 0},
            headers={"Origin": "http://localhost", "X-Auth-Code": "test-auth"},
        )

        self.assertEqual(retry.status_code, 409, retry.get_json())
        self.assertIn("đang xử lý", retry.get_json()["error"])

    def test_change_password_route_rejects_missing_origin_and_referer(self):
        # Đã đăng nhập bằng session nhưng request thiếu Origin/Referer -> vẫn chặn mutation.
        login = self.client.post("/login", data={"auth_code": "test-auth"})
        self.assertEqual(login.status_code, 302, login.status_code)

        response = self.client.post(
            "/api/accounts/change-password",
            json={"credentials": "user@example.com"},
        )
        self.assertEqual(response.status_code, 403, response.get_json())

    def test_change_password_route_rejects_untrusted_origin(self):
        response = self.client.post(
            "/api/accounts/change-password",
            json={"credentials": "user@example.com"},
            headers={"Origin": "http://evil.example", "X-Auth-Code": "test-auth"},
        )
        self.assertEqual(response.status_code, 403, response.get_json())

        retry = self.client.post(
            "/api/accounts/change-password-retry",
            json={"batch_id": "x", "index": 0},
            headers={"Origin": "http://evil.example", "X-Auth-Code": "test-auth"},
        )
        self.assertEqual(retry.status_code, 403, retry.get_json())

    def test_change_password_route_rejects_bad_input(self):
        empty = self.client.post(
            "/api/accounts/change-password",
            json={"credentials": ""},
            headers={"Origin": "http://localhost", "X-Auth-Code": "test-auth"},
        )
        self.assertEqual(empty.status_code, 400, empty.get_json())

        bad_email = self.client.post(
            "/api/accounts/change-password",
            json={"credentials": "not-an-email"},
            headers={"Origin": "http://localhost", "X-Auth-Code": "test-auth"},
        )
        self.assertEqual(bad_email.status_code, 400, bad_email.get_json())

        bad_workers = self.client.post(
            "/api/accounts/change-password",
            json={"credentials": "user@example.com", "workers": "abc"},
            headers={"Origin": "http://localhost", "X-Auth-Code": "test-auth"},
        )
        self.assertEqual(bad_workers.status_code, 400, bad_workers.get_json())

        too_many = self.client.post(
            "/api/accounts/change-password",
            json={"credentials": "\n".join(f"user{i}@example.com" for i in range(51))},
            headers={"Origin": "http://localhost", "X-Auth-Code": "test-auth"},
        )
        self.assertEqual(too_many.status_code, 400, too_many.get_json())

    @patch("webui.email_change_api.threading.Thread", side_effect=RuntimeError("thread unavailable"))
    def test_change_password_start_failure_marks_rows_failed(self, _thread):
        response = self.client.post(
            "/api/accounts/change-password",
            json={"credentials": "user@example.com"},
            headers={"Origin": "http://localhost", "X-Auth-Code": "test-auth"},
        )

        self.assertEqual(response.status_code, 500, response.get_json())
        batch_id = response.get_json()["batch_id"]
        status = self.client.get(
            f"/api/accounts/change-password-status?batch_id={batch_id}",
            headers={"X-Auth-Code": "test-auth"},
        )
        payload = status.get_json()
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["results"][0]["status"], "failed")

    def test_change_password_route_rejects_batch_when_progress_is_full(self):
        # Chỉ batch đang chạy mới chặn: trim luôn dọn batch đã hoàn tất.
        with email_change_api._progress_lock:
            for i in range(email_change_api._MAX_PROGRESS_BATCHES):
                email_change_api._password_progress[f"old-{i}"] = {
                    "batch_id": f"old-{i}",
                    "status": "running",
                    "created_at": str(i),
                    "emails": ["user@example.com"],
                    "results": [{"index": 0, "email": "user@example.com", "status": "queued", "detail": ""}],
                }

        response = self.client.post(
            "/api/accounts/change-password",
            json={"credentials": "user@example.com"},
            headers={"Origin": "http://localhost", "X-Auth-Code": "test-auth"},
        )

        self.assertEqual(response.status_code, 429, response.get_json())
        self.assertIn("quá nhiều batch", response.get_json()["error"])


if __name__ == "__main__":
    unittest.main()
