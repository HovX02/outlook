#!/usr/bin/env python3
"""通过 Roxy profile 执行一次 Outlook 注册，并将结果写入项目 SQLite。

浏览器路线与纯协议路线共用 RegisterResult/account_store；CAPTCHA、服务协议和
OAuth consent 保留为人工确认点。脚本不会把 token 写入日志，只写入 accounts/outlook.db。
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sqlite3
import string
import time
import urllib.parse
from pathlib import Path
import sys

import requests
from dotenv import load_dotenv
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

PROJECT_DIR = Path(__file__).resolve().parents[1]
load_dotenv(PROJECT_DIR / ".env")
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from outlook_api_reg.account_store import save_register_result
from outlook_api_reg.cf_domain_mail import CFDomainMailClient, allocate_address
from outlook_api_reg.models import RegisterResult
from outlook_api_reg.post_register import _config_str, _is_consent_page
from outlook_api_reg.roxy_browser import RoxyBrowserClient, roxy_cdp_session

MAIL_CLIENT_ID = "9e5f94bc-e8a4-4e73-b8be-63364c29d753"
GRAPH_SCOPE = (
    "https://graph.microsoft.com/Mail.ReadWrite "
    "https://graph.microsoft.com/Mail.Send "
    "https://graph.microsoft.com/User.Read offline_access openid profile"
)


def _load_roxy_settings_from_account_manager() -> None:
    """在未显式设置 ROXY_* 时，复用 account_manager 的本机配置。"""
    if os.environ.get("ROXY_TOKEN") or os.environ.get("ROXY_API_KEY"):
        return
    candidates = [
        Path(os.environ.get("ACCOUNT_MANAGER_DB", "")).expanduser(),
        PROJECT_DIR.parent / "account_manager" / "account_manager.db",
        Path("/Users/flx/IdeaProjects/to-api/account_manager/account_manager.db"),
    ]
    for db_path in candidates:
        if not str(db_path) or not db_path.exists():
            continue
        try:
            with sqlite3.connect(str(db_path)) as conn:
                rows = dict(conn.execute(
                    "select key,value from system_settings where key in "
                    "('app.roxy_api','app.roxy_token','app.roxy_workspace_id')"
                ).fetchall())
            values = {k: json.loads(v) for k, v in rows.items()}
            if values.get("app.roxy_token"):
                os.environ.setdefault("ROXY_API", str(values.get("app.roxy_api") or "http://127.0.0.1:50000"))
                os.environ.setdefault("ROXY_TOKEN", str(values["app.roxy_token"]))
                if values.get("app.roxy_workspace_id"):
                    os.environ.setdefault("ROXY_WORKSPACE_ID", str(values["app.roxy_workspace_id"]))
                return
        except (OSError, sqlite3.Error, TypeError, ValueError, json.JSONDecodeError):
            continue


def _rand(n: int = 10) -> str:
    return "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(n))


def _click_next(page) -> None:
    page.get_by_role("button", name="Next", exact=True).click()
    page.wait_for_timeout(800)


def _is_visible(locator) -> bool:
    """Playwright ``count`` only means present in DOM; this checks actionability."""
    try:
        return (
            bool(locator.count())
            and locator.first.is_visible()
            and locator.first.is_enabled()
        )
    except Exception:  # noqa: BLE001
        return False


def _body_text(page) -> str:
    try:
        return page.locator("body").inner_text(timeout=2_000)
    except Exception:  # noqa: BLE001
        return ""


def _is_recovery_step_url(url: str) -> bool:
    low = (url or "").lower()
    return (
        "account.live.com/proofs" in low
        or "account.live.com/interrupt/credentialaction" in low
    )


def _classify_ms_page(url: str, body: str) -> str:
    """Classify Microsoft's post-signup / OAuth interstitial experiments."""
    low_url = (url or "").lower()
    low = (body or "").lower()
    if "localhost/?code=" in low_url:
        return "callback"
    if "privacynotice.account.microsoft.com" in low_url or "quick note about your microsoft account" in low:
        return "privacy"
    if "fido/create" in low_url or "setting up your passkey" in low:
        return "passkey"
    if "stay signed in" in low:
        return "kmsi"
    if "let this app access your info" in low or "consent/update" in low_url:
        return "consent"
    if "are you trying to sign in to thunderbird" in low:
        return "oauth_continue"
    if "account.microsoft.com" in low_url:
        return "account_home"
    if "outlook.live.com/mail" in low_url:
        return "outlook"
    if "account creation has been blocked" in low or "unusual activity" in low:
        return "blocked"
    if "prove you're human" in low or "press and hold" in low:
        return "captcha"
    return "other"


def _click_named(page, name: str) -> bool:
    button = page.get_by_role("button", name=name, exact=True).first
    if not _is_visible(button):
        return False
    previous_url = page.url
    try:
        button.click(timeout=5_000)
    except PlaywrightTimeoutError:
        # Microsoft can begin navigation while Playwright is still waiting for
        # the old button's actionability.  Let the state loop classify the new
        # page instead of turning a successful transition into a fatal error.
        return page.url != previous_url
    page.wait_for_timeout(900)
    return True


def _extract_consent_config(page) -> dict[str, str]:
    """从 window.$Config 或 HTML 提取 consent POST 字段。"""
    try:
        data = page.evaluate(
            """() => {
                for (const cfg of [window.$Config, window.ServerData]) {
                    if (!cfg) continue;
                    const canary = cfg.sCanary || cfg.canary || '';
                    const scope = cfg.sRawInputScopes || cfg.scope || '';
                    const clientId = cfg.sClientId || cfg.client_id || '';
                    if (canary && scope) {
                        return {
                            canary: String(canary),
                            client_id: String(clientId),
                            scope: String(scope),
                        };
                    }
                }
                return null;
            }"""
        )
        if isinstance(data, dict) and data.get("canary") and data.get("scope"):
            return {k: str(v) for k, v in data.items()}
    except Exception:  # noqa: BLE001
        pass
    html = page.content()
    return {
        "canary": _config_str(html, "sCanary"),
        "client_id": _config_str(html, "sClientId"),
        "scope": _config_str(html, "sRawInputScopes"),
    }


def _click_consent(page, *, native_ui=None) -> bool:
    """Submit OAuth consent on account.live.com/Consent/Update."""
    previous_url = page.url
    try:
        page.bring_to_front()
        page.wait_for_load_state("domcontentloaded", timeout=15_000)
    except PlaywrightTimeoutError:
        pass

    deadline = time.time() + 20
    cfg: dict[str, str] = {}
    while time.time() < deadline:
        cfg = _extract_consent_config(page)
        if cfg.get("canary") and cfg.get("scope"):
            break
        page.wait_for_timeout(500)

    if _submit_consent_inpage(page, cfg):
        page.wait_for_timeout(2_000)
        if _classify_ms_page(page.url, _body_text(page)) == "callback":
            return True
        if _classify_ms_page(page.url, _body_text(page)) != "consent":
            return True

    if _submit_consent_http(page, cfg):
        return True

    for name in (
        "Accept", "Yes", "Agree", "Allow", "Continue",
        "Tanggapin", "Setuju", "同意", "接受",
    ):
        if _click_named(page, name):
            return True
        loc = page.get_by_text(name, exact=True).first
        if _is_visible(loc):
            try:
                loc.click(force=True, timeout=5_000)
            except PlaywrightTimeoutError:
                if page.url != previous_url:
                    return True
                continue
            page.wait_for_timeout(900)
            return True

    for selector in (
        'button:has-text("Accept")',
        'input[type="submit"][value*="Accept" i]',
        'input[type="submit"][value*="Yes" i]',
        'input[name="ucaction"][value="Yes"]',
        'button[type="submit"]',
        '[data-testid="primaryButton"]',
        'button.ms-Button--primary',
    ):
        loc = page.locator(selector).first
        if _is_visible(loc):
            try:
                loc.click(force=True, timeout=5_000)
            except PlaywrightTimeoutError:
                if page.url != previous_url:
                    return True
                continue
            page.wait_for_timeout(900)
            return True

    clicked = page.evaluate(
        """() => {
            for (const el of document.querySelectorAll(
                'button, input[type="submit"], [role="button"], span[role="button"]'
            )) {
                const text = (el.innerText || el.value || el.getAttribute('aria-label') || '').trim();
                if (!/^accept$|^yes$/i.test(text)) continue;
                const style = window.getComputedStyle(el);
                if (style.display === 'none' || style.visibility === 'hidden') continue;
                el.click();
                return true;
            }
            return false;
        }"""
    )
    if clicked:
        page.wait_for_timeout(1_500)
        if _classify_ms_page(page.url, _body_text(page)) != "consent":
            return True

    if native_ui is not None:
        print("[oauth] consent Playwright 未跳转，切换 Roxy 前台 tab 后 native 点击 Accept…")
        page.bring_to_front()
        time.sleep(0.6)
        native_ui.activate()
        time.sleep(0.5)
        for x, y in ((620, 640), (650, 640), (700, 640), (580, 640)):
            native_ui.click_relative(x, y)
            time.sleep(1.5)
            page.bring_to_front()
            if _classify_ms_page(page.url, _body_text(page)) != "consent":
                return True
    return False


def _submit_consent_inpage(page, cfg: dict[str, str]) -> bool:
    """在页面上下文 form.submit()，携带完整 cookie 会话。"""
    canary = (cfg or {}).get("canary", "")
    client_id = (cfg or {}).get("client_id", "")
    scope = (cfg or {}).get("scope", "")
    if not (canary and scope):
        return False
    print("[oauth] consent 页内 form POST ucaction=Yes …")
    try:
        ok = page.evaluate(
            """([canary, clientId, scope]) => {
                const form = document.querySelector('form') || document.createElement('form');
                if (!form.parentElement) document.body.appendChild(form);
                form.method = 'POST';
                form.action = location.href;
                const fields = {
                    ucaction: 'Yes',
                    client_id: clientId,
                    scope,
                    cscope: '',
                    canary,
                };
                for (const [name, value] of Object.entries(fields)) {
                    let input = form.querySelector(`input[name="${name}"]`);
                    if (!input) {
                        input = document.createElement('input');
                        input.type = 'hidden';
                        input.name = name;
                        form.appendChild(input);
                    }
                    input.value = value;
                }
                form.submit();
                return true;
            }""",
            [canary, client_id, scope],
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[oauth] consent 页内 submit 异常: {exc}")
        return False
    return bool(ok)


def _submit_consent_http(page, cfg: dict[str, str] | None = None) -> bool:
    """POST ucaction=Yes with the browser cookie jar (post_register 同款)。"""
    url = page.url
    fields = cfg or _extract_consent_config(page)
    canary = fields.get("canary", "")
    client_id = fields.get("client_id", "")
    scope = fields.get("scope", "")
    if not (canary and scope):
        print(
            "[oauth] consent 页字段不全: canary=%s client_id=%s scope=%s"
            % (bool(canary), bool(client_id), bool(scope))
        )
        return False
    print("[oauth] consent HTTP POST ucaction=Yes …")
    try:
        resp = page.request.post(
            url,
            form={
                "ucaction": "Yes",
                "client_id": client_id,
                "scope": scope,
                "cscope": "",
                "canary": canary,
            },
            headers={
                "Origin": "https://account.live.com",
                "Referer": url,
            },
            max_redirects=20,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[oauth] consent HTTP POST 异常: {exc}")
        return False
    final = (resp.url or "").strip()
    print(f"[oauth] consent POST 响应 status={resp.status} url={final[:120]}")
    if "localhost" in final and "code=" in final:
        page.goto(final, wait_until="domcontentloaded", timeout=60_000)
        return True
    if final and final != url:
        try:
            page.goto(final, wait_until="domcontentloaded", timeout=60_000)
        except PlaywrightTimeoutError:
            pass
        if _classify_ms_page(page.url, _body_text(page)) != "consent":
            return True
    return False


def _visible(page, selectors: list[str], timeout: int = 60_000):
    """Return the first visible locator; selectors may survive MS experiments/locales."""
    last = None
    for selector in selectors:
        loc = page.locator(selector).first
        try:
            loc.wait_for(state="visible", timeout=timeout)
            return loc
        except PlaywrightTimeoutError as exc:
            last = exc
    raise RuntimeError(f"找不到可见控件: {selectors}") from last


def _wait_step(page, text: str) -> None:
    try:
        page.get_by_text(text, exact=True).first.wait_for(state="visible", timeout=60_000)
    except PlaywrightTimeoutError as exc:
        raise RuntimeError(f"Microsoft 注册页面未进入步骤「{text}」: {page.url}") from exc


def _choose_combobox(page, label: str, option: str) -> None:
    """选择 Microsoft 新版自定义 combobox（不是 HTML select）。"""
    box = _visible(page, [
        f'[role="combobox"][aria-label="{label}"]',
        f'[role="combobox"][aria-label*="{label}"]',
        f'input[placeholder="{label}"]',
        f'button[aria-label*="{label}"]',
        f'button:has-text("{label}")',
        f'[role="button"]:has-text("{label}")',
    ], timeout=10_000)
    # Fluent UI 的浮动 label 会覆盖按钮几像素，普通 click 会被 label 拦截。
    box.click(force=True)
    choice = page.get_by_role("option", name=option, exact=True).first
    try:
        choice.wait_for(state="visible", timeout=5_000)
        choice.click()
    except PlaywrightTimeoutError:
        # 某些实验版本将下拉项渲染成普通文本节点。
        page.get_by_text(option, exact=True).last.click()


def _fill_first(page, selector: str, value: str) -> None:
    """Fill a preferred selector, falling back to the first visible input.

    Microsoft changes generated ``name``/``id`` attributes between locales and
    signup experiments, while the visible form order remains stable.
    """
    preferred = page.locator(selector).first
    if preferred.count():
        preferred.fill(value)
        return
    visible = page.locator("input:visible").first
    if not visible.count():
        raise RuntimeError(f"找不到可填写输入框: {selector}")
    visible.fill(value)


def _fill_signup_auto(
    page,
    email: str,
    password: str,
    first: str,
    last: str,
    *,
    country: str = "US",
    birth_day: str = "23",
    birth_month: str = "August",
    birth_year: str = "1988",
    signup_url: str = "",
) -> None:
    """Playwright signup fill — no manual prompts; used by native+CDP hybrid batch."""
    country_names = {
        "US": "United States",
        "PH": "Philippines",
        "SG": "Singapore",
        "CA": "Canada",
        "AU": "Australia",
        "GB": "United Kingdom",
    }
    url = signup_url or "https://signup.live.com/signup?lic=1&mkt=EN-US"
    page.goto(url, wait_until="domcontentloaded", timeout=60_000)
    print(f"[signup] 邮箱页 title={page.title()[:60]!r}")
    _visible(page, ['input[type="email"]', 'input[aria-label*="Email"]']).fill(email)
    _click_next(page)
    print("[signup] 密码页")
    _visible(page, ['input[type="password"]', 'input[aria-label*="Password"]']).fill(password)
    page.bring_to_front()
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)
    _click_next(page)
    print("[signup] 生日页")
    cc = (country or "US").strip().upper()
    if cc and page.get_by_role("combobox", name="Country/Region", exact=True).count():
        _choose_combobox(page, "Country/Region", country_names.get(cc, cc))
    _choose_combobox(page, "Birth day", str(birth_day))
    _choose_combobox(page, "Birth month", str(birth_month))
    year = _visible(
        page,
        [
            'input[name*="BirthYear"]',
            'input[aria-label*="Birth year"]',
            'input[placeholder="Year"]',
            'input[type="number"]',
        ],
    )
    year.fill(str(birth_year))
    _click_next(page)
    print("[signup] 姓名页")
    first_box = page.get_by_role("textbox", name="First name", exact=True)
    first_box.wait_for(state="visible", timeout=60_000)
    first_box.fill(first)
    last_box = page.get_by_role("textbox", name="Last name", exact=True)
    last_box.wait_for(state="visible", timeout=60_000)
    last_box.fill(last)
    _click_next(page)
    print("[signup] 已提交，等待 CAPTCHA / proofs…")


def _fill_signup(page, email: str, password: str, first: str, last: str, country: str) -> None:
    page.goto("https://signup.live.com/signup", wait_until="domcontentloaded", timeout=60_000)
    _visible(page, ['input[type="email"]', 'input[aria-label*="Email"]']).fill(email)
    _click_next(page)
    _visible(page, ['input[type="password"]', 'input[aria-label*="Password"]']).fill(password)
    # Chrome/Roxy 可能弹出浏览器原生的“Save password?”提示；先让注册页重新获得焦点。
    page.bring_to_front()
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)
    _click_next(page)
    # Microsoft occasionally renders native selects or comboboxes depending on locale.
    # 新版页面显示为 Country/Region、Day、Month 三个自定义下拉框。
    if country and page.get_by_role("combobox", name="Country/Region", exact=True).count():
        _choose_combobox(page, "Country/Region", {"US": "United States"}.get(country, country))
    _choose_combobox(page, "Birth day", "14")
    _choose_combobox(page, "Birth month", "May")
    year = _visible(page, ['input[name*="BirthYear"]', 'input[aria-label*="Birth year"]',
                           'input[placeholder="Year"]', 'input[type="number"]'])
    year.fill("1993")
    _click_next(page)
    # Fluent UI 版本通常只通过关联 label 暴露姓名框，DOM input 本身没有 aria-label。
    first_box = page.get_by_role("textbox", name="First name", exact=True)
    first_box.wait_for(state="visible", timeout=60_000)
    first_box.fill(first)
    last_box = page.get_by_role("textbox", name="Last name", exact=True)
    last_box.wait_for(state="visible", timeout=60_000)
    last_box.fill(last)
    print("[manual] 即将提交注册协议；确认后按 Enter，由脚本点击 Next。")
    input("确认创建该 Microsoft 账号后按 Enter 继续：")
    # Use DOM automation so Chromium's native password-save bubble cannot
    # intercept the human click. The confirmation remains an explicit gate.
    _click_next(page)


def _proofs(page, recovery_email: str) -> None:
    # CAPTCHA completion is asynchronous.  The challenge can briefly say
    # "completed" and then reset, so URL transition—not a fixed sleep—is the
    # source of truth.
    deadline = time.time() + 120
    retry_prompted = False
    while time.time() < deadline and not _is_recovery_step_url(page.url):
        body = _body_text(page).lower()
        if "account creation has been blocked" in body or "unusual activity" in body:
            raise RuntimeError("Microsoft 风控拦截了账号创建（Account creation has been blocked），未生成账号/token")
        if "please try again" in body and not retry_prompted:
            print("[manual] CAPTCHA 未通过，请在浏览器重试；成功跳转后再按 Enter。")
            input("完成重试后按 Enter 继续：")
            retry_prompted = True
        page.wait_for_timeout(800)
    if not _is_recovery_step_url(page.url):
        raise RuntimeError(f"CAPTCHA 后 120s 仍未进入恢复邮箱步骤: {page.url}")
    if "credentialaction" in page.url.lower():
        add_email = page.get_by_role("button", name="Add email", exact=True).first
        add_email.wait_for(state="visible", timeout=30_000)
        add_email.click()
        page.wait_for_timeout(900)
    client = CFDomainMailClient()
    for attempt in range(2):
        email_box = page.locator(
            'input[type="email"], input[aria-label*="email" i], '
            'input[placeholder*="example" i]'
        ).first
        try:
            email_box.wait_for(state="visible", timeout=30_000)
        except Exception as exc:
            raise RuntimeError(
                f"恢复邮箱输入框在 proofs 页面未就绪: {page.url}"
            ) from exc
        email_box.fill(recovery_email)
        _click_next(page)
        code = client.read_security_code(recovery_email, timeout=150, poll_interval=4)
        if not code:
            raise RuntimeError(f"未收到 CF 验证码(attempt={attempt + 1})")
        code_box = page.locator(
            'input[type="tel"], input[placeholder*="Code" i], input[name*="code" i]'
        ).first
        code_box.wait_for(state="visible", timeout=30_000)
        code_box.fill(code)
        _click_next(page)
        page.wait_for_timeout(1800)
        if not _is_recovery_step_url(page.url):
            break


def _wait_for_captcha_or_signup_result(page, *, timeout: int = 60_000) -> str:
    """Wait for the post-agreement outcome before asking the user for CAPTCHA."""
    deadline = time.time() + timeout / 1000
    while time.time() < deadline:
        state = _classify_ms_page(page.url, _body_text(page))
        if state == "blocked":
            raise RuntimeError("Microsoft 风控拦截了账号创建（Account creation has been blocked），未生成账号/token")
        if state == "captcha" or _is_recovery_step_url(page.url):
            return state
        page.wait_for_timeout(700)
    raise RuntimeError(f"提交注册协议后页面未进入 CAPTCHA/proofs: {page.url}")


def _dismiss_passkey(page, *, native_ui=None) -> bool:
    """Dismiss Microsoft passkey / FIDO enrollment interstitial."""
    for name in ("Cancel", "Skip for now", "Not now", "No thanks", "Skip"):
        if _click_named(page, name):
            return True
    try:
        label = page.evaluate(
            """() => {
                const labels = ['Cancel', 'Skip for now', 'Not now', 'No thanks', 'Skip'];
                for (const want of labels) {
                    const el = [...document.querySelectorAll('button, a, input[type=button]')]
                      .find(x => (x.innerText || x.value || '').trim() === want);
                    if (el) { el.click(); return want; }
                }
                return '';
            }"""
        )
        if label:
            page.wait_for_timeout(1_200)
            return True
    except Exception:  # noqa: BLE001
        pass
    if native_ui is not None:
        native_ui.activate()
        time.sleep(0.5)
        for _ in range(4):
            native_ui.press("Escape")
            time.sleep(0.5)
        page.wait_for_timeout(1_500)
        return _classify_ms_page(page.url, _body_text(page)) != "passkey"
    return False


def _finish_account_setup(page, *, timeout: int = 120_000, native_ui=None, auto: bool = False) -> None:
    """Finish post-proof privacy, passkey and KMSI interstitials.

    Microsoft changes the order of these pages.  Drive by current state instead
    of assuming that a fixed sleep means the consumer session is ready.
    """
    deadline = time.time() + timeout / 1000
    last_state = ""
    while time.time() < deadline:
        body = _body_text(page)
        state = _classify_ms_page(page.url, body)
        if state != last_state:
            print(f"[account] state={state} url={page.url[:120]}")
            last_state = state
        if state == "blocked":
            raise RuntimeError("Microsoft 风控拦截了账号创建")
        if state == "privacy":
            if _click_named(page, "OK"):
                continue
        elif state == "passkey":
            if _dismiss_passkey(page, native_ui=native_ui):
                continue
            if auto:
                page.wait_for_timeout(1_200)
                continue
            print("[manual] 请在 Roxy 中取消蓝牙/Passkey 设置，回到 Microsoft 页面。")
            input("完成取消后按 Enter 继续：")
            continue
        elif state == "kmsi":
            if _click_named(page, "Yes") or _click_named(page, "No"):
                continue
        elif state in {"account_home", "outlook"}:
            return
        page.wait_for_timeout(700)
    raise RuntimeError(f"Microsoft 账户登录态未在 {timeout // 1000}s 内就绪: {page.url}")


def _open_outlook(page, *, native_ui=None, auto: bool = False) -> None:
    page.goto("https://outlook.live.com/mail/0/", wait_until="domcontentloaded", timeout=60_000)
    deadline = time.time() + 90
    while time.time() < deadline:
        body = _body_text(page)
        state = _classify_ms_page(page.url, body)
        if state == "outlook" and ("New mail" in body or "Outlook" in body):
            print("[account] Outlook 收件箱已打开")
            return
        if state in {"privacy", "passkey", "kmsi"}:
            _finish_account_setup(page, timeout=60_000, native_ui=native_ui, auto=auto)
            if "outlook.live.com/mail" not in page.url.lower():
                page.goto("https://outlook.live.com/mail/0/", wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(800)
    raise RuntimeError(f"Outlook 收件箱未就绪: {page.url}")


def _oauth(page, *, native_ui=None, auto: bool = False) -> tuple[str, str]:
    params = {
        "client_id": MAIL_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": "http://localhost",
        "response_mode": "query",
        "scope": GRAPH_SCOPE,
    }
    page.goto(
        "https://login.microsoftonline.com/common/oauth2/v2.0/authorize?" + urllib.parse.urlencode(params),
        wait_until="domcontentloaded", timeout=60_000,
    )
    deadline = time.time() + 150
    prompted_consent = False
    last_state = ""
    while time.time() < deadline:
        body = _body_text(page)
        state = _classify_ms_page(page.url, body)
        if state != last_state:
            print(f"[oauth] state={state} url={page.url[:120]}")
            last_state = state
        if state == "callback":
            break
        if state == "privacy":
            _click_named(page, "OK")
        elif state == "passkey":
            _dismiss_passkey(page, native_ui=native_ui)
        elif state == "kmsi":
            _click_named(page, "Yes") or _click_named(page, "No")
        elif state == "oauth_continue":
            _click_named(page, "Continue")
        elif state == "consent":
            if not prompted_consent:
                if auto:
                    print("[oauth] 自动提交 consent…")
                else:
                    print("[manual] OAuth 权限页已打开；确认后由脚本点击 Accept。")
                    input("确认 OAuth Accept 后按 Enter 继续：")
                prompted_consent = True
            if _click_consent(page, native_ui=native_ui):
                page.wait_for_timeout(1_500)
                if _classify_ms_page(page.url, _body_text(page)) == "callback":
                    break
            else:
                print("[oauth] consent 本轮未跳转，继续等待重试…")
        else:
            # Some experiments first show an account tile or a Continue button.
            account = page.locator('[data-test-id="accountTile"], [role="button"]:has-text("@outlook.com")').first
            if _is_visible(account):
                account.click()
            elif _is_visible(page.get_by_role("button", name="Continue", exact=True)):
                page.get_by_role("button", name="Continue", exact=True).click()
        page.wait_for_timeout(700)
    if _classify_ms_page(page.url, "") != "callback":
        raise RuntimeError(f"OAuth 未返回 localhost code，最终状态={last_state}: {page.url}")
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(page.url).query)
    code = (query.get("code") or [""])[0]
    if not code:
        raise RuntimeError("OAuth 回调缺少 code")
    return code, page.url


def _exchange(code: str) -> dict[str, str]:
    response = requests.post(
        "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        data={
            "client_id": MAIL_CLIENT_ID,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": "http://localhost",
        },
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    if not data.get("refresh_token"):
        raise RuntimeError(f"token 交换未返回 refresh_token: {data.get('error', 'unknown')}")
    return {k: str(v) for k, v in data.items() if v is not None}


def main() -> int:
    ap = argparse.ArgumentParser(description="Roxy 浏览器完整 Outlook 注册并落库")
    group = ap.add_mutually_exclusive_group(required=True)
    group.add_argument("--profile-id")
    group.add_argument("--profile-name")
    ap.add_argument("--email", help="留空则随机生成 Outlook 地址")
    ap.add_argument("--password", help="留空则随机生成密码")
    ap.add_argument("--recovery-email", help="留空则从 CF 域名后端分配")
    ap.add_argument(
        "--country",
        help="注册国家/地区；留空则保留 Microsoft 根据代理 IP 推断的默认值",
    )
    ap.add_argument("--keep-open", action="store_true")
    ap.add_argument("--keep-cookies", action="store_true", help="不清理 profile Cookie（默认注册前清空）")
    args = ap.parse_args()

    _load_roxy_settings_from_account_manager()
    roxy = RoxyBrowserClient()
    if not roxy.health():
        raise SystemExit(f"Roxy API 不可用: {roxy.last_error}")
    profile_id = args.profile_id
    if not profile_id:
        matches = [p for p in roxy.list_profiles() if p.name == args.profile_name]
        if not matches:
            raise SystemExit(f"未找到 Roxy profile: {args.profile_name}")
        profile_id = matches[0].id

    email = args.email or f"xuy{_rand(14)}@outlook.com"
    password = args.password or f"Q!{_rand(12)}7Z"
    recovery_email = args.recovery_email or allocate_address(CFDomainMailClient())
    first, last = "Evan", "Mercer"
    print(f"[roxy] profile={profile_id[:8]}… email={email} recovery={recovery_email}")

    if not args.keep_cookies:
        roxy.close(profile_id)
        # Roxy close 是异步的；立刻 clear/open 可能复用尚未退出的旧进程。
        time.sleep(1.0)
        roxy.clear_profile_cookies(profile_id)
        time.sleep(0.8)
        print("[roxy] 已清空 profile 本地/云端 Cookie（指纹与代理保留）")

    with roxy_cdp_session(profile_id, client=roxy, close_on_exit=not args.keep_open) as (_, context):
        # Roxy 启动时会保留 dashboard Tab；注册必须使用新 Tab，避免复用旧页面/旧导航状态。
        page = context.new_page()
        _fill_signup(page, email, password, first, last, args.country or "")
        signup_state = _wait_for_captcha_or_signup_result(page)
        if signup_state == "captcha":
            input("请完成人机验证，确认页面已跳转后按 Enter 继续：")
        _proofs(page, recovery_email)
        _finish_account_setup(page)
        _open_outlook(page)
        oauth_page = context.new_page()
        _, redirect_url = _oauth(oauth_page)

    token = _exchange(urllib.parse.parse_qs(urllib.parse.urlsplit(redirect_url).query)["code"][0])
    result = RegisterResult(
        success=True,
        email=email,
        password=password,
        redirect_url=redirect_url,
        refresh_token=token["refresh_token"],
        client_id=MAIL_CLIENT_ID,
        recovery_email=recovery_email,
        recovery_password="cf_domain",
        extra={"route": "roxy", "proofs_method": "cf_domain", "proofs_satisfied": "yes"},
    )
    saved = save_register_result(result, str(PROJECT_DIR / "accounts"), batch_label="roxy-manual")
    print(f"注册成功并已落库: {saved}")
    print(f"邮箱: {email}\n密码: {password}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
