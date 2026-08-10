import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path

LOG_FILE = Path(__file__).parent / "logs" / "pipeline.log"
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
EDT      = ZoneInfo("America/New_York")

IDLE_PHRASES = ("Sleeping", "No pending", "No run_cycle", "Nothing to index")


class _EDTFormatter(logging.Formatter):
    """Forces all log timestamps to EDT (America/New_York)."""
    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, tz=EDT)
        return dt.strftime(datefmt or "%Y-%m-%d %H:%M:%S EDT")


class _NoIdleFilter(logging.Filter):
    """Blocks idle polling messages from the log file."""
    def filter(self, record):
        return not any(p in record.getMessage() for p in IDLE_PHRASES)


def get_logger(agent_name: str) -> logging.Logger:
    logger = logging.getLogger(agent_name)

    if logger.handlers:
        return logger  # already configured

    logger.setLevel(logging.INFO)

    formatter = _EDTFormatter(
        fmt="[%(name)s] - %(asctime)s : %(message)s",
    )

    # console - shows everything including idle messages
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logger.addHandler(console)

    # file - idle polling messages excluded
    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler.addFilter(_NoIdleFilter())
    logger.addHandler(file_handler)

    return logger
