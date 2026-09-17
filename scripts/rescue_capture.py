"""浏览器版解封 + 抓包：登录 abuse 号，抓 login/verify-email/输码/密码 全流程 HTTP 请求。"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from outlook_api_reg.roxy_browser import RoxyBrowserClient, roxy_cdp_session
from outlook_api_reg.cf_domain_mail import CFDomainMailClient, load_config

EMAIL = os.environ.get("RESCUE_EMAIL", "krmdubeovu@outlook.com")
PASSWORD = os.environ.get("RESCUE_PASSWORD", "")
RECOVERY = os.environ.get("RESCUE_RECOVERY", "snfi1tllfjfg@fovts.dev")
OUT = os.environ.get("RESCUE_CAPTURE_OUT", "rescue_capture.jsonl")

captured: list[dict] = []


def _cap_req(req):
    try:
        post = req.post_data or ""
    except Exception:
        post = ""
    if req.resource_type in ("xhr", "fetch", "document") and (
        "live.com" in req.url or "microsoftonline.com" in req.url or "account.live.com" in req.url
    ):
        captured.append({"type": "req", "method": req.method, "url": req.url, "post": (post or "")[:3000]})


def _cap_resp(resp):
    if "live.com" in resp.url or "microsoftonline.com" in resp.url or "account.live.com" in resp.url:
        try:
            body = resp.text()[:3000]
        except Exception:
            body = ""
        captured.append({"type": "resp", "status": resp.status, "url": resp.url, "body": body})


def _click(page, *sels):
    for sel in sels:
        try:
            b = page.locator(sel).first
            if b.count() and b.is_visible():
                b.click(timeout=5000)
                return True
        except Exception:
            pass
    return False


def _read_cf_code():
    try:
        return CFDomainMailClient(load_config()).read_security_code(RECOVERY, timeout=120, poll_interval=4.0)
    except Exception as e:
        print(f"[cf] 读码异常: {e}")
        return ""


def _body_text(page):
    try:
        return page.evaluate("() => document.body.innerText").lower()
    except Exception:
        return ""


def main():
    roxy = RoxyBrowserClient()
    if not roxy.health():
        raise SystemExit(f"Roxy API 不可用: {roxy.last_error}")
    profiles = roxy.list_profiles()
    if not profiles:
        raise SystemExit("无 Roxy profile")
    profile_id = profiles[0].id
    print(f"[rescue] profile={profile_id[:8]}… email={EMAIL}")

    with roxy_cdp_session(profile_id, client=roxy, close_on_exit=True) as (_, context):
        page = context.new_page()
        page.on("request", _cap_req)
        page.on("response", _cap_resp)

        page.goto("https://login.live.com/login.srf?wa=wsignin1.0&wreply=https://outlook.live.com/mail/",
                  wait_until="domcontentloaded", timeout=90000)
        page.wait_for_timeout(2000)

        # 填邮箱
        try:
            inp = page.locator('input[type="email"], input[name="loginfmt"]').first
            inp.wait_for(state="visible", timeout=30000)
            inp.fill(EMAIL)
            page.wait_for_timeout(400)
            _click(page, "#idSIButton9", "#iNext", 'button[type="submit"]', 'input[type="submit"]')
            print("[rescue] 已填邮箱点 Next")
        except Exception as e:
            print(f"[rescue] 填邮箱异常: {e}")

        # 轮询流转
        for _ in range(12):
            page.wait_for_timeout(2000)
            body = _body_text(page)
            url = page.url.lower()
            if "verify your email" in body or "send code" in body or "we'll send a code" in body:
                print("[rescue] Verify your email 页")
                for sel in ('input[type="email"]', 'input[type="text"]', 'input:not([type])'):
                    inp = page.locator(sel).first
                    if inp.count() and inp.is_visible():
                        inp.fill(RECOVERY)
                        print(f"[rescue] 填恢复邮箱 ({sel})")
                        break
                page.wait_for_timeout(400)
                _click(page, 'button:has-text("Send code")', 'button:has-text("Next")', "#idSIButton9", "#iNext")
                code = _read_cf_code()
                print(f"[rescue] 读到验证码: {code}")
                if code:
                    for sel in ('input[name="otc"]', '#otc', 'input[autocomplete="one-time-code"]', 'input[type="text"]'):
                        inp = page.locator(sel).first
                        if inp.count() and inp.is_visible():
                            inp.fill(code)
                            print(f"[rescue] 输码 ({sel})")
                            break
                    page.wait_for_timeout(400)
                    _click(page, 'button:has-text("Verify")', 'button:has-text("Next")', "#idSIButton9", "#iNext", 'input[type="submit"]')
                    print("[rescue] 已提交验证码")
                    # 继续等密码页/登录
                    for _ in range(6):
                        page.wait_for_timeout(2000)
                        b2 = _body_text(page)
                        u2 = page.url.lower()
                        if "enter your password" in b2 or 'type="password"' in b2:
                            print("[rescue] 密码页，填密码")
                            pwd = page.locator('input[type="password"]').first
                            if pwd.count() and pwd.is_visible():
                                pwd.fill(PASSWORD)
                                page.wait_for_timeout(400)
                                _click(page, "#idSIButton9", "#iNext", 'button[type="submit"]', 'input[type="submit"]')
                                print("[rescue] 已填密码")
                            break
                        if "outlook.live.com" in u2 or "account.live.com" in u2:
                            print("[rescue] 已登录")
                            break
                break
            if "enter your password" in body or 'type="password"' in body:
                print("[rescue] 密码页")
                pwd = page.locator('input[type="password"]').first
                if pwd.count() and pwd.is_visible():
                    pwd.fill(PASSWORD)
                    page.wait_for_timeout(400)
                    _click(page, "#idSIButton9", "#iNext", 'button[type="submit"]', 'input[type="submit"]')
                    print("[rescue] 已填密码")
                break
            if "outlook.live.com" in url or "account.live.com" in url:
                print("[rescue] 已登录")
                break

        page.wait_for_timeout(6000)

    with open(OUT, "w", encoding="utf-8") as f:
        for c in captured:
            f.write(json.dumps(c, ensure_ascii=False) + "\n")
    print(f"[rescue] 已保存 {OUT}（{len(captured)} 条）")


if __name__ == "__main__":
    main()
