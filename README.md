# 바로채널

조직 기반 권한, 필수 읽음 확인, 순차 결재를 갖춘 사내 소통 시스템이다.

- 기획서: [barochannel.md](barochannel.md)
- 업무 채널 기획: [work_channel.md](work_channel.md)
- 업무 연동 개발 가이드: [work_integration_guide.md](work_integration_guide.md)
- 개발 일정: [project_plan.md](project_plan.md)

## 개발 환경

Python 3.12, [uv](https://docs.astral.sh/uv/), Docker가 필요하다.

```bash
uv sync
cp .env.example .env
docker compose up -d
uv run alembic upgrade head
uv run barochannel
```

시스템 관리자의 이메일과 비밀번호는 `.env`의 `BARO_ADMIN_EMAIL`, `BARO_ADMIN_PASSWORD`로 정한다. 처음 로그인하면 계정이 만들어지고, 로그인하면 바로 관리자 모드가 된다. 소속과 사용자, 그룹은 `전체` 메뉴에서 등록한다. 화면 왼쪽 위의 회사명은 `BARO_COMPANY_NAME`으로 정한다.

서버는 <http://localhost:8000>에서 열린다. `/healthz`는 데이터베이스 연결 상태를 알려준다.

## 샘플 할 일 시스템

업무 채널의 외부 연동을 확인하는 별도 서버다. 바로채널 전용 규격 없이 평범한 JSON API이고, 화면은 `src/app/templates/works/todo`에 있다([work_channel.md](work_channel.md)). 바로채널과 다른 터미널에서 띄운다.

```bash
uv run samples/todo_api.py
```

토큰은 `TODO_API_TOKEN`(기본 `dev-todo-token`), 저장 파일은 `TODO_DB`(기본 `todo.db`), 주소와 포트는 `TODO_HOST`(기본 `127.0.0.1`), `TODO_PORT`(기본 `8100`)로 바꾼다. 바로채널의 `.env`에 `BARO_INTEGRATION_ALLOWED_HOSTS=127.0.0.1:8100`이 있어야 부를 수 있다. 샘플 데이터의 영업 그룹 "할 일" 채널에서 해 볼 수 있는 것:

- `/add`: 할 일 등록 화면이 팝업으로 열린다. 등록한 뒤 **남기고 닫기**를 누르면 채널에 메시지가 남고, **닫기**를 누르면 아무것도 남지 않는다.
- 메시지의 **상세 보기**: 할 일 시스템에서 최신 상태를 불러온다. 쓰기 권한자는 **시작·완료·다시 열기**를 누를 수 있고, 처리한 뒤 **남기고 닫기**를 누르면 그 메시지의 쓰레드에 답글로 남는다.
- `/find`: 할 일을 찾아 하나를 고르고 **남기고 닫기**로 채널에 공유한다.
- 서버 훅: 채널 설정에서 `/add`의 **훅 키 발급**을 누르고, 나온 주소와 키로 이 서버를 다시 띄운다. 이 시스템에서 직접 상태를 바꾸면(`PATCH /todos/{id}`에 `{"status": "done", "actor": "정영업"}`, <http://127.0.0.1:8100/docs>) 채널에 메시지가 새로 남는다.

  ```bash
  TODO_WEBHOOK_URL=http://127.0.0.1:8000/hooks/work/1 TODO_WEBHOOK_SECRET=발급받은키 uv run samples/todo_api.py
  ```

## 샘플 데이터

개발 DB를 지우고 샘플 데이터를 넣는다. `BARO_DEBUG=true`이고 DB가 로컬일 때만 동작한다. 첨부 저장소도 함께 비운다.

```bash
uv run barochannel-admin reset-dev
```

샘플 조직은 `src/app/seed_data/`의 엑셀 파일에 있다. 소속·사용자는 `sample_org.xlsx`, 그룹·그룹 관리자는 `sample_groups.xlsx`, 채널·권한은 `sample_channels.xlsx`다. 화면의 엑셀 일괄 등록과 같은 양식과 코드로 읽으므로, 파일을 고치면 샘플 데이터가 바뀐다. 글, 투표, 결재 문서, 결재 채널의 승인선과 양식은 `seed.py`에 있으며 소속·사용자·채널을 이름(사용자는 이메일 앞부분)으로 찾으니 엑셀을 고칠 때 함께 맞춘다.

사람과 회사는 모두 가상이고, 비밀번호는 모두 `qlalfqjsgh-1`이다(한글 자판으로 `비밀번호-1`을 영문 상태에서 친 것). 시스템 관리자는 `.env`의 계정으로 만들어진다.

이메일은 소속 약어와 번호(인사 `hr`, 재무 `fi`, 영업 `sa`, 생산 `pd`, 품질 `qa`), 이름은 성과 소속을 붙인 형태다.

| 계정 | 이름 | 역할 | 확인할 수 있는 것 |
| --- | --- | --- | --- |
| `hr1@baro.example` | 박인사 | 인사팀, 전사 그룹 관리자 | 공지 작성, 건의함 전체 열람 |
| `fi1@baro.example` | 김재무 | 재무팀 | 지출결의 최종 승인 |
| `sa1@baro.example` | 정영업 | 영업본부, 영업 그룹 관리자 | 채널 생성과 권한 설정, 승인선 편집, 지출결의 1차 승인 |
| `sa2@baro.example` | 이영업 | 영업1팀 | 일반 사용자, 건의함은 자기 글만 열람, 지출결의 기안, 할 일 채널 `/add`·`/find` |
| `sa3@baro.example` | 강영업 | 영업1팀 인턴 | 영업 일반 쓰기 거부 (사용자 설정 우선) |
| `sa4@baro.example` | 송영업 | 영업2팀 | 상위 소속(영업본부) 승인선으로 결재 (김재무 단독 승인) |
| `pd1@baro.example` | 임생산 | 생산본부, 생산 그룹 관리자 | 채널 생성과 권한 설정 |
| `pd2@baro.example` | 문생산 | 생산팀 | 고객 미팅 읽기 허용 (타 부서 협업자) |
| `qa1@baro.example` | 배품질 | 품질팀 | 생산 그룹 채널 이용, 작업 표준 문서 작성 |

## 역할

| 역할 | 지정 | 할 수 있는 일 |
| --- | --- | --- |
| 시스템 관리자 | `.env` | 조직 관리, 그룹 생성, 그룹 관리자 지정, 엑셀 일괄 등록 |
| 그룹 관리자 | 시스템 관리자가 지정 | 그룹 안의 채널 생성, 순서, 글 고정, 그룹·채널 권한 설정 (자신 포함), 채널·권한 엑셀 일괄 등록, 승인선 편집, 승인자가 비활성인 결재 폐기 |
| 승인자 | 승인선에 등록, 또는 기안자가 결재선에 추가 | 등록 승인자는 그 결재 채널의 모든 권한, 추가된 승인자는 그 문서만. 승인, 반려, 승인 취소 |
| 검토자 | 기안자가 작성·상신할 때 지정 | 그 문서만 읽기, 답글, 검토 결과 표시 |
| 참조자 | 기안자가 작성·상신할 때 지정 | 상신된 뒤 그 문서만 읽기, 답글 |
| 일반 사용자 | 권한 설정 | 받은 권한 안에서 읽기, 쓰기, 답글 |

## 자주 쓰는 명령

```bash
uv run pytest                                   # 테스트 (docker compose가 떠 있어야 한다)
uv run ruff check . && uv run ruff format .     # 린트와 포맷
uv run alembic revision --autogenerate -m "..." # 마이그레이션 생성
```

## 구조

| 경로 | 내용 |
| --- | --- |
| `src/app/main.py` | 앱 생성, 라우터 등록, 실행 진입점 |
| `src/app/cli.py` | 개발 DB 초기화 |
| `src/app/seed.py` | 샘플 데이터 |
| `src/app/core/` | 설정, 데이터베이스, 인증 의존성, CSRF, 마크다운, 템플릿 |
| `src/app/models/` | SQLAlchemy 모델 |
| `src/app/services/` | 권한 판정, 결재 규칙, 멘션, 감사 로그, 글 화면 모델, 실시간 전달, 파일 저장 |
| `src/app/routers/` | 기능별 라우터. 새 라우터는 `main.py`의 `create_app`에 등록한다 |
| `src/app/templates/`, `src/app/static/` | Jinja2 템플릿, 정적 파일 |
| `migrations/` | Alembic 마이그레이션 |
| `samples/` | 업무 채널 연동 확인용 샘플 할 일 시스템 |
| `tests/` | 테스트 |

## 개발 규칙

- 권한 판정은 `services/permissions.py` 한 곳에만 둔다. 볼 수 없는 글은 404로 돌려준다.
- 인증이 필요한 라우터는 `current_user` 의존성을 건다. `tests/test_security.py`가 모든 경로를 검사한다.
- 변경 요청은 htmx로 보낸다. CSRF 토큰은 `base.html`의 `hx-headers`가 붙인다.
- 실시간 전달에는 번호만 싣고, 내용은 `services/delivery.py`가 수신자별로 권한을 판정해 만든다.
- 파일은 S3 API로만 다룬다.
- 첨부 허용 확장자는 `BARO_ALLOWED_EXTENSIONS`로 정한다. `services/storage.py`의 `KNOWN_TYPES`에 없는 확장자는 내려받기만 된다.
- 첨부는 지우기만 할 수 있다. 저장소의 파일은 지우지 않고 `deleted_at`으로 숨긴다.
- 정적 파일은 템플릿에서 `asset()`으로 연결한다. 주소에 버전이 붙어 1년간 캐시된다.
- SSE는 보이는 탭만 연결한다. 숨겨진 탭까지 연결하면 브라우저의 호스트당 연결 6개를 넘겨 화면이 멈춘다. 보이는 탭은 채널 연결과 개수 연결(`/events/me`)을 함께 쓴다.
- 결재 규칙은 `services/approvals.py`의 판정 함수에 둔다. 버튼 표시와 서버 처리가 같은 함수를 쓴다.
- 결재 처리는 `locked_document`로 결재 행을 잠근 뒤 판정한다. 잠그기 전에 읽은 값으로 판정하지 않는다.
- 임시저장 비공개는 `can_view_post`와 `timeline_posts`가 맡는다. 글을 조회하는 곳을 새로 만들면 이 둘을 지나게 한다.
- 검토자, 참조자, 결재선에 직접 추가된 승인자, 지나간 승인자는 채널이 아니라 문서에만 권한이 있다. 글 하나를 다룰 때는 `post_access`를 쓴다. 이들에게는 채널 실시간 연결을 열지 않는다.
- 채널별 새 글 수는 `services/unread.py`가 센다. 채널의 글만 세고(답글 제외) 소통·문서·투표·업무 채널 중 읽기 권한이 있는 채널만 센다. 처음 보이는 채널은 그때까지 읽은 것으로 시작한다. 개수 연결(`/events/me`)은 지켜보는 채널에 글이 오면 신호만 받고 다시 센다.
- 결재선·검토자·참조자 규칙은 `services/approvals.py`의 `check_plan` 한 곳에서 판정한다. 작성(임시저장)과 상신이 같은 함수를 쓴다.
- 검색은 `services/search.py`가 채널 권한 판정 결과를 SQL 조건으로 옮긴다. 검색에 새 글 종류를 더하면 볼 수 없는 글이 섞이지 않는지 `tests/test_search.py`에 시험을 더한다.
- 결재 발신·수신은 `services/dispatch.py`에 둔다. 한 문서의 발신 사슬은 `root_approval_id`로 묶어 이미 거친 채널로 되돌아가지 않게 한다.
- 문서 채널의 문서는 지울 수 없다(F-43). 글을 지우는 경로를 새로 만들면 `is_document`를 확인한다.
- 엑셀 일괄 등록은 `services/bulk_import.py`(조직, 그룹)와 `services/channel_import.py`(채널, 권한)에 둔다. 채널·권한은 관리하는 그룹 안에서만 바꾸고, 권한은 화면과 같이 `normalize_grant`를 거친다. 모든 행을 먼저 검사하고 문제가 하나라도 있으면 아무것도 바꾸지 않는다. 기존 항목은 적은 칸만 바꾸고, 파일에 없는 항목은 지우지 않는다. 내려받는 파일의 글자 칸은 수식으로 저장하지 않는다.
- 멘션과 읽음 확인은 `services/mentions.py`에 둔다. 내 할 일 개수는 그 목록을 그대로 세어 목록과 숫자가 어긋나지 않게 한다.
- 여러 사람의 권한을 판정할 때는 `access_for_users`를 쓴다. 판정 규칙은 `decide_access` 하나다.
- 시스템 관리자 기능은 `AdminUser` 의존성을 건다. 관리자 지정을 DB나 화면으로 옮기지 않는다.
- 화면은 `static/app.css`의 색상 변수를 쓴다. 토스의 설계 원칙은 참고하되 브랜드 자산은 쓰지 않는다.
- 업무 채널의 화면은 `templates/works/{화면}/`에 코드로 두고 `screen.toml`로 정의한다(앱 시작 때 검사). 화면은 `static/works/baro-work.js`(SDK)로 바로채널의 중계(`/works/{명령어}/api/...`)를 거쳐서만 외부 API를 부르고, CSP가 직접 호출과 인라인 스크립트를 막는다. 중계와 메시지 검사는 `services/work.py`에 둔다. 호출 주소는 `check_endpoint`로 `BARO_INTEGRATION_ALLOWED_HOSTS`의 호스트만 허용하고, `screen.toml`에 적힌 경로만 보내고, 리다이렉트를 따라가지 않는다.
- 업무 메시지(`work_message`)는 남긴 그대로 둔다. 고치는 경로를 만들지 않는다. 같은 `Idempotency-Key`(팝업마다 하나, 훅은 `X-Baro-Event-Id`)로는 한 번만 남긴다. 바로채널은 외부 데이터의 사본을 두지 않는다.
- 시험은 `services.work.transport`에 샘플 할 일 시스템의 ASGI 앱을 끼워 외부 호출을 대신한다.
- 업무 화면을 추가하는 방법과 외부 시스템이 지킬 사항은 [work_integration_guide.md](work_integration_guide.md)에 있다.
