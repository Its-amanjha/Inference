"""UI-only response fixtures for the Inference redesign; never production data."""
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from playwright.sync_api import Page, expect


def exercise_design(page: Page, artifacts: Path) -> None:
    # Existing smoke checks exercise real aggregates. These deterministic replies
    # exercise visual states without repeatedly hitting dashboard scan limits.
    metrics = {
        'success_rate_percent': None, 'terminal_executions_24h': 0,
        'workflows': None, 'executions_24h': None,
        'confirmed_silent_error_spans_24h': 1,
        'scope': 'Browser response fixture; incomplete saved history.',
        'window_end': '2026-09-14T00:00:00Z',
        'data': [{'instance_id': 'source', 'name': 'Source',
                  'workflows_complete': False, 'executions_complete': False}],
    }
    # Same shape as SQLite datetime('now'): UTC without a zone designator.
    recent = (datetime.now(timezone.utc) - timedelta(minutes=12)).strftime('%Y-%m-%d %H:%M:%S')
    findings = {'data': [{
        'id': 'fixture-error', 'instance_id': 'source', 'workflow_name': 'Order synchronization',
        'node_name': 'Update inventory', 'count': 2, 'error_type': 'continuation_error',
        'error_message': '<img src=x onerror=alert(1)> Upstream service returned an error.',
        'occurred_at': recent, 'execution_id': '4821',
    }], 'next_cursor': None}
    triage = {'data': [dict(findings['data'][0]), {
        'id': 'fixture-error-2', 'instance_id': 'source', 'workflow_name': 'Invoice export',
        'node_name': '', 'count': 1, 'error_type': 'span_error', 'execution_id': '',
        'occurred_at': recent, 'error_message': 'Telemetry reports an execution error.',
    }], 'next_cursor': None}
    page.route('**/api/n8n_proxy/dashboard*', lambda route: route.fulfill(json=metrics))
    page.route('**/api/errors?*', lambda route: route.fulfill(json=findings))
    try:
        page.evaluate("location.hash = 'dashboard'")
        page.get_by_text('Order synchronization', exact=True).wait_for()
        expect(page.get_by_text('Silent failure', exact=True)).to_be_visible()
        expect(page.locator('.finding-row img')).to_have_count(0)
        expect(page.locator('.finding-row .severity-pill.error')).to_have_text('Silent failure')
        expect(page.locator('.finding-row time[datetime]')).to_contain_text(re.compile(r'\d+ min ago'))
        expect(page.locator('.finding-row').get_by_text('Execution 4821', exact=True)).to_be_visible()
        expect(page.locator('.metric-label svg')).to_have_count(4)
        # Metric values are plain text; each metric is named by its label, not by a bare-number heading.
        expect(page.locator('.metric-cell h2')).to_have_count(0)
        expect(page.locator('.metric-cell .metric-number')).to_have_count(4)
        expect(page.get_by_role('article', name='Total workflows')).to_contain_text('Unavailable')
        expect(page.locator('.dashboard .panel-head svg')).to_have_count(3)
        assert page.locator('.metric-strip').bounding_box()['y'] < page.locator('.dashboard-columns').bounding_box()['y']
        expect(page.locator('.coverage-details')).not_to_have_attribute('open', '')
        # Partial data is a dashed amber status note, never the red failure treatment.
        expect(page.locator('.coverage-partial[role=status]')).to_be_visible()
        expect(page.locator('.coverage-partial')).to_contain_text('Source: workflow and execution history incomplete')
        expect(page.locator('.dashboard .coverage .error')).to_have_count(0)
        expect(page.locator('.metric-unknown')).to_have_count(2)
        expect(page.get_by_text('Coverage incomplete', exact=True)).to_have_count(2)
        expect(page.get_by_text('No completed runs', exact=True)).to_be_visible()
        expect(page.get_by_role('link', name='Explain with AI')).to_have_attribute(
            'href', '#assistant?error_id=fixture-error&instance_id=source')
        expect(page.get_by_role('navigation', name='All views')).to_be_visible()
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.screenshot(path=str(artifacts / 'inference-populated-desktop.png'), full_page=True)
        page.get_by_role('link', name='Explain with AI').click()
        page.get_by_role('heading', name='AI copilot', exact=False).wait_for()
        page.evaluate("location.hash = 'dashboard'")
        page.get_by_text('Order synchronization', exact=True).wait_for()
        page.evaluate("async () => (await import('/js/themes.js')).setTheme('dark')")
        expect(page.locator('html')).to_have_attribute('data-theme', 'dark')
        page.screenshot(path=str(artifacts / 'inference-populated-dark.png'), full_page=True)
        page.set_viewport_size({'width': 390, 'height': 844})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        expect(page.get_by_role('navigation', name='All views')).not_to_be_visible()
        page.screenshot(path=str(artifacts / 'inference-populated-mobile.png'), full_page=True)
        page.evaluate("async () => (await import('/js/themes.js')).setTheme('light')")
        page.unroute('**/api/errors?*')
        page.route('**/api/errors?*', lambda route: route.fulfill(
            status=503, content_type='application/json',
            body=json.dumps({'detail': 'Findings temporarily unavailable.', 'code': 'unavailable'})))
        page.get_by_role('button', name='Refresh', exact=True).click()
        page.get_by_text('Findings temporarily unavailable.', exact=True).wait_for()
        expect(page.get_by_text('No open findings', exact=True)).to_have_count(0)
        expect(page.get_by_text('Total workflows', exact=True)).to_be_visible()

        # Errors view: dense triage table, loaded-page type filters and in-place resolve.
        page.unroute('**/api/errors?*')
        page.route('**/api/errors?*', lambda route: route.fulfill(json=triage))
        page.route('**/api/errors/*/resolve', lambda route: route.fulfill(
            json={'id': 'fixture-error', 'status': 'resolved'}))
        page.set_viewport_size({'width': 1440, 'height': 1000})
        page.evaluate("location.hash = 'errors'")
        errors_table = page.get_by_role('region', name='Errors table')
        errors_table.wait_for()
        expect(errors_table.locator('tbody tr')).to_have_count(2)
        expect(errors_table.locator('.severity-pill.error').first).to_have_text('Silent failure')
        expect(errors_table.get_by_text('Execution error', exact=True)).to_be_visible()
        expect(errors_table.locator('time[datetime]').first).to_contain_text(re.compile(r'\d+ min ago'))
        expect(errors_table.get_by_text('Unavailable', exact=True)).to_have_count(1)
        expect(errors_table.locator('img')).to_have_count(0)
        filters = page.get_by_role('group', name='Filter loaded errors by type')
        filters.get_by_role('button', name='Silent failures', exact=True).click()
        expect(filters.get_by_role('button', name='Silent failures', exact=True)).to_have_attribute('aria-pressed', 'true')
        expect(page.get_by_text('Showing 1 of 2 loaded', exact=True)).to_be_visible()
        expect(errors_table.locator('tbody tr')).to_have_count(1)
        filters.get_by_role('button', name='All', exact=True).click()
        expect(page.get_by_text('Showing 2 of 2 loaded', exact=True)).to_be_visible()
        errors_table.get_by_role('button', name='Mark resolved').first.click()
        expect(page.locator('.triage-announce[role=status]')).to_have_text(
            'Order synchronization · Update inventory marked resolved.')
        expect(errors_table.locator('tbody tr')).to_have_count(1)
        expect(errors_table.get_by_role('button', name='Mark resolved')).to_be_focused()
        expect(page.get_by_text('Showing 1 of 1 loaded', exact=True)).to_be_visible()
        page.set_viewport_size({'width': 390, 'height': 844})
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.screenshot(path=str(artifacts / 'inference-errors-mobile.png'), full_page=True)
        print('PASS: Inference desktop/mobile navigation, anomaly queue, inert error text, explicit assistant target, partial coverage, dark theme, isolated API failure and error triage table.', flush=True)
    finally:
        page.unroute('**/api/n8n_proxy/dashboard*')
        page.unroute('**/api/errors?*')
        page.unroute('**/api/errors/*/resolve')
        page.set_viewport_size({'width': 1440, 'height': 1000})
