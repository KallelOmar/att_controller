
import asyncio
import json
import logging
import os
import time
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from fastapi import BackgroundTasks, FastAPI
from pydantic import BaseModel, Field, HttpUrl


# Base URL used to list devices and send Connection Requests to them.
SIMULATOR_URL = os.getenv("SIMULATOR_URL", "http://localhost:9000").rstrip("/")

# Device states. SUCCEEDED and FAILED are final; TIMED_OUT can still be
# corrected by a late TransferComplete or BOOT for the same commandKey.
FINAL_STATES = ("SUCCEEDED", "FAILED")

# Waiting state -> (timeout in seconds, failureReason when it expires).
# ACCEPTED covers download + reboot, which can take several minutes.
STATE_TIMEOUTS = {
    "NOTIFIED": (int(os.getenv("NOTIFIED_TIMEOUT_SEC", "60")), "NO_CALLBACK"),
    "DOWNLOAD_SENT": (int(os.getenv("DOWNLOAD_SENT_TIMEOUT_SEC", "60")), "NO_ACK"),
    "ACCEPTED": (int(os.getenv("ACCEPTED_TIMEOUT_SEC", "600")), "NO_COMPLETION"),
}
TIMEOUT_CHECK_SEC = 5

# A BOOT sooner than this after ACCEPTED cannot be the post-upgrade reboot:
# it is a duplicated/late BOOT from before the Download.
MIN_REBOOT_SEC = int(os.getenv("MIN_REBOOT_SEC", "5"))


# Logging: PERIODIC Informs are logged at DEBUG, so they are hidden at INFO.
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
)
logger = logging.getLogger("controller")
logger.setLevel(LOG_LEVEL)

# uvicorn's access line can't tell PERIODIC from other events,
# so show it only in DEBUG mode.
if LOG_LEVEL != "DEBUG":
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)

# Temporary in-memory storage
campaigns = []
devices = {}


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

    logger.info(
        "Device status: campaignId=%s deviceId=%s %s -> %s reason=%s",
        campaign["id"],
        attempt["deviceId"],
        previous,
        status,
        reason,
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


async def timeout_loop():
    while True:
        await asyncio.sleep(TIMEOUT_CHECK_SEC)
        try:
            check_timeouts()
        except Exception:
            logger.exception("Timeout check failed")


@asynccontextmanager
async def lifespan(_app):
    task = asyncio.create_task(timeout_loop())
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


def send_connection_requests(campaign):
    """Track every target device as PENDING, then ask each to call back."""
    current_version = campaign["deviceFilter"]["currentVersion"]
    try:
        targets = fetch_target_devices(current_version)
    except (OSError, ValueError) as exc:
        logger.error(
            "Cannot list target devices: campaignId=%s error=%s",
            campaign["id"],
            exc,
        )
        return

    for device_id in targets:
        campaign["devices"].setdefault(
            device_id, new_attempt(device_id, campaign)
        )
    logger.info(
        "Sending Connection Requests: campaignId=%s currentVersion=%s "
        "devices=%d",
        campaign["id"],
        current_version,
        len(targets),
    )

    for device_id in targets:
        attempt = campaign["devices"][device_id]
        if attempt["status"] != "PENDING":
            continue

        # Set before sending: the device may call back before urlopen returns.
        set_device_status(campaign, attempt, "NOTIFIED")
        url = f"{SIMULATOR_URL}/devices/{device_id}/connection-request"
        try:
            urllib.request.urlopen(
                urllib.request.Request(url, method="POST"), timeout=5
            )
        except OSError as exc:
            logger.warning(
                "Connection Request failed: deviceId=%s error=%s",
                device_id,
                exc,
            )
            set_device_status(campaign, attempt, "PENDING")


@app.post("/campaigns", status_code=201)
def create_campaign(
    request: CreateCampaignRequest,
    background_tasks: BackgroundTasks,
):
    current_version = request.deviceFilter.currentVersion

    for existing in campaigns:
        if (
            existing["deviceFilter"]["currentVersion"] == current_version
            and existing["targetVersion"] == request.targetVersion
            and existing["status"] in ("PENDING", "IN_PROGRESS")
        ):
            logger.info(
                "Matching active campaign already exists: id=%s",
                existing["id"],
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
        "devices": {},
    }

    campaigns.append(campaign)

    # Runs after the response is sent, so the API call returns immediately.
    background_tasks.add_task(send_connection_requests, campaign)

    logger.info(
        "Campaign created: id=%s currentVersion=%s targetVersion=%s",
        campaign["id"],
        current_version,
        campaign["targetVersion"],
    )

    return {
        "id": campaign["id"],
        "status": campaign["status"],
        "message": "Campaign created successfully",
    }


# ---------- Device API ----------

@app.post("/devices/{device_id}/inform")
def receive_inform(device_id: str, request: InformRequest):
    # PERIODIC Informs are routine and frequent: log them only at DEBUG.
    event_log_level = (
        logging.DEBUG if request.event == "PERIODIC" else logging.INFO
    )

    logger.log(
        event_log_level,
        "INFORM received: deviceId=%s event=%s firmwareVersion=%s",
        device_id,
        request.event,
        request.firmwareVersion,
    )

    # The path ID is authoritative. Log any mismatch in the body.
    if request.deviceId != device_id:
        logger.warning(
            "Device ID mismatch: path=%s body=%s",
            device_id,
            request.deviceId,
        )

    # Create or update the device inventory. Any Inform registers an unknown
    # device: after a controller restart, devices only send PERIODIC until
    # their next reboot, so waiting for BOOT would hide them.
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
        logger.info(
            "Device registered: deviceId=%s via event=%s",
            device_id,
            request.event,
        )

    device["firmwareVersion"] = request.firmwareVersion
    device["lastSeen"] = utc_now()
    device["lastEvent"] = request.event
    if request.event == "BOOT":
        device["lastBoot"] = device["lastSeen"]

    logger.log(
        event_log_level,
        "Device updated: deviceId=%s firmwareVersion=%s",
        device_id,
        request.firmwareVersion,
    )

    # Process TransferComplete if the event contains it.
    if request.transferComplete is not None:
        tc = request.transferComplete

        logger.info(
            "TRANSFER_COMPLETE received: deviceId=%s commandKey=%s "
            "faultCode=%s startTime=%s completeTime=%s",
            device_id,
            tc.commandKey,
            tc.faultCode,
            tc.startTime,
            tc.completeTime,
        )

        matching_campaign = None
        attempt = None

        for campaign in campaigns:
            candidate = campaign["devices"].get(device_id)
            if candidate and candidate.get("commandKey") == tc.commandKey:
                matching_campaign = campaign
                attempt = candidate
                break

        if attempt is None:
            logger.warning(
                "Unknown commandKey in TransferComplete: deviceId=%s "
                "commandKey=%s",
                device_id,
                tc.commandKey,
            )
        elif attempt["status"] in FINAL_STATES:
            logger.info(
                "Duplicate/late TransferComplete ignored: deviceId=%s "
                "commandKey=%s existingStatus=%s",
                device_id,
                tc.commandKey,
                attempt["status"],
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
                logger.info(
                    "Stale BOOT ignored: campaignId=%s deviceId=%s",
                    campaign["id"],
                    device_id,
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
        for campaign in campaigns:
            if campaign["status"] not in ("PENDING", "IN_PROGRESS"):
                continue

            expected_version = campaign["deviceFilter"]["currentVersion"]

            if request.firmwareVersion != expected_version:
                continue

            attempt = campaign["devices"].get(device_id)

            # Only devices targeted by the campaign and not yet sent a
            # Download get one: prevents duplicate Download commands.
            if attempt is None or attempt["status"] not in (
                "PENDING",
                "NOTIFIED",
            ):
                logger.info(
                    "Download not issued: campaignId=%s deviceId=%s "
                    "status=%s",
                    campaign["id"],
                    device_id,
                    attempt["status"] if attempt else "NOT_TARGETED",
                )
                continue

            command_key = str(uuid4())
            attempt["commandKey"] = command_key
            set_device_status(campaign, attempt, "DOWNLOAD_SENT")

            campaign["status"] = "IN_PROGRESS"

            commands.append(
                {
                    "type": "DOWNLOAD",
                    "commandKey": command_key,
                    "url": campaign["imageUrl"],
                    "targetVersion": campaign["targetVersion"],
                }
            )

            logger.info(
                "DOWNLOAD command prepared: campaignId=%s deviceId=%s "
                "commandKey=%s targetVersion=%s",
                campaign["id"],
                device_id,
                command_key,
                campaign["targetVersion"],
            )

            # One command per Inform in this initial version.
            break

        if not commands:
            logger.info(
                "No Download command issued: deviceId=%s firmwareVersion=%s",
                device_id,
                request.firmwareVersion,
            )

    return {"commands": commands}


@app.post("/devices/{device_id}/command-response")
def receive_command_response(
    device_id: str,
    request: CommandResponseRequest,
):
    logger.info(
        "COMMAND_RESPONSE received: deviceId=%s commandKey=%s status=%s "
        "reason=%s",
        device_id,
        request.commandKey,
        request.status,
        request.reason,
    )

    for campaign in campaigns:
        attempt = campaign["devices"].get(device_id)

        if attempt and attempt.get("commandKey") == request.commandKey:
            # Only a first answer to the Download counts; duplicates and
            # answers after the outcome is known are ignored.
            if attempt["status"] not in ("DOWNLOAD_SENT", "TIMED_OUT"):
                logger.info(
                    "Command response ignored: deviceId=%s status=%s",
                    device_id,
                    attempt["status"],
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

    logger.warning(
        "Command response has unknown commandKey: deviceId=%s commandKey=%s",
        device_id,
        request.commandKey,
    )
    return {"status": "unknown_command"}


@app.get("/health")
def health():
    return {"status": "ok"}
