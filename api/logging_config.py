"""
Audit-style JSON logging for the FastAPI service.

CLAUDE.md rule 5: every /query call logs timestamp, query hash, k,
latency, model — never the raw query text or chunk text. The query text
may contain sensitive context (a clinician's free-text question about a
patient), so we hash it with SHA-256 and log a 16-char prefix as the
correlation handle.

The formatter emits one JSON object per log line so the output is easy
to ship to a log aggregator (Datadog, Loki, CloudWatch). The two
canonical events are `query_received` (start of request) and
`query_completed` (after generation, with metrics).
"""

from __future__ import annotations

import hashlib
import json
import logging
import sys
import time
from typing import Any


class JsonAuditFormatter(logging.Formatter):
    """Render every log record as a single line of JSON."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        extras = getattr(record, "audit", None)
        if isinstance(extras, dict):
            payload.update(extras)
        return json.dumps(payload, separators=(",", ":"))


def configure_logging(level: str = "INFO") -> logging.Logger:
    """Install the JSON formatter on the root logger; return the audit logger.

    Third-party libraries (httpx, urllib3, huggingface_hub) log every HTTP
    request at INFO. These don't leak our user data, but they bury our own
    audit lines under noise. Cap them at WARNING.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonAuditFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
    for noisy in ("httpx", "urllib3", "huggingface_hub", "filelock"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    return logging.getLogger("rag.audit")


def hash_query(query: str) -> str:
    """16-char SHA-256 prefix — enough entropy for log correlation, not reversible."""
    return hashlib.sha256(query.encode("utf-8")).hexdigest()[:16]
