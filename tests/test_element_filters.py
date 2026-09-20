import asyncio

import pytest

from web_search_neo import browser_tools, main


def test_bad_arguments_explain_exact_recovery_without_running_browser():
    with pytest.raises(ValueError, match='function body'):
        asyncio.run(main.web_info('execute_js', {'code': 'document.title'}))
    with pytest.raises(ValueError, match='fields='):
        main._validate_arguments('browser_fill_fields', 'fill', {'fields': ['wrong']})
    schema = asyncio.run(main.web_info('action_schema', {'action': 'page_elements'}))
    assert 'interactive' in schema['params_schema']['properties']['category']['enum']


def test_interactive_filters_before_pagination_and_budget(local_site):
    sid = 'filter-regression'
    browser_tools.open_page(local_site.base_url, session_id=sid, headless=True, profile_mode='temporary')
    try:
        driver = browser_tools._get_session(sid).driver
        driver.execute_script('''
          document.body.innerHTML = '<p>noise</p>' + '<button>Other</button>'.repeat(12)
            + '<button id="first" aria-label="Save first"><span>Sa</span><span>ve</span></button>'
            + '<button id="hidden" style="display:none">Save hidden</button>'
            + '<button id="disabled" disabled>Save disabled</button>'
            + '<div role="button" aria-label="Save second" id="second" tabindex="0"></div>'
            + '<input id="email" placeholder="Email address">'
            + '<div contenteditable="true" aria-label="Message"></div>';
        ''')
        args = dict(session_id=sid, category='interactive', visible_only=True,
                    enabled_only=True, text_pattern='Save', limit=1)
        first = browser_tools.get_page_elements(**args)
        assert first['found']['interactive'] == 2
        assert first['interactive'][0]['selector'] == '#first'
        assert first['range']['interactive']['next_offset'] == 1
        second = browser_tools.get_page_elements(**args, offset=1)
        assert second['interactive'][0]['selector'] == '#second'
        assert second['range']['interactive']['next_offset'] is None
        fields = browser_tools.get_page_elements(session_id=sid, category='interactive', role='textbox')
        assert any(row['selector'] == '#email' for row in fields['interactive'])
        buttons = browser_tools.get_page_elements(session_id=sid, category='buttons', text_pattern='Save first', limit=1)
        assert buttons['found']['buttons'] == 1
        assert buttons['buttons'][0]['selector'] == '#first'
    finally:
        browser_tools.close_session(sid)
