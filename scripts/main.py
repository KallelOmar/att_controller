
import logging
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, HttpUrl


# Configure application logs
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s - %(message)s",
)
logger = logging.getLogger("controller")

app = FastAPI(title="Device Management Controller")

# Temporary in-memory storage; SQLite will come later.
campaigns = []


class DeviceFilter(BaseModel):
    currentVersion: str = Field(min_length=1)


class CreateCampaignRequest(BaseModel):
    targetVersion: str = Field(min_length=1)
    imageUrl: HttpUrl
    deviceFilter: DeviceFilter
    maxConcurrent: int = Field(gt=0)
    failureThresholdPercent: float = Field(ge=0, le=100)


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
                "Matching active campaign already exists: id=%s "
                "currentVersion=%s targetVersion=%s",
                existing["id"],
                current_version,
                request.targetVersion,
            )

            return {
                "id": existing["id"],
                "status": existing["status"],
                "status_code": 200,
                "message": "Matching active campaign already exists",
            }
    campaign_id = str(uuid4())

    campaign = {
        "id": campaign_id,
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
        "Campaign created: id=%s currentVersion=%s targetVersion=%s "
        "maxConcurrent=%s",
        campaign_id,
        request.deviceFilter.currentVersion,
        request.targetVersion,
        request.maxConcurrent,
    )

    return {
        "id": campaign_id,
        "status": campaign["status"],
        "status_code": 201,
        "message": "Campaign created successfully",
    }


@app.get("/health")
def health():
    return {"status": "ok"}

