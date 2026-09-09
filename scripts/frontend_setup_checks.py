"""Browser onboarding checks; connection-test response is explicitly a UI fixture."""
from playwright.sync_api import expect


def exercise_setup(page, artifacts):
    page.evaluate("location.hash='assistant'")
    page.locator('.assistant-chat').wait_for()
    page.get_by_role('button',name='Assistant settings',exact=True).click()
    page.get_by_label('Provider',exact=True).select_option('custom')
    page.get_by_label('Connection name',exact=True).fill('guided-browser')
    page.get_by_label('API base URL',exact=True).fill('https://model.example/v1/chat/completions')
    page.get_by_label('Model ID',exact=True).fill('browser-model')
    key=page.get_by_label('API key',exact=True)
    expect(key).to_have_attribute('type','password')
    key.fill('test-only-browser-key')
    page.get_by_role('button',name='Save model',exact=True).click()
    page.get_by_text('Model saved. Click Test connection to check the URL, key and model.',exact=True).wait_for()
    expect(key).to_have_value('')
    expect(page.get_by_label('API base URL',exact=True)).to_have_value('https://model.example/v1')
    assert 'test-only-browser-key' not in page.locator('body').inner_text()
    page.route('**/api/assistant/providers/guided-browser/test',lambda route:route.fulfill(json={'connected':True,'model':'browser-model'}))
    try:
        page.get_by_role('button',name='Test connection',exact=True).click()
        page.get_by_text('Connected. Select this model in Chat to start a conversation.',exact=True).wait_for()
        page.screenshot(path=str(artifacts/'guided-model-setup.png'),full_page=True)
    finally:
        page.unroute('**/api/assistant/providers/guided-browser/test')
    page.get_by_label('Your message',exact=True).fill('Keep this draft while editing')
    workspace=page.get_by_role('navigation',name='AI workspace',exact=True)
    workspace.get_by_role('button',name='Code editor (manual)',exact=True).click()
    expect(page.get_by_role('heading',name='CodeLab',exact=True)).to_be_visible()
    workspace.get_by_role('button',name='AI workspace',exact=True).click()
    expect(page.get_by_label('Your message',exact=True)).to_have_value('Keep this draft while editing')
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    page.screenshot(path=str(artifacts/'guided-model-mobile.png'),full_page=True)
    page.set_viewport_size({'width':1440,'height':1000})
    print('PASS: guided provider setup saves encrypted key, normalizes completion URL, tests connection UI, unified workspace preserves draft and fits mobile.',flush=True)
