"""Keeps request URLs, bot tokens and secret query values out of logs and tracebacks.

Exception text and log arguments often carry full request URLs, and Telegram URLs carry
the bot token. The filter rewrites each record's message and traceback before any handler
formats it: URLs are cut down to their host, then token and secret values are redacted.
"""

import logging
import re
import traceback

URL_PATTERN = re.compile(r"(?i)\b(https?)://([^/\s'\"<>?#]+)[^\s'\"<>]*")
# Scheme-less links (www.example.com/path) and protocol-relative //host/path, which
# the scheme pattern misses. The lookbehind keeps it out of file paths such as /tmp/a.mp4.
SCHEMELESS_URL_PATTERN = re.compile(
    r"(?<![\w./@-])(?://)?((?:[a-z0-9-]+\.)+[a-z]{2,24})(/[^\s'\"<>]*)", re.IGNORECASE
)
SECRET_PATTERNS = (
    # Telegram bot tokens look like "123456789:AA..." (also inside /bot<token>/ paths).
    (re.compile(r"(?<!\d)\d{5,}:[A-Za-z0-9_-]{20,}"), "<redacted-token>"),
    # Secret key=value pairs, including compound names such as access_token= or apikey=.
    (
        re.compile(
            r"(?i)([\w-]*(?:token|key|secret|sessdata|password))=[^&\s'\"<>]+",
        ),
        r"\1=<redacted>",
    ),
)


def _host_only(match: re.Match) -> str:
    return f"{match.group(1).lower()}://{match.group(2)}/…"


def redact(text: str) -> str:
    text = URL_PATTERN.sub(_host_only, text)
    text = SCHEMELESS_URL_PATTERN.sub(lambda m: f"{m.group(1)}/…", text)
    for pattern, replacement in SECRET_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class RedactSecrets(logging.Filter):
    """Redacts the message and the formatted traceback of every record it passes."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = redact(record.getMessage())
        except Exception:
            # A bad format string is the caller's bug. Keep the record as it was so the
            # handler reports the logging error, rather than hiding it.
            return True
        record.msg, record.args = message, ()
        if record.exc_info:
            try:
                # The formatter reuses exc_text when it is set, so the traceback is redacted too.
                record.exc_text = redact(
                    "".join(traceback.format_exception(*record.exc_info))
                )
            except Exception:
                record.exc_info, record.exc_text = None, "<traceback unavailable>"
        return True


def install_redaction() -> None:
    """Attach the filter to every root handler, so all loggers are covered."""
    for handler in logging.getLogger().handlers:
        handler.addFilter(RedactSecrets())
