"""Readable process logging for the Codex execution service."""

import logging
import os
import sys


class ConsoleFormatter(logging.Formatter):
    COLORS = {
        logging.DEBUG: "\033[90m",
        logging.INFO: "\033[36m",
        logging.WARNING: "\033[33m",
        logging.ERROR: "\033[31m",
        logging.CRITICAL: "\033[1;31m",
    }
    RESET = "\033[0m"

    def __init__(self, stream):
        super().__init__("%(asctime)s %(levelname)-8s %(name)s | %(message)s", "%H:%M:%S")
        self.use_color = stream.isatty() and not os.getenv("NO_COLOR")

    def format(self, record):
        line = super().format(record)
        if not self.use_color:
            return line
        return f"{self.COLORS.get(record.levelno, '')}{line}{self.RESET}"


def configure_logging():
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not root.handlers:
        root.addHandler(logging.StreamHandler(sys.stderr))
    for handler in root.handlers:
        if isinstance(handler, logging.StreamHandler):
            handler.setFormatter(ConsoleFormatter(handler.stream))

