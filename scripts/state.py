"""Shared controller state. db.py will persist it."""

import threading
from datetime import datetime, timezone


# Temporary in-memory storage
campaigns = []
devices = {}

# Handlers run in a thread pool next to the scheduler thread: hold this
# lock for every check-then-update of the state above.
state_lock = threading.Lock()


def utc_now():
    return datetime.now(timezone.utc).isoformat()
