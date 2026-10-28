import io
import logging
import unittest

from daphne.redact import RedactSecrets, redact

TOKEN = "123456789:AAFakeTokenFakeTokenFakeTokenFake"


class TestRedaction(unittest.TestCase):
    def _log(self, emit) -> str:
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        handler.addFilter(RedactSecrets())
        logger = logging.getLogger("test.redact")
        logger.handlers, logger.propagate = [handler], False
        logger.setLevel(logging.DEBUG)
        emit(logger)
        return stream.getvalue()

    def test_bot_token_in_a_url_is_redacted(self):
        text = redact(f"fetch failed: https://api.telegram.org/bot{TOKEN}/getFile")
        self.assertNotIn(TOKEN, text)
        self.assertIn("<redacted-token>", text)

    def test_secret_query_values_are_redacted(self):
        text = redact(
            "GET https://example.invalid/x?token=abc123&page=2 and api_key=zzz"
        )
        self.assertNotIn("abc123", text)
        self.assertNotIn("zzz", text)
        self.assertIn("page=2", text)

    def test_traceback_text_is_redacted_too(self):
        def fail(logger):
            try:
                raise RuntimeError(
                    f"stream failed at https://api.telegram.org/bot{TOKEN}/x"
                )
            except RuntimeError:
                logger.exception("download failed")

        out = self._log(fail)
        self.assertNotIn(TOKEN, out)
        self.assertIn("Traceback", out)
        self.assertIn("download failed", out)


if __name__ == "__main__":
    unittest.main()
