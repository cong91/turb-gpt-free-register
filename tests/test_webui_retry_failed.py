"""WebUI tests: retry-all-failed endpoints, parallel retry gating, plan display."""
import unittest
from unittest.mock import patch

from webui import password_change_api, twofa_change_api
from webui.personal_info_progress import _progress_lock
from webui.app import create_app


def _immediate_thread(thread_mock):
    def start_thread(*, target, args, **_kwargs):
        class ImmediateThread:
            def start(self):
                target(*args)

        return ImmediateThread()

    thread_mock.side_effect = start_thread


class TwofaRetryFailedApiTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        save_patcher = patch(
            "webui.twofa_change_api.db.save_personal_info_change_batch",
            return_value={"batch_id": "b" * 32, "exportable_count": 1},
        )
        self.save_batch = save_patcher.start()
        self.addCleanup(save_patcher.stop)

    def tearDown(self):
        with _progress_lock:
            twofa_change_api._twofa_progress.clear()

    @patch("webui.twofa_change_api.threading.Thread")
    @patch("webui.twofa_change_api.run_twofa_change_batch")
    def test_retry_success_clears_previous_error(self, run_batch, thread):
        _immediate_thread(thread)
        run_batch.side_effect = [
            [{"ok": False, "persisted": False, "email": "user@example.com", "error": "old failure"}],
            [{"ok": True, "persisted": True, "email": "user@example.com"}],
        ]
        credentials = "user@example.com----pw----TOTPBASE32"
        initial = self.client.post(
            "/api/accounts/change-twofa",
            json={"credentials": credentials},
            headers={"Origin": "http://localhost", "X-Auth-Code": "test-auth"},
        )
        retry = self.client.post(
            "/api/accounts/change-twofa-retry",
            json={"batch_id": initial.get_json()["batch_id"], "index": 0, "credentials": credentials},
            headers={"Origin": "http://localhost", "X-Auth-Code": "test-auth"},
        )
        row = retry.get_json()["results"][0]
        self.assertEqual(row["status"], "success")
        self.assertNotIn("error", row)
        self.assertNotIn("old failure", row["detail"])

    @patch("webui.twofa_change_api.threading.Thread")
    @patch("webui.twofa_change_api.run_twofa_change_batch")
    def test_retry_all_start_failure_restores_failed_rows(self, run_batch, thread):
        _immediate_thread(thread)
        run_batch.return_value = [
            {"ok": False, "persisted": False, "email": "user@example.com", "error": "initial failure"},
        ]
        credentials = "user@example.com----pw----TOTPBASE32"
        initial = self.client.post(
            "/api/accounts/change-twofa",
            json={"credentials": credentials},
            headers={"Origin": "http://localhost", "X-Auth-Code": "test-auth"},
        )
        batch_id = initial.get_json()["batch_id"]
        thread.side_effect = RuntimeError("thread unavailable")
        retry = self.client.post(
            "/api/accounts/change-twofa-retry-failed",
            json={"batch_id": batch_id, "credentials": credentials},
            headers={"Origin": "http://localhost", "X-Auth-Code": "test-auth"},
        )
        snapshot = self.client.get(
            f"/api/accounts/change-twofa-status?batch_id={batch_id}",
            headers={"X-Auth-Code": "test-auth"},
        ).get_json()
        self.assertEqual(retry.status_code, 500)
        self.assertEqual(snapshot["status"], "completed")
        self.assertEqual(snapshot["results"][0]["status"], "failed")
        self.assertEqual(snapshot["running"], 0)

    @patch("webui.twofa_change_api.threading.Thread")
    @patch("webui.twofa_change_api.run_twofa_change_batch")
    def test_retry_failed_reruns_only_failed_rows_with_workers(self, run_batch, thread):
        _immediate_thread(thread)
        run_batch.side_effect = [
            [
                {"ok": True, "persisted": True, "email": "a@example.com", "account_id": 1},
                {"ok": False, "persisted": False, "email": "b@example.com", "error": "login failed"},
            ],
            [
                {"ok": True, "persisted": True, "email": "b@example.com", "account_id": 2},
            ],
        ]
        credentials = "a@example.com----pw----TOTPBASE32\nb@example.com----pw----TOTPBASE32"

        initial = self.client.post(
            "/api/accounts/change-twofa",
            json={"credentials": credentials, "workers": 2},
            headers={"Origin": "http://localhost", "X-Auth-Code": "test-auth"},
        )
        self.assertEqual(initial.status_code, 202, initial.get_json())
        batch_id = initial.get_json()["batch_id"]
        self.assertEqual(initial.get_json()["failed"], 1)

        retry = self.client.post(
            "/api/accounts/change-twofa-retry-failed",
            json={"batch_id": batch_id, "credentials": credentials, "workers": 3},
            headers={"Origin": "http://localhost", "X-Auth-Code": "test-auth"},
        )

        self.assertEqual(retry.status_code, 202, retry.get_json())
        payload = retry.get_json()
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["failed"], 0)
        self.assertEqual(run_batch.call_count, 2)
        retried_items = run_batch.call_args_list[1].args[0]
        self.assertEqual([item.email for item in retried_items], ["b@example.com"])
        self.assertEqual(run_batch.call_args_list[1].kwargs["workers"], 3)

    @patch("webui.twofa_change_api.threading.Thread")
    @patch("webui.twofa_change_api.run_twofa_change_batch")
    def test_retry_failed_skips_row_already_retrying(self, run_batch, thread):
        _immediate_thread(thread)
        run_batch.return_value = [
            {"ok": False, "persisted": False, "email": "a@example.com", "error": "x"},
            {"ok": False, "persisted": False, "email": "b@example.com", "error": "y"},
        ]
        credentials = "a@example.com----pw----TOTPBASE32\nb@example.com----pw----TOTPBASE32"
        initial = self.client.post(
            "/api/accounts/change-twofa",
            json={"credentials": credentials},
            headers={"Origin": "http://localhost", "X-Auth-Code": "test-auth"},
        )
        batch_id = initial.get_json()["batch_id"]

        # Row 0 đang được retry thủ công (running): retry-all chỉ chạy row 1.
        with _progress_lock:
            twofa_change_api._twofa_progress[batch_id]["results"][0]["status"] = "running"
        run_batch.side_effect = None
        run_batch.return_value = [
            {"ok": True, "persisted": True, "email": "b@example.com", "account_id": 9},
        ]

        retry = self.client.post(
            "/api/accounts/change-twofa-retry-failed",
            json={"batch_id": batch_id, "credentials": credentials, "workers": 2},
            headers={"Origin": "http://localhost", "X-Auth-Code": "test-auth"},
        )

        self.assertEqual(retry.status_code, 202, retry.get_json())
        retried_items = run_batch.call_args_list[-1].args[0]
        self.assertEqual([item.email for item in retried_items], ["b@example.com"])

    @patch("webui.twofa_change_api.threading.Thread")
    @patch("webui.twofa_change_api.run_twofa_change_batch")
    def test_retry_failed_rejects_when_no_failed_rows(self, run_batch, thread):
        _immediate_thread(thread)
        run_batch.return_value = [
            {"ok": True, "persisted": True, "email": "a@example.com", "account_id": 1},
        ]
        initial = self.client.post(
            "/api/accounts/change-twofa",
            json={"credentials": "a@example.com----pw----TOTPBASE32"},
            headers={"Origin": "http://localhost", "X-Auth-Code": "test-auth"},
        )
        retry = self.client.post(
            "/api/accounts/change-twofa-retry-failed",
            json={
                "batch_id": initial.get_json()["batch_id"],
                "credentials": "a@example.com----pw----TOTPBASE32",
            },
            headers={"Origin": "http://localhost", "X-Auth-Code": "test-auth"},
        )
        self.assertEqual(retry.status_code, 409, retry.get_json())
        self.assertIn("Không có tài khoản lỗi", retry.get_json()["error"])

    @patch("webui.twofa_change_api.threading.Thread")
    @patch("webui.twofa_change_api.run_twofa_change_batch")
    def test_codex_option_is_snapshotted_and_preserved_for_row_retry(self, run_batch, thread):
        _immediate_thread(thread)
        run_batch.side_effect = [
            [{"ok": False, "persisted": False, "email": "user@example.com", "error": "login", "codex": {"status": "skipped", "ok": False, "reason": "change failed"}}],
            [{"ok": False, "persisted": False, "email": "user@example.com", "error": "login again", "codex": {"status": "skipped", "ok": False, "reason": "change failed"}}],
        ]
        client = self.client
        initial = client.post(
            "/api/accounts/change-twofa",
            json={"credentials": "user@example.com----pw----TOTPBASE32", "login_codex_after_change": True},
            headers={"Origin": "http://localhost", "X-Auth-Code": "test-auth"},
        )
        self.assertEqual(initial.status_code, 202, initial.get_json())
        batch_id = initial.get_json()["batch_id"]
        self.assertTrue(run_batch.call_args_list[0].kwargs["login_codex_after_change"])
        with _progress_lock:
            twofa_change_api._twofa_progress[batch_id]["login_codex_after_change"] = True

        retry = client.post(
            "/api/accounts/change-twofa-retry",
            json={"batch_id": batch_id, "index": 0, "credentials": "user@example.com----pw----TOTPBASE32"},
            headers={"Origin": "http://localhost", "X-Auth-Code": "test-auth"},
        )
        self.assertEqual(retry.status_code, 202, retry.get_json())
        self.assertTrue(run_batch.call_args_list[1].kwargs["login_codex_after_change"])
        self.assertEqual(retry.get_json()["results"][0]["codex"]["status"], "skipped")

        with (
            patch("webui.twofa_change_api.threading.Thread") as thread,
            patch("webui.twofa_change_api.run_twofa_change_batch") as run_batch,
        ):
            response = self.client.post(
                "/api/accounts/change-twofa",
                json={"credentials": "user@example.com----pw----TOTPBASE32", "login_codex_after_change": "false"},
                headers={"Origin": "http://localhost", "X-Auth-Code": "test-auth"},
            )
        self.assertEqual(response.status_code, 400, response.get_json())
        thread.assert_not_called()
        run_batch.assert_not_called()

    @patch("webui.twofa_change_api.threading.Thread")
    @patch("webui.twofa_change_api.run_twofa_change_batch")
    def test_per_row_retry_allowed_while_another_retry_is_running(self, run_batch, thread):
        _immediate_thread(thread)
        run_batch.side_effect = [
            [
                {"ok": False, "persisted": False, "email": "a@example.com", "error": "x"},
                {"ok": False, "persisted": False, "email": "b@example.com", "error": "y"},
            ],
            [{"ok": True, "persisted": True, "email": "b@example.com", "account_id": 9}],
        ]
        credentials = "a@example.com----pw----TOTPBASE32\nb@example.com----pw----TOTPBASE32"
        initial = self.client.post(
            "/api/accounts/change-twofa",
            json={"credentials": credentials},
            headers={"Origin": "http://localhost", "X-Auth-Code": "test-auth"},
        )
        batch_id = initial.get_json()["batch_id"]
        with _progress_lock:
            twofa_change_api._twofa_progress[batch_id]["results"][0]["status"] = "running"
            twofa_change_api._twofa_progress[batch_id]["status"] = "running"
        retry = self.client.post(
            "/api/accounts/change-twofa-retry",
            json={"batch_id": batch_id, "index": 1, "credentials": credentials},
            headers={"Origin": "http://localhost", "X-Auth-Code": "test-auth"},
        )
        self.assertEqual(retry.status_code, 202, retry.get_json())
        self.assertEqual(retry.get_json()["results"][1]["status"], "success")
        self.assertEqual([item.email for item in run_batch.call_args_list[1].args[0]], ["b@example.com"])


class PasswordRetryFailedApiTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        save_patcher = patch(
            "webui.password_change_api.db.save_personal_info_change_batch",
            return_value={"batch_id": "b" * 32, "exportable_count": 1},
        )
        self.save_batch = save_patcher.start()
        self.addCleanup(save_patcher.stop)

    def tearDown(self):
        with _progress_lock:
            password_change_api._password_progress.clear()

    @patch("webui.password_change_api.threading.Thread")
    @patch("webui.password_change_api.run_password_change_batch")
    def test_retry_success_clears_previous_error(self, run_batch, thread):
        _immediate_thread(thread)
        run_batch.side_effect = [
            [{"ok": False, "persisted": False, "email": "user@example.com", "error": "old failure"}],
            [{"ok": True, "persisted": True, "email": "user@example.com"}],
        ]
        initial = self.client.post(
            "/api/accounts/change-password",
            json={"credentials": "user@example.com"},
            headers={"Origin": "http://localhost", "X-Auth-Code": "test-auth"},
        )
        retry = self.client.post(
            "/api/accounts/change-password-retry",
            json={"batch_id": initial.get_json()["batch_id"], "index": 0},
            headers={"Origin": "http://localhost", "X-Auth-Code": "test-auth"},
        )
        row = retry.get_json()["results"][0]
        self.assertEqual(row["status"], "success")
        self.assertNotIn("error", row)
        self.assertNotIn("old failure", row["detail"])

    @patch("webui.password_change_api.threading.Thread")
    @patch("webui.password_change_api.run_password_change_batch")
    def test_codex_progress_is_forwarded_before_the_batch_finishes(self, run_batch, thread):
        _immediate_thread(thread)
        observed = []

        def run(items, *, progress_callback, **_kwargs):
            progress_callback(0, {
                "status": "running",
                "email": items[0].email,
                "codex": {"status": "running", "ok": False},
            })
            with _progress_lock:
                batch_id = next(iter(password_change_api._password_progress))
            observed.append(password_change_api._password_progress_snapshot(batch_id))
            return [{"ok": True, "persisted": True, "email": items[0].email}]

        run_batch.side_effect = run
        response = self.client.post(
            "/api/accounts/change-password",
            json={"credentials": "user@example.com", "login_codex_after_change": True},
            headers={"Origin": "http://localhost", "X-Auth-Code": "test-auth"},
        )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(observed[0]["results"][0]["codex"]["status"], "running")

    @patch("webui.password_change_api.threading.Thread")
    @patch("webui.password_change_api.run_password_change_batch")
    def test_retry_all_start_failure_restores_failed_rows(self, run_batch, thread):
        _immediate_thread(thread)
        run_batch.return_value = [
            {"ok": False, "persisted": False, "email": "user@example.com", "error": "initial failure"},
        ]
        initial = self.client.post(
            "/api/accounts/change-password",
            json={"credentials": "user@example.com"},
            headers={"Origin": "http://localhost", "X-Auth-Code": "test-auth"},
        )
        batch_id = initial.get_json()["batch_id"]
        thread.side_effect = RuntimeError("thread unavailable")
        retry = self.client.post(
            "/api/accounts/change-password-retry-failed",
            json={"batch_id": batch_id},
            headers={"Origin": "http://localhost", "X-Auth-Code": "test-auth"},
        )
        snapshot = self.client.get(
            f"/api/accounts/change-password-status?batch_id={batch_id}",
            headers={"X-Auth-Code": "test-auth"},
        ).get_json()
        self.assertEqual(retry.status_code, 500)
        self.assertEqual(snapshot["status"], "completed")
        self.assertEqual(snapshot["results"][0]["status"], "failed")
        self.assertEqual(snapshot["running"], 0)

    @patch("webui.password_change_api.threading.Thread")
    @patch("webui.password_change_api.run_password_change_batch")
    def test_retry_failed_reruns_failed_rows_and_shows_plan(self, run_batch, thread):
        _immediate_thread(thread)
        run_batch.side_effect = [
            [
                {"ok": False, "persisted": False, "email": "a@example.com", "error": "x"},
                {
                    "ok": True,
                    "persisted": True,
                    "email": "b@example.com",
                    "account_id": 2,
                    "plan_check": {
                        "accepted": True,
                        "ok": True,
                        "current_plan_type": "plus",
                        "plus_trial_eligible": False,
                    },
                },
            ],
            [
                {
                    "ok": True,
                    "persisted": True,
                    "email": "a@example.com",
                    "account_id": 3,
                    "plan_check": {
                        "accepted": True,
                        "ok": True,
                        "current_plan_type": "free",
                        "plus_trial_eligible": True,
                    },
                },
            ],
        ]
        initial = self.client.post(
            "/api/accounts/change-password",
            json={"credentials": "a@example.com\nb@example.com", "workers": 2},
            headers={"Origin": "http://localhost", "X-Auth-Code": "test-auth"},
        )
        self.assertEqual(initial.status_code, 202, initial.get_json())
        batch_id = initial.get_json()["batch_id"]
        # Row thành công phải hiển thị gói ngay trong kết quả đầu tiên.
        success_row = initial.get_json()["results"][1]
        self.assertIn("Gói: plus", success_row["detail"])
        self.assertEqual(success_row["plan_check"]["current_plan_type"], "plus")

        retry = self.client.post(
            "/api/accounts/change-password-retry-failed",
            json={"batch_id": batch_id, "workers": 2},
            headers={"Origin": "http://localhost", "X-Auth-Code": "test-auth"},
        )

        self.assertEqual(retry.status_code, 202, retry.get_json())
        payload = retry.get_json()
        self.assertEqual(payload["status"], "completed")
        self.assertEqual(payload["failed"], 0)
        retried_row = payload["results"][0]
        self.assertEqual(retried_row["status"], "success")
        self.assertIn("Gói: free (có Plus trial)", retried_row["detail"])
        retried_items = run_batch.call_args_list[-1].args[0]
        self.assertEqual([item.email for item in retried_items], ["a@example.com"])
        self.assertEqual(run_batch.call_args_list[-1].kwargs["workers"], 2)


if __name__ == "__main__":
    unittest.main()
