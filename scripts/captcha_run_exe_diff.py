#!/usr/bin/env python3
"""
对比 outlook-api-register 与 exe 26.7.11 的 captcha.run press/silent POST 体。

用法:
  python scripts/captcha_run_exe_diff.py
  python scripts/captcha_run_exe_diff.py --exe /path/to/微软注册机*.vmp.exe
  python scripts/captcha_run_exe_diff.py --dump-live --mode press
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from outlook_api_reg.captcha import build_captcha_run_payload_exe, build_captcha_run_payload_legacy
from outlook_api_reg.px_cookies import solver_context

DEFAULT_EXE = os.path.expanduser("~/Downloads/微软注册机账密代理版26.7.11.vmp.exe")

EXE_API_MARKERS = [
    "https://apicn.captcha.run/v2/tasks",
    "https://api.captcha-run.com/v2/tasks/",
    "?captchaType=press",
    "?captchaType=silent",
    '{"captchaType": "PxCaptcha2","uaid": "',
    'response.pressToken[\'_px3\']',
    'response.silentToken.pxcts',
    '"developer":"<CAPTCHA_RUN_DEVELOPER_ID>"',
    '"country":"US","timezone":"America/New_York"',
    ',"login": "',
    '","password": "',
    '","port": ',
    '","uuid": "","vid": "',
]


def extract_exe_fragments(exe_path: str) -> dict[str, list[str]]:
    if not os.path.isfile(exe_path):
        return {"error": [f"exe 不存在: {exe_path}"]}

    data = open(exe_path, "rb").read()
    found: dict[str, list[str]] = {"markers": [], "json_fragments": [], "absent_in_exe": []}

    for m in EXE_API_MARKERS:
        if m.encode() in data or m in data:
            found["markers"].append(m)

    region = data[3294000:3294500] if len(data) > 3294500 else data
    for part in re.split(rb"[\x00-\x08\x0b-\x1f]+", region):
        if len(part) < 8:
            continue
        s = part.decode("utf-8", "replace")
        if any(k in s for k in ("captchaType", "developer", "login", "pressToken", "silentToken", "apicn")):
            found["json_fragments"].append(s)

    for key in ("websiteURL", "challengeUrl", "appId", "cookies", "_px3", "_pxde", "pxcts"):
        if key.encode() not in data[3290000:3320000] if len(data) > 3320000 else key.encode() not in data:
            found["absent_in_exe"].append(key)

    return found


def sample_ctx() -> dict:
    import requests

    return solver_context(
        requests.Session(),
        page_url="https://signup.live.com/signup?mkt=EN-US",
        uaid="edd7bf56b22a4754aaa6a18a975f4755",
        challenge_meta={
            "uuid": "722290a2-7e85-11f1-9faf-493f8a95f715",
            "vid": "f9ae0ff8-7c3c-11f1-9b3f-7b8648524521",
            "challengeUrl": "https://iframe.hsprotect.net/index.html?app_id=PXzC5j78di",
        },
        proxy="us.rapidproxy.io:5001:user-session-123:secret",
    )


def diff_table(exe_keys: set[str], ours: dict) -> list[tuple[str, str, str]]:
    rows = []
    for key in sorted(exe_keys | set(ours.keys())):
        in_exe = "✓" if key in exe_keys else "✗"
        in_ours = "✓" if key in ours else "✗"
        note = ""
        if key in exe_keys and key not in ours:
            note = "我们缺"
        elif key not in exe_keys and key in ours:
            note = "我们多传"
        rows.append((key, in_exe, in_ours, note))
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="captcha.run exe vs project payload diff")
    parser.add_argument("--exe", default=DEFAULT_EXE)
    parser.add_argument("--mode", choices=["press", "silent"], default="press")
    parser.add_argument("--dump-live", action="store_true", help="用 CAPTCHA_RUN_API_KEY 真发一次并打印响应")
    args = parser.parse_args()

    ctx = sample_ctx()
    ctx["country"] = "US"
    ctx["timezone"] = "America/New_York"

    exe_payload = build_captcha_run_payload_exe(ctx)
    legacy_payload = build_captcha_run_payload_legacy(ctx)
    exe_info = extract_exe_fragments(args.exe)

    print("=" * 60)
    print("captcha.run POST 对比（方案 C）")
    print("=" * 60)
    print(f"\nEndpoint (exe): POST https://apicn.captcha.run/v2/tasks/?captchaType={args.mode}")
    print("Headers: Authorization: Bearer <key>, Content-Type: application/json\n")

    print("## exe 二进制还原 body 模板（扁平 JSON）")
    print(json.dumps(exe_payload, ensure_ascii=False, indent=2))

    print("\n## 我们旧版 body（CAPTCHA_RUN_LEGACY_PAYLOAD=1）")
    print(json.dumps(legacy_payload, ensure_ascii=False, indent=2))

    exe_field_set = set(exe_payload.keys())
    print("\n## 字段差异（exe 模板 vs 旧版）")
    for key, in_exe, in_legacy, note in diff_table(exe_field_set, legacy_payload):
        if note:
            print(f"  {key:16} exe={in_exe} legacy={in_legacy}  → {note}")

    print("\n## exe 二进制证据")
    if "error" in exe_info:
        print(" ", exe_info["error"][0])
    else:
        print("  markers 命中:", len(exe_info["markers"]), "/", len(EXE_API_MARKERS))
        for m in exe_info["markers"]:
            print("   ✓", m)
        print("  payload 区字符串片段:")
        for frag in exe_info["json_fragments"]:
            print("   ·", frag[:120])
        print("  captcha 代码区未见键名（我们旧版多传）:", ", ".join(exe_info["absent_in_exe"]))

    print("\n## Windows 抓包步骤（拿 exe 真实 POST）")
    print("  1. Windows VM + Reqable/Fiddler，系统代理指向抓包工具")
    print("  2. 过滤 host: apicn.captcha.run 或 api.captcha-run.com")
    print("  3. 运行 exe 触发一次 press + 一次 silent")
    print("  4. 导出 HAR，对比 POST /v2/tasks/?captchaType=press 的 JSON body")
    print("  5. 重点核对: login/password/host/port、uuid/vid、developer/country/timezone")

    if args.dump_live:
        from dotenv import load_dotenv

        load_dotenv()
        os.environ.setdefault("CAPTCHA_RUN_DEBUG", "1")
        from outlook_api_reg.captcha import solve_perimeterx_captcha_run

        print(f"\n## 实发 captcha.run mode={args.mode}（exe 模板）")
        sol = solve_perimeterx_captcha_run(ctx, mode=args.mode, max_wait=90)
        print("  结果:", "px3 OK" if sol and sol.get("px3") else sol)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
