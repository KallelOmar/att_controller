"""Shared controller state. db.py persists the campaigns."""

import threading
from datetime import datetime, timezone


# Working copy of the state; campaigns are written through to db.py.
campaigns = []
devices = {}

# The endpoints and the scheduler run in parallel threads: take this lock  before reading and then changing the state
state_lock = threading.Lock()


def utc_now():
    return datetime.now(timezone.utc).isoformat()
