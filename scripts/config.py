"""Tuning parameters, documented in config.yml."""

from pathlib import Path

import yaml


CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yml"
with open(CONFIG_PATH) as config_file:
    config = yaml.safe_load(config_file)

# Base URL used to list devices and send Connection Requests to them.
SIMULATOR_URL = config["simulator_url"].rstrip("/")

# Seconds a device may stay in each waiting state before it goes TIMED_OUT.
NOTIFIED_TIMEOUT_SEC = config["timeouts"]["notified_sec"]
DOWNLOAD_SENT_TIMEOUT_SEC = config["timeouts"]["download_sent_sec"]
ACCEPTED_TIMEOUT_SEC = config["timeouts"]["accepted_sec"]

# A device that does not call back is nudged again after RETRY_BACKOFF_SEC,
# then twice that, up to MAX_RETRIES times.
MAX_RETRIES = config["retry"]["max_retries"]
RETRY_BACKOFF_SEC = config["retry"]["backoff_sec"]

SCHEDULER_TICK_SEC = config["scheduler_tick_sec"]

# The failure threshold is checked only once this many devices finished,
# so the first failures cannot halt a campaign on their own.
MIN_SAMPLE_SIZE = config["min_sample_size"]

# A BOOT sooner than this after ACCEPTED cannot be the post-upgrade reboot:
# it is a duplicated/late BOOT from before the Download.
MIN_REBOOT_SEC = config["min_reboot_sec"]

LOG_LEVEL = config["log_level"].upper()

# SQLite file holding campaigns and their devices; relative to config.yml.
DB_PATH = CONFIG_PATH.parent / config["db_path"]
