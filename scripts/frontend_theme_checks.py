"""Verify actual persisted themes, contrast and shared icons in a local browser."""
import colorsys
import re
from pathlib import Path
from playwright.sync_api import Page, expect


def contrast(foreground: str, background: str) -> float:
    def luminance(value):
        channels = [int(n) / 255 for n in re.findall(r'\d+', value)[:3]]
        linear = [v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4 for v in channels]
        return sum(v * w for v, w in zip(linear, [.2126, .7152, .0722]))
    a, b = sorted([luminance(foreground), luminance(background)])
    return (b + .05) / (a + .05)


def danger_hue(value: str) -> bool:
    """True for a saturated red/rose/coral colour, the family reserved for failures."""
    hue, _, saturation = colorsys.rgb_to_hls(*(int(n) / 255 for n in re.findall(r'\d+', value)[:3]))
    return saturation >= .25 and (hue * 360 >= 330 or hue * 360 <= 30)


def exercise_themes(page: Page, artifacts: Path) -> None:
    for path in ['/', '/js/app.js', '/js/lib/html.js', '/css/components.css', '/themes/dark.json']:
        response = page.request.get(page.url.split("#")[0].rstrip("/") + path)
        assert response.status == 200
        assert response.headers.get('cache-control') == 'no-store', path
    page.evaluate("location.hash = 'settings'")
    page.get_by_role('button', name='Save preferences', exact=True).wait_for()
    page.get_by_label('Theme', exact=True).first.select_option('n8n')
    page.get_by_role('button', name='Save preferences', exact=True).click()
    expect(page.locator('html')).to_have_attribute('data-theme', 'n8n')
    expect(page.get_by_label('Color theme', exact=True)).to_have_value('n8n')
    expect(page.get_by_role('button', name='Save preferences', exact=True).locator('svg')).to_have_count(1)
    page.get_by_role('button', name='Save preferences', exact=True).click()
    expect(page.get_by_text('Preferences saved.', exact=True)).to_have_count(1)
    page.screenshot(path=str(artifacts / 'settings-fixed.png'), full_page=True)
    page.reload(wait_until='domcontentloaded')
    expect(page.locator('html')).to_have_attribute('data-theme', 'n8n')
    expect(page.get_by_label('Theme', exact=True).first).to_have_value('n8n')
    assert page.evaluate('getComputedStyle(document.documentElement).colorScheme') == 'dark'
    for theme in ['light', 'dark', 'n8n']:
        assert page.evaluate("async theme => (await import('/js/themes.js')).setTheme(theme)", theme)
        colors = page.evaluate('''() => {
            const host = document.createElement('div');
            document.body.append(host);
            const pairs = [['var(--color-graphite)','var(--color-ash-mist)'],
                ['var(--color-smoke)','var(--surface-card-surface)'],
                ['var(--action-text)','var(--action-fill)'],
                ['var(--selection-text)','var(--selection-fill)']];
            const result = pairs.map(([fg,bg]) => {
                host.style.color=fg;host.style.backgroundColor=bg;
                const s=getComputedStyle(host);return [s.color,s.backgroundColor];
            });host.remove();return result;
        }''')
        assert all(contrast(*pair) >= 4.5 for pair in colors), (theme, colors)
        # Informational notices (e.g. "Preferences saved.") must stay out of the danger hue family.
        info = page.evaluate('''async () => {
            const {notice} = await import('/js/lib/html.js');
            const host = document.createElement('div');
            document.body.append(host);
            const s = getComputedStyle(notice(host, 'Theme notice check.'));
            const result = [s.color, s.backgroundColor];
            host.remove(); return result;
        }''')
        assert contrast(*info) >= 4.5, (theme, info)
        assert not any(danger_hue(value) for value in info), (theme, info)
        # Waterfall bars stay visible on their track; failed spans use a distinct error fill (plus a text marker in observe.js).
        waterfall = page.evaluate('''() => {
            const track = document.createElement('div'); track.className = 'waterfall-track';
            const bar = document.createElement('div'); bar.className = 'waterfall-bar';
            const failed = document.createElement('div'); failed.className = 'waterfall-bar failed';
            track.append(bar, failed); document.body.append(track);
            const result = {track:getComputedStyle(track).backgroundColor, bar:getComputedStyle(bar).backgroundColor,
                failed:getComputedStyle(failed).backgroundColor};
            track.remove(); return result;
        }''')
        assert contrast(waterfall['bar'], waterfall['track']) >= 3, (theme, waterfall)
        assert contrast(waterfall['failed'], waterfall['track']) >= 3, (theme, waterfall)
        assert danger_hue(waterfall['failed']) and not danger_hue(waterfall['bar']), (theme, waterfall)
    page.evaluate("location.hash = 'assistant'")
    page.locator('.assistant-chat').wait_for()
    expect(page.locator('.page-heading h1')).to_have_text('AI copilot')
    expect(page.get_by_role('button', name='New conversation', exact=True).locator('svg')).to_have_count(1)
    expect(page.locator('.page-heading svg')).to_have_attribute('aria-hidden', 'true')
    page.evaluate('scrollTo(0,0)')
    page.screenshot(path=str(artifacts / 'n8n-theme-desktop.png'), full_page=True)
    page.set_viewport_size({'width':390, 'height':844})
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.screenshot(path=str(artifacts / 'n8n-theme-mobile.png'), full_page=True)
    page.set_viewport_size({'width':1440, 'height':1000})
    page.route('**/themes/n8n.json', lambda route: route.fulfill(json={'id':'n8n', 'tokens':{}}))
    try:
        assert page.evaluate("async () => (await import('/js/themes.js?invalid-n8n')).setTheme('n8n')") is False
        expect(page.locator('html')).to_have_attribute('data-theme', 'light')
        assert page.evaluate('getComputedStyle(document.body).backgroundColor') == 'rgb(245, 247, 249)'
    finally:
        page.unroute('**/themes/n8n.json')
    page.get_by_label('Color theme', exact=True).select_option('light')
    expect(page.locator('html')).to_have_attribute('data-theme', 'light')
    print('PASS: n8n theme persisted via Settings/reload, header selector, 3-theme contrast, neutral info notices, failed-span pattern, semantic icons, mobile layout and invalid-theme fallback.', flush=True)
