"""Keeps bot tokens and secret query values out of log lines and tracebacks.

Exception text often carries the request URL, and Telegram URLs carry the bot token.
The filter rewrites each record's message and traceback before any handler formats it.
"""

import logging
import re
import traceback

SECRET_PATTERNS = (
    # Telegram bot tokens look like "123456789:AA..." and appear in /bot<token>/ paths.
    (re.compile(r"(?<!\d)\d{5,}:[A-Za-z0-9_-]{20,}"), "<redacted-token>"),
    # Query or key=value secrets, such as ?token=... or api_key=...
    (
        re.compile(r"(?i)\b(token|key|secret|sessdata|password|api_key)=[^&\s'\"<>]+"),
        r"\1=<redacted>",
    ),
)


def redact(text: str) -> str:
    for pattern, replacement in SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class RedactSecrets(logging.Filter):
    """Redacts the message and the formatted traceback of every record it passes."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = ()
        if record.exc_info:
            # The formatter reuses exc_text when it is set, so the traceback is redacted too.
            record.exc_text = redact(
                "".join(traceback.format_exception(*record.exc_info))
            )
        return True


def install_redaction() -> None:
    """Attach the filter to every root handler, so all loggers are covered."""
    for handler in logging.getLogger().handlers:
        handler.addFilter(RedactSecrets())
