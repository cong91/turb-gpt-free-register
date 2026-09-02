import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from core import codex_retry_service


class CodexCredentialDispatchTests(unittest.TestCase):
    def setUp(self):
        self._wireguard_disabled = patch(
            "core.nordvpn_wireguard.is_per_profile_proxy_enabled",
            return_value=False,
        )
        self._wireguard_disabled.start()
        self.addCleanup(self._wireguard_disabled.stop)
        self._rotating_proxy_disabled = patch(
            "core.account_network.resolve_rotating_proxy",
            return_value=None,
        )
        self._rotating_proxy_disabled.start()
        self.addCleanup(self._rotating_proxy_disabled.stop)

    @patch("core.registration_service._deactivate_job")
    @patch("core.registration_service.codex_retry_service.release")
    @patch("core.registration_service.db.update_job")
    @patch("core.registration_service.db.update_account_codex_status")
    @patch("core.registration_service.db.get_job")
    @patch("core.registration_service._activate_job", return_value=True)
    @patch("config.roxybrowser.REGISTRATION_DRIVER", "cloak")
    @patch("core.registration_service.codex_retry_service.run_worker")
    def test_legacy_browser_registration_auto_job_is_skipped_without_opening_browser(
        self,
        run_worker,
        _activate,
        get_job,
        update_status,
        update_job,
        release,
        _deactivate,
    ):
        from core import registration_service

        get_job.return_value = {
            "id": 77,
            "status": "pending",
            "provider_context": {"trigger": "registration_auto_free"},
        }

        registration_service._run_codex_retry_job(
            77,
            "legacy-codex.log",
            "free-browser@example.com",
            7,
        )

        run_worker.assert_not_called()
        update_status.assert_called_once_with(
            "free-browser@example.com",
            "skipped",
            "旧的 registration_auto_free 自动 Codex OAuth 任务来自 cloak 注册，没有注册 browser 可复用，已跳过，禁止另起浏览器；新注册必须由 registration worker 同步执行",
        )
        update_job.assert_called_once()
        self.assertEqual(update_job.call_args.kwargs["status"], "cancelled")
        release.assert_called_once_with("free-browser@example.com")

    @patch("core.registration_service._deactivate_job")
    @patch("core.registration_service.codex_retry_service.release")
    @patch("core.registration_service.db.update_job")
    @patch("core.registration_service.db.update_account_codex_status")
    @patch("core.registration_service.db.get_account", return_value={
        "id": 7,
        "extra_json": '{"registration_driver":"cloak"}',
    })
    @patch("core.registration_service.db.get_job")
    @patch("core.registration_service._activate_job", return_value=True)
    @patch("config.roxybrowser.REGISTRATION_DRIVER", "protocol")
    @patch("core.registration_service.codex_retry_service.run_worker")
    def test_legacy_browser_job_is_skipped_using_persisted_driver_after_config_change(
        self,
        run_worker,
        _activate,
        get_job,
        _get_account,
        update_status,
        update_job,
        release,
        _deactivate,
    ):
        from core import registration_service

        get_job.return_value = {
            "id": 78,
            "status": "pending",
            "provider_context": {"trigger": "registration_auto_free"},
        }

        registration_service._run_codex_retry_job(
            78,
            "legacy-codex.log",
            "free-browser@example.com",
            7,
        )

        run_worker.assert_not_called()
        self.assertIn("cloak 注册", update_status.call_args.args[2])
        update_job.assert_called_once()
        release.assert_called_once_with("free-browser@example.com")

    @patch("core.codex_oauth.run_codex_oauth")
    @patch("core.codex_retry_service.db.update_account_codex_status")
    @patch("core.codex_retry_service.db.get_account_by_email")
    def test_retry_worker_passes_imported_credentials_to_oauth(
        self,
        get_account,
        _update_status,
        run_codex_oauth,
    ):
        get_account.return_value = {
            "email": "user@example.com",
            "registration_password": "openai-password",
            "totp_secret": "JBSWY3DPEHPK3PXP",
            "codex_login_mode": "credentials",
        }
        run_codex_oauth.return_value = {"ok": True, "status": "success"}

        with tempfile.TemporaryDirectory() as temp_dir:
            result = codex_retry_service.run_worker(
                "user@example.com",
                target_log_path=Path(temp_dir) / "retry.log",
            )

        self.assertTrue(result["ok"])
        kwargs = run_codex_oauth.call_args.kwargs
        self.assertIn("credentials", kwargs)
        self.assertTrue(kwargs["fresh_browser_profile"])
        self.assertNotIn("existing_driver", kwargs)
        self.assertNotIn("existing_opened", kwargs)
        self.assertNotIn("existing_browser", kwargs)
        self.assertNotIn("existing_context", kwargs)
        self.assertNotIn("existing_page", kwargs)
        self.assertNotIn("existing_session_info", kwargs)
        credentials = kwargs["credentials"]
        self.assertEqual(credentials.email, "user@example.com")
        self.assertEqual(credentials.password, "openai-password")
        self.assertEqual(credentials.totp_secret, "JBSWY3DPEHPK3PXP")
        self.assertNotIn("openai-password", str(result))
        self.assertNotIn("JBSWY3DPEHPK3PXP", str(result))

    @patch("core.account_network.resolve_rotating_proxy", return_value="http://203.0.113.50:8080")
    @patch("core.codex_oauth.run_codex_oauth")
    @patch("core.codex_retry_service.db.update_account_codex_status")
    @patch("core.codex_retry_service.db.get_account_by_email")
    def test_retry_worker_uses_scoped_rotating_lease(
        self,
        get_account,
        _update_status,
        run_codex_oauth,
        resolve_proxy,
    ):
        get_account.return_value = {"email": "user@example.com"}
        run_codex_oauth.return_value = {"ok": True, "status": "success"}

        with tempfile.TemporaryDirectory() as temp_dir:
            result = codex_retry_service.run_worker(
                "user@example.com",
                proxy_lane_id=6,
                target_log_path=Path(temp_dir) / "retry.log",
            )

        self.assertTrue(result["ok"])
        resolve_proxy.assert_called_once_with(
            None,
            scope="codex_retry",
            lane_id=6,
        )
        self.assertEqual(
            run_codex_oauth.call_args.kwargs["proxy"],
            "http://203.0.113.50:8080",
        )

    @patch("core.nordvpn_wireguard.is_per_profile_proxy_enabled", return_value=True)
    @patch("core.nordvpn_wireguard.proxy_for_registration")
    @patch("core.codex_oauth.run_codex_oauth")
    @patch("core.codex_retry_service.db.update_account_codex_status")
    @patch("core.codex_retry_service.db.get_account_by_email")
    def test_retry_worker_uses_wireguard_lease_instead_of_proxy_pool(
        self,
        get_account,
        _update_status,
        run_codex_oauth,
        proxy_for_registration,
        _wireguard_enabled,
    ):
        get_account.return_value = {"email": "user@example.com"}
        run_codex_oauth.return_value = {"ok": True, "status": "success"}

        @contextmanager
        def wireguard_context():
            yield "socks5://127.0.0.1:25000"

        proxy_for_registration.side_effect = wireguard_context

        with tempfile.TemporaryDirectory() as temp_dir:
            result = codex_retry_service.run_worker(
                "user@example.com",
                target_log_path=Path(temp_dir) / "retry.log",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(
            run_codex_oauth.call_args.kwargs["proxy"],
            "socks5://127.0.0.1:25000",
        )
        proxy_for_registration.assert_called_once_with()

    @patch("core.account_network.resolve_rotating_proxy", return_value="http://rotating")
    @patch("core.nordvpn_wireguard.is_per_profile_proxy_enabled", return_value=True)
    @patch("core.nordvpn_wireguard.proxy_for_registration")
    @patch("core.codex_oauth.run_codex_oauth")
    @patch("core.codex_retry_service.db.update_account_codex_status")
    @patch("core.codex_retry_service.db.get_account_by_email")
    def test_retry_worker_prefers_wireguard_when_rotating_proxy_is_available(
        self,
        get_account,
        _update_status,
        run_codex_oauth,
        proxy_for_registration,
        _wireguard_enabled,
        resolve_proxy,
    ):
        get_account.return_value = {"email": "user@example.com"}
        run_codex_oauth.return_value = {"ok": True, "status": "success"}

        @contextmanager
        def wireguard_context():
            yield "socks5://127.0.0.1:25000"

        proxy_for_registration.side_effect = wireguard_context

        with tempfile.TemporaryDirectory() as temp_dir:
            result = codex_retry_service.run_worker(
                "user@example.com",
                proxy_lane_id=6,
                target_log_path=Path(temp_dir) / "retry.log",
            )

        self.assertTrue(result["ok"])
        resolve_proxy.assert_not_called()
        proxy_for_registration.assert_called_once_with()
        self.assertEqual(
            run_codex_oauth.call_args.kwargs["proxy"],
            "socks5://127.0.0.1:25000",
        )

    @patch("core.account_network.resolve_rotating_proxy", return_value="http://rotating")
    @patch("core.nordvpn_wireguard.is_per_profile_proxy_enabled", return_value=True)
    @patch("core.nordvpn_wireguard.proxy_for_registration")
    @patch("core.codex_oauth.run_codex_oauth")
    @patch("core.codex_retry_service.db.update_account_codex_status")
    @patch("core.codex_retry_service.db.get_account_by_email")
    def test_retry_worker_falls_back_to_rotating_proxy_when_wireguard_is_unavailable(
        self,
        get_account,
        _update_status,
        run_codex_oauth,
        proxy_for_registration,
        _wireguard_enabled,
        resolve_proxy,
    ):
        get_account.return_value = {"email": "user@example.com"}
        run_codex_oauth.return_value = {"ok": True, "status": "success"}

        @contextmanager
        def unavailable_wireguard():
            raise RuntimeError("wireguard unavailable")
            yield None

        proxy_for_registration.side_effect = unavailable_wireguard

        with tempfile.TemporaryDirectory() as temp_dir:
            result = codex_retry_service.run_worker(
                "user@example.com",
                proxy_lane_id=6,
                target_log_path=Path(temp_dir) / "retry.log",
            )

        self.assertTrue(result["ok"])
        proxy_for_registration.assert_called_once_with()
        resolve_proxy.assert_called_once_with(
            None,
            scope="codex_retry",
            lane_id=6,
        )
        self.assertEqual(
            run_codex_oauth.call_args.kwargs["proxy"],
            "http://rotating",
        )

    @patch("core.codex_oauth.run_codex_oauth")
    @patch("core.codex_retry_service.db.update_account_codex_status")
    @patch("core.codex_retry_service.db.get_account_by_email")
    def test_retry_worker_uses_saved_credentials_for_registered_accounts(
        self,
        get_account,
        _update_status,
        run_codex_oauth,
    ):
        get_account.return_value = {
            "email": "user@example.com",
            "registration_password": "openai-password",
            "totp_secret": "JBSWY3DPEHPK3PXP",
            "email_source": "outlook",
        }
        run_codex_oauth.return_value = {"ok": True, "status": "success"}

        with tempfile.TemporaryDirectory() as temp_dir:
            codex_retry_service.run_worker(
                "user@example.com",
                target_log_path=Path(temp_dir) / "retry.log",
            )

        self.assertIn("credentials", run_codex_oauth.call_args.kwargs)
        credentials = run_codex_oauth.call_args.kwargs["credentials"]
        self.assertEqual(credentials.email, "user@example.com")
        self.assertEqual(credentials.password, "openai-password")
        self.assertEqual(credentials.totp_secret, "JBSWY3DPEHPK3PXP")

    @patch("core.codex_oauth.run_codex_oauth")
    @patch("core.codex_retry_service.db.update_account_codex_status")
    @patch("core.codex_retry_service.db.get_account_by_email")
    def test_retry_worker_keeps_email_otp_when_saved_credentials_are_incomplete(
        self,
        get_account,
        _update_status,
        run_codex_oauth,
    ):
        get_account.return_value = {
            "email": "user@example.com",
            "registration_password": "openai-password",
            "totp_secret": "",
            "email_source": "outlook",
        }
        run_codex_oauth.return_value = {"ok": True, "status": "success"}

        with tempfile.TemporaryDirectory() as temp_dir:
            codex_retry_service.run_worker(
                "user@example.com",
                target_log_path=Path(temp_dir) / "retry.log",
            )

        self.assertNotIn("credentials", run_codex_oauth.call_args.kwargs)

    @patch("core.codex_retry_service.time.sleep")
    @patch("core.codex_oauth.run_codex_oauth")
    @patch("core.codex_retry_service.db.update_account_codex_status")
    @patch("core.codex_retry_service.db.get_account_by_email")
    def test_retry_worker_retries_transient_browser_navigation_failure(
        self,
        get_account,
        _update_status,
        run_codex_oauth,
        sleep,
    ):
        get_account.return_value = {"email": "user@example.com"}
        run_codex_oauth.side_effect = [
            {"ok": False, "status": "failed", "message": "RuntimeError: net::ERR_EMPTY_RESPONSE"},
            {"ok": True, "status": "success"},
        ]

        with tempfile.TemporaryDirectory() as temp_dir:
            result = codex_retry_service.run_worker(
                "user@example.com",
                target_log_path=Path(temp_dir) / "retry.log",
            )

        self.assertTrue(result["ok"])
        self.assertEqual(run_codex_oauth.call_count, 2)
        sleep.assert_called()

    @patch("core.codex_oauth.run_codex_oauth")
    @patch("core.codex_retry_service.db.update_account_codex_status")
    @patch("core.codex_retry_service.db.get_account_by_email")
    def test_retry_worker_passes_selected_oauth_driver(
        self,
        get_account,
        _update_status,
        run_codex_oauth,
    ):
        get_account.return_value = {"email": "user@example.com"}
        run_codex_oauth.return_value = {"ok": True, "status": "success"}

        with tempfile.TemporaryDirectory() as temp_dir:
            codex_retry_service.run_worker(
                "user@example.com",
                oauth_driver="same_as_registration",
                target_log_path=Path(temp_dir) / "retry.log",
            )

        self.assertEqual(
            run_codex_oauth.call_args.kwargs["oauth_driver"],
            "same_as_registration",
        )


if __name__ == "__main__":
    unittest.main()
