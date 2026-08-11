"""Conservative evidence-only diagnostics. Never infer failure from topology."""

from datetime import datetime, timezone
from typing import Any


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _terminal_ns(execution: dict) -> int | None:
    status = execution.get('status')
    if status not in {'success', 'error', 'canceled', 'cancelled', 'crashed'}:
        return None
    raw = execution.get('end_ns')
    if type(raw) is int and raw >= 0:
        return raw
    raw = execution.get('stoppedAt', execution.get('stopped_at'))
    if not isinstance(raw, str):
        return None
    try:
        end = datetime.fromisoformat(raw.replace('Z', '+00:00'))
        if end.tzinfo is None:
            return None
        delta = end - datetime(1970, 1, 1, tzinfo=timezone.utc)
        return (delta.days * 86400 + delta.seconds) * 10**9 + delta.microseconds * 1000
    except (ValueError, OverflowError):
        return None


def _failed(run: dict) -> bool:
    if run.get('executionStatus') == 'error' or bool(run.get('error')):
        return True
    continuation = _dict(_dict(run.get('taskData')).get('continuation'))
    return continuation.get('executionStatus') == 'error' or bool(continuation.get('error'))


def classify(
    spans: list[dict], execution: dict | None, now_ns: int,
    grace_seconds: int = 30, zero_output_nodes: set[str] | None = None,
) -> list[dict]:
    """Return fixed, payload-free diagnostics without modifying supplied objects.

    Item-drop history is trusted caller evidence in span.history, containing at
    least ten successful_runs and zero zero_output_runs. No topology-derived
    expected-node list is used: only execution runData proves a node actually ran.
    """
    ex = _dict(execution)
    terminal = _terminal_ns(ex)
    ready = terminal is not None and now_ns >= terminal + max(30, grace_seconds) * 10**9
    data = _dict(ex.get('data'))
    result_data = _dict(data.get('resultData', ex.get('resultData')))
    run_data = _dict(result_data.get('runData', ex.get('runData')))
    runs = {name: [run for run in values if isinstance(run, dict)]
            for name, values in run_data.items() if isinstance(values, list)}
    observed = {span.get('node_name') or span.get('name') for span in spans if span.get('node_id')}
    missing_executed = any(values and name not in observed for name, values in runs.items())
    diagnostics = []
    for span in spans:
        status = span.get('status')
        item = {'span_id': span.get('span_id', ''),
                'health_status': 'ok' if status == 'ok' else 'unknown',
                'error_type': None, 'error_summary': None, 'silent': 0}
        node_runs = runs.get(span.get('node_name') or span.get('name'), [])
        if status == 'error':
            item.update(health_status='error', error_type='span_error',
                        error_summary='Telemetry reports an execution error.')
        elif ready and any(_failed(run) for run in node_runs):
            item.update(health_status='error', error_type='continuation_error',
                        error_summary='Execution evidence confirms a node error.', silent=1)
        elif ready and node_runs and span.get('node_id') in (zero_output_nodes or set()):
            history = _dict(span.get('history'))
            successful = history.get('successful_runs')
            inp, out = span.get('input_items'), span.get('output_items')
            if (type(inp) is int and inp > 0 and type(out) is int and out == 0
                    and type(successful) is int and successful >= 10
                    and type(history.get('zero_output_runs')) is int
                    and history['zero_output_runs'] == 0):
                item.update(health_status='low', error_type='zero_output',
                            error_summary='Opted-in node produced no items despite reliable output history.',
                            silent=1)
        if ready and missing_executed and not span.get('node_id') and not item['error_type']:
            item.update(health_status='incomplete', error_type='incomplete_telemetry',
                        error_summary='An executed node has no matching telemetry; failure is not established.')
        diagnostics.append(item)
    return diagnostics
