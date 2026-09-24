import json
import unittest
from unittest.mock import patch

from core.pay153_checkout_extractor import (
    CHECKOUT_SENTINEL_FLOW,
    CHECKOUT_URL,
    CSRF_WARMUP_URL,
    SENTINEL_REQ_URL,
    CheckoutExtractor,
    Credentials,
    ExtractorConfig,
)

SENTINEL_TOKEN = json.dumps(
    {
        "p": "proof",
        "t": "",
        "c": "challenge-token",
        "id": "device",
        "flow": CHECKOUT_SENTINEL_FLOW,
        "so": "so-value",
    }
)


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload or {})

    def json(self):
        if self._payload is None:
            raise ValueError("no json payload")
        return self._payload


class _FakeSession:
    def __init__(self, posts: list[_FakeResponse] | None = None) -> None:
        self.headers: dict[str, str] = {}
        self.cookies: dict[str, str] = {}
        self.proxies: dict[str, str] = {}
        self.trust_env = False
        self.posts: list[dict] = []
        self.gets: list[str] = []
        self._responses = list(posts or [])

    def post(self, url, json=None, data=None, headers=None, timeout=None):
        self.posts.append(
            {"url": url, "json": json, "data": data, "headers": dict(headers or {})}
        )
        return self._responses.pop(0)

    def get(self, url, headers=None, timeout=None):
        self.gets.append(url)
        return _FakeResponse(200, {"csrfToken": "warm"})

    def close(self):
        pass


class CheckoutExtractorSentinelTests(unittest.TestCase):
    def _extractor(self, sessions: list[_FakeSession], logs: list[str] | None = None) -> CheckoutExtractor:
        remaining = list(sessions)

        def factory():
            return remaining.pop(0) if remaining else _FakeSession([])

        return CheckoutExtractor(
            Credentials(access_token="token"),
            config=ExtractorConfig(apply_promo=False, verify_proxy_country=False),
            session_factory=factory,
            sleeper=lambda _seconds: None,
            logger=(logs if logs is not None else []).append,
        )

    def test_create_checkout_sends_sentinel_proof_and_provider_payload(self):
        checkout_session = _FakeSession(
            [_FakeResponse(200, {"checkout_session_id": "oaics_proof_123"})]
        )
        sentinel_session = _FakeSession([_FakeResponse(200, {"token": "srv-token"})])
        extractor = self._extractor([checkout_session, sentinel_session])

        with patch(
            "core.sentinel_runner.generate_sentinel_token", return_value=SENTINEL_TOKEN
        ) as generate:
            session, checkout = extractor._create_checkout_with_retry(
                "http://proxy:8080"
            )

        self.assertIs(session, checkout_session)
        self.assertEqual(checkout["cs_id"], "oaics_proof_123")
        self.assertEqual(checkout_session.gets, [CSRF_WARMUP_URL])

        self.assertEqual(len(sentinel_session.posts), 1)
        sentinel_post = sentinel_session.posts[0]
        self.assertEqual(sentinel_post["url"], SENTINEL_REQ_URL)
        self.assertEqual(
            sentinel_post["headers"]["Referer"],
            "https://sentinel.openai.com/backend-api/sentinel/frame.html",
        )
        generate_kwargs = generate.call_args.kwargs
        self.assertEqual(generate_kwargs["flow"], CHECKOUT_SENTINEL_FLOW)
        self.assertEqual(generate_kwargs["page_url"], "https://chatgpt.com/")
        self.assertEqual(generate_kwargs["device_id"], extractor.device_id)

        checkout_post = checkout_session.posts[-1]
        self.assertEqual(checkout_post["url"], CHECKOUT_URL)
        self.assertEqual(checkout_post["headers"]["OpenAI-Sentinel-Token"], SENTINEL_TOKEN)
        so_token = json.loads(checkout_post["headers"]["OpenAI-Sentinel-SO-Token"])
        self.assertEqual(so_token["so"], "so-value")
        self.assertEqual(so_token["flow"], CHECKOUT_SENTINEL_FLOW)
        self.assertTrue(checkout_post["json"]["check_card_proxy"])
        self.assertEqual(checkout_post["json"]["cancel_url"], "https://chatgpt.com/")
        self.assertEqual(checkout_post["json"]["checkout_ui_mode"], "custom")

    def test_create_checkout_proceeds_without_proof_when_sentinel_runner_fails(self):
        checkout_session = _FakeSession(
            [_FakeResponse(200, {"checkout_session_id": "oaics_noproof_123"})]
        )
        sentinel_session = _FakeSession([_FakeResponse(200, {"token": "srv-token"})])
        logs: list[str] = []
        extractor = self._extractor([checkout_session, sentinel_session], logs)

        with patch(
            "core.sentinel_runner.generate_sentinel_token",
            side_effect=RuntimeError("node missing"),
        ):
            _session, checkout = extractor._create_checkout_with_retry(
                "http://proxy:8080"
            )

        self.assertEqual(checkout["cs_id"], "oaics_noproof_123")
        checkout_post = checkout_session.posts[-1]
        self.assertNotIn("OpenAI-Sentinel-Token", checkout_post["headers"])
        self.assertNotIn("OpenAI-Sentinel-SO-Token", checkout_post["headers"])
        self.assertTrue(any("Sentinel proof unavailable" in message for message in logs))

    def test_create_checkout_skips_proof_when_sentinel_endpoint_rejects(self):
        checkout_session = _FakeSession(
            [_FakeResponse(200, {"checkout_session_id": "oaics_noproof_456"})]
        )
        sentinel_session = _FakeSession([_FakeResponse(403, {"detail": "denied"})])
        logs: list[str] = []
        extractor = self._extractor([checkout_session, sentinel_session], logs)

        with patch(
            "core.sentinel_runner.generate_sentinel_token"
        ) as generate:
            _session, checkout = extractor._create_checkout_with_retry(
                "http://proxy:8080"
            )

        generate.assert_not_called()
        self.assertEqual(checkout["cs_id"], "oaics_noproof_456")
        self.assertNotIn(
            "OpenAI-Sentinel-Token", checkout_session.posts[-1]["headers"]
        )
        self.assertTrue(any("Sentinel request HTTP 403" in message for message in logs))

    def test_create_checkout_ignores_warmup_failure(self):
        class _BrokenWarmupSession(_FakeSession):
            def get(self, url, headers=None, timeout=None):
                self.gets.append(url)
                raise RuntimeError("warmup refused")

        checkout_session = _BrokenWarmupSession(
            [_FakeResponse(200, {"checkout_session_id": "oaics_warm_789"})]
        )
        sentinel_session = _FakeSession([_FakeResponse(403, {"detail": "denied"})])
        logs: list[str] = []
        extractor = self._extractor([checkout_session, sentinel_session], logs)

        _session, checkout = extractor._create_checkout_with_retry("http://proxy:8080")

        self.assertEqual(checkout["cs_id"], "oaics_warm_789")
        self.assertEqual(checkout_session.gets, [CSRF_WARMUP_URL])
        self.assertTrue(any("Checkout warmup note" in message for message in logs))


if __name__ == "__main__":
    unittest.main()
