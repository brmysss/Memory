import logging
import os
import sys


def setup_logger():
    _logger = logging.getLogger("Moment")
    level_name = (os.getenv("MOMENT_LOG_LEVEL") or "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    _logger.setLevel(level)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)

    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    console_handler.setFormatter(formatter)

    _logger.addHandler(console_handler)

    return _logger


logger = setup_logger()


class SuppressInvalidHTTPRequestFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            return "Invalid HTTP request received" not in record.getMessage()
        except Exception:
            return True
