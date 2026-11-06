from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEV_SECRET_KEY = "dev-only-secret-key"


class Settings(BaseSettings):
    """환경 차이는 코드가 아니라 BARO_ 환경 변수나 .env 파일로 분리한다."""

    model_config = SettingsConfigDict(env_prefix="BARO_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://barochannel:barochannel@localhost:5433/barochannel"
    db_null_pool: bool = False
    host: str = "127.0.0.1"
    port: int = 8000
    debug: bool = False
    secret_key: str = DEV_SECRET_KEY
    session_max_age_seconds: int = 12 * 60 * 60

    # 시스템 관리자의 로그인 정보는 화면이나 DB가 아니라 환경 변수로만 관리한다.
    admin_email: str = ""
    admin_password: str = ""
    admin_mode_minutes: int = 30

    s3_endpoint_url: str = "http://127.0.0.1:9000"
    s3_access_key: str = "barochannel"
    s3_secret_key: str = "barochannel"
    s3_bucket: str = "barochannel"
    s3_region: str = "us-east-1"
    signed_url_seconds: int = 60

    max_upload_mb: int = 20
    max_files_per_post: int = 10

    @field_validator("admin_email")
    @classmethod
    def normalize_admin_email(cls, value: str) -> str:
        return value.strip().lower()

    @field_validator("admin_password")
    @classmethod
    def check_admin_password(cls, value: str) -> str:
        if value and len(value) < 8:
            raise ValueError("BARO_ADMIN_PASSWORD는 8자 이상이어야 합니다.")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
