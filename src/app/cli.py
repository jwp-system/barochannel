"""서버에서만 하는 관리 작업."""

import argparse
import asyncio
from urllib.parse import urlsplit

import boto3
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from app.core.config import Settings, get_settings
from app.core.db import SessionLocal, engine
from app.seed import EMAIL_DOMAIN, SAMPLE_PASSWORD, seed

LOCAL_HOSTS = {"localhost", "127.0.0.1"}


def reset_refusal(settings: Settings) -> str | None:
    """운영 DB를 지우지 않도록 개발 모드의 로컬 DB만 허용한다."""
    if not settings.debug:
        return "BARO_DEBUG=true일 때만 초기화할 수 있습니다."
    host = urlsplit(settings.database_url.replace("+asyncpg", "")).hostname
    if host not in LOCAL_HOSTS:
        return "로컬 DB만 초기화할 수 있습니다."
    return None


async def drop_everything() -> None:
    async with engine.begin() as connection:
        await connection.execute(text("DROP SCHEMA public CASCADE"))
        await connection.execute(text("CREATE SCHEMA public"))
    await engine.dispose()


def clear_bucket() -> None:
    settings = get_settings()
    client = boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        region_name=settings.s3_region,
    )
    names = {bucket["Name"] for bucket in client.list_buckets()["Buckets"]}
    if settings.s3_bucket not in names:
        return
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=settings.s3_bucket):
        for item in page.get("Contents", []):
            client.delete_object(Bucket=settings.s3_bucket, Key=item["Key"])


async def run_seed() -> None:
    async with SessionLocal() as session:
        await seed(session)
    await engine.dispose()


def reset_dev() -> None:
    asyncio.run(drop_everything())
    clear_bucket()
    command.upgrade(Config("alembic.ini"), "head")
    asyncio.run(run_seed())


def main() -> None:
    parser = argparse.ArgumentParser(description="바로채널 서버 관리 도구")
    parser.add_argument(
        "command",
        choices=["reset-dev"],
        help="reset-dev: 개발 DB를 지우고 샘플 데이터를 넣는다",
    )
    parser.parse_args()
    refusal = reset_refusal(get_settings())
    if refusal:
        parser.error(refusal)
    reset_dev()
    print(f"샘플 데이터를 넣었습니다. 사용자 비밀번호: {SAMPLE_PASSWORD} (@{EMAIL_DOMAIN})")
