"""
Logging configuration for caldav-cal-api.

Sets up two loguru sinks:

- Console (stderr): defaults to INFO level, overridable via the
  ``CALDAV_CAL_API_LOG_LEVEL`` env var or the ``--debug`` CLI flag (which forces DEBUG).
- Log file: always at DEBUG level, rotated at 10 MB, retained for 10 days.

``setup_logging()`` runs at import time and removes loguru's default handler first,
otherwise every record would be emitted twice (once by loguru's built-in stderr sink,
once by ours).

Ported near-verbatim from the sibling caldav_tasks_api project so the two libraries
behave identically when used side by side; only the env var and the log directory name
differ.
"""

import os
import sys
from pathlib import Path

from loguru import logger
from platformdirs import user_log_dir

# The console sink's id is tracked at module level so enable_debug_logging() can swap
# that specific handler out without disturbing the file sink.
_console_handler_id: int | None = None

# Shared between setup_logging() and enable_debug_logging() so the two console sinks are
# visually identical and only differ in level.
_CONSOLE_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> "
    "| <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> "
    "- <level>{message}</level>"
)


def setup_logging() -> None:
    """Configure the loguru console and file sinks.

    Called automatically on module import. The console level comes from the
    ``CALDAV_CAL_API_LOG_LEVEL`` env var (default ``INFO``); the file sink always
    records at DEBUG so a failure can be diagnosed after the fact even when the console
    was quiet.

    Notes
    -----
    A failure to create the log file is never fatal: it is reported on stderr and the
    library carries on with console logging only. Losing logs must not stop a user from
    reading their calendar.
    """
    global _console_handler_id

    # Drop loguru's default stderr handler, otherwise our sink duplicates every record.
    logger.remove()

    console_log_level = os.environ.get("CALDAV_CAL_API_LOG_LEVEL", "INFO").upper()

    try:
        _console_handler_id = logger.add(
            sys.stderr,
            level=console_log_level,
            format=_CONSOLE_FORMAT,
            colorize=True,
        )
    except ValueError:
        # An unusable level string in the env var must not prevent logging entirely.
        _console_handler_id = logger.add(
            sys.stderr,
            level="INFO",
            format=_CONSOLE_FORMAT,
            colorize=True,
        )
        logger.warning(
            f"Invalid CALDAV_CAL_API_LOG_LEVEL '{console_log_level}'. "
            "Defaulting to INFO for console."
        )
        console_log_level = "INFO"

    app_name = "caldav-cal-api"
    app_author = "thiswillbeyourgithub"

    try:
        log_file_dir = Path(user_log_dir(app_name, app_author))
        log_file_dir.mkdir(parents=True, exist_ok=True)
        log_file_path = log_file_dir / "app.log"

        logger.add(
            log_file_path,
            level="DEBUG",
            rotation="10 MB",
            retention="10 days",
            compression="zip",
            format=(
                "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {process}:{thread} "
                "| {name}:{function}:{line} - {message}"
            ),
            encoding="utf-8",
        )
        logger.debug(
            f"Logging initialized. Console level: {console_log_level}. "
            f"Log file: {log_file_path}"
        )
    except Exception as e:
        print(f"CRITICAL: Failed to initialize file logger: {e}", file=sys.stderr)
        logger.error(f"Failed to initialize file logger: {e}")
        logger.warning(
            "File logging disabled due to an error. Check permissions or disk space."
        )


def enable_debug_logging() -> None:
    """Switch the console sink to DEBUG level.

    Intended for the CLI ``--debug`` flag, so debug output appears on the terminal
    without the user having to set ``CALDAV_CAL_API_LOG_LEVEL`` and re-run.
    """
    global _console_handler_id

    if _console_handler_id is not None:
        try:
            logger.remove(_console_handler_id)
        except ValueError:
            pass  # Already removed by someone else, harmless.

    _console_handler_id = logger.add(
        sys.stderr,
        level="DEBUG",
        format=_CONSOLE_FORMAT,
        colorize=True,
    )
    logger.debug("Console log level set to DEBUG via --debug flag.")


# Configure logging as a side effect of import: the package's __init__ imports this
# module first so that any log emitted during a later import is already formatted.
setup_logging()

__all__ = ["logger", "enable_debug_logging", "setup_logging"]
