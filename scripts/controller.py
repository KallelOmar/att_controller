"""Device Management Controller: the HTTP APIs.

Run from att_controller/:  uvicorn controller:app --app-dir scripts
The endpoints take state_lock, then call the logic in campaigns.py and
devices.py; scheduler.py runs in the background.
"""

import asyncio
from contextlib import asynccontextmanager

from fastapi import BackgroundTasks, FastAPI, HTTPException

import campaigns
import devices
from log import logger
from models import (
    CommandResponseRequest,
    CreateCampaignRequest,
    InformRequest,
)
from scheduler import scheduler_loop
from state import state_lock


@asynccontextmanager
async def lifespan(_app):
    task = asyncio.create_task(scheduler_loop())
    yield
    task.cancel()


app = FastAPI(title="Device Management Controller", lifespan=lifespan)


# ---------- Campaign API ----------

def find_campaign(campaign_id, action):
    """Log the API call, then return the campaign or raise 404."""
    # The ID comes from the URL: pass it as an argument, never in the format.
    logger.info("[campaign %s] %s", campaign_id, action)
    campaign = campaigns.find(campaign_id)
    if campaign is None:
        logger.info("[campaign %s] not found", campaign_id)
        raise HTTPException(status_code=404, detail="Campaign not found")
    return campaign


def campaign_reply(campaign, message):
    return {
        "id": campaign["id"],
        "status": campaign["status"],
        "message": message,
    }


@app.post("/campaigns", status_code=201)
def create_campaign(
    request: CreateCampaignRequest,
    background_tasks: BackgroundTasks,
):
    with state_lock:
        campaign, created = campaigns.create(request)
        reply = campaign_reply(
            campaign,
            "Campaign created successfully"
            if created
            else "Matching active campaign already exists",
        )

    if created:
        # Runs after the response is sent, so the API call returns
        # immediately.
        background_tasks.add_task(campaigns.load_targets, campaign)
    return reply


@app.get("/campaigns/{campaign_id}")
def get_campaign(campaign_id: str):
    with state_lock:
        campaign = find_campaign(campaign_id, "get campaign details")
        # One snapshot under the lock, so status and counts agree.
        return campaigns.details(campaign)


@app.post("/campaigns/{campaign_id}/pause")
def pause_campaign(campaign_id: str):
    with state_lock:
        campaign = find_campaign(campaign_id, "pause campaign")
        try:
            message = campaigns.pause(campaign)
        except campaigns.CampaignStateError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return campaign_reply(campaign, message)


@app.post("/campaigns/{campaign_id}/resume")
def resume_campaign(campaign_id: str):
    with state_lock:
        campaign = find_campaign(campaign_id, "resume campaign")
        try:
            message = campaigns.resume(campaign)
        except campaigns.CampaignStateError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return campaign_reply(campaign, message)


# ---------- Device API ----------

@app.post("/devices/{device_id}/inform")
def receive_inform(device_id: str, request: InformRequest):
    devices.log_inform(device_id, request)

    with state_lock:
        devices.update_inventory(device_id, request)

        # Process TransferComplete if the event contains it.
        if request.transferComplete is not None:
            devices.handle_transfer_complete(
                device_id, request.transferComplete
            )

        if request.event == "BOOT":
            devices.handle_boot(device_id, request.firmwareVersion)

        # A connection-request Inform is the opportunity to return a command.
        commands = []
        if request.event == "CONNECTION_REQUEST":
            commands = campaigns.issue_download(
                device_id, request.firmwareVersion
            )

    return {"commands": commands}


@app.post("/devices/{device_id}/command-response")
def receive_command_response(
    device_id: str,
    request: CommandResponseRequest,
):
    devices.log_command_response(device_id, request)
    with state_lock:
        status = devices.handle_command_response(device_id, request)
    return {"status": status}


@app.get("/health")
def health():
    return {"status": "ok"}
