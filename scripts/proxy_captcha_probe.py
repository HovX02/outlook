#!/usr/bin/env python3
"""隔离验证：captcha.run 用当前 .env 代理能否解 PxCaptcha2（silent / press）。

不触碰微软接口，只测 captcha.run + 代理连通性：
  POST /v2/tasks/  (uuid/vid 空，与 exe 一致)
  GET  ?captchaType=silent  轮询完整 JSON
  GET  ?captchaType=press   轮询完整 JSON
"""
from __future__ import annotations

import json
import pathlib
import sys
import time

import requests

ROOT = pathlib.Path(__file__).resolve().parents[1]


def load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for line in (ROOT / ".env").read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env


def parse_proxy(s: str):
    parts = s.split(":")
    if len(parts) != 4:
        return None
    host, port, user, pw = parts
    return host, int(port), user, pw


def main() -> int:
    env = load_env()
    key = env.get("CAPTCHA_RUN_API_KEY", "")
    base = env.get("CAPTCHA_RUN_API_BASE", "https://apicn.captcha.run")
    proxy_raw = env.get("HTTP_PROXY", "")
    cfg = parse_proxy(proxy_raw)
    if not (key and cfg):
        print("缺 key 或代理")
        return 1
    host, port, user, pw = cfg
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    payload = {
        "captchaType": "PxCaptcha2",
        "uaid": "",
        "uuid": "",
        "vid": "",
        "userAgent": "Win",
        "country": "US",
        "timezone": "America/New_York",
        "host": host,
        "port": port,
        "login": user,
        "password": pw,
        **(
            {"developer": dev}
            if (dev := os.environ.get("CAPTCHA_RUN_DEVELOPER_ID", "").strip())
            else {}
        ),
    }
    print("POST /v2/tasks/  payload:")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    r = requests.post(f"{base}/v2/tasks/", json=payload, headers=headers, timeout=30)
    print("create ->", r.status_code, r.text[:300])
    if r.status_code >= 400:
        return 1
    tid = r.json().get("taskId") or r.json().get("id")
    print("taskId=", tid)

    for mode in ("silent", "press"):
        print(f"\n===== poll {mode} =====")
        start = time.time()
        while time.time() - start < 100:
            time.sleep(3)
            p = requests.get(f"{base}/v2/tasks/{tid}?captchaType={mode}", headers=headers, timeout=30)
            if p.status_code == 404:
                print(f"  [{int(time.time()-start)}s] 404 (queue)")
                continue
            data = p.json()
            resp = data.get("response") if isinstance(data.get("response"), dict) else {}
            print(
                f"  [{int(time.time()-start)}s] status={data.get('status')} "
                f"silent={bool(resp.get('silentToken'))} press={bool(resp.get('pressToken'))} "
                f"reason={data.get('reason') or resp.get('reason')} "
                f"ip={data.get('ip') or resp.get('ip')} deducted={data.get('deducted')}"
            )
            st = (data.get("status") or "").lower()
            if resp.get("silentToken") and mode == "silent":
                print("  FULL:", json.dumps(data, ensure_ascii=False)[:600])
                break
            if resp.get("pressToken") and mode == "press":
                print("  FULL:", json.dumps(data, ensure_ascii=False)[:600])
                break
            if st in ("fail", "failed", "error", "success"):
                print("  FULL:", json.dumps(data, ensure_ascii=False)[:800])
                break
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
