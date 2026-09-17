#!/usr/bin/env python3
"""Overnight Outlook registration loop — new US sticky proxy per attempt until success."""
from __future__ import annotations

import json
import os
import random
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from outlook_api_reg.proxy_utils import (  # noqa: E402
    preflight_proxy,
    probe_exit_stability,
    random_sid,
    rewrite_ipwo_zone_country,
)
from outlook_api_reg.register import register_one, save_account  # noqa: E402

LOG_DIR = Path("/tmp")
SUCCESS_PATH = LOG_DIR / "outlook_reg_success.json"
MAIN_LOG = LOG_DIR / "outlook_reg_overnight.log"
ATTEMPT_LOG = LOG_DIR / "outlook_reg_attempts.jsonl"

PROXY_FILE = ROOT / "proxies_ipwo.txt"


def pick_proxy() -> str:
    lines = [
        ln.strip()
        for ln in PROXY_FILE.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.startswith("#")
    ]
    base = random.choice(lines)
    new_sid = random_sid(8)
    proxy = re.sub(r"sid_\d+", f"sid_{new_sid}", base)
    return rewrite_ipwo_zone_country(proxy, "US")


def append_attempt(record: dict) -> None:
    with ATTEMPT_LOG.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def main() -> int:
    max_attempts = int(os.environ.get("OVERNIGHT_MAX_ATTEMPTS", "30"))
    os.environ.setdefault("PX_SOLVER", "swiftshader")
    os.environ.setdefault("PX_SWIFTSHADER_HEADFUL", "1")
    os.environ.setdefault("PX_OS_PRESS", "1")
    os.environ.setdefault("PX_SWIFTSHADER_TARGET", "parent")
    os.environ.setdefault("REG_PROXY_RETRIES", "1")
    # Focus KPI on account creation; proofs can block overnight runs without real pool
    os.environ.setdefault("OUTLOOK_SKIP_PROOFS", "1")

    failures: list[dict] = []
    for n in range(1, max_attempts + 1):
        proxy = pick_proxy()
        ok, info = preflight_proxy(proxy)
        if not ok:
            rec = {"attempt": n, "proxy_sid": proxy.split("sid_")[1][:8], "error": f"preflight: {info}"}
            failures.append(rec)
            append_attempt(rec)
            print(f"[{n}] skip preflight fail: {info}")
            continue
        sticky, sticky_info, _ = probe_exit_stability(proxy, samples=2)
        if not sticky:
            rec = {"attempt": n, "error": f"unstable: {sticky_info}"}
            failures.append(rec)
            append_attempt(rec)
            print(f"[{n}] skip unstable: {sticky_info}")
            continue

        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        banner = f"\n{'='*72}\n[{ts}] attempt {n}/{max_attempts} proxy={info}\n{'='*72}\n"
        print(banner)
        with MAIN_LOG.open("a", encoding="utf-8") as fh:
            fh.write(banner)

        t0 = time.time()
        try:
            result = register_one(
                country="US",
                proxy=proxy,
                px_mode="local",
                skip_post_login=False,
                fetch_mail_token=True,
            )
        except Exception as exc:  # noqa: BLE001
            result = type("R", (), {"success": False, "error": str(exc), "email": "", "password": ""})()

        elapsed = round(time.time() - t0, 1)
        rec = {
            "attempt": n,
            "elapsed_s": elapsed,
            "proxy_exit": info,
            "success": result.success,
            "email": getattr(result, "email", ""),
            "error": getattr(result, "error", ""),
        }
        failures.append(rec)
        append_attempt(rec)

        with MAIN_LOG.open("a", encoding="utf-8") as fh:
            fh.write(f"attempt={n} success={result.success} elapsed={elapsed}s error={rec['error'][:500]}\n")

        if result.success:
            path = save_account(result, str(ROOT / "accounts"))
            payload = {
                "email": result.email,
                "password": result.password,
                "refresh_token": (result.refresh_token or "")[:80],
                "continuation_token_hint": result.extra.get("uaid", "") if hasattr(result, "extra") else "",
                "account_file": str(path),
                "attempt": n,
                "proxy_exit": info,
                "elapsed_s": elapsed,
                "saved_at": ts,
            }
            SUCCESS_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"\n✅ SUCCESS attempt {n}: {result.email} -> {SUCCESS_PATH}")
            return 0

        print(f"[{n}] FAIL ({elapsed}s): {rec['error'][:200]}")
        time.sleep(random.uniform(3, 8))

    summary = {"attempts": max_attempts, "failures": failures[-10:]}
    (LOG_DIR / "outlook_reg_fail_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"\n❌ All {max_attempts} attempts failed. See {ATTEMPT_LOG}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
