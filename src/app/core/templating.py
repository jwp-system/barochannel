from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.core.security import csrf_token
from app.models import CHANNEL_TYPES
from app.services.admin import is_system_admin

APP_DIR = Path(__file__).resolve().parent.parent
LOCAL_TZ = ZoneInfo("Asia/Seoul")

templates = Jinja2Templates(directory=APP_DIR / "templates")


def format_time(value: datetime | None) -> str:
    return value.astimezone(LOCAL_TZ).strftime("%m-%d %H:%M") if value else ""


def format_size(size: int) -> str:
    if size < 1024 * 1024:
        return f"{max(size // 1024, 1)}KB"
    return f"{size / 1024 / 1024:.1f}MB"


def asset(path: str) -> str:
    """파일이 바뀌면 주소도 바뀌게 해서 오래 캐시해도 새 파일을 받게 한다."""
    version = int((APP_DIR / "static" / path).stat().st_mtime)
    return f"/static/{path}?v={version}"


templates.env.filters["time"] = format_time
templates.env.filters["filesize"] = format_size
templates.env.globals["CHANNEL_TYPES"] = CHANNEL_TYPES
templates.env.globals["csrf_token"] = csrf_token
templates.env.globals["asset"] = asset
templates.env.globals["is_system_admin"] = is_system_admin


def render_text(name: str, **context: object) -> str:
    """요청 객체 없이 조각을 만든다. SSE 전송도 같은 템플릿을 쓴다."""
    return templates.env.get_template(name).render(**context)


def fragment(name: str, **context: object) -> HTMLResponse:
    return HTMLResponse(render_text(name, **context))
