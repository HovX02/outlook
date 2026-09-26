"""HTTP routes."""
from __future__ import annotations

import json
import logging
import os
import queue
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, StreamingResponse

from model.dto import web_requests as dto
from service.account.keepalive_service import keepalive_one
from service.web import rescue_adapter
from service.web import runtime as rt

logger = logging.getLogger(__name__)

rescue_and_persist = rescue_adapter.rescue_and_persist
rescue_proxy_raw = rescue_adapter.rescue_proxy_raw
count_rescues_from_log = rescue_adapter.count_rescues_from_log

router = APIRouter()
_captcha_provider_meta = rt._captcha_provider_meta

@router.get("/api/config")
def get_config() -> JSONResponse:
    # 代理不回显；captcha.run key 以数据库为主，回显「是否已存 + 掩码」，不回明文。
    proxy = ""
    captcha_key = (os.environ.get("CAPTCHA_RUN_API_KEY") or rt.app_db.get_setting("CAPTCHA_RUN_API_KEY") or "").strip()
    cf_active = rt.cf_domain_mail.cf_domain_backend_active() and rt.cf_domain_mail.cf_configured()
    recovery_pool_configured = rt.ext_recovery_pool.external_pool_enabled()
    recovery_configured = cf_active or recovery_pool_configured
    recovery_backend = rt.cf_domain_mail.recovery_backend()
    env_mode = rt.DEFAULT_TOKEN_MODE
    if env_mode in ("login_exe", "recovery"):
        default_product = "graph_recovery"
    elif cf_active or env_mode == "graph":
        default_product = "graph_recovery"
    else:
        default_product = "graph"
    return JSONResponse(
        {
            "proxy": proxy,
            "captcha_key_masked": rt._mask(captcha_key),
            "captcha_key_set": bool(captcha_key),
            "px_modes": ["solver", "offcaptcha", "local"],
            "captcha_providers": [row["id"] for row in rt.CAPTCHA_PROVIDER_CATALOG],
            "domains": [
                {
                    "value": domain,
                    "label": rt.reg_constants.OUTLOOK_EMAIL_DOMAIN_LABELS.get(domain, domain),
                }
                for domain in rt.reg_constants.OUTLOOK_EMAIL_DOMAINS
            ],
            "countries": [
                {
                    "code": code,
                    "name": name,
                    "name_zh": rt.reg_constants.REGISTRATION_COUNTRY_NAMES_ZH.get(code, name),
                }
                for code, name in sorted(rt.reg_constants.REGISTRATION_COUNTRY_NAMES.items())
            ],
            "default_country": "US",
            "mail_client_id": rt.MAIL_CLIENT_ID,
            "token_modes": rt.TOKEN_MODES,
            "product_modes": rt.PRODUCT_MODES,
            "default_token_mode": default_product,
            "mail_token_mode_env": env_mode,
            "dual_ready": rt.DUAL_READY,
            "batch_ready": rt.BATCH_READY,
            "keepalive_ready": rt.KEEPALIVE_READY,
            "rescue_ready": rt.RESCUE_READY,
            "export_formats": rt.EXPORT_FORMATS,
            "recovery_pool_configured": recovery_pool_configured,
            "cf_domain_configured": cf_active,
            "recovery_backend": recovery_backend,
            "recovery_configured": recovery_configured,
            "proxy_pool": rt.proxy_pool.pool_stats(),
            "proxy_pool_file": str(rt.proxy_pool.pool_file()),
            "proxy_pool_backend": rt.proxy_pool.storage_backend(),
            "execution_routes": rt.EXECUTION_ROUTES,
            "database": rt.app_db.db_status(),
        }
    )


# ---------------------------------------------------------------------------
# 应用设置（DB 为主：captcha.run / EzCaptcha / CapSolver key 存 app_meta）
# ---------------------------------------------------------------------------



@router.get("/api/settings")
def get_settings(reveal: bool = Query(False)) -> JSONResponse:
    out: dict[str, Any] = {}
    for key in rt._SETTINGS_KEYS:
        st = dict(rt._setting_status(key))
        if reveal and key.endswith("_API_KEY"):
            st["key"] = (os.environ.get(key) or rt.app_db.get_setting(key) or "").strip()
        out[key.lower()] = st
    return JSONResponse(out)



@router.post("/api/settings")
def save_settings(req: dto.SettingsRequest) -> JSONResponse:
    """把打码服务 key 存进数据库（DB 为主）。传空串=清空该项；不传=保持不变。"""
    mapping = {
        "CAPTCHA_RUN_API_KEY": req.captcha_run_api_key,
        "EZCAPTCHA_API_KEY": req.ezcaptcha_api_key,
        "CAPSOLVER_API_KEY": req.capsolver_api_key,
        "OFFCAPTCHA_API_KEY": req.offcaptcha_api_key,
        "OFFCAPTCHA_SOFT_ID": req.offcaptcha_soft_id,
        "DEFAULT_CAPTCHA_PROVIDER": req.default_captcha_provider,
    }
    changed = []
    for key, value in mapping.items():
        if value is None:
            continue
        cleaned = value.strip()
        if key == "DEFAULT_CAPTCHA_PROVIDER" and cleaned:
            if not _captcha_provider_meta(cleaned):
                raise HTTPException(status_code=400, detail=f"Unknown CAPTCHA provider: {cleaned}")
        rt.app_db.set_setting(key, cleaned)
        if key.endswith("_API_KEY"):
            if cleaned:
                os.environ[key] = cleaned
            else:
                os.environ.pop(key, None)
        changed.append(key.lower())
    return JSONResponse({"ok": True, "changed": changed, "settings": {k.lower(): rt._setting_status(k) for k in rt._SETTINGS_KEYS}})



@router.get("/api/captcha-provider/{provider_id}/key")
def get_captcha_provider_key(provider_id: str) -> JSONResponse:
    """本机控制台编辑弹窗回显完整 Key（仅 localhost 管理用途）。"""
    meta = _captcha_provider_meta(provider_id)
    if not meta:
        raise HTTPException(status_code=404, detail="Unknown CAPTCHA provider")
    key_setting = meta.get("key_setting") or ""
    if not key_setting:
        return JSONResponse({"ok": True, "provider": provider_id, "key": "", "configured": True})
    val = (os.environ.get(key_setting) or rt.app_db.get_setting(key_setting) or "").strip()
    return JSONResponse(
        {"ok": True, "provider": provider_id, "key": val, "configured": bool(val), "masked": rt._mask(val)}
    )



