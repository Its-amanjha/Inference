"""Browser administration/backup checks against isolated real application data."""
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import re
import secrets
import sqlite3
import sys
import tempfile

from playwright.sync_api import expect


def exercise_admin(page):
    page.evaluate("location.hash = 'settings'")
    page.get_by_role('button', name='Add user', exact=True).click()
    page.get_by_label('Username', exact=True).fill('browser_viewer')
    page.get_by_label('Password', exact=True).fill(secrets.token_urlsafe(24))
    page.get_by_role('group', name='Role', exact=True).get_by_role('button', name='Viewer', exact=True).click()
    page.get_by_role('button', name='Save user', exact=True).click()
    expect(page.get_by_role('cell', name='browser_viewer', exact=True)).to_have_count(1)
    row = page.get_by_role('row').filter(has=page.get_by_role('cell', name='browser_viewer', exact=True))
    row.get_by_role('button', name='Edit user browser_viewer', exact=True).click()
    page.get_by_role('group', name='Role', exact=True).get_by_role('button', name='Operator', exact=True).click()
    page.get_by_role('button', name='Save user', exact=True).click()
    expect(row.get_by_role('cell', name='Operator', exact=True)).to_have_count(1)
    preferences = page.locator('section').filter(has=page.get_by_role('heading', name='Your preferences', exact=True))
    preferences.get_by_role('group', name='Rows per list', exact=True).get_by_role('button', name='10', exact=True).click()
    preferences.get_by_label('Error notifications', exact=True).uncheck()
    preferences.get_by_role('button', name='Save preferences', exact=True).click()
    page.get_by_text('Preferences saved.', exact=True).wait_for()
    page.reload(wait_until='domcontentloaded')
    expect(preferences.get_by_role('group', name='Rows per list', exact=True).get_by_role('button', name='10', exact=True)).to_have_attribute('aria-pressed', 'true')
    expect(preferences.get_by_label('Error notifications', exact=True)).not_to_be_checked()
    page.evaluate("document.querySelector('#toasts').replaceChildren(); window.smokeSocket.onmessage({data:JSON.stringify({event:'errors.changed'})})")
    expect(page.locator('#toasts .toast')).to_have_count(0)
    page.evaluate("window.smokeSocket.onmessage({data:JSON.stringify({event:'backups.changed'})})")
    expect(page.get_by_text('Backup history updated.', exact=True)).to_have_count(1)
    with page.expect_request(lambda request: '/api/errors?' in request.url) as errors_request:
        page.evaluate("location.hash = 'errors'")
    assert 'limit=10' in errors_request.value.url
    with page.expect_request(lambda request: '/api/otel/traces?' in request.url) as traces_request:
        page.evaluate("location.hash = 'observe'")
    assert 'limit=10' in traces_request.value.url
    page.evaluate("location.hash = 'settings'")
    preferences.get_by_label('Error notifications', exact=True).check()
    preferences.get_by_role('button', name='Save preferences', exact=True).click()
    page.get_by_text('Preferences saved.', exact=True).wait_for()
    page.evaluate("window.smokeSocket.onmessage({data:JSON.stringify({event:'errors.changed'})})")
    expect(page.get_by_text('Execution errors updated.', exact=True)).to_have_count(1)

    # AI prices: a price of your own is listed as Yours, a malformed number never reaches the server,
    # and removing the price takes the model off the list.
    prices = page.locator('section.price-book')
    prices.get_by_role('button', name='Add your price', exact=True).click()
    prices.get_by_label('Model ID', exact=True).fill('browser-house-model')
    prices.get_by_label('Input per 1M tokens (USD)', exact=True).fill('0,5')
    prices.get_by_label('Output per 1M tokens (USD)', exact=True).fill('1.5')
    prices.get_by_role('button', name='Save price', exact=True).click()
    assert prices.get_by_label('Input per 1M tokens (USD)', exact=True).evaluate('node => node.validity.valid') is False
    prices.get_by_label('Input per 1M tokens (USD)', exact=True).fill('0.5')
    prices.get_by_role('button', name='Save price', exact=True).click()
    prices.get_by_text('Price saved. New AI steps with this model are priced with it.', exact=True).wait_for()
    priced = prices.get_by_role('row').filter(has=page.get_by_role('cell', name='browser-house-model', exact=True))
    expect(priced.get_by_role('cell', name='Yours', exact=True)).to_have_count(1)
    priced.get_by_role('button', name='Price actions for browser-house-model', exact=True).click()
    page.get_by_role('menu', name='Price actions for browser-house-model').get_by_role('menuitem', name='Remove your price', exact=True).click()
    prices.get_by_text('Your price for browser-house-model is removed.', exact=True).wait_for()
    expect(prices.get_by_role('cell', name='browser-house-model', exact=True)).to_have_count(0)

    passphrase = secrets.token_urlsafe(24)
    page.evaluate("location.hash = 'secrets'")
    # One verb throughout: the page's Add secret opens the form, whose submit is also Add secret.
    page.get_by_role('button', name='Add secret', exact=True).click()
    page.get_by_label('Vault name', exact=True).fill('BROWSER_BACKUP')
    page.get_by_label('New secret value', exact=True).fill(passphrase)
    page.get_by_role('button', name='Add secret', exact=True).click()
    page.get_by_text('Secret saved.', exact=True).wait_for()
    expect(page.get_by_text('$BROWSER_BACKUP', exact=True)).to_have_count(1)
    assert passphrase not in page.locator('body').inner_text()

    page.evaluate("location.hash = 'backup'")
    page.get_by_role('button', name='Add schedule', exact=True).click()
    page.get_by_label('Name', exact=True).fill('Browser recovery copy')
    page.get_by_role('button', name=re.compile(r'^Frequency:')).click()
    page.get_by_role('listbox', name='Frequency').get_by_role('option', name='Daily', exact=True).click()
    page.get_by_label('Export passphrase reference', exact=True).fill('$BROWSER_BACKUP')
    page.get_by_label('Enabled', exact=True).uncheck()
    page.get_by_role('button', name='Save schedule', exact=True).click()
    page.get_by_role('heading', name='Browser recovery copy', exact=True).wait_for()
    expect(page.get_by_role('button', name=re.compile(r'^Run now'))).to_have_count(0)
    expect(page.get_by_role('button', name='Enable schedule Browser recovery copy', exact=True)).to_have_count(1)
    page.get_by_role('button', name='Edit schedule Browser recovery copy', exact=True).click()
    page.get_by_label('Enabled', exact=True).check()
    page.get_by_role('button', name='Save schedule', exact=True).click()
    with page.expect_response(lambda response: '/api/backups/schedules/' in response.url and response.url.endswith('/run')) as run:
        page.get_by_role('button', name='Run now: Browser recovery copy', exact=True).click()
    assert run.value.status == 200, run.value.text()
    assert run.value.json()['status'] == 'succeeded', run.value.text()
    page.get_by_text('Backup outcome: Succeeded.', exact=True).wait_for()
    run = page.get_by_role('row').filter(has=page.get_by_role('cell', name='Succeeded', exact=True))
    expect(run.get_by_role('button', name=re.compile(r'^Download backup from '))).to_have_count(1)
    with page.expect_download() as downloaded:
        run.get_by_role('button', name=re.compile(r'^Download backup from ')).click()
    assert downloaded.value.failure() is None

    # Destructive schedule actions sit in the row's More actions menu and confirm in a dialog naming the target.
    schedule = page.locator('section.card').filter(has=page.get_by_role('heading', name='Browser recovery copy', exact=True))
    schedule.get_by_role('button', name='More actions for Browser recovery copy', exact=True).click()
    page.get_by_role('menu', name='More actions for Browser recovery copy').get_by_role('menuitem', name='Delete schedule', exact=True).click()
    dialog = page.get_by_role('dialog', name='Delete schedule')
    expect(dialog.get_by_text('Browser recovery copy', exact=True)).to_have_count(1)
    expect(dialog.get_by_role('button', name='Cancel', exact=True)).to_be_focused()
    dialog.get_by_role('button', name='Cancel', exact=True).click()
    expect(page.get_by_role('dialog')).to_have_count(0)
    expect(page.get_by_role('heading', name='Browser recovery copy', exact=True)).to_have_count(1)

    # Recover the browser-downloaded archive into a fresh temporary destination.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from backend.modules.backups.restore import restore_archive
    with tempfile.TemporaryDirectory(prefix='inference-browser-restore-') as folder:
        archive = Path(folder) / 'download.ibackup'
        downloaded.value.save_as(archive)
        data = archive.read_bytes()
        assert passphrase.encode() not in data
        destination = Path(folder) / 'restored'
        # Playwright owns an event loop; offline restore owns a separate one.
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(restore_archive, data, passphrase, destination).result(timeout=30)
        with closing(sqlite3.connect(destination / 'dashboard.db')) as connection:
            assert connection.execute("SELECT role FROM users WHERE username='browser_viewer'").fetchone() == ('operator',)
        assert (destination / 'notes' / 'Smoke.md').is_file()
    print('PASS: browser user creation/role edit, AI price add/validate/remove, encrypted secret, pause/enable schedule, manual run/download, named delete-schedule confirmation cancel, and offline restore of downloaded archive.', flush=True)
