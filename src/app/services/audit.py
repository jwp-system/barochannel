from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog


def record_audit(
    session: AsyncSession,
    *,
    actor_id: int | None,
    action: str,
    target_type: str,
    target_id: int,
    **detail: Any,
) -> None:
    """호출한 쪽의 트랜잭션에 함께 들어가 업무 변경과 같이 커밋된다."""
    session.add(
        AuditLog(
            actor_id=actor_id,
            action=action,
            target_type=target_type,
            target_id=target_id,
            detail=detail,
        )
    )
