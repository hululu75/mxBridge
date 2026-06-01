from __future__ import annotations

import asyncio
import getpass
import json
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

SSO_TOKEN_FILE = ".sso_token.json"

_SSO_SELECTORS = [
    "button[data-testid='sso-button']",
    "button:has-text('SSO')",
    "button:has-text('Single Sign-On')",
    "button:has-text('Sign in with Single Sign-On')",
    "button:has-text('Sign in with SSO')",
    "button:has-text('Enterprise SSO')",
    "button:has-text('OIDC')",
    "a:has-text('SSO')",
    "a:has-text('Single Sign-On')",
]

_NEXT_SELECTORS = [
    "button:has-text('Next')",
    "button:has-text('Get started')",
    "button:has-text('Continue')",
    "button[data-testid='next-btn']",
]

_SUBMIT_SELECTORS = [
    "button[data-testid='login-btn']",
    "button[type='submit']",
    "input[type='submit']",
    "button:has-text('Log in')",
    "button:has-text('Login')",
    "button:has-text('Sign in')",
    "button:has-text('Sign In')",
]

_CONSENT_SELECTORS = [
    "button[type='submit']",
    "button:has-text('Allow')",
    "button:has-text('Accept')",
    "button:has-text('Grant')",
    "button:has-text('Consent')",
    "button:has-text('Approve')",
    "button:has-text('Authorize')",
    "button:has-text('Continue')",
    "button:has-text('Yes')",
]

_INPUT_SELECTORS = {
    "username": ["#username", "input[name='username']", "input[id*='username']", "input[type='text']"],
    "password": ["#password", "input[name='password']", "input[type='password']"],
    "otp": [
        "#otp", "input[name='otp']", "input[id*='otp']",
        "input[id*='totp']", "input[name='totp']",
        "input[autocomplete='one-time-code']",
        "input[inputmode='numeric']",
    ],
}

_SKIP_SELECTORS = [
    "button:has-text('Skip')",
    "button:has-text('Skip for now')",
    "button:has-text('Later')",
    "button:has-text('Not now')",
    "button:has-text('Use another device')",
]

_RK_INPUT_SELECTORS = [
    "input[placeholder*='recovery']",
    "input[placeholder*='Recovery']",
    "input[placeholder*='key']",
    "input[placeholder*='Key']",
    "input[placeholder*='phrase']",
    "input[placeholder*='Phrase']",
    "input[type='text']",
    "input[type='password']",
    "textarea",
]

_RK_SUBMIT_SELECTORS = [
    "button[type='submit']",
    "button:has-text('Continue')",
    "button:has-text('Verify')",
    "button:has-text('Submit')",
]


def _load_cached_token(homeserver: str) -> Optional[dict]:
    path = os.path.join(os.getcwd(), SSO_TOKEN_FILE)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r") as f:
            data = json.load(f)
        if data.get("homeserver") != homeserver:
            return None
        if data.get("device_id") and data.get("access_token"):
            return data
    except Exception:
        pass
    return None


def _save_cached_token(homeserver: str, access_token: str, device_id: str) -> None:
    path = os.path.join(os.getcwd(), SSO_TOKEN_FILE)
    data = {
        "homeserver": homeserver,
        "access_token": access_token,
        "device_id": device_id,
    }
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


async def sso_login(
    homeserver: str,
    element_url: str,
    user_id: str,
    device_id: str = "",
    username: str = "",
    password: str = "",
) -> tuple[str, str]:
    from playwright.async_api import async_playwright

    if not username:
        username = input(f"[sso] Username for {user_id}: ").strip()
    if not password:
        password = getpass.getpass(f"[sso] Password for {username}: ")
    if not username or not password:
        raise RuntimeError("Username and password are required")

    token = None
    device = device_id

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        try:
            token, device = await asyncio.wait_for(
                _do_sso_flow(page, element_url, username, password, user_id, device_id),
                timeout=180,
            )
        except Exception:
            await _debug_screenshot(page)
            raise

        if not token:
            await _debug_screenshot(page)

        await browser.close()

    if not token:
        raise RuntimeError("SSO login failed: no access_token obtained")

    _save_cached_token(homeserver, token, device)
    logger.info("[sso] Login successful, device_id=%s", device)
    return token, device


async def _do_sso_flow(
    page,
    element_url: str,
    username: str,
    password: str,
    user_id: str,
    device_id: str,
) -> tuple[Optional[str], str]:
    token = None
    device = device_id

    async def on_response(response):
        nonlocal token, device
        url = response.url
        try:
            if "access_token=" in url or "login_token=" in url:
                from urllib.parse import urlparse, parse_qs
                qs = parse_qs(urlparse(url).fragment if "#" in url else urlparse(url).query)
                for key in ("access_token", "login_token", "token"):
                    if key in qs:
                        token = qs[key][0]
                        break
            if "/_matrix/client/" in url:
                try:
                    body = await response.text()
                    data = json.loads(body)
                    if "access_token" in data:
                        token = data["access_token"]
                        device = data.get("device_id", device)
                except Exception:
                    pass
        except Exception:
            pass

    page.on("response", on_response)

    login_url = element_url.rstrip("/") + "/#/login"
    logger.info("[sso] Opening %s ...", login_url)
    await page.goto(login_url, wait_until="networkidle")
    await asyncio.sleep(3)

    for _round in range(15):
        current_url = page.url
        logger.info("[sso] round %d URL: %s", _round, current_url)

        if "keycloak" in current_url.lower() or "auth" in current_url.lower():
            await _fill_keycloak_form(page, username, password)
            break

        if "totp" in current_url.lower() or "otp" in current_url.lower():
            totp_code = getpass.getpass("[sso] TOTP verification code: ").strip()
            await _submit_totp(page, totp_code)
            break

        for sel in _NEXT_SELECTORS:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=500):
                    await btn.click()
                    logger.info("[sso] Clicked: %s", sel)
                    await asyncio.sleep(2)
                    break
            except Exception:
                continue

        for sel in _SSO_SELECTORS:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=500):
                    await btn.click()
                    logger.info("[sso] Clicked: %s", sel)
                    await asyncio.sleep(3)
                    break
            except Exception:
                continue

        for sel in _SUBMIT_SELECTORS:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=500):
                    await btn.click()
                    logger.info("[sso] Clicked: %s", sel)
                    await asyncio.sleep(3)
                    break
            except Exception:
                continue

        await asyncio.sleep(2)

    if not token:
        await _handle_post_login_page(page)

    if not token:
        logger.info("[sso] Waiting for access_token from network requests...")
        for _ in range(30):
            await asyncio.sleep(2)
            if token:
                break

    if not token:
        token = await _extract_token_from_browser(page)

    return token, device


async def _handle_post_login_page(page) -> None:
    current_url = page.url
    logger.info("[sso] Post-login URL: %s", current_url)

    for i in range(15):
        await asyncio.sleep(2)
        new_url = page.url
        logger.info("[sso] post-login check %d: %s", i, new_url)

        if "consent" in new_url.lower():
            await _handle_consent_page(page)
            continue
        if "web.collab" in new_url or "element" in new_url:
            logger.info("[sso] Redirected back to Element")
            await asyncio.sleep(5)
            break
        if "login.collab" in new_url:
            logger.info("[sso] At callback proxy, waiting...")

    await page.wait_for_load_state("networkidle")
    await _debug_screenshot(page)

    page_text = ""
    try:
        page_text = await page.evaluate("() => document.body?.innerText?.substring(0, 1000) || ''")
    except Exception:
        pass

    if "recovery key" in page_text.lower() or "confirm your digital identity" in page_text.lower():
        logger.info("[sso] Device verification page detected")
        use_rk = page.locator("button:has-text('Use recovery key'), a:has-text('Use recovery key')")
        try:
            if await use_rk.first.is_visible(timeout=1000):
                await use_rk.first.click()
                logger.info("[sso] Clicked 'Use recovery key'")
                await asyncio.sleep(2)
        except Exception:
            pass

        rk_clicked = False
        for sel in _RK_INPUT_SELECTORS:
            try:
                el = page.locator(sel).first
                if await el.is_visible(timeout=500):
                    break
            except Exception:
                continue

        recovery_key = input("[sso] Enter recovery key (or press Enter to skip): ").strip()
        if recovery_key:
            for sel in _RK_INPUT_SELECTORS:
                try:
                    el = page.locator(sel).first
                    if await el.is_visible(timeout=500):
                        await el.fill(recovery_key)
                        rk_clicked = True
                        break
                except Exception:
                    continue
            if rk_clicked:
                for sel in _RK_SUBMIT_SELECTORS:
                    try:
                        btn = page.locator(sel).first
                        if await btn.is_visible(timeout=500):
                            await btn.click()
                            logger.info("[sso] Submitted recovery key")
                            await asyncio.sleep(5)
                            await page.wait_for_load_state("networkidle")
                            break
                    except Exception:
                        continue

        skip_clicked = False
        for sel in _SKIP_SELECTORS:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=500):
                    await btn.click()
                    logger.info("[sso] Clicked skip: %s", sel)
                    skip_clicked = True
                    await asyncio.sleep(3)
                    break
            except Exception:
                continue

        if not skip_clicked and not rk_clicked:
            remove_btn = page.locator("button:has-text('Use another device'), a:has-text('Use another device')")
            try:
                if await remove_btn.first.is_visible(timeout=500):
                    await remove_btn.first.click()
                    logger.info("[sso] Clicked 'Use another device' to skip")
                    await asyncio.sleep(3)
            except Exception:
                pass

        await asyncio.sleep(3)
        await _debug_screenshot(page)


async def _extract_token_from_browser(page) -> Optional[str]:
    try:
        result = await page.evaluate("""() => {
            try {
                const d = localStorage.getItem('matrix-sdk-session');
                if (d) {
                    const parsed = JSON.parse(d);
                    if (parsed.accessToken) return parsed.accessToken;
                }
            } catch(e) {}
            try {
                const keys = Object.keys(localStorage);
                for (const k of keys) {
                    try {
                        const v = JSON.parse(localStorage.getItem(k));
                        if (v && v.accessToken) return v.accessToken;
                        if (v && v.access_token) return v.access_token;
                    } catch(e) {}
                }
            } catch(e) {}
            try {
                const mx = window.matrixClient;
                if (mx && mx.getAccessToken) return mx.getAccessToken();
                if (mx && mx._http && mx._http.opts) return mx._http.opts.accessToken;
            } catch(e) {}
            return null;
        }""")
        if result:
            logger.info("[sso] Got token from localStorage/matrixClient")
            return result
    except Exception:
        pass

    try:
        result = await page.evaluate("""async () => {
            try {
                const dbs = await indexedDB.databases();
                for (const dbInfo of dbs) {
                    try {
                        const idb = await new Promise((resolve, reject) => {
                            const req = indexedDB.open(dbInfo.name);
                            req.onsuccess = () => resolve(req.result);
                            req.onerror = () => resolve(null);
                        });
                        if (!idb) continue;
                        for (const name of idb.objectStoreNames) {
                            try {
                                const tx = idb.transaction(name, 'readonly');
                                const store = tx.objectStore(name);
                                const items = await new Promise((resolve) => {
                                    const req = store.getAll();
                                    req.onsuccess = () => resolve(req.result);
                                    req.onerror = () => resolve([]);
                                });
                                for (const item of items) {
                                    const obj = item.value || item.session || item;
                                    if (typeof obj === 'object' && obj) {
                                        if (obj.accessToken) return obj.accessToken;
                                        if (obj.access_token) return obj.access_token;
                                        if (obj.token) return obj.token;
                                    }
                                }
                            } catch(e) {}
                        }
                    } catch(e) {}
                }
            } catch(e) {}
            return null;
        }""")
        if result:
            logger.info("[sso] Got token from IndexedDB")
            return result
    except Exception:
        pass

    return None


async def _debug_screenshot(page) -> None:
    try:
        path = os.path.join(os.getcwd(), "sso_debug.png")
        await page.screenshot(path=path, full_page=True)
        logger.info("[sso] Screenshot saved to %s (URL: %s)", path, page.url)
        title = await page.title()
        body_text = await page.evaluate("() => document.body?.innerText?.substring(0, 500) || ''")
        logger.info("[sso] Page title: %s", title)
        logger.info("[sso] Body text: %.200s", body_text)
    except Exception as e:
        logger.warning("[sso] Debug screenshot failed: %s", e)


async def _handle_consent_page(page) -> bool:
    current_url = page.url
    if "consent" not in current_url.lower():
        return False
    logger.info("[sso] Found consent page, clicking approve...")
    for sel in _CONSENT_SELECTORS:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=1000):
                await btn.click()
                logger.info("[sso] Clicked consent button: %s", sel)
                await asyncio.sleep(5)
                await page.wait_for_load_state("networkidle")
                logger.info("[sso] URL after consent: %s", page.url)
                return True
        except Exception:
            continue
    await _debug_screenshot(page)
    logger.warning("[sso] Could not find consent button")
    return False


async def _fill_field(page, field_type: str, value: str) -> bool:
    for sel in _INPUT_SELECTORS.get(field_type, []):
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=1000):
                await el.fill(value)
                return True
        except Exception:
            continue
    return False


async def _click_submit(page) -> bool:
    for sel in _SUBMIT_SELECTORS:
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=500):
                await el.click()
                return True
        except Exception:
            continue
    return False


async def _fill_keycloak_form(page, username: str, password: str) -> None:
    logger.info("[sso] Filling Keycloak login form...")

    await _fill_field(page, "username", username)
    await _fill_field(page, "password", password)
    await _click_submit(page)

    logger.info("[sso] Password submitted, waiting for next page...")
    await asyncio.sleep(5)
    await page.wait_for_load_state("networkidle")
    logger.info("[sso] Current URL after password: %s", page.url)

    current_url = page.url
    if "error" in current_url.lower() or "invalid" in current_url.lower():
        logger.error("[sso] Login may have failed, checking page...")
        await _debug_screenshot(page)

    if "totp" in current_url.lower() or "otp" in current_url.lower():
        totp_code = getpass.getpass("[sso] TOTP verification code: ").strip()
        await _submit_totp(page, totp_code)
    else:
        totp_code = getpass.getpass("[sso] TOTP verification code: ").strip()
        if totp_code:
            filled = await _fill_field(page, "otp", totp_code)
            if filled:
                await _click_submit(page)
                logger.info("[sso] TOTP submitted, waiting for redirect...")
                await asyncio.sleep(5)
                await page.wait_for_load_state("networkidle")
                logger.info("[sso] Current URL after TOTP: %s", page.url)
            else:
                logger.warning("[sso] No OTP field found, current URL: %s", page.url)
                await _debug_screenshot(page)

    logger.info("[sso] Waiting for final redirect...")
    for i in range(15):
        await asyncio.sleep(2)
        new_url = page.url
        logger.info("[sso] post-login URL check %d: %s", i, new_url)
        if "consent" in new_url.lower():
            await _handle_consent_page(page)
            continue
        if "web.collab" in new_url or "element" in new_url:
            logger.info("[sso] Redirected back to Element")
            await asyncio.sleep(5)
            break
        if "login.collab" in new_url:
            logger.info("[sso] At callback proxy, waiting...")
    await page.wait_for_load_state("networkidle")
    await _debug_screenshot(page)


async def _submit_totp(page, totp_code: str) -> None:
    if not totp_code:
        logger.warning("[sso] No TOTP code entered")
        return

    filled = await _fill_field(page, "otp", totp_code)
    if not filled:
        logger.warning("[sso] Could not find OTP input field")
        return

    await _click_submit(page)
    logger.info("[sso] TOTP submitted, waiting for redirect...")
    await asyncio.sleep(5)
    await page.wait_for_load_state("networkidle")
