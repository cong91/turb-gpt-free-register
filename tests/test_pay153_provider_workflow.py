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
        self.assertEqual(workflow.local_method_strategy("momo", 2), "standalone")
        self.assertEqual(workflow.local_method_strategy("momo", 3), "late_promo")
        self.assertEqual(workflow.local_method_strategy("momo", 4), "standalone")

    def test_pix_local_method_strategy_matches_pay153_retry_cycle(self):
        self.assertEqual(workflow.local_method_strategy("pix", 1), "standalone")
        self.assertEqual(workflow.local_method_strategy("pix", 2), "late_promo")
        self.assertEqual(workflow.local_method_strategy("pix", 3), "inline")
        self.assertEqual(workflow.local_method_strategy("pix", 4), "standalone")

    def test_checkout_payload_attaches_promo_for_momo_only_on_create(self):
        self.assertIn("promo_campaign", workflow._checkout_payload("momo", "VN", "VND", "camp", promo_on_create=True))
        self.assertNotIn("promo_campaign", workflow._checkout_payload("momo", "VN", "VND", "camp"))
        self.assertNotIn("promo_campaign", workflow._checkout_payload("pix", "BR", "BRL", "camp", promo_on_create=True))
        self.assertIn("promo_campaign", workflow._checkout_payload("hosted", "US", "USD", "camp"))

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

    def test_custom_confirm_flags_blocked_confirmation(self):
        http = Mock()
        http.post.side_effect = [
            SimpleNamespace(
                status_code=200,
                text='{"status":"blocked"}',
                json=lambda: {"status": "blocked"},
            ),
        ]

        with patch.object(workflow, "_sentinel_headers", return_value={}), self.assertRaisesRegex(
            RuntimeError, "CUSTOM_CONFIRM_BLOCKED"
        ):
            workflow._custom_confirm_and_start(
                http, "token", "oaics_test", "openai_ie", "cpmt_momo", None, "device", "did",
            )

    def test_momo_native_route_creates_confirmation_token_and_harvests_authorization_url(self):
        state = {
            "payment_method_types": ["card", "momo"],
            "currency": "vnd",
            "checkout_amount_minor": 42,
            "publishable_key": "pk_test_123",
        }
        authorize_url = "https://pm-redirects.stripe.com/authorize/acct_1abc/pa_nonce_x9"
        with patch.object(workflow.momo_oaics, "fetch_native_ready_state", return_value=state), patch.object(
            workflow, "_custom_checkout_taxes", return_value=state
        ), patch.object(
            workflow.provider_checkout, "create_provider_payment_method", return_value="pm_momo1"
        ) as create_pm, patch.object(
            workflow.momo_oaics, "create_oaics_confirmation_token", return_value="ctoken_1"
        ) as create_ctoken, patch.object(
            workflow.stripe_checkout, "build_http", return_value=Mock()
        ), patch.object(
            workflow, "_sentinel_headers", return_value={}
        ), patch.object(
            workflow.momo_oaics,
            "confirm_oaics_native_momo",
            # Nested payload proves the authorization URL is composed through
            # the real depth walk, not a flat field copy.
            return_value={"next_action": {"details": {"redirect_url": authorize_url}}},
        ) as confirm_native:
            result = workflow._run_momo_custom_provider(
                "token", "oaics_test", "openai_ie", "VN", "VND", None, "device", "did",
                Mock(), Mock(), "plus-1-month-free", {"checkout_session_id": "oaics_test"},
                "standalone", "user@example.test", lambda _message: None,
            )

        self.assertEqual(create_pm.call_args.args[1], "pk_test_123")
        self.assertEqual(create_pm.call_args.args[3], "momo")
        self.assertEqual(create_ctoken.call_args.args[1], "pk_test_123")
        self.assertEqual(create_ctoken.call_args.args[2], "pm_momo1")
        self.assertEqual(confirm_native.call_args.args[4], "ctoken_1")
        self.assertEqual(result["provider_redirect_url"], authorize_url)
        self.assertEqual(result["payment_method_id"], "pm_momo1")
        self.assertEqual(result["generation_kind"], "momo_oaics_checkout")
        self.assertEqual(result["checkout_amount"], 42)
        self.assertTrue(result["promo_applied"])
        self.assertEqual(result["amount_verification"], "verified_discounted")

    def test_momo_cpmt_route_keeps_custom_method_chain_and_harvests_url(self):
        state = {
            "checkout_amount_minor": 0,
            "currency": "VND",
            "custom_payment_methods": [{"id": "cpmt_momo", "name": "MoMo"}],
        }
        authorize_url = "https://pm-redirects.stripe.com/authorize/acct_1/pa_nonce_7"
        started = {
            "next_action": {
                "url": authorize_url,
                "type": "momo_display_qr_code",
                "momo_display_qr_code": {"data": "momo-qr-payload"},
            },
        }
        with patch.object(workflow.momo_oaics, "fetch_native_ready_state", return_value=state), patch.object(
            workflow.momo_oaics, "fetch_stable_state", return_value=state
        ), patch.object(workflow, "_custom_checkout_taxes", return_value=state), patch.object(
            workflow, "_custom_confirm_and_start", return_value={"confirmed": {}, "started": started}
        ) as confirm_and_start:
            result = workflow._run_momo_custom_provider(
                "token", "oaics_test", "openai_ie", "VN", "VND", None, "device", "did",
                Mock(), Mock(), "plus-1-month-free", {"checkout_session_id": "oaics_test"},
                "late_promo", "user@example.test", lambda _message: None,
            )

        self.assertEqual(confirm_and_start.call_args.args[4], "cpmt_momo")
        self.assertEqual(result["custom_payment_method_id"], "cpmt_momo")
        self.assertEqual(result["provider_redirect_url"], authorize_url)
        self.assertEqual(result["qr_data"], "momo-qr-payload")
        self.assertEqual(result["generation_kind"], "custom_payment_method")
        self.assertTrue(result["promo_applied"])

    def test_momo_explicit_empty_methods_rebuild(self):
        with patch.object(
            workflow.momo_oaics, "fetch_native_ready_state", return_value={"payment_method_types": []}
        ), self.assertRaisesRegex(RuntimeError, "MOMO_CHECKOUT_REBUILD_REQUIRED"):
            workflow._run_momo_custom_provider(
                "token", "oaics_test", "openai_ie", "VN", "VND", None, "device", "did",
                Mock(), Mock(), "plus-1-month-free", {"checkout_session_id": "oaics_test"},
                "late_promo", "user@example.test", lambda _message: None,
            )

    def test_momo_promotion_incompatible_update_raises_rebuild_marker(self):
        state = {"payment_method_types": ["momo"], "checkout_amount_minor": 500000, "currency": "VND"}
        with patch.object(workflow.momo_oaics, "fetch_native_ready_state", return_value=state), patch.object(
            workflow.momo_oaics, "fetch_stable_state", return_value=state
        ), patch.object(
            workflow,
            "_update_promotion",
            side_effect=RuntimeError(
                "Promotion update HTTP 400: promotion is not compatible with the checkout's payment methods"
            ),
        ), self.assertRaisesRegex(RuntimeError, "MOMO_PROMOTION_INCOMPATIBLE_REBUILD_REQUIRED"):
            workflow._run_momo_custom_provider(
                "token", "oaics_test", "openai_ie", "VN", "VND", None, "device", "did",
                Mock(), Mock(), "plus-1-month-free", {"checkout_session_id": "oaics_test"},
                "late_promo", "user@example.test", lambda _message: None,
            )

    def test_momo_native_confirm_blocked_propagates_rebuild_marker(self):
        state = {
            "payment_method_types": ["momo"],
            "currency": "VND",
            "checkout_amount_minor": 42,
            "publishable_key": "pk_test_123",
        }
        with patch.object(workflow.momo_oaics, "fetch_native_ready_state", return_value=state), patch.object(
            workflow, "_custom_checkout_taxes", return_value=state
        ), patch.object(workflow.provider_checkout, "create_provider_payment_method", return_value="pm_momo1"), patch.object(
            workflow.momo_oaics, "create_oaics_confirmation_token", return_value="ctoken_1"
        ), patch.object(workflow.stripe_checkout, "build_http", return_value=Mock()), patch.object(
            workflow, "_sentinel_headers", return_value={}
        ), patch.object(
            workflow.momo_oaics,
            "confirm_oaics_native_momo",
            side_effect=RuntimeError("MOMO_OAICS_CONFIRM_BLOCKED: native confirm was blocked"),
        ), self.assertRaisesRegex(RuntimeError, "MOMO_OAICS_CONFIRM_BLOCKED"):
            workflow._run_momo_custom_provider(
                "token", "oaics_test", "openai_ie", "VN", "VND", None, "device", "did",
                Mock(), Mock(), "plus-1-month-free", {"checkout_session_id": "oaics_test"},
                "standalone", "user@example.test", lambda _message: None,
            )

    def test_momo_cpmt_blocked_confirm_exhausts_retries_and_propagates(self):
        state = {
            "checkout_amount_minor": 0,
            "currency": "VND",
            "custom_payment_methods": [{"id": "cpmt_momo", "name": "MoMo"}],
        }
        with patch.object(workflow.momo_oaics, "fetch_native_ready_state", return_value=state), patch.object(
            workflow.momo_oaics, "fetch_stable_state", return_value=state
        ), patch.object(workflow, "_custom_checkout_taxes", return_value=state), patch.object(
            workflow,
            "_custom_confirm_and_start",
            side_effect=RuntimeError("CUSTOM_CONFIRM_BLOCKED: blocked upstream"),
        ) as confirm_and_start, patch.object(workflow.time, "sleep"), self.assertRaisesRegex(
            RuntimeError, "CUSTOM_CONFIRM_BLOCKED"
        ):
            workflow._run_momo_custom_provider(
                    "token", "oaics_test", "openai_ie", "VN", "VND", None, "device", "did",
                    Mock(), Mock(), "plus-1-month-free", {"checkout_session_id": "oaics_test"},
                    "late_promo", "user@example.test", lambda _message: None,
                )

        self.assertEqual(confirm_and_start.call_count, 3)

    def test_momo_cpmt_blocked_confirm_retries_with_fresh_sentinel_then_succeeds(self):
        state = {
            "checkout_amount_minor": 0,
            "currency": "VND",
            "custom_payment_methods": [{"id": "cpmt_momo", "name": "MoMo"}],
        }
        started = {
            "next_action": {
                "url": "https://pm-redirects.stripe.com/authorize/acct_1/pa_nonce_7",
                "momo_display_qr_code": {"data": "momo-qr-payload"},
            },
        }
        with patch.object(workflow.momo_oaics, "fetch_native_ready_state", return_value=state), patch.object(
            workflow.momo_oaics, "fetch_stable_state", return_value=state
        ), patch.object(workflow, "_custom_checkout_taxes", return_value=state), patch.object(
            workflow,
            "_custom_confirm_and_start",
            side_effect=[
                RuntimeError("CUSTOM_CONFIRM_BLOCKED: blocked upstream"),
                {"confirmed": {}, "started": started},
            ],
        ) as confirm_and_start, patch.object(workflow.time, "sleep"):
            result = workflow._run_momo_custom_provider(
                "token", "oaics_test", "openai_ie", "VN", "VND", None, "device", "did",
                Mock(), Mock(), "plus-1-month-free", {"checkout_session_id": "oaics_test"},
                "late_promo", "user@example.test", lambda _message: None,
            )

        self.assertEqual(confirm_and_start.call_count, 2)
        self.assertEqual(result["provider_redirect_url"], "https://pm-redirects.stripe.com/authorize/acct_1/pa_nonce_7")

    def test_momo_create_time_promo_rejection_maps_to_rebuild_marker(self):
        chatgpt_http = Mock()
        with patch.object(workflow, "extract_access_token", return_value=("token", {})), patch.object(
            workflow, "_preflight_campaign", return_value="plus-1-month-free"
        ), patch.object(
            workflow,
            "_create_checkout",
            side_effect=RuntimeError(
                "OpenAI Checkout HTTP 400: promotion is not compatible with the checkout's payment methods"
            ),
        ), patch.object(
            workflow.stripe_checkout, "build_http", return_value=Mock()
        ), self.assertRaisesRegex(RuntimeError, "MOMO_PROMOTION_INCOMPATIBLE_REBUILD_REQUIRED"):
            workflow.run_provider_checkout(
                "raw-token",
                "momo",
                entry_proxy="http://entry.example.test:8080",
                payment_proxy="http://payment.example.test:8080",
                promotion_proxy="http://promotion.example.test:8080",
                local_method_strategy="standalone",
                log=lambda _message: None,
            )
        del chatgpt_http

    def test_custom_confirm_blocked_on_non_200_is_rebuildable(self):
        http = Mock()
        http.post.side_effect = [
            SimpleNamespace(
                status_code=403,
                text='{"status":"blocked"}',
                json=lambda: {"status": "blocked"},
            ),
        ]

        with patch.object(workflow, "_sentinel_headers", return_value={}), self.assertRaisesRegex(
            RuntimeError, "CUSTOM_CONFIRM_BLOCKED"
        ):
            workflow._custom_confirm_and_start(
                http, "token", "oaics_test", "openai_ie", "cpmt_momo", None, "device", "did",
            )

    def test_custom_confirm_accepts_result_field_as_success(self):
        http = Mock()
        http.post.side_effect = [
            SimpleNamespace(status_code=200, json=lambda: {"result": "success"}),
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

    def test_momo_processor_preflight_reads_account_entity(self):
        http = Mock()
        http.get.return_value = SimpleNamespace(status_code=200, json=lambda: {
            "accounts": {
                "acct1": {"account": {"processor": {"a001": {"processor_entity": "openai_ie"}}}},
            },
        })
        with patch.object(workflow.stripe_checkout, "build_http", return_value=http):
            entity = workflow._preflight_momo_processor("token", "acct1", None, "device", "did", lambda _m: None)
        self.assertEqual(entity, "openai_ie")

    def test_momo_processor_preflight_returns_empty_without_processor(self):
        http = Mock()
        http.get.return_value = SimpleNamespace(status_code=200, json=lambda: {"accounts": {"acct1": {}}})
        with patch.object(workflow.stripe_checkout, "build_http", return_value=http):
            entity = workflow._preflight_momo_processor("token", "acct1", None, "device", "did", lambda _m: None)
        self.assertEqual(entity, "")

    def test_run_provider_checkout_fails_fast_for_openai_llc_momo_account(self):
        with patch.object(workflow, "extract_access_token", return_value=("token", {"account_id": "acct1"})), patch.object(
            workflow, "_preflight_campaign", return_value="plus-1-month-free"
        ), patch.object(
            workflow, "_preflight_momo_processor", return_value="openai_llc"
        ), patch.object(
            workflow, "_create_checkout"
        ) as create_checkout, patch.object(
            workflow.stripe_checkout, "build_http", return_value=Mock()
        ), self.assertRaisesRegex(RuntimeError, "MOMO_PROCESSOR_UNSUPPORTED"):
            workflow.run_provider_checkout(
                "raw-token",
                "momo",
                entry_proxy="http://entry.example.test:8080",
                payment_proxy="http://payment.example.test:8080",
                promotion_proxy="http://promotion.example.test:8080",
                log=lambda _message: None,
            )

        create_checkout.assert_not_called()

    def test_run_provider_checkout_momo_continues_for_openai_ie(self):
        chatgpt_http = Mock()
        custom_result = {
            "provider": "momo",
            "provider_redirect_url": "https://pm-redirects.stripe.com/authorize/acct_1/pa_nonce_9",
        }
        with patch.object(workflow, "extract_access_token", return_value=("token", {"account_id": "acct1"})), patch.object(
            workflow, "_preflight_campaign", return_value="plus-1-month-free"
        ), patch.object(
            workflow, "_preflight_momo_processor", return_value="openai_ie"
        ), patch.object(
            workflow,
            "_create_checkout",
            return_value=({"checkout_session_id": "oaics_test", "processor_entity": "openai_ie"}, chatgpt_http),
        ), patch.object(workflow.stripe_checkout, "build_http", return_value=Mock()), patch.object(
            workflow, "_run_momo_custom_provider", return_value=custom_result
        ):
            result = workflow.run_provider_checkout(
                "raw-token",
                "momo",
                entry_proxy="http://entry.example.test:8080",
                payment_proxy="http://payment.example.test:8080",
                promotion_proxy="http://promotion.example.test:8080",
                log=lambda _message: None,
            )

        self.assertEqual(result["checkout_session_id"], "oaics_test")
        chatgpt_http.close.assert_called_once()

    def test_run_provider_checkout_routes_momo_oaics_to_momo_workflow(self):
        chatgpt_http = Mock()
        custom_result = {
            "provider": "momo",
            "provider_redirect_url": "https://pm-redirects.stripe.com/authorize/acct_1/pa_nonce_9",
            "checkout_amount": 0,
            "checkout_currency": "VND",
            "amount_verification": "verified_discounted",
        }
        with patch.object(workflow, "extract_access_token", return_value=("token", {"email": "user@example.test"})), patch.object(
            workflow, "_preflight_campaign", return_value="plus-1-month-free"
        ), patch.object(
            workflow,
            "_create_checkout",
            return_value=({"checkout_session_id": "oaics_test", "processor_entity": "openai_ie"}, chatgpt_http),
        ), patch.object(workflow.stripe_checkout, "build_http", return_value=Mock()), patch.object(
            workflow, "_run_momo_custom_provider", return_value=custom_result
        ) as run_momo:
            result = workflow.run_provider_checkout(
                "raw-token",
                "momo",
                entry_proxy="http://entry.example.test:8080",
                payment_proxy="http://payment.example.test:8080",
                promotion_proxy="http://promotion.example.test:8080",
                log=lambda _message: None,
            )

        self.assertEqual(run_momo.call_args.args[5], "http://entry.example.test:8080")
        self.assertEqual(run_momo.call_args.args[11]["checkout_session_id"], "oaics_test")
        self.assertEqual(run_momo.call_args.args[12], "standalone")
        self.assertEqual(result["checkout_session_id"], "oaics_test")
        self.assertEqual(result["provider_redirect_url"], custom_result["provider_redirect_url"])
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
