import contextlib
import io
import logging
import unittest

from daphne.redact import RedactSecrets, install_redaction, redact

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
        self.assertIn("api.telegram.org", text)

    def test_secret_query_values_are_redacted(self):
        text = redact(
            "GET https://example.invalid/x?token=abc123&page=2 and api_key=zzz"
        )
        self.assertNotIn("abc123", text)
        self.assertNotIn("zzz", text)

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

    def test_compound_secret_names_are_redacted(self):
        text = redact(
            "a?access_token=AAA&refresh_token=BBB&client_secret=CCC&apikey=DDD&page=2"
        )
        for secret in ("AAA", "BBB", "CCC", "DDD"):
            self.assertNotIn(secret, text)

    def test_request_urls_are_cut_to_the_host(self):
        text = redact("failed GET https://api.example.invalid/x/y?q=secret-path-value")
        self.assertIn("api.example.invalid", text)
        self.assertNotIn("secret-path-value", text)
        self.assertNotIn("/x/y", text)

    def test_bad_format_string_is_not_hidden(self):
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.addFilter(RedactSecrets())
        logger = logging.getLogger("test.redact.badformat")
        logger.handlers, logger.propagate = [handler], False
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            logger.warning("needs %s and %s", "one")  # wrong arg count must surface
        self.assertIn("Logging error", err.getvalue())

    def test_install_covers_child_loggers_on_the_root_handler(self):
        root = logging.getLogger()
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter("%(message)s"))
        root.addHandler(handler)
        try:
            install_redaction()
            logging.getLogger("daphne.child").warning(
                "token=SECRETVALUE https://x.example.invalid/p"
            )
        finally:
            root.removeHandler(handler)
        out = stream.getvalue()
        self.assertNotIn("SECRETVALUE", out)
        self.assertNotIn("/p", out)


if __name__ == "__main__":
    unittest.main()
