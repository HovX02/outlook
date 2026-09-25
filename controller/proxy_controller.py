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

@router.get("/api/proxy-pool")
def get_proxy_pool(
    provider: Optional[str] = Query(None),
    limit: int = Query(5000, ge=1, le=20000),
    offset: int = Query(0, ge=0),
) -> JSONResponse:
    store = rt.proxy_pool.load_store()
    return JSONResponse({
        "ok": True,
        "backend": rt.proxy_pool.storage_backend(),
        "stats": rt.proxy_pool.pool_stats(provider=provider),
        "providers": rt.proxy_pool.list_providers(),
        "settings": store.get("settings") or {},
        "proxies": rt.proxy_pool.list_entries(for_api=True, provider=provider, limit=limit, offset=offset),
        "bindings": rt.proxy_pool.bindings_for_api(limit=min(limit, 2000), offset=offset),
        "file": str(rt.proxy_pool.pool_file()),
    })



@router.get("/api/proxy-pool/analytics")
def get_proxy_pool_analytics(
    provider: Optional[str] = Query(None),
    country: Optional[str] = Query(None),
    reg_country: Optional[str] = Query(None),
    days: int = Query(0, ge=0, le=365),
) -> JSONResponse:
    return JSONResponse({
        "ok": True,
        "analytics": rt.proxy_pool.proxy_analytics(
            provider=provider,
            country=country,
            reg_country=reg_country,
            days=days,
        ),
    })



@router.get("/api/proxy-pool/analytics/timeseries")
def get_proxy_pool_timeseries(
    provider: Optional[str] = Query(None),
    country: Optional[str] = Query(None),
    reg_country: Optional[str] = Query(None),
    days: int = Query(30, ge=1, le=365),
    group_by: str = Query("provider"),
) -> JSONResponse:
    return JSONResponse({
        "ok": True,
        "timeseries": rt.proxy_pool.proxy_analytics_timeseries(
            provider=provider,
            country=country,
            reg_country=reg_country,
            days=days,
            group_by=group_by,
        ),
    })



@router.post("/api/proxy-pool/backfill-countries")
def backfill_proxy_countries(force: bool = Query(False)) -> JSONResponse:
    result = rt.proxy_pool.backfill_proxy_countries(force=force)
    return JSONResponse({
        "ok": True,
        **result,
        "proxies": rt.proxy_pool.list_entries(for_api=True),
    })



@router.post("/api/proxy-pool/ensure")
def ensure_proxy_pool(req: dto.ProxyPoolEnsureRequest) -> JSONResponse:
    result = rt.proxy_pool.ensure_templates(
        req.templates,
        text=req.text,
        provider=(req.provider or "web").strip(),
        country=(req.country or "").strip(),
    )
    return JSONResponse({
        "ok": True,
        **result,
        "stats": rt.proxy_pool.pool_stats(),
        "proxies": rt.proxy_pool.list_entries(for_api=True),
    })



@router.post("/api/proxy-pool")
def add_proxy_pool(req: dto.ProxyPoolAddRequest) -> JSONResponse:
    templates = list(req.templates or [])
    if req.text:
        for line in req.text.replace("\r", "").split("\n"):
            line = line.strip()
            if line and not line.startswith("#"):
                templates.append(line)
    if not templates:
        raise HTTPException(status_code=400, detail="Please provide at least one proxy template.")
    created = rt.proxy_pool.add_proxies(
        templates,
        label=(req.label or "").strip(),
        provider=(req.provider or "").strip(),
        country=(req.country or "").strip(),
    )
    return JSONResponse({
        "ok": True,
        "added": len(created),
        "proxies": rt.proxy_pool.list_entries(for_api=True),
        "stats": rt.proxy_pool.pool_stats(),
    })



@router.put("/api/proxy-pool/{proxy_id}")
def update_proxy_pool_item(proxy_id: str, req: dto.ProxyPoolUpdateRequest) -> JSONResponse:
    ent = rt.proxy_pool.update_proxy(
        proxy_id,
        label=req.label,
        template=req.template,
        provider=req.provider,
        country=req.country,
        enabled=req.enabled,
    )
    if not ent:
        raise HTTPException(status_code=404, detail="Proxy not found.")
    return JSONResponse({"ok": True, "proxy": {**ent, "template_masked": rt.proxy_pool.mask_template(ent.get("template") or "")}})



@router.post("/api/proxy-pool/delete")
def delete_proxy_pool(req: dto.ProxyPoolDeleteRequest) -> JSONResponse:
    n = rt.proxy_pool.delete_proxies(req.ids or [])
    return JSONResponse({"ok": True, "deleted": n, "stats": rt.proxy_pool.pool_stats()})



@router.post("/api/proxy-pool/check")
def check_proxy_pool(req: dto.ProxyPoolCheckRequest) -> JSONResponse:
    results = rt.proxy_pool.check_proxies(req.ids, timeout=max(5, min(int(req.timeout or 15), 60)))
    ok_n = sum(1 for r in results if r.get("ok"))
    return JSONResponse({
        "ok": True,
        "checked": len(results),
        "healthy": ok_n,
        "results": results,
        "stats": rt.proxy_pool.pool_stats(),
        "proxies": rt.proxy_pool.list_entries(for_api=True),
    })



@router.post("/api/proxy-pool/settings")
def update_proxy_pool_settings(req: dto.ProxyPoolSettingsRequest) -> JSONResponse:
    settings = rt.proxy_pool.update_settings(
        strategy=req.strategy,
        require_healthy=req.require_healthy,
        sticky_per_account=req.sticky_per_account,
    )
    return JSONResponse({"ok": True, "settings": settings})



@router.post("/api/proxy-pool/bind")
def bind_proxy_pool(req: dto.ProxyPoolBindRequest) -> JSONResponse:
    store = rt.proxy_pool.load_store()
    ent = rt.proxy_pool.entry_by_id(store, req.proxy_id)
    if not ent:
        raise HTTPException(status_code=404, detail="Proxy not found.")
    resolved = rt.proxy_pool.resolve_template(ent.get("template") or "")
    if not resolved:
        raise HTTPException(status_code=400, detail="Invalid proxy template.")
    rt.proxy_pool.bind_account(req.email.strip().lower(), req.proxy_id, resolved, purpose="manual")
    return JSONResponse({"ok": True, "email": req.email.strip().lower(), "resolved_masked": rt.proxy_pool.mask_template(resolved)})



@router.post("/api/proxy-pool/unbind")
def unbind_proxy_pool(req: dto.ProxyPoolUnbindRequest) -> JSONResponse:
    n = rt.proxy_pool.unbind_accounts(req.emails or [])
    return JSONResponse({"ok": True, "removed": n})


