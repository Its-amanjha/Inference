"""Errors are grouped by what failed, not by run.

The same instance, workflow, step, error type and message are one error with several occurrences. Values
that differ from run to run inside a message (IDs, times, long numbers, e-mail addresses) are masked before
comparing, so "order 1042 not found" and "order 1043 not found" are one problem; three-digit codes such as
HTTP 404 and 500 stay, so different statuses stay different problems.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

_VOLATILE = re.compile(
    r'\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b'  # UUIDs
    r'|\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?'  # times
    r'|\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{12,}\b'  # hashes and request IDs
    r'|[\w.+-]+@[\w-]+(?:\.[\w-]+)+'  # e-mail addresses
    r'|\d{4,}',  # IDs and other long numbers
    re.IGNORECASE)


def normalize(message: Any) -> str:
    """A message as compared for grouping: one line, run-specific values masked, lower case."""
    text = ' '.join(str(message or '').split())[:500]
    return _VOLATILE.sub('#', text).lower()


def group_key(instance_id: Any, workflow_id: Any, node_name: Any, error_type: Any, message: Any) -> str:
    parts = [str(instance_id or ''), str(workflow_id or ''), str(node_name or ''), str(error_type or ''), normalize(message)]
    return hashlib.sha256(json.dumps(parts).encode()).hexdigest()


def run_error_key(instance_id: str, execution_id: str) -> str:
    """The occurrence key of a failure read from n8n's own execution data; one per run."""
    return hashlib.sha256(f'{instance_id}:{execution_id}:execution'.encode()).hexdigest()
