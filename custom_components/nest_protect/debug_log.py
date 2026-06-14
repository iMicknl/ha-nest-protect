"""Temporary debug instrumentation for auth-expiration investigation."""

from __future__ import annotations

import json
import time
from pathlib import Path

SESSION_ID = "ad7286"
LOG_PATHS = (
    Path("/config/debug-ad7286.log"),
    Path("debug-ad7286.log"),
)


def agent_debug_log(
    location: str,
    message: str,
    data: dict,
    hypothesis_id: str,
    run_id: str = "pre-fix",
) -> None:
    """Append one NDJSON debug line for hypothesis testing."""
    payload = {
        "sessionId": SESSION_ID,
        "timestamp": int(time.time() * 1000),
        "location": location,
        "message": message,
        "data": data,
        "hypothesisId": hypothesis_id,
        "runId": run_id,
    }
    line = json.dumps(payload, default=str) + "\n"
    for path in LOG_PATHS:
        try:
            with path.open("a", encoding="utf-8") as log_file:
                log_file.write(line)
            return
        except OSError:
            continue
