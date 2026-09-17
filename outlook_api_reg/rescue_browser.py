"""浏览器版解封（rescue）：用 Roxy 浏览器登录 abuse/locked 号 → 验证恢复邮箱 → 解锁。

协议版卡在 GetOneTimeCode State 204（发码需要浏览器指纹），故解封走浏览器：
  Roxy 浏览器(真实指纹) + CFDomainMailClient 收码。

用法：
  from outlook_api_reg.rescue_browser import rescue_one
  result = rescue_one(email, password, recovery_email, proxy="host:port:user:pass")
"""
from __future__ import annotations

import logging
from typing import Optional

from .cf_domain_mail import CFDomainMailClient, load_config
from .roxy_browser import RoxyBrowserClient, roxy_cdp_session

logger = logging.getLogger(__name__)

_SIGNIN_URL = (
    "https://login.live.com/login.srf?wa=wsignin1.0"
    "&wreply=https://outlook.live.com/mail/"
)


def _click(page, *sels) -> bool:
    for sel in sels:
        try:
            b = page.locator(sel).first
            if b.count() and b.is_visible():
                b.click(timeout=4000)
                return True
        except Exception:
            pass
    return False


def _body_text(page) -> str:
    try:
        return page.evaluate("() => document.body.innerText").lower()
    except Exception:
        return ""


def _read_cf_code(recovery_email: str, since_ts: float = 0.0) -> str:
    try:
        return CFDomainMailClient(load_config()).read_security_code(
            recovery_email, timeout=120, poll_interval=4.0, since_ts=since_ts,
        )
    except Exception as exc:
        logger.error("CF 读码异常: %s", exc)
        return ""


def rescue_one(
    email: str,
    password: str,
    recovery_email: str = "",
    *,
    proxy: Optional[str] = None,
    profile_id: Optional[str] = None,
) -> dict:
    """解封一个 locked/abuse 号。返回 {ok, state, detail}。"""
    roxy = RoxyBrowserClient()
    if not roxy.health():
        return {"ok": False, "state": "error", "detail": f"Roxy API 不可用: {roxy.last_error}"}
    if not profile_id:
        profiles = roxy.list_profiles()
        if not profiles:
            return {"ok": False, "state": "error", "detail": "无 Roxy profile"}
        profile_id = profiles[0].id

    try:
        with roxy_cdp_session(profile_id, client=roxy, close_on_exit=True) as (_, context):
            page = context.new_page()
            page.goto(_SIGNIN_URL, wait_until="domcontentloaded", timeout=90000)
            page.wait_for_timeout(1500)

            # 填邮箱
            try:
                inp = page.locator('input[type="email"], input[name="loginfmt"]').first
                inp.wait_for(state="visible", timeout=30000)
                inp.fill(email)
                page.wait_for_timeout(300)
                _click(page, "#idSIButton9", "#iNext", 'button[type="submit"]', 'input[type="submit"]')
            except Exception as exc:
                return {"ok": False, "state": "error", "detail": f"填邮箱异常: {exc}"}

            for _ in range(10):
                page.wait_for_timeout(1500)
                body = _body_text(page)
                url = page.url.lower()

                if url.startswith("https://outlook.live.com") or url.startswith("https://account.live.com"):
                    return {"ok": True, "state": "unlocked", "detail": url}

                if "verify your email" in body or "send code" in body or "we'll send a code" in body:
                    if not recovery_email:
                        return {"ok": False, "state": "needs_phone", "detail": "需恢复邮箱但未提供"}
                    # 填恢复邮箱 + Send code
                    for sel in ('input[type="email"]', 'input[type="text"]', 'input:not([type])'):
                        i2 = page.locator(sel).first
                        if i2.count() and i2.is_visible():
                            i2.fill(recovery_email)
                            break
                    page.wait_for_timeout(300)
                    import time as _t
                    since = _t.time()
                    _click(page, 'button:has-text("Send code")', 'button:has-text("Next")', "#idSIButton9", "#iNext")
                    code = _read_cf_code(recovery_email, since_ts=since)
                    if not code:
                        return {"ok": False, "state": "needs_phone", "detail": "未读到恢复邮箱验证码"}
                    # 输码
                    for sel in ('input[name="otc"]', '#otc', 'input[autocomplete="one-time-code"]', 'input[type="text"]'):
                        i3 = page.locator(sel).first
                        if i3.count() and i3.is_visible():
                            i3.fill(code)
                            break
                    page.wait_for_timeout(300)
                    _click(page, 'button:has-text("Verify")', 'button:has-text("Next")', "#idSIButton9", "#iNext", 'input[type="submit"]')
                    continue

                if "enter your password" in body or 'type="password"' in body:
                    pwd = page.locator('input[type="password"]').first
                    if pwd.count() and pwd.is_visible():
                        pwd.fill(password)
                        page.wait_for_timeout(300)
                        _click(page, "#idSIButton9", "#iNext", 'button[type="submit"]', 'input[type="submit"]')
                    continue

            url = page.url.lower()
            if url.startswith("https://outlook.live.com") or url.startswith("https://account.live.com"):
                return {"ok": True, "state": "unlocked", "detail": url}
            return {"ok": False, "state": "failed", "detail": f"未完成登录: {url[:120]}"}
    except Exception as exc:
        logger.exception("rescue_one 异常")
        return {"ok": False, "state": "error", "detail": str(exc)[:120]}
