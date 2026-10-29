"""Request and event counters, served by GET /metrics.

Counters live in memory and restart from zero with the controller;
startedAt tells the window they cover. Campaign progress is not here:
GET /campaigns/{id} reports it.
"""

import threading
import urllib.error
from collections import Counter

from state import utc_now


started_at = utc_now()

# (method, route template, status code) of requests the controller serves.
incoming = Counter()
# (method, endpoint, result) of requests the controller sends. The result
# is the HTTP status code, or timeout / connection_error.
outgoing = Counter()
# Informs received, by event type.
informs = Counter()
# Protocol events worth watching: duplicates, unknown commandKeys, etc.
events = Counter()

# Counters are updated from the event loop, the thread pool and the scheduler thread, partly outside state_lock: they have their own lock.
metrics_lock = threading.Lock()


def count_incoming(method, route, status_code):
    with metrics_lock:
        incoming[(method, route, status_code)] += 1


def count_outgoing(method, endpoint, result):
    with metrics_lock:
        outgoing[(method, endpoint, str(result))] += 1


def count_inform(event):
    with metrics_lock:
        informs[event] += 1


def count_event(name):
    with metrics_lock:
        events[name] += 1


def error_result(exc):
    """The outgoing result to record for a failed urlopen."""
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code
    if isinstance(exc, TimeoutError) or isinstance(
        getattr(exc, "reason", None), TimeoutError
    ):
        return "timeout"
    return "connection_error"


def is_success(result):
    return str(result).startswith("2")


def by_endpoint(counter):
    """{"POST /path": {"total", "success", "errored", "byStatus"}}."""
    # TODO: split "errored" into 4xx, 5xx and network errors, so alerts fire on controller bugs (5xx), not on a normal 409.
    endpoints = {}
    for (method, path, result), count in sorted(
        counter.items(), key=lambda item: tuple(map(str, item[0]))
    ):
        entry = endpoints.setdefault(
            f"{method} {path}",
            {"total": 0, "success": 0, "errored": 0, "byStatus": {}},
        )
        entry["total"] += count
        entry["success" if is_success(result) else "errored"] += count
        entry["byStatus"][str(result)] = count
    return endpoints


def totals(endpoints):
    return {
        key: sum(entry[key] for entry in endpoints.values())
        for key in ("total", "success", "errored")
    }


def snapshot():
    with metrics_lock:
        served = by_endpoint(incoming)
        sent = by_endpoint(outgoing)
        return {
            "startedAt": started_at,
            "incoming": {**totals(served), "byEndpoint": served},
            "outgoing": {**totals(sent), "byEndpoint": sent},
            "informsByEvent": dict(sorted(informs.items())),
            "events": dict(sorted(events.items())),
        }
