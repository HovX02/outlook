#!/usr/bin/env python3
"""Outlook API 注册控制台 — FastAPI 入口 + CLI 薄包装。"""
from __future__ import annotations

import base64
import logging
import os
import random
import secrets
import string
import sys
from contextlib import asynccontextmanager

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, Request
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

def _init_auth() -> None:
    username = os.environ.get("WEB_USERNAME")
    password = os.environ.get("WEB_PASSWORD")
    if not username or not password:
        gen_user = "admin"
        gen_pass = "".join(random.choices(string.ascii_letters + string.digits, k=10))
        with open(".env", "a") as f:
            f.write(f"\n# Auto-generated Web UI Auth\n")
            f.write(f"WEB_USERNAME={gen_user}\n")
            f.write(f"WEB_PASSWORD={gen_pass}\n")
        os.environ["WEB_USERNAME"] = gen_user
        os.environ["WEB_PASSWORD"] = gen_pass
        logging.info("Auto-generated Web UI Auth: %s:%s (saved to .env)", gen_user, gen_pass)

_init_auth()

app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("CORS_ALLOW_ORIGINS", "*").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.middleware("http")
async def basic_auth_middleware(request: Request, call_next):
    if request.method == "OPTIONS":
        return await call_next(request)

    path = request.url.path
    if path == "/api/ping":
        return await call_next(request)

    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Basic "):
        return JSONResponse(
            status_code=401,
            content={"detail": "Unauthorized"},
            headers={"WWW-Authenticate": "Basic realm=\"Web Console\""},
        )

    try:
        encoded_credentials = auth_header.split(" ", 1)[1]
        decoded_credentials = base64.b64decode(encoded_credentials).decode("utf-8")
        username, password = decoded_credentials.split(":", 1)
    except Exception:
        return JSONResponse(
            status_code=401,
            content={"detail": "Invalid credentials format"},
            headers={"WWW-Authenticate": "Basic realm=\"Web Console\""},
        )

    correct_username = os.environ.get("WEB_USERNAME", "")
    correct_password = os.environ.get("WEB_PASSWORD", "")

    is_correct_username = secrets.compare_digest(username.encode("utf8"), correct_username.encode("utf8"))
    is_correct_password = secrets.compare_digest(password.encode("utf8"), correct_password.encode("utf8"))

    if not (is_correct_username and is_correct_password):
        return JSONResponse(
            status_code=401,
            content={"detail": "Incorrect username or password"},
            headers={"WWW-Authenticate": "Basic realm=\"Web Console\""},
        )

    return await call_next(request)

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
