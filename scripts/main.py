
import logging
import os
from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from fastapi import FastAPI
from pydantic import BaseModel, Field, HttpUrl


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

app = FastAPI(title="Device Management Controller")

# Temporary in-memory storage
campaigns = []
devices = {}


def utc_now():
    return datetime.now(timezone.utc).isoformat()


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
    startTime: str | None = None
    completeTime: str | None = None


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

@app.post("/campaigns", status_code=201)
def create_campaign(request: CreateCampaignRequest):
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
        elif attempt["status"] in ("SUCCEEDED", "FAILED"):
            logger.info(
                "Duplicate/late TransferComplete ignored: deviceId=%s "
                "commandKey=%s existingStatus=%s",
                device_id,
                tc.commandKey,
                attempt["status"],
            )
        else:
            attempt["transferComplete"] = tc.model_dump()
            attempt["completedAt"] = utc_now()

            if tc.faultCode == 0:
                attempt["status"] = "SUCCEEDED"
            else:
                attempt["status"] = "FAILED"
                attempt["failureReason"] = (
                    f"TransferComplete faultCode={tc.faultCode}"
                )

            logger.info(
                "Upgrade outcome recorded: campaignId=%s deviceId=%s "
                "status=%s",
                matching_campaign["id"],
                device_id,
                attempt["status"],
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

            existing_attempt = campaign["devices"].get(device_id)

            # Prevent duplicate Download commands for this campaign/device.
            if existing_attempt is not None:
                logger.info(
                    "Download not issued: device already tracked in campaign "
                    "campaignId=%s deviceId=%s status=%s",
                    campaign["id"],
                    device_id,
                    existing_attempt["status"],
                )
                continue

            command_key = str(uuid4())

            campaign["devices"][device_id] = {
                "deviceId": device_id,
                "status": "DOWNLOAD_SENT",
                "commandKey": command_key,
                "currentVersion": request.firmwareVersion,
                "targetVersion": campaign["targetVersion"],
                "createdAt": utc_now(),
                "completedAt": None,
            }

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
            # Do not overwrite an outcome already recorded.
            if attempt["status"] in ("SUCCEEDED", "FAILED"):
                logger.info(
                    "Command response ignored: attempt already terminal "
                    "deviceId=%s status=%s",
                    device_id,
                    attempt["status"],
                )
                return {"status": "recorded"}

            if request.status == "ACCEPTED":
                attempt["status"] = "ACCEPTED"
            else:
                attempt["status"] = "FAILED"
                attempt["failureReason"] = (
                    request.reason or "Command rejected"
                )
                attempt["completedAt"] = utc_now()

            logger.info(
                "Command state updated: campaignId=%s deviceId=%s status=%s",
                campaign["id"],
                device_id,
                attempt["status"],
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
