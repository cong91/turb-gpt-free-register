import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from webui.app import create_app


class Sub2APIAutomationContractTests(unittest.TestCase):
    def setUp(self):
        self.client = create_app(auth_code="test-auth").test_client()
        self.headers = {"X-Auth-Code": "test-auth"}

    def test_registration_callback_waits_until_all_jobs_are_terminal(self):
        from core.sub2api_automation import registration_completion_event

        self.assertIsNone(
            registration_completion_event(
                "req-1",
                [
                    {"status": "success", "account_id": 11},
                    {"status": "running", "account_id": None},
                ],
            )
        )

    def test_registration_callback_contains_counts_without_credentials(self):
        from core.sub2api_automation import registration_completion_event

        event = registration_completion_event(
            "req-1",
            [
                {"status": "success", "account_id": 11},
                {"status": "failed", "account_id": None},
            ],
        )

        self.assertEqual(
            event,
            {
                "request_id": "req-1",
                "event_id": "req-1:registration:completed",
                "kind": "registration",
                "status": "completed",
                "requested_count": 2,
                "succeeded_count": 1,
                "failed_count": 1,
                "pending_count": 0,
            },
        )
        self.assertNotIn("access_token", event)
        self.assertNotIn("refresh_token", event)

    def test_registration_callback_waits_for_expected_job_count(self):
        from core.sub2api_automation import registration_completion_event

        self.assertIsNone(
            registration_completion_event(
                "req-expected",
                [
                    {
                        "status": "success",
                        "provider_context": {
                            "sub2api_automation_requested_count": 2,
                        },
                    }
                ],
            )
        )

    def test_registration_callback_counts_retries_as_one_account(self):
        from core.sub2api_automation import registration_completion_event

        context = {
            "sub2api_automation_requested_count": 1,
        }
        event = registration_completion_event(
            "req-retry",
            [
                {
                    "id": 10,
                    "root_job_id": 10,
                    "retry_attempt": 0,
                    "status": "failed",
                    "provider_context": context,
                },
                {
                    "id": 11,
                    "root_job_id": 10,
                    "retry_attempt": 1,
                    "status": "success",
                    "provider_context": context,
                },
            ],
        )

        self.assertEqual(event["requested_count"], 1)
        self.assertEqual(event["succeeded_count"], 1)
        self.assertEqual(event["failed_count"], 0)

    def test_reauthorization_completion_contains_no_credentials(self):
        from core.sub2api_automation import reauthorization_completion_event

        event = reauthorization_completion_event(
            {
                "status": "success",
                "provider_context": {
                    "sub2api_automation_kind": "reauthorization",
                    "sub2api_automation_request_id": "req-2",
                    "sub2api_account_id": "42",
                    "sub2api_automation_email": "person@example.com",
                },
            }
        )

        self.assertEqual(event["status"], "succeeded")
        self.assertEqual(event["account_id"], 42)
        self.assertNotIn("access_token", event)
        self.assertNotIn("refresh_token", event)

    def test_completion_callback_url_maps_to_reauthorization_completion(self):
        from core.sub2api_automation import _completion_callback_url

        self.assertEqual(
            _completion_callback_url(
                "https://sub2.example/api/v1/integrations/openai/auto-provision/callback",
                "reauthorization/completion",
            ),
            "https://sub2.example/api/v1/integrations/openai/auto-provision/reauthorization/completion",
        )

    @patch(
        "webui.sub2api_automation_api.registration_jobs_api.create_registration_jobs"
    )
    @patch(
        "webui.sub2api_automation_api.db.list_jobs_for_automation_request",
        return_value=[],
    )
    def test_provision_route_forwards_automation_context(self, list_jobs, create_jobs):
        create_jobs.return_value = ({"ok": True, "submitted": 2}, 200)

        response = self.client.post(
            "/api/automation/provision",
            json={
                "request_id": "req-3",
                "count": 2,
                "workers": 2,
                "email_source": "outlook",
                "callback_url": "https://sub2.example/api/v1/integrations/openai/auto-provision/callback",
            },
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.get_json()["submitted"], 2)
        self.assertEqual(
            create_jobs.call_args.kwargs["automation_context"],
            {
                "sub2api_automation_request_id": "req-3",
                "sub2api_automation_kind": "registration",
                "sub2api_callback_url": "https://sub2.example/api/v1/integrations/openai/auto-provision/callback",
            },
        )
        list_jobs.assert_called_once_with("req-3")

    @patch("webui.sub2api_automation_api.submit_codex_retry_for_account")
    @patch("webui.sub2api_automation_api.db.get_account_by_email")
    @patch(
        "webui.sub2api_automation_api.db.list_jobs_for_automation_request",
        return_value=[],
    )
    @patch("webui.sub2api_automation_api.codex_config.CODEX_AUTH_URL_SOURCE", "sub2")
    def test_reauthorize_route_rejects_account_email_mismatch(
        self, list_jobs, get_account, submit_retry
    ):
        get_account.return_value = {
            "id": 44,
            "email": "person@example.com",
            "access_token": "access-token",
        }

        response = self.client.post(
            "/api/automation/reauthorize",
            json={
                "request_id": "req-4",
                "account_id": 45,
                "email": "person@example.com",
                "callback_url": "https://sub2.example/api/v1/integrations/openai/auto-provision/callback",
            },
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn("same account", response.get_json()["error"])
        list_jobs.assert_not_called()
        submit_retry.assert_not_called()

    @patch("webui.sub2api_automation_api.submit_codex_retry_for_account")
    @patch("webui.sub2api_automation_api.db.get_account_by_email")
    @patch(
        "webui.sub2api_automation_api.db.list_jobs_for_automation_request",
        return_value=[],
    )
    @patch("webui.sub2api_automation_api.codex_config.CODEX_AUTH_URL_SOURCE", "sub2")
    def test_reauthorize_route_accepts_matching_account_and_forwards_context(
        self, list_jobs, get_account, submit_retry
    ):
        get_account.return_value = {
            "id": 44,
            "email": "person@example.com",
            "access_token": "access-token",
        }
        submit_retry.return_value = {"accepted": True, "job_id": 77}

        response = self.client.post(
            "/api/automation/reauthorize",
            json={
                "request_id": "req-accepted-reauth",
                "account_id": 44,
                "email": "person@example.com",
                "callback_url": "https://sub2.example/api/v1/integrations/openai/auto-provision/callback",
            },
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.get_json()["job_id"], 77)
        self.assertEqual(
            submit_retry.call_args.kwargs["automation_context"],
            {
                "sub2api_automation_request_id": "req-accepted-reauth",
                "sub2api_automation_kind": "reauthorization",
                "sub2api_callback_url": "https://sub2.example/api/v1/integrations/openai/auto-provision/callback",
                "sub2api_callback_path": "/api/v1/integrations/openai/auto-provision/reauthorization/callback",
                "sub2api_callback_event_id": "req-accepted-reauth:reauthorization:oauth-callback",
                "sub2api_account_id": "44",
                "sub2api_automation_email": "person@example.com",
            },
        )

    @patch("core.sub2api_automation._send_event", return_value=True)
    @patch("core.sub2api_automation.db.list_jobs_for_automation_request")
    @patch("core.sub2api_automation.db.get_job")
    def test_terminal_dispatcher_sends_registration_and_reauthorization_events(
        self, get_job, list_jobs, send_event
    ):
        from core.sub2api_automation import notify_job_completion

        registration_context = {
            "sub2api_automation_kind": "registration",
            "sub2api_automation_request_id": "req-terminal-registration",
            "sub2api_callback_url": "https://sub2.example/api/v1/integrations/openai/auto-provision/callback",
            "sub2api_automation_requested_count": 1,
        }
        registration_job = {
            "status": "success",
            "account_id": 101,
            "provider_context": registration_context,
        }
        reauthorization_context = {
            "sub2api_automation_kind": "reauthorization",
            "sub2api_automation_request_id": "req-terminal-reauth",
            "sub2api_callback_url": "https://sub2.example/api/v1/integrations/openai/auto-provision/callback",
            "sub2api_account_id": "42",
            "sub2api_automation_email": "person@example.com",
        }
        reauthorization_job = {
            "status": "success",
            "provider_context": reauthorization_context,
        }

        get_job.side_effect = lambda job_id: (
            registration_job if int(job_id) == 101 else reauthorization_job
        )
        list_jobs.return_value = [registration_job]

        self.assertTrue(notify_job_completion(101))
        registration_event = send_event.call_args.args[1]
        self.assertEqual(registration_event["kind"], "registration")
        self.assertEqual(registration_event["succeeded_count"], 1)
        self.assertNotIn("access_token", registration_event)

        send_event.reset_mock()
        self.assertTrue(notify_job_completion(202))
        reauthorization_event = send_event.call_args.args[1]
        self.assertEqual(reauthorization_event["kind"], "reauthorization")
        self.assertEqual(
            send_event.call_args.kwargs["endpoint"], "reauthorization/completion"
        )
        self.assertEqual(reauthorization_event["account_id"], 42)
        self.assertNotIn("refresh_token", reauthorization_event)

    @patch(
        "webui.sub2api_automation_api.registration_jobs_api.create_registration_jobs"
    )
    @patch(
        "webui.sub2api_automation_api.db.list_jobs_for_automation_request",
        return_value=[],
    )
    def test_registration_full_cycle_reports_partial_success_for_replenishment(
        self, list_jobs, create_jobs
    ):
        from core.sub2api_automation import registration_completion_event

        create_jobs.return_value = ({"ok": True, "submitted": 2}, 200)
        response = self.client.post(
            "/api/automation/provision",
            json={
                "request_id": "req-full-registration-1",
                "count": 2,
                "workers": 2,
                "email_source": "outlook",
                "callback_url": "https://sub2.example/api/v1/integrations/openai/auto-provision/callback",
            },
            headers=self.headers,
        )

        self.assertEqual(response.status_code, 202)
        context = create_jobs.call_args.kwargs["automation_context"]
        context["sub2api_automation_requested_count"] = 2
        jobs = [
            {"status": "success", "account_id": 101, "provider_context": context},
            {"status": "running", "account_id": None, "provider_context": context},
        ]
        self.assertIsNone(
            registration_completion_event("req-full-registration-1", jobs)
        )

        jobs[1] = {
            "status": "failed",
            "account_id": None,
            "provider_context": context,
        }
        event = registration_completion_event("req-full-registration-1", jobs)
        self.assertEqual(event["requested_count"], 2)
        self.assertEqual(event["succeeded_count"], 1)
        self.assertEqual(event["failed_count"], 1)
        self.assertEqual(event["pending_count"], 0)
        self.assertNotIn("access_token", event)
        self.assertNotIn("refresh_token", event)

        create_jobs.return_value = ({"ok": True, "submitted": 1}, 200)
        response = self.client.post(
            "/api/automation/provision",
            json={
                "request_id": "req-full-registration-2",
                "count": 1,
                "workers": 1,
                "email_source": "outlook",
                "callback_url": "https://sub2.example/api/v1/integrations/openai/auto-provision/callback",
            },
            headers=self.headers,
        )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(create_jobs.call_count, 2)
        list_jobs.assert_any_call("req-full-registration-1")
        list_jobs.assert_any_call("req-full-registration-2")

    @patch("core.codex_oauth._sub2_codex_request_json", return_value={"ok": True})
    @patch(
        "config.sub2api.get_sub2api_account_defaults",
        return_value={"model_mapping": {}, "priority": 1, "group_ids": []},
    )
    @patch("config.sub2api.SUB2API_AUTOMATION_CALLBACK_SECRET", "callback-secret")
    def test_reauthorization_callback_forwards_complete_callback_url(
        self, defaults, request_json
    ):
        from core.codex_oauth import _submit_sub2_callback, sub2_callback_override

        with sub2_callback_override(
            "/api/v1/integrations/openai/auto-provision/reauthorization/callback",
            {
                "request_id": "req-full-reauth",
                "event_id": "req-full-reauth:reauthorization:oauth-callback",
                "account_id": 42,
                "email": "person@example.com",
            },
        ):
            result = _submit_sub2_callback(
                "http://localhost:1455/auth/callback?code=authorization-code&state=oauth-state",
                session_id="session-42",
                redirect_uri="http://localhost:1455/auth/callback",
            )

        self.assertEqual(result, {"ok": True})
        method, path, body = request_json.call_args.args
        self.assertEqual(method, "POST")
        self.assertEqual(
            path,
            "/api/v1/integrations/openai/auto-provision/reauthorization/callback",
        )
        self.assertEqual(
            body,
            {
                "session_id": "session-42",
                "callback_url": "http://localhost:1455/auth/callback?code=authorization-code&state=oauth-state",
                "redirect_uri": "http://localhost:1455/auth/callback",
                "request_id": "req-full-reauth",
                "event_id": "req-full-reauth:reauthorization:oauth-callback",
                "account_id": 42,
                "email": "person@example.com",
            },
        )
        self.assertNotIn("code", body)
        self.assertNotIn("state", body)
        self.assertEqual(
            request_json.call_args.kwargs["extra_headers"],
            {"X-Sub2API-Automation-Secret": "callback-secret"},
        )
        self.assertNotIn("access_token", body)
        self.assertNotIn("refresh_token", body)

    @patch("core.codex_retry_service.db.update_account_codex_status")
    @patch("core.account_network.resolve_rotating_proxy", return_value=None)
    @patch("core.nordvpn_wireguard.is_per_profile_proxy_enabled", return_value=False)
    @patch("core.codex_retry_service.check_stop_requested")
    @patch("core.codex_retry_service.db.get_account_by_email", return_value={"email": "person@example.com"})
    @patch("config.reload_all")
    @patch("core.codex_oauth.run_codex_oauth")
    def test_reauthorization_worker_uses_force_and_callback_context(
        self,
        run_oauth,
        reload_all,
        get_account,
        check_stop,
        per_profile_proxy,
        rotating_proxy,
        update_status,
    ):
        from core import codex_oauth
        from core.codex_retry_service import run_worker

        observed = {}

        def fake_run_codex_oauth(email, **kwargs):
            observed["email"] = email
            observed["kwargs"] = kwargs
            observed["override"] = codex_oauth._SUB2_CALLBACK_OVERRIDE.get()
            return {"status": "success", "ok": True, "message": "mock success"}

        run_oauth.side_effect = fake_run_codex_oauth
        with TemporaryDirectory() as temp_dir:
            result = run_worker(
                "person@example.com",
                target_log_path=Path(temp_dir) / "reauthorization.log",
                sub2_callback_context={
                    "path": "/api/v1/integrations/openai/auto-provision/reauthorization/callback",
                    "request_id": "req-worker",
                    "event_id": "req-worker:reauthorization:oauth-callback",
                    "account_id": 42,
                    "email": "person@example.com",
                },
            )

        self.assertTrue(result["ok"])
        self.assertEqual(observed["email"], "person@example.com")
        self.assertTrue(observed["kwargs"]["force"])
        self.assertEqual(
            observed["override"]["path"],
            "/api/v1/integrations/openai/auto-provision/reauthorization/callback",
        )
        self.assertEqual(
            observed["override"]["extra_body"]["request_id"], "req-worker"
        )
        run_oauth.assert_called_once()
        update_status.assert_called_once_with("person@example.com", "success", None)


if __name__ == "__main__":
    unittest.main()
