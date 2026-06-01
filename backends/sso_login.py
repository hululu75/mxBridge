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


def _save_cached_token(homeserver: str, access_token: str, device_id: str, refresh_token: str = "") -> None:
    path = os.path.join(os.getcwd(), SSO_TOKEN_FILE)
    data = {
        "homeserver": homeserver,
        "access_token": access_token,
        "device_id": device_id,
    }
    if refresh_token:
        data["refresh_token"] = refresh_token
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


async def _try_refresh_token(homeserver: str, refresh_token: str) -> Optional[tuple[str, str, str]]:
    """Use refresh_token to silently get a new access_token.

    Returns (access_token, refresh_token, device_id) or None on failure.
    """
    if not refresh_token:
        return None
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{homeserver.rstrip('/')}/_matrix/client/v3/auth/refresh",
                json={"refresh_token": refresh_token},
            ) as resp:
                if resp.status == 200:
                    body = await resp.json()
                    new_access = body.get("access_token", "")
                    new_refresh = body.get("refresh_token", refresh_token)
                    if new_access:
                        logger.info("[sso] Token silently refreshed via refresh_token")
                        return new_access, new_refresh, ""
                else:
                    text = await resp.text()
                    logger.info("[sso] Refresh token rejected (HTTP %d): %s", resp.status, text[:100])
    except Exception as e:
        logger.debug("[sso] refresh_token attempt failed: %s", e)
    return None


async def sso_login(
    homeserver: str,
    element_url: str,
    user_id: str,
    device_id: str = "",
    username: str = "",
    password: str = "",
    recovery_key: str = "",
) -> tuple[str, str]:
    cached = _load_cached_token(homeserver)
    if cached:
        token = cached["access_token"]
        dev_id = cached["device_id"]
        cached_refresh = cached.get("refresh_token", "")
        logger.info("[sso] Using cached SSO token (device_id=%s)", dev_id)
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{homeserver}/_matrix/client/v3/account/whoami",
                    headers={"Authorization": f"Bearer {token}"},
                ) as resp:
                    if resp.status == 200:
                        body = await resp.json()
                        confirmed = body.get("device_id", dev_id)
                        if confirmed != dev_id:
                            logger.info("[sso] Cached device_id corrected via whoami: %s -> %s", dev_id, confirmed)
                            dev_id = confirmed
                            _save_cached_token(homeserver, token, dev_id, cached_refresh)
                        logger.info("[sso] Cached token valid for %s device=%s", body.get("user_id"), dev_id)
                        return token, dev_id, cached_refresh
                    else:
                        logger.info("[sso] Cached token invalid, trying refresh_token...")
                        refreshed = await _try_refresh_token(homeserver, cached_refresh)
                        if refreshed:
                            new_token, new_refresh, _ = refreshed
                            _save_cached_token(homeserver, new_token, dev_id, new_refresh)
                            return new_token, dev_id, new_refresh
                        logger.info("[sso] refresh_token also invalid, re-login required")
        except Exception as e:
            logger.info("[sso] Cache validation failed: %s, re-login required", e)

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
            token, device, refresh_token = await asyncio.wait_for(
                _do_sso_flow(page, element_url, username, password, user_id, device_id, homeserver, recovery_key),
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

    # Confirm the real device_id from the server — localStorage may hold a stale value.
    try:
        import aiohttp
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{homeserver.rstrip('/')}/_matrix/client/v3/account/whoami",
                headers={"Authorization": f"Bearer {token}"},
            ) as resp:
                if resp.status == 200:
                    body = await resp.json()
                    confirmed = body.get("device_id", "")
                    if confirmed and confirmed != device:
                        logger.info("[sso] device_id corrected via whoami: %s -> %s", device, confirmed)
                        device = confirmed
    except Exception as e:
        logger.warning("[sso] Post-login whoami failed: %s", e)

    _save_cached_token(homeserver, token, device, refresh_token)
    logger.info("[sso] Login successful, device_id=%s refresh_token=%s", device, "yes" if refresh_token else "no")
    return token, device, refresh_token


async def _do_sso_flow(
    page,
    element_url: str,
    username: str,
    password: str,
    user_id: str,
    device_id: str,
    homeserver: str = "",
    recovery_key: str = "",
) -> tuple[Optional[str], str, str]:
    token = None
    device = device_id
    refresh_token = ""
    hs_prefix = homeserver.rstrip("/") if homeserver else ""

    async def on_request(request):
        nonlocal token
        if token:
            return
        url = request.url
        if "/_matrix/" in url or (hs_prefix and url.startswith(hs_prefix)):
            auth = request.headers.get("authorization", "")
            if auth.lower().startswith("bearer ") and len(auth) > 20:
                candidate = auth[7:].strip()
                logger.info("[sso] Captured Bearer token from request to %s (len=%d)", url[:80], len(candidate))
                token = candidate

    async def on_response(response):
        nonlocal token, device, refresh_token
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
                        if data.get("refresh_token"):
                            refresh_token = data["refresh_token"]
                            logger.info("[sso] Captured refresh_token (len=%d)", len(refresh_token))
                except Exception:
                    pass
        except Exception:
            pass

    page.on("request", on_request)
    page.on("response", on_response)

    login_url = element_url.rstrip("/") + "/#/login"
    logger.info("[sso] Opening %s ...", login_url)
    await page.goto(login_url, wait_until="networkidle")
    await asyncio.sleep(3)

    element_host = element_url.rstrip("/").split("//")[-1].split("/")[0] if element_url else ""

    for _round in range(15):
        current_url = page.url
        logger.info("[sso] round %d URL: %s", _round, current_url)

        if "keycloak" in current_url.lower() or (
            "auth" in current_url.lower() and element_host and element_host not in current_url
        ):
            await _fill_keycloak_form(page, username, password, element_url)
            break

        if "totp" in current_url.lower() or "otp" in current_url.lower():
            totp_code = getpass.getpass("[sso] TOTP verification code: ").strip()
            await _submit_totp(page, totp_code)
            break

        clicked = False
        for sel in _NEXT_SELECTORS:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=500):
                    await btn.click()
                    logger.info("[sso] Clicked: %s", sel)
                    await asyncio.sleep(2)
                    clicked = True
                    break
            except Exception:
                continue

        if not clicked:
            for sel in _SSO_SELECTORS:
                try:
                    btn = page.locator(sel).first
                    if await btn.is_visible(timeout=500):
                        await btn.click()
                        logger.info("[sso] Clicked: %s", sel)
                        await asyncio.sleep(3)
                        clicked = True
                        break
                except Exception:
                    continue

        if not clicked:
            for sel in _SUBMIT_SELECTORS:
                try:
                    btn = page.locator(sel).first
                    if await btn.is_visible(timeout=500):
                        await btn.click()
                        logger.info("[sso] Clicked: %s", sel)
                        await asyncio.sleep(3)
                        clicked = True
                        break
                except Exception:
                    continue

        await asyncio.sleep(2)

    if not token:
        await _handle_post_login_page(page, element_url, recovery_key)

    await _handle_device_verification(page, recovery_key)

    done_selectors = ["button:has-text('Done')", "a:has-text('Done')"]
    for sel in done_selectors:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=2000):
                await btn.click()
                logger.info("[sso] Clicked 'Done' button")
                await asyncio.sleep(5)
                break
        except Exception:
            pass

    if not token:
        logger.info("[sso] Waiting for access_token from network requests...")
        for _ in range(10):
            await asyncio.sleep(2)
            if token:
                break

    if not token:
        logger.info("[sso] No token from initial requests, trying route interception + reload...")
        intercepted_token = None

        async def route_handler(route):
            nonlocal intercepted_token
            headers = route.request.headers
            url = route.request.url
            auth = headers.get("authorization", "")
            if auth and len(auth) > 20:
                logger.info("[sso] ROUTE intercepted auth header on %s: %s", url[:80], auth[:50])
                if not intercepted_token:
                    intercepted_token = auth
            if "/_matrix/" in url and "/sync" in url:
                logger.info("[sso] /sync request headers: %s", dict(headers))
            await route.continue_()

        try:
            await page.route("**/_matrix/**", route_handler)
            await page.route("**/sync**", route_handler)
            logger.info("[sso] Routes set up, reloading page...")
            await page.reload(wait_until="domcontentloaded")
            logger.info("[sso] Page reloaded, waiting for /sync request...")
            for _ in range(15):
                await asyncio.sleep(2)
                if intercepted_token:
                    break
            await page.unroute("**/_matrix/**", route_handler)
            await page.unroute("**/sync**", route_handler)
        except Exception as e:
            logger.debug("[sso] Route interception failed: %s", e)

        if intercepted_token:
            candidate = intercepted_token
            if candidate.lower().startswith("bearer "):
                candidate = candidate[7:].strip()
            logger.info("[sso] Got token via route interception (len=%d)", len(candidate))
            token = candidate
        else:
            logger.info("[sso] Route interception found no auth header")

    if not token:
        try:
            cookies = await page.context.cookies()
            logger.info("[sso] All cookies: %s", [(c['name'], c['value'][:50], c['domain']) for c in cookies])
        except Exception as e:
            logger.debug("[sso] Cookie dump failed: %s", e)

    if not token and hs_prefix:
        try:
            whoami_resp = await page.request.get(
                f"{hs_prefix}/_matrix/client/v3/account/whoami"
            )
            whoami_text = await whoami_resp.text()
            logger.info("[sso] whoami status=%s body=%s", whoami_resp.status, whoami_text[:300])
            whoami_data = json.loads(whoami_text)
            if whoami_resp.status == 200 and "user_id" in whoami_data:
                device = whoami_data.get("device_id", device)
                logger.info("[sso] whoami OK: user=%s device=%s", whoami_data["user_id"], device)
        except Exception as e:
            logger.debug("[sso] whoami failed: %s", e)

    if not token:
        try:
            result = await page.evaluate("""async () => {
                const peg = window.mxMatrixClientPeg || window.mx_matrixClientPeg;
                if (peg) {
                    const mc = typeof peg.get === 'function' ? peg.get() : null;
                    if (mc) {
                        if (typeof mc.getAccessToken === 'function') {
                            const t = mc.getAccessToken();
                            if (t) return t;
                        }
                        if (mc._http && mc._http.opts && mc._http.opts.accessToken) return mc._http.opts.accessToken;
                    }
                }
                const wkeys = Object.getOwnPropertyNames(window).filter(k => 
                    k.toLowerCase().includes('matrix') || k.toLowerCase().includes('peg') || k.toLowerCase().includes('client')
                );
                for (const key of wkeys) {
                    try {
                        const obj = window[key];
                        if (obj && typeof obj === 'object') {
                            if (typeof obj.get === 'function') {
                                const mc = obj.get();
                                if (mc) {
                                    if (typeof mc.getAccessToken === 'function') {
                                        const t = mc.getAccessToken();
                                        if (t) return 'found:' + key + ':' + t;
                                    }
                                    if (mc._http && mc._http.opts && mc._http.opts.accessToken) return 'found:' + key + ':' + mc._http.opts.accessToken;
                                }
                            }
                        }
                    } catch(e) {}
                }
                return 'window_keys:' + JSON.stringify(wkeys);
            }""")
            if result:
                if result.startswith('found:'):
                    token = result.split(':', 2)[2]
                    logger.info("[sso] Got token from window JS runtime")
                elif result.startswith('window_keys:'):
                    logger.info("[sso] Window keys with matrix/peg/client: %s", result[12:])
                else:
                    token = result
                    logger.info("[sso] Got token from matrixClient.getAccessToken()")
        except Exception as e:
            logger.debug("[sso] JS matrixClient search failed: %s", e)

    if not device:
        try:
            device = await page.evaluate("() => localStorage.getItem('mx_device_id') || ''")
        except Exception:
            pass

    return token, device, refresh_token


async def _handle_post_login_page(page, element_url: str = "", recovery_key: str = "") -> None:
    current_url = page.url
    logger.info("[sso] Post-login URL: %s", current_url)
    element_host = element_url.rstrip("/").split("//")[-1].split("/")[0] if element_url else ""

    for i in range(15):
        await asyncio.sleep(2)
        new_url = page.url
        logger.info("[sso] post-login check %d: %s", i, new_url)

        if "consent" in new_url.lower():
            await _handle_consent_page(page)
            continue
        if element_host and element_host in new_url:
            logger.info("[sso] Redirected back to Element")
            await asyncio.sleep(5)
            break
        if "/#/" in new_url or "/#/login" in new_url or "/#/home" in new_url:
            logger.info("[sso] Redirected back to Element (hash route)")
            await asyncio.sleep(5)
            break

    await page.wait_for_load_state("domcontentloaded")
    await _debug_screenshot(page)

    page_text = ""
    try:
        page_text = await page.evaluate("() => document.body?.innerText?.substring(0, 1000) || ''")
    except Exception:
        pass
    logger.info("[sso] page_text for verification check: %.200s", page_text)

    if "recovery key" in page_text.lower() or "confirm your digital identity" in page_text.lower():
        logger.info("[sso] Device verification page detected")
        if not recovery_key:
            recovery_key = input("[sso] Enter recovery key (or press Enter to skip): ").strip()
        if recovery_key:
            use_rk = page.locator("button:has-text('Use recovery key'), a:has-text('Use recovery key')")
            try:
                if await use_rk.first.is_visible(timeout=1000):
                    await use_rk.first.click()
                    logger.info("[sso] Clicked 'Use recovery key'")
                    await asyncio.sleep(2)
            except Exception:
                pass
            for sel in _RK_INPUT_SELECTORS:
                try:
                    el = page.locator(sel).first
                    if await el.is_visible(timeout=500):
                        await el.fill(recovery_key)
                        break
                except Exception:
                    continue
            for sel in _RK_SUBMIT_SELECTORS:
                try:
                    btn = page.locator(sel).first
                    if await btn.is_visible(timeout=500):
                        await btn.click()
                        logger.info("[sso] Submitted recovery key")
                        await asyncio.sleep(5)
                        await page.wait_for_load_state("domcontentloaded")
                        break
                except Exception:
                    continue
        else:
            logger.warning("[sso] No recovery key provided, trying to skip...")
            for sel in _SKIP_SELECTORS:
                try:
                    btn = page.locator(sel).first
                    if await btn.is_visible(timeout=500):
                        await btn.click()
                        logger.info("[sso] Clicked skip: %s", sel)
                        await asyncio.sleep(3)
                        break
                except Exception:
                    continue

        await asyncio.sleep(3)
        await _debug_screenshot(page)

    try:
        clicked = await page.evaluate("""() => {
            const btns = document.querySelectorAll('button, a, [role="button"]');
            for (const btn of btns) {
                if (btn.innerText.trim() === 'Done') {
                    btn.click();
                    return true;
                }
            }
            return false;
        }""")
        if clicked:
            logger.info("[sso] Clicked 'Done' button via JS")
            await asyncio.sleep(5)
        else:
            logger.info("[sso] No 'Done' button found via JS")
    except Exception as e:
        logger.debug("[sso] Done button click failed: %s", e)


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
                await page.wait_for_load_state("domcontentloaded")
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


async def _fill_keycloak_form(page, username: str, password: str, element_url: str = "") -> None:
    logger.info("[sso] Filling Keycloak login form...")
    element_host = element_url.rstrip("/").split("//")[-1].split("/")[0] if element_url else ""

    await _fill_field(page, "username", username)
    await _fill_field(page, "password", password)
    await _click_submit(page)

    logger.info("[sso] Password submitted, waiting for next page...")
    await asyncio.sleep(5)
    await page.wait_for_load_state("domcontentloaded")
    logger.info("[sso] Current URL after password: %s", page.url)

    current_url = page.url
    if "error" in current_url.lower() or "invalid" in current_url.lower():
        logger.error("[sso] Login may have failed, checking page...")
        await _debug_screenshot(page)

    totp_needed = "totp" in current_url.lower() or "otp" in current_url.lower()
    if not totp_needed:
        for sel in _INPUT_SELECTORS["otp"]:
            try:
                el = page.locator(sel).first
                if await el.is_visible(timeout=500):
                    totp_needed = True
                    break
            except Exception:
                continue

    if totp_needed:
        totp_code = getpass.getpass("[sso] TOTP verification code: ").strip()
        if totp_code:
            filled = await _fill_field(page, "otp", totp_code)
            if filled:
                await _click_submit(page)
                logger.info("[sso] TOTP submitted, waiting for redirect...")
                await asyncio.sleep(5)
                await page.wait_for_load_state("domcontentloaded")
                logger.info("[sso] Current URL after TOTP: %s", page.url)
            else:
                logger.warning("[sso] No OTP field found despite detection, current URL: %s", page.url)
                await _debug_screenshot(page)
    else:
        logger.info("[sso] No TOTP required, continuing...")

    logger.info("[sso] Waiting for final redirect...")
    for i in range(15):
        await asyncio.sleep(2)
        new_url = page.url
        logger.info("[sso] post-login URL check %d: %s", i, new_url)
        if "consent" in new_url.lower():
            await _handle_consent_page(page)
            continue
        if element_host and element_host in new_url:
            logger.info("[sso] Redirected back to Element")
            await asyncio.sleep(5)
            break
        if "/#/" in new_url:
            logger.info("[sso] Redirected back to Element (hash route)")
            await asyncio.sleep(5)
            break
    await page.wait_for_load_state("domcontentloaded")
    await _debug_screenshot(page)


async def _handle_device_verification(page, recovery_key: str = "") -> None:
    """Handle the Element 'Confirm your digital identity' verification overlay if present."""
    try:
        page_text = await page.evaluate("() => document.body?.innerText?.substring(0, 1000) || ''")
    except Exception:
        return

    if "confirm your digital identity" not in page_text.lower() and "recovery key" not in page_text.lower():
        return

    logger.info("[sso] Device verification overlay detected, handling...")
    if not recovery_key:
        recovery_key = input("[sso] Enter recovery key (or press Enter to skip): ").strip()

    if recovery_key:
        use_rk = page.locator("button:has-text('Use recovery key'), a:has-text('Use recovery key')")
        try:
            if await use_rk.first.is_visible(timeout=1000):
                await use_rk.first.click()
                logger.info("[sso] Clicked 'Use recovery key'")
                await asyncio.sleep(2)
        except Exception:
            pass
        for sel in _RK_INPUT_SELECTORS:
            try:
                el = page.locator(sel).first
                if await el.is_visible(timeout=500):
                    await el.fill(recovery_key)
                    break
            except Exception:
                continue
        for sel in _RK_SUBMIT_SELECTORS:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=500):
                    await btn.click()
                    logger.info("[sso] Submitted recovery key")
                    await asyncio.sleep(5)
                    await page.wait_for_load_state("domcontentloaded")
                    break
            except Exception:
                continue
    else:
        logger.warning("[sso] No recovery key provided, trying to skip verification...")
        for sel in _SKIP_SELECTORS:
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=500):
                    await btn.click()
                    logger.info("[sso] Clicked skip: %s", sel)
                    await asyncio.sleep(3)
                    break
            except Exception:
                continue


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
    await page.wait_for_load_state("domcontentloaded")
