"""감사 로그를 사람이 읽는 말로 바꾼다. 로그에는 번호만 있으므로 이름은 보여 줄 때 찾는다."""

import json
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Approval,
    AuditLog,
    Channel,
    Department,
    Post,
    User,
    WorkspaceGroup,
)
from app.models.approval import REVIEW_LABELS

ACTION_LABELS = {
    "admin.pin_change": "PIN 변경",
    "admin.pin_failure": "PIN 틀림",
    "admin.pin_locked": "PIN 잠김",
    "admin.pin_reset": "PIN 초기화",
    "admin.pin_success": "관리자 모드 시작",
    "approval.submit": "상신",
    "approval.approve": "승인",
    "approval.reject": "반려",
    "approval.cancel": "승인 취소",
    "approval.withdraw": "기안 취소",
    "approval.discard": "폐기",
    "approval.receive": "수신 접수",
    "approval.review": "검토",
    "approval.dispatch": "발신",
    "approval.line_change": "승인선 변경",
    "attachment.delete": "첨부 삭제",
    "channel.create": "채널 생성",
    "channel.update": "채널 설정 변경",
    "channel.dispatch_target": "발신 대상 변경",
    "department.create": "소속 추가",
    "department.update": "소속 변경",
    "group.create": "그룹 생성",
    "group.update": "그룹 이름 변경",
    "group.archive": "그룹 보관",
    "group.unarchive": "그룹 보관 해제",
    "group.manager_add": "그룹 관리자 지정",
    "group.manager_remove": "그룹 관리자 해제",
    "mention.ack": "읽음 확인",
    "mention.reconfirm": "재확인 요청",
    "permission.change": "권한 변경",
    "post.delete": "글 삭제",
    "system.seed": "샘플 데이터 생성",
    "user.create": "사용자 등록",
    "user.update": "사용자 변경",
}

# 화면의 종류 선택. 값은 action 앞부분이다.
ACTION_GROUPS: dict[str, tuple[str, tuple[str, ...]]] = {
    "permission.": ("권한 변경", ("permission.",)),
    "approval.": ("결재", ("approval.",)),
    "mention.": ("읽음 확인", ("mention.",)),
    "post.": ("글·첨부 삭제", ("post.", "attachment.")),
    "channel.": ("채널 설정", ("channel.",)),
    "group.": ("그룹", ("group.",)),
    "org.": ("조직", ("user.", "department.")),
    "admin.": ("관리자", ("admin.", "system.")),
}

TARGET_LABELS = {
    "approval": "결재",
    "post": "글",
    "channel": "채널",
    "group": "그룹",
    "user": "사용자",
    "department": "소속",
    "system": "시스템",
}

# 상세 항목 이름과, 값이 번호일 때 가리키는 대상.
FIELDS: dict[str, tuple[str, str | None]] = {
    "user_id": ("사용자", "user"),
    "user_ids": ("사용자", "user"),
    "approvers": ("승인자", "user"),
    "reviewers": ("검토자", "user"),
    "references": ("참조자", "user"),
    "line_changed": ("결재선 직접 수정", None),
    "blocked": ("비활성 승인자", "user"),
    "sent_by": ("보낸 사람", "user"),
    "department_id": ("소속", "department"),
    "departments": ("소속", "department"),
    "parent_id": ("상위 소속", "department"),
    "channel_id": ("채널", "channel"),
    "target_channel_id": ("받는 채널", "channel"),
    "post_id": ("글", "post"),
    "received_post_id": ("받은 글", "post"),
    "received_approval_id": ("받은 결재", "approval"),
    "subject": ("대상", None),
    "name": ("이름", None),
    "email": ("이메일", None),
    "status": ("상태", None),
    "type": ("종류", None),
    "step_no": ("단계", None),
    "comment": ("의견", None),
    "reason": ("사유", None),
    "via": ("경로", None),
    "send_type": ("발신 방식", None),
    "auto_send": ("자동 발신", None),
    "pin_limit": ("고정 글 수", None),
    "sort_order": ("순서", None),
    "users": ("사용자 수", None),
    "read": ("읽기", None),
    "write": ("쓰기", None),
    "reply_read": ("답글 읽기", None),
    "reply_write": ("답글 쓰기", None),
    "before": ("변경 전", None),
    "after": ("변경 후", None),
}

VALUE_LABELS = {
    "allow": "허용",
    "deny": "거부",
    "active": "사용",
    "inactive": "비활성",
    "excel": "엑셀 일괄 등록",
    "manual": "직접",
    "auto": "자동",
    "talk": "대화",
    "vote": "투표",
    "doc": "문서",
    "approval": "결재",
    **REVIEW_LABELS,
}

MODELS: dict[str, tuple[type, Any]] = {
    "user": (User, User.name),
    "department": (Department, Department.name),
    "channel": (Channel, Channel.name),
    "group": (WorkspaceGroup, WorkspaceGroup.name),
    "approval": (Approval, Approval.title),
    "post": (Post, Post.title),
}

# 변경 전후가 번호 목록인 기록. 승인선은 승인자 번호를 순서대로 담는다.
LIST_KINDS = {"approval.line_change": "user"}


def action_label(action: str) -> str:
    return ACTION_LABELS.get(action, action)


def _numbers(value: object) -> list[int]:
    if isinstance(value, bool):
        return []
    if isinstance(value, int):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, int) and not isinstance(item, bool)]
    return []


def _subject(value: object) -> tuple[str, int] | None:
    if isinstance(value, str) and ":" in value:
        kind, _, number = value.partition(":")
        if kind in ("user", "department") and number.isdigit():
            return kind, int(number)
    return None


class Names:
    """번호를 이름으로 바꾼다. 한 화면에 필요한 번호를 모아 종류마다 한 번만 조회한다."""

    def __init__(self) -> None:
        self.wanted: dict[str, set[int]] = {}
        self.found: dict[str, dict[int, str]] = {}

    def want(self, kind: str | None, value: object) -> None:
        if kind in MODELS:
            self.wanted.setdefault(kind, set()).update(_numbers(value))

    def want_detail(self, log: AuditLog) -> None:
        self.want(log.target_type, log.target_id)
        for key, value in (log.detail or {}).items():
            kind = FIELDS.get(key, ("", None))[1]
            self.want(kind, value)
            if subject := _subject(value):
                self.want(*subject)
            if key in ("before", "after"):
                if isinstance(value, dict):
                    for inner, inner_value in value.items():
                        self.want(FIELDS.get(inner, ("", None))[1], inner_value)
                else:
                    self.want(LIST_KINDS.get(log.action), value)

    async def load(self, session: AsyncSession) -> "Names":
        for kind, ids in self.wanted.items():
            model, column = MODELS[kind]
            rows = await session.execute(select(model.id, column).where(model.id.in_(ids)))
            self.found[kind] = {number: name or "" for number, name in rows}
        return self

    def name(self, kind: str | None, number: int) -> str:
        label = TARGET_LABELS.get(kind or "", "")
        found = self.found.get(kind or "", {}).get(number)
        if found:
            return f"{found} (#{number})"
        if kind in MODELS and number not in self.found.get(kind, {}):
            return f"{label} #{number} (없음)"
        return f"{label} #{number}".strip()

    def target(self, log: AuditLog) -> str:
        if log.target_type == "system":
            return "시스템"
        name = self.name(log.target_type, log.target_id)
        label = TARGET_LABELS.get(log.target_type, log.target_type)
        return name if name.startswith(label) else f"{label} · {name}"


def _plain(value: object) -> str:
    if value is None:
        return "없음"
    if isinstance(value, bool):
        return "예" if value else "아니오"
    if isinstance(value, str):
        return VALUE_LABELS.get(value, value) or "(빈 값)"
    if isinstance(value, list) and all(isinstance(item, str | int) for item in value):
        return ", ".join(_plain(item) for item in value) or "없음"
    return json.dumps(value, ensure_ascii=False)


def format_value(names: Names, key: str, value: object, kind: str | None = None) -> str:
    kind = kind or FIELDS.get(key, ("", None))[1]
    if subject := _subject(value):
        return f"{TARGET_LABELS[subject[0]]} {names.name(*subject)}"
    if (
        kind is not None
        and (numbers := _numbers(value))
        and len(numbers) == (len(value) if isinstance(value, list) else 1)
    ):
        return ", ".join(names.name(kind, number) for number in numbers)
    return _plain(value)


@dataclass(frozen=True)
class Change:
    label: str
    before: str
    after: str
    changed: bool


@dataclass(frozen=True)
class Detail:
    fields: list[tuple[str, str]]
    changes: list[Change]
    raw: str


def describe(names: Names, log: AuditLog) -> Detail:
    detail = dict(log.detail or {})
    before, after = detail.pop("before", None), detail.pop("after", None)
    has_change = "before" in (log.detail or {}) or "after" in (log.detail or {})
    fields = [
        (FIELDS.get(key, (key, None))[0], format_value(names, key, value))
        for key, value in detail.items()
    ]
    changes: list[Change] = []
    if isinstance(before, dict) or isinstance(after, dict):
        old, new = before or {}, after or {}
        for key in dict.fromkeys([*old, *new]):
            left = format_value(names, key, old.get(key)) if key in old else "없음"
            right = format_value(names, key, new.get(key)) if key in new else "없음"
            changes.append(Change(FIELDS.get(key, (key, None))[0], left, right, left != right))
    elif has_change:
        kind = LIST_KINDS.get(log.action)
        left = format_value(names, "before", before, kind)
        right = format_value(names, "after", after, kind)
        changes.append(Change("값", left, right, left != right))
    raw = json.dumps(log.detail or {}, ensure_ascii=False, indent=2)
    return Detail(fields, changes, raw)


async def names_for(session: AsyncSession, logs: Iterable[AuditLog], full: bool) -> Names:
    names = Names()
    for log in logs:
        if full:
            names.want_detail(log)
        else:
            names.want(log.target_type, log.target_id)
    return await names.load(session)
