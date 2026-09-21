#!/usr/bin/env python3
"""浏览器走救援登录，每步截图，便于对照协议卡在哪。"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from config.constants import GRAPH_MAIL_SCOPE, MAIL_CLIENT_ID, MAIL_REDIRECT_URI
from service.resource.proxy.proxy_utils import parse_proxy

SHOT_DIR = Path(os.environ.get("RESCUE_SHOT_DIR", "/tmp/rescue_browser_shots"))


def _shot(page, name: str, note: str = "") -> Path:
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    path = SHOT_DIR / f"{name}.png"
    page.screenshot(path=str(path), full_page=True, timeout=60000, animations="disabled")
    meta = {"file": str(path), "url": page.url, "title": page.title(), "note": note}
    (SHOT_DIR / f"{name}.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[shot] {name}: {page.url[:90]} | {note}")
    return path


def _proxy_cfg(raw: str) -> dict | None:
    cfg = parse_proxy(raw)
    if not cfg:
        return None
    return {"server": f"http://{cfg.host}:{cfg.port}", "username": cfg.username, "password": cfg.password}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("email")
    parser.add_argument("password")
    parser.add_argument("--proxy", required=True)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument(
        "--manual",
        action="store_true",
        help="有头模式打开浏览器，走到 Abuse 页后保持不关，供用户手动点 Next / 按住",
    )
    args = parser.parse_args()
    if args.manual:
        args.headless = False

    from playwright.sync_api import sync_playwright

    params = {
        "client_id": MAIL_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": MAIL_REDIRECT_URI,
        "scope": GRAPH_MAIL_SCOPE,
        "login_hint": args.email,
    }
    auth_url = "https://login.live.com/oauth20_authorize.srf?" + urllib.parse.urlencode(params)
    proxy = _proxy_cfg(args.proxy)
    if not proxy:
        print("proxy parse failed", file=sys.stderr)
        return 2

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=args.headless, proxy=proxy)
        ctx = browser.new_context(
            viewport={"width": 1280, "height": 900},
            locale="en-US",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
        )
        page = ctx.new_page()
        page.set_default_timeout(60000)
        try:
            page.goto(auth_url, wait_until="domcontentloaded", timeout=90000)
            time.sleep(2)
            _shot(page, "01_authorize", "OAuth 登录页")

            if page.get_by_text("Use your password", exact=False).count():
                page.get_by_text("Use your password", exact=False).first.click(timeout=8000)
                time.sleep(2)
                _shot(page, "01b_use_password", "切换到密码登录")

            if page.locator('input[type="password"], input[name="passwd"]').count() == 0:
                email_loc = page.locator('input[type="email"]:visible, input[name="loginfmt"]:visible')
                if email_loc.count():
                    email_loc.first.fill(args.email)
                    _shot(page, "02_email_filled", "已填邮箱")
                    for sel in ('input[type="submit"]', 'button[type="submit"]', '#idSIButton9'):
                        if page.locator(sel).count():
                            page.locator(sel).first.click(timeout=5000)
                            break
                    time.sleep(3)

            if page.locator('input[type="password"], input[name="passwd"]').count():
                page.fill('input[type="password"], input[name="passwd"]', args.password)
                _shot(page, "03_password_filled", "已填密码")
                for sel in ('input[type="submit"]', 'button[type="submit"]', '#idSIButton9'):
                    if page.locator(sel).count():
                        page.locator(sel).first.click(timeout=5000)
                        break
                page.wait_for_load_state("domcontentloaded", timeout=90000)
                time.sleep(4)

            _shot(page, "04_after_password", "密码提交后")

            def _has_oauth_code(u: str) -> bool:
                if "nativeclient" in u:
                    return True
                q = urllib.parse.urlparse(u).query
                return urllib.parse.parse_qs(q).get("code", [""])[0] != ""

            def _on_abuse() -> bool:
                return "/Abuse" in page.url or "Account_ServiceAbuseInterruptPage" in (page.content() or "")

            if args.manual:
                print("[manual] 等待进入 Abuse 页（最多 90s）…")
                for wait in range(90):
                    if _on_abuse():
                        break
                    time.sleep(1)
                if _on_abuse():
                    _shot(page, "05_abuse_page", "Abuse 解封页")
                    print("[wait] Abuse 页 PX 传感器预热 15s…")
                    time.sleep(15)
                else:
                    _shot(page, "05_stuck", f"未到 Abuse，当前: {page.url[:80]}")
                    print(f"[manual] 警告：当前未在 Abuse 页，URL={page.url[:100]}")
                print("\n" + "=" * 60)
                print(f"账号: {args.email}")
                print("浏览器已打开。请你手动操作：")
                print("  1) 若在 Abuse 页 → 点 Next")
                print("  2) 出现 Press and Hold → 按住 8-10 秒")
                print("  3) 解封成功后浏览器会跳转，完成后直接关窗口即可")
                print("（本进程将保持 2 小时，窗口关了自己也会停）")
                print("=" * 60 + "\n")
                try:
                    time.sleep(7200)
                except KeyboardInterrupt:
                    pass
                return 0

            for hop in range(8):
                url = page.url
                body = page.content()
                if "/Abuse" in url or "Account_ServiceAbuseInterruptPage" in body:
                    _shot(page, "05_abuse_page", "Abuse 解封页")
                    print("[wait] Abuse 页等待 PX 传感器预热…")
                    for px_wait in range(30):
                        has_px = any(
                            "hsprotect.net" in (fr.url or "") or "perimeterx" in (fr.url or "").lower()
                            for fr in page.frames
                        )
                        if has_px and px_wait >= 8:
                            break
                        time.sleep(1)
                    _shot(page, "05a_px_warmup", f"PX 预热 {px_wait+1}s")
                    if page.get_by_role("button", name="Next").count():
                        page.get_by_role("button", name="Next").first.click(timeout=8000)
                        for after in range(15):
                            time.sleep(2)
                            txt = page.content().lower()
                            if "press and hold" in txt or "press & hold" in txt:
                                _shot(page, "05c_press_hold", "出现按住挑战")
                                break
                            if "something went wrong" in txt:
                                _shot(page, "05b_abuse_next_fail", "点 Next 后服务报错")
                                break
                        else:
                            _shot(page, "05b_abuse_next", "点击 Next 后")
                    break
                if _has_oauth_code(url):
                    _shot(page, "06_oauth_success", "已拿到 OAuth code")
                    break
                time.sleep(2)
                _shot(page, f"04b_hop_{hop}", f"跳转 hop {hop}")

            # 等 PX iframe / 按住挑战
            for wait in range(20):
                for fr in page.frames:
                    u = fr.url or ""
                    if "hsprotect.net" in u or "perimeterx" in u.lower():
                        try:
                            fr.page.screenshot(path=str(SHOT_DIR / "07_px_iframe_page.png"), full_page=True)
                        except Exception:
                            pass
                txt = page.content().lower()
                if "press and hold" in txt or "press & hold" in txt:
                    _shot(page, "08_press_hold", "检测到 Press and Hold 挑战")
                    break
                if "riskblock" in txt or "blocked" in txt:
                    _shot(page, "08_blocked", "页面出现 blocked 文案")
                    break
                time.sleep(2)
            else:
                _shot(page, "08_final", "等待 PX 挑战超时后的最终页")

            print(json.dumps({"ok": True, "shot_dir": str(SHOT_DIR)}, ensure_ascii=False))
            browser.close()
            return 0
        except Exception as exc:
            _shot(page, "99_error", f"{type(exc).__name__}: {exc}")
            print(json.dumps({"ok": False, "error": str(exc), "shot_dir": str(SHOT_DIR)}, ensure_ascii=False))
            browser.close()
            return 1


if __name__ == "__main__":
    raise SystemExit(main())
