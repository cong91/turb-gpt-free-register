from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from core.codex_agent import (
    AGENT_VERSION,
    AgentIdentityRegistrationError,
    generate_ed25519_keypair,
    register_agent,
    register_task,
)
from core.codex_agent_service import _agent_failure_message


class _Response:
    def __init__(self, status_code: int, payload: dict | None = None, text: str | None = None):
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text if text is not None else json.dumps(self._payload)

    def json(self) -> dict:
        return self._payload


class CodexAgentIdentityTests(unittest.TestCase):
    def test_register_agent_sends_current_codex_bootstrap_contract(self):
        response = _Response(200, {"agent_runtime_id": "runtime-123"})

        with patch("core.codex_agent._agent_post", return_value=response) as post:
            runtime_id = register_agent(
                "access-token",
                "ssh-ed25519 public-key",
                display_name="Legacy display name",
            )

        self.assertEqual(runtime_id, "runtime-123")
        payload = post.call_args.kwargs["payload"]
        self.assertEqual(
            payload,
            {
                "abom": {
                    "agent_version": AGENT_VERSION,
                    "agent_harness_id": "codex-cli",
                    "running_location": "local",
                },
                "agent_public_key": "ssh-ed25519 public-key",
                "capabilities": ["responsesapi"],
                "ttl": None,
            },
        )

    def test_register_agent_reports_registry_entitlement_failure(self):
        response = _Response(
            403,
            {
                "error": {
                    "message": "Agent registry is not enabled.",
                    "code": "agent_registry_not_enabled",
                }
            },
        )

        with patch("core.codex_agent._agent_post", return_value=response):  # noqa: SIM117
            with self.assertRaises(AgentIdentityRegistrationError) as raised:
                register_agent("access-token", "ssh-ed25519 public-key")

        self.assertEqual(raised.exception.status_code, 403)
        self.assertEqual(raised.exception.error_code, "agent_registry_not_enabled")
        self.assertIn("Agent Registry chưa được bật", str(raised.exception))

    def test_register_task_accepts_plain_task_id_response(self):
        private_key, _ = generate_ed25519_keypair()
        response = _Response(200, {"task_id": "task-123"})

        with patch("core.codex_agent._agent_post", return_value=response):
            task_id = register_task(
                "access-token",
                "runtime-123",
                private_key,
            )

        self.assertEqual(task_id, "task-123")

    def test_service_explains_registry_entitlement_failure(self):
        error = AgentIdentityRegistrationError(
            403,
            "agent_registry_not_enabled",
            "Agent registry is not enabled.",
        )

        self.assertEqual(
            _agent_failure_message(error),
            "Agent Registry chưa được bật cho account/workspace này; không thể tạo Agent Identity.",
        )


if __name__ == "__main__":
    unittest.main()
