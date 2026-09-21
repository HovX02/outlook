#!/usr/bin/env python3
"""Outlook API 注册控制台 — FastAPI 入口 + CLI 薄包装。"""
from __future__ import annotations

import logging
import os
import sys
from contextlib import asynccontextmanager

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from controller.account_controller import router as account_router
from controller.database_controller import router as database_router
from controller.health_controller import router as health_router
from controller.proxy_controller import router as proxy_router
from controller.register_controller import router as register_router
from controller.rescue_controller import router as rescue_router
from controller.settings_controller import router as settings_router
from controller.verify_controller import router as verify_router
from service.web import runtime as rt

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    rt._startup_log()
    yield


app = FastAPI(title="Outlook API 注册控制台", version="2.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("CORS_ALLOW_ORIGINS", "*").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

for _router in (
    health_router,
    settings_router,
    register_router,
    account_router,
    verify_router,
    rescue_router,
    proxy_router,
    database_router,
):
    app.include_router(_router)


@app.get("/api/ping")
def ping() -> JSONResponse:
    return JSONResponse({"ok": True})


def _run_cli() -> int:
    from service.registration.cli import main as cli_main

    return cli_main()


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] not in ("-m",):
        sys.exit(_run_cli())
    import uvicorn

    uvicorn.run(
        "main:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8890")),
        reload=os.environ.get("RELOAD", "").lower() in ("1", "true", "yes"),
    )
