"""Campaign lifecycle: create, load targets, progress, pause and resume.

Callers hold state_lock, except load_targets, a background task that
takes it itself.
"""

import json
import logging
import urllib.parse
import urllib.request
from uuid import uuid4

from pydantic import BaseModel, Field, HttpUrl

import db
import metrics
from config import MIN_SAMPLE_SIZE, SIMULATOR_URL
from devices import (
    DEVICE_STATES,
    IN_FLIGHT_STATES,
    new_attempt,
    set_device_status,
)
from log import log_campaign, log_device, logger
from state import campaigns, state_lock, utc_now


# Campaign states that still own their devices: a second campaign for the
# same upgrade is refused while one of these exists. HALTED and COMPLETED are final.
ACTIVE_CAMPAIGN_STATES = ("PENDING", "IN_PROGRESS", "PAUSED")


class CampaignStateError(Exception):
    """The campaign's status does not allow the requested action."""


# ---------- Campaign API models ----------

class DeviceFilter(BaseModel):
    currentVersion: str = Field(min_length=1)


class CreateCampaignRequest(BaseModel):
    targetVersion: str = Field(min_length=1)
    imageUrl: HttpUrl
    deviceFilter: DeviceFilter
    maxConcurrent: int = Field(gt=0)
    failureThresholdPercent: float = Field(ge=0, le=100)


# ---------- Progress ----------

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

    # TODO: maybe don't count NO_CALLBACK as a failure: an offline device never got the firmware 
    # To discuss (README "Questions to discuss")
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
    db.save_campaign(campaign)
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


# ---------- Creation ----------

def fetch_target_devices(current_version: str):
    # TODO: add pagination. This call returns every device at once
    # fine for 1,000, not for millions.
    query = urllib.parse.urlencode({"version": current_version})
    try:
        with urllib.request.urlopen(
            f"{SIMULATOR_URL}/devices?{query}", timeout=10
        ) as response:
            metrics.count_outgoing("GET", "/devices", response.status)
            return [device["deviceId"] for device in json.load(response)]
    except OSError as exc:
        metrics.count_outgoing("GET", "/devices", metrics.error_result(exc))
        raise


def load_targets(campaign):
    """Track every target device as PENDING; the scheduler nudges them."""
    current_version = campaign["deviceFilter"]["currentVersion"]
    # TODO: retry with backoff. Safe, since listing only reads; today a
    # failure leaves the campaign PENDING until the next controller start.
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
        db.save_campaign_and_devices(campaign)

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


def create(request):
    """Return (campaign, created): an active duplicate is returned as is."""
    current_version = request.deviceFilter.currentVersion

    for existing in campaigns:
        if (
            existing["deviceFilter"]["currentVersion"] == current_version
            and existing["targetVersion"] == request.targetVersion
            and existing["status"] in ACTIVE_CAMPAIGN_STATES
        ):
            log_campaign(
                logging.INFO,
                existing,
                "creation request matches this active campaign, none created",
            )
            return existing, False

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
    db.save_campaign(campaign)

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
    return campaign, True


# ---------- Queries ----------

def find(campaign_id):
    return next((c for c in campaigns if c["id"] == campaign_id), None)


def details(campaign):
    """Input parameters, status and progress, as GET /campaigns/{id}."""
    progress = campaign_counts(campaign)
    progress["failureRatePercent"] = round(progress["failureRatePercent"], 1)
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


# ---------- Pause / resume ----------

def pause(campaign):
    """Pause the campaign; return the API message."""
    status = campaign["status"]

    if status == "PAUSED":
        log_campaign(logging.INFO, campaign, "pause ignored, already PAUSED")
        return "Campaign is already paused"

    if status not in ("PENDING", "IN_PROGRESS"):
        log_campaign(
            logging.INFO, campaign, "pause refused, campaign is %s", status
        )
        raise CampaignStateError(
            f"Campaign is {status}: only a PENDING or IN_PROGRESS "
            "campaign can be paused"
        )

    # NOTIFIED devices go back to PENDING, so they don't time out during the pause
    #  Devices that already have a Download continue
    requeued = 0
    for attempt in campaign["devices"].values():
        if attempt["status"] == "NOTIFIED":
            set_device_status(campaign, attempt, "PENDING")
            requeued += 1

    campaign["statusBeforePause"] = status
    campaign["status"] = "PAUSED"
    campaign["pausedAt"] = utc_now()
    db.save_campaign(campaign)
    log_campaign(
        logging.INFO,
        campaign,
        "%s -> PAUSED requeued=%d inFlight=%d",
        status,
        requeued,
        count_in_flight(campaign),
    )
    return "Campaign paused"


def resume(campaign):
    """Resume a paused campaign; return the API message."""
    status = campaign["status"]

    if status != "PAUSED":
        log_campaign(
            logging.INFO, campaign, "resume refused, campaign is %s", status
        )
        raise CampaignStateError(
            f"Campaign is {status}: only a PAUSED campaign can be resumed"
        )

    # No cursor needed: the next tick nudges the devices still PENDING.
    campaign["status"] = campaign["statusBeforePause"]
    campaign["statusBeforePause"] = None
    campaign["pausedAt"] = None
    db.save_campaign(campaign)
    log_campaign(logging.INFO, campaign, "PAUSED -> %s", campaign["status"])
    return "Campaign resumed"


# ---------- Restart ----------

def restore():
    """Reload the saved campaigns at startup, before the scheduler runs.

    Returns the campaigns whose targets were never loaded: the controller
    stopped first, or the device list could not be fetched.
    """
    restored = db.load_campaigns()
    campaigns.extend(restored)

    requeued = 0
    for campaign in restored:
        if campaign["status"] not in ACTIVE_CAMPAIGN_STATES:
            continue
        for attempt in campaign["devices"].values():
            # Its callback may have been lost while the controller was down. It has no Download yet, so sending it a new Connection Request is safe; 
            # this is not counted as a retry.
            # DOWNLOAD_SENT and ACCEPTED devices just keep waiting.
            if attempt["status"] == "NOTIFIED":
                set_device_status(campaign, attempt, "PENDING")
                requeued += 1

    logger.info(
        "[controller] restored campaigns=%d devices=%d requeued=%d",
        len(restored),
        sum(len(campaign["devices"]) for campaign in restored),
        requeued,
    )
    return [
        campaign
        for campaign in restored
        if not campaign["devices"]
        and "PENDING" in (campaign["status"], campaign["statusBeforePause"])
    ]


# ---------- Download ----------

def issue_download(device_id, firmware_version):
    """Return the commands for a CONNECTION_REQUEST Inform."""
    # Default response: no commands to deliver.
    commands = []
    reason_logged = False

    for campaign in campaigns:
        if campaign["status"] not in ACTIVE_CAMPAIGN_STATES:
            continue

        expected_version = campaign["deviceFilter"]["currentVersion"]

        if firmware_version != expected_version:
            continue

        # Late callback to a Connection Request sent before the pause: the device was requeued and is nudged on resume.
        if campaign["status"] == "PAUSED":
            log_device(
                logging.INFO,
                device_id,
                "no DOWNLOAD, campaign is PAUSED",
                campaign=campaign,
            )
            metrics.count_event("callbacksWhilePaused")
            reason_logged = True
            continue

        attempt = campaign["devices"].get(device_id)

        # Only devices targeted by the campaign and not yet sent a Download get one: prevents duplicate Download commands.
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
        metrics.count_event("downloadsIssued")

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
            firmware_version,
        )
    return commands
