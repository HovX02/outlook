#!/usr/bin/env python3
"""Open an existing Roxy profile and verify that Outlook can be reached."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parents[1]
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from outlook_api_reg.roxy_browser import RoxyBrowserClient, roxy_cdp_session


def main() -> int:
    parser = argparse.ArgumentParser(description="Roxy 指纹浏览器 Outlook 连通性测试")
    parser.add_argument("--profile-id", default=os.environ.get("ROXY_PROFILE_ID", ""))
    parser.add_argument("--profile-name", default="", help="按 Roxy 窗口名称选择 profile")
    parser.add_argument("--url", default="https://outlook.live.com/mail/0/")
    parser.add_argument("--keep-open", action="store_true", help="测试后保留 Roxy 窗口")
    args = parser.parse_args()

    client = RoxyBrowserClient()
    if not client.health():
        raise SystemExit(f"Roxy API 不可用: {client.last_error}")

    profile_id = args.profile_id.strip()
    if not profile_id:
        profiles = client.list_profiles()
        if args.profile_name:
            matched = [p for p in profiles if p.name == args.profile_name]
            if not matched:
                raise SystemExit(f"未找到 Roxy profile: {args.profile_name}")
            profile_id = matched[0].id
        elif len(profiles) == 1:
            profile_id = profiles[0].id
        else:
            names = ", ".join(p.name for p in profiles) or "无"
            raise SystemExit(f"请用 --profile-id/--profile-name 指定 profile；当前: {names}")

    print(f"[roxy] 打开 profile={profile_id[:8]}…", flush=True)
    with roxy_cdp_session(profile_id, client=client, close_on_exit=not args.keep_open) as (_, context):
        page = context.pages[0] if context.pages else context.new_page()
        response = page.goto(args.url, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(3_000)
        print(f"[outlook] status={response.status if response else 'n/a'}", flush=True)
        print(f"[outlook] url={page.url}", flush=True)
        print(f"[outlook] title={page.title()}", flush=True)
        if "outlook" not in page.url.lower() and "microsoft" not in page.url.lower() and "live.com" not in page.url.lower():
            raise SystemExit("Outlook 导航结果异常")
    print(f"[roxy] 测试通过；窗口{'已保留' if args.keep_open else '已关闭'}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

