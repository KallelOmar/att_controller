"""Mini scheduler: times out stuck devices, halts or completes campaigns,
and nudges PENDING devices while under maxConcurrent."""

import asyncio
import logging
import time
import urllib.request

import metrics
from campaigns import count_in_flight, update_campaign_status
from config import SCHEDULER_TICK_SEC, SIMULATOR_URL
from devices import check_timeouts, retry_or_give_up, set_device_status
from log import log_device, logger
from state import campaigns, state_lock


def select_devices_to_notify(campaign):
    """Move PENDING devices to NOTIFIED while under maxConcurrent."""
    free = campaign["maxConcurrent"] - count_in_flight(campaign)
    if free <= 0:
        return []

    # A device waiting to retry is skipped until its retryAt.
    # TODO: at scale, keep a queue of PENDING devices instead of looping over all devices on every tick.
    now = time.time()
    selected = [
        attempt
        for attempt in campaign["devices"].values()
        if attempt["status"] == "PENDING"
        and (attempt.get("retryAt") or 0) <= now
    ][:free]

    # Set before sending: the device may call back before urlopen returns.
    for attempt in selected:
        attempt["retryAt"] = None
        set_device_status(campaign, attempt, "NOTIFIED")
    return selected


def post_connection_request(campaign, device_id):
    """Ask the device to call back; return the error, or None if sent."""
    url = f"{SIMULATOR_URL}/devices/{device_id}/connection-request"
    endpoint = "/devices/{deviceId}/connection-request"
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, method="POST"), timeout=5
        ) as response:
            metrics.count_outgoing("POST", endpoint, response.status)
        return None
    except OSError as exc:
        metrics.count_outgoing("POST", endpoint, metrics.error_result(exc))
        log_device(
            logging.WARNING,
            device_id,
            "connection request failed: %s",
            exc,
            campaign=campaign,
        )
        return exc


def send_connection_request(campaign, attempt):
    if post_connection_request(campaign, attempt["deviceId"]) is None:
        return
    # Not sent: counts as a try, like a Connection Request with no callback.
    with state_lock:
        if attempt["status"] == "NOTIFIED":
            retry_or_give_up(campaign, attempt)


def send_probe(campaign, attempt):
    """One Connection Request after NO_ACK or NO_COMPLETION.

    Safe: a TIMED_OUT device is never given a Download. The Inform it
    sends back reports its firmware version, which can prove the upgrade.
    """
    log_device(
        logging.INFO,
        attempt["deviceId"],
        "probe after %s, connection request sent",
        attempt["failureReason"],
        campaign=campaign,
    )
    metrics.count_event("probesSent")
    post_connection_request(campaign, attempt["deviceId"])


def scheduler_tick():
    with state_lock:
        # Timeouts first, so the slots they free are refilled this tick.
        to_probe = check_timeouts()
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
    for campaign, attempt in to_probe:
        send_probe(campaign, attempt)


async def scheduler_loop():
    while True:
        await asyncio.sleep(SCHEDULER_TICK_SEC)
        try:
            # urlopen blocks: run in a thread so the API keeps serving.
            await asyncio.to_thread(scheduler_tick)
        except Exception:
            logger.exception("[controller] scheduler tick failed")
