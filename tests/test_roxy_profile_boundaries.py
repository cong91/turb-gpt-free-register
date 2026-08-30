# -*- coding: utf-8 -*-
import unittest
from pathlib import Path


class RoxyProfileBoundaryTests(unittest.TestCase):
    def test_registration_modules_do_not_import_profile_manager(self):
        root = Path(__file__).resolve().parents[1]
        for relative in (
            "main.py",
            "core/roxy_registration.py",
            "core/roxy_codex_oauth.py",
            "core/registration_service.py",
            "core/roxybrowser_client.py",
        ):
            source = (root / relative).read_text(encoding="utf-8")
            self.assertNotIn("roxy_profile_manager", source, relative)
            self.assertNotIn("RoxyProfileManager", source, relative)

    def test_manager_client_is_not_registration_cleanup_client(self):
        root = Path(__file__).resolve().parents[1]
        source = (root / "core/roxy_profile_manager_client.py").read_text(encoding="utf-8")
        self.assertNotIn("cleanup_profile", source)
        self.assertNotIn("open_profile()", source)


if __name__ == "__main__":
    unittest.main()
