"""Real Chromium regression for the human-visible cursor, through web_action."""
import asyncio
import time

from web_search_neo import browser_tools, main


def test_ordinary_click_and_pointer_show_a_cursor_in_real_chromium(local_site, tmp_path):
    session_id = "presence-browser-regression"
    try:
        browser_tools.open_page(local_site.base_url, session_id=session_id,
                                profile_mode="temporary", headless=True)
        session = browser_tools._get_session(session_id)
        # Exercise visible-mode overlays in an isolated headless browser so CI
        # does not steal the user's desktop or require an extension installation.
        session.headless = False
        driver = session.driver
        driver.execute_script("""
          document.body.innerHTML = '<h1>Virtual cursor check</h1><button id="go">Continue</button>';
          document.body.style.cssText = 'margin:60px;font:20px system-ui;background:#101827;color:#eef';
          document.querySelector('#go').style.cssText = 'margin:40px;width:220px;height:70px';
          document.querySelector('#go').onclick = function () { this.remove(); };
        """)
        point = driver.execute_script("const r=document.querySelector('#go').getBoundingClientRect();return {x:r.x+r.width/2,y:r.y+r.height/2}")
        result = asyncio.run(main.web_action([{
            "action": "click", "selector": "#go", "session_id": session_id, "wait_seconds": 0,
        }]))
        assert result["success"], result
        time.sleep(.15)

        def cursor_state():
            return driver.execute_script("""
              const c=document.querySelector('[data-wsn-presence="cursor"]');
              return c ? {display:getComputedStyle(c).display,x:parseFloat(c.style.left),
                y:parseFloat(c.style.top),arrow:!!c.shadowRoot.querySelector('svg'),
                hit:document.elementFromPoint(parseFloat(c.style.left),parseFloat(c.style.top))===c} : null;
            """)

        cursor = cursor_state()
        assert cursor and cursor["display"] == "block" and cursor["arrow"]
        assert abs(cursor["x"] - point["x"]) <= 1 and abs(cursor["y"] - point["y"]) <= 1
        assert not cursor["hit"], "the decorative cursor must not intercept page input"
        driver.save_screenshot(str(tmp_path / "cursor-after-dom-click.png"))

        browser_tools.screenshot(session_id)
        assert cursor_state()["display"] == "block", "capture must restore the human-visible cursor"
        driver.execute_script("document.querySelector('[data-wsn-presence=cursor]').remove()")
        result = asyncio.run(main.web_action([{
            "action": "pointer", "pointer_action": "click", "x": 330, "y": 210,
            "session_id": session_id, "include_summary": False,
        }]))
        assert result["success"], result
        cursor = cursor_state()
        assert cursor and cursor["x"] == 330 and cursor["y"] == 210
        assert driver.execute_script("return document.querySelectorAll('[data-wsn-presence=ripple]').length") > 0
        driver.execute_script("""
          const unrelated=document.createElement('button'); unrelated.id='same';
          unrelated.textContent='Main document'; document.body.appendChild(unrelated);
          const f=document.createElement('iframe'); f.id='frame';
          f.style.cssText='display:block;width:400px;height:200px;margin:40px;border:0';
          f.srcdoc='<button id="same" style="margin:35px;width:140px;height:50px">Frame target</button>';
          document.body.appendChild(f);
        """)
        time.sleep(.15)
        expected = driver.execute_script("""
          const f=document.querySelector('#frame'), r=f.getBoundingClientRect();
          const b=f.contentDocument.querySelector('#same').getBoundingClientRect();
          return {x:r.left+b.left+b.width/2,y:r.top+b.top+b.height/2};
        """)
        result = asyncio.run(main.web_action([{
            "action": "click", "selector": "#same", "frame_selector": "#frame",
            "session_id": session_id, "wait_seconds": 0,
        }]))
        assert result["success"], result
        cursor = cursor_state()
        assert abs(cursor["x"] - expected["x"]) <= 1 and abs(cursor["y"] - expected["y"]) <= 1
    finally:
        browser_tools.close_session(session_id)
