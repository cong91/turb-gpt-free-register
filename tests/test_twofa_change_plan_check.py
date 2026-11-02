import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core.account_security import TwofaChangeInput
from core.twofa_change_plan_check import run_twofa_plan_check


class TwofaChangePlanCheckTests(unittest.TestCase):
    def setUp(self):
        self.item = TwofaChangeInput(
            email="CaseSensitive@example.com",
            password="fake-password",
            current_totp_secret="FAKETOTPSECRET",
        )
        self.driver = Mock()
        self.profile = SimpleNamespace(driver=self.driver)

    def test_success_forwards_credentials_and_records_plan_metadata(self):
        result = {"ok": True, "persisted": True}
        service_result = {
            "accepted": True,
            "ok": True,
            "current_plan_type": "plus",
            "plus_trial_eligible": True,
        }
        browser_transport = object()

        with (
            patch("core.plan_check_service.run_sync_plan_check", return_value=service_result) as check_plan,
            patch("core.account_export.BrowserPageTransport") as transport_type,
        ):
            transport_type.return_value = browser_transport
            run_twofa_plan_check(
                account_id=314,
                item=self.item,
                access_token="fake-access-token",
                profile=self.profile,
                result=result,
            )

        transport_type.assert_called_once_with(self.driver)
        check_plan.assert_called_once_with(
            account_id=314,
            email="CaseSensitive@example.com",
            access_token="fake-access-token",
            trigger="twofa_change",
            browser_transport=browser_transport,
        )
        self.assertEqual(
            result["plan_check"],
            {
                "accepted": True,
                "ok": True,
                "status": "success",
                "current_plan_type": "plus",
                "plus_trial_eligible": True,
                "error": None,
            },
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result["persisted"])
        self.assertNotIn("warning", result)

    def test_plan_failure_is_nested_and_warns_without_changing_primary_result(self):
        result = {"ok": True, "persisted": True}
        with (
            patch(
                "core.plan_check_service.run_sync_plan_check",
                return_value={"accepted": False, "ok": False, "error": "plan unavailable"},
            ),
            patch("core.account_export.BrowserPageTransport"),
        ):
            run_twofa_plan_check(
                account_id=314,
                item=self.item,
                access_token="fake-access-token",
                profile=self.profile,
                result=result,
            )

        self.assertEqual(
            result["plan_check"],
            {
                "accepted": False,
                "ok": False,
                "status": "failed",
                "current_plan_type": None,
                "plus_trial_eligible": None,
                "error": "plan unavailable",
            },
        )
        self.assertEqual(result["warning"], "plan unavailable")
        self.assertTrue(result["ok"])
        self.assertTrue(result["persisted"])

    def test_service_exception_becomes_nested_failure_and_warning(self):
        result = {"ok": True, "persisted": True}
        with (
            patch(
                "core.plan_check_service.run_sync_plan_check",
                side_effect=RuntimeError("service unavailable"),
            ),
            patch("core.account_export.BrowserPageTransport"),
        ):
            run_twofa_plan_check(
                account_id=314,
                item=self.item,
                access_token="fake-access-token",
                profile=self.profile,
                result=result,
            )

        self.assertFalse(result["plan_check"]["ok"])
        self.assertEqual(result["plan_check"]["status"], "failed")
        self.assertEqual(result["plan_check"]["error"], "RuntimeError: service unavailable")
        self.assertEqual(result["warning"], "RuntimeError: service unavailable")
        self.assertTrue(result["ok"])
        self.assertTrue(result["persisted"])

    def test_missing_access_token_returns_existing_failure_without_service_call(self):
        result = {"ok": True, "persisted": True}
        with (
            patch("core.plan_check_service.run_sync_plan_check") as check_plan,
            patch("core.account_export.BrowserPageTransport") as transport_type,
        ):
            run_twofa_plan_check(
                account_id=314,
                item=self.item,
                access_token="",
                profile=self.profile,
                result=result,
            )

        self.assertEqual(
            result["plan_check"],
            {"accepted": False, "error": "access token is missing"},
        )
        check_plan.assert_not_called()
        transport_type.assert_not_called()
        self.assertTrue(result["ok"])
        self.assertTrue(result["persisted"])

    def test_existing_warning_is_preserved_when_plan_check_fails(self):
        result = {"ok": True, "persisted": True, "warning": "earlier warning"}
        with (
            patch(
                "core.plan_check_service.run_sync_plan_check",
                return_value={"accepted": False, "ok": False, "error": "plan unavailable"},
            ),
            patch("core.account_export.BrowserPageTransport"),
        ):
            run_twofa_plan_check(
                account_id=314,
                item=self.item,
                access_token="fake-access-token",
                profile=self.profile,
                result=result,
            )

        self.assertEqual(result["warning"], "earlier warning")
        self.assertFalse(result["plan_check"]["ok"])
        self.assertTrue(result["ok"])
        self.assertTrue(result["persisted"])


if __name__ == "__main__":
    unittest.main()
