import asyncio
from types import SimpleNamespace

import pytest

from web_search_neo import browser_tools as b, main


def test_public_script_default_is_single_shot():
    schema = asyncio.run(main.web_info('action_schema', {'action': 'run_script'}))
    assert schema['input_schema']['properties']['retry_on_uncaught']['default'] is False


@pytest.mark.parametrize('value', ['false', 1, None])
def test_full_schemas_refuses_ambiguous_boolean(value):
    with pytest.raises(ValueError, match='boolean'):
        asyncio.run(main.web_info('capabilities', {'full_schemas': value}))


@pytest.mark.parametrize('inventory', [None, ['Runtime.evaluate']])
def test_status_distinguishes_live_and_shipped_capabilities(monkeypatch, inventory):
    browser = {'extension_version': b.expected_extension_version()}
    if inventory is not None:
        browser['allowed_cdp_methods'] = inventory
    monkeypatch.setattr(b, 'get_chrome_bridge', lambda: SimpleNamespace(
        status=lambda *_: {'connected': True, 'browser': browser}))
    status = b._companion_status()
    assert status['cdp_capabilities_verified'] is (inventory is not None)
    if inventory is None:
        assert status['live_allowed_cdp_methods'] is None
        assert status['missing_cdp_methods'] is None
    else:
        assert 'Page.reload' in status['missing_cdp_methods']
        assert status['outdated'] is True
