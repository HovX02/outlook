#!/usr/bin/env python3
"""纯协议探针：bootstrap → verify#1 → captcha.run press（多 payload 对比）。"""

from __future__ import annotations

import json
import os
import sys
import time

from dotenv import load_dotenv

load_dotenv()
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import requests

from common.msa_api import build_msa_risk_verify_signature, check_available_signin_name, risk_initialize, risk_verify
from service.registration.bootstrap_service import bootstrap_session, preload_perimeterx
from service.captcha.captcha_service import (
    build_captcha_run_payload,
    build_captcha_run_payload_legacy,
    solve_perimeterx_captcha_run,
)
from common.http_session import OutlookHttpSession
from model.entity.register_models import AccountInfo
from service.risk.px_cookies import build_px_metadata, solver_context
from service.registration.register_service import _pick_available_email, _random_email_prefix
from service.risk.risk_service import _acquire_silent_px


def _poll_raw(task_id: str, key: str, base: str, n: int = 8) -> None:
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    for i in range(n):
        time.sleep(3)
        r = requests.get(f"{base}/v2/tasks/{task_id}", headers=headers, timeout=30)
        data = r.json()
        resp = data.get("response") or {}
        print(
            f"  poll{i+1} status={data.get('status')} "
            f"press={bool(resp.get('pressToken'))} silent={bool(resp.get('silentToken'))} "
            f"resp_keys={list(resp.keys())[:8]}"
        )
        if resp.get("pressToken") or resp.get("silentToken"):
            print("  token sample:", json.dumps(resp, ensure_ascii=False)[:400])
            return


def _create_and_poll(payload: dict, mode: str, key: str, base: str) -> None:
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    print("\n--- payload keys:", sorted(payload.keys()))
    create = requests.post(f"{base}/v2/tasks/?captchaType={mode}", json=payload, headers=headers, timeout=30)
    print("create", create.status_code, create.text[:200])
    if create.status_code >= 400:
        return
    tid = create.json().get("taskId")
    if tid:
        _poll_raw(tid, key, base)


def main() -> int:
    proxy = os.environ.get("HTTP_PROXY") or ""
    country = os.environ.get("OUTLOOK_REG_COUNTRY", "CA")
    key = os.environ.get("CAPTCHA_RUN_API_KEY", "")
    if not key:
        print("需要 CAPTCHA_RUN_API_KEY")
        return 1

    http = OutlookHttpSession(proxy=proxy)
    ctx = bootstrap_session(http)
    preload_perimeterx(http, ctx)
    email, _ = _pick_available_email(http, ctx, _random_email_prefix(), domain="@outlook.com")
    account = AccountInfo(
        email=email, password="Aa1!abcdefghij", first_name="Sam", last_name="Lee",
        country=country, birth_date="15:06:1995",
    )
    risk_initialize(http, ctx, "")
    px_meta = _acquire_silent_px(http, ctx, mode="solver", proxy=proxy, country=country)
    sig = build_msa_risk_verify_signature(account, ctx)
    resp1 = risk_verify(
        http, ctx, continuation_token=ctx.continuation_token,
        risk_provider_metadata=build_px_metadata(px_meta), msa_risk_verify_signature=sig,
    )
    print("verify#1 state=", resp1.get("state"))
    meta = (resp1.get("challengeDetails") or {}).get("challengeMetadata") or {}
    print("challenge meta:", json.dumps(meta, ensure_ascii=False))

    sctx = solver_context(
        http.session, page_url=ctx.signup_page_url, uaid=ctx.uaid,
        challenge_meta=meta, proxy=proxy, country=country,
    )

    base = os.environ.get("CAPTCHA_RUN_API_BASE", "https://apicn.captcha.run")
    print("\n=== exe press payload ===")
    _create_and_poll(build_captcha_run_payload(sctx, phase="press"), "press", key, base)
    print("\n=== legacy press payload ===")
    _create_and_poll(build_captcha_run_payload_legacy(sctx), "press", key, base)

    print("\n=== solve_perimeterx_captcha_run (120s) ===")
    sol = solve_perimeterx_captcha_run(sctx, mode="press", max_wait=120)
    print("result:", "px3 OK" if sol and sol.get("px3") else sol)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
