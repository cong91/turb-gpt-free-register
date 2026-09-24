"""MoMo OAICS ported helpers: ported PAY.153 reference invariants."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core import pay153_momo_oaics as momo


class MomoPromoAmountTests(unittest.TestCase):
    def test_accepts_zero_to_fifty_vnd(self):
        for amount in (0, "0", 1, 42, "50", "50.0"):
            with self.subTest(amount=amount):
                self.assertTrue(momo.is_momo_promo_amount(amount, "VND"))
                self.assertTrue(momo.is_momo_promo_amount(amount, "vnd"))

    def test_rejects_above_limit_or_non_vnd_or_garbage(self):
        for amount, currency in (
            (51, "VND"), (-1, "VND"), (None, "VND"), (True, "VND"),
            ("abc", "VND"), (0, "PHP"), (42, ""), (0, None),
        ):
            with self.subTest(amount=amount, currency=currency):
                self.assertFalse(momo.is_momo_promo_amount(amount, currency))


class MomoAuthorizationUrlTests(unittest.TestCase):
    def test_finds_pa_nonce_url_in_nested_payload(self):
        payload = {
            "confirmed": {
                "next_action": [
                    {"url": "https://pm-redirects.stripe.com/authorize/acct_1abcDEF/pa_nonce_xYz123"}
                ]
            }
        }
        self.assertEqual(
            momo.momo_authorization_url(payload),
            "https://pm-redirects.stripe.com/authorize/acct_1abcDEF/pa_nonce_xYz123",
        )

    def test_finds_sa_nonce_url(self):
        self.assertEqual(
            momo.momo_authorization_url({"a": "https://pm-redirects.stripe.com/authorize/acct_9/sa_nonce_88"}),
            "https://pm-redirects.stripe.com/authorize/acct_9/sa_nonce_88",
        )

    def test_decodes_url_encoded_candidates(self):
        encoded = "https%3A%2F%2Fpm-redirects.stripe.com%2Fauthorize%2Facct_1%2Fpa_nonce_9"
        self.assertEqual(
            momo.momo_authorization_url({"redirect": encoded}),
            "https://pm-redirects.stripe.com/authorize/acct_1/pa_nonce_9",
        )

    def test_returns_empty_for_non_authorization_urls(self):
        self.assertEqual(momo.momo_authorization_url({
            "checkout_url": "https://chatgpt.com/checkout/openai_ie/oaics_1",
            "other": "https://pm-redirects.stripe.com/authorize/other",
            "deep": {"x": ""},
        }), "")

    def test_first_match_wins_across_payloads(self):
        first = "https://pm-redirects.stripe.com/authorize/acct_1/pa_nonce_a"
        second = "https://pm-redirects.stripe.com/authorize/acct_1/pa_nonce_b"
        self.assertEqual(momo.momo_authorization_url({"u": second}, {"u": first}), second)


class ConfirmationBlockedTests(unittest.TestCase):
    def test_detects_blocked_status_variants(self):
        for payload in (
            {"status": "blocked"},
            {"status": "payment_blocked"},
            {"result": "blocked_review"},
            {"a": {"b": {"code": "BLOCKED"}}},
        ):
            with self.subTest(payload=payload):
                self.assertTrue(momo.checkout_confirmation_is_blocked(payload))

    def test_negations_and_unknown_values_are_not_blocked(self):
        for payload in (
            {"status": "not_blocked"},
            {"status": "unblocked"},
            {"status": "success"},
            {"status": ""},
            {},
        ):
            with self.subTest(payload=payload):
                self.assertFalse(momo.checkout_confirmation_is_blocked(payload))

    def test_raw_text_fallback(self):
        self.assertTrue(momo.checkout_confirmation_is_blocked({}, '{"status":"blocked_x"}'))
        self.assertFalse(momo.checkout_confirmation_is_blocked({}, '{"status":"success"}'))


class MomoRebuildMarkerTests(unittest.TestCase):
    def test_reference_markers_require_rebuild(self):
        markers = (
            "MOMO_CHECKOUT_REBUILD_REQUIRED: no method published",
            "MOMO_PROMOTION_INCOMPATIBLE_REBUILD_REQUIRED: rejected",
            "MOMO_CREATE_PROMOTION_NOT_APPLIED_REBUILD_REQUIRED",
            "MOMO_METHOD_REMOVED_REBUILD_REQUIRED",
            "MOMO_PROMO_AMOUNT_REQUIRED: amount=5000",
            "MOMO_OAICS_CONFIRM_BLOCKED: blocked",
            "CUSTOM_CONFIRM_BLOCKED",
            "MOMO_REDIRECT_MISSING",
        )
        for marker in markers:
            with self.subTest(marker=marker):
                self.assertTrue(momo.momo_requires_rebuild(RuntimeError(marker)))
                self.assertTrue(momo.momo_requires_rebuild(marker))

    def test_unrelated_errors_do_not_rebuild(self):
        self.assertFalse(momo.momo_requires_rebuild(RuntimeError("Checkout approval HTTP 502")))
        self.assertFalse(momo.momo_requires_rebuild(RuntimeError("timeout")))


class OaicsMethodSnapshotTests(unittest.TestCase):
    def test_native_momo_is_detected(self):
        state = {"payment_method_types": ["card", "momo"]}
        self.assertEqual(momo.oaics_stage_native_methods(state), ["card", "momo"])

    def test_explicit_empty_container_overrides_fallback(self):
        fallback = {"payment_method_types": ["momo"]}
        state = {"payment_method_types": []}
        self.assertEqual(momo.oaics_stage_native_methods(state, fallback), [])

    def test_fallback_used_only_without_explicit_container(self):
        fallback = {"payment_method_types": ["momo"]}
        self.assertEqual(momo.oaics_stage_native_methods({}, fallback), ["momo"])

    def test_stale_snapshot_is_ignored_when_current_is_explicit(self):
        state = {"previous_session": {"payment_method_types": ["momo"]}, "payment_method_types": []}
        self.assertEqual(momo.oaics_stage_native_methods(state), [])

    def test_mapping_snapshot_reads_availability(self):
        state = {"payment_methods": {"momo": {"available": True}, "card": {"available": False}}}
        self.assertEqual(momo.oaics_stage_native_methods(state), ["momo"])

    def test_labeled_cpmt_is_matched(self):
        state = {"custom_payment_methods": [{"id": "cpmt_abc", "name": "MoMo Wallet"}]}
        self.assertEqual(momo.oaics_stage_custom_method_id(state), "cpmt_abc")

    def test_unlabelled_sole_cpmt_is_not_momo(self):
        state = {"custom_payment_methods": [{"id": "cpmt_xyz"}]}
        self.assertEqual(momo.oaics_stage_custom_method_id(state), "")

    def test_unrelated_cpmt_is_not_momo(self):
        state = {"custom_payment_methods": [{"id": "cpmt_1", "name": "Card"}, {"id": "cpmt_2", "name": "PayPal"}]}
        self.assertEqual(momo.oaics_stage_custom_method_id(state), "")

    def test_route_decision_prefers_native_then_cpmt_then_rebuild(self):
        native = {"payment_method_types": ["momo"], "custom_payment_methods": [{"id": "cpmt_1", "name": "MoMo"}]}
        self.assertEqual(momo.momo_route_decision(native)["route"], "native")
        cpmt = {"custom_payment_methods": [{"id": "cpmt_1", "name": "MoMo"}]}
        decision = momo.momo_route_decision(cpmt)
        self.assertEqual(decision["route"], "cpmt")
        self.assertEqual(decision["custom_method_id"], "cpmt_1")
        empty = {"payment_method_types": []}
        self.assertEqual(momo.momo_route_decision(empty)["route"], "rebuild")


class MomoPromotionActionTests(unittest.TestCase):
    def test_rebuild_when_momo_not_ready(self):
        self.assertEqual(
            momo.momo_promotion_action(["card"], "", 500000, "VND", True), "rebuild"
        )

    def test_already_discounted_skips_update(self):
        self.assertEqual(
            momo.momo_promotion_action(["momo"], "", 42, "VND", True), "already_discounted"
        )

    def test_rebuild_late_when_create_promo_not_settled(self):
        self.assertEqual(
            momo.momo_promotion_action(["momo"], "", 500000, "VND", True, True), "rebuild_late"
        )

    def test_refresh_for_late_update(self):
        self.assertEqual(
            momo.momo_promotion_action(["momo"], "", 500000, "VND", True, False), "refresh"
        )

    def test_continue_without_promo_request(self):
        self.assertEqual(
            momo.momo_promotion_action(["momo"], "cpmt_1", 500000, "VND", False), "continue"
        )


class MomoUtilityTests(unittest.TestCase):
    def test_nested_scalar_reads_deep_values(self):
        payload = {"a": {"clientSecret": "seti_1_secret_2"}, "b": "noise"}
        self.assertEqual(momo.nested_scalar(payload, ("client_secret", "clientSecret")), "seti_1_secret_2")

    def test_nested_scalar_returns_empty_when_absent(self):
        self.assertEqual(momo.nested_scalar({"a": 1}, ("client_secret",)), "")

    def test_redaction_hides_payment_secrets(self):
        text = "confirm failed ctoken_abc123 seti_9_secret_x pi_1yy"
        redacted = momo.redact_payment_error(text)
        self.assertNotIn("ctoken_abc123", redacted)
        self.assertNotIn("seti_9_secret_x", redacted)
        self.assertNotIn("pi_1yy", redacted)
        self.assertIn("[PAYMENT_SECRET]", redacted)


class FetchNativeReadyStateTests(unittest.TestCase):
    def _response(self, payload):
        return SimpleNamespace(status_code=200, json=lambda: payload)

    def test_polls_until_native_momo_published(self):
        http = Mock()
        http.get.side_effect = [
            self._response({}),
            self._response({}),
            self._response({"payment_method_types": ["momo"]}),
        ]
        with patch.object(momo.time, "sleep"):
            state = momo.fetch_native_ready_state(
                http, "token", "oaics_1", "openai_ie", "device",
                log=lambda _message: None,
            )

        self.assertEqual(http.get.call_count, 3)
        self.assertEqual(momo.oaics_stage_native_methods(state), ["momo"])

    def test_poller_does_not_inherit_an_explicit_empty_snapshot(self):
        http = Mock()
        http.get.side_effect = [
            self._response({"payment_method_types": []}),
            self._response({"payment_method_types": ["momo"]}),
        ]
        preserve = {"payment_method_types": ["momo"]}
        with patch.object(momo.time, "sleep"):
            state = momo.fetch_native_ready_state(
                http, "token", "oaics_1", "openai_ie", "device",
                preserve_from=preserve, attempts=5, log=lambda _message: None,
            )

        self.assertEqual(http.get.call_count, 2)
        self.assertEqual(momo.oaics_stage_native_methods(state), ["momo"])


class NativeConfirmChainTests(unittest.TestCase):
    def test_blocked_payload_raises_rebuild_marker_after_ping(self):
        http = Mock()
        http.post.side_effect = [
            SimpleNamespace(status_code=200, json=dict),
            SimpleNamespace(status_code=200, text='{"status":"blocked"}', json=lambda: {"status": "blocked"}),
        ]
        with self.assertRaisesRegex(RuntimeError, "MOMO_OAICS_CONFIRM_BLOCKED"):
            momo.confirm_oaics_native_momo(
                http, "token", "oaics_1", "openai_ie", "ctoken_1", "device", "did",
                {"OpenAI-Sentinel-Token": "sen"}, log=lambda _message: None,
            )
        self.assertEqual(http.post.call_count, 2)

    def test_blocked_marker_on_non_200_still_rebuilds(self):
        http = Mock()
        http.post.return_value = SimpleNamespace(
            status_code=403, text='{"status":"blocked"}', json=lambda: {"status": "blocked"},
        )
        with self.assertRaisesRegex(RuntimeError, "MOMO_OAICS_CONFIRM_BLOCKED"):
            momo.confirm_oaics_native_momo(
                http, "token", "oaics_1", "openai_ie", "ctoken_1", "device", "did",
                {}, log=lambda _message: None,
            )

    def test_sentinel_ping_failure_fails_closed(self):
        http = Mock()
        http.post.side_effect = [SimpleNamespace(status_code=500, json=dict, text="")]
        with self.assertRaisesRegex(RuntimeError, "MOMO_OAICS_SENTINEL_PING_FAILED"):
            momo.confirm_oaics_native_momo(
                http, "token", "oaics_1", "openai_ie", "ctoken_1", "device", "did",
                {"OpenAI-Sentinel-Token": "sen"}, log=lambda _message: None,
            )

    def test_ping_and_confirm_carry_sentinel_headers_and_confirm_token(self):
        http = Mock()
        http.post.side_effect = [
            SimpleNamespace(status_code=200, json=dict),
            SimpleNamespace(status_code=200, text="{}", json=lambda: {"status": "success"}),
        ]
        momo.confirm_oaics_native_momo(
            http, "token", "oaics_1", "openai_ie", "ctoken_1", "device", "did",
            {"OpenAI-Sentinel-Token": "sen"}, log=lambda _message: None,
        )
        ping_headers = http.post.call_args_list[0].kwargs["headers"]
        confirm_headers = http.post.call_args_list[1].kwargs["headers"]
        confirm_body = http.post.call_args_list[1].kwargs["json"]
        self.assertEqual(ping_headers["OpenAI-Sentinel-Token"], "sen")
        self.assertEqual(confirm_headers["OpenAI-Sentinel-Token"], "sen")
        self.assertEqual(confirm_body["confirm_token"], "ctoken_1")
        self.assertEqual(confirm_body["selected_payment_method_type"], "momo")

    def test_fetch_stable_state_raises_rebuild_when_snapshot_keeps_changing(self):
        http = Mock()
        http.get.side_effect = [
            SimpleNamespace(status_code=200, json=lambda: {"payment_method_types": ["momo"], "checkout_amount_minor": 10}),
            SimpleNamespace(status_code=200, json=lambda: {"payment_method_types": ["momo"], "checkout_amount_minor": 42}),
        ]
        with patch.object(momo.time, "sleep"), self.assertRaisesRegex(
            RuntimeError, "MOMO_CHECKOUT_REBUILD_REQUIRED"
        ):
            momo.fetch_stable_state(
                http, "token", "oaics_1", "openai_ie", "device",
                attempts=3, log=lambda _message: None,
            )

    def test_fetch_stable_state_returns_when_two_snapshots_agree(self):
        http = Mock()
        http.get.side_effect = [
            SimpleNamespace(status_code=200, json=lambda: {"payment_method_types": ["momo"], "checkout_amount_minor": 42}),
            SimpleNamespace(status_code=200, json=lambda: {"payment_method_types": ["momo"], "checkout_amount_minor": 42}),
        ]
        with patch.object(momo.time, "sleep"):
            state = momo.fetch_stable_state(
                http, "token", "oaics_1", "openai_ie", "device",
                attempts=3, log=lambda _message: None,
            )
        self.assertEqual(momo.oaics_stage_native_methods(state), ["momo"])

    def test_create_confirmation_token_returns_ctoken(self):
        stripe_http = Mock()
        stripe_http.post.return_value = SimpleNamespace(
            status_code=200, text="{}", json=lambda: {"id": "ctoken_1"},
        )
        self.assertEqual(
            momo.create_oaics_confirmation_token(stripe_http, "pk_test_1", "pm_1"),
            "ctoken_1",
        )

    def test_create_confirmation_token_rejects_missing_ids(self):
        with self.assertRaisesRegex(RuntimeError, "PUBLISHABLE_KEY_MISSING"):
            momo.create_oaics_confirmation_token(Mock(), "", "pm_1")
        with self.assertRaisesRegex(RuntimeError, "PAYMENT_METHOD_INVALID"):
            momo.create_oaics_confirmation_token(Mock(), "pk_test_1", "not_pm")


if __name__ == "__main__":
    unittest.main()
