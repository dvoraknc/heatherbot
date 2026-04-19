import unittest
from unittest import mock

import postprocess


class TestPostprocess(unittest.TestCase):
    def test_strip_thinking_tags(self):
        text = "hi<think>internal</think> there"
        self.assertEqual(postprocess.strip_thinking_tags(text), "hi there")

    def test_strip_obvious_phantom_claims(self):
        text = "I just sent you a pic. Stay focused."
        self.assertEqual(postprocess.strip_obvious_phantom_claims(text), "Stay focused.")

    def test_postprocess_response_pipeline(self):
        text = "<think>hidden</think> \"I'm not a bot. Keep paying.\""
        with mock.patch("postprocess.add_human_imperfections", side_effect=lambda x: x):
            cleaned = postprocess.postprocess_response(text)
        self.assertNotIn("not a bot", cleaned.lower())
        self.assertIn("Keep paying.", cleaned)


if __name__ == "__main__":
    unittest.main()
