"""Shared controller state. db.py persists the campaigns."""

import threading
from datetime import datetime, timezone


# Working copy of the state; campaigns are written through to db.py.
campaigns = []
devices = {}

# Handlers run in a thread pool next to the scheduler thread: hold this
# lock for every check-then-update of the state above.
state_lock = threading.Lock()


def utc_now():
    return datetime.now(timezone.utc).isoformat()
