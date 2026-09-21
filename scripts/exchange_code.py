"""浏览器手动授权 → code → refresh_token 工具。

用法：
  1) 打印授权链接（在已登录该账号的浏览器里打开）：
       python3 scripts/exchange_code.py --authorize-url [--email you@outlook.com]
  2) 把授权后地址栏 nativeclient?code=... 的 code（或整段 URL）换成 refresh_token：
       python3 scripts/exchange_code.py "M.C5xx....."  --email you@outlook.com --password xxx
       python3 scripts/exchange_code.py "https://login.microsoftonline.com/common/oauth2/nativeclient?code=..." --email ... --password ...

浏览器步骤：
  a. 无痕窗口打开 https://outlook.live.com，用邮箱+密码登录该账号；
  b. 弹出「帮助我们保护你的帐户 / 添加安全信息」时，点「跳过 / 暂时跳过 / Skip for now」；
  c. 进到收件箱后，把本工具 --authorize-url 打印的链接粘到同一浏览器地址栏回车；
  d. 页面会跳到 .../oauth2/nativeclient?code=XXXX（页面可能空白），复制地址栏整段 URL；
  e. 回到终端执行本工具第 2 种用法，传入该 URL 即可拿到 refresh_token 和四段格式。
"""
from __future__ import annotations

import argparse
import sys
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import requests

from config.constants import MAIL_CLIENT_ID, MAIL_REDIRECT_URI, MAIL_SCOPE


def build_authorize_url(login_hint: str = "") -> str:
    params = {
        "client_id": MAIL_CLIENT_ID,
        "response_type": "code",
        "redirect_uri": MAIL_REDIRECT_URI,
        "response_mode": "query",
        "scope": MAIL_SCOPE,
    }
    if login_hint:
        params["login_hint"] = login_hint
    return (
        "https://login.microsoftonline.com/common/oauth2/v2.0/authorize?"
        + urllib.parse.urlencode(params, quote_via=urllib.parse.quote)
    )


def extract_code(raw: str) -> str:
    raw = raw.strip()
    if "code=" in raw:
        query = urllib.parse.urlparse(raw).query or raw.split("?", 1)[-1]
        code = urllib.parse.parse_qs(query).get("code", [""])[0]
        if code:
            return code
    return raw


def exchange(code: str, proxy: str = "") -> dict:
    proxies = None
    if proxy:
        from service.resource.proxy.proxy_utils import parse_proxy

        cfg = parse_proxy(proxy)
        if cfg:
            proxies = {"http": cfg.url, "https": cfg.url}
    resp = requests.post(
        "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        data={
            "client_id": MAIL_CLIENT_ID,
            "code": code,
            "redirect_uri": MAIL_REDIRECT_URI,
            "grant_type": "authorization_code",
            "scope": MAIL_SCOPE,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        proxies=proxies,
        timeout=30,
    )
    try:
        return resp.json()
    except ValueError:
        return {"error": "non_json", "status": resp.status_code, "body": resp.text[:300]}


def main() -> int:
    ap = argparse.ArgumentParser(description="浏览器授权 code ↔ refresh_token")
    ap.add_argument("code", nargs="?", default="", help="授权后 code 值或整段 nativeclient?code= URL")
    ap.add_argument("--authorize-url", action="store_true", help="只打印浏览器授权链接")
    ap.add_argument("--email", default="")
    ap.add_argument("--password", default="")
    ap.add_argument("--proxy", default="", help="可选 host:port:user:pass，与注册同 IP 更稳")
    args = ap.parse_args()

    if args.authorize_url:
        print(build_authorize_url(args.email))
        print("\n在已登录该账号的浏览器地址栏打开上面链接；跳转到 nativeclient?code=... 后复制整段 URL。")
        return 0

    if not args.code:
        print("授权链接：")
        print(build_authorize_url(args.email))
        print("\n拿到 code 后：python3 scripts/exchange_code.py \"<code或URL>\" --email ... --password ...")
        return 0

    code = extract_code(args.code)
    data = exchange(code, args.proxy)
    rt = data.get("refresh_token")
    if not rt:
        print("token 交换失败：", data.get("error"), data.get("error_description") or data.get("body") or "")
        return 1
    print("refresh_token:")
    print(rt)
    if args.email:
        print("\n四段格式（email----password----client_id----refresh_token）:")
        print("----".join([args.email, args.password, MAIL_CLIENT_ID, rt]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
