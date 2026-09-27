"""Browser checks for Observability › AI cost against the loopback n8n simulator: cost read from n8n's own
executions (no telemetry), an HTTP Request call to OpenAI priced from its usage block, an n8n chat-model call
with no price, and no run payload reaching the browser."""
import re

from playwright.sync_api import expect

from frontend_fleet_fixture import AI_PRIVATE_TEXT, choose_instance


def exercise_ai_cost(page, state, artifacts):
    state['ai_runs'] = True
    # Listeners must be Python functions; bodies are read only for requests that finished.
    finished = []

    def keep(request):
        if '/api/otel/runs' in request.url:
            finished.append(request)
    page.on('requestfinished', keep)
    try:
        page.set_viewport_size({'width': 1440, 'height': 1000})
        # Switch to All instances on a quiet view: the switch re-renders the open view.
        page.evaluate("location.hash = 'notes'")
        page.locator('#main h1').filter(has_text='Notes').wait_for()
        choose_instance(page, '')
        page.evaluate("location.hash = 'observe'")
        expect(page.get_by_role('button', name='AI cost', exact=True)).to_have_attribute('aria-pressed', 'true')
        strip = page.get_by_role('region', name='AI cost, last 7 days')
        # 4,000 input tokens at $2, 8,000 cached at $0.50 and 900 output at $8 per million (gpt-4.1's bundled
        # price) is $0.0192; the gpt-4o-mini call has no price here, so the total is a lower bound.
        expect(strip).to_contain_text('At least $0.0192')
        expect(strip.locator('.observe-metric-cell').filter(has_text='Without a price').locator('.metric-number')).to_have_text('1')
        by_workflow = page.get_by_role('region', name='AI cost by workflow')
        expect(by_workflow.get_by_role('cell', name='Invoice summary', exact=True)).to_have_count(1)
        runs = page.get_by_role('region', name='Runs with AI calls')
        expect(runs).to_contain_text('12,600 in · 940 out')
        runs.get_by_role('button', name=re.compile(r'^Invoice summary, started ')).click()
        panel = page.get_by_role('dialog', name='Invoice summary')
        calls = panel.locator('.run-calls > li')
        expect(calls).to_have_count(2)
        summarize = calls.filter(has_text='Summarize invoice')
        expect(summarize).to_contain_text('$0.0192')
        expect(summarize).to_contain_text('OpenAI · gpt-4.1-2025-04-14')
        expect(summarize).to_contain_text('12,000 in (8,000 cached) · 900 out · bundled price')
        expect(calls.filter(has_text='OpenAI Chat Model')).to_contain_text('No price')
        expect(calls.filter(has_text='OpenAI Chat Model')).to_contain_text('600 in · 40 out · no price for this model')
        page.screenshot(path=str(artifacts / 'ai-cost-run.png'))
        panel.get_by_role('button', name='Close details', exact=True).click()
        expect(page.get_by_role('dialog')).to_have_count(0)
        # A run on record is not read again; the totals stay the same.
        page.get_by_role('button', name='Check for new runs', exact=True).click()
        expect(page.locator('#observe-cost .load-progress')).to_have_count(0)
        expect(strip).to_contain_text('At least $0.0192')
        page.screenshot(path=str(artifacts / 'ai-cost-desktop.png'), full_page=True)
        # Traces stay one tab away, and a link can open them directly.
        page.get_by_role('button', name='Traces', exact=True).click()
        expect(page.get_by_role('region', name='Telemetry summary metrics')).to_be_visible()
        expect(strip).to_be_hidden()
        page.evaluate("location.hash = 'notes'")
        page.locator('#main h1').filter(has_text='Notes').wait_for()
        page.evaluate("location.hash = 'observe?tab=traces'")
        expect(page.get_by_role('button', name='Traces', exact=True)).to_have_attribute('aria-pressed', 'true')
        page.set_viewport_size({'width': 390, 'height': 844})
        page.get_by_role('button', name='AI cost', exact=True).click()
        expect(page.get_by_role('region', name='AI cost, last 7 days')).to_contain_text('At least $0.0192')
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'AI cost overflows on a phone'
        page.screenshot(path=str(artifacts / 'ai-cost-mobile.png'), full_page=True)
        page.set_viewport_size({'width': 1440, 'height': 1000})
        # Only counts, models and prices leave the server: no prompt or answer text.
        assert AI_PRIVATE_TEXT not in page.content()
        assert finished, 'no AI cost request finished'
        for request in finished:
            response = request.response()
            if response and response.ok:
                assert AI_PRIVATE_TEXT not in response.text(), request.url
    finally:
        page.remove_listener('requestfinished', keep)
        state['ai_runs'] = False
    print('PASS: AI cost read from n8n executions without telemetry: HTTP Request usage priced with cached input, '
          'an unpriced n8n model shown as a lower bound, per-workflow totals, run panel, tabs, phone layout, no payload text.', flush=True)
