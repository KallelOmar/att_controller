
import asyncio
import json
import logging
import sys
import threading
import time
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import uuid4

import yaml
from fastapi import BackgroundTasks, FastAPI, HTTPException
from pydantic import BaseModel, Field, HttpUrl


# Tuning parameters, documented in config.yml.
CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yml"
with open(CONFIG_PATH) as config_file:
    config = yaml.safe_load(config_file)

# Base URL used to list devices and send Connection Requests to them.
SIMULATOR_URL = config["simulator_url"].rstrip("/")

# Device states. SUCCEEDED and FAILED are final; TIMED_OUT can still be
# corrected by a late TransferComplete or BOOT for the same commandKey.
DEVICE_STATES = (
    "PENDING",
    "NOTIFIED",
    "DOWNLOAD_SENT",
    "ACCEPTED",
    "SUCCEEDED",
    "FAILED",
    "TIMED_OUT",
)
FINAL_STATES = ("SUCCEEDED", "FAILED")

# States in which the controller is waiting on the device. They count
# toward the campaign's maxConcurrent.
IN_FLIGHT_STATES = ("NOTIFIED", "DOWNLOAD_SENT", "ACCEPTED")

# Campaign states that still own their devices: a second campaign for the
# same upgrade is refused while one of these exists. HALTED and COMPLETED
# are final.
ACTIVE_CAMPAIGN_STATES = ("PENDING", "IN_PROGRESS", "PAUSED")

# Waiting state -> (timeout in seconds, failureReason when it expires).
# ACCEPTED covers download + reboot, which can take several minutes.
STATE_TIMEOUTS = {
    "NOTIFIED": (config["timeouts"]["notified_sec"], "NO_CALLBACK"),
    "DOWNLOAD_SENT": (config["timeouts"]["download_sent_sec"], "NO_ACK"),
    "ACCEPTED": (config["timeouts"]["accepted_sec"], "NO_COMPLETION"),
}
SCHEDULER_TICK_SEC = config["scheduler_tick_sec"]

# The failure threshold is checked only once this many devices finished,
# so the first failures cannot halt a campaign on their own.
MIN_SAMPLE_SIZE = config["min_sample_size"]

# A BOOT sooner than this after ACCEPTED cannot be the post-upgrade reboot:
# it is a duplicated/late BOOT from before the Download.
MIN_REBOOT_SEC = config["min_reboot_sec"]


# ---------- Logging ----------
# Line format: UTC time, level, one [subject] tag, then key=value details.
#   2026-10-29 10:15:04.502Z INFO  [GW-B74D0FB132E7] ACCEPTED -> SUCCEEDED
#       campaign=9a86f0c9-1b2c-4d3e-8f40-5a6b7c8d9e0f
#   2026-10-29 10:16:40.002Z WARN
#       [campaign 9a86f0c9-1b2c-4d3e-8f40-5a6b7c8d9e0f] IN_PROGRESS -> HALTED
# Levels: ERROR = the controller itself fails; WARN = unexpected but
# handled; INFO = normal progress, including device failures, which are
# expected outcomes; DEBUG = high-volume detail such as PERIODIC Informs.
LOG_LEVEL = config["log_level"].upper()

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
    # The ID comes from the URL: pass it as an argument, never in the format.
    # The campaign goes last as a detail, so the device stays the only tag.
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

# Temporary in-memory storage
campaigns = []
devices = {}

# Handlers run in a thread pool next to the scheduler thread: hold this
# lock for every check-then-update of the state above.
state_lock = threading.Lock()


def utc_now():
    return datetime.now(timezone.utc).isoformat()


# ---------- Device state machine ----------

def new_attempt(device_id, campaign):
    return {
        "deviceId": device_id,
        "status": "PENDING",
        "commandKey": None,
        "currentVersion": campaign["deviceFilter"]["currentVersion"],
        "targetVersion": campaign["targetVersion"],
        "failureReason": None,
        "stateSince": time.time(),
        "updatedAt": utc_now(),
    }


def set_device_status(campaign, attempt, status, reason=None):
    previous = attempt["status"]
    attempt["status"] = status
    attempt["failureReason"] = reason
    attempt["stateSince"] = time.time()
    attempt["updatedAt"] = utc_now()

    log_device(
        logging.INFO,
        attempt["deviceId"],
        "%s -> %s%s",
        previous,
        status,
        f" reason={reason}" if reason else "",
        campaign=campaign,
    )


def check_timeouts():
    now = time.time()
    for campaign in campaigns:
        for attempt in list(campaign["devices"].values()):
            timeout = STATE_TIMEOUTS.get(attempt["status"])
            if timeout is None:
                continue
            seconds, reason = timeout
            if now - attempt["stateSince"] > seconds:
                set_device_status(campaign, attempt, "TIMED_OUT", reason)


# ---------- Scheduler ----------

def count_in_flight(campaign):
    return sum(
        attempt["status"] in IN_FLIGHT_STATES
        for attempt in campaign["devices"].values()
    )


def campaign_counts(campaign):
    """Device counts per state and the failure rate of finished devices."""
    by_state = dict.fromkeys(DEVICE_STATES, 0)
    failure_reasons = {}
    for attempt in campaign["devices"].values():
        by_state[attempt["status"]] += 1
        if attempt["status"] in ("FAILED", "TIMED_OUT"):
            reason = attempt["failureReason"]
            failure_reasons[reason] = failure_reasons.get(reason, 0) + 1

    # TODO: exclude TIMED_OUT with reason NO_CALLBACK: the device was
    # offline and never got the image, so it says nothing about the
    # firmware and should not count toward halting the campaign.
    failed = by_state["FAILED"] + by_state["TIMED_OUT"]
    finished = by_state["SUCCEEDED"] + failed
    return {
        "total": len(campaign["devices"]),
        "inFlight": sum(by_state[state] for state in IN_FLIGHT_STATES),
        "finished": finished,
        "succeeded": by_state["SUCCEEDED"],
        "failed": failed,
        "failureRatePercent": 100 * failed / finished if finished else 0.0,
        "byState": by_state,
        "failureReasons": dict(sorted(failure_reasons.items())),
    }


def update_campaign_status(campaign):
    """Halt above the failure threshold; complete once every device is done."""
    counts = campaign_counts(campaign)
    previous = campaign["status"]
    if (
        counts["finished"] >= MIN_SAMPLE_SIZE
        and counts["failureRatePercent"] > campaign["failureThresholdPercent"]
    ):
        campaign["status"] = "HALTED"
    elif counts["byState"]["PENDING"] == 0 and count_in_flight(campaign) == 0:
        campaign["status"] = "COMPLETED"
    else:
        return

    campaign["endedAt"] = utc_now()
    log_campaign(
        logging.WARNING if campaign["status"] == "HALTED" else logging.INFO,
        campaign,
        "%s -> %s failed=%d finished=%d total=%d "
        "failureRate=%.1f%% threshold=%s%%",
        previous,
        campaign["status"],
        counts["failed"],
        counts["finished"],
        counts["total"],
        counts["failureRatePercent"],
        campaign["failureThresholdPercent"],
    )


def select_devices_to_notify(campaign):
    """Move PENDING devices to NOTIFIED while under maxConcurrent."""
    free = campaign["maxConcurrent"] - count_in_flight(campaign)
    if free <= 0:
        return []

    selected = [
        attempt
        for attempt in campaign["devices"].values()
        if attempt["status"] == "PENDING"
    ][:free]

    # Set before sending: the device may call back before urlopen returns.
    for attempt in selected:
        set_device_status(campaign, attempt, "NOTIFIED")
    return selected


def send_connection_request(campaign, attempt):
    device_id = attempt["deviceId"]
    url = f"{SIMULATOR_URL}/devices/{device_id}/connection-request"
    try:
        urllib.request.urlopen(
            urllib.request.Request(url, method="POST"), timeout=5
        )
    except OSError as exc:
        log_device(
            logging.WARNING,
            device_id,
            "connection request failed: %s",
            exc,
            campaign=campaign,
        )
        # Back to the queue; a later tick retries it.
        with state_lock:
            if attempt["status"] == "NOTIFIED":
                set_device_status(campaign, attempt, "PENDING")


def scheduler_tick():
    with state_lock:
        # Timeouts first, so the slots they free are refilled this tick.
        check_timeouts()
        # Before nudging: a campaign over its threshold starts no device.
        for campaign in campaigns:
            if campaign["status"] == "IN_PROGRESS":
                update_campaign_status(campaign)
        to_notify = [
            (campaign, attempt)
            for campaign in campaigns
            if campaign["status"] == "IN_PROGRESS"
            for attempt in select_devices_to_notify(campaign)
        ]

    # Outside the lock: the devices' callbacks need it while we send.
    for campaign, attempt in to_notify:
        send_connection_request(campaign, attempt)


async def scheduler_loop():
    while True:
        await asyncio.sleep(SCHEDULER_TICK_SEC)
        try:
            # urlopen blocks: run in a thread so the API keeps serving.
            await asyncio.to_thread(scheduler_tick)
        except Exception:
            logger.exception("[controller] scheduler tick failed")


@asynccontextmanager
async def lifespan(_app):
    task = asyncio.create_task(scheduler_loop())
    yield
    task.cancel()


app = FastAPI(title="Device Management Controller", lifespan=lifespan)


# ---------- Campaign API models ----------

class DeviceFilter(BaseModel):
    currentVersion: str = Field(min_length=1)


class CreateCampaignRequest(BaseModel):
    targetVersion: str = Field(min_length=1)
    imageUrl: HttpUrl
    deviceFilter: DeviceFilter
    maxConcurrent: int = Field(gt=0)
    failureThresholdPercent: float = Field(ge=0, le=100)


# ---------- Device API models ----------

class TransferComplete(BaseModel):
    commandKey: str
    faultCode: int
    startTime: float | None = None
    completeTime: float | None = None


class InformRequest(BaseModel):
    deviceId: str
    firmwareVersion: str
    event: Literal[
        "BOOT",
        "CONNECTION_REQUEST",
        "PERIODIC",
        "TRANSFER_COMPLETE",
    ]
    transferComplete: TransferComplete | None = None


class CommandResponseRequest(BaseModel):
    commandKey: str
    status: Literal["ACCEPTED", "REJECTED"]
    reason: str | None = None


# ---------- Campaign API ----------

def fetch_target_devices(current_version: str):
    query = urllib.parse.urlencode({"version": current_version})
    with urllib.request.urlopen(
        f"{SIMULATOR_URL}/devices?{query}", timeout=10
    ) as response:
        return [device["deviceId"] for device in json.load(response)]


def load_targets(campaign):
    """Track every target device as PENDING; the scheduler nudges them."""
    current_version = campaign["deviceFilter"]["currentVersion"]
    try:
        targets = fetch_target_devices(current_version)
    except (OSError, ValueError) as exc:
        log_campaign(
            logging.ERROR,
            campaign,
            "cannot list target devices from %s: %s",
            SIMULATOR_URL,
            exc,
        )
        return

    with state_lock:
        for device_id in targets:
            campaign["devices"].setdefault(
                device_id, new_attempt(device_id, campaign)
            )
        # Paused while loading: stay PAUSED, resume will start it.
        if campaign["status"] == "PAUSED":
            campaign["statusBeforePause"] = "IN_PROGRESS"
            paused = True
        else:
            campaign["status"] = "IN_PROGRESS"
            paused = False

    if paused:
        log_campaign(
            logging.INFO,
            campaign,
            "targets=%d loaded while PAUSED, resume will start them",
            len(targets),
        )
    else:
        log_campaign(
            logging.INFO,
            campaign,
            "PENDING -> IN_PROGRESS targets=%d maxConcurrent=%d",
            len(targets),
            campaign["maxConcurrent"],
        )


@app.post("/campaigns", status_code=201)
def create_campaign(
    request: CreateCampaignRequest,
    background_tasks: BackgroundTasks,
):
    current_version = request.deviceFilter.currentVersion

    with state_lock:
        for existing in campaigns:
            if (
                existing["deviceFilter"]["currentVersion"] == current_version
                and existing["targetVersion"] == request.targetVersion
                and existing["status"] in ACTIVE_CAMPAIGN_STATES
            ):
                log_campaign(
                    logging.INFO,
                    existing,
                    "creation request matches this active campaign, "
                    "none created",
                )
                return {
                    "id": existing["id"],
                    "status": existing["status"],
                    "message": "Matching active campaign already exists",
                }

        campaign = {
            "id": str(uuid4()),
            "targetVersion": request.targetVersion,
            "imageUrl": str(request.imageUrl),
            "deviceFilter": request.deviceFilter.model_dump(),
            "maxConcurrent": request.maxConcurrent,
            "failureThresholdPercent": request.failureThresholdPercent,
            "status": "PENDING",
            "statusBeforePause": None,
            "createdAt": utc_now(),
            "pausedAt": None,
            "endedAt": None,
            "devices": {},
        }

        campaigns.append(campaign)

    # Runs after the response is sent, so the API call returns immediately.
    background_tasks.add_task(load_targets, campaign)

    log_campaign(
        logging.INFO,
        campaign,
        "created currentVersion=%s targetVersion=%s maxConcurrent=%d "
        "failureThreshold=%s%%",
        current_version,
        campaign["targetVersion"],
        campaign["maxConcurrent"],
        campaign["failureThresholdPercent"],
    )

    return {
        "id": campaign["id"],
        "status": campaign["status"],
        "message": "Campaign created successfully",
    }


def find_campaign(campaign_id, action):
    """Log the API call, then return the campaign or raise 404."""
    # The ID comes from the URL: pass it as an argument, never in the format.
    logger.info("[campaign %s] %s", campaign_id, action)
    campaign = next((c for c in campaigns if c["id"] == campaign_id), None)
    if campaign is None:
        logger.info("[campaign %s] not found", campaign_id)
        raise HTTPException(status_code=404, detail="Campaign not found")
    return campaign


@app.get("/campaigns/{campaign_id}")
def get_campaign(campaign_id: str):
    with state_lock:
        campaign = find_campaign(campaign_id, "get campaign details")

        # One snapshot under the lock, so status and counts agree.
        progress = campaign_counts(campaign)
        progress["failureRatePercent"] = round(
            progress["failureRatePercent"], 1
        )
        progress["minSampleSize"] = MIN_SAMPLE_SIZE
        return {
            "id": campaign["id"],
            "status": campaign["status"],
            "targetVersion": campaign["targetVersion"],
            "imageUrl": campaign["imageUrl"],
            "deviceFilter": campaign["deviceFilter"],
            "maxConcurrent": campaign["maxConcurrent"],
            "failureThresholdPercent": campaign["failureThresholdPercent"],
            "createdAt": campaign["createdAt"],
            "pausedAt": campaign["pausedAt"],
            "endedAt": campaign["endedAt"],
            "progress": progress,
        }


@app.post("/campaigns/{campaign_id}/pause")
def pause_campaign(campaign_id: str):
    with state_lock:
        campaign = find_campaign(campaign_id, "pause campaign")
        status = campaign["status"]

        if status == "PAUSED":
            log_campaign(
                logging.INFO, campaign, "pause ignored, already PAUSED"
            )
            return {
                "id": campaign["id"],
                "status": status,
                "message": "Campaign is already paused",
            }

        if status not in ("PENDING", "IN_PROGRESS"):
            log_campaign(
                logging.INFO, campaign, "pause refused, campaign is %s", status
            )
            raise HTTPException(
                status_code=409,
                detail=f"Campaign is {status}: only a PENDING or "
                "IN_PROGRESS campaign can be paused",
            )

        # NOTIFIED devices have no Download yet: requeue them, so a
        # callback during the pause does not end as NO_CALLBACK. Devices
        # with a Download keep going and are still tracked.
        requeued = 0
        for attempt in campaign["devices"].values():
            if attempt["status"] == "NOTIFIED":
                set_device_status(campaign, attempt, "PENDING")
                requeued += 1

        campaign["statusBeforePause"] = status
        campaign["status"] = "PAUSED"
        campaign["pausedAt"] = utc_now()
        log_campaign(
            logging.INFO,
            campaign,
            "%s -> PAUSED requeued=%d inFlight=%d",
            status,
            requeued,
            count_in_flight(campaign),
        )
        return {
            "id": campaign["id"],
            "status": campaign["status"],
            "message": "Campaign paused",
        }


@app.post("/campaigns/{campaign_id}/resume")
def resume_campaign(campaign_id: str):
    with state_lock:
        campaign = find_campaign(campaign_id, "resume campaign")
        status = campaign["status"]

        if status != "PAUSED":
            log_campaign(
                logging.INFO,
                campaign,
                "resume refused, campaign is %s",
                status,
            )
            raise HTTPException(
                status_code=409,
                detail=f"Campaign is {status}: only a PAUSED campaign "
                "can be resumed",
            )

        # No cursor needed: the next tick nudges the devices still PENDING.
        campaign["status"] = campaign["statusBeforePause"]
        campaign["statusBeforePause"] = None
        campaign["pausedAt"] = None
        log_campaign(
            logging.INFO, campaign, "PAUSED -> %s", campaign["status"]
        )
        return {
            "id": campaign["id"],
            "status": campaign["status"],
            "message": "Campaign resumed",
        }


# ---------- Device API ----------

@app.post("/devices/{device_id}/inform")
def receive_inform(device_id: str, request: InformRequest):
    # PERIODIC Informs are routine and frequent: log them only at DEBUG.
    event_log_level = (
        logging.DEBUG if request.event == "PERIODIC" else logging.INFO
    )

    # One line per Inform, with the TransferComplete result when present.
    tc = request.transferComplete
    details = ""
    if tc is not None:
        details = f" commandKey={tc.commandKey} faultCode={tc.faultCode}"
        if tc.startTime is not None and tc.completeTime is not None:
            details += f" duration={tc.completeTime - tc.startTime:.0f}s"
    log_device(
        event_log_level,
        device_id,
        "inform event=%s firmwareVersion=%s%s",
        request.event,
        request.firmwareVersion,
        details,
    )

    # The path ID is authoritative. Log any mismatch in the body.
    if request.deviceId != device_id:
        log_device(
            logging.WARNING,
            device_id,
            "body deviceId=%s differs from the URL",
            request.deviceId,
        )

    with state_lock:
        # Create or update the device inventory. Any Inform registers an
        # unknown device: after a controller restart, devices only send
        # PERIODIC until their next reboot, so waiting for BOOT would hide
        # them.
        device = devices.get(device_id)
        if device is None:
            device = {
                "deviceId": device_id,
                "firmwareVersion": request.firmwareVersion,
                "lastSeen": None,
                "lastEvent": None,
                "lastBoot": None,
            }
            devices[device_id] = device
            log_device(
                logging.DEBUG,
                device_id,
                "registered in the inventory via event=%s",
                request.event,
            )

        device["firmwareVersion"] = request.firmwareVersion
        device["lastSeen"] = utc_now()
        device["lastEvent"] = request.event
        if request.event == "BOOT":
            device["lastBoot"] = device["lastSeen"]

        # Process TransferComplete if the event contains it.
        if tc is not None:
            matching_campaign = None
            attempt = None

            for campaign in campaigns:
                candidate = campaign["devices"].get(device_id)
                if candidate and candidate.get("commandKey") == tc.commandKey: #Correlate the TransferComplete with the correponding Download using commandKey.
                    matching_campaign = campaign
                    attempt = candidate
                    break

            if attempt is None:
                log_device(
                    logging.WARNING,
                    device_id,
                    "TransferComplete with unknown commandKey=%s ignored",
                    tc.commandKey,
                )
            elif attempt["status"] in FINAL_STATES:
                log_device(
                    logging.INFO,
                    device_id,
                    "duplicate/late TransferComplete ignored, already %s",
                    attempt["status"],
                    campaign=matching_campaign,
                )
            else:
                attempt["transferComplete"] = tc.model_dump()
                if tc.faultCode == 0:
                    set_device_status(matching_campaign, attempt, "SUCCEEDED")
                else:
                    set_device_status(
                        matching_campaign,
                        attempt,
                        "FAILED",
                        f"FAULT:{tc.faultCode}",
                    )

        # A BOOT without TransferComplete after a Download: the reported
        # firmware version tells whether the upgrade was applied.
        if request.event == "BOOT":
            for campaign in campaigns:
                attempt = campaign["devices"].get(device_id)
                if (
                    attempt is None
                    or attempt["commandKey"] is None
                    or attempt["status"] not in ("ACCEPTED", "TIMED_OUT")
                ):
                    continue

                if (
                    attempt["status"] == "ACCEPTED"
                    and time.time() - attempt["stateSince"] < MIN_REBOOT_SEC
                ):
                    log_device(
                        logging.INFO,
                        device_id,
                        "stale BOOT ignored, sooner than %ss after ACCEPTED",
                        MIN_REBOOT_SEC,
                        campaign=campaign,
                    )
                    continue

                if request.firmwareVersion == attempt["targetVersion"]:
                    set_device_status(campaign, attempt, "SUCCEEDED")
                elif request.firmwareVersion == attempt["currentVersion"]:
                    set_device_status(campaign, attempt, "FAILED", "REVERTED")
                else:
                    set_device_status(
                        campaign,
                        attempt,
                        "FAILED",
                        f"UNEXPECTED_VERSION:{request.firmwareVersion}",
                    )

        # Default response: no commands to deliver.
        commands = []

        # A connection-request Inform is the opportunity to return a command.
        if request.event == "CONNECTION_REQUEST":
            reason_logged = False
            for campaign in campaigns:
                if campaign["status"] not in ACTIVE_CAMPAIGN_STATES:
                    continue

                expected_version = campaign["deviceFilter"]["currentVersion"]

                if request.firmwareVersion != expected_version:
                    continue

                # Late callback to a Connection Request sent before the
                # pause: the device was requeued and is nudged on resume.
                if campaign["status"] == "PAUSED":
                    log_device(
                        logging.INFO,
                        device_id,
                        "no DOWNLOAD, campaign is PAUSED",
                        campaign=campaign,
                    )
                    reason_logged = True
                    continue

                attempt = campaign["devices"].get(device_id)

                # Only devices targeted by the campaign and not yet sent a
                # Download get one: prevents duplicate Download commands.
                if attempt is None or attempt["status"] not in (
                    "PENDING",
                    "NOTIFIED",
                ):
                    log_device(
                        logging.INFO,
                        device_id,
                        "no DOWNLOAD, device is %s",
                        attempt["status"] if attempt else "NOT_TARGETED",
                        campaign=campaign,
                    )
                    reason_logged = True
                    continue

                command_key = str(uuid4())
                attempt["commandKey"] = command_key
                set_device_status(campaign, attempt, "DOWNLOAD_SENT")

                commands.append(
                    {
                        "type": "DOWNLOAD",
                        "commandKey": command_key,
                        "url": campaign["imageUrl"],
                        "targetVersion": campaign["targetVersion"],
                    }
                )

                log_device(
                    logging.INFO,
                    device_id,
                    "DOWNLOAD issued commandKey=%s targetVersion=%s",
                    command_key,
                    campaign["targetVersion"],
                    campaign=campaign,
                )

                # One command per Inform in this initial version.
                break

            if not commands and not reason_logged:
                log_device(
                    logging.INFO,
                    device_id,
                    "no DOWNLOAD, no active campaign for firmwareVersion=%s",
                    request.firmwareVersion,
                )

    return {"commands": commands}


@app.post("/devices/{device_id}/command-response")
def receive_command_response(
    device_id: str,
    request: CommandResponseRequest,
):
    log_device(
        logging.INFO,
        device_id,
        "command-response status=%s commandKey=%s%s",
        request.status,
        request.commandKey,
        f" reason={request.reason}" if request.reason else "",
    )

    with state_lock:
        for campaign in campaigns:
            attempt = campaign["devices"].get(device_id)

            if attempt and attempt.get("commandKey") == request.commandKey:
                # Only a first answer to the Download counts; duplicates and
                # answers after the outcome is known are ignored.
                if attempt["status"] not in ("DOWNLOAD_SENT", "TIMED_OUT"):
                    log_device(
                        logging.INFO,
                        device_id,
                        "duplicate/late command-response ignored, "
                        "already %s",
                        attempt["status"],
                        campaign=campaign,
                    )
                    return {"status": "recorded"}

                if request.status == "ACCEPTED":
                    set_device_status(campaign, attempt, "ACCEPTED")
                else:
                    set_device_status(
                        campaign,
                        attempt,
                        "FAILED",
                        f"REJECTED:{request.reason or 'UNKNOWN'}",
                    )
                return {"status": "recorded"}

    log_device(
        logging.WARNING,
        device_id,
        "command-response with unknown commandKey=%s ignored",
        request.commandKey,
    )
    return {"status": "unknown_command"}


@app.get("/health")
def health():
    return {"status": "ok"}
