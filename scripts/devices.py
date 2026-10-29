"""Device state machine and handling of what the devices report.

Callers hold state_lock: the API handlers and the scheduler take it.
"""

import logging
import time
from typing import Literal

from pydantic import BaseModel

import db
import metrics
from config import (
    ACCEPTED_TIMEOUT_SEC,
    DOWNLOAD_SENT_TIMEOUT_SEC,
    MAX_RETRIES,
    MIN_REBOOT_SEC,
    NOTIFIED_TIMEOUT_SEC,
    RETRY_BACKOFF_SEC,
)
from log import log_device
from state import campaigns, devices, utc_now


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

# Waiting state -> (timeout in seconds, failureReason when it expires).
# ACCEPTED covers download + reboot, which can take several minutes.
STATE_TIMEOUTS = {
    "NOTIFIED": (NOTIFIED_TIMEOUT_SEC, "NO_CALLBACK"),
    "DOWNLOAD_SENT": (DOWNLOAD_SENT_TIMEOUT_SEC, "NO_ACK"),
    "ACCEPTED": (ACCEPTED_TIMEOUT_SEC, "NO_COMPLETION"),
}


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
        # Connection Requests resent so far, and when the next one may go.
        "retries": 0,
        "retryAt": None,
    }


def set_device_status(campaign, attempt, status, reason=None, note=""):
    previous = attempt["status"]
    attempt["status"] = status
    attempt["failureReason"] = reason
    attempt["stateSince"] = time.time()
    attempt["updatedAt"] = utc_now()
    # Every device transition passes here: saving here persists them all,
    # with the commandKey or TransferComplete set just before the call.
    db.save_device(campaign, attempt)

    log_device(
        logging.INFO,
        attempt["deviceId"],
        "%s -> %s%s%s",
        previous,
        status,
        f" reason={reason}" if reason else "",
        f" {note}" if note else "",
        campaign=campaign,
    )


def retry_or_give_up(campaign, attempt):
    """A Connection Request got no callback or could not be sent: send it
    again, or give up.

    Resending it is safe: the device has received no command yet. Between
    tries the device waits as PENDING, so it does not hold a maxConcurrent
    slot; the scheduler skips it until retryAt.
    """
    # Not saved yet: a device restored from the database starts at 0.
    retries = attempt.get("retries", 0)
    if retries >= MAX_RETRIES:
        set_device_status(campaign, attempt, "TIMED_OUT", "NO_CALLBACK")
        return

    attempt["retries"] = retries + 1
    delay = RETRY_BACKOFF_SEC * 2 ** retries
    attempt["retryAt"] = time.time() + delay
    set_device_status(
        campaign,
        attempt,
        "PENDING",
        "NO_CALLBACK",
        note=f"retry={retries + 1}/{MAX_RETRIES} in={delay}s",
    )
    metrics.count_event("connectionRequestRetries")


def check_timeouts():
    now = time.time()
    for campaign in campaigns:
        for attempt in list(campaign["devices"].values()):
            timeout = STATE_TIMEOUTS.get(attempt["status"])
            if timeout is None:
                continue
            seconds, reason = timeout
            if now - attempt["stateSince"] <= seconds:
                continue
            if attempt["status"] == "NOTIFIED":
                retry_or_give_up(campaign, attempt)
            else:
                set_device_status(campaign, attempt, "TIMED_OUT", reason)


# ---------- Informs ----------

def log_inform(device_id, request):
    """One line per Inform; called before taking the lock."""
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


def update_inventory(device_id, request):
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


def handle_transfer_complete(device_id, tc):
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
        metrics.count_event("unknownCommandKeys")
    elif attempt["status"] in FINAL_STATES:
        log_device(
            logging.INFO,
            device_id,
            "duplicate/late TransferComplete ignored, already %s",
            attempt["status"],
            campaign=matching_campaign,
        )
        metrics.count_event("duplicateTransferCompletes")
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


def handle_boot(device_id, firmware_version):
    # A BOOT without TransferComplete after a Download: the reported
    # firmware version tells whether the upgrade was applied.
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
            metrics.count_event("staleBootsIgnored")
            continue

        if firmware_version == attempt["targetVersion"]:
            set_device_status(campaign, attempt, "SUCCEEDED")
        elif firmware_version == attempt["currentVersion"]:
            set_device_status(campaign, attempt, "FAILED", "REVERTED")
        else:
            set_device_status(
                campaign,
                attempt,
                "FAILED",
                f"UNEXPECTED_VERSION:{firmware_version}",
            )


# ---------- Command responses ----------

def log_command_response(device_id, request):
    """One line per command-response; called before taking the lock."""
    log_device(
        logging.INFO,
        device_id,
        "command-response status=%s commandKey=%s%s",
        request.status,
        request.commandKey,
        f" reason={request.reason}" if request.reason else "",
    )


def handle_command_response(device_id, request):
    """Record the device's answer to a Download; return the API status."""
    for campaign in campaigns:
        attempt = campaign["devices"].get(device_id)

        if attempt and attempt.get("commandKey") == request.commandKey:
            # Only a first answer to the Download counts; duplicates and
            # answers after the outcome is known are ignored.
            if attempt["status"] not in ("DOWNLOAD_SENT", "TIMED_OUT"):
                log_device(
                    logging.INFO,
                    device_id,
                    "duplicate/late command-response ignored, already %s",
                    attempt["status"],
                    campaign=campaign,
                )
                metrics.count_event("duplicateCommandResponses")
                return "recorded"

            if request.status == "ACCEPTED":
                set_device_status(campaign, attempt, "ACCEPTED")
            else:
                set_device_status(
                    campaign,
                    attempt,
                    "FAILED",
                    f"REJECTED:{request.reason or 'UNKNOWN'}",
                )
            return "recorded"

    log_device(
        logging.WARNING,
        device_id,
        "command-response with unknown commandKey=%s ignored",
        request.commandKey,
    )
    metrics.count_event("unknownCommandKeys")
    return "unknown_command"
