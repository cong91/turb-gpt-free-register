import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core import pay153_provider_workflow as workflow


class Pay153ProviderWorkflowTests(unittest.TestCase):
    def test_create_checkout_prefers_direct_oaics_id_over_nested_stripe_id(self):
        http = Mock()
        http.post.return_value = SimpleNamespace(
            status_code=200,
            text='{"checkout_session_id":"oaics_direct","checkout":{"id":"cs_live_nested"}}',
            json=lambda: {
                "checkout_session_id": "oaics_direct",
                "processor_entity": "openai_ie",
                "checkout": {"id": "cs_live_nested"},
            },
        )

        with patch.object(workflow.stripe_checkout, "build_http", return_value=http), patch.object(
            workflow, "_sentinel_headers", return_value={}
        ):
            data, returned_http = workflow._create_checkout(
                "token",
                {"checkout_ui_mode": "custom"},
                None,
                "device",
                "did",
                lambda _message: None,
            )

        self.assertIs(returned_http, http)
        self.assertEqual(data["checkout_session_id"], "oaics_direct")
        self.assertTrue(data["is_custom_checkout"])
        self.assertEqual(data["stripe_checkout_session_id"], "cs_live_nested")
        self.assertEqual(data["custom_checkout_session_id"], "oaics_direct")
        self.assertEqual(
            data["checkout_session_ids"],
            {"stripe": "cs_live_nested", "custom": "oaics_direct"},
        )

    def test_create_checkout_prefers_oaics_in_custom_payload_when_ids_are_nested(self):
        http = Mock()
        http.post.return_value = SimpleNamespace(
            status_code=200,
            text='{"checkout":{"id":"cs_live_nested"},"custom":{"id":"oaics_nested"}}',
            json=lambda: {"checkout": {"id": "cs_live_nested"}, "custom": {"id": "oaics_nested"}},
        )

        with patch.object(workflow.stripe_checkout, "build_http", return_value=http), patch.object(
            workflow, "_sentinel_headers", return_value={}
        ):
            data, _returned_http = workflow._create_checkout(
                "token",
                {"checkout_ui_mode": "custom"},
                None,
                "device",
                "did",
                lambda _message: None,
            )

        self.assertEqual(data["checkout_session_id"], "oaics_nested")
        self.assertTrue(data["is_custom_checkout"])

    def test_create_checkout_keeps_stripe_id_for_hosted_payload(self):
        http = Mock()
        http.post.return_value = SimpleNamespace(
            status_code=200,
            text='{"checkout":{"id":"cs_live_nested"},"custom":{"id":"oaics_nested"}}',
            json=lambda: {"checkout": {"id": "cs_live_nested"}, "custom": {"id": "oaics_nested"}},
        )

        with patch.object(workflow.stripe_checkout, "build_http", return_value=http), patch.object(
            workflow, "_sentinel_headers", return_value={}
        ):
            data, _returned_http = workflow._create_checkout(
                "token",
                {"checkout_ui_mode": "redirect"},
                None,
                "device",
                "did",
                lambda _message: None,
            )

        self.assertEqual(data["checkout_session_id"], "cs_live_nested")
        self.assertNotIn("is_custom_checkout", data)

    def test_momo_local_method_strategy_matches_pay153_retry_cycle(self):
        self.assertEqual(workflow.local_method_strategy("momo", 1), "late_promo")
        self.assertEqual(workflow.local_method_strategy("momo", 2), "inline")
        self.assertEqual(workflow.local_method_strategy("momo", 3), "standalone")
        self.assertEqual(workflow.local_method_strategy("momo", 4), "late_promo")

    def test_pix_local_method_strategy_matches_pay153_retry_cycle(self):
        self.assertEqual(workflow.local_method_strategy("pix", 1), "standalone")
        self.assertEqual(workflow.local_method_strategy("pix", 2), "late_promo")
        self.assertEqual(workflow.local_method_strategy("pix", 3), "inline")
        self.assertEqual(workflow.local_method_strategy("pix", 4), "standalone")

    def test_custom_confirm_accepts_momo_qr_action_without_redirect_url(self):
        http = Mock()
        http.post.side_effect = [
            SimpleNamespace(status_code=200, json=lambda: {"status": "success"}),
            SimpleNamespace(
                status_code=200,
                json=lambda: {
                    "status": "requires_action",
                    "next_action": {
                        "type": "momo_display_qr_code",
                        "momo_display_qr_code": {"data": "momo-qr-payload"},
                    },
                },
            ),
        ]

        with patch.object(workflow, "_sentinel_headers", return_value={}):
            result = workflow._custom_confirm_and_start(
                http, "token", "oaics_test", "openai_ie", "cpmt_momo", None, "device", "did",
            )

        self.assertEqual(result["started"]["next_action"]["momo_display_qr_code"]["data"], "momo-qr-payload")

    def test_momo_oaics_returns_qr_payment_material(self):
        state = {
            "checkout_amount_minor": 0,
            "currency": "VND",
            "custom_payment_methods": [
                {"id": "cpmt_other", "name": "Other payment"},
                {"id": "cpmt_momo", "name": "MoMo"},
            ],
        }
        started = {
            "next_action": {
                "type": "momo_display_qr_code",
                "momo_display_qr_code": {
                    "data": "000201010212momo",
                    "image_url_png": "https://pay.example.test/momo.png",
                    "expires_at": 1_800_000_000,
                },
            },
        }
        with patch.object(workflow, "_custom_checkout_state", return_value=state), patch.object(
            workflow, "_custom_checkout_taxes", return_value=state
        ), patch.object(
            workflow, "_custom_confirm_and_start", return_value={"confirmed": {}, "started": started}
        ) as confirm_and_start:
            result = workflow._run_custom_provider(
                "momo", "token", "oaics_test", "openai_ie", "VN", "VND", None,
                "device", "did", Mock(), Mock(), "plus-1-month-free", "user@example.test",
            )

        self.assertEqual(confirm_and_start.call_args.args[4], "cpmt_momo")
        self.assertEqual(result["qr_data"], "000201010212momo")
        self.assertEqual(result["qr_image_png"], "https://pay.example.test/momo.png")
        self.assertEqual(result["checkout_amount"], 0)
        self.assertEqual(result["amount_verification"], "verified_zero")

    def test_momo_oaics_requires_a_momo_method_when_multiple_methods_exist(self):
        state = {
            "checkout_amount_minor": 0,
            "currency": "VND",
            "custom_payment_methods": [
                {"id": "cpmt_first", "name": "Wallet A"},
                {"id": "cpmt_second", "name": "Wallet B"},
            ],
        }
        with patch.object(workflow, "_custom_checkout_state", return_value=state), patch.object(
            workflow, "_custom_checkout_taxes", return_value=state
        ), patch.object(workflow.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "MOMO custom payment method is not available"):
                workflow._run_custom_provider(
                    "momo", "token", "oaics_test", "openai_ie", "VN", "VND", None,
                    "device", "did", Mock(), Mock(), "plus-1-month-free", "user@example.test",
                )

    def test_momo_oaics_polls_until_its_custom_method_is_published(self):
        initial_state = {"checkout_amount_minor": 0, "currency": "VND", "custom_payment_methods": []}
        ready_state = {
            "checkout_amount_minor": 0,
            "currency": "VND",
            "custom_payment_methods": [{"id": "cpmt_momo", "name": "MoMo"}],
        }
        started = {
            "next_action": {
                "type": "momo_display_qr_code",
                "momo_display_qr_code": {"data": "momo-qr-payload"},
            },
        }
        with patch.object(workflow, "_custom_checkout_state", side_effect=[initial_state, ready_state]) as get_state, patch.object(
            workflow, "_custom_checkout_taxes", return_value=initial_state
        ), patch.object(workflow, "_custom_confirm_and_start", return_value={"confirmed": {}, "started": started}), patch.object(
            workflow.time, "sleep"
        ) as sleep:
            result = workflow._run_custom_provider(
                "momo", "token", "oaics_test", "openai_ie", "VN", "VND", None,
                "device", "did", Mock(), Mock(), "plus-1-month-free", "user@example.test",
            )

        self.assertEqual(get_state.call_count, 2)
        sleep.assert_called_once_with(0.8)
        self.assertEqual(result["qr_data"], "momo-qr-payload")

    def test_run_provider_checkout_routes_momo_oaics_to_custom_workflow(self):
        chatgpt_http = Mock()
        custom_result = {
            "provider": "momo",
            "qr_data": "momo-qr-payload",
            "checkout_amount": 0,
            "checkout_currency": "VND",
            "amount_verification": "verified_zero",
        }
        with patch.object(workflow, "extract_access_token", return_value=("token", {"email": "user@example.test"})), patch.object(
            workflow, "_preflight_campaign", return_value="plus-1-month-free"
        ), patch.object(
            workflow,
            "_create_checkout",
            return_value=({"checkout_session_id": "oaics_test", "processor_entity": "openai_ie"}, chatgpt_http),
        ), patch.object(workflow.stripe_checkout, "build_http", return_value=Mock()), patch.object(
            workflow, "_run_custom_provider", return_value=custom_result
        ) as run_custom:
            result = workflow.run_provider_checkout(
                "raw-token",
                "momo",
                entry_proxy="http://entry.example.test:8080",
                payment_proxy="http://payment.example.test:8080",
                promotion_proxy="http://promotion.example.test:8080",
                log=lambda _message: None,
            )

        self.assertEqual(run_custom.call_args.args[0], "momo")
        self.assertEqual(run_custom.call_args.args[6], "http://entry.example.test:8080")
        self.assertEqual(result["checkout_session_id"], "oaics_test")
        self.assertEqual(result["qr_data"], "momo-qr-payload")
        chatgpt_http.close.assert_called_once()

    def test_run_provider_checkout_forwards_momo_submission_strategy(self):
        chatgpt_http = Mock()
        provider_result = {"provider_redirect_url": "https://pay.example.test/momo"}
        with patch.object(workflow, "extract_access_token", return_value=("token", {})), patch.object(
            workflow, "_preflight_campaign", return_value="plus-1-month-free"
        ), patch.object(
            workflow,
            "_create_checkout",
            return_value=({"checkout_session_id": "cs_live_test", "processor_entity": "openai_ie"}, chatgpt_http),
        ), patch.object(workflow.stripe_checkout, "build_http", return_value=Mock()), patch.object(
            workflow.provider_checkout, "stripe_to_provider", return_value=provider_result
        ) as stripe_to_provider:
            workflow.run_provider_checkout(
                "raw-token",
                "momo",
                entry_proxy="http://entry.example.test:8080",
                payment_proxy="http://payment.example.test:8080",
                promotion_proxy="http://promotion.example.test:8080",
                local_method_strategy="late_promo",
                log=lambda _message: None,
            )

        self.assertEqual(stripe_to_provider.call_args.kwargs["local_method_strategy"], "late_promo")
        chatgpt_http.close.assert_called_once()
