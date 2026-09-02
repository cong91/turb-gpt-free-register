# -*- coding: utf-8 -*-
import unittest
from unittest.mock import patch

from config import env_loader
from webui import config_editor


class Gmail123452026ConfigTests(unittest.TestCase):
    def test_cdk_batch_is_not_a_config_or_secret_field(self):
        fields = {field["key"]: field for field in config_editor.EDITABLE_FIELDS}

        self.assertNotIn("GMAIL_123452026_CDKS", fields)
        self.assertNotIn("GMAIL_123452026_CDKS", env_loader.SECRET_ENV_KEYS)
        self.assertNotIn("GMAIL_123452026_CDKS", env_loader.EXPLICIT_EMPTY_LIST_ENV_KEYS)
        self.assertNotIn("GMAIL_123452026_CDKS", config_editor.EXPLICIT_EMPTY_LIST_KEYS)
        self.assertEqual(fields["GMAIL_123452026_ACCOUNTS_PER_CDK"]["type"], "int")



if __name__ == "__main__":
    unittest.main()
