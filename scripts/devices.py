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


# Device states. SUCCEEDED and FAILED are final
# A late TransferComplete or BOOT for the same commandKey can correct TIMED_OUT
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

# Devices in these states are being upgraded
IN_FLIGHT_STATES = ("NOTIFIED", "DOWNLOAD_SENT", "ACCEPTED")

# Waiting state -> (timeout in seconds, failureReason when it expires)
# ACCEPTED covers download + reboot, which can take several minutes
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
        # TODO: save both in db.py. Not saved, a restart resets them
        # At worst 2 extra Connection Requests, which are safe to repeat
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
    # with the commandKey or TransferComplete set just before the call
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
    # Not saved yet: a device restored from the database starts at 0
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
    """Time out stuck devices; return those to probe.

    A Download is never resent: after NO_ACK or NO_COMPLETION the device
    may be installing it. It only gets one more Connection Request, the
    probe, whose Inform reports its firmware version.
    """
    now = time.time()
    to_probe = []
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
                to_probe.append((campaign, attempt))
    return to_probe


# ---------- Informs ----------

def log_inform(device_id, request):
    """One line per Inform; called before taking the lock."""
    # PERIODIC Informs are routine and frequent: log them only at DEBUG
    event_log_level = (
        logging.DEBUG if request.event == "PERIODIC" else logging.INFO
    )

    # One line per Inform, with the TransferComplete result when present
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
    # Create or update the device inventory
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
         #Correlate the TransferComplete with the correponding Download using commandKey
        if candidate and candidate.get("commandKey") == tc.commandKey:
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
        # TODO: if the device is FAILED with REVERTED, use the fault code as the reason.
        # REVERTED: the device rebooted but came back on its old firmware: the upgrade didn't take effect.
        # The BOOT on the old version can arrive before the TransferComplete fault. Still FAILED, only the reason changes.
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


def handle_firmware_version(device_id, event, firmware_version):
    # After a Download with no TransferComplete yet, the reported firmware version tells whether the upgrade was applied.
    for campaign in campaigns:
        attempt = campaign["devices"].get(device_id)
        if (
            attempt is None
            or attempt["commandKey"] is None
            or attempt["status"] not in ("ACCEPTED", "TIMED_OUT")
        ):
            continue

        # In a PERIODIC or CONNECTION_REQUEST Inform, only the target version counts (SUCCEEDED). 
        # The old version proves nothing: the device may still be downloading and has not rebooted yet.
        if event != "BOOT":
            if firmware_version == attempt["targetVersion"]:
                set_device_status(
                    campaign, attempt, "SUCCEEDED", note=f"evidence={event}"
                )
                metrics.count_event("evidenceFromInform")
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
            # TODO: after a timeout, accept it only for NO_ACK. After
            # NO_COMPLETION the device already answered ACCEPTED.
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
