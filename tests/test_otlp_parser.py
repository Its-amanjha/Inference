import gzip
import json

import pytest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

from backend.modules.observability.ingest import IngestError, parse_traces


def span(**changes):
    return {"traceId": "ab" * 16, "spanId": "cd" * 8, "name": "node", "startTimeUnixNano": "100", "endTimeUnixNano": "200", **changes}


def payload(spans, resource=None):
    return json.dumps({"resourceSpans": [{"resource": {"attributes": resource or []}, "scopeSpans": [{"spans": spans}]}]}).encode()


def attr(key, value):
    return {"key": key, "value": {"intValue" if isinstance(value, int) else "stringValue": value}}


def test_equivalent_json_protobuf_and_exact_model():
    attrs = [attr("gen_ai.request.model", "openai/gpt-4o-2024-08-06"), attr("gen_ai.usage.input_tokens", 23)]
    request = ExportTraceServiceRequest()
    s = request.resource_spans.add().scope_spans.add().spans.add()
    s.trace_id = bytes.fromhex("ab" * 16)
    s.span_id = bytes.fromhex("cd" * 8)
    s.name = "node"
    s.start_time_unix_nano = 100
    s.end_time_unix_nano = 200
    s.attributes.add(key="gen_ai.request.model").value.string_value = "openai/gpt-4o-2024-08-06"
    s.attributes.add(key="gen_ai.usage.input_tokens").value.int_value = 23
    actual = parse_traces(payload([span(attributes=attrs)]), "application/json")
    assert actual == parse_traces(request.SerializeToString(), "application/x-protobuf")
    assert actual["spans"][0]["tokens_in"] == 23
    assert actual["spans"][0]["model"] == "openai/gpt-4o-2024-08-06"


@pytest.mark.parametrize("changes", [{"traceId": "00" * 16}, {"spanId": "secret"}, {"parentSpanId": "00" * 8}, {"startTimeUnixNano": "-1"}, {"endTimeUnixNano": "99"}, {"startTimeUnixNano": True}, {"status": {"code": 9}}])
def test_individual_invalid_spans_are_rejected(changes):
    result = parse_traces(payload([span(**changes), span()]), "application/json")
    assert result["rejected_spans"] == 1
    assert len(result["spans"]) == 1


@pytest.mark.parametrize("body", [b"[]", b"null", b"{", b'{"resourceSpans":{}}', b'{"resourceSpans":[{"scopeSpans":"bad"}]}'])
def test_bad_envelope_is_safe_error(body):
    with pytest.raises(IngestError) as exc:
        parse_traces(body, "application/json")
    assert exc.value.status == 400


def test_limits_count_invalid_spans_and_bounded_gzip():
    for body, encoding in [(b"x" * (2 * 1024 * 1024 + 1), "identity"), (gzip.compress(b" " * (8 * 1024 * 1024 + 1)), "gzip"), (payload([{}] * 2001), "identity")]:
        with pytest.raises(IngestError) as exc:
            parse_traces(body, "application/json", encoding)
        assert exc.value.status == 413
    body = payload([span()])
    assert parse_traces(gzip.compress(body), "application/json", "gzip") == parse_traces(body, "application/json")


def test_secrets_content_and_large_values_dropped_counts_preserved():
    attrs = [attr(k, "PRIVATE") for k in ["api_key", "authorization", "gen_ai.prompt", "n8n.node.input", "gen_ai.output.messages", "http.request.header.cookie"]]
    attrs += [attr("huge", "x" * 20000), attr("n8n.node.output_items", 0), attr("gen_ai.usage.prompt_tokens", 8)]
    result = parse_traces(payload([span(attributes=attrs)]), "application/json")["spans"][0]
    assert "PRIVATE" not in result["attributes_json"]
    assert "huge" not in json.loads(result["attributes_json"])
    assert len(result["attributes_json"].encode()) <= 16384
    assert result["output_items"] == 0
    assert result["tokens_in"] == 8


def test_unicode_repair_is_selective_and_resource_cannot_assign_instance():
    attrs = [attr("n8n.workflow.name", "cafÃ©"), attr("ordinary", "東京 café Ã")]
    result = parse_traces(payload([span(attributes=attrs)], [attr("instance_id", "forged")]), "application/json")["spans"][0]
    assert result["workflow_name"] == "café"
    assert json.loads(result["attributes_json"])["ordinary"] == "東京 café Ã"
    assert "instance_id" not in result


@pytest.mark.parametrize("body,media,encoding,status", [(b"x", "text/plain", "identity", 415), (b"x", "application/json", "br", 415), (b"bad", "application/json", "gzip", 400), (b"\xff", "application/x-protobuf", "identity", 400)])
def test_transport_rejections(body, media, encoding, status):
    with pytest.raises(IngestError) as exc:
        parse_traces(body, media, encoding)
    assert exc.value.status == status


def test_usage_only_keeps_counts_and_nested_secrets_are_removed():
    attrs = [attr("gen_ai.usage.input_tokens", "PRIVATE"), attr("gen_ai.usage.cache_read.input_tokens", 2), attr("gen_ai.usage.reasoning.output_tokens", 3), attr("gen_ai.provider.name", "anthropic")]
    attrs.append({"key": "custom", "value": {"kvlistValue": {"values": [attr("password", "PRIVATE"), attr("safe", "ok")]}}})
    result = parse_traces(payload([span(attributes=attrs)]), "application/json")["spans"][0]
    safe = json.loads(result["attributes_json"])
    assert "PRIVATE" not in result["attributes_json"]
    assert safe["gen_ai.usage.cache_read.input_tokens"] == 2
    assert safe["gen_ai.usage.reasoning.output_tokens"] == 3
    assert safe["gen_ai.provider.name"] == "anthropic"
    assert safe["custom"] == {"safe": "ok"}


def test_model_identifier_is_never_silently_truncated():
    model = "vendor/" + "v" * 1200
    result = parse_traces(payload([span(attributes=[attr("gen_ai.request.model", model)])]), "application/json")
    assert result["spans"][0]["model"] == model


@pytest.mark.parametrize("changes", [{"startTimeUnixNano": str(2**63)}, {"kind": "SERVER"}, {"kind": -1}, {"name": "\ud800"}])
def test_storage_unsafe_span_is_rejected(changes):
    assert parse_traces(payload([span(**changes)]), "application/json")["rejected_spans"] == 1


def test_multiple_resource_attribute_budget_and_duplicate_preservation():
    resource = [attr("resource." + str(i), "r" * 1000) for i in range(20)]
    attrs = [attr("span." + str(i), "s" * 1000) for i in range(20)]
    result = parse_traces(payload([span(attributes=attrs)] * 2, resource), "application/json")
    assert len(result["spans"]) == 2
    assert len(result["spans"][0]["attributes_json"].encode()) <= 16384
    assert isinstance(json.loads(result["spans"][0]["attributes_json"]), dict)


def test_truncated_and_concatenated_gzip_are_rejected():
    compressed = gzip.compress(payload([span()]))
    for body in [compressed[:-4], compressed + compressed]:
        with pytest.raises(IngestError) as exc:
            parse_traces(body, "application/json", "gzip")
        assert exc.value.status == 400


def test_malicious_double_value_does_not_escape_safe_parser_boundary():
    attributes = [{"key": "measurement", "value": {"doubleValue": 10**1000}}]
    result = parse_traces(payload([span(attributes=attributes)]), "application/json")
    assert len(result["spans"]) == 1
    assert json.loads(result["spans"][0]["attributes_json"]) == {}


@pytest.mark.parametrize("parent", [False, 0, []])
def test_parent_id_must_be_string_or_null(parent):
    assert parse_traces(payload([span(parentSpanId=parent)]), "application/json")["rejected_spans"] == 1


def test_content_in_exception_and_urls_is_dropped():
    attrs = [attr("exception.message", "PRIVATE"), attr("exception.stacktrace", "PRIVATE"), attr("url.full", "https://host/?api_key=PRIVATE"), attr("http.url", "https://user:PRIVATE@host/")]
    safe = parse_traces(payload([span(attributes=attrs)]), "application/json")["spans"][0]["attributes_json"]
    assert "PRIVATE" not in safe
