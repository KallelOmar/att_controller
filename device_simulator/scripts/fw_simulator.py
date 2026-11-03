import asyncio
import os
import random
import time
import uuid
from collections import Counter
from contextlib import asynccontextmanager
from typing import Any, Coroutine

import httpx
from fastapi import FastAPI, HTTPException

JsonObject = dict[str, Any]
Device = dict[str, Any]

CONTROLLER_URL = os.getenv("CONTROLLER_URL", "http://localhost:8080").rstrip("/")
DEVICE_COUNT = int(os.getenv("DEVICE_COUNT", "1000"))
SEED = int(os.getenv("SEED", "42"))
OLD_VERSION = os.getenv("OLD_VERSION", "1.0.0")
PERIODIC_INFORM_SEC = int(os.getenv("PERIODIC_INFORM_SEC", "300"))
BOOT_SPREAD_SEC = int(os.getenv("BOOT_SPREAD_SEC", "10"))
DOWNLOAD_MIN_SEC = int(os.getenv("DOWNLOAD_MIN_SEC", "2"))
DOWNLOAD_MAX_SEC = int(os.getenv("DOWNLOAD_MAX_SEC", "20"))
REBOOT_MIN_SEC = int(os.getenv("REBOOT_MIN_SEC", "5"))
REBOOT_MAX_SEC = int(os.getenv("REBOOT_MAX_SEC", "240"))
P_DUPLICATE = float(os.getenv("P_DUPLICATE", "0.05"))
P_LATE = float(os.getenv("P_LATE", "0.05"))
LATE_EXTRA_SEC = int(os.getenv("LATE_EXTRA_SEC", "600"))

PROFILES = {
    "offline": float(os.getenv("P_OFFLINE", "0.05")),
    "reject": float(os.getenv("P_REJECT", "0.03")),
    "fault": float(os.getenv("P_FAULT", "0.04")),
    "no_tc": float(os.getenv("P_NO_TC", "0.03")),
    "revert": float(os.getenv("P_REVERT", "0.02")),
}


def validate_settings() -> None:
    probabilities = {"P_DUPLICATE": P_DUPLICATE, "P_LATE": P_LATE, **PROFILES}
    invalid = [name for name, value in probabilities.items() if not 0 <= value <= 1]
    if invalid:
        raise ValueError(f"Probabilities must be between 0 and 1: {', '.join(invalid)}")
    if sum(PROFILES.values()) > 1:
        raise ValueError("The sum of profile probabilities must not exceed 1")
    if DEVICE_COUNT < 1:
        raise ValueError("DEVICE_COUNT must be greater than zero")
    if PERIODIC_INFORM_SEC < 1:
        raise ValueError("PERIODIC_INFORM_SEC must be greater than zero")
    if BOOT_SPREAD_SEC < 0:
        raise ValueError("BOOT_SPREAD_SEC must be non-negative")
    if DOWNLOAD_MIN_SEC < 0 or DOWNLOAD_MAX_SEC < DOWNLOAD_MIN_SEC:
        raise ValueError("Download delay bounds must be non-negative and ordered")
    if REBOOT_MIN_SEC < 0 or REBOOT_MAX_SEC < REBOOT_MIN_SEC:
        raise ValueError("Reboot delay bounds must be non-negative and ordered")
    if LATE_EXTRA_SEC < 60:
        raise ValueError("LATE_EXTRA_SEC must be at least 60")


validate_settings()

fleet_rng = random.Random(SEED)
rng = random.Random(SEED + 1)
devices: dict[str, Device] = {}
client: httpx.AsyncClient | None = None
background_tasks: set[asyncio.Task[None]] = set()


def _task_finished(task: asyncio.Task[None]) -> None:
    background_tasks.discard(task)
    if task.cancelled():
        return
    exception = task.exception()
    if exception is not None:
        print(f"[sim] Background task failed: {exception!r}")


def spawn(coroutine: Coroutine[Any, Any, None]) -> None:
    """Keep a reference to a fire-and-forget task and report its failures."""
    task = asyncio.create_task(coroutine)
    background_tasks.add(task)
    task.add_done_callback(_task_finished)


def pick_profile() -> str:
    threshold = fleet_rng.random()
    cumulative_share = 0.0
    for name, share in PROFILES.items():
        cumulative_share += share
        if threshold < cumulative_share:
            return name
    return "healthy"


async def post(path: str, body: JsonObject) -> JsonObject | None:
    """Send a device callback to the controller and sometimes duplicate it."""
    if client is None:
        raise RuntimeError("HTTP client is not initialized")

    try:
        response = await client.post(f"{CONTROLLER_URL}{path}", json=body)
        response.raise_for_status()
        if rng.random() < P_DUPLICATE:
            spawn(duplicate_post(path, body))
        payload = response.json() if response.content else {}
        if not isinstance(payload, dict):
            raise ValueError("Controller response must be a JSON object")
        return payload
    except (httpx.HTTPError, ValueError) as exc:
        print(f"[sim] POST {path} failed: {exc!r}")
        return None


async def duplicate_post(path: str, body: JsonObject) -> None:
    """Send a duplicate callback after a short, randomized delay."""
    await asyncio.sleep(rng.uniform(0.1, 5))
    if client is None:
        return
    try:
        response = await client.post(f"{CONTROLLER_URL}{path}", json=body)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        print(f"[sim] Duplicate POST {path} failed: {exc!r}")


async def inform(
    device: Device,
    event: str,
    transfer_complete: JsonObject | None = None,
) -> None:
    body: JsonObject = {
        "deviceId": device["id"],
        "firmwareVersion": device["version"],
        "event": event,
    }
    if transfer_complete is not None:
        body["transferComplete"] = transfer_complete

    response = await post(f"/devices/{device['id']}/inform", body)
    for command in (response or {}).get("commands", []):
        if command.get("type") == "DOWNLOAD":
            spawn(handle_download(device, command))


async def respond(
    device: Device,
    command_key: str,
    status: str,
    reason: str | None = None,
) -> None:
    body: JsonObject = {"commandKey": command_key, "status": status}
    if reason is not None:
        body["reason"] = reason
    await post(f"/devices/{device['id']}/command-response", body)


async def handle_download(device: Device, command: JsonObject) -> None:
    command_key = command.get("commandKey")
    target_version = command.get("targetVersion")
    if not command_key or not target_version:
        await respond(device, command_key or "", "REJECTED", "MALFORMED_COMMAND")
        return
    if device["busy"]:
        await respond(device, command_key, "REJECTED", "BUSY")
        return
    if device["profile"] == "reject":
        await respond(device, command_key, "REJECTED", "UNSUPPORTED_IMAGE")
        return

    device["busy"] = True
    try:
        await respond(device, command_key, "ACCEPTED")
        start_time = time.time()
        await asyncio.sleep(rng.uniform(DOWNLOAD_MIN_SEC, DOWNLOAD_MAX_SEC))
        await asyncio.sleep(rng.uniform(REBOOT_MIN_SEC, REBOOT_MAX_SEC))
        complete_time = time.time()

        profile = device["profile"]
        if profile in ("healthy", "no_tc"):
            device["version"] = target_version

        if profile in ("no_tc", "revert"):
            await inform(device, "BOOT")
            return

        transfer_complete = {
            "commandKey": command_key,
            "faultCode": 9010 if profile == "fault" else 0,
            "startTime": start_time,
            "completeTime": complete_time,
        }
        if rng.random() < P_LATE:
            await inform(device, "BOOT")
            await asyncio.sleep(rng.uniform(60, LATE_EXTRA_SEC))
        await inform(device, "TRANSFER_COMPLETE", transfer_complete)
    finally:
        device["busy"] = False


async def periodic_loop(device: Device) -> None:
    # Power-on: spread BOOT informs so the fleet does not hit the controller at once.
    await asyncio.sleep(rng.uniform(0, BOOT_SPREAD_SEC))
    if device["profile"] != "offline":
        await inform(device, "BOOT")

    await asyncio.sleep(rng.uniform(0, PERIODIC_INFORM_SEC))
    while True:
        if device["profile"] != "offline" and not device["busy"]:
            await inform(device, "PERIODIC")
        await asyncio.sleep(PERIODIC_INFORM_SEC)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global client

    client = httpx.AsyncClient(
        limits=httpx.Limits(max_connections=500),
        timeout=10,
    )
    devices.clear()
    for _ in range(DEVICE_COUNT):
        device_id = f"GW-{uuid.UUID(int=fleet_rng.getrandbits(128)).hex[:12].upper()}"
        devices[device_id] = {
            "id": device_id,
            "version": OLD_VERSION,
            "profile": pick_profile(),
            "busy": False,
        }

    periodic_tasks = [
        asyncio.create_task(periodic_loop(device)) for device in devices.values()
    ]
    profile_counts = dict(Counter(device["profile"] for device in devices.values()))
    print(f"[sim] {DEVICE_COUNT} devices -> {CONTROLLER_URL}: {profile_counts}")
    try:
        yield
    finally:
        for task in periodic_tasks + list(background_tasks):
            task.cancel()
        await asyncio.gather(
            *periodic_tasks,
            *background_tasks,
            return_exceptions=True,
        )
        background_tasks.clear()
        await client.aclose()
        client = None


app = FastAPI(title="Firmware Upgrade Device Simulator", lifespan=lifespan)


@app.post("/devices/{device_id}/connection-request", status_code=202)
async def connection_request(device_id: str) -> JsonObject:
    device = devices.get(device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Unknown device")

    if device["profile"] != "offline" and not device["busy"]:

        async def wake_device() -> None:
            await asyncio.sleep(rng.uniform(0.2, 3))
            await inform(device, "CONNECTION_REQUEST")

        spawn(wake_device())
    return {"delivered": True}


@app.get("/devices")
async def list_devices(version: str | None = None) -> list[JsonObject]:
    """List devices without revealing their behavior profiles."""
    return [
        {"deviceId": device["id"], "firmwareVersion": device["version"]}
        for device in devices.values()
        if version is None or device["version"] == version
    ]


@app.get("/_admin/summary")
async def admin_summary() -> JsonObject:
    """Return interviewer-only ground truth for grading the run report."""
    return {
        "profiles": dict(Counter(device["profile"] for device in devices.values())),
        "versions": dict(Counter(device["version"] for device in devices.values())),
        "busy": sum(device["busy"] for device in devices.values()),
    }
