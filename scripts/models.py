"""Request bodies of the campaign and device APIs."""

from typing import Literal

from pydantic import BaseModel, Field, HttpUrl


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
