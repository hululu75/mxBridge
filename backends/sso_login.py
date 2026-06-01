from __future__ import annotations

import asyncio
import getpass
import json
import logging
import os
import re
from typing import Optional

logger = logging.getLogger(__name__)

SSO_TOKEN_FILE = ".sso_token.json"
SSO_TOKEN_MAX_AGE = 3600


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
    """Log in via Element Web SSO flow using Playwright.

    Returns (access_token, device_id).
    """
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

        token, device = await asyncio.wait_for(
            _do_sso_flow(page, element_url, username, password, user_id, device_id),
            timeout=120,
        )

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
        if "/_matrix/client/" in url and "/sync" not in url:
            try:
                body = await response.text()
                data = json.loads(body)
                if "access_token" in data:
                    token = data["access_token"]
                    device = data.get("device_id", device)
            except Exception:
                pass

    page.on("response", on_response)

    logger.info("[sso] Opening %s ...", element_url)
    await page.goto(element_url)

    await page.wait_for_load_state("networkidle")
    await asyncio.sleep(2)

    sso_clicked = False

    sso_selectors = [
        "button[data-testid='sso-button']",
        "button:has-text('SSO')",
        "button:has-text('Single Sign-On')",
        "button:has-text('Sign in with Single Sign-On')",
        "button:has-text('Sign in with SSO')",
        "button:has-text('Enterprise SSO')",
        "button:has-text('OIDC')",
        "button:has-text('Log in with Single Sign-On')",
        "a:has-text('SSO')",
        "a:has-text('Single Sign-On')",
        "a:has-text('Sign in with Single Sign-On')",
    ]

    for sel in sso_selectors:
        try:
            btn = page.locator(sel).first
            if await btn.is_visible(timeout=2000):
                await btn.click()
                sso_clicked = True
                logger.info("[sso] Clicked SSO button (%s)", sel)
                break
        except Exception:
            continue

    if not sso_clicked:
        logger.warning("[sso] No SSO button found, waiting for page redirect...")
        await asyncio.sleep(3)

    await asyncio.sleep(3)
    await page.wait_for_load_state("networkidle")

    current_url = page.url
    logger.info("[sso] Current URL: %s", current_url)

    if "keycloak" in current_url.lower() or "auth" in current_url.lower() or "login" in current_url.lower():
        await _fill_keycloak_form(page, username, password)
    else:
        for attempt in range(3):
            try:
                user_selectors = [
                    "#username",
                    "input[name='username']",
                    "input[type='text']:visible",
                    "input[id*='user']",
                    "input[id*='name']",
                ]
                for sel in user_selectors:
                    el = page.locator(sel).first
                    if await el.is_visible(timeout=1000):
                        await el.fill(username)
                        break

                pass_selectors = [
                    "#password",
                    "input[name='password']",
                    "input[type='password']",
                ]
                for sel in pass_selectors:
                    el = page.locator(sel).first
                    if await el.is_visible(timeout=1000):
                        await el.fill(password)
                        break

                submit_selectors = [
                    "button[type='submit']",
                    "input[type='submit']",
                    "button:has-text('Log in')",
                    "button:has-text('Login')",
                    "button:has-text('Sign in')",
                    "button:has-text('Sign In')",
                    "button:has-text('Continue')",
                    "button:has-text('Submit')",
                ]
                for sel in submit_selectors:
                    el = page.locator(sel).first
                    if await el.is_visible(timeout=1000):
                        await el.click()
                        break

                await asyncio.sleep(5)
                await page.wait_for_load_state("networkidle")

                new_url = page.url
                if "element" in new_url.lower() or "cloud.collab" in new_url.lower():
                    logger.info("[sso] Login form submitted, redirected to %s", new_url)
                    break

            except Exception as e:
                logger.debug("[sso] Attempt %d: %s", attempt + 1, e)

    if not token:
        logger.info("[sso] Waiting for access_token from network requests...")
        for _ in range(15):
            await asyncio.sleep(2)
            if token:
                break

    if not token:
        try:
            await page.goto(element_url + "/#/settings/all")
            await asyncio.sleep(3)
            await page.wait_for_load_state("networkidle")
            for _ in range(10):
                await asyncio.sleep(2)
                if token:
                    break
        except Exception:
            pass

    return token, device


async def _fill_keycloak_form(page, username: str, password: str) -> None:
    logger.info("[sso] Filling Keycloak login form...")

    user_selectors = [
        "#username",
        "input[name='username']",
        "input[id*='username']",
        "input[type='text']",
    ]
    for sel in user_selectors:
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=2000):
                await el.fill(username)
                break
        except Exception:
            continue

    pass_selectors = [
        "#password",
        "input[name='password']",
        "input[type='password']",
    ]
    for sel in pass_selectors:
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=2000):
                await el.fill(password)
                break
        except Exception:
            continue

    submit_selectors = [
        "input[type='submit']",
        "button[type='submit']",
        "button:has-text('Log in')",
        "button:has-text('Login')",
        "button:has-text('Sign in')",
        "button:has-text('Sign In')",
    ]
    for sel in submit_selectors:
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=1000):
                await el.click()
                break
        except Exception:
            continue

    await asyncio.sleep(3)
    await page.wait_for_load_state("networkidle")

    totp_code = getpass.getpass("[sso] TOTP verification code: ").strip()
    if not totp_code:
        logger.warning("[sso] No TOTP code entered")
        return

    otp_selectors = [
        "#otp",
        "input[name='otp']",
        "input[id*='otp']",
        "input[id*='totp']",
        "input[name='totp']",
        "input[autocomplete='one-time-code']",
        "input[inputmode='numeric']",
    ]
    for sel in otp_selectors:
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=2000):
                await el.fill(totp_code)
                break
        except Exception:
            continue

    for sel in submit_selectors:
        try:
            el = page.locator(sel).first
            if await el.is_visible(timeout=1000):
                await el.click()
                break
        except Exception:
            continue

    logger.info("[sso] TOTP submitted, waiting for redirect...")
    await asyncio.sleep(5)
    await page.wait_for_load_state("networkidle")
