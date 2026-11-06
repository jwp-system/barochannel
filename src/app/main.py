import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import urlencode

import uvicorn
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import Depends, FastAPI, Request, Response, status
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.core.config import DEV_SECRET_KEY, get_settings
from app.core.db import engine
from app.core.deps import AdminPinRequired, LoginRequired, PinChangeRequired
from app.core.security import verify_csrf
from app.core.sessions import ClockTolerantSessionMiddleware
from app.core.templating import APP_DIR
from app.routers import (
    admin_groups,
    attachments,
    auth,
    events,
    health,
    org,
    pages,
    posts,
    spaces,
)
from app.services.admin import PIN_CHANGE_PATH
from app.services.realtime import hub
from app.services.storage import ensure_bucket

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncGenerator[None]:
    settings = get_settings()
    if not settings.debug and settings.secret_key == DEV_SECRET_KEY:
        raise RuntimeError("운영에서는 BARO_SECRET_KEY를 설정해야 합니다.")
    if not settings.debug and settings.admin_email and not settings.admin_password:
        raise RuntimeError("BARO_ADMIN_EMAIL을 쓰려면 BARO_ADMIN_PASSWORD를 설정해야 합니다.")
    try:
        await ensure_bucket()
    except (BotoCoreError, ClientError) as error:
        logger.warning("파일 저장소를 확인하지 못했습니다: %s", error)
    hub.start()
    yield
    await hub.stop()
    await engine.dispose()


class CachedStaticFiles(StaticFiles):
    """주소에 버전이 들어가므로 브라우저가 다시 묻지 않게 한다."""

    def file_response(self, *args: Any, **kwargs: Any) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


async def login_required(request: Request, _: LoginRequired) -> Response:
    if request.headers.get("HX-Request"):
        return Response(headers={"HX-Redirect": "/login"})
    return RedirectResponse("/login", status.HTTP_303_SEE_OTHER)


async def admin_pin_required(request: Request, _: AdminPinRequired) -> Response:
    target = f"/admin/pin?{urlencode({'next': request.url.path})}"
    if request.headers.get("HX-Request"):
        return Response(headers={"HX-Redirect": target})
    return RedirectResponse(target, status.HTTP_303_SEE_OTHER)


async def pin_change_required(request: Request, _: PinChangeRequired) -> Response:
    if request.headers.get("HX-Request"):
        return Response(headers={"HX-Redirect": PIN_CHANGE_PATH})
    return RedirectResponse(PIN_CHANGE_PATH, status.HTTP_303_SEE_OTHER)


def create_app() -> FastAPI:
    settings = get_settings()
    application = FastAPI(
        title="바로채널",
        lifespan=lifespan,
        dependencies=[Depends(verify_csrf)],
        docs_url="/docs" if settings.debug else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.debug else None,
    )
    application.add_middleware(
        ClockTolerantSessionMiddleware,
        secret_key=settings.secret_key,
        session_cookie="baro_session",
        max_age=settings.session_max_age_seconds,
        same_site="lax",
        https_only=not settings.debug,
    )
    application.add_exception_handler(LoginRequired, login_required)
    application.add_exception_handler(AdminPinRequired, admin_pin_required)
    application.add_exception_handler(PinChangeRequired, pin_change_required)
    application.mount("/static", CachedStaticFiles(directory=APP_DIR / "static"), name="static")
    for module in (health, auth, pages, org, admin_groups, spaces, posts, attachments, events):
        application.include_router(module.router)
    return application


app = create_app()


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
        timeout_graceful_shutdown=5,
        # 기본 5초면 잠시 멈췄다 누를 때마다 연결을 새로 맺는다.
        timeout_keep_alive=65,
    )
