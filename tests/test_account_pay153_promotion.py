import unittest
from contextlib import contextmanager
from unittest.mock import Mock, patch


class AccountPay153PromotionTests(unittest.TestCase):
    def test_registration_flow_reuses_proxy_and_browser_for_promotion(self):
        from core import account_pay153_promotion

        browser_transport = Mock()
        raw = {
            "ok": True,
            "status": "success",
            "result": {
                "checkout_session_id": "cs_live_browser_promotion",
                "amount_verification": "verified_zero",
            },
        }
        plan = {"ok": True, "current_plan_type": "free", "plus_trial_eligible": False}
        with (
            patch.object(account_pay153_promotion, "required_account_proxy") as required_proxy,
            patch.object(
                account_pay153_promotion.extract_link_service,
                "_run_local_checkout",
                return_value=raw,
            ) as checkout,
            patch.object(account_pay153_promotion.db, "update_account_pay153_promotion"),
        ):
            result = account_pay153_promotion.run_account_pay153_promotion_probe(
                account_id=7,
                email="free@example.com",
                access_token="token",
                plan_result=plan,
                checkout_proxy="http://registration-proxy:8080",
                browser_transport=browser_transport,
            )

        self.assertTrue(result["ok"])
        required_proxy.assert_not_called()
        self.assertEqual(checkout.call_args.kwargs["proxy"], "http://registration-proxy:8080")
        self.assertIs(checkout.call_args.kwargs["browser_transport"], browser_transport)
    def test_non_free_account_skips_promotion(self):
        from core.account_pay153_promotion import run_account_pay153_promotion_probe

        plan = {"ok": True, "current_plan_type": "plus", "plus_trial_eligible": False}
        with patch("core.account_pay153_promotion.db.update_account_pay153_promotion") as persist:
            result = run_account_pay153_promotion_probe(
                account_id=1,
                email="paid@example.com",
                access_token="token",
                plan_result=plan,
            )

        self.assertEqual(result["status"], "skipped")
        self.assertTrue(result["ok"])
        persist.assert_called_once()

    def test_free_account_uses_verified_vietnam_proxy_and_pay153_checkout(self):
        from core import account_pay153_promotion

        @contextmanager
        def vietnam_proxy(*_args, **_kwargs):
            yield "http://vn-proxy:8080", "rotating_proxy"

        raw = {
            "ok": True,
            "status": "success",
            "link_type": "ph_short",
            "result": {
                "checkout_session_id": "cs_live_promotion_123",
                "amount_verification": "verified_zero",
            },
        }
        plan = {"ok": True, "current_plan_type": "free", "plus_trial_eligible": False}
        with (
            patch.object(account_pay153_promotion, "required_account_proxy", vietnam_proxy),
            patch.object(account_pay153_promotion.extract_link_service, "_run_local_checkout", return_value=raw) as checkout,
            patch.object(account_pay153_promotion.db, "update_account_pay153_promotion") as persist,
        ):
            result = account_pay153_promotion.run_account_pay153_promotion_probe(
                account_id=7,
                email="free@example.com",
                access_token="token",
                plan_result=plan,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["checkout_session_kind"], "cs_live")
        self.assertEqual(result["promotion_proxy_country"], "VN")
        checkout.assert_called_once_with(
            token="token",
            link_type="ph_short",
            proxy="http://vn-proxy:8080",
            promotion_proxy="http://vn-proxy:8080",
            browser_transport=None,
            checkout_proxy_country="VN",
            promotion_proxy_country="VN",
            verify_proxy_country=True,
            apply_promo=True,
            log=unittest.mock.ANY,
        )
        persist.assert_called_once_with(7, result)

    def test_non_zero_promotion_is_not_reported_as_success(self):
        from core import account_pay153_promotion

        @contextmanager
        def vietnam_proxy(*_args, **_kwargs):
            yield "http://vn-proxy:8080", "proxy_pool"

        raw = {
            "ok": True,
            "status": "success",
            "result": {
                "checkout_session_id": "cs_live_promotion_123",
                "amount_verification": "verified_nonzero",
            },
        }
        plan = {"ok": True, "current_plan_type": "free", "plus_trial_eligible": False}
        with (
            patch.object(account_pay153_promotion, "required_account_proxy", vietnam_proxy),
            patch.object(account_pay153_promotion.extract_link_service, "_run_local_checkout", return_value=raw),
            patch.object(account_pay153_promotion.db, "update_account_pay153_promotion"),
        ):
            result = account_pay153_promotion.run_account_pay153_promotion_probe(
                account_id=7,
                email="free@example.com",
                access_token="token",
                plan_result=plan,
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "failed")
        self.assertIn("zero", result["error"].lower())


if __name__ == "__main__":
    unittest.main()
