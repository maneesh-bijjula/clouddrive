"""Bounded process-local API observations and credential-free structured logs."""

import json
import logging
import math
import re
import threading
from collections import OrderedDict, deque
from contextvars import ContextVar
from uuid import uuid4

logger = logging.getLogger("clouddrive.events")
logger.setLevel(logging.INFO)
if not logger.handlers:
    logger.addHandler(logging.StreamHandler())
logger.propagate = False
correlation = ContextVar("clouddrive_request_id", default=None)
_safe_id = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,79}\Z")
LATENCY_BOUNDS_MS = (1, 5, 10, 25, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 30000)
MAX_WORKSPACES = 128
MAX_LATENCY_SAMPLES = 512
_lock = threading.Lock()
_workspaces = OrderedDict()
_inflight = 0


def request_id(value=None):
    return value if isinstance(value, str) and _safe_id.fullmatch(value) else str(uuid4())


def event(name, **fields):
    # Call sites provide small, fixed field sets: never headers, exception text,
    # request bodies, filenames, raw URLs or query strings.
    logger.info(
        json.dumps(
            {"event": name, "request_id": correlation.get(), **fields}, separators=(",", ":")
        )
    )


def enter_request(limit):
    global _inflight
    with _lock:
        if _inflight >= limit:
            return False
        _inflight += 1
        return True


def leave_request():
    global _inflight
    with _lock:
        _inflight -= 1


def inflight():
    with _lock:
        return _inflight


def observe(workspace_id, status, elapsed_ms):
    if workspace_id is None:
        return
    elapsed_ms = max(0.0, float(elapsed_ms))
    with _lock:
        metric = _workspaces.get(workspace_id)
        if metric is None:
            metric = {
                "request_count": 0,
                "status_classes": {str(code): 0 for code in range(2, 6)},
                "latency_buckets": [0] * (len(LATENCY_BOUNDS_MS) + 1),
                "latency_sum_ms": 0.0,
                "samples": deque(maxlen=MAX_LATENCY_SAMPLES),
            }
            _workspaces[workspace_id] = metric
            if len(_workspaces) > MAX_WORKSPACES:
                _workspaces.popitem(last=False)
        _workspaces.move_to_end(workspace_id)
        metric["request_count"] += 1
        key = str(status // 100)
        if key in metric["status_classes"]:
            metric["status_classes"][key] += 1
        bucket = next(
            (i for i, bound in enumerate(LATENCY_BOUNDS_MS) if elapsed_ms <= bound),
            len(LATENCY_BOUNDS_MS),
        )
        metric["latency_buckets"][bucket] += 1
        metric["latency_sum_ms"] += elapsed_ms
        metric["samples"].append(elapsed_ms)


def snapshot(workspace_id):
    with _lock:
        metric = _workspaces.get(workspace_id)
        if metric is None:
            count, statuses, buckets, total, samples = (
                0,
                {},
                [0] * (len(LATENCY_BOUNDS_MS) + 1),
                0,
                [],
            )
        else:
            count = metric["request_count"]
            statuses = dict(metric["status_classes"])
            buckets = list(metric["latency_buckets"])
            total = metric["latency_sum_ms"]
            samples = sorted(metric["samples"])
    return {
        "scope": "Authenticated requests for this workspace in this API process; resets on restart or bounded LRU eviction",
        "request_count": count,
        "status_classes": statuses,
        "latency_histogram_ms": {
            **{str(bound): buckets[i] for i, bound in enumerate(LATENCY_BOUNDS_MS)},
            "+Inf": buckets[-1],
        },
        "histogram_semantics": "Non-cumulative bucket counts; API handler through response headers, excluding response-body transmission",
        "latency_sum_ms": round(total, 3),
        "recent_sample_count": len(samples),
        "recent_sample_limit": MAX_LATENCY_SAMPLES,
        "recent_p50_ms": round(samples[math.ceil(len(samples) * 0.50) - 1], 3) if samples else None,
        "recent_p95_ms": round(samples[math.ceil(len(samples) * 0.95) - 1], 3) if samples else None,
    }
