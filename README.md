# 바로채널

조직 기반 권한, 필수 읽음 확인, 순차 결재를 갖춘 사내 소통 시스템이다.

- 기획서: [barochannel.md](barochannel.md)
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

시스템 관리자의 이메일과 비밀번호는 `.env`의 `BARO_ADMIN_EMAIL`, `BARO_ADMIN_PASSWORD`로 정한다. 처음 로그인하면 계정이 만들어지고, 초기 PIN `123456`을 새 PIN으로 바꿔야 다른 화면을 쓸 수 있다. 소속과 사용자, 그룹은 그 뒤 `전체` 메뉴에서 등록한다.

PIN을 잊으면 서버에서 초기 상태로 되돌린다. PIN은 `BARO_SECRET_KEY`와 함께 저장되므로, 이 값을 바꿔도 PIN을 다시 정해야 한다.

```bash
uv run barochannel-admin reset-pin
```

서버는 <http://localhost:8000>에서 열린다. `/healthz`는 데이터베이스 연결 상태를 알려준다.

## 샘플 데이터

개발 DB를 지우고 샘플 데이터를 넣는다. `BARO_DEBUG=true`이고 DB가 로컬일 때만 동작한다. 첨부 저장소도 함께 비운다.

```bash
uv run barochannel-admin reset-dev
```

사람과 회사는 모두 가상이고, 비밀번호는 모두 `baro-sample-1`이다. 시스템 관리자는 `.env`의 계정으로 만들어지며 초기 PIN 상태로 시작한다.

| 계정 | 역할 | 확인할 수 있는 것 |
| --- | --- | --- |
| `seoyeon@baro.example` | 인사팀, 전사 그룹 관리자 | 공지 작성, 건의함 전체 열람 |
| `minjae@baro.example` | 영업본부, 영업 그룹 관리자 | 채널 생성과 권한 설정 |
| `taehyun@baro.example` | 개발본부, 개발 그룹 관리자 | 채널 생성과 권한 설정 |
| `haneul@baro.example` | 영업1팀 | 일반 사용자, 건의함은 자기 글만 열람 |
| `jihoon@baro.example` | 영업1팀 인턴 | 영업 일반 쓰기 거부 (사용자 설정 우선) |
| `subin@baro.example` | 개발팀 | 고객 미팅 읽기 허용 (타 부서 협업자) |

## 역할

| 역할 | 지정 | 할 수 있는 일 |
| --- | --- | --- |
| 시스템 관리자 | `.env` | 조직 관리, 그룹 생성, 그룹 관리자 지정 (PIN 필요) |
| 그룹 관리자 | 시스템 관리자가 지정 | 그룹 안의 채널 생성, 순서, 권한, 글 고정 |
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
| `src/app/cli.py` | PIN 초기화, 개발 DB 초기화 |
| `src/app/seed.py` | 샘플 데이터 |
| `src/app/core/` | 설정, 데이터베이스, 인증 의존성, CSRF, 마크다운, 템플릿 |
| `src/app/models/` | SQLAlchemy 모델 |
| `src/app/services/` | 권한 판정, 감사 로그, 글 화면 모델, 실시간 전달, 파일 저장 |
| `src/app/routers/` | 기능별 라우터. 새 라우터는 `main.py`의 `create_app`에 등록한다 |
| `src/app/templates/`, `src/app/static/` | Jinja2 템플릿, 정적 파일 |
| `migrations/` | Alembic 마이그레이션 |
| `tests/` | 테스트 |

## 개발 규칙

- 권한 판정은 `services/permissions.py` 한 곳에만 둔다. 볼 수 없는 글은 404로 돌려준다.
- 인증이 필요한 라우터는 `current_user` 의존성을 건다. `tests/test_security.py`가 모든 경로를 검사한다.
- 변경 요청은 htmx로 보낸다. CSRF 토큰은 `base.html`의 `hx-headers`가 붙인다.
- 실시간 전달에는 번호만 싣고, 내용은 `services/delivery.py`가 수신자별로 권한을 판정해 만든다.
- 파일은 S3 API로만 다룬다.
- 정적 파일은 템플릿에서 `asset()`으로 연결한다. 주소에 버전이 붙어 1년간 캐시된다.
- SSE는 보이는 탭만 연결한다. 숨겨진 탭까지 연결하면 브라우저의 호스트당 연결 6개를 넘겨 화면이 멈춘다.
- 시스템 관리자 기능은 `AdminUser` 의존성을 건다. 관리자 지정을 DB나 화면으로 옮기지 않는다.
- 화면은 `static/app.css`의 색상 변수를 쓴다. 토스의 설계 원칙은 참고하되 브랜드 자산은 쓰지 않는다.
