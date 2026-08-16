"""End-to-end: exercise the REAL _submit_landed / _real_click_create_button /
_log_form_state methods on a simulated register form.

Case A: a real submit (form unmounts + hCaptcha CHALLENGE frame spawns).
Case B: an inert button (the "isn't clicking" case) - must NOT report landed.
Case C: a PRE-EXISTING hCaptcha WIDGET iframe (Discord mounts it with the
        form before any click) - must NOT report landed on its own, or the
        bot skips to the captcha phase while the form was never submitted.
"""
import asyncio

from browser_engine import async_playwright
from server import DiscordAutomation

OK_HTML = """<html><body>
<form id='f'>
  <input name='email' type='email' value='ok@example.com'>
  <button type='button' onclick="var f=document.getElementById('f');if(f)f.remove();var i=document.createElement('iframe');i.src='https://newassets.hcaptcha.com/captcha/v1/abc/hcaptcha-challenge.html';document.body.appendChild(i)">Create Account</button>
</form></body></html>"""

BLOCKED_HTML = """<html><body>
<form id='f'>
  <input name='email' type='email' value='bad@example.combad@example.com'>
  <div class='error_abc'>Format der E-Mail-Adresse ist ungültig</div>
  <button type='button'>Create Account</button>
</form></body></html>"""

# Widget iframe present WITH the form, button does nothing (the bug case).
WIDGET_HTML = """<html><body>
<form id='f'>
  <input name='email' type='email' value='ok@example.com'>
  <iframe src='https://newassets.hcaptcha.com/captcha/v1/frame.html' title='Widget containing checkbox for hCaptcha security challenge'></iframe>
  <button type='button'>Create Account</button>
</form></body></html>"""


def data_url(html: str) -> str:
    return ("data:text/html," + html.replace(" ", "%20").replace("<", "%3C")
            .replace(">", "%3E").replace('"', "%22").replace("'", "%27"))


async def check_landed(bot, label):
    before = await bot._submit_landed(timeout=2.0)
    print(f"{label} before click, landed:", repr(before))
    clicked = await bot._real_click_create_button()
    print(f"{label} real click sent:", clicked)
    await asyncio.sleep(1.0)
    after = await bot._submit_landed(timeout=2.0)
    print(f"{label} after click, landed:", repr(after))
    return before, after


async def main():
    pw = await async_playwright().start()
    b = await pw.chromium.launch(headless=True)
    ctx = await b.new_context()
    page = await ctx.new_page()
    bot = DiscordAutomation(headless=True)
    bot._page = page

    # ── Case A: real submit lands (form gone + challenge frame) ──
    await page.goto(data_url(OK_HTML))
    await asyncio.sleep(0.5)
    before, after = await check_landed(bot, "A")
    assert "captcha" in after, "real submit should be detected as landed"

    # ── Case B: inert form (the 'isn't clicking' case) ──
    await page.goto(data_url(BLOCKED_HTML))
    await asyncio.sleep(0.5)
    before, after = await check_landed(bot, "B")
    assert after == "", "inert form must NOT report a landed submit"
    print("B4 form-state dump:")
    await bot._log_form_state("test (blocked)")

    # ── Case C: pre-existing widget iframe + unsent form ──
    await page.goto(data_url(WIDGET_HTML))
    await asyncio.sleep(0.5)
    before, after = await check_landed(bot, "C")
    assert before == "", "pre-existing widget must not be 'landed' before the click"
    assert after == "", "pre-existing widget alone must not report a landed submit"

    print("SUBMIT TEST PASSED")
    await b.close()


asyncio.run(main())
