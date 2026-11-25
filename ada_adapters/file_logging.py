"""Opt-in rotating file logs for hosts that do not use the terminal."""

import logging
from functools import partial
from logging.handlers import RotatingFileHandler
import sys

from ada_core.errors import redact_secrets


_redact_log_text = partial(redact_secrets, include_escaped=True)


class _RedactSecrets(logging.Filter):
    def filter(self, record):
        # Interpolate first: a secret can span both a message and its arguments.
        record.msg = _redact_log_text(record.getMessage())
        record.args = ()
        if record.exc_info:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = _redact_log_text(record.exc_text)
        if record.stack_info:
            record.stack_info = _redact_log_text(record.stack_info)
        return True


class _RedactedRotatingFileHandler(RotatingFileHandler):
    def handleError(self, record: logging.LogRecord) -> None:
        # The default stderr diagnostic can print the raw chained exception.
        if not isinstance(sys.exc_info()[1], OSError):
            super().handleError(record)
        elif logging.raiseExceptions and sys.stderr:
            try:
                sys.stderr.write("Could not write to the rotating log file.\n")
            except (OSError, ValueError):
                pass


def configure_redacted_file_logger(log_path, *, max_bytes=5_000_000, backup_count=3):
    """Return a standalone logger; the caller owns its file handler's lifetime."""
    handler = _RedactedRotatingFileHandler(
        log_path, maxBytes=max_bytes, backupCount=backup_count, encoding="utf-8",
    )
    handler.addFilter(_RedactSecrets())
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger = logging.Logger("ada.file", level=logging.INFO)
    logger.propagate = False
    logger.addHandler(handler)
    return logger
