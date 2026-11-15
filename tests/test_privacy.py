import io
import logging
import unittest

from demo import AllowListFilter, Handler


class PrivacyTests(unittest.TestCase):
    def test_filter_drops_sensitive_values(self):
        output = io.StringIO(); handler = logging.StreamHandler(output); handler.addFilter(AllowListFilter())
        logger = logging.getLogger("remedy-test"); logger.handlers = [handler]; logger.setLevel(logging.INFO)
        logger.info("email=fake@example.invalid note=private", extra={"case_id": "case-x", "route": "B", "sub": "B_goodwill", "reasons": ["SAFE"]})
        text = output.getvalue()
        self.assertNotIn("fake@example.invalid", text); self.assertNotIn("private", text)
        self.assertIn("case_id=case-x", text)

    def test_request_logging_disabled(self):
        self.assertIsNone(Handler.log_message(object(), "secret %s", "value"))


if __name__ == "__main__": unittest.main()
