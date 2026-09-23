"""Bounded OTLP decoding; attribution, persistence and costing live outside this module."""
from __future__ import annotations

import base64
import json
import math
import re
import zlib
from typing import Any

from google.protobuf.json_format import MessageToDict
from google.protobuf.message import DecodeError
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

from backend.modules.assistant.repair_context import cap_text, mask_text

MAX_BODY = 2 * 1024 * 1024
MAX_DECOMPRESSED = 8 * 1024 * 1024
MAX_SPANS = 2000
MAX_ATTRIBUTES = 16 * 1024


class IngestError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _bad() -> IngestError:
    return IngestError(400, "invalid_otlp", "Malformed OTLP trace payload")


def _integer(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError
    if isinstance(value, str) and not re.fullmatch(r"[0-9]+", value):
        raise ValueError
    result = int(value)
    if not 0 <= result <= 2**63 - 1:
        raise ValueError
    return result


def _identifier(value: Any, length: int) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-fA-F0-9]{%d}" % length, value) or int(value, 16) == 0:
        raise ValueError
    return value.lower()


def _repair(value: str) -> str:
    # Require both a suspicious sequence and a reversible, valid UTF-8 decoding.
    if not any(marker in value for marker in ("Ã", "Â", "â€", "ðŸ")):
        return value
    for codec in ("latin1", "cp1252"):
        try:
            fixed = value.encode(codec).decode("utf-8")
            if fixed.encode("utf-8").decode(codec) == value:
                return fixed
        except (UnicodeError, LookupError):
            pass
    return value


def _text(value: Any, limit: int = 1024) -> str:
    if not isinstance(value, str):
        raise ValueError
    # Reject surrogate code points rather than allowing storage encoding failures.
    value.encode("utf-8")
    return _repair(value)[:limit]


def _value(value: Any, depth: int = 0) -> Any:
    if not isinstance(value, dict) or len(value) != 1 or depth > 12:
        raise ValueError
    key, item = next(iter(value.items()))
    if key == "stringValue":
        if not isinstance(item, str):
            raise ValueError
        item.encode("utf-8")
        return _repair(item)
    if key == "intValue":
        if isinstance(item, bool) or not isinstance(item, (str, int)) or not re.fullmatch(r"-?[0-9]+", str(item)):
            raise ValueError
        number = int(item)
        if not -(2**63) <= number < 2**63:
            raise ValueError
        return number
    if key == "boolValue" and isinstance(item, bool):
        return item
    if key == "doubleValue" and isinstance(item, (float, int)) and not isinstance(item, bool) and math.isfinite(item):
        return item
    if key == "arrayValue" and isinstance(item, dict) and isinstance(item.get("values", []), list):
        return [_value(v, depth + 1) for v in item.get("values", [])]
    if key == "kvlistValue" and isinstance(item, dict):
        return _attributes(item.get("values", []), depth + 1)
    # Binary attributes can carry arbitrary payloads, so omit them.
    raise ValueError


def _sensitive(key: str) -> bool:
    key = re.sub(r"([a-z])([A-Z])", r"\1_\2", key).lower()
    parts = set(re.split(r"[^a-z0-9]+", key))
    if parts & {"secret", "secrets", "password", "authorization", "authentication", "auth", "cookie", "credential", "credentials", "headers", "body", "payload", "message", "messages", "content", "stacktrace", "url", "query", "statement"} or "api_key" in key or "apikey" in key:
        return True
    if parts & {"prompt", "prompts", "completion", "completions", "input", "output", "token", "tokens"}:
        return not (key.startswith(("gen_ai.usage.", "llm.usage.")) or key in {"n8n.node.input_items", "n8n.node.output_items", "n8n.node.input.items", "n8n.node.output.items", "input_items", "output_items"})
    return False


def _attributes(items: Any, depth: int = 0) -> dict[str, Any]:
    if not isinstance(items, list):
        raise ValueError
    result: dict[str, Any] = {}
    size = 2
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("key"), str):
            continue
        key = item["key"]
        if _sensitive(key) or len(key) > 256:
            continue
        try:
            value = _value(item.get("value"), depth)
            if key.startswith(("gen_ai.usage.", "llm.usage.")) or key in {"n8n.node.input_items", "n8n.node.output_items", "n8n.node.input.items", "n8n.node.output.items", "input_items", "output_items"}:
                value = _integer(value)
            encoded = json.dumps({key: value}, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
            continue
        if size + len(encoded) <= MAX_ATTRIBUTES:
            result[key] = value
            size += len(encoded)
    return result


def _normalize(span: Any, resource: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(span, dict):
        raise ValueError
    parent = span.get("parentSpanId")
    if parent is not None and not isinstance(parent, str):
        raise ValueError
    start = _integer(span.get("startTimeUnixNano", 0))
    end = _integer(span.get("endTimeUnixNano", 0))
    if end < start:
        raise ValueError
    kind = _integer(span.get("kind", 0))
    status = span.get("status", {})
    if not isinstance(status, dict) or kind > 5:
        raise ValueError
    code = _integer(status.get("code", 0))
    if code > 2:
        raise ValueError
    attrs = dict(resource)
    attrs.update(_attributes(span.get("attributes", [])))
    # Re-apply the combined resource/span size bound without truncating JSON.
    safe: dict[str, Any] = {}
    size = 2
    for key, value in attrs.items():
        encoded = json.dumps({key: value}, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        if size + len(encoded) <= MAX_ATTRIBUTES:
            safe[key] = value
            size += len(encoded)
    result: dict[str, Any] = {
        "trace_id": _identifier(span.get("traceId"), 32), "span_id": _identifier(span.get("spanId"), 16),
        "parent_id": _identifier(parent, 16) if parent else "",
        "name": _text(span.get("name", "")), "kind": kind, "start_ns": start, "end_ns": end,
        "status": ("unset", "ok", "error")[code],
        "attributes_json": json.dumps(safe, ensure_ascii=False, separators=(",", ":"), allow_nan=False),
    }
    # The span's own error text (OTLP status.message) names what failed. It is used only for the error
    # record, after the same secret masking as repair context, capped, and never stored with the span.
    message = status.get("message")
    try:
        text = _text(message, 2048).strip() if code == 2 and isinstance(message, str) else ""
    except ValueError:
        text = ""  # Unusable error text never rejects the span; the record falls back to a generic line.
    result["status_message"] = cap_text(mask_text(text)[0], 300)[0] if text else ""
    for name, keys in {
        "workflow_id": ("n8n.workflow.id", "workflow.id", "workflow_id"),
        "workflow_name": ("n8n.workflow.name", "workflow.name", "workflow_name"),
        "execution_id": ("n8n.execution.id", "execution.id", "execution_id"),
        "node_id": ("n8n.node.id", "node.id", "node_id"),
        "node_name": ("n8n.node.name", "node.name", "node_name"),
        "model": ("gen_ai.response.model", "gen_ai.request.model", "llm.model_name"),
    }.items():
        found = next((safe[k] for k in keys if k in safe), None)
        result[name] = _text(str(found), MAX_ATTRIBUTES if name == "model" else 1024) if isinstance(found, (str, int)) and not isinstance(found, bool) else (None if name == "model" else "")
    for name, keys in {
        "tokens_in": ("gen_ai.usage.input_tokens", "gen_ai.usage.prompt_tokens", "llm.usage.prompt_tokens"),
        "tokens_out": ("gen_ai.usage.output_tokens", "gen_ai.usage.completion_tokens", "llm.usage.completion_tokens"),
        "input_items": ("n8n.node.input_items", "n8n.node.input.items", "input_items"),
        "output_items": ("n8n.node.output_items", "n8n.node.output.items", "output_items"),
    }.items():
        try:
            result[name] = _integer(next((safe[k] for k in keys if k in safe), None))
        except ValueError:
            result[name] = None
    return result


def parse_traces(body: bytes, content_type: str, content_encoding: str = "identity") -> dict[str, Any]:
    """Decode OTLP JSON/protobuf with per-span rejection and fixed safe errors."""
    if len(body) > MAX_BODY:
        raise IngestError(413, "otlp_too_large", "OTLP request exceeds size limit")
    encoding = content_encoding.strip().lower()
    if encoding not in ("identity", "none", "", "gzip"):
        raise IngestError(415, "unsupported_encoding", "Unsupported OTLP content encoding")
    if encoding == "gzip":
        try:
            decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
            body = decoder.decompress(body, MAX_DECOMPRESSED + 1)
            if len(body) > MAX_DECOMPRESSED or decoder.unconsumed_tail:
                raise IngestError(413, "otlp_too_large", "Decompressed OTLP request exceeds size limit")
            if not decoder.eof or decoder.unused_data:
                raise _bad()
        except zlib.error:
            raise _bad() from None
    media = content_type.split(";", 1)[0].strip().lower()
    try:
        if media == "application/json":
            envelope = json.loads(body, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        elif media == "application/x-protobuf":
            request = ExportTraceServiceRequest()
            request.ParseFromString(body)
            envelope = MessageToDict(request, use_integers_for_enums=True)
            for resource in envelope.get("resourceSpans", []):
                for scope in resource.get("scopeSpans", []):
                    for span in scope.get("spans", []):
                        for key in ("traceId", "spanId", "parentSpanId"):
                            if key in span:
                                span[key] = base64.b64decode(span[key]).hex()
        else:
            raise IngestError(415, "unsupported_media_type", "Unsupported OTLP content type")
        if not isinstance(envelope, dict) or not isinstance(envelope.get("resourceSpans", []), list):
            raise ValueError
        spans: list[dict[str, Any]] = []
        rejected = encountered = 0
        for resource in envelope.get("resourceSpans", []):
            if not isinstance(resource, dict) or not isinstance(resource.get("resource", {}), dict) or not isinstance(resource.get("scopeSpans", []), list):
                raise ValueError
            attrs = _attributes(resource.get("resource", {}).get("attributes", []))
            for scope in resource.get("scopeSpans", []):
                if not isinstance(scope, dict) or not isinstance(scope.get("spans", []), list):
                    raise ValueError
                for raw in scope.get("spans", []):
                    encountered += 1
                    if encountered > MAX_SPANS:
                        raise IngestError(413, "too_many_spans", "OTLP request exceeds span limit")
                    try:
                        spans.append(_normalize(raw, attrs))
                    except (ValueError, TypeError, UnicodeError, RecursionError):
                        rejected += 1
        return {"spans": spans, "rejected_spans": rejected}
    except (ValueError, TypeError, UnicodeError, DecodeError, RecursionError):
        raise _bad() from None
