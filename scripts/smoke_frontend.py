"""Exercise native frontend with real local API and an isolated data directory."""
from pathlib import Path
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time

import httpx
from playwright.sync_api import sync_playwright, expect
from frontend_fleet_fixture import fleet_fixture, exercise_fleet
from frontend_admin_checks import exercise_admin
from frontend_theme_checks import choose_theme

ROOT = Path(__file__).resolve().parents[1]


def _launch_browser(pw):
    """Launch Playwright Chromium with configurable channel and flags.

    Default MUST remain channel='msedge' for Windows reviewer parity.
    Overrides:
      CLI flag: --browser <name> or --browser=<name>
      Env var:  INFERENCE_SMOKE_BROWSER=<name>
    Special values:
      'chromium', 'bundled' -> launches bundled Chromium (no channel).
    """
    choice = None
    for i, arg in enumerate(sys.argv[:-1]):
        if arg == '--browser':
            choice = sys.argv[i + 1].strip()
            break
    if choice is None:
        for arg in sys.argv:
            if arg.startswith('--browser='):
                choice = arg.split('=', 1)[1].strip()
                break
    if choice is None:
        choice = os.environ.get('INFERENCE_SMOKE_BROWSER', '').strip() or None

    if choice is None:
        choice = 'msedge'

    kwargs = {'headless': True}
    args = []
    if sys.platform.startswith('linux'):
        args.extend(['--no-sandbox', '--disable-dev-shm-usage', '--disable-setuid-sandbox'])

    extra_args = os.environ.get('INFERENCE_SMOKE_BROWSER_ARGS', '').strip()
    if extra_args:
        args.extend(extra_args.split())

    if args:
        kwargs['args'] = args

    if choice.lower() not in ('chromium', 'bundled', 'none', ''):
        kwargs['channel'] = choice

    return pw.chromium.launch(**kwargs)


def main() -> None:
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    base = f'http://127.0.0.1:{port}'
    artifacts = ROOT / 'test-results'
    artifacts.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='inference-ui-', ignore_cleanup_errors=True) as folder:
        env = {**os.environ, 'INFERENCE_DATA_DIR': folder, 'INFERENCE_SECURE_COOKIES': 'false', 'INFERENCE_EGRESS_ALLOW_CIDRS': '127.0.0.1/32',
               'INFERENCE_BOOTSTRAP_TOKEN': '', 'INFERENCE_ALLOWED_ORIGINS': ''}
        with (artifacts / 'frontend-server.log').open('w') as log:
            process = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'backend.main:create_app',
                '--factory', '--host', '127.0.0.1', '--port', str(port), '--no-access-log'],
                cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            try:
                with httpx.Client(trust_env=False) as client:
                    for _ in range(150):
                        try:
                            if client.get(base + '/healthz').status_code == 200:
                                break
                        except httpx.TransportError:
                            pass
                        time.sleep(.1)
                    else:
                        raise RuntimeError('Local server did not start')
                with sync_playwright() as pw:
                    browser = _launch_browser(pw)
                    page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                    page.add_init_script("window.WebSocket = class extends WebSocket { constructor(...args) { super(...args); window.smokeSocket = this; } }")
                    errors = []
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.goto(base, wait_until='domcontentloaded')
                    page.get_by_label('Username', exact=True).wait_for()
                    page.screenshot(path=str(artifacts / 'frontend-setup.png'), full_page=True)
                    password = secrets.token_urlsafe(24)
                    page.get_by_label('Username', exact=True).fill('uiowner')
                    page.get_by_label('Password', exact=True).fill(password)
                    page.get_by_label('Confirm password', exact=True).fill(password)
                    page.get_by_role('button', name='Create admin account').click()
                    page.get_by_role('heading', name='Fleet overview', exact=True).wait_for()
                    # The metric strip is the dashboard's first data; the telemetry strip was removed.
                    page.get_by_role('article', name='Total workflows', exact=True).wait_for()
                    assert page.evaluate('getComputedStyle(document.body).backgroundColor') == 'rgb(245, 247, 249)'
                    expect(page.get_by_role('navigation', name='All views')).to_be_visible()
                    expect(page.get_by_role('navigation', name='All views').get_by_role('link')).to_have_count(13)
                    expect(page.get_by_role('navigation', name='All views').get_by_role('link', name='Workflows', exact=True)).to_have_attribute('href', '#workflows')
                    page.screenshot(path=str(artifacts / 'frontend-dashboard.png'), full_page=True)
                    # Sidebar group names label their links; they are not headings ahead of the page h1.
                    expect(page.locator('.sidebar h2')).to_have_count(0)
                    assert page.evaluate("document.querySelector('h1,h2,h3,h4,h5,h6')?.tagName") == 'H1'
                    expect(page.get_by_role('group', name='Monitor', exact=True)).to_be_visible()
                    # The live-update and theme-reload checks rebuild the overview; serve an empty-fleet
                    # fixture so they do not spend the real dashboard limit (4/minute) that the fleet
                    # aggregate check below depends on.
                    page.route('**/api/n8n_proxy/dashboard*', lambda route: route.fulfill(json={
                        'workflows': 0, 'executions_24h': 0, 'terminal_executions_24h': 0,
                        'success_rate_percent': None, 'confirmed_silent_error_spans_24h': 0,
                        'window_start': '2026-09-14T00:00:00+00:00', 'window_end': '2026-09-15T00:00:00+00:00',
                        'data': [], 'scope': 'Browser fixture for live-update checks.',
                        'limits': {'pages_per_resource': 10, 'rows_per_page': 250, 'concurrency': 3, 'instance_timeout_seconds': 30}}))
                    # Live events offer a refresh instead of rebuilding the view under the reader.
                    page.evaluate("document.querySelector('#toasts').replaceChildren(); document.querySelector('#main .stack').dataset.smokeMarker = 'kept'")
                    page.evaluate("for (const n of [1, 2]) window.smokeSocket.onmessage({data:JSON.stringify({event:'backups.changed'})})")
                    updates = page.locator('#main .live-updates[role=status]')
                    expect(updates).to_have_count(1)
                    expect(updates).to_contain_text('New activity since this view loaded.')
                    expect(updates.locator('.live-updates-count')).to_have_text(re.compile(r'^\d+ updates$'))
                    assert page.evaluate("!!(document.querySelector('#main .live-updates').compareDocumentPosition(document.querySelector('#main h1')) & Node.DOCUMENT_POSITION_FOLLOWING)")
                    page.wait_for_timeout(1000)
                    expect(page.locator('#main [data-smoke-marker=kept]')).to_have_count(1)
                    backup_toast = page.locator('#toasts .toast').filter(has_text='Backup history updated.')
                    expect(backup_toast).to_have_count(1)
                    expect(backup_toast.locator('.toast-count')).to_have_text(re.compile(r'^×\d+$'))
                    expect(backup_toast.get_by_role('button', name='Dismiss notification', exact=True)).to_be_visible()
                    for event in ['notes.changed', 'workflow:promoted', 'langgraph:run']:
                        page.evaluate("event => window.smokeSocket.onmessage({data:JSON.stringify({event})})", event)
                    expect(page.locator('#toasts .toast')).to_have_count(3)
                    expect(backup_toast).to_have_count(0)
                    overlaps = page.evaluate('''() => {
                        const hit = (a, b) => a.left < b.right && b.left < a.right && a.top < b.bottom && b.top < a.bottom;
                        const boxes = [...document.querySelectorAll('#toasts .toast')].map(node => node.getBoundingClientRect());
                        const controls = [...document.querySelectorAll('.topbar a, .topbar button, .topbar select,#main .page-heading-row a, #main .page-heading-row button, #main .live-updates button')];
                        return controls.filter(node => boxes.some(box => hit(box, node.getBoundingClientRect()))).map(node => node.textContent.trim() || node.getAttribute('aria-label'));
                    }''')
                    assert overlaps == [], overlaps
                    page.get_by_role('button', name='Refresh view', exact=True).click()
                    expect(updates).to_have_count(0)
                    page.get_by_role('heading', name='Fleet overview', exact=True).wait_for()
                    expect(page.locator('#main [data-smoke-marker=kept]')).to_have_count(0)
                    notes_toast = page.locator('#toasts .toast').filter(has_text='Notes updated.')
                    notes_toast.get_by_role('button', name='Dismiss notification', exact=True).click()
                    expect(notes_toast).to_have_count(0)
                    page.evaluate("document.querySelector('#toasts').replaceChildren()")
                    choose_theme(page, 'Dark')
                    expect(page.locator('html')).to_have_attribute('data-theme', 'dark')
                    assert page.evaluate('getComputedStyle(document.body).backgroundColor') == 'rgb(24, 25, 27)'
                    page.reload(wait_until='domcontentloaded')
                    expect(page.locator('html')).to_have_attribute('data-theme', 'dark')
                    choose_theme(page, 'Light')
                    expect(page.locator('html')).to_have_attribute('data-theme', 'light')
                    page.unroute('**/api/n8n_proxy/dashboard*')
                    routes = ['workflows','promote', 'errors', 'observe', 'fleet-health', 'codelab',
                              'assistant', 'containers', 'notes', 'backup', 'secrets', 'instances', 'settings']
                    for route in routes:
                        page.evaluate('(route) => location.hash = route', route)
                        page.wait_for_function("() => !document.querySelector('#main')?.textContent.includes('Loading ')")
                        page.locator('#main h1').wait_for()
                        assert 'Try again' not in page.locator('#main').inner_text(), route
                        if route == 'assistant':
                            page.get_by_role('heading', name='Connect a model to get started', exact=True).wait_for()
                            expect(page.get_by_role('button', name='Send message', exact=True)).to_be_disabled()
                            expect(page.get_by_label('Your message', exact=True)).to_be_disabled()
                            page.set_viewport_size({'width':1520,'height':900})
                            composer = page.get_by_role('button', name='Send message', exact=True).bounding_box()
                            assert composer and composer['y'] + composer['height'] <= 900, composer
                            assert page.locator('.assistant-chat').bounding_box()['width'] > 650
                            page.get_by_role('button', name='Connect model', exact=True).click()
                            expect(page.get_by_label('Connection name', exact=True)).to_be_focused()
                            page.get_by_text('Providers and remote tools', exact=True).click()
                            page.evaluate('scrollTo(0,0)')
                            page.screenshot(path=str(artifacts / 'assistant-desktop.png'), full_page=True)
                            page.set_viewport_size({'width':390,'height':844})
                            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                            page.screenshot(path=str(artifacts / 'assistant-mobile.png'), full_page=True)
                            page.set_viewport_size({'width':1440,'height':1000})
                        print('View:', route, flush=True)
                    page.evaluate("location.hash = 'notes'")
                    page.get_by_label('Markdown path', exact=True).fill('Smoke.md')
                    page.get_by_label('Markdown content', exact=True).fill('# Local smoke\n<script>window.compromised=true</script>')
                    page.get_by_role('button', name='Save note', exact=True).click()
                    page.get_by_text('Note saved.', exact=True).wait_for()
                    expect(page.get_by_role('region', name='Markdown preview').get_by_role('heading', name='Local smoke')).to_have_count(1)
                    assert page.evaluate('window.compromised') is None
                    markdown = page.evaluate('''async () => {
                        const {renderMarkdown} = await import('/js/lib/markdown.js');
                        const host = document.createElement('div');
                        renderMarkdown(host, '# Title\\n\\n**bold** and *italic* with `code`\\n\\n- first\\n- second\\n\\n3. third\\n\\n> quotation\\n\\n```html\\n<script>alert(1)</script>\\n```\\n\\n[safe](https://example.com) [unsafe](javascript:alert) ![image](https://example.com/x.png)\\n<img src=x onerror=alert(1)>');
                        const result = {heading:host.querySelector('h1')?.textContent,
                            bold:host.querySelector('strong')?.textContent,
                            emphasis:host.querySelector('em')?.textContent,
                            list:host.querySelectorAll('li').length,
                            quote:host.querySelector('blockquote')?.textContent,
                            code:host.querySelector('pre code')?.textContent,
                            links:[...host.querySelectorAll('a')].map(a=>a.getAttribute('href')),
                            dangerous:host.querySelectorAll('script,img,iframe,object,style,[onerror]').length};
                        renderMarkdown(host, 'x\\n'.repeat(5001));
                        result.large = host.querySelectorAll('pre').length;
                        return result;
                    }''')
                    assert markdown == {'heading':'Title', 'bold':'bold', 'emphasis':'italic', 'list':3,
                        'quote':'quotation', 'code':'<script>alert(1)</script>',
                        'links':['https://example.com'], 'dangerous':0, 'large':1}, markdown
                    page.reload(wait_until='domcontentloaded')
                    page.get_by_role('button', name='Smoke.md', exact=True).click()
                    expect(page.get_by_label('Markdown content')).to_have_value('# Local smoke\n<script>window.compromised=true</script>')
                    page.set_viewport_size({'width': 390, 'height': 844})
                    page.evaluate("location.hash = 'dashboard'")
                    page.get_by_role('heading', name='Fleet overview', exact=True).wait_for()
                    page.screenshot(path=str(artifacts / 'frontend-mobile.png'), full_page=True)
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'Mobile page overflow'
                    page.evaluate("window.smokeSocket.onmessage({data:JSON.stringify({event:'notes.changed'})})")
                    mobile_toast = page.locator('#toasts .toast').filter(has_text='Notes updated.')
                    expect(mobile_toast).to_have_count(1)
                    box = mobile_toast.bounding_box()
                    assert box and box['x'] >= 15 and box['x'] + box['width'] <= 390 - 15 and box['y'] + box['height'] <= 844, box
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth'), 'Toast overflow'
                    page.evaluate("document.querySelector('#toasts').replaceChildren()")
                    page.get_by_text('Menu', exact=True).click()
                    assert page.get_by_role('navigation', name='All views').is_visible()
                    page.screenshot(path=str(artifacts / 'frontend-mobile-nav.png'), full_page=True)
                    page.get_by_role('navigation', name='All views').get_by_role('link', name='Settings', exact=True).click()
                    expect(page.get_by_role('navigation', name='All views')).not_to_be_visible()
                    page.locator('#main h1').wait_for()
                    page.get_by_role('button', name='Account menu', exact=True).click()
                    page.get_by_role('button', name='Sign out', exact=True).click()
                    page.get_by_role('button', name='Sign in', exact=True).wait_for()
                    page.get_by_label('Username', exact=True).fill('uiowner')
                    page.get_by_label('Password', exact=True).fill(password)
                    page.get_by_role('button', name='Sign in', exact=True).click()
                    page.get_by_role('button', name='Account menu', exact=True).wait_for()
                    page.set_viewport_size({'width': 1440, 'height': 1000})
                    with fleet_fixture() as (fixture_url, fixture_state):
                        exercise_fleet(page, fixture_url, fixture_state)
                        # AI repair runs after the approval scenario and the fleet aggregate check that spends the
                        # real dashboard budget; its own dashboard visit is served from a fixture.
                        from frontend_repair_checks import exercise_repair
                        exercise_repair(page, fixture_url, fixture_state, artifacts)
                    exercise_admin(page)
                    from frontend_design_checks import exercise_design
                    exercise_design(page, artifacts)
                    from frontend_theme_checks import exercise_themes
                    exercise_themes(page, artifacts)
                    from frontend_setup_checks import exercise_setup
                    exercise_setup(page, artifacts)
                    race = page.evaluate('''async () => {
                        const {setTheme} = await import('/js/themes.js?smoke-race');
                        await Promise.all([setTheme('dark'),setTheme('light')]);
                        return document.documentElement.dataset.theme;
                    }''')
                    assert race == 'light'
                    page.route('**/themes/dark.json', lambda route: route.fulfill(status=503, body='Unavailable'))
                    failed = page.evaluate('''async () => {
                        const {setTheme} = await import('/js/themes.js?smoke-failure');
                        return await setTheme('dark');
                    }''')
                    assert failed is False
                    expect(page.locator('html')).to_have_attribute('data-theme','light')
                    assert page.evaluate('getComputedStyle(document.body).backgroundColor') == 'rgb(245, 247, 249)'
                    page.unroute('**/themes/dark.json')
                    assert page.evaluate("async () => (await import('/js/themes.js?smoke-failure')).setTheme('dark')") is True
                    print('PASS: JSON theme appearance, latest-choice race protection, failed-load fallback and retry.', flush=True)
                    assert not errors, errors
                    browser.close()
                    print('PASS: setup, 14 views, real note persistence, escaped content, mobile navigation, logout/login; no page errors.', flush=True)
            finally:
                if os.name == 'nt':
                    subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
                else:
                    process.terminate()
                process.wait(timeout=10)
    # On Windows the venv launcher can exit before its interpreter child releases dashboard.db,
    # so the data directory may outlive the context manager; remove it once the handle closes.
    for _ in range(40):
        if not os.path.exists(folder):
            break
        shutil.rmtree(folder, ignore_errors=True)
        time.sleep(.25)
    else:
        print(f'NOTE: temporary data directory could not be removed: {folder}', flush=True)


if __name__ == '__main__':
    main()
