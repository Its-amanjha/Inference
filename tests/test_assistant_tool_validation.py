"""Rejected tool calls must name the tool and the failing fields, and never echo their values."""
import pytest
from fastapi import HTTPException

from backend.modules.assistant.tools import validate

SECRET = 'sk-live-TOOL-ARGUMENT-SECRET-0123456789'


def rejected(name, arguments):
    with pytest.raises(HTTPException) as caught:
        validate(name, arguments)
    assert caught.value.status_code == 422
    return caught.value.detail


def test_missing_required_field_is_named():
    detail = rejected('propose_workflow_patch', {'instance_id': 'alpha', 'workflow_id': 'wf'})
    assert detail['code'] == 'invalid_tool_arguments'
    assert 'propose_workflow_patch' in detail['detail']
    assert 'base_content_hash' in detail['detail'] and 'operations' in detail['detail']


def test_wrong_type_is_named_without_its_value():
    detail = rejected('propose_workflow_patch', {
        'instance_id': 'alpha', 'workflow_id': 'wf', 'base_content_hash': 'a' * 64,
        'summary': 'Fix the call', 'operations': [{'op': 'set_code', 'node': 'Code', 'code': 12345}]})
    assert 'operations' in detail['detail'] and 'code' in detail['detail']
    assert '12345' not in detail['detail']


def test_unknown_field_is_named_and_values_never_leak():
    detail = rejected('propose_workflow_patch', {
        'instance_id': 'alpha', 'workflow_id': 'wf', 'base_content_hash': 'a' * 64, 'summary': 'Fix',
        'operations': [{'op': 'set_parameter', 'node': 'HTTP Request', 'path': 'url', 'value': 'https://example.test'}],
        'api_key': SECRET})
    assert 'api_key' in detail['detail']
    assert SECRET not in detail['detail']


def test_unknown_tool_still_reports_separately():
    detail = rejected('drop_database', {'instance_id': 'alpha'})
    assert detail['code'] == 'unknown_tool'


def test_detail_stays_bounded_for_many_bad_fields():
    operations = [{'op': 'set_code', 'node': 'Code', 'code': index} for index in range(40)]
    detail = rejected('propose_workflow_patch', {
        'instance_id': 'alpha', 'workflow_id': 'wf', 'base_content_hash': 'a' * 64,
        'summary': 'Fix', 'operations': operations})
    assert len(detail['detail']) < 500


def test_valid_arguments_still_pass():
    result = validate('propose_workflow_patch', {
        'instance_id': 'alpha', 'workflow_id': 'wf', 'base_content_hash': 'a' * 64, 'summary': 'Fix the URL',
        'operations': [{'op': 'set_parameter', 'node': 'HTTP Request', 'path': 'url', 'value': 'https://example.test/v2'}]})
    assert result['workflow_id'] == 'wf' and result['operations'][0]['op'] == 'set_parameter'
