from fastapi import APIRouter, Response, status
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.core.db import SessionDep

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz(response: Response, session: SessionDep) -> dict[str, str]:
    try:
        await session.execute(text("SELECT 1"))
    except (SQLAlchemyError, OSError):
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "unavailable"}
    return {"status": "ok"}
