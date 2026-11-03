"""SQLite persistence, so a campaign survives a controller restart.
"""

import json
import sqlite3

from config import DB_PATH


SCHEMA = """
CREATE TABLE IF NOT EXISTS campaigns (
    id                        TEXT PRIMARY KEY,
    target_version            TEXT NOT NULL,
    image_url                 TEXT NOT NULL,
    current_version           TEXT NOT NULL,
    max_concurrent            INTEGER NOT NULL,
    failure_threshold_percent REAL NOT NULL,
    status                    TEXT NOT NULL,
    status_before_pause       TEXT,
    created_at                TEXT NOT NULL,
    paused_at                 TEXT,
    ended_at                  TEXT
);

-- One row per device attempt in a campaign.
CREATE TABLE IF NOT EXISTS campaign_devices (
    campaign_id       TEXT NOT NULL REFERENCES campaigns(id),
    device_id         TEXT NOT NULL,
    status            TEXT NOT NULL,
    command_key       TEXT,
    current_version   TEXT NOT NULL,
    target_version    TEXT NOT NULL,
    failure_reason    TEXT,
    state_since       REAL NOT NULL,  -- epoch seconds, for the timeouts
    updated_at        TEXT NOT NULL,
    transfer_complete TEXT,           -- JSON, once one is received
    PRIMARY KEY (campaign_id, device_id)
);
"""

SAVE_CAMPAIGN = """
INSERT INTO campaigns VALUES (
    :id, :target_version, :image_url, :current_version, :max_concurrent,
    :failure_threshold_percent, :status, :status_before_pause,
    :created_at, :paused_at, :ended_at
)
ON CONFLICT (id) DO UPDATE SET
    status = excluded.status,
    status_before_pause = excluded.status_before_pause,
    paused_at = excluded.paused_at,
    ended_at = excluded.ended_at
"""

SAVE_DEVICE = """
INSERT INTO campaign_devices VALUES (
    :campaign_id, :device_id, :status, :command_key, :current_version,
    :target_version, :failure_reason, :state_since, :updated_at,
    :transfer_complete
)
ON CONFLICT (campaign_id, device_id) DO UPDATE SET
    status = excluded.status,
    command_key = excluded.command_key,
    failure_reason = excluded.failure_reason,
    state_since = excluded.state_since,
    updated_at = excluded.updated_at,
    transfer_complete = excluded.transfer_complete
"""

# One connection, used only while state_lock is held.
connection = None


def connect():
    global connection
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    # WAL: a commit appends to a log instead of rewriting the database.
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(SCHEMA)


def close():
    connection.close()


# ---------- dict <-> row ----------

def campaign_row(campaign):
    return {
        "id": campaign["id"],
        "target_version": campaign["targetVersion"],
        "image_url": campaign["imageUrl"],
        "current_version": campaign["deviceFilter"]["currentVersion"],
        "max_concurrent": campaign["maxConcurrent"],
        "failure_threshold_percent": campaign["failureThresholdPercent"],
        "status": campaign["status"],
        "status_before_pause": campaign["statusBeforePause"],
        "created_at": campaign["createdAt"],
        "paused_at": campaign["pausedAt"],
        "ended_at": campaign["endedAt"],
    }


def campaign_from_row(row):
    return {
        "id": row["id"],
        "targetVersion": row["target_version"],
        "imageUrl": row["image_url"],
        "deviceFilter": {"currentVersion": row["current_version"]},
        "maxConcurrent": row["max_concurrent"],
        "failureThresholdPercent": row["failure_threshold_percent"],
        "status": row["status"],
        "statusBeforePause": row["status_before_pause"],
        "createdAt": row["created_at"],
        "pausedAt": row["paused_at"],
        "endedAt": row["ended_at"],
        "devices": {},
    }


def device_row(campaign, attempt):
    tc = attempt.get("transferComplete")
    return {
        "campaign_id": campaign["id"],
        "device_id": attempt["deviceId"],
        "status": attempt["status"],
        "command_key": attempt["commandKey"],
        "current_version": attempt["currentVersion"],
        "target_version": attempt["targetVersion"],
        "failure_reason": attempt["failureReason"],
        "state_since": attempt["stateSince"],
        "updated_at": attempt["updatedAt"],
        "transfer_complete": json.dumps(tc) if tc is not None else None,
    }


def device_from_row(row):
    attempt = {
        "deviceId": row["device_id"],
        "status": row["status"],
        "commandKey": row["command_key"],
        "currentVersion": row["current_version"],
        "targetVersion": row["target_version"],
        "failureReason": row["failure_reason"],
        "stateSince": row["state_since"],
        "updatedAt": row["updated_at"],
    }
    if row["transfer_complete"] is not None:
        attempt["transferComplete"] = json.loads(row["transfer_complete"])
    return attempt


# ---------- Writes: each call is one committed transaction ----------

def save_campaign(campaign):
    with connection:
        connection.execute(SAVE_CAMPAIGN, campaign_row(campaign))


def save_device(campaign, attempt):
    with connection:
        connection.execute(SAVE_DEVICE, device_row(campaign, attempt))


def save_campaign_and_devices(campaign):
    """The campaign and all its devices at once, when targets load."""
    with connection:
        connection.execute(SAVE_CAMPAIGN, campaign_row(campaign))
        connection.executemany(
            SAVE_DEVICE,
            [
                device_row(campaign, attempt)
                for attempt in campaign["devices"].values()
            ],
        )


# ---------- Read at startup ----------

def load_campaigns():
    """All saved campaigns, oldest first, each with its devices."""
    by_id = {}
    for row in connection.execute(
        "SELECT * FROM campaigns ORDER BY created_at"
    ):
        by_id[row["id"]] = campaign_from_row(row)

    # Insertion order, so the scheduler nudges devices in the same order.
    for row in connection.execute(
        "SELECT * FROM campaign_devices ORDER BY rowid"
    ):
        campaign = by_id[row["campaign_id"]]
        campaign["devices"][row["device_id"]] = device_from_row(row)
    return list(by_id.values())
