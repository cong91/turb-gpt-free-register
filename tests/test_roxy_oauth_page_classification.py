import unittest

from core.roxy_registration import _is_oauth_consent_like


class _PageStateDriver:
    def __init__(self, state):
        self.state = state

    def execute_script(self, _script):
        return self.state


class RoxyOAuthPageClassificationTests(unittest.TestCase):
    def test_authorize_url_with_email_entry_is_not_consent(self):
        driver = _PageStateDriver({
            "url": "https://auth.openai.com/oauth/authorize?client_id=codex",
            "has_email_entry": True,
            "has_consent_action": False,
        })

        self.assertFalse(_is_oauth_consent_like(driver))

    def test_authorize_url_with_consent_action_is_consent(self):
        driver = _PageStateDriver({
            "url": "https://auth.openai.com/oauth/authorize?client_id=codex",
            "has_email_entry": False,
            "has_consent_action": True,
        })

        self.assertTrue(_is_oauth_consent_like(driver))


if __name__ == "__main__":
    unittest.main()
