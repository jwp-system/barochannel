"""파일은 S3 API로만 다룬다. MinIO와 네이버클라우드 Object Storage를 설정으로 바꾼다."""

from functools import lru_cache
from pathlib import PurePath
from typing import BinaryIO
from urllib.parse import quote

import boto3
from anyio import to_thread
from botocore.client import BaseClient
from botocore.config import Config
from botocore.exceptions import ClientError

from app.core.config import get_settings

# 표에 없는 확장자는 설정으로 허용해도 미리보기 없이 내려받기만 된다. svg, html도 마찬가지다.
KNOWN_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
    ".txt": "text/plain; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    ".zip": "application/zip",
    ".hwp": "application/x-hwp",
    ".hwpx": "application/hwp+zip",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


def mime_for(file_name: str) -> str | None:
    """브라우저가 보낸 형식을 믿지 않고 허용 확장자로 정한다. 허용되지 않으면 None이다."""
    suffix = PurePath(file_name).suffix.lower()
    if suffix.removeprefix(".") not in get_settings().allowed_extensions:
        return None
    return KNOWN_TYPES.get(suffix, "application/octet-stream")


@lru_cache
def _client() -> BaseClient:
    settings = get_settings()
    return boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        region_name=settings.s3_region,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def _ensure_bucket() -> None:
    bucket = get_settings().s3_bucket
    try:
        _client().head_bucket(Bucket=bucket)
    except ClientError:
        _client().create_bucket(Bucket=bucket)


async def ensure_bucket() -> None:
    await to_thread.run_sync(_ensure_bucket)


async def put_file(key: str, stream: BinaryIO, content_type: str) -> None:
    await to_thread.run_sync(
        lambda: _client().upload_fileobj(
            stream, get_settings().s3_bucket, key, ExtraArgs={"ContentType": content_type}
        )
    )


def signed_download_url(key: str, file_name: str, mime: str, *, inline: bool) -> str:
    settings = get_settings()
    disposition = "inline" if inline else "attachment"
    return _client().generate_presigned_url(
        "get_object",
        Params={
            "Bucket": settings.s3_bucket,
            "Key": key,
            "ResponseContentType": mime,
            "ResponseContentDisposition": f"{disposition}; filename*=UTF-8''{quote(file_name)}",
        },
        ExpiresIn=settings.signed_url_seconds,
    )
