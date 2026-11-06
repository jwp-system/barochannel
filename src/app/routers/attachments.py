from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from fastapi.responses import RedirectResponse

from app.core.db import SessionDep
from app.core.deps import AuthzDep, current_user
from app.models import Attachment
from app.routers.posts import post_context
from app.services.storage import signed_download_url

router = APIRouter(
    tags=["attachments"], include_in_schema=False, dependencies=[Depends(current_user)]
)


@router.get("/attachments/{attachment_id}")
async def download(
    attachment_id: int,
    session: SessionDep,
    authz: AuthzDep,
    download: Annotated[bool, Query()] = False,
) -> Response:
    """글을 볼 수 있는지 확인한 뒤에만 짧은 유효기간의 서명 URL로 보낸다."""
    attachment = await session.get(Attachment, attachment_id)
    if attachment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "파일을 찾을 수 없습니다.")
    post, *_ = await post_context(session, authz, attachment.post_id)
    if post.deleted_at is not None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "파일을 찾을 수 없습니다.")
    url = signed_download_url(
        attachment.storage_key,
        attachment.file_name,
        attachment.mime,
        inline=attachment.is_image and not download,
    )
    return RedirectResponse(url, status.HTTP_303_SEE_OTHER, headers={"Cache-Control": "no-store"})
