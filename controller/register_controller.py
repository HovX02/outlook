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
_captcha_provider_configured = rt._captcha_provider_configured
_parse_proxy_selection = rt._parse_proxy_selection
_persist_job = rt._persist_job
_cancel_job = rt._cancel_job

@router.get("/api/register-options")
def get_register_options() -> JSONResponse:
    return JSONResponse(rt._register_options_payload())



@router.post("/api/register")
def start_register(req: dto.RegisterRequest) -> JSONResponse:
    if req.count < 1:
        raise HTTPException(status_code=400, detail="Quantity must be at least 1.")
    route = str(req.execution_route or "protocol").strip().lower()
    route_info = next((r for r in rt.EXECUTION_ROUTES if r["id"] == route), None)
    if route_info is None:
        raise HTTPException(status_code=400, detail=f"Unknown execution route: {route}")
    if not route_info["ready"] and not req.dry_run:
        raise HTTPException(
            status_code=501,
            detail=f"{route_info['label']} is selected, but the corresponding adapter is not yet implemented. Aborting to avoid unintended protocol fallback.",
        )
    req.execution_route = route
    if not req.dry_run and req.count > 20:
        raise HTTPException(status_code=400, detail="Real registration limit is 20 per request. Please batch them.")
    if not req.dry_run:
        provider = rt._resolve_captcha_provider(req.model_dump())
        if not _captcha_provider_meta(provider):
            raise HTTPException(status_code=400, detail=f"Unknown CAPTCHA provider: {provider}")
        if not _captcha_provider_configured(provider):
            meta = _captcha_provider_meta(provider) or {}
            raise HTTPException(
                status_code=400,
                detail=f"Please configure {meta.get('label') or provider} Key in the 'Captcha Services' page first.",
            )
        req.captcha_provider = provider
        legacy_key = (req.captcha_key or "").strip()
        if legacy_key:
            rt.app_db.set_setting("CAPTCHA_RUN_API_KEY", legacy_key)
        use_pool, provider_filter = _parse_proxy_selection(req.model_dump())
        req.use_proxy_pool = use_pool
        if provider_filter:
            req.proxy_provider = provider_filter
        if use_pool:
            stats = rt.proxy_pool.pool_stats(provider=provider_filter)
            if stats.get("enabled", 0) < 1:
                hint = "No available proxies in the pool"
                if provider_filter:
                    hint += f" (Provider: {provider_filter})"
                raise HTTPException(status_code=400, detail=f"{hint}. Please add and enable proxies in the 'Proxy Pool' page first.")
        else:
            proxy = (req.proxy or "").strip()
            if not proxy:
                raise HTTPException(status_code=400, detail="Please select a proxy allocation method.")
            rt.proxy_pool.ensure_templates([proxy], provider="web")
    concurrency = max(1, min(int(req.concurrency or 1), req.count))

    with rt._jobs_lock:
        running_real = [
            j for j in rt._jobs.values() if j.status == "running" and not j.params.get("dry_run")
        ]
        if running_real and not req.dry_run:
            ids = ", ".join(j.batch_label or j.id[:8] for j in running_real)
            raise HTTPException(
                status_code=409,
                detail=f"Registration tasks are already in progress ({ids}). Please wait for them to finish or click 'Stop Task'.",
            )
        job_id = uuid.uuid4().hex[:12]
        params = req.model_dump()
        params["concurrency"] = concurrency
        stored_nos = [int(r.get("batch_no") or 0) for r in rt._load_jobs_store()]
        live_nos = [int(getattr(j, "batch_no", 0) or 0) for j in rt._jobs.values()]
        params["batch_no"] = max(stored_nos + live_nos + [0]) + 1
        params["batch_label"] = rt._make_batch_label(params, params["batch_no"], jobs=rt._jobs)
        job = rt.Job(job_id, params)
        rt._jobs[job_id] = job
        try:
            _persist_job(job)
        except Exception:  # noqa: BLE001
            pass

    threading.Thread(target=rt._job_worker, args=(job_id,), daemon=True).start()
    return JSONResponse(
        {
            "job_id": job_id,
            "count": req.count,
            "concurrency": concurrency,
            "dry_run": req.dry_run,
            "batch_no": params["batch_no"],
            "batch_label": params["batch_label"],
        }
    )



@router.get("/api/jobs")
def list_jobs() -> JSONResponse:
    merged: dict[str, dict[str, Any]] = {}
    for rec in rt._load_jobs_store():
        if rec.get("id"):
            merged[rec["id"]] = rec
    with rt._jobs_lock:
        for j in rt._jobs.values():
            merged[j.id] = j.summary()
    jobs = sorted(merged.values(), key=lambda x: x.get("created_at") or "", reverse=True)
    return JSONResponse({"jobs": jobs})



@router.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> JSONResponse:
    job = rt._jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Task not found")
    return JSONResponse(job.snapshot())



@router.get("/api/jobs/{job_id}/events")
def job_events(job_id: str) -> StreamingResponse:
    job = rt._jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Task not found")

    def gen():
        yield f"data: {json.dumps({'type': 'snapshot', 'snapshot': job.snapshot()}, ensure_ascii=False)}\n\n"
        while True:
            try:
                ev = job._queue.get(timeout=15)
            except queue.Empty:
                yield ": keep-alive\n\n"
                if job.status != "running":
                    break
                continue
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
            if ev.get("type") == "done":
                break

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )



@router.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str) -> JSONResponse:
    with rt._jobs_lock:
        job = rt._jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Task not found or already finished (can only cancel active tasks)")
        if job.status != "running":
            return JSONResponse({"ok": True, "job_id": job_id, "status": job.status, "already": True})
        _cancel_job(job)
        return JSONResponse({"ok": True, "job_id": job_id, "status": job.status})



@router.post("/api/jobs/cancel-running")
def cancel_running_jobs() -> JSONResponse:
    cancelled: list[str] = []
    with rt._jobs_lock:
        for job in list(rt._jobs.values()):
            if job.status == "running" and not job.params.get("dry_run"):
                _cancel_job(job)
                cancelled.append(job.id)
    if not cancelled:
        return JSONResponse({"ok": True, "cancelled": [], "message": "No active tasks"})
    return JSONResponse({"ok": True, "cancelled": cancelled})


# ---------------------------------------------------------------------------
# 账号读取 / 统计 / 导入导出 / 删除 / 元数据
# ---------------------------------------------------------------------------


