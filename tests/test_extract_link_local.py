import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core import extract_link_service


class LocalExtractLinkTests(unittest.TestCase):
    def test_extract_workspace_renders_free_plus_and_uses_matching_button_ids(self):
        root = Path(__file__).resolve().parents[1]
        for name in ("index.html", "index_legacy.html"):
            source = (root / "webui" / "templates" / name).read_text(encoding="utf-8")
            self.assertIn("const planLabel = acc.plus_trial_eligible ? 'Free Plus'", source)
            self.assertIn("$('#btnExtractWorkspaceRun').disabled", source)
            self.assertIn("$('#btnExtractWorkspaceRefresh')?.addEventListener", source)
            self.assertIn("$('#btnExtractWorkspaceSelectAll')?.addEventListener", source)
            self.assertNotIn("$('#extractWorkspaceRun').disabled", source)

    def test_auto_mode_uses_local_without_remote_configuration(self):
        with patch.object(extract_link_service, "_runtime_setting", side_effect=lambda name, default=None: {
            "EXTRACT_LINK_MODE": "auto",
            "EXTRACT_LINK_API_BASE": "",
            "EXTRACT_LINK_CDK": "",
        }.get(name, default)):
            self.assertEqual(extract_link_service._mode(), "local")

    def test_auto_mode_always_uses_local_pay153_even_with_remote_configuration(self):
        with patch.object(extract_link_service, "_runtime_setting", side_effect=lambda name, default=None: {
            "EXTRACT_LINK_MODE": "auto",
            "EXTRACT_LINK_API_BASE": "https://extract.example.test",
            "EXTRACT_LINK_CDK": "test-cdk",
        }.get(name, default)):
            self.assertEqual(extract_link_service._mode(), "local")

    def test_explicit_remote_mode_remains_available(self):
        with patch.object(extract_link_service, "_runtime_setting", side_effect=lambda name, default=None: {
            "EXTRACT_LINK_MODE": "remote",
            "EXTRACT_LINK_API_BASE": "https://extract.example.test",
            "EXTRACT_LINK_CDK": "test-cdk",
        }.get(name, default)):
            self.assertEqual(extract_link_service._mode(), "remote")

    def test_local_checkout_maps_short_link_to_existing_result_contract(self):
        fake_result = SimpleNamespace(
            long_url="https://chatgpt.com/checkout/openai_ie/oaics_test",
            cs_id="oaics_test",
            processor_entity="openai_ie",
            billing_country="PH",
            currency="PHP",
            amount_verification="verified_zero",
            amount_minor=0,
            amount_currency="PHP",
        )
        fake_extractor = SimpleNamespace(extract=lambda: fake_result)
        settings = {
            "EXTRACT_LINK_LOCAL_BILLING_COUNTRY": "PH",
            "EXTRACT_LINK_LOCAL_CURRENCY": "PHP",
            "EXTRACT_LINK_LOCAL_PLAN_NAME": "chatgptplusplan",
            "EXTRACT_LINK_LOCAL_PROMO_CAMPAIGN_ID": "plus-1-month-free",
            "EXTRACT_LINK_LOCAL_APPLY_PROMO": "true",
            "EXTRACT_LINK_LOCAL_CHECKOUT_ATTEMPTS": "3",
            "EXTRACT_LINK_LOCAL_UPDATE_ATTEMPTS": "3",
            "EXTRACT_LINK_REQUEST_TIMEOUT": "30",
        }
        with patch.object(extract_link_service, "_runtime_setting", side_effect=lambda name, default=None: settings.get(name, default)), patch(
            "core.pay153_checkout_extractor.parse_credentials", return_value=SimpleNamespace()
        ), patch("core.pay153_checkout_extractor.CheckoutExtractor", return_value=fake_extractor):
            result = extract_link_service._run_local_checkout(
                token="token", link_type="ph_short", proxy="http://127.0.0.1:8000", log=lambda _message: None,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["link_type"], "ph_short")
        self.assertEqual(result["result"]["long_url"], fake_result.long_url)
        self.assertEqual(result["result"]["copy_paste"], fake_result.long_url)
        self.assertEqual(result["result"]["payment_method"], "ph_short")

    def test_local_checkout_uses_selected_pay153_provider_workflow(self):
        provider_result = {
            "provider_redirect_url": "https://pay.example.test/upi/123",
            "checkout_session_id": "cs_live_test",
            "processor_entity": "openai_ie",
            "checkout_country": "IN",
            "checkout_currency": "INR",
            "checkout_amount": 0,
            "amount_verification": "verified_zero",
            "qr_data": "upi-payload",
        }
        with patch(
            "core.pay153_provider_workflow.run_provider_checkout",
            return_value=provider_result,
        ) as workflow:
            result = extract_link_service._run_local_checkout(
                token="token",
                link_type="upi",
                proxy="http://127.0.0.1:8000",
                payment_proxy="http://127.0.0.1:8001",
                promotion_proxy="http://127.0.0.1:8002",
                log=lambda _message: None,
            )

        workflow.assert_called_once_with(
            "token",
            "upi",
            entry_proxy="http://127.0.0.1:8000",
            payment_proxy="http://127.0.0.1:8001",
            promotion_proxy="http://127.0.0.1:8002",
            log=workflow.call_args.kwargs["log"],
        )
        self.assertEqual(result["link_type"], "upi")
        self.assertEqual(result["result"]["qr_data"], "upi-payload")

    def test_local_checkout_accepts_momo_qr_only_provider_result(self):
        with patch(
            "core.pay153_provider_workflow.run_provider_checkout",
            return_value={"qr_data": "momo-payload", "checkout_amount": 0},
        ):
            result = extract_link_service._run_local_checkout(
                token="token",
                link_type="momo",
                proxy=None,
                log=lambda _message: None,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["link_type"], "momo")
        self.assertEqual(result["result"]["qr_data"], "momo-payload")
        self.assertEqual(result["result"]["long_url"], "")
        self.assertEqual(result["result"]["copy_paste"], "momo-payload")

    def test_enqueue_local_mode_does_not_require_cdk(self):
        with patch.object(extract_link_service, "_mode", return_value="local"), patch.object(
            extract_link_service, "_runtime_setting", side_effect=lambda name, default=None: default
        ), patch.object(extract_link_service.db, "claim_account_extract", return_value=True), patch.object(
            extract_link_service, "_prepare_proxy_inventory"
        ), patch.object(extract_link_service._EXECUTOR, "submit", return_value=SimpleNamespace()) as submit:
            result = extract_link_service.enqueue_account_extract(
                account_id=1,
                email="user@example.test",
                access_token="token",
                link_type=None,
            )

        self.assertTrue(result["accepted"])
        self.assertEqual(result["mode"], "local")
        self.assertEqual(result["link_type"], "ph_short")
        self.assertEqual(submit.call_args.kwargs["cdk"], "")

    def test_auto_local_worker_acquires_rotating_payment_and_promotion_proxies(self):
        leases = {
            "extract_link_payment": "http://proxy.payment:8080",
            "extract_link_promotion": "http://proxy.promotion:8080",
        }

        def resolve(proxy, *, scope, lane_id):
            self.assertIsNone(proxy)
            self.assertEqual(lane_id, 7)
            return leases[scope]

        with (
            patch.object(extract_link_service.db, "mark_account_extract_running", return_value=True),
            patch.object(extract_link_service, "_mode", return_value="local"),
            patch.object(extract_link_service, "resolve_rotating_proxy", side_effect=resolve) as resolve_mock,
            patch.object(
                extract_link_service,
                "_run_local_checkout",
                return_value={"ok": True, "status": "success", "result": {"copy_paste": "link"}},
            ) as local_checkout,
            patch.object(extract_link_service, "release_rotating_proxy") as release,
            patch.object(extract_link_service.db, "update_account_extract"),
            patch.object(extract_link_service._QUEUE_SLOTS, "release"),
        ):
            result = extract_link_service._run_extract(
                account_id=8,
                email="user@example.com",
                access_token="token",
                link_type="kakao",
                cdk="",
                trigger="manual",
                proxy_lane_id=7,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(resolve_mock.call_count, 2)
        local_checkout.assert_called_once_with(
            token="token",
            link_type="kakao",
            proxy=leases["extract_link_promotion"],
            payment_proxy=leases["extract_link_payment"],
            promotion_proxy=leases["extract_link_promotion"],
            log=local_checkout.call_args.kwargs["log"],
        )
        self.assertEqual(release.call_count, 2)

    def test_provider_proxy_roles_follow_pay153_route_stages(self):
        self.assertEqual(extract_link_service._proxy_roles("momo"), ("extract_link",))
        self.assertEqual(extract_link_service._proxy_roles("ph_short"), ("extract_link", "extract_link_promotion"))
        self.assertEqual(extract_link_service._proxy_roles("gcash"), ("extract_link", "extract_link_promotion"))
        self.assertEqual(extract_link_service._proxy_roles("paypal"), ("extract_link_promotion", "extract_link_payment"))
        self.assertEqual(extract_link_service._proxy_roles("upi"), ("extract_link_promotion", "extract_link_payment"))

    def test_proxy_pool_selection_normalizes_and_masks_credentials(self):
        from config import proxy as proxy_config

        with patch.object(proxy_config, "PROXY_POOL", [
            "http://pool-user:pool-secret@proxy.example.test:8080",
            "socks5://127.0.0.1:7897",
        ]):
            self.assertEqual(
                extract_link_service._request_proxy(None, pool_index="0"),
                "http://pool-user:pool-secret@proxy.example.test:8080",
            )
            options = extract_link_service.proxy_options()

        self.assertEqual([item["id"] for item in options["pool"]], ["0", "1"])
        self.assertNotIn("pool-secret", options["pool"][0]["label"])

    def test_rotating_inventory_prepares_each_provider_scope_once(self):
        from config import proxy as proxy_config

        original = set(extract_link_service._PROXY_INVENTORY_READY)
        extract_link_service._PROXY_INVENTORY_READY.clear()
        try:
            with patch.object(proxy_config, "ROTATING_PROXY_ENABLED", True), patch.object(
                extract_link_service, "prepare_rotating_proxy_lanes"
            ) as prepare:
                extract_link_service._prepare_proxy_inventory("momo")
                extract_link_service._prepare_proxy_inventory("paypal")

            self.assertEqual(
                [call.kwargs["scope"] for call in prepare.call_args_list],
                ["extract_link", "extract_link_promotion", "extract_link_payment"],
            )
        finally:
            extract_link_service._PROXY_INVENTORY_READY.clear()
            extract_link_service._PROXY_INVENTORY_READY.update(original)

    def test_webui_proxy_pool_route_is_authenticated_and_secret_free(self):
        from webui.app import create_app

        app = create_app(auth_code="test-auth")
        with patch.object(
            extract_link_service,
            "proxy_options",
            return_value={"rotating_enabled": True, "pool": [{"id": "0", "label": "http://***:***@proxy.test:8080"}]},
        ):
            denied = app.test_client().get("/api/extract-link/proxies")
            allowed = app.test_client().get(
                "/api/extract-link/proxies",
                headers={"X-Auth-Code": "test-auth"},
            )

        self.assertIn(denied.status_code, {401, 403})
        self.assertEqual(allowed.status_code, 200)
        self.assertNotIn("pool-secret", allowed.get_data(as_text=True))

    def test_bulk_route_forwards_role_specific_proxy_selection(self):
        from webui.app import create_app

        app = create_app(auth_code="test-auth")
        account = {
            "id": 42,
            "email": "free-plus@example.test",
            "access_token": "token",
            "current_plan_type": "free",
            "plan_type": "free",
            "plus_trial_eligible": True,
        }
        queued = {"accepted": True, "busy": False, "link_type": "gcash", "mode": "local", "future": SimpleNamespace()}
        with patch("webui.app.db.get_account", return_value=account), patch.object(
            extract_link_service, "enqueue_account_extract", return_value=queued
        ) as enqueue:
            response = app.test_client().post(
                "/api/accounts/extract-link-bulk",
                headers={"X-Auth-Code": "test-auth"},
                json={
                    "account_ids": [42],
                    "link_type": "gcash",
                    "proxy_pool_index": "0",
                    "promotion_proxy_pool_index": "1",
                },
            )

        self.assertEqual(response.status_code, 202)
        self.assertEqual(enqueue.call_args.kwargs["proxy_pool_index"], "0")
        self.assertEqual(enqueue.call_args.kwargs["promotion_proxy_pool_index"], "1")

    def test_enqueue_local_accepts_selected_payment_rail(self):
        with patch.object(extract_link_service, "_mode", return_value="local"), patch.object(
            extract_link_service.db, "claim_account_extract", return_value=True
        ), patch.object(extract_link_service, "_prepare_proxy_inventory"), patch.object(
            extract_link_service._EXECUTOR, "submit", return_value=SimpleNamespace()
        ) as submit:
            result = extract_link_service.enqueue_account_extract(
                account_id=1,
                email="user@example.test",
                access_token="token",
                link_type="pix",
            )

        self.assertTrue(result["accepted"])
        self.assertEqual(result["link_type"], "pix")
        self.assertEqual(submit.call_args.kwargs["link_type"], "pix")

    def test_payment_method_options_are_catalogued_for_local_pay153(self):
        with patch.object(extract_link_service, "_mode", return_value="local"), patch(
            "core.pay153_provider_workflow._upi_enabled", return_value=False
        ):
            result = extract_link_service.payment_method_options()

        ids = [item["id"] for item in result["payment_methods"]]
        self.assertEqual(result["default_payment_method"], "ph_short")
        self.assertIn("pix", ids)
        self.assertIn("paypal", ids)
        self.assertNotIn("twint", ids)

    def test_kakao_checkout_carries_promo_on_create_like_pay153(self):
        from core.pay153_provider_workflow import _checkout_payload

        kakao = _checkout_payload("kakao", "KR", "KRW", "plus-1-month-free")
        pix = _checkout_payload("pix", "BR", "BRL", "plus-1-month-free")
        self.assertEqual(kakao["checkout_ui_mode"], "custom")
        self.assertEqual(kakao["promo_campaign"]["promo_campaign_id"], "plus-1-month-free")
        self.assertNotIn("promo_campaign", pix)

    def test_webui_payment_method_options_route_is_authenticated_and_secret_free(self):
        from webui.app import create_app

        app = create_app(auth_code="test-auth")
        with patch.object(
            extract_link_service,
            "payment_method_options",
            return_value={"mode": "local", "payment_methods": [{"id": "pix", "label": "PIX"}], "default_payment_method": "pix"},
        ):
            denied = app.test_client().get("/api/extract-link/options")
            allowed = app.test_client().get(
                "/api/extract-link/options",
                headers={"X-Auth-Code": "test-auth"},
            )

        self.assertIn(denied.status_code, {401, 403})
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(allowed.get_json()["payment_methods"][0]["id"], "pix")
        self.assertNotIn("api_key", allowed.get_data(as_text=True).lower())


if __name__ == "__main__":
    unittest.main()
