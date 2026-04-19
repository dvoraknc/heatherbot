import os
import tempfile
import unittest
from unittest import mock

import aws_secrets_loader as loader


class TestAwsSecretsLoader(unittest.TestCase):
    def setUp(self):
        self._old_env = os.environ.copy()

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._old_env)

    def test_inject_env_sets_expected_defaults(self):
        loader.inject_env({"telegram_api_id": "123", "telegram_api_hash": "abc"})
        self.assertEqual(os.environ.get("TELEGRAM_API_ID"), "123")
        self.assertEqual(os.environ.get("TELEGRAM_API_HASH"), "abc")
        self.assertEqual(os.environ.get("BOT_PERSONA"), "kelly")
        self.assertEqual(os.environ.get("ENABLE_MONETIZATION"), "true")

    def test_inject_env_accepts_uppercase_secret_keys(self):
        loader.inject_env(
            {
                "TELEGRAM_API_ID": "777",
                "TELEGRAM_API_HASH": "hash777",
                "BOT_PERSONA": "kelly",
                "ENABLE_MONETIZATION": "true",
                "PAYMENT_BOT_TOKEN": "token777",
            }
        )
        self.assertEqual(os.environ.get("TELEGRAM_API_ID"), "777")
        self.assertEqual(os.environ.get("TELEGRAM_API_HASH"), "hash777")
        self.assertEqual(os.environ.get("PAYMENT_BOT_TOKEN"), "token777")

    def test_validate_runtime_config_requires_payment_token_for_live_kelly(self):
        os.environ["BOT_PERSONA"] = "kelly"
        os.environ["ENABLE_MONETIZATION"] = "true"
        os.environ.pop("PAYMENT_BOT_TOKEN", None)
        with self.assertRaises(SystemExit):
            loader.validate_runtime_config()

    def test_validate_runtime_config_allows_non_monetized_kelly_without_payment_token(self):
        os.environ["BOT_PERSONA"] = "kelly"
        os.environ["ENABLE_MONETIZATION"] = "false"
        os.environ.pop("PAYMENT_BOT_TOKEN", None)
        loader.validate_runtime_config()

    def test_restore_session_skips_when_file_exists(self):
        with tempfile.TemporaryDirectory() as td:
            session_file = os.path.join(td, "kelly_session.session")
            with open(session_file, "w", encoding="utf-8") as fh:
                fh.write("ok")
            with mock.patch.object(loader, "TELEGRAM_SESSION_FILE", session_file):
                loader.maybe_restore_session_from_s3()


if __name__ == "__main__":
    unittest.main()
