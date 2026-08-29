from copy import deepcopy

import pytest

from backend.modules.observability.classifier import classify


SPAN = {'span_id': 's', 'node_id': 'n', 'node_name': 'Code', 'status': 'ok',
        'input_items': 10, 'output_items': 0}


def execution(status='success', runs=None):
    return {'status': status, 'stoppedAt': '1970-01-01T00:01:00Z',
            'data': {'resultData': {'runData': runs or {}}}}


def test_span_error_is_confirmed_without_execution_and_redacted():
    span = {**SPAN, 'status': 'error', 'error_summary': 'secret password xyz'}
    result = classify([span], None, 0)[0]
    assert result['health_status'] == 'error'
    assert 'xyz' not in str(result)


@pytest.mark.parametrize('status,now', [('waiting', 120), ('running', 120), ('success', 89)])
def test_wait_and_late_telemetry_cannot_infer_continuation_error(status, now):
    ex = execution(status, {'Code': [{'executionStatus': 'error'}]})
    assert classify([SPAN], ex, now * 10**9)[0]['silent'] == 0


@pytest.mark.parametrize('run', [{'executionStatus': 'error'}, {'error': {'message': 'secret'}},
    {'taskData': {'continuation': {'error': {'message': 'secret'}}}}])
def test_confirmed_run_error_after_grace(run):
    result = classify([SPAN], execution(runs={'Code': [run]}), 90 * 10**9)[0]
    assert result['health_status'] == 'error'
    assert result['silent'] == 1
    assert 'secret' not in str(result)


def test_continuation_boolean_does_not_prove_error():
    ex = execution(runs={'Code': [{'taskData': {'continuation': True}}]})
    assert classify([SPAN], ex, 100 * 10**9)[0]['silent'] == 0


def test_branch_absence_and_no_telemetry_never_error():
    ex = execution(runs={'Switch': [{'executionStatus': 'success'}]})
    ex['workflowData'] = {'nodes': [{'id': 'n', 'name': 'Code'},
        {'id': 'unused', 'name': 'Unvisited', 'disabled': True}]}
    result = classify([SPAN], ex, 100 * 10**9)
    assert all(d['health_status'] not in {'error', 'low'} for d in result)
    assert classify([], ex, 100 * 10**9) == []


def test_missing_executed_node_is_incomplete_not_failed():
    root = {'span_id': 'root', 'status': 'ok'}
    result = classify([root], execution(runs={'Code': [{}]}), 100 * 10**9)
    assert result[0]['health_status'] == 'incomplete'
    assert result[0]['silent'] == 0


def test_zero_output_requires_opt_in_ran_evidence_and_reliable_history():
    ex = execution(runs={'Code': [{'executionStatus': 'success'}]})
    span = {**SPAN, 'history': {'successful_runs': 10, 'zero_output_runs': 0}}
    assert classify([span], ex, 100 * 10**9)[0]['silent'] == 0
    assert classify([SPAN], ex, 100 * 10**9, zero_output_nodes={'n'})[0]['silent'] == 0
    assert classify([span], execution(), 100 * 10**9, zero_output_nodes={'n'})[0]['silent'] == 0
    result = classify([span], ex, 100 * 10**9, zero_output_nodes={'n'})[0]
    assert result['health_status'] == 'low'
    assert result['silent'] == 1


def test_unknown_and_inputs_preserved():
    span = {**SPAN, 'status': 'unset'}
    before = deepcopy(span)
    assert classify([span], None, 0)[0]['health_status'] == 'unknown'
    assert span == before
