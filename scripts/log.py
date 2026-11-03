import logging
import sys
import time

from config import LOG_LEVEL


GREEN, YELLOW, RED, DIM, RESET = (
    "\033[32m", "\033[33m", "\033[31m", "\033[2m", "\033[0m"
)


class LogFormatter(logging.Formatter):
    LEVEL_NAMES = {"WARNING": "WARN", "CRITICAL": "CRIT"}
    LEVEL_COLORS = {
        "DEBUG": DIM, "INFO": GREEN, "WARN": YELLOW, "ERROR": RED, "CRIT": RED
    }
    STATE_COLORS = {
        "SUCCEEDED": GREEN,
        "COMPLETED": GREEN,
        "FAILED": RED,
        "HALTED": RED,
        "TIMED_OUT": YELLOW,
    }

    def __init__(self, use_colors):
        super().__init__(datefmt="%Y-%m-%d %H:%M:%S")
        self.converter = time.gmtime
        self.use_colors = use_colors

    def format(self, record):
        level = self.LEVEL_NAMES.get(record.levelname, record.levelname)
        message = record.getMessage()
        # Lines from uvicorn or other libraries get a tag like ours.
        if not record.name.startswith("controller"):
            message = f"[{record.name.split('.')[0]}] {message}"

        if self.use_colors:
            for state, color in self.STATE_COLORS.items():
                message = message.replace(
                    f"-> {state}", f"-> {color}{state}{RESET}"
                )
            level = f"{self.LEVEL_COLORS.get(level, '')}{level:<5}{RESET}"
        else:
            level = f"{level:<5}"

        line = (
            f"{self.formatTime(record, self.datefmt)}"
            f".{int(record.msecs):03d}Z {level} {message}"
        )
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


# Colours only on a terminal: logs redirected to a file stay plain text.
log_handler = logging.StreamHandler()
log_handler.setFormatter(LogFormatter(use_colors=sys.stderr.isatty()))

# Replaces the handlers uvicorn installed, so its lines share our format.
logging.getLogger().handlers = [log_handler]
for name in ("uvicorn", "uvicorn.access"):
    logging.getLogger(name).handlers = []
    logging.getLogger(name).propagate = True

logger = logging.getLogger("controller")
logger.setLevel(LOG_LEVEL)

# uvicorn's access line can't tell PERIODIC from other events,
# so show it only in DEBUG mode.
if LOG_LEVEL != "DEBUG":
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def log_campaign(level, campaign, message, *args):
    logger.log(level, "[campaign %s] " + message, campaign["id"], *args)


def log_device(level, device_id, message, *args, campaign=None):
    if campaign is None:
        logger.log(level, "[%s] " + message, device_id, *args)
    else:
        logger.log(
            level,
            "[%s] " + message + ", campaign=%s",
            device_id,
            *args,
            campaign["id"],
        )
