#!/usr/bin/env python3
"""用 offcaptcha.com 打码测试 Outlook 协议注册。

文档：https://offcaptcha.com/docs
任务类型：PXCaptchaInvisible（verify#1）→ PXCaptchaPressAndHold（verify#2）

环境变量：
  OFFCAPTCHA_API_KEY   必填（也认 OFFCAPTCHA_KEY）
  HTTP_PROXY / --proxy 建议 US 住宅，与打码同 IP

用法：
  python3 scripts/offcaptcha_register.py --ping
  python3 scripts/offcaptcha_register.py --proxy host:port:user:pass --country US -v
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
sys.path.insert(0, str(ROOT))

from service import offcaptcha  # noqa: E402
from service.registration.register_service import register_one, save_account  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="OffCaptcha → Outlook 注册测试")
    parser.add_argument("--key", help="OFFCAPTCHA_API_KEY（默认读环境变量）")
    parser.add_argument("--proxy", help="HTTP 代理 host:port:user:pass 或 URL")
    parser.add_argument("--country", default="US")
    parser.add_argument("--domain", default="@outlook.com")
    parser.add_argument("--prefix", help="邮箱前缀（默认随机）")
    parser.add_argument("--ping", action="store_true", help="只探测 API key，不注册")
    parser.add_argument("--skip-login", action="store_true", help="只注册不换 refresh_token")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.key:
        os.environ["OFFCAPTCHA_API_KEY"] = args.key.strip()
    os.environ["PX_SOLVER"] = "offcaptcha"

    if not offcaptcha.api_key():
        print("未配置 OFFCAPTCHA_API_KEY。在 .env 写入或传 --key")
        print("文档：https://offcaptcha.com/docs")
        return 1

    print("=== OffCaptcha Outlook 注册测试 ===")
    print(f"API: {offcaptcha.api_base()}")
    ping = offcaptcha.ping()
    print(f"key 探测: ok={ping.get('ok')} base={ping.get('base')}")
    for path in ("/balance", "/users/me", "/tasks/"):
        hit = ping.get(path)
        if isinstance(hit, dict):
            print(f"  GET {path} → HTTP {hit.get('http')}")
    if args.ping:
        return 0 if ping.get("ok") else 2

    proxy = args.proxy or os.environ.get("HTTP_PROXY") or os.environ.get("OUTLOOK_PROXY") or ""
    if not proxy:
        print("未给代理。OffCaptcha 文档要求 PX 任务带住宅代理，建议 --proxy")
    else:
        print(f"代理: {proxy.split(':')[0]}:{proxy.split(':')[1] if ':' in proxy else ''}")

    try:
        result = register_one(
            email_prefix=args.prefix,
            email_domain=args.domain,
            country=args.country,
            proxy=proxy or None,
            px_mode="solver",
            skip_post_login=args.skip_login,
            fetch_mail_token=not args.skip_login,
        )
    except Exception as exc:
        logging.exception("注册失败")
        print(f"注册失败: {exc}")
        return 3

    if not result.success:
        print(f"注册失败: {result.error}")
        return 4

    path = save_account(result, batch_id="offcaptcha", batch_label="offcaptcha")
    print(f"注册成功: {result.email}")
    print(f"密码: {result.password}")
    if result.refresh_token:
        print(f"refresh_token: {result.refresh_token[:40]}...")
    print(f"已保存: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
