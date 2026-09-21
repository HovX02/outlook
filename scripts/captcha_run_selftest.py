#!/usr/bin/env python3
"""captcha.run 自检（exe 同款打码平台）。"""

from __future__ import annotations

import argparse
import os
import sys

from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from service.captcha.captcha_service import get_captcha_run_balance, solve_perimeterx_captcha_run
from service.risk.px_cookies import solver_context


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--key", help="CAPTCHA_RUN_API_KEY")
    parser.add_argument("--mode", choices=["press", "silent"], default="press")
    args = parser.parse_args()

    key = args.key or os.environ.get("CAPTCHA_RUN_API_KEY", "")
    if not key:
        print("未配置 CAPTCHA_RUN_API_KEY")
        return 1

    os.environ["CAPTCHA_RUN_API_KEY"] = key
    bal = get_captcha_run_balance()
    print(f"余额: {bal if bal is not None else '查询失败（key 可能仍有效）'}")

    ctx = solver_context(
        __import__("requests").Session(),
        page_url="https://signup.live.com/",
        uaid="00000000000000000000000000000000",
    )
    sol = solve_perimeterx_captcha_run(ctx, mode=args.mode)
    if sol and sol.get("px3"):
        print(f"PX 解算成功 mode={args.mode} keys={list(sol.keys())}")
        return 0

    print(f"captcha.run {args.mode} 失败，检查 key/余额/站点支持")
    return 2


if __name__ == "__main__":
    sys.exit(main())
