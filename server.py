import asyncio
import base64
import json
import os
import random
import re
import socket
import time
from typing import Optional

from browser_engine import async_playwright, ENGINE

from captcha_solver import (
    NoCaptchaAI,
    extract_hcaptcha_sitekey,
    extract_hcaptcha_rqdata,
    extract_funcaptcha_task,
    read_hcaptcha_token,
    set_hcaptcha_token_on_page,
    solve_funcaptcha_pixels,
    solve_hcaptcha_accessibility,
)
from duckmail import TempMail


# ── Shared JS: robust login-link / back-to-login detection ──────────────
# Discord renders the "Already have an account?" control as a REAL
# <button type="submit"> inside the register form, and its label can carry
# non-breaking spaces / split spans — plain substring matching on raw
# textContent fails (that's exactly how runs end up on /login). This helper
# normalizes ALL whitespace (incl. \u00a0) across textContent + aria-label +
# title + value and tests the blacklist. Injected INSIDE each evaluate's
# arrow-function body (the engine wraps function-looking strings in parens
# and calls them, so a top-level const would be a syntax error).
_LOGIN_LINK_GUARD = r"""const __isLoginLink = (el) => {
    const raw = (el.textContent || '') + ' ' +
                (el.getAttribute('aria-label') || '') + ' ' +
                (el.getAttribute('title') || '') + ' ' +
                (el.value || '');
    const t = raw.toLowerCase().replace(/\s+/g, ' ').trim();
    return /(already|have an account|log ?in|sign ?in|signin|back to|forgot|login)/.test(t);
};"""

# ── Shared JS: find Discord's REQUIRED ToS checkbox (real controls only) ──
# Discord's register form has two checkboxes: the required Terms-of-Service
# agreement and an optional marketing/"email updates" box. Older code
# matched [class*="checkbox"], which ALSO hit styled container divs (double
# toggles) and the marketing box (the "wrong checkbox"). This targets ONLY
# real checkbox controls (native input / role=checkbox / data-state), skips
# the marketing box by its label, and returns the click point of the first
# unchecked ToS box (or null when none remains).
_TOS_TARGET_JS = r"""() => {
    const els = document.querySelectorAll(
        'input[type="checkbox"], [role="checkbox"], [data-state]');
    for (const cb of els) {
        if (cb.checked || cb.getAttribute('aria-checked') === 'true'
            || cb.getAttribute('data-state') === 'checked') continue;
        if (cb.offsetParent === null) continue;
        const r = cb.getBoundingClientRect();
        if (!r || r.width < 5 || r.height < 5) continue;
        let label = '';
        try {
            const lab = cb.closest('label') || (cb.id ? document.querySelector('label[for="' + CSS.escape(cb.id) + '"]') : null);
            if (lab) label = lab.innerText || '';
        } catch (e) {}
        if (!label) {
            try {
                const wrap = cb.closest('[class*="checkbox"]');
                if (wrap) label = wrap.innerText || '';
            } catch (e) {}
        }
        const low = label.toLowerCase().replace(/\s+/g, ' ').trim();
        // Skip the optional marketing / email-updates box — only the ToS
        // agreement is required to enable Continue.
        if (/email|marketing|updat|news|promotion|newsletter|exclusive|offers|subscribe/.test(low)) continue;
        return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
    }
    return null;
}"""


# ── Discord rate-limit phrases — rotate the proxy the moment these show ──
_RATE_LIMIT_KEYWORDS = (
    "the resource is being rate limited",
    "resource is being rate limited",
    "you are being rate limited",
    "rate limited",
    "ratelimited",
    "too many requests",
    "slowdown",
    "try again later",
)


# ── Discord register-page state ─────────────────────────────────────────
# The navigation poll reads the page through TWO independent channels:
#   1. JS evaluation (page.evaluate) — the engine falls back to the raw CDP
#      websocket when the reattached WebDriver session's JS context is stale
#      (the old white-screen bug: page loaded, title="Discord", but every
#      evaluate() returned None and the bot rotated a healthy session).
#   2. CDP DOM-presence checks (driver.cdp.is_element_present) — no JS
#      execution required at all.
_NAV_STATE_JS = r"""() => {
    const body = document.body;
    if (!body) return JSON.stringify({error: "no-body"});
    const text = body.innerText || "";
    const titleLow = (document.title || "").toLowerCase();
    const challenge = /just a moment|checking your browser|verify you are human|attention required/.test(titleLow + " " + text.toLowerCase().substring(0, 800)) || !!document.querySelector('iframe[src*="challenges.cloudflare.com"], #challenge-stage, #cf-challenge-running, #cf-chl');
    // Broad selectors — Discord uses aria-label, not name
    const email = document.querySelector('input[name="email"], input[type="email"], input[aria-label*="email" i], input[aria-label*="Email"], input[id*="email" i]');
    const username = document.querySelector('input[name="username"], input[aria-label*="username" i], input[aria-label*="display" i]');
    const password = document.querySelector('input[name="password"], input[type="password"], input[aria-label*="password" i]');
    const hasAge = /birthday|date of birth|born|how old/i.test(text.substring(0, 400));
    const hasMonth = document.querySelector('[class*="month" i], [aria-label*="month" i], select');
    const isLogin = /login|sign in|welcome back/i.test(text.substring(0, 400));
    const hasQR = document.querySelector('img[src*="qr" i], [class*="qr" i]');
    const continueBtn = document.querySelector('button[type="submit"], button[class*="continue" i]');
    return JSON.stringify({
        url: location.href,
        title: document.title || "",
        readyState: document.readyState || "",
        email: email !== null,
        username: username !== null,
        password: password !== null,
        ageGate: hasAge || hasMonth !== null,
        isLogin: isLogin,
        hasQR: hasQR,
        hasButton: continueBtn !== null,
        hasAppMount: document.querySelector("#app-mount") !== null,
        inputCount: document.querySelectorAll("input").length,
        buttonCount: document.querySelectorAll("button").length,
        cfClearance: document.cookie.indexOf("cf_clearance=") !== -1,
        challenge: challenge,
        textPreview: text.substring(0, 250)
    });
}"""

# CDP DOM-presence selectors used when JS evaluation is unavailable.
_CDP_NAV_SELECTORS = {
    "email": 'input[name="email"], input[type="email"], input[aria-label*="email" i], input[id*="email" i]',
    "username": 'input[name="username"], input[aria-label*="username" i], input[aria-label*="display" i]',
    "password": 'input[name="password"], input[type="password"], input[aria-label*="password" i]',
    "hasAppMount": "#app-mount",
    "challenge": 'iframe[src*="challenges.cloudflare.com"], #challenge-stage, #cf-challenge-running',
}


# ── Full-form readiness probe ─────────────────────────────────────────
# _goto_register() returns as soon as email+username (or the age gate) paint,
# but the SPA keeps hydrating: password, the three DOB dropdowns, ToS and the
# Continue button can appear a beat later. Filling before they exist — and
# before React has attached its value trackers — is what produced runs where
# the bot typed into a half-rendered page and the form ended up empty. This
# probe is the "is it actually all there yet?" gate _wait_for_form_ready polls.
_FORM_READY_JS = r"""() => {
    const vis = (el) => !!el && (el.offsetParent !== null || el.getClientRects().length > 0);
    const q = (sel) => document.querySelector(sel);
    const email = q('input[name="email"], input[type="email"], input[autocomplete="email"], input[aria-label*="email" i], input[placeholder*="email" i], input[id*="email" i]');
    const username = q('input[name="username"], input[autocomplete="username"], input[aria-label*="username" i], input[id*="username" i], input[placeholder*="username" i]');
    const password = q('input[name="password"], input[type="password"], input[autocomplete="new-password"], input[aria-label*="password" i]');
    // DOB controls: native <select> or React-Select combobox/container whose
    // label/placeholder/text mentions month/day/year in the page's locale
    // (Dutch "Dag/Maand/Jaar", French "Jour/Mois/Année", ...).
    const DOB_LABELS = __DOB_LABELS__;
    const seen = {};
    const controls = Array.from(document.querySelectorAll(
        'select, [role="combobox"], [role="listbox"], [class*="select" i], [class*="dropdown" i], [class*="control" i]'
    ));
    for (const el of controls) {
        if (!vis(el)) continue;
        const cls = typeof el.className === 'string' ? el.className : '';
        const acc = (cls + ' ' + (el.getAttribute('aria-label') || '') + ' ' +
                     (el.getAttribute('name') || '') + ' ' + (el.getAttribute('id') || '') + ' ' +
                     (el.getAttribute('placeholder') || '') + ' ' +
                     ((el.textContent || '').slice(0, 80))).toLowerCase();
        for (const key of Object.keys(DOB_LABELS)) {
            for (const al of DOB_LABELS[key]) {
                if (new RegExp('(^|[^a-z0-9])' + al + '([^a-z0-9]|$)').test(acc)) { seen[key] = true; break; }
            }
            if (seen[key]) break;
        }
    }
    const body = document.body ? document.body.innerText : '';
    return JSON.stringify({
        email: vis(email),
        username: vis(username),
        password: vis(password),
        dob: Object.keys(seen).length,
        dobText: /date of birth|birthday/i.test(body),
        inputs: document.querySelectorAll('input').length,
        buttons: document.querySelectorAll('button').length,
        readyState: document.readyState || '',
    });
}"""


# Robust DOB (Month/Day/Year) setter. Discord's DOB control has changed
# across builds: native <select>, a React-Select combobox, or a custom div.
# This targets the control BY LABEL (aria-label / name / id / placeholder /
# class) and sets the matching option directly — never "first N inputs",
# never tab-roulette, never typing into whatever happens to have focus.
_DOB_FALLBACK_JS = r"""
async () => {
    const LABEL = __LABEL__;
    const OPT = __OPT__;
    const DOB_LABELS = __DOB_LABELS__;
    const norm = (s) => (s == null ? '' : String(s)).replace(/\s+/g, ' ').trim();
    const low = (s) => norm(s).toLowerCase();
    const monthIndex = ['january','february','march','april','may','june',
        'july','august','september','october','november','december']
        .indexOf(low(OPT)) + 1;
    // Localized month names (Dutch "januari", "maart", French "janvier",
    // "mars", ...) resolve to their numeric index so the option match works
    // in whatever locale Discord is serving.
    const MONTH_ALIASES = {
        'januari':1,'janvier':1,'januar':1,'enero':1,'gennaio':1,
        'februari':2,'février':2,'fevrier':2,'februar':2,'febrero':2,'febbraio':2,
        'maart':3,'mars':3,'märz':3,'marzo':3,'março':3,'marzec':3,
        'april':4,'avril':4,'abril':4,
        'mei':5,'mai':5,'mayo':5,'maggio':5,'maj':5,
        'juni':6,'juin':6,'junio':6,'giugno':6,
        'juli':7,'juillet':7,'julio':7,'luglio':7,
        'augustus':8,'août':8,'aout':8,'agosto':8,
        'september':9,'septembre':9,'septiembre':9,'settembre':9,
        'oktober':10,'octobre':10,'octubre':10,'ottobre':10,
        'november':11,'novembre':11,'noviembre':11,
        'december':12,'décembre':12,'dezember':12,'diciembre':12,'dicembre':12,
    };
    const wantNum = monthIndex || MONTH_ALIASES[low(OPT)] || (parseInt(OPT, 10) || 0);
    const wantStr = low(OPT);
    const optionMatches = (text, value) => {
        const t = low(text || ''); const v = low(value || '');
        if (!t && !v) return false;
        if (t === wantStr) return true;
        if (MONTH_ALIASES[t] && MONTH_ALIASES[t] === wantNum) return true;
        if (!wantNum) return false;
        const n = String(wantNum);
        const p = n.length === 1 ? '0' + n : n;
        const z = wantNum > 1 ? String(wantNum - 1) : '0';
        const zp = z.length === 1 ? '0' + z : z;
        return t === n || v === n || t === p || v === p || t === z || v === z || t === zp || v === zp;
    };
    const labelHits = (el) => {
        const cls = (typeof el.className === 'string') ? el.className : '';
        const acc = norm(el.getAttribute('aria-label') || '') + ' ' +
                    norm(el.getAttribute('name') || '') + ' ' +
                    norm(el.getAttribute('id') || '') + ' ' +
                    norm(el.getAttribute('placeholder') || '') + ' ' +
                    norm(el.getAttribute('data-label') || '') + ' ' +
                    norm(cls);
        const a = low(acc);
        const labels = DOB_LABELS[LABEL] || [LABEL.toLowerCase()];
        for (const al of labels) {
            if (new RegExp('(^|[^a-z0-9])' + al + '([^a-z0-9]|$)').test(a)) return true;
        }
        return false;
    };
    let candidates = Array.from(document.querySelectorAll(
        'select, [role="combobox"], [role="listbox"], [class*="select" i], [class*="dropdown" i], [class*="control" i]'
    )).filter(labelHits);
    if (!candidates.length) {
        const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null);
        let node;
        const labels = DOB_LABELS[LABEL] || [LABEL.toLowerCase()];
        const re = new RegExp('(^|[^a-z0-9])(' + labels.join('|') + ')([^a-z0-9]|$)');
        while ((node = walker.nextNode())) {
            if (!re.test(low(norm(node.textContent)))) continue;
            let p = node.parentElement;
            for (let i = 0; p && i < 5; i++) {
                if (p.matches && p.matches('select, [role="combobox"], [class*="select" i], [class*="dropdown" i], [class*="control" i]')) {
                    candidates.push(p);
                    break;
                }
                p = p.parentElement;
            }
            if (candidates.length) break;
        }
    }
    for (const el of candidates) {
        const tag = el.tagName.toLowerCase();
        if (tag !== 'select' && el.offsetParent === null) continue;
        if (tag === 'select') {
            for (const opt of Array.from(el.options || [])) {
                if (optionMatches(opt.text || opt.label, opt.value)) {
                    el.value = opt.value;
                    el.dispatchEvent(new Event('change', { bubbles: true }));
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    return 'native:' + (opt.text || opt.value);
                }
            }
            continue;
        }
        el.scrollIntoView({ block: 'center' });
        el.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true }));
        el.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true }));
        el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
        for (let attempt = 0; attempt < 8; attempt++) {
            if (attempt > 0) await new Promise(r => setTimeout(r, 250));
            const opts = document.querySelectorAll('[role="option"], [id*="option" i], [class*="option" i], ul li');
            for (const opt of opts) {
                const t = norm(opt.textContent || opt.getAttribute('aria-label') || '');
                if (!t) continue;
                if (optionMatches(t, opt.getAttribute('data-value') || opt.getAttribute('value') || t)) {
                    opt.scrollIntoView({ block: 'nearest' });
                    opt.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
                    opt.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
                    opt.dispatchEvent(new MouseEvent('click', { bubbles: true }));
                    return 'combo:' + t;
                }
            }
        }
    }
    return 'not_found';
}
"""

# Locale-aware DOB dropdown labels — Discord localizes the register form to
# the proxy's region (e.g. Dutch "Dag/Maand/Jaar"), so label matching accepts
# the common spellings, not just English.
_DOB_LABEL_ALIASES = {
    "Month": ["month", "maand", "mois", "monat", "mes", "mês", "mese", "miesiąc", "månad", "måned", "měsíc"],
    "Day": ["day", "dag", "jour", "tag", "día", "dia", "giorno", "dzień"],
    "Year": ["year", "jaar", "an", "année", "annee", "jahr", "año", "ano", "anno", "rok", "år"],
}


# ── Log verbosity ─────────────────────────────────────────
# Normal mode prints ONLY the essential signup events listed below (plus
# warnings / errors, which always print). Everything else — proxy sweeps,
# nav polls, fingerprint rotations, captcha retries, mail polling — only
# appears in the ALL logs: run with LOG_LEVEL=all to see it.
_LOG_ALL = os.environ.get("LOG_LEVEL", "").strip().lower() \
    in ("all", "debug", "verbose")

_ESSENTIAL_PREFIXES = (
    "[Nav] Navigating to ",                 # navigating to Discord
    "Using configured email:",              # email in use
    "[Mail] No email configured",           # creating an inbox
    "[Mail] [OK]",                          # inbox ready / verification link
    "Email: ",                              # filled email field
    "Display: ",                            # filled username + password fields
    "[Form] ToS",                           # ToS checkbox clicked
    "[Form] All fields + ToS verified OK",
    "[Form] Form filled - checking for hCaptcha",
    "Clicking Create Account",
    "[OK] Account button clicked",
    "[OK] Create Account submitted",
    "[Captcha] Checking for hCaptcha",
    "[Captcha] Waiting for hCaptcha to load",
    "[Captcha] Checkbox clicked",           # auto-clicked the hCaptcha checkbox
    "[Captcha] Clicking hCaptcha checkbox", # about to click the widget checkbox
    "[Captcha] [READY]",                    # hCaptcha rendered
    "[Captcha] [OK]",
    "[Accessibility] Clicked 3-dots",
    "[Accessibility] Accessibility option clicked",
    "[Accessibility] NEW captcha detected",  # chained captcha re-trigger
    "[Accessibility] [OK]",                 # solved / no new challenge
    "[Accessibility] Q",                    # question answers — refined below
)


def _log_essential(message: str) -> bool:
    """True when the message is one of the essential signup events."""
    if not any(message.startswith(p) for p in _ESSENTIAL_PREFIXES):
        return False
    # A Q-line that isn't an actual answer (e.g. "Q1 skipped via JS click")
    if message.startswith("[Accessibility] Q") and " solved:" not in message:
        return False
    return True


# ── TOR Control ───────────────────────────────────────────

def _tor_newnym():
    """Signal TOR to switch to a new identity (fresh exit node)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(15)
        s.connect(("127.0.0.1", 9051))
        s.recv(1024)
        s.sendall(b"AUTHENTICATE\r\n")
        auth_resp = s.recv(1024).decode().strip()
        if "250" not in auth_resp:
            s.close()
            print(f"[TOR] auth failed: {auth_resp}", flush=True)
            return False
        s.sendall(b"SIGNAL NEWNYM\r\n")
        resp = s.recv(1024).decode().strip()
        s.close()
        if "250" in resp:
            time.sleep(3)
            return True
        print(f"[TOR] newnym rejected: {resp}", flush=True)
    except Exception as e:
        print(f"[TOR] newnym error: {e}", flush=True)
    return False


def _tor_check():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(2)
        s.connect(("127.0.0.1", 9050))
        s.close()
        return True
    except:
        return False


PAST_CAPTCHA_KEYWORDS = ['/channels', '/verify', '/welcome', '@me', 'discord.com/app']

_BIO_POOL = [
    "just vibing",
    "professional sleeper",
    "i like turtles",
    "certified yapper",
    "caffeine powered",
    "music > everything",
    "gamer for life",
    "casually existing",
    "be nice or leave",
    "no thoughts, only vibes",
]

import stealth
from stealth import (
    apply_cdp_stealth,
    build_context_options,
    build_init_script,
    launch_args,
)

# Secondary navigations (verification link, token page). Halved from 60s:
# these pages are light and a hang this long only wastes a dead-session slot.
NAV_TIMEOUT_MS = 30000


# ═══════════════════════════════════════════════════════════════
# Human Behavior Simulation & Fingerprint Randomization
# ═══════════════════════════════════════════════════════════════

async def human_type(page, selector: str, text: str):
    """Type with variable speed, occasional pauses, and rare backspaces.
    Human-like typing with Gaussian-distributed delays."""
    try:
        await page.click(selector)
    except Exception:
        pass
    for i, char in enumerate(text):
        # Base 40-110ms per char with Gaussian distribution (fast but varied)
        delay = max(18, random.gauss(75, 28)) / 1000.0
        if random.random() < 0.04:  # 4% chance of pause
            delay += random.gauss(600, 200) / 1000.0
        if random.random() < 0.02 and i > 0:  # 2% chance of backspace
            await page.keyboard.press("Backspace")
            await asyncio.sleep(random.gauss(150, 50) / 1000.0)
        await page.keyboard.type(char, delay=int(delay * 1000))
        await asyncio.sleep(delay)

async def human_mouse_move(page, x: int, y: int):
    """Bezier curve mouse movement instead of instant teleport.
    Creates a natural, curved mouse path between current position and target."""
    try:
        current = await page.evaluate("() => ({x: window.mouseX || 0, y: window.mouseY || 0})")
    except Exception:
        current = {'x': x - 100, 'y': y - 50}
    if not current or current.get('x') == 0:
        current = {'x': x - 100, 'y': y - 50}
    steps = random.randint(8, 20)
    for t in range(1, steps + 1):
        progress = t / steps
        # Quadratic bezier with random control point
        cp_x = (current['x'] + x) / 2 + random.randint(-30, 30)
        cp_y = (current['y'] + y) / 2 + random.randint(-20, 20)
        bx = (1-progress)**2 * current['x'] + 2*(1-progress)*progress * cp_x + progress**2 * x
        by = (1-progress)**2 * current['y'] + 2*(1-progress)*progress * cp_y + progress**2 * y
        await page.mouse.move(bx, by)
        await asyncio.sleep(random.gauss(0.008, 0.003))

class DiscordAutomation:
    def __init__(self, headless: bool = False, email: str = "",
                 proxy=None, worker_id: str = "B1", domain: str = "vibify.cc"):
        self.headless = headless
        self.worker_id = worker_id
        self._domain = (domain or "glasswhitehub.com").strip().lower() or "glasswhitehub.com"
        # proxy: dict {proto, host, port, username, password, key} or None
        self.proxy = proxy
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._ua = ""
        self._tor_enabled = False
        # True when the browser is running DIRECT (no proxy, no TOR) — the
        # last-resort transport when every residential session is dead and
        # TOR is unreachable. _build_context() honors it instead of raising.
        self._direct = False
        self._screenshots: list = []
        self._activity_log: list = []
        self._email = (email or os.environ.get("ACCOUNT_EMAIL", "")).strip()
        self._username = ""
        self._password = ""
        self._token = ""
        self._solver = NoCaptchaAI(log=self._log)
        # duckmail.sbs client — created once per bot, reused across attempts.
        # (Lost in the cybertemp→duckmail switch, which silently killed every
        # inbox creation with a NoneType crash — see git log efb6f99.)
        self._mail: Optional[TempMail] = TempMail(log=self._log)
        self._user_id = ""
        self._avatar_data = ""
        self._bio = ""
        self._humanized = False
        self._exit_ip = ""
        # Set when Discord asks for phone verification after account creation
        # — the worker then rotates proxy + fingerprint + mail domain and retries.
        self.phone_verify_detected = False
        # True once this session actually rendered Discord's register page
        # (used by the worker to distinguish dead sessions from soft failures).
        self._nav_ok = False
        # True when the mail provider failed BEFORE Discord even loaded — the
        # worker uses this to retry the same proxy/fingerprint instead of
        # rotating (mail failures are not IP problems).
        self._mail_failed = False
        # Human-readable reason the last _goto_register() returned False —
        # surfaced in the worker's per-attempt summary so every failure is
        # self-explanatory ("TOR circuit blocked: page unresponsive after 9s").
        self._nav_error: str = ""
        # Set by the app when the user hits Stop — aborts an in-flight
        # navigation wait immediately so Stop actually stops (the browser is
        # then PARKED on Discord and reused on the next Start).
        self._stopped = asyncio.Event()
        # Engine-owned identity: Camoufox mints a fresh randomized profile
        # per launch — there is no bot-side fingerprint to keep.
        self._fingerprint = {}

    def _log(self, message: str, level: str = "info") -> None:
        # The store keeps EVERYTHING so the dashboard's ALL LOGS toggle can
        # show the full detail; the console only prints essential events +
        # warnings/errors unless LOG_LEVEL=all.
        essential = level in ("warn", "error") or _log_essential(message)
        print_console = _LOG_ALL or essential
        tagged = f"[{self.worker_id}] {message}"
        entry = {
            "time": time.strftime("%H:%M:%S"),
            "timestamp": time.time(),
            "level": level,
            "essential": essential,
            "message": tagged
        }
        self._activity_log.append(entry)
        if len(self._activity_log) > 500:
            self._activity_log = self._activity_log[-500:]
        if print_console:
            print(f"[{entry['time']}] [{level.upper()}] {tagged}", flush=True)

    def _log_exception(self, message: str, exc: Exception) -> None:
        # Record the EXACT problem (exception class + full traceback) into
        # the activity log, so the dashboard's ALL LOGS toggle shows why a
        # step failed instead of only a stderr traceback it never sees.
        import traceback
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        self._log(f"{message} — {tb.rstrip()}", level="error")

    def get_activity_log(self) -> list:
        return self._activity_log

    def _launch_proxy(self) -> Optional[dict]:
        """The proxy rides on browser launch (Camoufox applies it at launch
        — a context-level proxy would either be ignored or rejected).
        Returns the Playwright-style {server, username, password} dict
        (or None for TOR/direct)."""
        if not (self.proxy and isinstance(self.proxy, dict)):
            return None
        p = self.proxy
        proto = p.get("proto", "http")
        lp = {"server": f"{proto}://{p.get('host')}:{p.get('port')}"}
        if p.get("username"):
            lp["username"] = p.get("username")
            lp["password"] = p.get("password", "")
        return lp

    async def _relaunch_browser(self) -> None:
        """Close and relaunch the browser bound to self.proxy. The engine
        cannot change a running browser's proxy (it is a launch flag), so a
        proxy change requires a full relaunch."""
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None
        args = launch_args(headless=self.headless)
        # The engine pins the proxy at browser launch. When there is no sticky
        # residential session, relaunch must ride TOR exactly like initialize()
        # does — otherwise switch_proxy(None) silently goes DIRECT (the
        # context-level proxy is ignored by the engine) and Discord's
        # Cloudflare blocks the datacenter IP with chrome-error.
        launch_proxy = self._launch_proxy()
        if launch_proxy is None and not self._direct and _tor_check():
            launch_proxy = {"server": "socks5://127.0.0.1:9050"}
            self._tor_enabled = True
        self._browser = await self._playwright.chromium.launch(
            headless=self.headless, args=args, proxy=launch_proxy)
        await self._build_context()

    _PROXY_IP_CACHE: dict = {}

    def _resolve_proxy_ip(self, host: str) -> str:
        """DNS-resolve the proxy host to an IP (best-effort, cached per host).
        Runs in a worker thread — never blocks the event loop."""
        if not host:
            return ""
        if host in self._PROXY_IP_CACHE:
            return self._PROXY_IP_CACHE[host]
        try:
            ip = socket.gethostbyname(host)
        except Exception:
            return ""
        if ip:
            self._PROXY_IP_CACHE[host] = ip
        return ip

    async def _log_proxy_exit_ip(self) -> None:
        """Best-effort: report the REAL egress IP of the current browser
        session (residential exit != gateway DNS IP). Bounded, never blocks."""
        page = self._page
        if page is None:
            return
        try:
            ip = await asyncio.wait_for(page.evaluate(
                "async () => { try { "
                "const r = await fetch('https://api.ipify.org?format=json', "
                "{cache: 'no-store'}); "
                "const d = await r.json(); return d.ip || ''; } "
                "catch (e) { return ''; } }"
            ), timeout=6)
        except Exception:
            return
        if ip:
            self._exit_ip = ip  # captured for the persistent proxy store
            label = "proxy session" if self.proxy else "TOR circuit"
            self._log(f"[Proxy] Exit IP ({label}): {ip}")

    async def _discover_vision_model(self) -> None:
        """Auto-arm the qwen3-vl image solver when OLLAMA_VISION_MODEL is
        unset: query the Ollama server for a vision-capable model and set
        the env var the captcha solver reads. Drag/silhouette/image-pick
        challenges then get solved with exact pixel coordinates instead of
        being skipped. Preference: qwen3-vl → qwen2.5vl → qwen2-vl →
        moondream → llava → minicpm-v → bakllava."""
        if (os.environ.get("OLLAMA_VISION_MODEL") or "").strip():
            return
        url = (os.environ.get("OLLAMA_URL") or os.environ.get("OLLAMA_BASE")
               or "http://localhost:11434").rstrip("/")
        try:
            import aiohttp

            async def _discover() -> str:
                async with aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=4)
                ) as s:
                    async with s.get(url + "/api/tags") as r:
                        if r.status != 200:
                            return ""
                        d = await r.json()
                names = [m.get("name", "") for m in d.get("models", [])]
                if not names:
                    return ""
                for pref in ("qwen3-vl", "qwen2.5vl", "qwen2-vl",
                             "moondream", "llava", "minicpm-v", "bakllava"):
                    for n in sorted(names, key=lambda x: -len(x)):
                        if n.split(":")[0] == pref or n.startswith(pref + ":"):
                            return n
                for n in sorted(names, key=lambda x: -len(x)):
                    if re.search(r"vl|vision|moondream|llava", n.lower()):
                        return n
                return ""

            model = await asyncio.wait_for(_discover(), timeout=5)
        except Exception:
            model = ""
        if model:
            os.environ["OLLAMA_VISION_MODEL"] = model
            self._log(f"[Vision] Image solver auto-armed: {model} (drag challenges solved with exact coordinates)")
        else:
            self._log("[Vision] No vision model found on Ollama server — drag/image challenges will fall back to Skip (set OLLAMA_VISION_MODEL)")

    async def initialize(self) -> None:
        self._playwright = await async_playwright().start()

        # Auto-arm the qwen3-vl image solver BEFORE the first captcha so
        # drag challenges are solved on the very first attempt.
        await self._discover_vision_model()

        # Best-human-stealth launch args: Camoufox owns launch prefs and the
        # fingerprint entirely, so there is nothing to add.
        args = launch_args(headless=self.headless)
        self._log(f"[Engine] {ENGINE} launch args: {len(args)}")

        # Engine-level identity: Camoufox mints a fresh randomized profile
        # per launch — no bot-side UA / font / GPU / locale selection. It
        # additionally geo-matches the fingerprint to the proxy's real exit
        # region.
        self._ua = ""
        self._fingerprint = {}
        self._log(f"[Fingerprint] Identity owned by {ENGINE} engine — fresh randomized profile per launch")

        # Launch the browser WITH the proxy. The engine applies it as a
        # --proxy-server launch arg — a proxy passed later to new_context()
        # would be silently ignored and traffic would go direct.
        launch_proxy = self._launch_proxy()
        if launch_proxy is None and _tor_check():
            launch_proxy = {"server": "socks5://127.0.0.1:9050"}
            self._tor_enabled = True
        self._browser = await self._playwright.chromium.launch(
            headless=self.headless, args=args, proxy=launch_proxy)

        # Standard desktop viewport (1920x1080) — most common real resolution
        await self._build_context()

        # Done — context created by _build_context with full CDP evasion

    async def _build_context(self) -> None:
        """Build a fresh browser context with current self.proxy.
        Shared by initialize() and switch_proxy()."""
        vp = {'width': 1920, 'height': 1080}
        ctx_opts = build_context_options(
            self._fingerprint, self._ua, proxy=self.proxy, viewport=vp
        )
        if self.proxy and isinstance(self.proxy, dict):
            p = self.proxy
            server = f"{p.get('proto', 'http')}://{p.get('host')}:{p.get('port')}"
            host_ip = await asyncio.to_thread(self._resolve_proxy_ip, p.get("host", ""))
            ip_part = f" IP={host_ip}," if host_ip else ""
            self._log(f"Proxy: {server} ({ip_part} auth={'yes' if p.get('username') else 'no'})")
        elif _tor_check():
            self._tor_enabled = True
            self._log("[TOR] Using TOR SOCKS5 proxy...")
            if _tor_newnym():
                self._log("[TOR] New identity requested")
            # Camoufox already rides the TOR proxy from browser launch — a
            # context-level proxy would be rejected by Playwright when the
            # browser was launched with one.
            await asyncio.sleep(1)
        elif getattr(self, "_direct", False):
            self._log("[Proxy] Direct connection - no proxy and TOR unavailable")
        else:
            self._log("[TOR] [FATAL] TOR SOCKS5 (127.0.0.1:9050) NOT reachable - TOR-only mode requires TOR running on this instance", level="error")
            self._tor_enabled = False
            raise RuntimeError("TOR not available - TOR-only mode requires TOR on 127.0.0.1:9050")

        self._context = await self._browser.new_context(**ctx_opts)
        if self._ua:
            self._log(f"User-Agent: {self._ua[:60]}...")
        else:
            self._log("[Fingerprint] User-Agent: engine-owned identity")
        await self._context.add_init_script(
            build_init_script(self._fingerprint, self._ua)
        )
        self._page = await self._context.new_page()

        # CDP-level webdriver removal — runs BEFORE init scripts, catches early checks
        await apply_cdp_stealth(self._context, self._page)

        # Report the real egress IP of this session (bounded, never blocks).
        asyncio.create_task(self._log_proxy_exit_ip())

    async def switch_proxy(self, new_proxy=None) -> bool:
        """Swap to a new proxy AND a fresh fingerprint. Returns True on success.

        The engine pins the proxy at browser launch, so switching to a
        DIFFERENT session relaunches the browser; reusing the same session
        only rebuilds the context (and keeps the fingerprint — rotating an
        identity on an unchanged IP just churns fingerprints)."""
        self._direct = False
        same_session = bool(
            new_proxy and self.proxy
            and new_proxy.get("key") == self.proxy.get("key")
        )
        proxy_changed = (new_proxy or {}).get("key") != (self.proxy or {}).get("key")
        self.proxy = new_proxy
        if same_session:
            # Pool recycled the SAME session (all sessions blacklisted then
            # re-issued). Rebuilding the context with the same fingerprint is
            # consistent — regenerating an identity on an unchanged IP only
            # churns fingerprints for nothing.
            self._log("[Fingerprint] Same proxy session reused - keeping fingerprint")
        else:
            # Fresh fingerprint per session — same UA/GPU/font on a new IP is a
            # fingerprinting red flag and a known trigger for phone verification.
            self.rotate_fingerprint()
        try:
            if self._page:
                await self._page.close()
            if self._context:
                await self._context.close()
        except Exception:
            pass
        self._page = None
        self._context = None
        try:
            # A failed previous relaunch can leave self._browser None — never
            # call _build_context() (which does browser.new_context()) on a
            # dead browser: that was the "'NoneType' object has no attribute
            # 'new_context'" crash. Relaunch the browser instead.
            if proxy_changed or self._browser is None:
                self._log("[Switch] Proxy changed — relaunching browser with new session")
                await self._relaunch_browser()
            else:
                await self._build_context()
            label = 'proxy ' + str(new_proxy.get('key','?')[:40]) if new_proxy else 'fresh TOR circuit'
            self._log(f"[Switch] Context rebuilt with {label}")
            return True
        except Exception as e:
            self._log(f"[Switch] Context rebuild failed: {e}", level="error")
            return False

    async def switch_direct(self) -> bool:
        """Relaunch the browser with NO proxy (direct egress). Last resort when
        every residential session is dead and TOR is unavailable — the LIVE
        tab still renders a real page instead of chrome-error."""
        self.proxy = None
        self._direct = True
        try:
            if self._page:
                await self._page.close()
            if self._context:
                await self._context.close()
        except Exception:
            pass
        self._page = None
        self._context = None
        try:
            await self._relaunch_browser()
            self._log("[Switch] Relaunched with direct connection (no proxy)")
            return True
        except Exception as e:
            self._log(f"[Switch] Direct relaunch failed: {e}", level="error")
            return False

    async def is_alive(self) -> bool:
        """True if the browser + page are still usable.

        A parked browser (kept alive across Stop/Start) can die while the
        worker is stopped — TOR circuit dropped, browser crashed. Reuse is
        gated on this: a dead parked browser gets closed and relaunched."""
        if self._browser is None or self._page is None:
            return False
        try:
            if not getattr(self._browser, "is_connected", True):
                return False
            url = await asyncio.wait_for(
                self._page.evaluate("location.href"), timeout=3.0)
            return bool(url)
        except Exception:
            return False

    def rotate_fingerprint(self) -> None:
        """Rotate to a brand-new browser identity.

        Camoufox mints a fresh persona on EVERY launch (and on every
        new_context()), so the next relaunch (new proxy session) is
        automatically a new, unlinkable identity."""
        self._fingerprint = {}
        self._ua = ""
        self._log(f"[Fingerprint] Rotated: fresh {ENGINE} profile on next launch (engine-owned identity)")

    async def _rebuild_context_with_tor(self) -> bool:
        """Close the context and reopen WITH a fresh TOR circuit."""
        try:
            if self._page:
                await self._page.close()
            if self._context:
                await self._context.close()
        except Exception:
            pass
        try:
            if not _tor_check():
                self._log("[Nav] TOR not available for rebuild", level="error")
                return False
            if _tor_newnym():
                self._log("[Nav] Fresh TOR circuit requested")
            await asyncio.sleep(1)
            self._context = await self._browser.new_context(
                **build_context_options(
                    self._fingerprint, self._ua,
                    proxy={'proto': 'socks5', 'host': '127.0.0.1', 'port': '9050'},
                    viewport=random.choice([
                        {'width': 860, 'height': 640},
                        {'width': 1024, 'height': 768},
                        {'width': 900, 'height': 700},
                    ]),
                )
            )
            await self._context.add_init_script(
                build_init_script(self._fingerprint, self._ua)
            )
            self._page = await self._context.new_page()
            await apply_cdp_stealth(self._context, self._page)
            self._log("[Nav] Rebuilt browser context WITH fresh TOR proxy")
            return True
        except Exception as e:
            self._log(f"[Nav] context rebuild failed: {e}", level="error")
            return False

    async def _read_nav_state(self):
        """Read the register-page state.

        Every evaluate() runs over the engine's own channel, so there is no
        stale-WebDriver session to fall back from. A healthy page ALWAYS
        answers; (None, None) is the one true "tab dead" signal.
        """
        try:
            checks = await asyncio.wait_for(
                self._page.evaluate(_NAV_STATE_JS), timeout=2.5)
            if checks:
                st = json.loads(checks)
                st["source"] = "cdp"
                return st, "cdp"
        except Exception:
            pass
        # Mid-navigation the old execution context is destroyed before the
        # new one registers — one quick retry, then report dead.
        try:
            await asyncio.sleep(0.15)
            checks = await asyncio.wait_for(
                self._page.evaluate(_NAV_STATE_JS), timeout=2.5)
            if checks:
                st = json.loads(checks)
                st["source"] = "cdp"
                return st, "cdp"
        except Exception:
            pass
        return None, None

    async def _cdp_dom_nav_state(self):
        """JS-only alias of _read_nav_state (kept for callers)."""
        return await self._read_nav_state()

    async def _goto_register(self) -> bool:
        """Navigate to discord.com/register — single attempt, no retries.

        If the form doesn't render, we return False immediately so the worker
        can rotate to a fresh TOR circuit. Retrying the same URL on the same
        circuit is pointless — if Discord blocked that exit node, it won't
        unblock on retry."""
        url = "https://discord.com/register"
        # 30s cap like the original build: the goto is only a warm-up — the
        # form-poll below is the real render gate and returns the INSTANT the
        # form paints (0.15s polling). Dead sessions still bail via the hard
        # cap; slow-but-alive sessions survive: the goto coroutine is
        # cancelled in the background (the tab keeps committing) and the
        # title/url grace-poll below lets them catch up before anything is
        # declared dead.
        timeout_ms = 30000

        self._log(f"[Nav] Navigating to {url} (timeout={timeout_ms}ms)...")
        t0 = time.time()
        try:
            # domcontentloaded (not "load"): "load" waits for EVERY subresource
            # including the hCaptcha widget iframe and all its JS, which through
            # slow proxies hangs for tens of seconds. The form-poll loop below
            # already waits for the Discord SPA to boot, so we lose nothing.
            #
            # WRAPPED in asyncio.wait_for: the engine's timeout is advisory
            # only. When the proxy is dead, Chromium's internal TCP retry logic
            # can hang regardless of timeout — asyncio.wait_for with a hard cap
            # kills the coroutine and forces a fresh proxy.
            await asyncio.wait_for(
                self._page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms),
                timeout=(timeout_ms / 1000.0) + 3.0,  # hard cap (never 18s)
            )
            self._log(f"[Nav] Page DOM ready in {time.time() - t0:.1f}s (not waiting for hCaptcha subresources)")
        except asyncio.TimeoutError:
            elapsed = time.time() - t0
            self._log(f"[Nav] Page.goto HARD TIMEOUT after {elapsed:.1f}s — proxy likely dead")

        # ── Check what we got ──
        try:
            page_title = await asyncio.wait_for(self._page.title(), timeout=3.0)
            page_url = await asyncio.wait_for(self._page.evaluate("location.href"), timeout=3.0)
        except Exception:
            page_title = "(unknown)"
            page_url = "(unknown)"
        if str(page_title) == "(unknown)" and str(page_url) == "(unknown)":
            # The hard cap can cancel goto while a slow-but-alive session is
            # still committing; the tab usually answers within a beat. Grace-
            # poll up to ~5s before declaring the session dead — this is what
            # lets the shorter goto cap skip dead sessions fast WITHOUT killing
            # slow-but-healthy ones.
            for _grace in range(5):
                await asyncio.sleep(1.0)
                try:
                    page_title = await asyncio.wait_for(self._page.title(), timeout=3.0)
                    page_url = await asyncio.wait_for(self._page.evaluate("location.href"), timeout=3.0)
                except Exception:
                    continue
                if str(page_title) != "(unknown)" or str(page_url) != "(unknown)":
                    break
        self._log('[Nav] Page: title="' + str(page_title)[:80] + '" url=' + str(page_url)[:80])

        # ── Dead proxy (cannot reach Discord at all) ──
        # chrome-error:// is a REAL dead signal (DNS/connection failure).
        # about:blank is NOT dead: a slow TOR/residential circuit can still be
        # committing the navigation when the goto cap fires, so a blank tab
        # means "still loading" here, not "dead". Dead sessions surface
        # chrome-error within seconds; blank-but-alive sessions just need the
        # render-wait loop below (which re-issues the goto if the tab stays
        # blank). Bouncing on about:blank is what made the bot "fail instantly
        # without waiting" on slow circuits.
        if "chrome-error://" in (page_url or ""):
            proxy_label = "PROXY SESSION" if self.proxy else "TOR CIRCUIT"
            self._nav_error = f"{proxy_label.lower()} dead (browser error page: {page_url[:60]})"
            self._log(f"[Nav] {proxy_label} DEAD (url={page_url[:60]}) - rotating to fresh circuit")
            return False
        if (page_url or "").strip() in ("", "about:blank"):
            self._log("[Nav] Tab still at about:blank after goto cap - navigation still committing, render-wait will re-issue if stuck", level="warn")

        # ── Page still loading (title + url unreadable) — do NOT bail. ──
        # A slow TOR/residential circuit can keep the page unreadable for
        # minutes while the navigation commits and React downloads. The
        # render-wait loop below is the only gate; real dead proxies were
        # already caught by the chrome-error / about:blank check above.
        title_blank = not str(page_title or "").strip()
        url_blank = not str(page_url or "").strip() or str(page_url or "").strip() in ("about:blank",)
        if title_blank and url_blank:
            self._log(f"[Nav] Page white/unreadable after goto (title empty, url={str(page_url)[:40]}) - render-wait keeps polling and rotates if it stays dead")

        # ── Quick body text check (403/Forbidden/Cloudflare) ──
        try:
            body_text = await asyncio.wait_for(
                self._page.evaluate("document.body ? document.body.innerText.substring(0, 500) : ''"),
                timeout=3.0)
            if body_text and any(kw in body_text.lower() for kw in (
                "forbidden", "403 forbidden", "access denied", "cloudflare",
                "attention required", "rate limit", "ratelimited", "rate limited",
                "too many requests", "slowdown", "try again later", "429",
            )):
                self._nav_error = f"blocked by Discord/Cloudflare — body text: {body_text[:80]}"
                self._log(f"[Nav] BLOCKED — body contains: {body_text[:100]}", level="warn")
                return False
        except Exception:
            pass

        # ── Rate-limit detection — rotate as soon as the message renders ──
        # Discord/Cloudflare throttle abused exit nodes with 429s that render
        # as "The resource is being rate limited." / "too many requests".
        # Check the FULL body text — the message often paints below the first
        # few hundred chars, so truncating would miss it.
        try:
            full_body = await asyncio.wait_for(
                self._page.evaluate("document.body ? document.body.innerText : ''"),
                timeout=3.0)
        except Exception:
            full_body = ""
        if any(kw in (full_body or "").lower() for kw in _RATE_LIMIT_KEYWORDS):
            self._nav_error = "rate limited (429) by Discord"
            self._log("[Nav] RATE LIMITED (429) - rotating circuit", level="warn")
            return False

        # ── Block keywords in title ──
        # Cloudflare managed-challenge interstitial is NOT fatal: it auto-
        # resolves in ~5-15s and drops cf_clearance. Bouncing on the title
        # was throwing away healthy sessions that just needed a beat. Flow
        # into the poll loop, which waits for cf_clearance and only rotates
        # if the challenge persists past its own budget.
        challenge_title_kws = ["just a moment", "attention required",
                               "checking your browser", "verify you are human"]
        fatal_title_kws = ["blocked", "cloudflare", "ddos-guard", "captcha",
                           "forbidden", "403", "access denied",
                           "you do not have permission", "error 1020",
                           "rate limit", "ratelimited", "rate limited",
                           "too many requests", "slowdown", "try again later"]
        title_lower = (page_title or "").lower()
        if any(kw in title_lower for kw in fatal_title_kws):
            self._nav_error = f"blocked - Cloudflare/firewall hard block (title: {str(page_title)[:60]})"
            self._log('[Nav] BLOCKED by Cloudflare/firewall (title: "' + str(page_title)[:60] + '")', level="warn")
            return False
        if any(kw in title_lower for kw in challenge_title_kws):
            self._log('[Nav] Cloudflare challenge title "' + str(page_title)[:40] + '" - waiting for auto-resolve in poll loop')
        # ── Check if Discord SPA shell loaded ──
        try:
            app_mount = await asyncio.wait_for(
                self._page.evaluate("document.querySelector('#app-mount') !== null"),
                timeout=5.0)
            if app_mount:
                self._log("[Nav] Discord SPA app-mount detected")
        except Exception:
            app_mount = False

        # ── Poll for form elements ──
        # Discord uses aria-label on inputs, not name/id — use broad selectors.
        #
        # NO RENDER TIMEOUT: this loop waits as long as it takes for the form
        # to fully render and returns the INSTANT it paints (checks every
        # 0.15s). There is no wall-clock budget — the only exits are real
        # signals: a successful render or a hard block (403 / rate-limit /
        # fatal title, detected above). An unreadable or blank page is still
        # loading — keep waiting; reload it up to max_reloads times to
        # re-fetch dropped JS bundles, then keep waiting.
        reload_after = 4.0       # blank this long -> reload to re-fetch JS bundles
        max_reloads = 2          # hard cap on reloads per session
        _render_wait_start = time.time()
        self._log(f"[Nav] Waiting for registration form to render (no timeout - reloads<={max_reloads}, reload_after={reload_after:.0f}s)...")
        blank_since = None       # when the page first looked blank
        challenge_since = None   # when a Cloudflare challenge first appeared
        reload_count = 0         # reloads attempted for a blank/hung SPA
        login_clicked = False    # already clicked the Register link once
        turnstile_tried = False  # already attempted a Turnstile bypass
        blank_nav_since = None   # when the tab first sat at about:blank (nav never committed)
        nav_reissues = 0         # re-issued gotos for a never-committed navigation
        dead_reads = 0           # consecutive unreadable polls -> page died (old-build bail)
        last_log = -1.0
        while True:
            # User hit Stop — abort the wait immediately (the browser gets
            # parked on Discord for reuse; it is NOT killed).
            if self._stopped.is_set():
                self._log("[Nav] Stopped by user - aborting navigation wait")
                self._nav_error = "stopped by user"
                return False
            elapsed = time.time() - _render_wait_start
            # Dual-channel read: page.evaluate() (the engine falls back to the
            # raw CDP websocket when the reattached WebDriver session's JS
            # context is stale — the old white-screen bug where the page
            # loaded fine, title read "Discord", but evaluate() returned
            # None and the bot rotated a PERFECTLY GOOD session), then CDP
            # DOM-presence checks that need no JS execution at all. state is
            # None ONLY when every channel failed = the page is genuinely
            # dead.
            state, _chan = await self._read_nav_state()

            if state is None:
                # Every read channel failed (WebDriver JS + CDP JS + CDP DOM)
                # — tab white / dead. Probe WHAT'S WRONG every ~3s for ALL
                # logs (title, url, readyState, body length, the error), then
                # after 20 consecutive dead polls rotate — the old-build
                # white-screen bail. A healthy page can NEVER reach here,
                # because the CDP fallback keeps reads alive.
                dead_reads += 1
                if elapsed >= last_log + 3.0:
                    last_log = elapsed
                    probe = "(no probe)"
                    try:
                        probe = await asyncio.wait_for(self._page.evaluate(
                            """() => {
                                try {
                                    return JSON.stringify({
                                        title: document.title || "",
                                        url: location.href || "",
                                        readyState: document.readyState || "",
                                        bodyLen: document.body ? (document.body.innerText || "").length : -1
                                    });
                                } catch (e) { return "probe-err: " + (e && e.message || e); }
                            }"""
                        ), timeout=2.0)
                    except Exception as _pe:
                        probe = f"probe-failed: {type(_pe).__name__}: {_pe}"
                    self._log(f"[Nav] Page unreadable ({int(elapsed)}s, {dead_reads}x in a row) - probe: {probe}")
                if dead_reads >= 20:
                    self._nav_error = "page unreadable 20x in a row (white screen / tab dead) - rotating circuit"
                    self._log("[Nav] Page unreadable 20x in a row - page died, rotating circuit")
                    return False
                await asyncio.sleep(0.3)
                continue
            dead_reads = 0

            if state:
                # ── Mid-wait page-health checks ──
                cur_url = (state.get("url") or "").strip() or ""
                # Browser error page appearing mid-wait = the circuit died.
                if "chrome-error://" in cur_url:
                    self._nav_error = "proxy/circuit dead (chrome-error page)"
                    self._log("[Nav] Browser error page - rotating circuit")
                    return False
                if cur_url in ("", "about:blank"):
                    # Navigation never committed (goto cap fired mid-commit on
                    # a slow circuit). Re-issue the goto so the page actually
                    # starts loading instead of waiting forever on a blank
                    # tab. max_reloads re-issues, then KEEP waiting — the
                    # directive is no render timeout.
                    if blank_nav_since is None:
                        blank_nav_since = time.time()
                    elif time.time() - blank_nav_since >= 5.0 and nav_reissues < max_reloads:
                        nav_reissues += 1
                        self._log(f"[Nav] Tab stuck at about:blank for {int(time.time() - blank_nav_since)}s - re-issuing goto ({nav_reissues}/{max_reloads})...", level="warn")
                        try:
                            await asyncio.wait_for(
                                self._page.goto(url, wait_until="domcontentloaded", timeout=30000),
                                timeout=33.0)
                        except Exception:
                            pass
                        await asyncio.sleep(1.0)
                        blank_nav_since = None
                else:
                    blank_nav_since = None

                # Log every ~4s with input/button counts for debugging
                if elapsed >= last_log + 4.0:
                    last_log = elapsed
                    self._log(f"[Nav] Poll {int(elapsed)}s ({_chan}): email={state.get('email')} ageGate={state.get('ageGate')} login={state.get('isLogin')} inputs={state.get('inputCount')} buttons={state.get('buttonCount')} cf={state.get('cfClearance')} text={state.get('textPreview','')[:60]}")

                if state.get("email") and state.get("username"):
                    self._log(f"[Nav] SUCCESS! Full form rendered after {int(elapsed)}s")
                    return True
                if state.get("email") and state.get("password"):
                    self._log(f"[Nav] SUCCESS! Email+password form rendered after {int(elapsed)}s")
                    return True
                if state.get("ageGate"):
                    self._log(f"[Nav] Age gate detected after {int(elapsed)}s - returning true, form filler handles it")
                    return True

                # Blank render + Cloudflare challenge handling.
                # Two very different causes, handled separately:
                #   1. Cloudflare managed challenge ("Just a moment..."): it
                #      auto-resolves and drops cf_clearance. WAIT for it as
                #      long as it takes — no bail-out (Turnstile widgets get
                #      re-attempted every ~10s).
                #   2. React failed to hydrate (a JS bundle dropped/errored): a
                #      reload re-fetches the bundles and almost always boots.
                #      Reload up to max_reloads times, then keep waiting.
                if state.get("challenge"):
                    if challenge_since is None:
                        challenge_since = time.time()
                        self._log("[Nav] Cloudflare 'Just a moment' challenge detected - waiting for auto-resolve (cf_clearance)...")
                    blank_since = None
                    if state.get("cfClearance"):
                        self._log("[Nav] cf_clearance set - challenge passed, waiting for React to boot...")
                        challenge_since = None
                    else:
                        # Cloudflare Turnstile widget (not the auto-resolving
                        # managed challenge). Click it with a real humanized
                        # click and re-attempt every ~10s — no bail-out; wait
                        # as long as it takes.
                        if (not turnstile_tried
                                or time.time() - challenge_since >= 10.0):
                            turnstile_tried = True
                            self._log("[Nav] Cloudflare Turnstile widget detected - clicking it...")
                            if await self._solve_turnstile_if_present():
                                self._log("[Nav] Turnstile clicked - waiting for React to boot...")
                                challenge_since = None
                    await asyncio.sleep(0.3)
                    continue
                challenge_since = None

                if (state.get("hasAppMount") and not state.get("inputCount")
                        and not state.get("buttonCount")
                        and not (state.get("textPreview") or "").strip()):
                    if blank_since is None:
                        blank_since = time.time()
                        self._log("[Nav] SPA shell mounted but React not booted - waiting for JS bundles (reload if stuck)...")
                    # cf_clearance set = challenge passed - assets unblocked, form should follow.
                    if state.get("cfClearance"):
                        if blank_since is not None:
                            self._log("[Nav] cf_clearance cookie appeared - Cloudflare challenge passed, waiting for React...")
                        blank_since = None
                    elif time.time() - blank_since >= reload_after and reload_count < max_reloads:
                        reload_count += 1
                        self._log(f"[Nav] React still blank after {int(reload_after)}s - reloading page (attempt {reload_count}/{max_reloads}) to re-fetch JS bundles...")
                        try:
                            await asyncio.wait_for(self._page.reload(), timeout=15.0)
                        except Exception:
                            pass
                        await asyncio.sleep(1.2)
                        blank_since = None
                        challenge_since = None
                        continue
                    # max_reloads exhausted — KEEP waiting, no timeout. A slow
                    # circuit can still paint the form minutes later.
                else:
                    blank_since = None
                if state.get("isLogin") and not login_clicked and elapsed >= 3.0:
                    self._log("[Nav] Login page detected \u2014 clicking Register link...")
                    try:
                        clicked_reg = await self._page.evaluate("""() => {
                            const all = document.querySelectorAll('a, button, [role="link"], [role="button"]');
                            for (const el of all) {
                                const t = (el.textContent || '').toLowerCase().replace(/\s+/g, ' ').trim();
                                if (t && /register|sign up|create account/i.test(t) && el.offsetParent !== null) {
                                    el.scrollIntoView({block: 'center'});
                                    el.click();
                                    return 'clicked';
                                }
                            }
                            return '';
                        }""")
                    except Exception:
                        clicked_reg = ''
                    if clicked_reg:
                        self._log("[Nav] Clicked Register link \u2014 continuing poll for register form...")
                        login_clicked = True
                        blank_since = None
                        await asyncio.sleep(0.3)
                        continue
                    self._log("[Nav] Login page, no Register link clickable \u2014 rotating circuit", level="warn")
                    break

            # Check for redirect to app
            try:
                cur = await asyncio.wait_for(self._page.evaluate("location.href"), timeout=1.0)
                if "discord.com/app" in cur or "discord.com/channels" in cur:
                    self._log(f"[Nav] Redirected to app: {cur[:60]}")
                    return True
            except Exception:
                pass
            await asyncio.sleep(0.15)

        # ── Form never rendered — dump page state for debugging ──
        try:
            dump = await asyncio.wait_for(self._page.evaluate("""() => {
                const inputs = Array.from(document.querySelectorAll('input')).slice(0, 20).map(function(e) { return {
                    type: e.type, name: e.name, id: e.id, ariaLabel: e.getAttribute('aria-label') || '',
                    placeholder: e.placeholder || '', visible: e.offsetParent !== null
                }; });
                const buttons = Array.from(document.querySelectorAll('button')).slice(0, 10).map(function(e) { return {
                    text: (e.innerText || '').substring(0, 40), type: e.type, visible: e.offsetParent !== null
                }; });
                return JSON.stringify({
                    title: document.title,
                    url: location.href,
                    bodyText: (document.body?.innerText || '').substring(0, 200),
                    inputs: inputs,
                    buttons: buttons,
                    hasAppMount: document.querySelector('#app-mount') !== null
                });
            }"""), timeout=2.0)
            self._log(f"[Nav] DEBUG page state: {dump}")
        except Exception:
            pass

        proxy_label = "fresh proxy session" if self.proxy else "fresh TOR circuit"
        self._nav_error = f"Discord form never rendered (loop exited without a render signal) — rotating to {proxy_label}"
        self._log(f"[Nav] Form never rendered - rotating to {proxy_label}", level="warn")
        return False

    async def capture_screenshot(self) -> str:
        if not self._page:
            return ""
        # Full-page capture can hang on Discord's SPA through slow proxies;
        # bound it and fall back to a viewport shot instead of stalling.
        try:
            screenshot = await asyncio.wait_for(self._page.screenshot(full_page=True), timeout=20)
        except asyncio.TimeoutError:
            try:
                screenshot = await asyncio.wait_for(self._page.screenshot(full_page=False), timeout=10)
            except Exception:
                screenshot = None
        except Exception:
            screenshot = None
        if not screenshot:
            return ""
        b64 = base64.b64encode(screenshot).decode('utf-8')
        self._screenshots.append(b64)
        if len(self._screenshots) > 100:
            self._screenshots = self._screenshots[-50:]
        return b64

    async def start_discord_signup(self) -> bool:
        if not self._page:
            await self.initialize()
        self.phone_verify_detected = False
        self._nav_ok = False
        self._mail_failed = False

        # app.py closes + nulls self._mail between attempts (prevents aiohttp
        # connector leaks) while REUSING this bot object for the next attempt —
        # re-create the duckmail client here or the next inbox creation crashes
        # with "'NoneType' object has no attribute 'create_inbox'" and the
        # worker spins forever on the same dead mail path.
        if self._mail is None:
            self._mail = TempMail(log=self._log)

        # No hardcoded email — create a duckmail.sbs inbox on the
        # Discord-friendly domain @glasswhitehub.com (pure REST API, no
        # browser, no proxy contention). Retry fast (2x, no backoff) when
        # duckmail hiccups.
        if not self._email:
            self._log(f"[Mail] No email configured - creating duckmail.sbs inbox (@{self._domain})...")
            nav_ok = False
            try:
                # duckmail is a pure REST client (api.duckmail.sbs, Hydra
                # API) — no browser involved. The inbox is created on the
                # fixed Discord-friendly domain @glasswhitehub.com.

                # Create inbox FIRST (with hard timeout) — never in parallel
                # with CDP navigation on the same browser.
                self._email = ""
                for mail_try in range(2):
                    try:
                        self._email = await asyncio.wait_for(
                            self._mail.create_inbox(), timeout=20.0)
                    except asyncio.TimeoutError:
                        self._log("[Mail] Inbox creation TIMED OUT after 20s", level="error")
                    except Exception as e:
                        self._log(f"[Mail] duckmail inbox error: {e}", level="error")
                    if self._email:
                        break
                    self._log(f"[Mail] Inbox creation failed — retrying ({mail_try + 1}/2)...", level="warn")

                # NOW navigate to Discord — inbox is ready (or we gave up)
                nav_ok = await self._goto_register()
            except Exception as e:
                self._log(f"[Mail] duckmail inbox error: {e}", level="error")
                self._email = ""

            if not self._email:
                self._mail_failed = True
                self._log("[FAIL] No email available - aborting signup", level="error")
                return False
            if not nav_ok:
                self._log("[FAIL] Could not navigate to Discord /register - aborting", level="error")
                return False
        else:
            self._log(f"Using configured email: {self._email}")
            if not await self._goto_register():
                self._log("[FAIL] Could not navigate to Discord /register - aborting", level="error")
                return False

        self._nav_ok = True
        self._log("=" * 40)
        self._log(f"Starting Discord signup with email: {self._email}")
        self._log("=" * 40)

        try:
            self._log("[Nav] Discord site rendered")
            await self.capture_screenshot()

            # ── Settle wait before touching the form ──
            # Discord's SPA keeps re-rendering after the form first paints;
            # writing values during that window gets them wiped on the next
            # re-render (the "fields stay empty" bug). Wait a fixed 20s so
            # hydration fully finishes before the filler runs.
            self._log("[Nav] Waiting 20s for Discord to fully settle before filling...")
            settle_deadline = time.time() + 20.0
            while time.time() < settle_deadline:
                if self._stopped.is_set():
                    self._nav_error = "stopped by user"
                    self._log("[Nav] Stopped by user during settle wait")
                    return False
                await asyncio.sleep(0.5)

            # Fill the form
            form_ok = await self._fill_registration_form()
            success = False
            if form_ok:
                self._log("[Form] Form filled - checking for hCaptcha...")
                # Cloudflare Turnstile can gate the form submit. Click it
                # with a real humanized click before the hCaptcha solver runs.
                if await self._solve_turnstile_if_present():
                    self._log("[Captcha] [OK] Turnstile clicked")
                success = await self._solve_hcaptcha_if_present()
            else:
                self._log("[FAIL] Form filling failed", level="error")

            if success:
                self._log("[OK] CAPTCHA SOLVED! Registration submitted.")
                # Discord can demand phone verification right after account
                # creation. Detect it BEFORE waiting on email — if present,
                # abort this attempt so the worker rotates proxy + fingerprint
                # + mail domain and retries (phone-gated accounts are dead).
                # Poll every second so a phone gate is caught the moment it
                # renders instead of after a fixed 5s sleep (cap ~6s so the
                # happy path to email verification isn't delayed).
                phone_detected = False
                for _ in range(10):
                    if await self._detect_phone_verification():
                        phone_detected = True
                        break
                    await asyncio.sleep(0.5)
                if phone_detected:
                    self.phone_verify_detected = True
                    self._log("[Phone] [DETECTED] Phone verification required - rotating proxy+fingerprint+domain", level="warn")
                    return False
                # Auto-verify: complete Discord email verification. Skipped
                # when a custom email is in use — the user clicks the link in
                # their own inbox, so we just tell them.
                await self._verify_account_email()
                # Login + grab the FULL token from localStorage. With a custom
                # email the user may need to click the verify link first, so
                # keep watching much longer (60s) and re-submit the login.
                self._token = await self._extract_token(
                    attempts=6 if self._email and not self._mail else 4,
                    poll_rounds=30 if self._email and not self._mail else 10,
                )
                if self._token:
                    self._log("[Token] [OK] Full token captured")
                    self._log(f"[Account] @{self._username or self._email.split('@')[0]} is in Discord and confirmed")
                    self._log(f"[Account] Email={self._email} | User={self._username} | Pass={self._password} | Date={time.strftime('%Y-%m-%d %H:%M')}")
                    await self._humanize_account()
                else:
                    self._log("[Token] No token yet (account may still be pending)", level="warn")
            else:
                self._log("[FAIL] Captcha solving failed", level="error")

        except Exception as e:
            self._log(f"Error: {e}", level="error")
            import traceback
            traceback.print_exc()
            success = False

        await self.capture_screenshot()
        return success

    async def _humanize_account(self) -> None:
        """Set avatar and bio on the newly created Discord account.

        Uses the Discord API directly with the captured token. Best-effort
        only — failures are logged but never block the account from being
        saved (a humanized account is nice but non-critical)."""
        if not (self._token and self._page):
            return
        try:
            import aiohttp
            bio = random.choice(_BIO_POOL)
            headers = {"Authorization": self._token, "Content-Type": "application/json"}
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10)) as s:
                # Set bio
                async with s.patch(
                    "https://discord.com/api/v9/users/@me",
                    json={"bio": bio}, headers=headers,
                ) as r:
                    if r.status == 200:
                        data = await r.json()
                        self._bio = bio
                        self._user_id = str(data.get("id", ""))
                        avatar_hash = data.get("avatar") or ""
                        if avatar_hash:
                            self._avatar_data = avatar_hash
                        self._humanized = True
                        self._log(f"[Humanize] Bio set: \"{bio}\"")
                    else:
                        self._log(f"[Humanize] API returned {r.status}", level="warn")
        except Exception as e:
            self._log(f"[Humanize] Error: {e}", level="warn")

    # ── Cloudflare Turnstile ─────────────────────────────────────────────
    # Discord sits behind Cloudflare, and a Turnstile captcha can gate
    # navigation / form submit / mail verification. The widget is clicked
    # with a real, humanized locator click on the Camoufox page (the engine's
    # humanize layer drives the pointer — never a synthetic JS event), then
    # we confirm the challenge cleared via cf_clearance or the widget
    # leaving the DOM.
    _TURNSTILE_SELECTORS = (
        'iframe[src*="challenges.cloudflare.com"]',
        'iframe[src*="turnstile"]',
        'div.cf-turnstile iframe',
    )

    async def _detect_turnstile(self) -> bool:
        """True when a Cloudflare Turnstile widget is on the page.

        Turnstile is a separate anti-bot layer from hCaptcha: Cloudflare
        mounts it inside a challenges.cloudflare.com iframe (a div with the
        ``cf-turnstile`` class in the page DOM)."""
        try:
            for sel in self._TURNSTILE_SELECTORS:
                loc = self._page.locator(sel)
                if await loc.count() > 0:
                    return True
        except Exception:
            pass
        # Frame-tree fallback: any live frame on challenges.cloudflare.com.
        try:
            for f in self._page.frames:
                if "challenges.cloudflare.com" in (f.url or ""):
                    return True
        except Exception:
            pass
        return False

    async def _solve_turnstile_if_present(self) -> bool:
        """Bypass a Cloudflare Turnstile widget with a humanized click.

        Clicks the widget checkbox with a real locator click on the Camoufox
        page, then confirms the challenge cleared via the cf_clearance
        cookie or the widget frame disappearing."""
        try:
            if not await self._detect_turnstile():
                return False
            self._log("[Turnstile] Widget present - clicking it...")
            # Humanized click on the widget checkbox (Camoufox drives the
            # pointer with its humanize layer — never a synthetic JS event).
            clicked = False
            for sel in self._TURNSTILE_SELECTORS:
                try:
                    loc = self._page.locator(sel)
                    if await loc.count() > 0:
                        await loc.first.click(timeout=4000)
                        clicked = True
                        break
                except Exception:
                    continue
            if not clicked:
                self._log("[Turnstile] No checkbox found to click", level="warn")
                return False
            # 3) Confirm the challenge cleared (cf_clearance or widget gone).
            for _ in range(10):
                try:
                    raw = await self._page.evaluate(
                        "() => document.cookie")
                    if "cf_clearance=" in (raw or ""):
                        self._log("[Turnstile] [OK] cf_clearance issued")
                        return True
                except Exception:
                    pass
                if not await self._detect_turnstile():
                    self._log("[Turnstile] [OK] widget resolved")
                    return True
                await asyncio.sleep(0.4)
            self._log("[Turnstile] Click sent but no clearance yet", level="warn")
            return False
        except Exception as e:
            self._log(f"[Turnstile] Error: {e}", level="warn")
            return False

    async def _detect_phone_verification(self) -> bool:
        """Check the current page for Discord's phone-verification screen.

        Discord shows this right after account creation (or as a login gate)
        when it suspects automation. Markers: a phone/tel input, or a phone
        heading/body. Returns True when the account needs a phone number."""
        try:
            result = await asyncio.wait_for(self._page.evaluate("""() => {
                // Phone input (name=phone / type=tel / aria/placeholder)
                const phoneInput = document.querySelector(
                    'input[name="phone"], input[type="tel"], ' +
                    'input[aria-label*="phone" i], input[placeholder*="phone" i], ' +
                    'input[autocomplete="tel"]');
                if (phoneInput && phoneInput.offsetParent !== null) {
                    return 'input';
                }
                const text = (document.body ? document.body.innerText : '').toLowerCase();
                const markers = [
                    'verify your phone', 'phone verification', 'verify your account',
                    'add a phone number', 'phone number required',
                    'we need to verify your account', 'enter your phone number',
                    'confirm your phone', 'what\'s your phone number',
                    'verify via phone', 'add your phone number',
                ];
                for (const kw of markers) {
                    if (text.includes(kw)) return 'text:' + kw;
                }
                return '';
            }"""), timeout=4.0)
            return bool(result)
        except Exception:
            return False

    async def _verify_account_email(self) -> bool:
        """Wait for the Discord verification email and open its link (best effort).
        Aborts early if Discord instead demands phone verification."""
        if not self._mail:
            return False
        try:
            link = await self._mail.wait_for_verification_link(timeout=150)
            if not link:
                self._log("[Mail] No verification link found yet - account may still be created", level="warn")
                return False
            self._log(f"[Mail] Opening verification link: {link[:80]}...")
            await self._page.goto(link, wait_until='domcontentloaded', timeout=NAV_TIMEOUT_MS)
            await asyncio.sleep(2)
            # Cloudflare Turnstile may gate the verification page - click it
            # if present.
            if await self._solve_turnstile_if_present():
                self._log("[Mail] [OK] Turnstile bypassed on verification page")
            # Discord shows a verification success page (or redirects to login)
            try:
                page_text = await self._page.evaluate(
                    "() => document.body.innerText.substring(0, 300)")
            except Exception:
                page_text = ""
            if any(w in (page_text or "").lower()
                   for w in ('verified', 'success', 'confirmation', 'you\'re all set')):
                self._log("[Mail] [OK] Email verification completed")
            await self.capture_screenshot()
            self._log("[Mail] [OK] Verification link opened")
            return True
        except Exception as e:
            self._log(f"[Mail] verification error: {e}", level="warn")
            return False

    async def _past_captcha(self) -> bool:
        """True when the page has moved past the captcha into Discord."""
        try:
            cur_url = self._page.url
            return any(k in cur_url for k in PAST_CAPTCHA_KEYWORDS)
        except:
            return False

    async def _hcaptcha_frame_for(self, iframe):
        """Resolve the live Playwright Frame for an hCaptcha iframe element.

        Locator.content_frame() returns None for attached cross-origin
        iframes on the patched engine - even though the frames are live and
        evaluable (the DOM-dump path proves it by iterating page.frames).
        So fall back to the page's frame tree, preferring the VISIBLE widget
        frame: body not aria-hidden AND containing a checkbox node. hCaptcha
        mounts a hidden twin (body aria-hidden=true, same URL) - never pick
        it when a visible one exists.
        """
        # 1) Direct content_frame() first (real Playwright: Locator.
        # content_frame is a property, so resolve the element handle first).
        try:
            frame = await (await iframe.element_handle(timeout=5000)).content_frame()
            if frame is not None:
                return frame
        except Exception:
            pass
        # 2) Frame-tree fallback: match by src, then by content.
        src = ""
        try:
            src = await iframe.get_attribute("src") or ""
        except Exception:
            src = ""
        probe_js = """() => {
            const b = document.body;
            return JSON.stringify({
                cb: !!document.querySelector('#checkbox, [role="checkbox"], .checkbox, input[type="checkbox"], [aria-checked], .button-submit'),
                hidden: b ? b.getAttribute('aria-hidden') : null
            });
        }"""
        best = None
        for f in self._page.frames:
            try:
                furl = f.url or ""
            except Exception:
                continue
            if "hcaptcha" not in furl:
                continue
            info = None
            try:
                raw = await f.evaluate(probe_js)
                info = json.loads(raw) if raw else None
            except Exception:
                info = None
            if src and src in furl:
                best = best or f
            if info and info.get("cb"):
                if info.get("hidden") != "true":
                    return f
                if best is None:
                    best = f
        return best

    async def _frame_js_ready(self, iframe, js) -> bool:
        """Evaluate `js` inside the iframe's content frame; False on any error."""
        frame = await self._hcaptcha_frame_for(iframe)
        if frame is None:
            return False
        try:
            val = await frame.evaluate(js)
            return bool(val)
        except Exception:
            return False

    async def _challenge_rendered(self, iframe) -> bool:
        """True only when the hCaptcha challenge iframe has genuinely painted.

        A challenge iframe is laid out at full size (>= 80px tall) the moment
        it is inserted, BEFORE its JS renders anything - so bounding-box checks
        alone report 'rendered' for a blank box. Require the frame document to
        be complete and to contain real challenge content before claiming ready.
        """
        return await self._frame_js_ready(iframe, """() => {
            if (document.readyState !== 'complete') return false;
            const body = document.getElementById('hcaptcha-body');
            if (body && body.offsetHeight >= 40) return true;
            const t = (document.body && document.body.innerText) || '';
            return t.trim().length >= 5;
        }""")

    async def _widget_rendered(self, iframe) -> bool:
        """True when the hCaptcha widget iframe is genuinely ready to click.

        Readiness is hCaptcha's own signal: the widget body is marked
        aria-hidden="true" until its JS has finished rendering the UI, then
        drops it once painted. A still-hidden body is NOT ready. Once that
        signal is satisfied, the presence of any real widget node (checkbox,
        toolbar trigger, refresh button or logo) is enough — the new widget
        lays its checkbox out in a way getBoundingClientRect() reports as
        0-sized, so geometry must never gate readiness.
        """
        return await self._frame_js_ready(iframe, """() => {
            const body = document.body;
            if (!body) return false;
            if (document.readyState !== 'complete') return false;
            // Ground truth for readiness: a real widget UI node (checkbox,
            // toolbar trigger, refresh button or logo) proves hCaptcha
            // painted the widget. Some widget builds keep the body
            // aria-hidden="true" even after painting (field probe showed
            // ariaHidden:"true", children:2, checkbox:true,
            // readyState:"complete" with the full widget DOM present), so
            // aria-hidden must never block readiness once a node exists.
            if (document.querySelector(
                    '#checkbox, .checkbox, [role="checkbox"], input[type="checkbox"], ' +
                    '[aria-checked], .button-submit, #menu-info, .display-menu-btn, ' +
                    '.refresh.button, .hcaptcha-logo')) return true;
            // No widget node yet — only now does body aria-hidden mean the
            // widget is still on the loader stage.
            if (body.getAttribute('aria-hidden') === 'true') return false;
            const laidOut = (el) => {
                if (!el) return false;
                const cs = getComputedStyle(el);
                if (cs.display === 'none' || cs.visibility === 'hidden') return false;
                const r = el.getBoundingClientRect();
                return !!r && r.width > 1 && r.height > 1;
            };
            // 1) The checkbox itself - the real click target.
            for (const sel of ['#checkbox', '.checkbox', '[role="checkbox"]',
                               'input[type="checkbox"]', '[aria-checked]',
                               '.button-submit']) {
                const els = document.querySelectorAll(sel);
                for (const el of els) {
                    if (laidOut(el)) return true;
                }
            }
            // 2) The widget toolbar (menu trigger / refresh / logo) proves
            //    the widget painted even while the checkbox is mid-render.
            for (const sel of ['#menu-info', '.display-menu-btn',
                               '.refresh.button', '.hcaptcha-logo']) {
                if (laidOut(document.querySelector(sel))) return true;
            }
            // 3) Any rendered text (e.g. the "I am human" label).
            const t = (body.innerText || '').trim();
            return t.length >= 3;
        }""")

    async def _widget_has_checkbox(self, iframe) -> bool:
        """Cheap probe: does the widget frame contain a checkbox node at all?

        Used to ALWAYS attempt the click even when the strict readiness
        probe (_widget_rendered) hasn't flipped yet — hCaptcha keeps some
        widget builds aria-hidden="true" while the checkbox is already
        painted and interactive, so readiness must never gate the attempt.
        """
        try:
            frame = await (await iframe.element_handle(timeout=4000)).content_frame()
            if frame is None:
                return False
            return bool(await frame.evaluate(
                "() => !!document.querySelector("
                "'#checkbox, [role=\"checkbox\"], .checkbox, "
                "input[type=\"checkbox\"], [aria-checked], .button-submit')"))
        except Exception:
            return False


    async def _widget_error_state(self, iframe) -> str:
        """If the hCaptcha widget iframe is showing hCaptcha's OWN error
        banner ("Rate limited or network error. Please retry.") return the
        banner text.

        hCaptcha renders this INSIDE the widget when its backend rejects the
        session (flagged IP, dead circuit, blocked hcaptcha.com API). The
        checkbox exists but is inert -- no click will ever register, because
        hCaptcha never initialized the widget. Returns "" when healthy.
        """
        frame = await self._hcaptcha_frame_for(iframe)
        if frame is None:
            return ""
        try:
            text = await frame.evaluate(
                "() => (document.body ? document.body.innerText : '')")
        except Exception:
            return ""
        low = (text or "").lower()
        for kw in ("rate limited or network error", "rate limited",
                   "network error", "please retry", "please try again",
                   "automated queries"):
            if kw in low:
                return (text or "").strip()[:120]
        return ""

    async def _retry_erroring_widget(self, iframe) -> bool:
        """Click hCaptcha's own retry/refresh control inside the widget frame
        so a transient network error gets one honest second chance.

        The refresh button (.refresh.button) reloads the widget; an explicit
        Retry link also appears in the error state. Returns True if any
        control was clicked.
        """
        frame = await self._hcaptcha_frame_for(iframe)
        if frame is None:
            return False
        try:
            for sel in (".refresh.button", "[aria-label*='Refresh']",
                        "button[aria-label*='Refresh']", "a:has-text('Retry')",
                        "div:has-text('Please try again')"):
                loc = frame.locator(sel).first
                try:
                    await loc.click(timeout=1500, force=True)
                    return True
                except Exception:
                    continue
        except Exception:
            pass
        return False

    async def _click_hcaptcha_checkbox(self, iframe) -> bool:
        """CLICK the hCaptcha 'Are you human' checkbox — always attempts.

        Never gated on the strict readiness probe: if the widget frame
        contains a checkbox node we click it, period. The current hCaptcha
        widget lays the checkbox out in a way getBoundingClientRect()
        reports as 0-sized, so geometry never gates the attempt either —
        the real click point is computed in-page (walking the checkbox's
        subtree, then ancestors, for the first sized element) and translated
        to page coordinates via the iframe's bounding box. Click order:

          1. Real mouse click (CDP input at the point — hCaptcha trusts it)
          2. Keyboard activation (focus the checkbox + Enter/Space — a
             role=checkbox is natively keyboard-activatable, so this works
             with zero coordinate math)
          3. JS el.click() dispatch

        Every attempt is verified against hCaptcha's own signals
        (aria-checked=true flip, challenge iframe spawn, or token) and the
        whole sequence retries up to 5 times.
        """
        frame = await self._hcaptcha_frame_for(iframe)
        if frame is None:
            self._log("[Captcha] Checkbox click skipped — no live hCaptcha frame attached",
                      level="debug")
            return False

        full_src = ""
        try:
            full_src = await iframe.get_attribute("src") or ""
        except Exception:
            full_src = ""
        iframe_src = full_src[:80] or "?"
        self._log(f"[Captcha] Clicking hCaptcha checkbox (iframe: {iframe_src})")

        # Inspect what's actually inside the widget frame (ALL LOGS only).
        try:
            probe = await frame.evaluate("""() => {
                const body = document.body;
                const anyCheckbox = !!document.querySelector(
                    '#checkbox, [role="checkbox"], .checkbox, input[type="checkbox"], [aria-checked], .button-submit');
                const t = (body && body.innerText || '').slice(0, 80);
                return JSON.stringify({
                    ariaHidden: body ? body.getAttribute('aria-hidden') : null,
                    children: body ? body.children.length : -1,
                    anyCheckbox,
                    readyState: document.readyState,
                    text: t.replace(/\s+/g, ' ').trim()
                });
            }""")
            self._log(f"[Captcha] Widget frame probe: {probe}", level="debug")
        except Exception as e:
            self._log(f"[Captcha] Widget frame probe error: {e}", level="debug")

        # ── Verification: only a click hCaptcha actually reacted to counts ──
        # hCaptcha confirms a registered click by flipping the checkbox's
        # aria-checked to "true", spawning the challenge iframe, or writing a
        # token. A locator/force click on a 0-sized or covered element can
        # "succeed" without hCaptcha ever reacting — never claim victory on
        # that. Poll the three signals for ~2s after each attempt.
        async def _confirm(attempt: str) -> bool:
            for _ in range(5):
                try:
                    flipped = await frame.evaluate(
                        "() => { const el = document.querySelector('[aria-checked]');"
                        " return !!el && el.getAttribute('aria-checked') === 'true'; }")
                    if flipped:
                        self._log(f"[Captcha] [OK] Checkbox {attempt} — hCaptcha confirmed (aria-checked=true)")
                        return True
                except Exception:
                    pass
                try:
                    chall = self._page.locator(
                        'iframe[title*="hCaptcha challenge"], '
                        'iframe[src*="hcaptcha-challenge"]')
                    if await chall.count() > 0:
                        self._log(f"[Captcha] [OK] Checkbox {attempt} — hCaptcha confirmed (challenge spawned)")
                        return True
                except Exception:
                    pass
                try:
                    if await read_hcaptcha_token(self._page):
                        self._log(f"[Captcha] [OK] Checkbox {attempt} — hCaptcha confirmed (token present)")
                        return True
                except Exception:
                    pass
                await asyncio.sleep(0.4)
            return False

        # ── Real click point inside the frame (frame-relative coords) ──
        # getBoundingClientRect() can report 0x0 for the checkbox node even
        # when it is painted and interactive; walk the subtree (then the
        # ancestors) for the first sized element and click its center.
        point = None
        try:
            point = await frame.evaluate("""() => {
                const sels = ['[role="checkbox"]', '#checkbox', '.checkbox',
                              'input[type="checkbox"]', '[aria-checked]', '.button-submit'];
                let el = null;
                for (const s of sels) { el = document.querySelector(s); if (el) break; }
                if (!el) return null;
                const sized = (n) => {
                    if (!n) return null;
                    const r = n.getBoundingClientRect();
                    return (r && r.width > 0 && r.height > 0)
                        ? {left: r.left, top: r.top, width: r.width, height: r.height} : null;
                };
                let rect = sized(el);
                if (!rect) {
                    let best = null, bestArea = 0;
                    const walk = (n) => {
                        const r = sized(n);
                        if (r) { const a = r.width * r.height; if (a > bestArea) { best = r; bestArea = a; } }
                        for (const c of n.children) walk(c);
                    };
                    for (const c of el.children) walk(c);
                    if (best) rect = best;
                }
                if (!rect) {
                    let p = el.parentElement;
                    while (p) { const r = sized(p); if (r) { rect = r; break; } p = p.parentElement; }
                }
                if (!rect) return null;
                return {x: rect.left + rect.width / 2, y: rect.top + rect.height / 2,
                        w: rect.width, h: rect.height};
            }""")
        except Exception:
            point = None
        if point:
            self._log(f"[Captcha] Checkbox center (frame coords): ({point['x']:.1f}, {point['y']:.1f}) "
                      f"size {point['w']:.1f}x{point['h']:.1f}", level="debug")

        # Translate frame coords → page coords via the iframe's bounding box.
        page_point = None
        try:
            iframe_box = await iframe.bounding_box()
        except Exception:
            iframe_box = None
        if point and iframe_box and iframe_box.get("width", 0) > 1:
            page_point = (iframe_box["x"] + point["x"], iframe_box["y"] + point["y"])
        elif iframe_box and iframe_box.get("width", 0) > 1:
            # No measurable checkbox rect — hCaptcha renders the checkbox at
            # the widget's left edge, vertically centered. Aim there.
            page_point = (iframe_box["x"] + iframe_box.get("width", 0) * 0.12,
                          iframe_box["y"] + iframe_box.get("height", 0) * 0.5)

        for attempt in range(1, 6):
            if attempt > 1:
                await asyncio.sleep(0.4)

            # Strategy 0: frame_locator click — the engine's reliable
            # cross-origin mechanism (the same one the accessibility solver
            # uses). Playwright resolves the frame lazily and clicks the
            # checkbox center with trusted input, computing all iframe
            # offsets internally. hCaptcha mounts a hidden twin sharing the
            # same src — non-actionable elements just time out and we move
            # to the next checkbox, so the visible widget always gets hit.
            if full_src and "hcaptcha" in full_src:
                try:
                    fl = self._page.frame_locator(f'iframe[src="{full_src}"]')
                    fl_cb = fl.locator(
                        '#checkbox, [role="checkbox"], .checkbox, '
                        'input[type="checkbox"], [aria-checked], .button-submit')
                    for ci in range(min(4, await fl_cb.count())):
                        try:
                            await fl_cb.nth(ci).click(timeout=3000)
                            if await _confirm(f"frame click #{ci} (attempt {attempt})"):
                                return True
                        except Exception:
                            continue
                except Exception as e:
                    self._log(f"[Captcha] frame_locator click failed: {str(e)[:120]}",
                              level="debug")

            # Strategy 1: real mouse click at the computed page point.
            if page_point:
                try:
                    cx, cy = page_point
                    await self._page.mouse.move(cx, cy, steps=2)
                    await asyncio.sleep(random.uniform(0.15, 0.35))
                    await self._page.mouse.click(cx, cy)
                    if await _confirm(f"mouse click (attempt {attempt})"):
                        return True
                except Exception as e:
                    self._log(f"[Captcha] Mouse click failed: {str(e)[:120]}", level="debug")

            # Strategy 2: keyboard activation — role=checkbox is natively
            # activatable via Enter/Space; no coordinates involved.
            try:
                await frame.evaluate("""() => {
                    const el = document.querySelector('[role="checkbox"], #checkbox, .checkbox, [aria-checked], .button-submit');
                    if (el) el.focus();
                }""")
                await asyncio.sleep(0.1)
                await self._page.keyboard.press("Enter")
                if await _confirm(f"keyboard Enter (attempt {attempt})"):
                    return True
                await self._page.keyboard.press("Space")
                if await _confirm(f"keyboard Space (attempt {attempt})"):
                    return True
            except Exception as e:
                self._log(f"[Captcha] Keyboard activation failed: {str(e)[:120]}", level="debug")

            # Strategy 3: JS el.click() — hCaptcha binds click listeners.
            try:
                js_clicked = await frame.evaluate("""() => {
                    const el = document.querySelector('[role="checkbox"], #checkbox, .checkbox, input[type="checkbox"], [aria-checked], .button-submit');
                    if (!el) return false;
                    el.click();
                    return true;
                }""")
                if js_clicked and await _confirm(f"JS click (attempt {attempt})"):
                    return True
            except Exception as e:
                self._log(f"[Captcha] JS click failed: {str(e)[:120]}", level="debug")

        # Nothing registered — dump the frame DOM to ALL LOGS so the user can
        # see exactly what hCaptcha rendered inside the widget.
        try:
            html = await frame.evaluate(
                "() => (document.body ? document.body.outerHTML : '').slice(0, 2000)")
            self._log(f"[Captcha] Checkbox click never confirmed — widget frame DOM:\n{html}",
                      level="debug")
        except Exception as e:
            self._log(f"[Captcha] Widget frame DOM dump failed: {e}", level="debug")
        return False

    async def _dump_captcha_dom(self, reason: str) -> None:
        """Dump the page + every hCaptcha iframe's DOM to the ALL LOGS.

        Debug-level only: visible with LOG_LEVEL=all or the dashboard ALL
        LOGS toggle, never in the default console.
        """
        try:
            url = self._page.url
        except Exception:
            url = "?"
        self._log(f"[DOM] Captcha DOM dump ({reason}) — page: {url[:90]}", level="debug")
        try:
            html = await self._page.evaluate(
                "() => (document.body ? document.body.outerHTML : '').slice(0, 2500)")
            self._log(f"[DOM] Page body:\n{html}", level="debug")
        except Exception as e:
            self._log(f"[DOM] Page body dump failed: {e}", level="debug")
        try:
            for f in self._page.frames:
                if 'hcaptcha' not in (f.url or ''):
                    continue
                try:
                    state = await f.evaluate("document.readyState")
                except Exception:
                    state = "?"
                # Per-iframe probe: aria-hidden + checkbox presence. The body
                # is mostly a giant minified loader script, so also dump the
                # TAIL of the body where the actual UI renders.
                try:
                    info = await f.evaluate("""() => {
                        const b = document.body;
                        const cb = document.querySelector(
                            '#checkbox, [role="checkbox"], .button-submit, input[type="checkbox"], [aria-checked]');
                        return JSON.stringify({
                            ariaHidden: b ? b.getAttribute('aria-hidden') : null,
                            children: b ? b.children.length : -1,
                            checkbox: !!cb,
                            readyState: document.readyState
                        });
                    }""")
                    self._log(f"[DOM] iframe probe {f.url[:70]}: {info}", level="debug")
                except Exception:
                    pass
                try:
                    fhtml = await f.evaluate(
                        "() => { const b = document.body || document.documentElement;"
                        " const h = b.outerHTML || ''; return h.slice(-1500); }")
                except Exception as e:
                    fhtml = f"<dump failed: {e}>"
                self._log(f"[DOM] iframe {f.url[:100]} readyState={state} (tail):\n{fhtml}",
                          level="debug")
        except Exception as e:
            self._log(f"[DOM] iframe dump failed: {e}", level="debug")

    async def _detect_challenge_mode(self, iframe_element) -> str:
        """Identify the hCaptcha state: 'checkbox' or 'drag'.

        A checkbox widget is solvable via the NoCaptchaAI token API. A drag
        puzzle (piece to position) must be solved in-browser with the mouse.
        When there is no checkbox, an active challenge is showing - classify
        it as a drag puzzle so the in-browser solver gets a chance (it
        self-verifies and fast-fails back to the API if nothing is found).
        """
        frame = await self._hcaptcha_frame_for(iframe_element)
        if frame is None:
            return "drag"
        try:
            await frame.wait_for_selector('#checkbox', state='visible', timeout=1500)
            return "checkbox"
        except Exception:
            return "drag"

    async def _try_solve_drag(self, iframe) -> Optional[bool]:
        """Drag puzzles are no longer supported — the accessibility text
        solver is the only solver. Return None so the caller keeps trying.
        """
        self._log("[Captcha] Drag solver removed — using accessibility solver only",
                  level="warn")
        return None

    async def _extract_sitekey_with_retry(self, timeout: float = 15.0,
                                          poll: float = 1.0) -> str:
        """Extract the hCaptcha sitekey, polling until it is valid.

        The captcha iframe mounts before its src carries the sitekey, so
        extracting too early returns a partial/garbage value. Poll every
        `poll` seconds for up to `timeout` seconds and only accept a
        well-formed UUID sitekey (extraction is validated upstream).
        """
        deadline = time.time() + timeout
        attempts = 0
        while True:
            attempts += 1
            sitekey = await extract_hcaptcha_sitekey(self._page)
            if sitekey:
                self._log(f"[Captcha] Sitekey ready (attempt {attempts}): {sitekey[:16]}...")
                return sitekey
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            self._log(f"[Captcha] Sitekey not ready yet (attempt {attempts}) - "
                      f"retrying in {int(min(poll, remaining))}s", level="warn")
            await asyncio.sleep(min(poll, remaining))
        self._log("[Captcha] Sitekey never appeared after retries", level="error")
        return ""

    async def _click_form_submit(self) -> bool:
        """Click Create Account / Continue after the captcha token is in place."""
        try:
            result = await self._page.evaluate("""() => {
                __LOGIN_LINK_GUARD__
                const _norm = (s) => (s || '').toLowerCase().replace(/\s+/g, ' ').trim();
                const btns = document.querySelectorAll('button');
                for (const btn of btns) {
                    if (btn.offsetParent === null) continue;
                    if (btn.disabled || btn.getAttribute('aria-disabled') === 'true') continue;
                    if (__isLoginLink(btn)) continue;
                    const t = _norm(btn.textContent);
                    if (t.includes('create account') || t.includes('continue') || t.includes('sign up')) {
                        btn.scrollIntoView({block: 'center'});
                        btn.click();
                        return t.slice(0, 24);
                    }
                }
                const submit = document.querySelector('[type="submit"]');
                if (submit && submit.offsetParent !== null && submit.closest('form')
                    && !submit.disabled && !__isLoginLink(submit)) {
                    submit.click();
                    return 'submit_btn';
                }
                const form = document.querySelector('form');
                if (form) {
                    // requestSubmit() with no arg activates the form's default
                    // submit button — which is the "Already have an account?"
                    // login link when the real Continue is disabled. Pick a
                    // real, enabled, non-login submit button instead.
                    for (const sb of form.querySelectorAll('button[type="submit"], [type="submit"]')) {
                        if (sb.disabled || sb.getAttribute('aria-disabled') === 'true') continue;
                        if (sb.offsetParent === null) continue;
                        if (__isLoginLink(sb)) continue;
                        if (form.requestSubmit) { form.requestSubmit(sb); return 'requestSubmit'; }
                        sb.click();
                        return 'form_submit_click';
                    }
                }
                return '';
            }""".replace('__LOGIN_LINK_GUARD__', _LOGIN_LINK_GUARD))
            if result:
                self._log(f"[Captcha] [OK] Submit clicked: {result}")
                return True
        except Exception as e:
            self._log(f"[Captcha] submit click error: {e}", level="warn")
        return False

    async def _solve_hcaptcha_if_present(self) -> bool:
        """Detect and solve the hCaptcha challenge.

        Smart detection: polls the DOM every second with real JS introspection.
        Knows the difference between widget-loading, challenge-loading, and
        challenge-ready states. Waits up to 60s for the challenge to fully render.
        """
        try:
            self._log("[Captcha] Checking for hCaptcha...")

            if await self._past_captcha():
                self._log(f"[Captcha] Already past captcha - at {self._page.url[:50]}")
                return True

            # ── Phase 1: Wait for hCaptcha to actually LOAD ──
            # Never fake: readiness is never claimed from static DOM. Two
            # honest paths to ready:
            #   · a rendered challenge iframe (height >= 80) — a challenge is
            #     actively showing, solve it directly;
            #   · the widget iframe with its document loaded — then hand it to
            #     the accessibility solver, which PROVES interactivity by
            #     opening the 3-dots menu before claiming anything.
            # IMPORTANT: we deliberately do NOT click the widget checkbox to
            # "test" readiness — clicking burns an hCaptcha attempt and puts
            # the challenge into a permanent "Please try again" state that
            # can never be solved (verified in the field).
            self._log("[Captcha] Waiting for hCaptcha to load...")
            iframe = None
            loop_start = time.time()
            widget_since = None  # when the widget iframe first appeared
            checkbox_clicked = False    # auto-clicked the hCaptcha checkbox
            checkbox_clicked_at = 0.0   # when the checkbox was clicked
            checkbox_passes = 0         # unconditional click passes run (max 2)
            funcaptcha_checked = False
            honest_logged = {8: False, 20: False}
            # If hCaptcha never loads (dead session / blocked scripts), do
            # not loop forever: rotate after 45s so the worker retries on a
            # fresh circuit.
            no_widget_deadline = loop_start + 45

            while True:
                if await self._past_captcha():
                    self._log(f"[Captcha] Already past captcha — at {self._page.url[:50]}")
                    return True

                # ── Fast-fail: rate limiting (rotate the moment it shows) ──
                try:
                    body = await self._page.evaluate(
                        "() => (document.body ? document.body.innerText : '').toLowerCase()")
                except Exception:
                    body = ""
                if any(k in body for k in _RATE_LIMIT_KEYWORDS):
                    self._log("[Captcha] RATE LIMITED — rotating circuit", level="warn")
                    return False

                # 1) Challenge iframe already rendered → genuinely ready.
                #    NEVER trust size alone: a blank/loading challenge iframe
                #    is laid out at full size before its JS paints. Only claim
                #    [READY] when the frame document is complete AND actually
                #    shows hCaptcha content.
                try:
                    chall = self._page.locator(
                        'iframe[title*="hCaptcha challenge"], iframe[src*="hcaptcha-challenge"]')
                    if await chall.count() > 0:
                        box = await chall.first.bounding_box()
                        if (box and box.get("height", 0) >= 80
                                and await self._challenge_rendered(chall.first)):
                            iframe = chall.first
                            self._log("[Captcha] [READY] hCaptcha challenge already rendered")
                            break
                except Exception:
                    pass

                # 2) Widget present → scan EVERY hCaptcha iframe. Discord
                #    mounts several (a hidden aria-hidden frame plus the real
                #    widget), and widget.first is not always the visible one.
                #    Find the first genuinely-rendered frame, auto-click its
                #    checkbox (user request) so hCaptcha spawns the challenge.
                try:
                    widgets = self._page.locator(
                        'iframe[title="Widget containing checkbox for hCaptcha security challenge"], '
                        'iframe[src*="newassets.hcaptcha.com"], '
                        'iframe[src*="hcaptcha.com"][src*="frame=checkbox"]'
                    )
                    wcount = await widgets.count()
                    if wcount > 0:
                        if widget_since is None:
                            widget_since = time.time()
                            self._log(f"[Captcha] hCaptcha widget present ({wcount} iframes) — waiting for it to initialize...")
                        # Sitekey: extract + log it once per session (user
                        # request — the widget src carries sitekey=).
                        if not getattr(self, "_hcaptcha_sitekey", ""):
                            try:
                                sk = await extract_hcaptcha_sitekey(self._page)
                                if sk:
                                    self._hcaptcha_sitekey = sk
                                    self._log(f"[Captcha] Sitekey: {sk}")
                            except Exception as e:
                                self._log(f"[Captcha] Sitekey extraction error: {e}", level="debug")
                        # Checkbox-only pass: token already present, no
                        # challenge needed.
                        if await read_hcaptcha_token(self._page):
                            self._log("[Captcha] [OK] hCaptcha already solved (token present)")
                            return True
                        # ── WIDGET-ERROR FAST-FAIL (root cause) ──
                        # When the widget itself shows "Rate limited or
                        # network error. Please retry." the checkbox is
                        # INERT -- hCaptcha's backend rejected this session
                        # (flagged IP / dead circuit), so no click will ever
                        # register. Retry the widget once (transient network
                        # errors recover), then rotate immediately instead of
                        # burning the 45s watchdog on a blocked session.
                        widget_err = ""
                        err_wi = -1
                        for wi in range(wcount):
                            widget_err = await self._widget_error_state(widgets.nth(wi))
                            if widget_err:
                                err_wi = wi
                                break
                        if widget_err:
                            self._log(
                                f"[Captcha] hCaptcha widget error: {widget_err!r}",
                                level="warn")
                            retried = await self._retry_erroring_widget(
                                widgets.nth(err_wi))
                            await asyncio.sleep(2.5)
                            still_err = await self._widget_error_state(
                                widgets.nth(err_wi))
                            if still_err:
                                self._log(
                                    f"[Captcha] Widget still erroring after retry "
                                    f"(retried={retried}) — rotating circuit NOW",
                                    level="warn")
                                return False
                            self._log(
                                "[Captcha] Widget recovered after retry — continuing")
                        # ── ALWAYS-CLICK PASS (user request) ──
                        # Click the checkbox UNCONDITIONALLY whenever any
                        # hCaptcha iframe is present — the readiness probes
                        # are informational only and must NEVER gate the
                        # attempt (they can report False via
                        # Locator.content_frame() even when the widget is
                        # interactive; the click helper itself falls back to
                        # matching the frame from the page's frame tree).
                        # `_click_hcaptcha_checkbox` is self-verifying: it
                        # tries mouse → keyboard → JS click and only reports
                        # success on hCaptcha's own signals (aria-checked
                        # flip, challenge spawn, or token). Run 2 full
                        # passes right after "Waiting for hCaptcha to
                        # load..." so a mid-init widget still gets clicked
                        # once it becomes interactive.
                        if not checkbox_clicked and checkbox_passes < 2:
                            checkbox_passes += 1
                            self._log(
                                f"[Captcha] Checkbox click pass {checkbox_passes}/2 (unconditional — widget present)")
                            for wi in range(wcount):
                                w = widgets.nth(wi)
                                if await self._click_hcaptcha_checkbox(w):
                                    checkbox_clicked = True
                                    checkbox_clicked_at = time.time()
                                    self._log("[Captcha] Checkbox clicked — waiting for challenge to spawn...")
                                    break
                        if checkbox_clicked and (time.time() - checkbox_clicked_at) < 5.0:
                            # hCaptcha swaps to the challenge a moment
                            # after the click — the next loop iteration
                            # (0.25s) catches the painted challenge iframe.
                            continue
                        # Hand the first genuinely-rendered widget to the
                        # accessibility solver.
                        rendered_widget = None
                        for wi in range(wcount):
                            w = widgets.nth(wi)
                            if await self._widget_rendered(w):
                                rendered_widget = w
                                break
                        if rendered_widget is None and checkbox_passes >= 2:
                            # Both click passes ran without confirmation and
                            # the readiness probe still fails — hand the
                            # widget off anyway: the accessibility solver
                            # locates the frame itself via frame_locator and
                            # does not need the checkbox clicked.
                            self._log(
                                "[Captcha] Readiness probe failed after 2 click passes — handing widget to accessibility solver anyway",
                                level="warn")
                            rendered_widget = widgets.nth(0)
                        if rendered_widget is not None:
                            iframe = rendered_widget
                            self._log("[Captcha] [READY] hCaptcha widget rendered — opening accessibility challenge")
                            break
                except Exception:
                    pass

                # 3) FunCAPTCHA (Arkose) escape: no hcaptcha after 15s but
                #    captcha-ish text on page → pixel solver.
                elapsed = time.time() - loop_start
                if (not iframe and elapsed > 15.0
                        and not funcaptcha_checked):
                    funcaptcha_checked = True
                    if not any('hcaptcha' in (f.url or '') for f in self._page.frames):
                        try:
                            page_text = await self._page.evaluate(
                                "() => (document.body ? document.body.innerText.substring(0, 500) : '')")
                            low = page_text.lower()
                            if ('captcha' in low or 'security' in low or 'verify' in low):
                                self._log("[Captcha] No hCaptcha frames — trying FunCAPTCHA solver", level="warn")
                                return await self._solve_funcaptcha()
                        except Exception:
                            pass

                # ── Honest progress — never claim to be solving a captcha
                # that hasn't loaded ──
                for threshold, flag in ((8, 8), (20, 20)):
                    if elapsed > threshold and not honest_logged[flag]:
                        honest_logged[flag] = True
                        if widget_since is not None:
                            self._log(
                                f"[Captcha] hCaptcha widget still initializing after {int(elapsed)}s...",
                                level="warn")
                        else:
                            self._log(
                                f"[Captcha] No hCaptcha widget after {int(elapsed)}s — hCaptcha script not loaded yet...",
                                level="warn")
                        await self._dump_captcha_dom(f"stuck at {int(elapsed)}s")

                # Watchdog: hCaptcha never loaded — rotate honestly.
                if time.time() > no_widget_deadline:
                    if widget_since is not None:
                        self._log("[Captcha] hCaptcha widget never became ready in 45s — "
                                  "scripts blocked or session stalled, rotating", level="warn")
                    else:
                        self._log("[Captcha] No hCaptcha widget in 45s — script blocked or dead "
                                  "session, rotating", level="warn")
                    await self._dump_captcha_dom("45s watchdog")
                    return False

                # Fast poll — hCaptcha paints within a few hundred ms of its
                # document completing, so 0.25s catches it almost immediately.
                await asyncio.sleep(0.25)

            if not iframe:
                # No hCaptcha iframe - check for FunCAPTCHA (Arkose) instead
                try:
                    if await self._past_captcha():
                        self._log(f"[Captcha] Registration went through - at {self._page.url[:50]}")
                        return True
                    page_text = await self._page.evaluate(
                        "() => document.body.innerText.substring(0, 500)")
                    has_captcha_text = ('captcha' in page_text.lower()
                                        or 'security' in page_text.lower()
                                        or 'verify' in page_text.lower())
                    if has_captcha_text:
                        self._log("[Captcha] FunCAPTCHA detected - pixel tile solver...")
                        return await self._solve_funcaptcha()
                    self._log(f"[Captcha] No captcha indicators on page: {self._page.url[:40]}", level="warn")
                    return False
                except Exception as e:
                    self._log(f"[Captcha] Captcha check error: {e}", level="warn")
                return False

            # Which challenge is showing?
            mode = await self._detect_challenge_mode(iframe)
            self._log(f"[Captcha] Challenge mode: {mode}")

            # ── ACCESSIBILITY CHALLENGE — THE ONLY SOLVER ──
            # Opens the 3-dots menu and uses the Accessibility Challenge,
            # which gives a text/audio question that's solvable locally
            # (math, word puzzles) with Ollama vision as fallback. Drag
            # challenges included: no page refresh — if it can't be solved
            # the attempt fails fast and the worker rotates proxy +
            # fingerprint + mail domain.
            self._log("[Captcha] Trying accessibility challenge (only solver)...")
            acc_result = await solve_hcaptcha_accessibility(self._page, iframe, log=self._log)
            if not acc_result:
                # The widget may have still been initializing on the first
                # attempt — wait and retry a couple times before rotating.
                for retry_i in range(2):
                    await asyncio.sleep(4)
                    self._log(
                        f"[Captcha] Challenge not solved — retrying accessibility (retry {retry_i + 1}/2)...",
                        level="warn")
                    acc_result = await solve_hcaptcha_accessibility(self._page, iframe, log=self._log)
                    if acc_result:
                        break
            if acc_result:
                self._log("[Captcha] [OK] Accessibility challenge solved!")
                # ── Detect ANY new hCaptcha that appears after a completed one ──
                # hCaptcha chains captchas: right after one finishes, a brand-new
                # challenge can pop up. The WIDGET iframe (newassets.hcaptcha.com)
                # stays in the DOM forever, so we ONLY look for the challenge
                # iframe (title="hCaptcha challenge" / hcaptcha-challenge.html)
                # to avoid re-solving the idle widget.
                solved_srcs = set()
                try:
                    solved_srcs.add(await iframe.get_attribute("src") or "")
                except Exception:
                    pass
                idle_checks = 0
                for check_i in range(4):
                    await asyncio.sleep(1.0)
                    if await self._past_captcha():
                        self._log("[Captcha] Page past captcha — clicking Create Account")
                        await self._click_form_submit()
                        return True
                    new_challenge = None
                    try:
                        new_challenge = await self._page.query_selector(
                            'iframe[title="hCaptcha challenge"], '
                            'iframe[src*="hcaptcha-challenge"]'
                        )
                    except Exception:
                        new_challenge = None
                    if new_challenge:
                        idle_checks = 0
                        try:
                            new_src = await new_challenge.get_attribute("src") or ""
                        except Exception:
                            new_src = ""
                        if new_src in solved_srcs:
                            # Same challenge element still closing — not new.
                            self._log(f"[Captcha] Same challenge still present ({check_i+1}/6)")
                            continue
                        solved_srcs.add(new_src)
                        self._log("[Captcha] NEW captcha detected — clicking 3-dots + accessibility again")
                        acc_result = await solve_hcaptcha_accessibility(
                            self._page, new_challenge, log=self._log
                        )
                        if not acc_result:
                            self._log("[Captcha] Chain captcha failed", level="error")
                            return False
                        continue  # solved — keep checking for the next one
                    idle_checks += 1
                    if idle_checks >= 2:
                        self._log("[Captcha] No new challenge — captcha fully done!")
                        break
                    self._log(f"[Captcha] No challenge yet ({check_i+1}/6)...")
                # Captcha chain finished — proceed to Create Account
                await self._click_form_submit()
                await asyncio.sleep(1.5)
                return True
            else:
                self._log("[Captcha] [FAIL] Accessibility challenge did not solve",
                          level="error")
                await asyncio.sleep(2)
                return False

        except Exception as e:
            self._log(f"[Captcha] Flow error: {e}", level="error")
            import traceback
            traceback.print_exc()
            return False

    async def _solve_funcaptcha(self) -> bool:
        """Solve FunCAPTCHA tile challenges with the offline pixel solver."""
        try:
            task = await extract_funcaptcha_task(self._page)
            if task:
                self._log(f"[FunCAPTCHA] Challenge: {task[:80]}")
            solved = await solve_funcaptcha_pixels(self._page, log=self._log)
            if solved:
                await self._click_form_submit()
                return True
            self._log("[FunCAPTCHA] [FAIL] Could not solve challenge", level="error")
            return False
        except Exception as e:
            self._log(f"[FunCAPTCHA] Error: {e}", level="error")
            import traceback
            traceback.print_exc()
            return False

    async def _form_ready(self) -> dict:
        """Evaluate _FORM_READY_JS with the locale-aware DOB label table."""
        try:
            v = await self._page.evaluate(
                _FORM_READY_JS.replace("__DOB_LABELS__", json.dumps(_DOB_LABEL_ALIASES)))
            return json.loads(v) if v else {}
        except Exception:
            return {}

    async def _select_dob(self, label: str, option_text: str) -> bool:
        """Select DOB dropdown. Discord uses custom React-Select components."""
        try:
            self._log(f"Selecting {label}: {option_text}")

            # The register SPA hydrates the DOB dropdowns a beat after the
            # credential inputs. Poll briefly for a DOB control to exist
            # before attempting any click - clicking a not-yet-rendered
            # control is how the old build typed into the wrong field.
            control_ready = False
            for _probe in range(8):
                try:
                    _st = await self._form_ready()
                except Exception:
                    _st = {}
                if int(_st.get("dob") or 0) >= 1:
                    control_ready = True
                    break
                await asyncio.sleep(0.4)
            if not control_ready:
                self._log(f"[Form] DOB control for {label} not rendered after ~3s — aborting", level="warn")
                return False

            # Strategy 1: JS click on placeholder, then find and click option.
            # Label matching is locale-aware (Dutch "Dag/Maand/Jaar", ...).
            label_re = "|".join(_DOB_LABEL_ALIASES.get(label, [label]))
            success = await self._page.evaluate(f"""
                async () => {{
                    const walker = document.createTreeWalker(
                        document.body, NodeFilter.SHOW_TEXT, null
                    );
                    let node;
                    let targetEl = null;
                    const labelRe = new RegExp('(^|[^a-z0-9])(' + '{label_re}' + ')([^a-z0-9]|$)');
                    while (node = walker.nextNode()) {{
                        if (labelRe.test(node.textContent.trim())) {{
                            const parent = node.parentElement;
                            if (parent && parent.offsetParent !== null &&
                                !parent.querySelector('input[name="email"]')) {{
                                targetEl = parent;
                                break;
                            }}
                        }}
                    }}

                    if (!targetEl) return 'no_element';

                    let clickTarget = targetEl;
                    for (let i = 0; i < 5; i++) {{
                        clickTarget = clickTarget.parentElement;
                        if (!clickTarget) break;
                        const style = window.getComputedStyle(clickTarget);
                        if (style.cursor === 'pointer' ||
                            clickTarget.getAttribute('tabindex') !== null ||
                            clickTarget.className.includes('control') ||
                            clickTarget.className.includes('css-')) {{
                            break;
                        }}
                    }}

                    if (!clickTarget) clickTarget = targetEl;
                    clickTarget.dispatchEvent(new MouseEvent('mousedown', {{bubbles: true, cancelable: true}}));
                    clickTarget.dispatchEvent(new MouseEvent('mouseup', {{bubbles: true, cancelable: true}}));
                    clickTarget.dispatchEvent(new MouseEvent('click', {{bubbles: true, cancelable: true}}));
                    // Options render async after the menu opens — poll for
                    // them instead of scanning once and giving up early.
                    for (let attempt = 0; attempt < 6; attempt++) {{
                        if (attempt > 0) await new Promise(r => setTimeout(r, 300));
                        const allOptions = document.querySelectorAll(
                            '[id*="option"], [role="option"], [class*="option"]'
                        );
                        for (const opt of allOptions) {{
                            const text = opt.textContent.trim();
                            if (text === '{option_text}') {{
                                opt.scrollIntoView({{block: 'nearest'}});
                                opt.dispatchEvent(new MouseEvent('mousedown', {{bubbles: true}}));
                                opt.dispatchEvent(new MouseEvent('mouseup', {{bubbles: true}}));
                                opt.dispatchEvent(new MouseEvent('click', {{bubbles: true}}));
                                return 'selected';
                            }}
                        }}
                    }}
                    return 'option_not_found';
                }}
            """)

            if success and 'selected' in str(success):
                self._log(f"Selected {label}: {option_text} ({success})")
                await asyncio.sleep(0.3)
                return True

            self._log(f"JS result for {label}: {success}")

            # Strategy 2: robust native-<select> / labeled-combobox setter.
            # Never tab-navigation roulette or type into "whatever has focus"
            # — that is how the birth year leaked into the email field.
            try:
                result2 = await self._page.evaluate(_DOB_FALLBACK_JS
                    .replace("__LABEL__", json.dumps(label))
                    .replace("__OPT__", json.dumps(option_text))
                    .replace("__DOB_LABELS__", json.dumps(_DOB_LABEL_ALIASES)))
                if result2 and str(result2).startswith(("native:", "combo:")):
                    self._log(f"Selected {label} ({result2})")
                    await asyncio.sleep(0.3)
                    return True
                self._log(f"DOB fallback for {label}: {result2}")
            except Exception as e:
                self._log(f"DOB fallback error for {label}: {e}", level="warn")

            self._log(f"All DOB strategies failed for {label}", level="warn")
            return False


        except Exception as e:
            self._log_exception(f"DOB error for {label}", e)
            return False

    async def _rate_limited(self) -> bool:
        """True when Discord shows its rate-limit message ("The resource is
        being rate limited.") on the current page. Cheap full-page text
        check so the worker rotates the proxy the instant it renders."""
        try:
            text = await self._page.evaluate(
                "() => (document.body ? document.body.innerText : '')")
        except Exception:
            return False
        low = (text or "").lower()
        return any(k in low for k in _RATE_LIMIT_KEYWORDS)

    async def _wait_for_form_ready(self, timeout: float = 30.0):
        """Wait for the register form to FULLY render before touching it.

        Returns "form" (credential inputs visible), "age_gate" (DOB controls
        up but credentials not shown yet - Discord asks for birthday first on
        some builds), or None (stopped / rate limited / timed out). The ready
        state must HOLD for ~0.5s so React's hydration and value trackers are
        attached before anything is written to a field - writing to a
        not-yet-hydrated input is exactly what made Discord wipe the value on
        its next re-render (the "nothing was filled" bug).
        """
        self._log(f"[Form] Waiting for the full form (email/username/password) to render (up to {timeout:.0f}s)...")
        start = time.time()
        last_log = -1.0
        stable_since = None
        eval_failed_logged = False
        while True:
            if self._stopped.is_set():
                self._nav_error = "stopped by user"
                self._log("[Form] Stopped by user while waiting for the form")
                return None
            if await self._rate_limited():
                self._nav_error = "rate limited (429) by Discord"
                self._log("[Form] RATE LIMITED while waiting for the form", level="warn")
                return None
            elapsed = time.time() - start
            try:
                st = await self._form_ready()
            except Exception as e:
                if not eval_failed_logged:
                    eval_failed_logged = True
                    self._log_exception("[Form] Read form-ready state failed", e)
                st = {}
            email = bool(st.get("email"))
            username = bool(st.get("username"))
            password = bool(st.get("password"))
            dob = int(st.get("dob") or 0)
            dob_text = bool(st.get("dobText"))
            full_form = email and username and password
            age_gate = (not email and not username and not password) and (dob >= 1 or dob_text)
            if full_form or age_gate:
                if stable_since is None:
                    stable_since = time.time()
                if time.time() - stable_since >= 0.5:
                    which = "age_gate" if age_gate else "form"
                    self._log(f"[Form] {'Age gate' if age_gate else 'Full form'} rendered in {elapsed:.1f}s (email={email} user={username} pass={password} dob={dob})")
                    return which
            else:
                stable_since = None
            if elapsed >= last_log + 4.0:
                last_log = elapsed
                self._log(f"[Form] Render wait {int(elapsed)}s: email={email} user={username} pass={password} dob={dob}/3 inputs={st.get('inputs')} buttons={st.get('buttons')}")
            if elapsed >= timeout:
                self._nav_error = f"register form never fully rendered after {int(elapsed)}s"
                self._log(f"[Form] Form never fully rendered after {int(elapsed)}s - rotating", level="warn")
                return None
            await asyncio.sleep(0.3)

    async def _read_form_values(self) -> dict:
        """Authoritative read-back of the register form's credential fields.

        Returns the CURRENT raw values (email/display/username/password plus a
        ToS count) so the filler can confirm a write actually landed — and
        re-check right before Create Account so a Discord re-render that
        wipes a field can never be submitted as if it were still filled."""
        try:
            v = await self._page.evaluate("""() => {
                const g = (sel) => { const e = document.querySelector(sel); return e ? (e.value || '') : ''; };
                return JSON.stringify({
                    email: g('input[name="email"], input[type="email"], input[autocomplete="email"], input[aria-label*="email" i], input[id*="email" i]'),
                    display: g('input[name="global_name"], input[aria-label*="display" i]'),
                    username: g('input[name="username"], input[autocomplete="username"], input[aria-label*="username" i], input[id*="username" i]'),
                    password: g('input[name="password"], input[type="password"], input[autocomplete="new-password"], input[aria-label*="password" i]'),
                    tos: document.querySelectorAll('input[type="checkbox"]:checked, [role="checkbox"][aria-checked="true"], [role="checkbox"][data-state="checked"]').length,
                });
            }""")
            return json.loads(v) if v else {}
        except Exception as e:
            self._log_exception("[Form] Read-back failed", e)
            return {}

    def _credentials_filled(self, vals: dict) -> bool:
        """The three credential fields actually hold the expected values."""
        return (vals.get("email") == self._email
                and vals.get("username") == self._username
                and vals.get("password") == self._password)

    async def _fill_credential_fields(self, display_name: str) -> None:
        """Fill every credential field and keep them filled (self-healing).

        This is the fix for the "email ends up holding the username" loop:
          1. Fill order is username → display → password → EMAIL LAST. Any
             keystroke that leaks (Discord's React re-renders mid-type,
             replaces the target input, so the characters go to whatever
             still has focus) lands in the still-empty email field — and the
             element-targeted email write below overwrites it instead of
             corrupting it.
          2. Primary write is Playwright fill() — native value setter +
             input event, targeted AT the element, with no global-keyboard
             focus dependency. Keystrokes are only a per-field last resort.
          3. After every write every credential field is re-read and any
             leaked/wiped value is healed with another targeted fill().
        """
        fields = [
            ("username", "input[name='username'], input[autocomplete='username'], input[aria-label*='username' i], input[id*='username' i]", self._username or ""),
            ("display", "input[name='global_name'], input[aria-label*='display name' i], input[aria-label*='display' i]", display_name),
            ("password", "input[name='password'], input[type='password'], input[autocomplete='new-password'], input[aria-label*='password' i]", self._password or ""),
            ("email", "input[name='email'], input[type='email'], input[autocomplete='email'], input[aria-label*='email' i], input[id*='email' i]", self._email or ""),
        ]
        for fname, sel, val in fields:
            if not val:
                continue
            for attempt in range(1, 4):
                try:
                    loc = self._page.locator(sel)
                    if (await loc.count()) == 0:
                        self._log(f"[Form] Field '{fname}' not found (attempt {attempt}/3)", level="warn")
                        break
                    el = loc.first
                    if not (await el.is_visible()):
                        self._log(f"[Form] Field '{fname}' not visible yet (attempt {attempt}/3)", level="warn")
                        await asyncio.sleep(0.6)
                        continue
                    try:
                        cur = await el.input_value()
                    except Exception:
                        cur = ""
                    if cur == val:
                        self._log(f"[Form] Field '{fname}' already holds value len={len(val)}")
                        break
                    # 1) Playwright fill() — React-compatible targeted write.
                    try:
                        await el.fill(val)
                        await asyncio.sleep(0.35)
                        try:
                            cur = await el.input_value()
                        except Exception:
                            cur = ""
                    except Exception:
                        cur = ""
                    # 2) Element-targeted keystrokes (only if fill() did not
                    #    stick — real trusted keys are the most human input).
                    if cur != val:
                        try:
                            await el.click()
                            await el.press("Control+A")
                            await el.press_sequentially(val, delay=40 + random.randint(10, 50))
                            await asyncio.sleep(0.45)
                            try:
                                cur = await el.input_value()
                            except Exception:
                                cur = ""
                        except Exception:
                            cur = ""
                    # 3) Native-setter JS write (React-compatible, last resort).
                    if cur != val:
                        try:
                            await self._page.evaluate(
                                """([sel, value]) => {
                                    const el = document.querySelector(sel);
                                    if (!el) return false;
                                    const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set;
                                    setter.call(el, value);
                                    el.dispatchEvent(new Event('input', { bubbles: true }));
                                    el.dispatchEvent(new Event('change', { bubbles: true }));
                                    return true;
                                }""",
                                [sel.split(",")[0].strip(), val],
                            )
                            await asyncio.sleep(0.4)
                        except Exception as e:
                            self._log_exception(f"[Form] Field '{fname}' JS fallback failed", e)
                    try:
                        cur = await el.input_value()
                    except Exception:
                        cur = ""
                    if cur == val:
                        self._log(f"[Form] Field '{fname}' verified: len={len(val)}")
                        break
                    self._log(f"[Form] Field '{fname}' mismatch (attempt {attempt}/3) got_len={len(cur)}", level="warn")
                except Exception as e:
                    self._log_exception(f"[Form] Field '{fname}' fill error", e)
                    break
                # Human pause between fields — reads like a real signup and
                # gives Discord's React time to finish re-rendering.
                await asyncio.sleep(random.uniform(0.4, 0.9))
            # Heal pass: THIS field's write (or its re-render) may have leaked
            # into / wiped another field. Re-fill anything that slipped.
            await self._heal_credential_fields(fields)

    async def _heal_credential_fields(self, fields) -> None:
        """Re-fill any credential field whose value got wiped or leaked."""
        for fname, sel, val in fields:
            if not val:
                continue
            try:
                loc = self._page.locator(sel)
                if (await loc.count()) == 0:
                    continue
                el = loc.first
                if not (await el.is_visible()):
                    continue
                try:
                    cur = await el.input_value()
                except Exception:
                    cur = ""
                if cur == val:
                    continue
                try:
                    await el.fill(val)
                    await asyncio.sleep(0.25)
                except Exception:
                    pass
            except Exception:
                pass

    async def _fill_registration_form(self) -> bool:
        try:
            self._log("=" * 40)
            self._log("FILLING REGISTRATION FORM (direct value set)")
            self._log("=" * 40)
            self._log(f"Email: {self._email}")
            # Humanization: a moment to "read" the form before typing.
            await asyncio.sleep(random.uniform(0.5, 1.3))

            # Rotate the proxy the moment Discord's rate-limit message shows.
            if await self._rate_limited():
                self._nav_error = "rate limited (429) by Discord"
                self._log("[Form] RATE LIMITED — rotating circuit", level="warn")
                return False

            # ── Pre-define DOB so the age-gate JS can use them ──
            month_val = random.randint(1, 12)
            day_val = str(random.randint(1, 28))
            year_val = str(random.randint(1990, 1999))
            months = ['January', 'February', 'March', 'April', 'May', 'June',
                     'July', 'August', 'September', 'October', 'November', 'December']
            month_name = months[month_val - 1]

            # ── Wait for the form (or age gate) to FULLY render first ──
            # The old 6x0.6s body-text poll returned on partial text and let
            # the filler run while React was still hydrating - fields looked
            # present but their handlers/value-trackers weren't attached yet,
            # so writes got wiped and the keystroke fallback typed into
            # whatever half-rendered element had focus. Gate on ACTUAL
            # visible inputs (+ DOB controls) before touching anything.
            phase = await self._wait_for_form_ready(timeout=30.0)
            if phase == "age_gate":
                self._log("[Form] Age gate detected — setting DOB before the main form...")
                await self._select_dob("Month", month_name)
                await asyncio.sleep(0.3)
                await self._select_dob("Day", day_val)
                await asyncio.sleep(0.3)
                await self._select_dob("Year", year_val)
                await asyncio.sleep(0.6)
                phase = await self._wait_for_form_ready(timeout=20.0)
            if phase != "form":
                self._log("[Form] Form never fully rendered — aborting fill", level="warn")
                return False

            # ── Generate credentials ──
            consonants = 'bcdfghjklmnpqrstvwxyz'
            vowels = 'aeiou'
            username = ''
            for _ in range(random.randint(8, 12)):
                username += random.choice(vowels if random.random() < 0.35 else consonants)
            username += str(random.randint(100, 9999))
            self._username = username
            display_name = self._username[:15]

            first = random.choice('ABCDEFGHJKLMNPQRSTUVWXYZ')
            body = ''
            for _ in range(random.randint(8, 11)):
                body += random.choice(vowels if random.random() < 0.35 else consonants)
            specials = '!@#$%&*'
            self._password = first + body + random.choice(specials) + str(random.randint(1, 99))

            self._log(f"Display: {display_name}  Username: {self._username}  Pass: ***")

            # ── Fill ALL fields, one at a time, self-healing ──
            # Discord's React controlled inputs wipe synthetic JS value sets
            # (the old "all fields :ok but readback empty" failure), and
            # keystrokes typed while React is re-rendering leak into whatever
            # element still has focus — the "email ends up holding the
            # username" loop. The fill order is username → display → password
            # → EMAIL LAST: any leak lands in the still-empty email field and
            # the element-targeted email write overwrites it instead of
            # looping forever. After every write every field is re-verified
            # and leaks are healed.
            await self._fill_credential_fields(display_name)

            # ── Quick verify each field ──
            try:
                vals = await self._read_form_values()
                if self._credentials_filled(vals):
                    self._log("[Form] All credential fields verified OK (ToS clicked separately)")
                else:
                    safe = {
                        "email": vals.get("email", ""),
                        "display": vals.get("display", ""),
                        "username": vals.get("username", ""),
                        "password_len": len(vals.get("password") or ""),
                        "tos": vals.get("tos", 0),
                    }
                    self._log(
                        f"[Form] VERIFY MISMATCH: readback={json.dumps(safe)} "
                        f"expected={json.dumps({'email': self._email, 'username': self._username, 'password_len': len(self._password or '')})}",
                        level="warn",
                    )
                    # ── Playwright fill() + keystroke fallback ──
                    # If React wiped any field, type it with REAL keystrokes
                    # so the form is genuinely filled before we press Create
                    # Account (never fake). Dump EVERY input on the page
                    # (type/name/aria/value/visibility) so the exact failure
                    # is visible in ALL LOGS.
                    try:
                        dump = await self._page.evaluate("""() => {
                            return JSON.stringify(Array.from(document.querySelectorAll('input')).map(function(e) {
                                return {
                                    type: e.type || '',
                                    name: e.name || '',
                                    id: e.id || '',
                                    aria: e.getAttribute('aria-label') || '',
                                    placeholder: e.placeholder || '',
                                    value: e.value || '',
                                    visible: e.offsetParent !== null
                                };
                            }));
                        }""")
                        self._log(f"[Form] INPUT DUMP: {dump}", level="warn")
                    except Exception as _de:
                        self._log_exception("[Form] INPUT DUMP failed", _de)
                    await self._fill_missing_fields(display_name)
                    # Re-read AFTER the fallback. If Discord's React still
                    # won't hold the values, rotating beats faking a Create
                    # Account submit on an empty form.
                    vals = await self._read_form_values()
                    if not self._credentials_filled(vals):
                        self._log(
                            "[Form] [FAIL] Fields still empty after JS + Playwright "
                            "fallback — aborting (never submit a blank form): "
                            + json.dumps({
                                "email": vals.get("email", ""),
                                "username": vals.get("username", ""),
                                "password_len": len(vals.get("password") or ""),
                            }),
                            level="error",
                        )
                        await self.capture_screenshot()
                        return False
                    self._log("[Form] Credential fields OK after fallback")
            except Exception as e:
                self._log_exception("[Form] Verify read-back failed", e)

            # ── DOB ──
            self._log(f"DOB: {month_name} {day_val}, {year_val}")
            await self._select_dob("Month", month_name)
            await self._human_pause()
            await self._select_dob("Day", day_val)
            await self._human_pause()
            await self._select_dob("Year", year_val)
            await self._human_pause()

            await asyncio.sleep(1.0)

            # ── VERIFY ToS is actually checked before trying Create Account ──
            try:
                verify = await self._page.evaluate("""() => {
                    const cbs = document.querySelectorAll('input[type="checkbox"]');
                    let checked = 0;
                    for (const cb of cbs) {
                        if (cb.checked) checked++;
                    }
                    const roleCbs = document.querySelectorAll('[role="checkbox"][aria-checked="true"]');
                    return { native: checked, role: roleCbs.length };
                }""")
                self._log(f"[Form] Checkbox state: native={verify.get('native',0)} role={verify.get('role',0)}")
            except Exception as e:
                self._log_exception("[Form] Checkbox state read failed", e)

            # ── REAL ToS click — ONE pass, exactly one click per box ──
            # Without a genuinely checked ToS, "Continue" stays disabled and
            # the run can fall through to the login link. Real mouse clicks,
            # once per checkbox — never re-click (that toggles it back off).
            n = await self._click_tos_checkboxes()
            if n > 0:
                self._log("[Form] ToS checkbox(es) verified checked")

            # ── FINAL gate: never submit an empty form ──
            # DOB selection + the ToS clicks make Discord's React re-render,
            # which is exactly when a value it silently dropped reappears
            # empty. Read the fields one last time; if anything is missing,
            # rotate instead of "faking" a Create Account on a blank form.
            final_vals = await self._read_form_values()
            if not self._credentials_filled(final_vals):
                self._log(
                    "[Form] [FAIL] Fields empty right before Create Account — "
                    "aborting instead of faking a submit: "
                    + json.dumps({
                        "email": final_vals.get("email", ""),
                        "username": final_vals.get("username", ""),
                        "password_len": len(final_vals.get("password") or ""),
                    }),
                    level="error",
                )
                await self.capture_screenshot()
                return False

            # ── Create Account Button — try multiple strategies ────────
            # Humanization: pause to review the filled form before submitting.
            await asyncio.sleep(random.uniform(0.9, 1.9))
            self._log("Clicking Create Account...")
            create_clicked = False

            for click_attempt in range(4):
                if create_clicked:
                    break
                if click_attempt > 0:
                    self._log(f"Retrying Create Account click (attempt {click_attempt+1}/4)...")
                    # Re-check the REQUIRED ToS checkbox on retry (React may
                    # have reset it) — real mouse click, never the optional
                    # marketing box or a styled container div.
                    try:
                        target = await self._page.evaluate(_TOS_TARGET_JS)
                        if target:
                            await self._page.mouse.click(target["x"], target["y"])
                            self._log("[Form] Re-checked ToS checkbox on retry")
                    except Exception as e:
                        self._log_exception("[Form] ToS re-check on retry failed", e)
                    await asyncio.sleep(1.0)  # Let React process

                try:
                    result = await self._page.evaluate("""() => {
                        __LOGIN_LINK_GUARD__
                        const _norm = (s) => (s || '').toLowerCase().replace(/\s+/g, ' ').trim();
                        // Strategy 1: Find button by text content (most reliable)
                        const btns = document.querySelectorAll('button, [role="button"], [type="submit"]');
                        for (const btn of btns) {
                            if (btn.offsetParent === null) continue;
                            // Check if disabled
                            if (btn.disabled || btn.getAttribute('aria-disabled') === 'true') continue;
                            // Never the "Already have an account?" / back-to-login
                            // link — Discord labels it with non-breaking spaces /
                            // split spans, so normalize whitespace and also check
                            // aria-label / title / value before trusting any text.
                            if (__isLoginLink(btn)) continue;
                            const t = _norm(btn.textContent);
                            const v = _norm(btn.value);
                            if (t.includes('create account') || t.includes('sign up') || t.includes('continue') ||
                                v.includes('create account') || v.includes('sign up')) {
                                btn.scrollIntoView({block: 'center'});
                                btn.click();
                                return 'btn_' + t.slice(0, 20);
                            }
                        }

                        // Strategy 2: real submit button — but NEVER a
                        // navigation button like "Already have an account?"
                        // (it's a type=submit button that navigates to /login
                        // and silently kills the run). Require an actual
                        // type="submit" inside a form + no login text.
                        for (const btn of btns) {
                            if (btn.offsetParent === null) continue;
                            if (btn.disabled || btn.getAttribute('aria-disabled') === 'true') continue;
                            if (__isLoginLink(btn)) continue;
                            if (btn.getAttribute('type') !== 'submit') continue;
                            const t = _norm(btn.textContent);
                            if (!btn.closest('form')) continue;
                            if (t.length > 2) {  // has meaningful text
                                btn.scrollIntoView({block: 'center'});
                                btn.click();
                                return 'btntype_' + t.slice(0, 20);
                            }
                        }

                        // Strategy 3: Form submit — but NEVER let the default
                        // submit button be the "Already have an account?" login
                        // link: requestSubmit() with no argument activates the
                        // form's default submit button, which IS the login link
                        // whenever the real Continue button is disabled. Pick a
                        // real, enabled, non-login submit button explicitly.
                        const forms = document.querySelectorAll('form');
                        for (const form of forms) {
                            if (form.offsetParent === null) continue;
                            for (const sb of form.querySelectorAll('button[type="submit"], [type="submit"]')) {
                                if (sb.disabled || sb.getAttribute('aria-disabled') === 'true') continue;
                                if (sb.offsetParent === null) continue;
                                if (__isLoginLink(sb)) continue;
                                if (form.requestSubmit) {
                                    form.requestSubmit(sb);
                                    return 'form_requestSubmit';
                                }
                                sb.click();
                                return 'form_submit_click';
                            }
                        }

                        return 'failed';
                    }""".replace('__LOGIN_LINK_GUARD__', _LOGIN_LINK_GUARD))
                    if result and result != 'failed':
                        create_clicked = True
                        self._log(f"[OK] Account button clicked: {result}")
                        break
                except Exception as e:
                    self._log(f"Create Account JS attempt {click_attempt+1} error: {e}", level="warn")

                # Playwright fallback: click the button directly
                if not create_clicked:
                    try:
                        btn_selectors = [
                            'button:has-text("Create Account")',
                            'button:has-text("Sign Up")',
                            'button:has-text("Continue")',
                            'button[type="submit"]',
                        ]
                        for sel in btn_selectors:
                            try:
                                btn = self._page.locator(sel).first
                                if await btn.count() > 0:
                                    is_disabled = await btn.is_disabled()
                                    if not is_disabled:
                                        # Never the "Already have an account?" /
                                        # back-to-login link — it navigates to
                                        # /login and silently kills the run.
                                        try:
                                            _txt = (await btn.inner_text() or "").lower()
                                        except Exception:
                                            _txt = ""
                                        try:
                                            _aria = (await btn.get_attribute("aria-label") or "").lower()
                                        except Exception:
                                            _aria = ""
                                        # Normalize whitespace — Discord labels the
                                        # login link with non-breaking spaces, so a
                                        # plain substring match misses it.
                                        _txt_norm = " ".join((_txt + " " + _aria).split())
                                        if any(k in _txt_norm for k in ("already have an account", "log in", "login", "sign in", "back to", "forgot")):
                                            self._log(f"[Form] Skipping fallback {sel} ({_txt_norm[:24]}) — login link", level="warn")
                                            continue
                                        await btn.scroll_into_view_if_needed()
                                        await btn.click()
                                        self._log(f"[OK] Playwright click: {sel}")
                                        create_clicked = True
                                        break
                            except Exception:
                                continue
                    except Exception as pw_e:
                        self._log(f"Playwright button click error: {pw_e}", level="warn")

                # Last resort: Enter key on password field — but ONLY when
                # the form's default submit button is not the "Already have
                # an account?" login link (Enter triggers implicit submission
                # via the default submit button; when the real Continue is
                # disabled, that default IS the login link and would send the
                # run to /login).
                if not create_clicked:
                    try:
                        safe_enter = bool(await self._page.evaluate("""() => {
                            __LOGIN_LINK_GUARD__
                            const form = document.querySelector('form');
                            if (!form) return false;
                            for (const sb of form.querySelectorAll('button[type="submit"], [type="submit"]')) {
                                if (sb.disabled || sb.getAttribute('aria-disabled') === 'true') continue;
                                if (sb.offsetParent === null) continue;
                                if (__isLoginLink(sb)) continue;
                                return true;
                            }
                            return false;
                        }""".replace('__LOGIN_LINK_GUARD__', _LOGIN_LINK_GUARD)))
                    except Exception as e:
                        self._log_exception("[Form] Enter-safety check failed", e)
                        safe_enter = False
                    if safe_enter:
                        try:
                            await self._page.locator('input[name="password"]').press('Enter')
                            self._log("Pressed Enter on password field")
                            create_clicked = True
                        except Exception as e:
                            self._log_exception("[Form] Enter key fallback failed", e)

            if create_clicked:
                self._log("[OK] Create Account submitted - waiting for response...")
            else:
                self._log("[FAIL] Could not click Create Account after all attempts!", level="error")
                await self.capture_screenshot()
                return False

            # Rotate the moment Discord answers with a rate-limit message.
            if await self._rate_limited():
                self._nav_error = "rate limited (429) by Discord"
                self._log("[Form] RATE LIMITED after submit — rotating circuit", level="warn")
                await self.capture_screenshot()
                return False

            await asyncio.sleep(2)
            await self.capture_screenshot()

            return True


        except Exception as e:
            self._log_exception("Form filling error", e)
            return False

    async def _click_tos_checkboxes(self) -> int:
        """Real-mouse-click Discord's REQUIRED ToS checkbox and VERIFY React
        registered it (the check must survive a re-render).

        The JS fill forges cb.checked + aria attributes — React ignores
        forged events and Discord's DOB re-render wipes them, leaving
        "Continue" disabled (which is how a run ends up clicking "Already
        have an account?"). Real Playwright mouse clicks at the checkbox's
        center generate trusted input React actually processes.

        Only the Terms-of-Service box is clicked: real controls only
        (native input / role=checkbox / data-state), never styled container
        divs that also match [class*="checkbox"], and never the optional
        marketing/"email updates" box. Fast — one real click, ~80ms between
        retries. Verifies with the definitive signal: boxes checked, OR
        the Continue button enabled (a genuinely checked ToS is what
        enables it).
        """
        clicked = 0
        # Fast: only the required ToS box, real clicks, ~80ms between
        # retries. Re-query each pass — an already-checked box is skipped,
        # so a registered click is never toggled back off.
        for _attempt in range(3):
            try:
                target = await self._page.evaluate(_TOS_TARGET_JS)
            except Exception:
                target = None
            if not target:
                break
            try:
                await self._page.mouse.click(target["x"], target["y"])
                clicked += 1
                await asyncio.sleep(0.08)
            except Exception:
                break
        verified = 0
        try:
            verified = int(await self._page.evaluate("""() => document.querySelectorAll(
                'input[type="checkbox"]:checked, [role="checkbox"][aria-checked="true"], [role="checkbox"][data-state="checked"]').length""") or 0)
        except Exception:
            pass
        continue_enabled = False
        try:
            continue_enabled = bool(await self._page.evaluate("""() => {
                for (const b of document.querySelectorAll('button')) {
                    const t = (b.textContent || '').toLowerCase().replace(/\s+/g, ' ').trim();
                    if (!(t.includes('continue') || t.includes('create account'))) continue;
                    if (b.disabled || b.getAttribute('aria-disabled') === 'true') continue;
                    return true;
                }
                return false;
            }"""))
        except Exception:
            pass
        self._log(f"[Form] ToS checkboxes: clicked {clicked}, verified {verified}, continue_enabled={continue_enabled}")
        if verified > 0 or continue_enabled:
            return max(verified, 1)
        return 0

    async def _fill_missing_fields(self, display_name: str) -> None:
        """Robust fallback for fields a write didn't keep.

        Delegates to the self-healing credential fill: fill order ends with
        email (so any keystroke leak is overwritten by the email write) and
        every field is re-verified + healed after each write.
        """
        await self._fill_credential_fields(display_name)

    async def _human_pause(self) -> None:
        await asyncio.sleep(random.uniform(0.08, 0.2))

    async def live_camera_loop(self, interval: int = 4) -> None:
        while True:
            await self.capture_screenshot()
            await asyncio.sleep(interval)

    async def _extract_token(self, attempts: int = 4,
                             poll_rounds: int = 10) -> str:
        """Login to Discord with the created account and grab the FULL token
        from localStorage. Discord stores it under 'token'.

        poll_rounds x 2s bounds the wait (20s default); pass a larger value
        (e.g. 30 = 60s) when a custom email needs manual verification before
        the login unlocks."""
        if not (self._email and self._password):
            return ""
        try:
            for i in range(attempts):
                try:
                    await self._page.goto("https://discord.com/login",
                                          wait_until="domcontentloaded",
                                          timeout=NAV_TIMEOUT_MS)
                    break
                except Exception:
                    await asyncio.sleep(2)
            await asyncio.sleep(1.5)
            try:
                email_input = self._page.locator('input[name="email"]').first
                await email_input.fill(self._email, timeout=8000)
                pw_input = self._page.locator('input[name="password"]').first
                await pw_input.fill(self._password, timeout=8000)
                await pw_input.press("Enter")
                self._log("[Token] Submitted login form")
            except Exception as e:
                self._log(f"[Token] Login fill error: {e}", level="warn")
                return ""
            # Wait for token to appear (abort early if phone-gated at login).
            # The React login form can eat the first Enter on a cold load, so
            # re-submit once after ~8s if the token still hasn't landed.
            resubmitted = False
            for round_i in range(poll_rounds):
                await asyncio.sleep(2.0)
                try:
                    if await self._detect_phone_verification():
                        self.phone_verify_detected = True
                        self._log("[Phone] [DETECTED] login gated by phone verification", level="warn")
                        return ""
                except Exception:
                    pass
                try:
                    token = await self._page.evaluate(
                        "() => localStorage.getItem('token') || ''"
                    )
                    if token and len(token) > 20:
                        return token.strip()
                except Exception:
                    pass
                if not resubmitted and round_i >= 3:
                    resubmitted = True
                    self._log("[Token] No token yet - re-submitting login form", level="warn")
                    try:
                        await self._page.evaluate("""() => {
                            const f = document.querySelector('form');
                            const btn = document.querySelector('button[type="submit"]');
                            if (f && f.requestSubmit) { f.requestSubmit(); return 'submitted'; }
                            if (btn) { btn.click(); return 'clicked'; }
                            return 'none';
                        }""")
                    except Exception:
                        pass
            return ""
        except Exception as e:
            self._log(f"[Token] extract error: {e}", level="warn")
            return ""

    def get_account(self) -> dict:
        """Return the generated account info (email, user, pass, full token)."""
        return {
            "email": self._email,
            "username": self._username,
            "password": self._password,
            "token": self._token,
            "proxy": self.proxy,
            "worker_id": self.worker_id,
            "user_id": self._user_id,
            "avatar": self._avatar_data,
            "bio": self._bio,
            "humanized": self._humanized,
            "domain": self._domain,
        }

    async def close(self) -> None:
        if self._mail:
            try:
                await self._mail.close()
            except Exception:
                pass
            self._mail = None
        if self._page:
            try:
                await self._page.close()
            except:
                pass
            self._page = None
        if self._context:
            try:
                await self._context.close()
            except:
                pass
            self._context = None
        if self._browser:
            try:
                await self._browser.close()
            except:
                pass
            self._browser = None
        if self._playwright:
            try:
                await self._playwright.stop()
            except:
                pass
            self._playwright = None

    def get_screenshots(self) -> list:
        return self._screenshots

    def get_latest_screenshot(self) -> str:
        if self._screenshots:
            return self._screenshots[-1]
        return ""


async def run_discord_automation():
    # Standalone CLI path — use a residential session when available
    # (vaultproxies.txt / VAULTPROXY_* env), TOR otherwise.
    proxy = None
    try:
        from proxies import pool as _proxy_pool
        if _proxy_pool.count == 0:
            await _proxy_pool.refresh()
        if _proxy_pool.count > 0:
            proxy = _proxy_pool.take()
            print(f"[CLI] Using proxy session: {proxy.get('key', '?')[:48]}...", flush=True)
    except Exception as e:
        print(f"[CLI] Proxy pool unavailable ({e}) — using TOR", flush=True)
    bot = DiscordAutomation(headless=True, proxy=proxy)
    try:
        await bot.initialize()
        success = await bot.start_discord_signup()
        if success:
            print("[OK] Discord automation completed")
        else:
            print("[FAIL] Discord automation failed")
        await asyncio.sleep(5)
    finally:
        await bot.close()


if __name__ == "__main__":
    asyncio.run(run_discord_automation())

