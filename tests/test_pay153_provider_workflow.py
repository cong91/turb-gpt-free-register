import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core import pay153_provider_workflow as workflow


class Pay153ProviderWorkflowTests(unittest.TestCase):
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
