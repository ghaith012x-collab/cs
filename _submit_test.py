"""End-to-end: exercise the REAL _submit_landed / _real_click_create_button /
_log_form_state methods on a simulated register form (one that submits by
adding a captcha iframe, one that is inert = the 'isn't clicking' case).
"""
import asyncio

from browser_engine import async_playwright
from server import DiscordAutomation

OK_HTML = """<html><body>
<form id='f'>
  <input name='email' type='email' value='ok@example.com'>
  <button type='button' onclick="var i=document.createElement('iframe');i.src='https://newassets.hcaptcha.com/captcha';document.body.appendChild(i)">Create Account</button>
</form></body></html>"""

BLOCKED_HTML = """<html><body>
<form id='f'>
  <input name='email' type='email' value='bad@example.combad@example.com'>
  <div class='error_abc'>Format der E-Mail-Adresse ist ungültig</div>
  <button type='button'>Create Account</button>
</form></body></html>"""


def data_url(html: str) -> str:
    return ("data:text/html," + html.replace(" ", "%20").replace("<", "%3C")
            .replace(">", "%3E").replace('"', "%22").replace("'", "%27"))


async def main():
    pw = await async_playwright().start()
    b = await pw.chromium.launch(headless=True)
    ctx = await b.new_context()
    page = await ctx.new_page()
    bot = DiscordAutomation(headless=True)
    bot._page = page

    # ── Case A: real click lands the submit (captcha iframe appears) ──
    await page.goto(data_url(OK_HTML))
    await asyncio.sleep(0.5)
    before = await bot._submit_landed(timeout=2.0)
    print("A1 before click, landed:", repr(before))
    clicked = await bot._real_click_create_button()
    print("A2 real click sent:", clicked)
    await asyncio.sleep(1.0)
    after = await bot._submit_landed(timeout=2.0)
    print("A3 after real click, landed:", repr(after))
    assert "captcha" in after, "submit should have landed after the real click"

    # ── Case B: inert button (the 'isn't clicking' case) ──
    await page.goto(data_url(BLOCKED_HTML))
    await asyncio.sleep(0.5)
    before = await bot._submit_landed(timeout=2.0)
    print("B1 before click, landed:", repr(before))
    clicked = await bot._real_click_create_button()
    print("B2 real click sent:", clicked)
    await asyncio.sleep(0.8)
    after = await bot._submit_landed(timeout=2.0)
    print("B3 after click, landed:", repr(after))
    assert after == "", "inert form must NOT report a landed submit"
    print("B4 form-state dump:")
    await bot._log_form_state("test (blocked)")

    print("SUBMIT TEST PASSED")
    await b.close()


asyncio.run(main())
