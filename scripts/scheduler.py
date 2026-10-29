"""Mini scheduler: times out stuck devices, halts or completes campaigns,
and nudges PENDING devices while under maxConcurrent."""

import asyncio
import logging
import urllib.request

import metrics
from campaigns import count_in_flight, update_campaign_status
from config import SCHEDULER_TICK_SEC, SIMULATOR_URL
from devices import check_timeouts, set_device_status
from log import log_device, logger
from state import campaigns, state_lock


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
    endpoint = "/devices/{deviceId}/connection-request"
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, method="POST"), timeout=5
        ) as response:
            metrics.count_outgoing("POST", endpoint, response.status)
    except OSError as exc:
        metrics.count_outgoing("POST", endpoint, metrics.error_result(exc))
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
